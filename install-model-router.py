#!/usr/bin/env python3
"""幂等安装本地模型路由器。

做什么:
  1. 把 router.py / translate.py / router-routes.json / model-catalog.json 复制到
     ~/.codex/model-router/
  2. 外科式修改 ~/.codex/config.toml（只动必要的键，保留你原有的 provider 和其他配置）：
       model_provider = "local-router"
       model          = <默认模型>
       model_catalog_json = "<...>/model-catalog.json"
       [model_providers.local-router]  base_url=http://127.0.0.1:8791/v1, wire_api="responses"
       [desktop] show-context-window-usage = true
  3. 只有在「路由代码或路由表」变化时才重启路由器；仅目录更新不重启
  4. 每次安装前备份 config.toml，改动清单写入 install-state.json，支持 --rollback

用法:
  python install-model-router.py              # 安装/更新
  python install-model-router.py --dry-run    # 只看会改什么
  python install-model-router.py --rollback   # 还原 config.toml 并停掉路由器
  python install-model-router.py --status     # 显示当前状态
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG = CODEX_HOME / "config.toml"
ROUTER_DIR = CODEX_HOME / "model-router"
STATE_FILE = ROUTER_DIR / "install-state.json"
PID_FILE = ROUTER_DIR / "router.pid"

PROVIDER_ID = "local-router"
DEFAULT_MODEL = "deepseek-v4.1-flash-opencode"
ROUTER_PORT = 8791
ROUTER_URL = f"http://127.0.0.1:{ROUTER_PORT}/v1"

COPY_FILES = ["router.py", "translate.py", "router-routes.json", "model-catalog.json"]
# 这些文件变化才需要重启路由器；model-catalog.json 不在其中（目录热加载，Codex 自己读）
RESTART_TRIGGERS = ["router.py", "translate.py", "router-routes.json"]

PROVIDER_BLOCK = f"""[model_providers.{PROVIDER_ID}]
name = "Local Model Router"
base_url = "{ROUTER_URL}"
wire_api = "responses"
requires_openai_auth = true
"""


# --------------------------------------------------------------------------- #
# TOML 外科编辑
# --------------------------------------------------------------------------- #

def split_sections(lines: list[str]) -> tuple[list[str], list[str]]:
    """返回 (顶层行, 其余行)。"""
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return lines[:i], lines[i:]
    return lines, []


def top_level_lines(lines: list[str]) -> list[str]:
    return split_sections(lines)[0]


def set_top_level(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    """在顶层设置 key = value；已存在则替换。返回 (新行, 是否变化)。"""
    top, rest = split_sections(lines)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(top):
        if pattern.match(line):
            new = f"{key} = {value}"
            if line.strip() == new:
                return lines, False
            top[i] = new
            return top + rest, True
    top.append(f"{key} = {value}")
    return top + rest, True


def section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """定位 [section] 的行号区间 [start, end)（含子表如 [section.sub]）。"""
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == header:
            start = i
        elif start is not None and s.startswith("[") and not s.startswith(f"[{section}."):
            return start, i
    return (start, len(lines)) if start is not None else None


def set_section_key(lines: list[str], section: str, key: str, value: str) -> tuple[list[str], bool]:
    """在 [section] 内设置 key；表不存在则追加整段。"""
    bounds = section_bounds(lines, section)
    if bounds is None:
        lines = lines + [f"[{section}]", f"{key} = {value}"]
        return lines, True
    start, end = bounds
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            new = f"{key} = {value}"
            if lines[i].strip() == new:
                return lines, False
            lines[i] = new
            return lines, True
    lines.insert(start + 1, f"{key} = {value}")
    return lines, True


def ensure_provider_block(lines: list[str]) -> tuple[list[str], bool]:
    if section_bounds(lines, f"model_providers.{PROVIDER_ID}") is not None:
        return lines, False
    block = PROVIDER_BLOCK.rstrip("\n").split("\n")
    # 插到最后一个 [model_providers.*] 段的末尾，保持同类配置聚在一起
    starts = [i for i, line in enumerate(lines)
              if line.strip().startswith("[model_providers.")]
    if not starts:
        return lines + [""] + block, True
    last_start = starts[-1]
    insert_at = len(lines)
    for i in range(last_start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            insert_at = i
            break
    return lines[:insert_at] + block + [""] + lines[insert_at:], True


# --------------------------------------------------------------------------- #
# 路由器进程管理
# --------------------------------------------------------------------------- #

def router_healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def router_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, errors="replace").stdout
        return pid if str(pid) in out else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def stop_router() -> bool:
    pid = router_pid()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return False
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    time.sleep(0.8)
    return True


def port_owner() -> int | None:
    """谁在监听 ROUTER_PORT —— 用来修复过期的 pid 文件。

    手工 `python router.py` 启动的实例不会写 pid 文件，此时 pid 文件是脏的，
    会让 stop_router 误判（杀不掉活的、又启一个抢不到端口）。
    """
    if sys.platform != "win32":
        return None
    cmd = ("(Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction "
           "SilentlyContinue | Select-Object -First 1).OwningProcess" % ROUTER_PORT)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                         capture_output=True, text=True, errors="replace").stdout.strip()
    return int(out) if out.isdigit() and int(out) > 0 else None


def sync_pid_file() -> None:
    """以端口实际占用者为准修正 pid 文件（手工启动的实例不会写它）。"""
    if not router_healthy():
        return
    owner = port_owner()
    if owner and router_pid() != owner:
        PID_FILE.write_text(str(owner), encoding="utf-8")
        print(f"pid 文件已修正为 {owner}")


def start_router() -> bool:
    """启动路由器。

    必须用 CREATE_BREAKAWAY_FROM_JOB：如果父进程处在 Job Object 里（比如从脚本/CI
    会话里拉起），普通 DETACHED_PROCESS 会随父进程一起被杀，路由器就会莫名死掉
    —— 这正是之前 Codex 收到 502 的原因。
    """
    # 已经有实例在服务就别再起一个（会抢不到端口，还会留下脏 pid 文件）
    if router_healthy():
        owner = port_owner()
        if owner and router_pid() != owner:
            PID_FILE.write_text(str(owner), encoding="utf-8")
            print(f"检测到已有实例在运行，pid 文件已修正为 {owner}")
        return True
    script = ROUTER_DIR / "router.py"
    log = ROUTER_DIR / "router.out.log"
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    flags = base | (subprocess.CREATE_BREAKAWAY_FROM_JOB if sys.platform == "win32" else 0)
    with log.open("ab") as fh:
        try:
            proc = subprocess.Popen([sys.executable, str(script)], cwd=str(ROUTER_DIR),
                                    stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                                    creationflags=flags, close_fds=True)
        except OSError:
            proc = subprocess.Popen([sys.executable, str(script)], cwd=str(ROUTER_DIR),
                                    stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                                    creationflags=base, close_fds=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    for _ in range(30):
        if router_healthy():
            return True
        time.sleep(0.3)
    return False


def autostart_path() -> Path:
    return (Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            / "Microsoft/Windows/Start Menu/Programs/Startup/CodexModelRouter.cmd")


def ensure_autostart() -> str | None:
    """写一个开机自启项（当前用户 Startup 文件夹，不需要管理员权限）。

    schtasks 建任务需要管理员，所以用 Startup 文件夹这个用户级方案。
    """
    target = autostart_path()
    content = (
        "@echo off\r\n"
        "rem Codex 模型路由器自启项（由 install-model-router.py 生成）\r\n"
        f'start "" /b "{sys.executable}" "{ROUTER_DIR / "router.py"}"\r\n'
    )
    if target.exists() and target.read_text(encoding="utf-8", errors="replace") == content:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target)


def remove_autostart() -> bool:
    target = autostart_path()
    if target.exists():
        target.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# 安装主流程
# --------------------------------------------------------------------------- #

VISIBILITY_VARIANTS = {"list", "hide", "none"}
SHELL_TYPES = {"unified_exec", "shell_command", "local_shell"}


def _no_duplicate_keys(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"重复的 JSON 键: {k!r}")
        seen.add(k)
    return dict(pairs)


def validate_catalog(path: Path) -> list[str]:
    """装之前先验：Codex 的 serde 对目录很挑，坏目录会让整个 config_load 失败。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_no_duplicate_keys)
    except (ValueError, OSError) as e:
        return [f"{path.name} JSON 不合法: {e}"]
    models = (data or {}).get("models")
    if not isinstance(models, list) or not models:
        return [f"{path.name} 缺少 models 数组或为空"]
    problems = []
    for m in models:
        slug = m.get("slug") or "<无 slug>"
        if m.get("visibility") not in VISIBILITY_VARIANTS:
            problems.append(f"{slug}: visibility={m.get('visibility')!r} 非法 "
                            f"(只能 {sorted(VISIBILITY_VARIANTS)})")
        if m.get("shell_type") not in SHELL_TYPES:
            problems.append(f"{slug}: shell_type={m.get('shell_type')!r} 非法")
        for field in ("context_window", "max_context_window", "supported_reasoning_levels"):
            if not m.get(field):
                problems.append(f"{slug}: 缺少 {field}")
    return problems


