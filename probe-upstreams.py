#!/usr/bin/env python3
"""探测上游：每个模型的可用协议与思考档位。

为什么必须实测：
  - 同一家上游把不同模型归到不同协议，哪些模型只认一种只能实测。
  - 思考档位有白名单，传不支持的档位上游会 400，档位集合各家不同。
  - 上下文长度优先用 models.dev 的官方目录做对照，最终以上游自报的强制值为准。

用法（默认探测 opencode zen/go）:
  python probe-upstreams.py                 # 全量探测，写 upstream-probe-results.json
  python probe-upstreams.py --only deepseek-flash,glm-5.3
  python probe-upstreams.py --skip-effort   # 只测协议，不测档位（快）

探测别家（例如 Cline）:
  python probe-upstreams.py --base-url https://api.cline.bot/api/v1 \
      --key-env CLINE_API_KEY --models-dev-provider "" \
      --only deepseek/deepseek-v4.1-flash --out cline-probe.json

注意：`--base-url` 是**不带**协议后缀的前缀，脚本会拼 /models、/responses、
/chat/completions。opencode 的 base 自带 /go/v1，两种都能用。
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
DEFAULT_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_SESSION = "codex-desktop-local"
DEFAULT_MODELS_DEV_PROVIDER = "opencode-go"


def _env(name: str) -> str:
    """先读进程环境变量；没有则读用户级环境变量（HKCU\\Environment）。

    setx 写的是注册表，已运行的父进程不会自动拿到，所以要补这一步，
    这样密钥始终只存在于用户级环境变量里，不落到任何文件。
    """
    if not name:
        return ""
    val = os.environ.get(name)
    if val:
        return val
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return ""
    return ""


# 这几个由 main() 按命令行参数覆盖
BASE = DEFAULT_BASE
KEY = ""
SESSION = DEFAULT_SESSION
SESSION_HEADER_NAME = "x-opencode-session"
SESSION_HEADER_REQUIRED = True
MODELS_DEV_PROVIDER = DEFAULT_MODELS_DEV_PROVIDER

# 带浏览器 UA：上游挂 Cloudflare，urllib 默认 UA 会被 403
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

EFFORT_CANDIDATES = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_SSL = ssl.create_default_context()


def http(url: str, body: dict | None = None, method: str = "POST",
         timeout: int = 60, retries: int = 3) -> tuple[int | None, str]:
    """发一次请求；403/429/5xx 与 TLS 断连按指数退避重试。"""
    data = json.dumps(body).encode() if body is not None else None
    last = (None, "no attempt")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {KEY}")
        req.add_header("Content-Type", "application/json")
        if SESSION_HEADER_REQUIRED and SESSION_HEADER_NAME:
            req.add_header(SESSION_HEADER_NAME, SESSION)
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            last = (e.code, payload)
            if e.code in (403, 408, 429) or e.code >= 500:
                time.sleep(1.5 * (2 ** attempt))
                continue
            return last
        except Exception as e:                      # TLS 断连 / 超时 / DNS
            last = (None, f"{type(e).__name__}: {e}")
            time.sleep(1.5 * (2 ** attempt))
    return last


def list_models() -> list[str]:
    status, text = http(f"{BASE}/models", method="GET")
    if status != 200:
        raise SystemExit(f"拉取模型列表失败: {status} {text[:300]}")
    return sorted(m["id"] for m in json.loads(text)["data"])


def fetch_models_dev() -> dict:
    """models.dev 是 opencode 自己的模型元数据源，提供官方 context/output 上限。

    别家上游在 models.dev 里可能没有条目，用 --models-dev-provider "" 关掉。
    """
    if not MODELS_DEV_PROVIDER:
        return {}
    req = urllib.request.Request("https://models.dev/api.json")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=90, context=_SSL) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get(MODELS_DEV_PROVIDER) or {}).get("models") or {}


def probe_responses(model: str) -> dict:
    status, text = http(f"{BASE}/responses", {
        "model": model, "input": "ping", "stream": False, "max_output_tokens": 16,
    })
    err = None
    if status != 200:
        try:
            err = json.loads(text).get("error", {}).get("message") or text[:200]
        except Exception:
            err = text[:200]
    return {"ok": status == 200, "status": status, "error": err}


def probe_chat(model: str) -> dict:
    status, text = http(f"{BASE}/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16, "stream": False,
    })
    err = None
    if status != 200:
        try:
            err = json.loads(text).get("error", {}).get("message") or text[:200]
        except Exception:
            err = text[:200]
    return {"ok": status == 200, "status": status, "error": err}


def probe_effort_responses(model: str, level: str) -> tuple[bool, str | None]:
    status, text = http(f"{BASE}/responses", {
        "model": model, "input": "ping", "stream": False, "max_output_tokens": 16,
        "reasoning": {"effort": level},
    })
    if status == 200:
        return True, None
    try:
        return False, json.loads(text).get("error", {}).get("message") or text[:160]
    except Exception:
        return False, text[:160]


def probe_effort_chat(model: str, level: str) -> tuple[bool, str | None]:
    status, text = http(f"{BASE}/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16, "stream": False, "reasoning_effort": level,
    })
    if status == 200:
        return True, None
    try:
        return False, json.loads(text).get("error", {}).get("message") or text[:160]
    except Exception:
        return False, text[:160]


def lookup_models_dev(dev: dict, model: str) -> dict:
    """models.dev 的键有时带 provider 前缀（如 cline-pass/deepseek-v4.1-flash），
    有时不带，这里都试一遍。"""
    for key in (model, model.split("/")[-1]):
        if key in dev:
            return dev[key] or {}
    tail = model.split("/")[-1]
    for k, v in dev.items():
        if k.split("/")[-1] == tail:
            return v or {}
    return {}


def main() -> int:
    global BASE, KEY, SESSION, SESSION_HEADER_NAME, SESSION_HEADER_REQUIRED, MODELS_DEV_PROVIDER

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="逗号分隔的模型 id")
    ap.add_argument("--skip-effort", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out", default="upstream-probe-results.json")
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="不带协议后缀的前缀")
    ap.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="放密钥的用户级环境变量名")
    ap.add_argument("--session-header", default="x-opencode-session:codex-desktop-local",
                    help="额外的会话头 name:value；传空串则不发")
    ap.add_argument("--models-dev-provider", default=DEFAULT_MODELS_DEV_PROVIDER,
                    help="models.dev 里的 provider 键；传空串则不做对照")
    args = ap.parse_args()

    BASE = args.base_url.rstrip("/")
    KEY = _env(args.key_env)
    if args.session_header:
        name, _, value = args.session_header.partition(":")
        SESSION_HEADER_NAME, SESSION, SESSION_HEADER_REQUIRED = name.strip(), value.strip(), True
    else:
        SESSION_HEADER_NAME, SESSION_HEADER_REQUIRED = "", False
    MODELS_DEV_PROVIDER = args.models_dev_provider

    if not KEY:
        print(f"缺少环境变量 {args.key_env}", file=sys.stderr)
        return 1

    print(f"上游: {BASE}")
    print(f"密钥: {args.key_env}")
    print(f"会话头: {SESSION_HEADER_NAME or '(不发)'}")
    live = list_models()
    if args.only:
        want = {m.strip() for m in args.only.split(",") if m.strip()}
        live = [m for m in live if m in want]
    print(f"线上模型 {len(live)} 个")

    try:
        dev = fetch_models_dev()
        print(f"models.dev opencode-go: {len(dev)} 个模型（用于 context/output 上限）")
    except Exception as e:
        dev = {}
        print(f"models.dev 拉取失败（继续，元数据留空）: {e}")

    results: dict[str, dict] = {}

    def work(model: str) -> tuple[str, dict]:
        entry: dict = {"model": model}
        r = probe_responses(model)
        c = probe_chat(model)
        entry["responses"] = r
        entry["chat"] = c
        meta = lookup_models_dev(dev, model)
        entry["models_dev"] = {
            "name": meta.get("name"),
            "context": (meta.get("limit") or {}).get("context"),
            "output": (meta.get("limit") or {}).get("output"),
            "reasoning": meta.get("reasoning"),
            "attachment": meta.get("attachment"),
            "tool_call": meta.get("tool_call"),
            "in_models_dev": bool(meta),
        }
        if not args.skip_effort:
            eff: dict = {}
            if c["ok"]:
                for lv in EFFORT_CANDIDATES:
                    ok, err = probe_effort_chat(model, lv)
                    eff.setdefault("chat", {})[lv] = {"ok": ok, "error": err}
            if r["ok"]:
                for lv in EFFORT_CANDIDATES:
                    ok, err = probe_effort_responses(model, lv)
                    eff.setdefault("responses", {})[lv] = {"ok": ok, "error": err}
            entry["effort"] = eff
        return model, entry

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (model, entry) in enumerate(pool.map(work, live), 1):
            results[model] = entry
            wires = "".join(k for k, v in
                            (("R", entry["responses"]), ("C", entry["chat"])) if v["ok"]) or "-"
            print(f"  [{i:>2}/{len(live)}] {model:<34} wire={wires:<2} "
                  f"ctx={entry['models_dev']['context']}")
    elapsed = time.time() - t0

    payload = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": BASE,
        "elapsed_sec": round(elapsed, 1),
        "effort_candidates": EFFORT_CANDIDATES,
        "models": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n完成，用时 {elapsed:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
