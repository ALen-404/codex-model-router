#!/usr/bin/env python3
"""Responses API <-> Chat Completions 双向翻译。

Codex 只会说 Responses API；opencode 的 DeepSeek 两种协议都通，但为了统一入口，
路由器把上游 wire 抽象掉：wire="responses" 直通，wire="chat" 走本模块翻译。

本模块只做纯数据变换，不发网络请求，方便单元测试。

实现的坑（全部来自实测，见 README「已知限制」与 upstream-probe-results.json）：
  1. 带 tool_calls 的 assistant 消息必须回传 reasoning_content，否则上游 400
     -> 按 call_id 缓存上一轮的推理文本，下一轮补回；未命中用占位文本兜底。
  2. chat 的 role="tool" 消息不能带图片（400 Invalid input）
     -> 工具返回里的图片拆到随后的 user 消息，且等整组并行 tool 消息结束后再发。
  3. assistant 的 tool_calls 必须紧跟对应 tool 结果
     -> 缺失的补占位文本；找不到对应调用的孤儿 tool 结果降级成 user 消息。
  4. 思考档位是白名单，传非法值上游 400 -> clamp_effort 夹到最近档位。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

# 实测确认的合法档位顺序（非法值上游直接 400，见 README 数字来源）
EFFORT_ORDER: list[str] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

MISSING_TOOL_RESULT = "(no tool result was recorded for this call)"
NO_REASONING_PLACEHOLDER = "(reasoning not available for this call)"

# 需要 call_id 的条目类型（缺了上游直接 400：missing field `call_id`）
CALL_ITEM_TYPES = {
    "function_call", "custom_tool_call", "local_shell_call",
    "computer_call", "mcp_call", "mcp_approval_request",
}
OUTPUT_ITEM_TYPES = {
    "function_call_output", "custom_tool_call_output", "local_shell_call_output",
    "computer_call_output", "mcp_approval_response",
}
# 上游对孤儿 tool 输出的两种拒绝（实测）：
#   缺 call_id      -> missing field `call_id`
#   call_id 无配对  -> No tool call found for tool output with call_id ...
# 实测唯一可行解：降级成 user 消息（补 call_id / 伪造 function_call 都会被拒）
ORPHAN_OUTPUT_LABEL = "[tool output without a matching call]"

# 单条 tool 结果里如果混了图片，图片会被拆到这条 user 消息的正文后
IMAGE_ATTACHMENT_SEPARATOR = "\n\n[attached tool image]\n"


def clamp_effort(level: str | None, allowed: Iterable[str] | None = None) -> str | None:
    """把请求的思考档位夹到上游白名单内；None 表示不传该参数。"""
    if level is None:
        return None
    allowed = list(allowed or EFFORT_ORDER)
    if not allowed:
        return None
    if level in allowed:
        return level
    if level not in EFFORT_ORDER:
        return None  # 完全不认识的档位：宁可不传，让上游用默认
    want = EFFORT_ORDER.index(level)
    return min(allowed, key=lambda a: abs(EFFORT_ORDER.index(a) - want))


def _delta_reasoning(delta: dict) -> str:
    """推理字段各家叫法不同，实测两例：
        opencode -> delta.reasoning_content
        Cline    -> delta.reasoning（另有 delta.reasoning_details 数组）
    """
    text = delta.get("reasoning_content") or delta.get("reasoning")
    if isinstance(text, str):
        return text
    details = delta.get("reasoning_details")
    if isinstance(details, list):
        return "".join(d.get("text") or "" for d in details if isinstance(d, dict))
    return ""


def _message_reasoning(message: dict) -> str:
    """同上，非流式的消息体。"""
    text = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(text, str) and text:
        return text
    details = message.get("reasoning_details")
    if isinstance(details, list):
        return "".join(d.get("text") or "" for d in details if isinstance(d, dict))
    return ""


# --------------------------------------------------------------------------- #

def _orphan_output_to_message(item: dict) -> dict:
    """把无主/缺字段的工具输出降级成 user 消息（实测唯一被上游接受的形状）。"""
    text = _output_to_text(item.get("output"))
    name = item.get("name") or item.get("type") or "tool"
    namespace = item.get("namespace")
    label = f"{namespace}.{name}" if namespace else str(name)
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text",
                     "text": f"{ORPHAN_OUTPUT_LABEL} {label}\n{text}"}],
    }


def normalize_responses_input(body: dict) -> tuple[dict, list[str]]:
    """让 Responses 请求体满足上游的条目约束。

    背景（实测，见 README「已知限制」）：Codex 的定时任务心跳会把
    `function_call_output` 写成只有 `id` / `name` / `namespace`、**没有 `call_id`**
    的形状（例：id="fco_...", name="automation_update", namespace="codex_app"）。
    回放历史时上游反序列化直接 400。补 `call_id` 或伪造配对 `function_call` 都会
    被上游以别的理由拒（No tool call found / reasoning_text must be passed back），
    只有降级成 user 消息能通过 —— 所以这里就做这一件事。

    返回 (新的 body, 告警列表)；对形状正常的请求是零改动。
    """
    raw = body.get("input")
    if not isinstance(raw, list):
        return body, []

    warnings: list[str] = []
    declared: set[str] = set()
    out: list[dict] = []
    fixed_missing = 0
    fixed_orphan = 0

    for item in raw:
        if not isinstance(item, dict):
            out.append(item)
            continue
        itype = item.get("type")

        if itype in CALL_ITEM_TYPES:
            # 注意必须检查真正的 call_id 字段：id 是条目自身 id，不能当作调用引用
            cid = item.get("call_id")
            if cid:
                declared.add(cid)
                out.append(item)
            elif item.get("id"):
                # 用条目 id 顶替，并让后续输出条目的 call_id 与之配对
                out.append({**item, "call_id": item["id"]})
                declared.add(item["id"])
                fixed_missing += 1
            else:
                # 既无 call_id 也无 id：降级成 user 消息，避免整个请求失败
                out.append(_orphan_output_to_message(item))
                fixed_missing += 1
            continue

        if itype in OUTPUT_ITEM_TYPES:
            cid = item.get("call_id")
            if cid and cid in declared:
                out.append(item)
                continue
            out.append(_orphan_output_to_message(item))
            if cid:
                fixed_orphan += 1        # 有 call_id 但找不到对应调用
            else:
                fixed_missing += 1       # 压根没有 call_id（定时任务心跳就是这个形状）
            continue

        out.append(item)

    if fixed_missing:
        warnings.append(f"normalized {fixed_missing} tool item(s) missing call_id -> user message")
    if fixed_orphan:
        warnings.append(f"normalized {fixed_orphan} orphan tool output(s) -> user message")
    if not warnings:
        return body, []
    return {**body, "input": out}, warnings


# --------------------------------------------------------------------------- #
# 推理文本缓存：assistant tool_calls 回传时必须带 reasoning_content
# --------------------------------------------------------------------------- #

class ReasoningCache:
    """call_id -> 该次工具调用发生时的推理文本。进程内 LRU，跨请求存活。"""

    def __init__(self, max_entries: int = 4096) -> None:
        self._data: dict[str, str] = {}
        self._max = max_entries

    def put(self, call_id: str, text: str) -> None:
        if not call_id or not text:
            return
        if len(self._data) >= self._max:
            for k in list(self._data)[: self._max // 4]:
                self._data.pop(k, None)
        self._data[call_id] = text

    def get(self, call_id: str) -> str:
        return self._data.get(call_id) or NO_REASONING_PLACEHOLDER


# --------------------------------------------------------------------------- #
# 工具定义 / 图像
# --------------------------------------------------------------------------- #

def _to_chat_tools(tools: Any) -> list[dict]:
    """Responses 的工具定义 -> chat 的工具定义（丢弃 chat 不认的类型）。"""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            continue  # web_search / file_search 等上游不支持，丢弃
        if "function" in t:                       # 已经是 chat 形状
            out.append(t)
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out


def _split_images(content: Any) -> tuple[list[str], list[str]]:
    """把一条 content 拆成 (文本片段, 图片 data/url 列表)。"""
    texts: list[str] = []
    images: list[str] = []
    if isinstance(content, str):
        texts.append(content)
        return texts, images
    for part in content or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            texts.append(part.get("text") or "")
        elif ptype == "input_image":
            url = part.get("image_url") or part.get("url")
            if url:
                images.append(url)
    return texts, images


def _extract_call_id(part: dict) -> str:
    return part.get("call_id") or part.get("id") or ""


def _output_to_text(content: Any) -> str:
    """function_call_output 的 output 可能是字符串或结构化数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts, _ = _split_images(content)
        return "\n".join(x for x in texts if x)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 请求翻译: Responses -> Chat
