#!/usr/bin/env python3
"""本地模型路由器：把 Codex 的 Responses 请求按路由表转发给上游。

为什么需要它：Codex 的一个 provider 只能绑定一种 wire 协议，而多个上游协议/模型
混在一个「选择模型」列表里就必须有一个中间层来分发。Codex 只跟本进程说话
（wire_api="responses"），本进程按请求体里的 model 决定去哪、用什么协议。

用法:
  python router.py                    # 前台运行，监听 127.0.0.1:8791
  python router.py --port 8791
  python router.py --selftest         # 不起服务，只校验路由表与密钥可读

端点:
  POST /v1/responses   Codex 主通道（也接受 /responses）
  GET  /v1/models      模型列表（来自路由表）
  GET  /healthz        健康检查（含上游可达性）

只用标准库，无第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import translate  # noqa: E402

ROUTER_DIR = Path(os.environ.get("CODEX_ROUTER_DIR", Path.home() / ".codex" / "model-router"))
ROUTES_FILE = ROUTER_DIR / "router-routes.json"
LOG_FILE = ROUTER_DIR / "router.log"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_SSL = ssl.create_default_context()
_REASONING_CACHE = translate.ReasoningCache()
_LOG_LOCK = threading.Lock()


def log(level: str, message: str, **fields: Any) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": level,
                       "msg": message, **fields}, ensure_ascii=False)
    with _LOG_LOCK:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    if level in ("ERROR", "WARN"):
        print(line, file=sys.stderr, flush=True)


def read_env(name: str) -> str:
    """先读进程环境变量，再读用户级环境变量（setx 写的是注册表）。"""
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


def load_routes() -> dict:
    if not ROUTES_FILE.exists():
        raise SystemExit(f"找不到路由表: {ROUTES_FILE}")
    with ROUTES_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def _headers(route: dict, key: str) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": route.get("user_agent") or DEFAULT_UA,
    }
    sess = route.get("session_header")
    if isinstance(sess, dict) and sess.get("name"):
        h[sess["name"]] = str(sess.get("value") or "codex-desktop-local")
    for k, v in (route.get("extra_headers") or {}).items():
        h[str(k)] = str(v)
    return h


def open_upstream(url: str, body: dict, route: dict, key: str,
                  retries: int = 3, timeout: int = 900):
    """建立上游连接。403/408/429/5xx 与 TLS 断连按指数退避重试。

    返回 (response, None) 或 (None, (status, text))。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_status, last_text = None, "no attempt"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in _headers(route, key).items():
            req.add_header(k, v)
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_SSL), None
        except urllib.error.HTTPError as e:
            last_text = e.read().decode("utf-8", "replace")
            last_status = e.code
            if e.code in (403, 408, 429) or e.code >= 500:
                wait = 1.5 * (2 ** attempt)
                log("WARN", "上游返回可重试状态，退避重试",
                    status=e.code, attempt=attempt, wait_sec=round(wait, 1), url=url)
                time.sleep(wait)
                continue
            return None, (e.code, last_text)
        except Exception as e:                      # TLS 断连 / 超时 / DNS
            last_text = f"{type(e).__name__}: {e}"
            last_status = None
            wait = 1.5 * (2 ** attempt)
            log("WARN", "上游连接异常，退避重试", error=last_text[:200],
                attempt=attempt, wait_sec=round(wait, 1), url=url)
            time.sleep(wait)
    return None, (last_status, last_text)