def digest(names: list[str], base: Path | None = None) -> str:
    """对一组文件求指纹；默认对「已安装」副本求，dry-run 时对源目录求。"""
    h = hashlib.sha256()
    base = base or ROUTER_DIR
    for n in names:
        p = base / n
        h.update(n.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def cmd_status() -> int:
    state = load_state()
    print(f"配置      {CONFIG}")
    print(f"路由器目录 {ROUTER_DIR}")
    print(f"健康      {'是' if router_healthy() else '否'}")
    print(f"PID       {router_pid()}")
    print(f"开机自启   {'已装: ' + str(autostart_path()) if autostart_path().exists() else '未装'}")
    print(f"重启指纹   {state.get('restart_digest', '<无>')[:16]}")
    print(f"当前指纹   {digest(RESTART_TRIGGERS, HERE)[:16]}")
    print(f"备份      {state.get('config_backup', '<无>')}")
    return 0


def cmd_rollback() -> int:
    state = load_state()
    backup = state.get("config_backup")
    if not backup or not Path(backup).exists():
        print("找不到备份，无法回滚", file=sys.stderr)
        return 1
    shutil.copy2(backup, CONFIG)
    print(f"已还原 config.toml <- {backup}")
    if stop_router():
        print("已停止路由器")
    if remove_autostart():
        print("已移除开机自启项")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--no-autostart", action="store_true",
                    help="不写开机自启项（Startup 文件夹）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="设为默认模型")
    args = ap.parse_args()

    if args.status:
        return cmd_status()
    if args.rollback:
        return cmd_rollback()

    missing = [f for f in COPY_FILES if not (HERE / f).exists()]
    if missing:
        print(f"缺少产物: {missing}；先跑 build-router-catalog.py", file=sys.stderr)
        return 1
    if not CONFIG.exists():
        print(f"找不到 {CONFIG}", file=sys.stderr)
        return 1

    problems = validate_catalog(HERE / "model-catalog.json")
    if problems:
        print("待安装的目录校验失败，拒绝安装（坏目录会让 Codex 起不来）:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("目录校验通过（枚举值合法、无重复键）")
    catalog = json.loads((HERE / "model-catalog.json").read_text(encoding="utf-8"))
    known_slugs = {m.get("slug") for m in catalog.get("models") or []}
    if args.model not in known_slugs:
        print(f"默认模型 {args.model!r} 不在目录里；可选: {sorted(known_slugs)}", file=sys.stderr)
        return 1

    # 1) 决定是否需要重启：只比路由代码/路由表（源目录 vs 上次安装时记录的指纹）
    state = load_state()
    new_digest = digest(RESTART_TRIGGERS, HERE)
    restart_needed = new_digest != state.get("restart_digest")

    # 2) 改 config.toml（先在内存里算出目标内容，dry-run 也能看到）
    original = CONFIG.read_text(encoding="utf-8")
    lines = original.split("\n")
    changed_keys: list[str] = []

    lines, c = set_top_level(lines, "model_provider", f'"{PROVIDER_ID}"')
    if c:
        changed_keys.append("model_provider")
    # 不覆盖用户已经在用的模型：只在缺失、或当前值不在目录里时才写默认值
    current_model = None
    for line in top_level_lines(lines):
        m = re.match(r'^\s*model\s*=\s*"([^"]+)"', line)
        if m:
            current_model = m.group(1)
            break
    if current_model in known_slugs:
        pass
    else:
        lines, c = set_top_level(lines, "model", f'"{args.model}"')
        if c:
            changed_keys.append("model")
    catalog_path = (ROUTER_DIR / "model-catalog.json").as_posix()
    lines, c = set_top_level(lines, "model_catalog_json", f'"{catalog_path}"')
    if c:
        changed_keys.append("model_catalog_json")
    lines, c = ensure_provider_block(lines)
    if c:
        changed_keys.append(f"model_providers.{PROVIDER_ID}")
    lines, c = set_section_key(lines, "desktop", "show-context-window-usage", "true")
    if c:
        changed_keys.append("desktop.show-context-window-usage")

    new_text = "\n".join(lines)

    print(f"路由器目录 {ROUTER_DIR}")
    print(f"配置改动   {changed_keys or '（无，已是目标状态）'}")
    print(f"路由器重启 {'需要' if restart_needed else '不需要（仅目录/无变化）'}")
    if args.dry_run:
        print("\n[dry-run] 未写入任何文件（含路由器目录）")
        print(f"  会复制: {', '.join(COPY_FILES)}")
        return 0

    # 3) 落盘：先复制路由器文件
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    for name in COPY_FILES:
        shutil.copy2(HERE / name, ROUTER_DIR / name)

    if changed_keys:
        backup = CONFIG.with_suffix(f".toml.bak-router-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(CONFIG, backup)
        CONFIG.write_text(new_text, encoding="utf-8")
        state["config_backup"] = str(backup)
        print(f"已备份     {backup.name}")

    state.update({
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "restart_digest": new_digest,
        "catalog_digest": digest(["model-catalog.json"]),
        "provider_id": PROVIDER_ID,
        "base_url": ROUTER_URL,
        "default_model": args.model,
        "config_backup": state.get("config_backup"),
    })
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 按需重启
    if not args.no_autostart:
        written = ensure_autostart()
        if written:
            print(f"已装开机自启 {written}")
    if restart_needed and not args.no_start:
        stop_router()
        if start_router():
            print(f"路由器已启动 (pid {router_pid()})")
        else:
            print("路由器启动失败，见 router.out.log", file=sys.stderr)
            return 1
    elif not router_healthy() and not args.no_start:
        if start_router():
            print(f"路由器未运行，已启动 (pid {router_pid()})")
        else:
            print("路由器启动失败，见 router.out.log", file=sys.stderr)
            return 1
    else:
        print("路由器保持运行（未重启）")
        sync_pid_file()

    print("\n装好了。重启 Codex 桌面端即可在「选择模型」里看到 DeepSeek 系列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