# --------------------------------------------------------------------------- #

@dataclass
class ChatRequest:
    body: dict
    warnings: list[str] = field(default_factory=list)


def responses_to_chat(
    body: dict,
    *,
    upstream_model: str,
    reasoning_cache: ReasoningCache | None = None,
    effort_levels: list[str] | None = None,
) -> ChatRequest:
    """把 Codex 发来的 Responses 请求翻成 chat/completions 请求体。"""
    cache = reasoning_cache or ReasoningCache()
    warnings: list[str] = []
    messages: list[dict] = []

    # system / developer 指令
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    items: list[dict] = []
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        items = [{"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": raw_input}]}]
    elif isinstance(raw_input, list):
        items = [i for i in raw_input if isinstance(i, dict)]

    # pending_tool_images: 并行 tool 组结束后一次性补发的用户图片消息
    pending_tool_images: list[str] = []
    pending_tool_calls: list[str] = []        # 已发出、还没拿到结果的 call_id
    tool_assistant: dict | None = None        # 正在累积的 assistant(tool_calls) 消息

    def flush_tool_images() -> None:
        if pending_tool_images:
            parts = [{"type": "text", "text": "[tool images]"}]
            parts += [{"type": "image_url", "image_url": {"url": u}}
                      for u in pending_tool_images]
            messages.append({"role": "user", "content": parts})
            pending_tool_images.clear()

    def close_tool_assistant(fill_missing: bool = True) -> None:
        """结束当前 assistant(tool_calls) 消息：缺结果的补占位，再允许换角色。"""
        nonlocal tool_assistant
        if tool_assistant is None:
            return
        if fill_missing:
            for cid in pending_tool_calls:
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": MISSING_TOOL_RESULT})
            pending_tool_calls.clear()
            warnings.append("tool_calls without results; inserted placeholder tool results")
        tool_assistant = None

    for item in items:
        itype = item.get("type")

        if itype == "message":
            role = item.get("role") or "user"
            texts, images = _split_images(item.get("content"))
            text = "\n".join(t for t in texts if t)
            if role == "assistant":
                close_tool_assistant()
                if text:
                    messages.append({"role": "assistant", "content": text})
                continue
            if role == "system":
                close_tool_assistant()
                if text:
                    messages.append({"role": "system", "content": text})
                continue
            # user
            close_tool_assistant()
            flush_tool_images()
            if images:
                parts = ([{"type": "text", "text": text}] if text else []) + \
                        [{"type": "image_url", "image_url": {"url": u}} for u in images]
                messages.append({"role": "user", "content": parts})
            elif text:
                messages.append({"role": "user", "content": text})
            continue

        if itype == "function_call":
            call_id = _extract_call_id(item)
            if tool_assistant is None:
                # 关键坑 1：带 tool_calls 的 assistant 必须带 reasoning_content。
                # 连续的 function_call 合并进同一条 assistant（chat 的并行调用形状）。
                tool_assistant = {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": cache.get(call_id),
                    "tool_calls": [],
                }
                messages.append(tool_assistant)
            tool_assistant["tool_calls"].append({
                "id": call_id,
                "type": "function",
                "function": {"name": item.get("name") or "",
                             "arguments": item.get("arguments") or "{}"},
            })
            pending_tool_calls.append(call_id)
            continue

        if itype == "function_call_output":
            call_id = _extract_call_id(item)
            if call_id in pending_tool_calls:
                pending_tool_calls.remove(call_id)
                # 关键坑 2：tool 消息不能带图，图片挂起到整组结束后再发
                images: list[str] = []
                if isinstance(item.get("output"), list):
                    images = [u for part in item["output"] if isinstance(part, dict)
                              and part.get("type") == "input_image"
                              for u in [part.get("image_url") or part.get("url")] if u]
                if images:
                    body_text = _output_to_text(
                        [p for p in item["output"] if isinstance(p, dict)
                         and p.get("type") != "input_image"])
                    text = (body_text + IMAGE_ATTACHMENT_SEPARATOR +
                            f"{len(images)} image(s) moved to a following user message")
                    pending_tool_images.extend(images)
                    warnings.append(f"split {len(images)} image(s) out of tool result")
                else:
                    text = _output_to_text(item.get("output"))
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": text or "(empty tool result)"})
                if not pending_tool_calls:      # 整组结束
                    tool_assistant = None
                    flush_tool_images()
            else:
                # 关键坑 3：孤儿 tool 结果 -> 降级成 user 消息
                close_tool_assistant(fill_missing=False)
                messages.append({"role": "user",
                                 "content": f"[tool result for unknown call {call_id}]\n"
                                            f"{_output_to_text(item.get('output'))}"})
                warnings.append(f"orphan tool result for call_id={call_id!r} -> user message")
            continue

        if itype == "reasoning":
            continue  # 历史推理不回灌，避免污染上下文

    close_tool_assistant()
    flush_tool_images()

    if not messages:
        messages.append({"role": "user", "content": ""})

    out: dict[str, Any] = {"model": upstream_model, "messages": messages, "stream": True}

    tools = _to_chat_tools(body.get("tools"))
    if tools:
        out["tools"] = tools
        if body.get("parallel_tool_calls") is not None:
            out["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
        if body.get("tool_choice") is not None:
            tc = body["tool_choice"]
            out["tool_choice"] = tc if tc in ("auto", "none", "required") else "auto"

    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]

    max_out = body.get("max_output_tokens")
    if isinstance(max_out, int) and max_out > 0:
        out["max_tokens"] = max_out

    effort = clamp_effort((body.get("reasoning") or {}).get("effort"), effort_levels)
    if effort:
        out["reasoning_effort"] = effort

    return ChatRequest(body=out, warnings=warnings)