def effective_route(body: dict, routes: dict) -> tuple[dict | None, str]:
    model = body.get("model") or ""
    route = routes.get(model)
    if route is None:
        return None, f"未知模型 {model!r}；路由表里没有这个 slug"
    return route, model


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-model-router/1.0"

    # -- 基础设施 ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:   # 静默默认访问日志
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _json(self, status: int, payload: dict) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(blob)
        self.close_connection = True

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self) -> None:                                  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/healthz", "/health"):
            cfg = self.server.routes_config                       # type: ignore[attr-defined]
            routes = cfg.get("routes") or {}
            probe = {}
            for slug, route in routes.items():
                key = read_env(route.get("key_env") or "")
                probe[slug] = {"key_env": route.get("key_env"),
                               "key_present": bool(key),
                               "upstream_base": route.get("upstream_base"),
                               "wire": route.get("wire")}
            self._json(200, {"status": "ok", "port": self.server.server_port,  # type: ignore[attr-defined]
                             "routes": len(routes), "models": probe})
            return
        if path in ("/v1/models", "/models"):
            cfg = self.server.routes_config                       # type: ignore[attr-defined]
            data = [{"id": slug, "object": "model", "owned_by": "codex-model-router"}
                    for slug in sorted((cfg.get("routes") or {}))]
            self._json(200, {"object": "list", "data": data})
            return
        self._json(404, {"error": {"message": f"no route for GET {path}", "type": "not_found"}})

    def do_POST(self) -> None:                                 # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/v1/responses", "/responses"):
            self._json(404, {"error": {"message": f"no route for POST {path}",
                                       "type": "not_found"}})
            return
        try:
            body = self._body()
        except Exception as e:
            self._json(400, {"error": {"message": f"请求体不是合法 JSON: {e}",
                                       "type": "invalid_request_error"}})
            return

        routes = self.server.routes_config.get("routes") or {}    # type: ignore[attr-defined]
        route, model = effective_route(body, routes)
        if route is None:
            self._json(400, {"error": {"message": model, "type": "invalid_request_error"}})
            return
        key = read_env(route.get("key_env") or "")
        if not key:
            self._json(500, {"error": {
                "message": f"环境变量 {route.get('key_env')} 未设置",
                "type": "configuration_error"}})
            return

        wire = (route.get("wire") or "responses").lower()
        streaming = bool(body.get("stream", True))
        # Codex 自己会写出上游不接受的工具条目（如定时任务心跳缺 call_id），
        # 两条 wire 都先归一化一遍，形状正常的请求零改动。
        body, norm_warnings = translate.normalize_responses_input(body)
        if norm_warnings:
            log("WARN", "请求归一化", model=model, warnings=norm_warnings)
        try:
            if wire == "chat":
                if streaming:
                    self._serve_chat_stream(body, route, key, model)
                else:
                    self._serve_chat_oneshot(body, route, key, model)
            else:
                if streaming:
                    self._serve_responses_stream(body, route, key, model)
                else:
                    self._serve_responses_oneshot(body, route, key, model)
        except BrokenPipeError:
            log("WARN", "客户端提前断开", model=model)
        except Exception as e:
            log("ERROR", "转发失败", model=model, wire=wire, error=f"{type(e).__name__}: {e}")
            try:
                self._json(502, {"error": {"message": f"{type(e).__name__}: {e}",
                                           "type": "upstream_error"}})
            except Exception:
                pass

    # -- wire=responses（直通，只改模型名与档位） --------------------------- #
    def _prepare_passthrough(self, body: dict, route: dict) -> dict:
        out = dict(body)
        out["model"] = route.get("upstream_model") or body.get("model")
        effort = translate.clamp_effort((body.get("reasoning") or {}).get("effort"),
                                        route.get("effort_levels"))
        if effort:
            out["reasoning"] = {**(body.get("reasoning") or {}), "effort": effort}
        else:
            out.pop("reasoning", None)
        return out

    def _upstream_url(self, route: dict) -> str:
        return (route.get("upstream_base") or "").rstrip("/") + (route.get("path") or "/responses")

    def _serve_responses_stream(self, body: dict, route: dict, key: str, model: str) -> None:
        payload = self._prepare_passthrough(body, route)
        url = self._upstream_url(route)
        resp, err = open_upstream(url, payload, route, key)
        if resp is None:
            status, text = err or (502, "unknown")
            log("ERROR", "上游拒绝", model=model, url=url, status=status, body=text[:400])
            self._json(status or 502, {"error": {"message": text[:2000],
                                                 "type": "upstream_error"}})
            return
        log("INFO", "直通", model=model, upstream=route.get("upstream_model"), url=url)
        self._sse_start()
        with resp:
            for raw in resp:
                self.wfile.write(raw)
            self.wfile.flush()

    def _serve_responses_oneshot(self, body: dict, route: dict, key: str, model: str) -> None:
        payload = self._prepare_passthrough(body, route)
        payload["stream"] = False
        url = self._upstream_url(route)
        resp, err = open_upstream(url, payload, route, key)
        if resp is None:
            status, text = err or (502, "unknown")
            self._json(status or 502, {"error": {"message": text[:2000],
                                                 "type": "upstream_error"}})
            return
        with resp:
            blob = resp.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(blob)
        self.close_connection = True

    # -- wire=chat（双向翻译） --------------------------------------------- #
    def _serve_chat_stream(self, body: dict, route: dict, key: str, model: str) -> None:
        chat_req = translate.responses_to_chat(
            body, upstream_model=route.get("upstream_model") or model,
            reasoning_cache=_REASONING_CACHE, effort_levels=route.get("effort_levels"))
        if chat_req.warnings:
            log("WARN", "请求翻译告警", model=model, warnings=chat_req.warnings)
        url = self._upstream_url(route)
        resp, err = open_upstream(url, chat_req.body, route, key)
        if resp is None:
            status, text = err or (502, "unknown")
            log("ERROR", "上游拒绝(chat)", model=model, url=url, status=status, body=text[:400])
            self._json(status or 502, {"error": {"message": text[:2000],
                                                 "type": "upstream_error"}})
            return
        log("INFO", "翻译转发", model=model, upstream=route.get("upstream_model"), url=url)
        self._sse_start()
        stream = translate.ChatToResponsesStream(
            route.get("upstream_model") or model,
            on_tool_call=lambda call_id, reasoning: _REASONING_CACHE.put(call_id, reasoning))
        with resp:
            for raw in resp:
                chunk = translate.parse_chat_sse(raw.decode("utf-8", "replace"))
                if chunk is None:
                    continue
                for event in stream.feed(chunk):
                    self.wfile.write(event)
                self.wfile.flush()
            for event in stream.fail("上游流提前结束，未收到 finish_reason"):
                self.wfile.write(event)
            self.wfile.flush()

    def _serve_chat_oneshot(self, body: dict, route: dict, key: str, model: str) -> None:
        chat_req = translate.responses_to_chat(
            body, upstream_model=route.get("upstream_model") or model,
            reasoning_cache=_REASONING_CACHE, effort_levels=route.get("effort_levels"))
        chat_req.body["stream"] = False
        url = self._upstream_url(route)
        resp, err = open_upstream(url, chat_req.body, route, key)
        if resp is None:
            status, text = err or (502, "unknown")
            self._json(status or 502, {"error": {"message": text[:2000],
                                                 "type": "upstream_error"}})
            return
        with resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if chat_req.warnings:
            log("WARN", "请求翻译告警", model=model, warnings=chat_req.warnings)
        self._json(200, translate.chat_response_to_responses(payload))


def selftest() -> int:
    cfg = load_routes()
    routes = cfg.get("routes") or {}
    if not routes:
        print("路由表为空")
        return 1
    print(f"路由表: {ROUTES_FILE}")
    print(f"模型数: {len(routes)}")
    bad = 0
    for slug, route in sorted(routes.items()):
        key_env = route.get("key_env") or ""
        has = bool(read_env(key_env))
        ok = has and bool(route.get("upstream_base")) and (route.get("wire") in ("responses", "chat"))
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else 'BAD'} {slug:<34} wire={route.get('wire'):<9} "
              f"{key_env:<22} key={'yes' if has else 'MISSING'} "
              f"base={route.get('upstream_base')}")
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 个路由有问题'}")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    cfg = load_routes()
    host = args.host or cfg.get("host") or "127.0.0.1"
    port = args.port or int(cfg.get("port") or 8791)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.routes_config = cfg                     # type: ignore[attr-defined]
    print(f"codex-model-router 监听 http://{host}:{port}")
    print(f"路由表 {ROUTES_FILE}  模型 {len(cfg.get('routes') or {})} 个")
    print(f"日志 {LOG_FILE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
