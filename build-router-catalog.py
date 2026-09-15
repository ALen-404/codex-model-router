#!/usr/bin/env python3
"""从 model-metadata.json 生成路由表与 Codex 模型目录。

产物:
  router-routes.json    给 router.py 用：slug -> 上游地址/密钥环境变量/wire/档位
  model-catalog.json    给 Codex 的 model_catalog_json 用：驱动「选择模型」列表

base_instructions / model_messages 从现有目录文件（cc-switch-model-catalog.json）
复制，保证不引入第二套提示词写法。

用法:
  python build-router-catalog.py
  python build-router-catalog.py --template ~/.codex/cc-switch-model-catalog.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = Path.home() / ".codex" / "cc-switch-model-catalog.json"

EFFORT_DESCRIPTIONS = {
    "none": "关闭思考：直接作答，最快、最省 token",
    "minimal": "最低强度思考：只做最少推理",
    "low": "轻量思考：快速响应优先",
    "medium": "中等思考：速度与深度平衡（默认）",
    "high": "高强度思考：复杂问题",
    "xhigh": "极高强度思考：难题攻坚",
    "max": "最高强度思考：允许最长推理预算",
}

# Codex 侧 serde 只认这三个（实测报错原文：expected one of `list`, `hide`, `none`）。
# 写成 "hidden" 之类会让整个 config_load 失败 —— 不只是目录不可用，是 Codex 起不来。
VISIBILITY_VARIANTS = {"list", "hide", "none"}
SHELL_TYPES = {"unified_exec", "shell_command", "local_shell"}


def _no_duplicate_keys(pairs):
    """json 解析钩子：发现重复键直接报错。

    Python 的 json 默认允许重复键（后者覆盖前者），但 Codex 用的 Rust serde 会
    直接拒绝 `duplicate field`，所以必须在写盘前就拦住。
    """
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"重复的 JSON 键: {k!r}")
        seen.add(k)
    return dict(pairs)


def validate_catalog(path: Path) -> list[str]:
    """严格校验目录文件：合法 JSON、无重复键、枚举值合法、必需字段齐全。"""
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as e:
        return [f"JSON 不合法: {e}"]
    models = (data or {}).get("models")
    if not isinstance(models, list) or not models:
        return ["缺少 models 数组或为空"]
    for m in models:
        slug = m.get("slug") or "<无 slug>"
        vis = m.get("visibility")
        if vis not in VISIBILITY_VARIANTS:
            problems.append(f"{slug}: visibility={vis!r} 非法，只能是 {sorted(VISIBILITY_VARIANTS)}")
        shell = m.get("shell_type")
        if shell not in SHELL_TYPES:
            problems.append(f"{slug}: shell_type={shell!r} 非法，只能是 {sorted(SHELL_TYPES)}")
        for field in ("context_window", "max_context_window", "supported_reasoning_levels"):
            if not m.get(field):
                problems.append(f"{slug}: 缺少 {field}")
    return problems


def load_template(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        models = (json.load(fh) or {}).get("models") or []
    return models[0] if models else {}


def build_catalog_model(meta: dict, template: dict) -> dict:
    levels = meta.get("supported_reasoning_levels") or []
    visibility = meta.get("visibility", "list")
    if visibility not in VISIBILITY_VARIANTS:
        raise SystemExit(
            f"模型 {meta.get('slug')} 的 visibility={visibility!r} 非法；"
            f"只能是 {sorted(VISIBILITY_VARIANTS)}（写错会让 Codex config_load 直接失败）")
    shell_type = meta.get("shell_type", "unified_exec")
    if shell_type not in SHELL_TYPES:
        raise SystemExit(f"模型 {meta.get('slug')} 的 shell_type={shell_type!r} 非法")
    entry = {
        "slug": meta["slug"],
        "display_name": meta["display_name"],
        "description": meta["description"],
        "context_window": meta["context_window"],
        "max_context_window": meta["max_context_window"],
        "default_reasoning_level": meta.get("default_reasoning_level", "medium"),
        "supported_reasoning_levels": [
            {"effort": lv, "description": EFFORT_DESCRIPTIONS.get(lv, lv)} for lv in levels
        ],
        "input_modalities": meta.get("input_modalities", ["text"]),
        "truncation_policy": meta.get("truncation_policy", {"mode": "tokens", "limit": 10000}),
        "supports_parallel_tool_calls": bool(meta.get("supports_parallel_tool_calls", True)),
        "supports_search_tool": bool(meta.get("supports_search_tool", False)),
        "supports_reasoning_summaries": bool(meta.get("supports_reasoning_summaries", True)),
        "shell_type": shell_type,
        # 用量显示需要分母：context_window 就是分母，effective_context_window_percent 决定预警阈值
        "effective_context_window_percent": meta.get("effective_context_window_percent", 95),
        "visibility": visibility,
        "priority": meta.get("priority", 100),
        "supported_in_api": True,
        "supports_image_detail_original": True,
        "apply_patch_tool_type": template.get("apply_patch_tool_type", "freeform"),
        "default_reasoning_summary": "none",
        "default_verbosity": template.get("default_verbosity", "low"),
        "support_verbosity": bool(template.get("support_verbosity", True)),
        "web_search_tool_type": template.get("web_search_tool_type", "text_and_image"),
        "service_tiers": [],
        "additional_speed_tiers": [],
        "experimental_supported_tools": [],
        "availability_nux": None,
        "upgrade": None,
    }
    for key in ("base_instructions", "model_messages"):
        if template.get(key):
            entry[key] = template[key]
    return entry


def build_routes(metadata: dict) -> dict:
    providers = metadata.get("providers") or {}
    routes: dict[str, dict] = {}
    for meta in metadata.get("models") or []:
        prov = providers.get(meta["provider"])
        if prov is None:
            raise SystemExit(f"模型 {meta['slug']} 引用了不存在的 provider {meta['provider']}")
        routes[meta["slug"]] = {
            "upstream_base": prov["upstream_base"],
            "path": meta.get("path") or ("/responses" if meta.get("wire") == "responses"
                                         else "/chat/completions"),
            "key_env": prov["key_env"],
            "upstream_model": meta["upstream_model"],
            "wire": meta.get("wire", "responses"),
            "effort_levels": meta.get("supported_reasoning_levels", []),
            "session_header": prov.get("session_header"),
            "display_name": meta["display_name"],
            "context_window": meta.get("context_window"),
            "max_output_tokens": meta.get("max_output_tokens"),
        }
    return routes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", default=str(HERE / "model-metadata.json"))
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    ap.add_argument("--out-dir", default=str(HERE))
    args = ap.parse_args()

    with open(args.metadata, encoding="utf-8") as fh:
        metadata = json.load(fh)

    template = load_template(Path(args.template))
    if template:
        print(f"模板: {args.template} （取 base_instructions / model_messages）")
    else:
        print(f"警告: 模板 {args.template} 不可用，目录将缺少 base_instructions", file=sys.stderr)

    models = [build_catalog_model(m, template) for m in metadata["models"]]
    catalog = {"models": models}
    routes = {
        "version": 1,
        "host": "127.0.0.1",
        "port": 8791,
        "generated_by": "build-router-catalog.py",
        "routes": build_routes(metadata),
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "model-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "router-routes.json").write_text(
        json.dumps(routes, ensure_ascii=False, indent=2), encoding="utf-8")

    listed = [m for m in models if m["visibility"] == "list"]
    hidden = [m for m in models if m["visibility"] != "list"]
    print(f"\n目录: {len(models)} 个模型（列表可见 {len(listed)}，隐藏 {len(hidden)}）")
    for m in models:
        flag = "list" if m["visibility"] == "list" else "hide"
        print(f"  [{flag}] {m['slug']:<32} ctx={m['context_window']:>9}  "
              f"levels={len(m['supported_reasoning_levels'])}  "
              f"modalities={','.join(m['input_modalities'])}")
    print(f"\n路由表: {len(routes['routes'])} 条")
    for slug, r in routes["routes"].items():
        print(f"  {slug:<32} wire={r['wire']:<9} {r['upstream_base']}{r['path']}")
    print(f"\n已写出:\n  {out / 'model-catalog.json'}\n  {out / 'router-routes.json'}")

    problems = validate_catalog(out / "model-catalog.json")
    if problems:
        print("\n目录校验失败:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print("目录校验通过：枚举值合法、无重复键")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