# --------------------------------------------------------------------------- #
# 响应翻译: Chat -> Responses（非流式）
# --------------------------------------------------------------------------- #

def _new_response_id() -> str:
    return "resp_" + uuid.uuid4().hex


def chat_usage_to_responses(usage: dict | None) -> dict:
    usage = usage or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        },
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {
            "reasoning_tokens": details.get("reasoning_tokens", 0)
        },
        "total_tokens": usage.get("total_tokens", 0),
    }


def chat_message_to_output_items(message: dict) -> list[dict]:
    """chat 的 assistant message -> Responses 的 output 数组。"""
    items: list[dict] = []
    reasoning = _message_reasoning(message)
    if reasoning:
        items.append({
            "id": "rs_" + uuid.uuid4().hex,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
            "content": None,
        })
    text = message.get("content")
    if isinstance(text, str) and text:
        items.append({
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        items.append({
            "id": "fc_" + uuid.uuid4().hex,
            "type": "function_call",
            "status": "completed",
            "call_id": call.get("id") or f"call_{i}",
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
        })
    return items


def unwrap_chat_payload(payload: dict) -> dict:
    """Cline 的非流式响应包了一层 {"data": {...}, "success": true}；流式不包。"""
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict)             and "choices" in payload["data"]:
        return payload["data"]
    return payload


def chat_response_to_responses(payload: dict, response_id: str | None = None) -> dict:
    payload = unwrap_chat_payload(payload)
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    status = "incomplete" if finish == "length" else "completed"
    return {
        "id": response_id or _new_response_id(),
        "object": "response",
        "created_at": payload.get("created"),
        "status": status,
        "model": payload.get("model"),
        "output": chat_message_to_output_items(message),
        "usage": chat_usage_to_responses(payload.get("usage")),
        "parallel_tool_calls": True,
        "incomplete_details": ({"reason": "max_output_tokens"} if status == "incomplete" else None),
        "error": None,
    }


# --------------------------------------------------------------------------- #
# 响应翻译: chat SSE -> Responses SSE（流式状态机）
# --------------------------------------------------------------------------- #

def _sse(event: str, data: dict) -> bytes:
    return (f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


@dataclass
class _StreamState:
    response_id: str
    created_at: int | None
    model: str
    text: str = ""
    reasoning: str = ""
    msg_item_id: str = ""
    msg_index: int = -1
    reasoning_item_id: str = ""
    reasoning_index: int = -1
    tools: dict[int, dict] = field(default_factory=dict)  # chat tool_call index -> 状态
    next_index: int = 0
    finished: bool = False
    finish_reason: str | None = None
    usage: dict | None = None
    has_reasoning_item: bool = False


class ChatToResponsesStream:
    """逐块把 chat SSE 转成 Responses SSE。

    上游 chunk 形状：
      {"choices":[{"delta":{"content":"..","reasoning_content":"..",
                            "tool_calls":[{"index":0,"id":"..","function":{"name":..,"arguments":..}}]},
                   "finish_reason":null}], "usage":{...}}
    """

    def __init__(self, model: str, response_id: str | None = None,
                 on_tool_call: Any = None) -> None:
        self.st = _StreamState(response_id=response_id or _new_response_id(),
                               created_at=None, model=model)
        self._on_tool_call = on_tool_call
        self._started = False

    # -- 事件构造 ---------------------------------------------------------- #
    def _response_object(self, status: str, output: list[dict] | None = None) -> dict:
        st = self.st
        return {
            "id": st.response_id,
            "object": "response",
            "created_at": st.created_at,
            "status": status,
            "model": st.model,
            "output": output if output is not None else [],
            "usage": chat_usage_to_responses(st.usage),
            "parallel_tool_calls": True,
            "incomplete_details": None,
            "error": None,
        }

    def _ensure_started(self, chunk: dict) -> list[bytes]:
        if self._started:
            return []
        self._started = True
        self.st.created_at = chunk.get("created")
        self.st.model = chunk.get("model") or self.st.model
        return [
            _sse("response.created", {"type": "response.created",
                                      "response": self._response_object("in_progress")}),
            _sse("response.in_progress", {"type": "response.in_progress",
                                          "response": self._response_object("in_progress")}),
        ]

    # -- 主入口 ------------------------------------------------------------ #
    def feed(self, chunk: dict) -> list[bytes]:
        if self.st.finished:      # finish_reason 之后上游可能还发残留块，一律忽略
            return []
        out = self._ensure_started(chunk)
        if chunk.get("usage"):
            self.st.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return out
        choice = choices[0]
        delta = choice.get("delta") or {}
        st = self.st

        reasoning = _delta_reasoning(delta)
        if reasoning:
            if not st.has_reasoning_item:
                st.has_reasoning_item = True
                st.reasoning_index = st.next_index
                st.next_index += 1
                st.reasoning_item_id = "rs_" + uuid.uuid4().hex
                out.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": st.reasoning_index,
                    "item": {"id": st.reasoning_item_id, "type": "reasoning",
                             "status": "in_progress", "summary": [], "content": None}}))
            st.reasoning += reasoning
            out.append(_sse("response.reasoning_summary_text.delta", {
                "type": "response.reasoning_summary_text.delta",
                "item_id": st.reasoning_item_id, "output_index": st.reasoning_index,
                "summary_index": 0, "delta": reasoning}))

        text = delta.get("content")
        if text:
            if not st.msg_item_id:
                st.msg_index = st.next_index
                st.next_index += 1
                st.msg_item_id = "msg_" + uuid.uuid4().hex
                out.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": st.msg_index,
                    "item": {"id": st.msg_item_id, "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []}}))
                out.append(_sse("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": st.msg_item_id,
                    "output_index": st.msg_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []}}))
            st.text += text
            out.append(_sse("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": st.msg_item_id,
                "output_index": st.msg_index, "content_index": 0, "delta": text}))

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            entry = st.tools.get(idx)
            if entry is None:
                entry = {"id": "fc_" + uuid.uuid4().hex, "output_index": st.next_index,
                         "call_id": tc.get("id") or f"call_{idx}", "name": "",
                         "arguments": "", "added": False}
                st.tools[idx] = entry
                st.next_index += 1
            if tc.get("id"):
                entry["call_id"] = tc["id"]
            if fn.get("name"):
                entry["name"] += fn["name"]
            if not entry["added"] and entry["name"]:
                entry["added"] = True
                out.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": entry["output_index"],
                    "item": {"id": entry["id"], "type": "function_call", "status": "in_progress",
                             "call_id": entry["call_id"], "name": entry["name"],
                             "arguments": ""}}))
            if fn.get("arguments"):
                entry["arguments"] += fn["arguments"]
                out.append(_sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta", "item_id": entry["id"],
                    "output_index": entry["output_index"], "delta": fn["arguments"]}))

        if choice.get("finish_reason"):
            st.finish_reason = choice["finish_reason"]
            out.extend(self._finish_events())
        return out

    def _finish_events(self) -> list[bytes]:
        if self.st.finished:
            return []
        st = self.st
        st.finished = True
        out: list[bytes] = []
        output: list[dict] = []

        if st.has_reasoning_item:
            item = {"id": st.reasoning_item_id, "type": "reasoning", "status": "completed",
                    "summary": ([{"type": "summary_text", "text": st.reasoning}]
                                if st.reasoning else []),
                    "content": None}
            out.append(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": st.reasoning_index,
                "item": item}))
            output.append(item)

        if st.msg_item_id:
            out.append(_sse("response.output_text.done", {
                "type": "response.output_text.done", "item_id": st.msg_item_id,
                "output_index": st.msg_index, "content_index": 0, "text": st.text}))
            out.append(_sse("response.content_part.done", {
                "type": "response.content_part.done", "item_id": st.msg_item_id,
                "output_index": st.msg_index, "content_index": 0,
                "part": {"type": "output_text", "text": st.text, "annotations": []}}))
            item = {"id": st.msg_item_id, "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": st.text, "annotations": []}]}
            out.append(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": st.msg_index,
                "item": item}))
            output.append(item)

        if st.has_reasoning_item and st.reasoning:
            self._remember_reasoning(st)

        for entry in sorted(st.tools.values(), key=lambda e: e["output_index"]):
            out.append(_sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": entry["id"],
                "output_index": entry["output_index"], "arguments": entry["arguments"]}))
            item = {"id": entry["id"], "type": "function_call", "status": "completed",
                    "call_id": entry["call_id"], "name": entry["name"],
                    "arguments": entry["arguments"]}
            out.append(_sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": entry["output_index"],
                "item": item}))
            output.append(item)

        status = "incomplete" if st.finish_reason == "length" else "completed"
        done = self._response_object(status, output)
        if status == "incomplete":
            done["incomplete_details"] = {"reason": "max_output_tokens"}
        out.append(_sse("response.completed", {"type": "response.completed", "response": done}))
        return out

    def _remember_reasoning(self, st: _StreamState) -> None:
        """把本轮的推理文本按 call_id 存起来，下一轮 tool_calls 回传时补上。"""
        if not self._on_tool_call or not st.reasoning:
            return
        for entry in st.tools.values():
            self._on_tool_call(entry["call_id"], st.reasoning)

    def fail(self, message: str, code: str = "upstream_error") -> list[bytes]:
        st = self.st
        if st.finished:
            return []
        st.finished = True
        resp = self._response_object("failed")
        resp["error"] = {"code": code, "message": message}
        return [_sse("response.failed", {"type": "response.failed", "response": resp})]


def parse_chat_sse(raw_line: str) -> dict | None:
    """从一行 chat SSE 里取出 JSON；[DONE] 与非 data 行返回 None。"""
    line = raw_line.strip()
    if not line or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload in ("", "[DONE]"):
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
