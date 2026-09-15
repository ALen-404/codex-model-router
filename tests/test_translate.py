#!/usr/bin/env python3
"""翻译层单元测试。

覆盖：档位夹取、Requests 形状转换、三个实测坑（reasoning_content 回传 /
tool 图片拆分 / tool 结果配对）、流式状态机事件序列。

运行:  python tests/test_translate.py        (或 python -m unittest discover tests)
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import translate as T  # noqa: E402


def ev(blobs: list[bytes]) -> list[tuple[str, dict]]:
    """把 SSE 字节块拆成 (事件名, data) 列表。"""
    out = []
    for b in blobs:
        text = b.decode("utf-8")
        name = ""
        data = {}
        for line in text.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((name, data))
    return out


# 兼容旧名
events = ev


class TestClampEffort(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(T.clamp_effort("high"), "high")

    def test_none_allowed(self):
        self.assertEqual(T.clamp_effort("none"), "none")

    def test_none_passthrough_when_none(self):
        self.assertIsNone(T.clamp_effort(None))

    def test_unknown_level_dropped(self):
        # 实测非法值上游 400，所以宁可不传
        self.assertIsNone(T.clamp_effort("bogus"))
        self.assertIsNone(T.clamp_effort(""))

    def test_clamped_to_nearest_allowed(self):
        allowed = ["low", "medium", "high"]
        self.assertEqual(T.clamp_effort("max", allowed), "high")
        self.assertEqual(T.clamp_effort("minimal", allowed), "low")
        self.assertEqual(T.clamp_effort("xhigh", allowed), "high")


class TestResponsesToChat(unittest.TestCase):
    def test_instructions_become_system(self):
        r = T.responses_to_chat({"instructions": "be nice",
                                 "input": "hello"}, upstream_model="m")
        self.assertEqual(r.body["messages"][0], {"role": "system", "content": "be nice"})
        self.assertEqual(r.body["messages"][1], {"role": "user", "content": "hello"})

    def test_string_input(self):
        r = T.responses_to_chat({"input": "hi"}, upstream_model="m")
        self.assertEqual(r.body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(r.body["model"], "m")

    def test_max_output_tokens_maps(self):
        r = T.responses_to_chat({"input": "x", "max_output_tokens": 1234},
                                upstream_model="m")
        self.assertEqual(r.body["max_tokens"], 1234)

    def test_tools_converted_to_chat_shape(self):
        r = T.responses_to_chat({
            "input": "x",
            "tools": [{"type": "function", "name": "shell", "description": "run",
                       "parameters": {"type": "object"}},
                      {"type": "web_search"}],
        }, upstream_model="m")
        self.assertEqual(len(r.body["tools"]), 1)
        self.assertEqual(r.body["tools"][0]["function"]["name"], "shell")

    def test_effort_clamped_into_body(self):
        r = T.responses_to_chat({"input": "x", "reasoning": {"effort": "max"}},
                                upstream_model="m", effort_levels=["low", "medium"])
        self.assertEqual(r.body["reasoning_effort"], "medium")

    def test_invalid_effort_not_forwarded(self):
        r = T.responses_to_chat({"input": "x", "reasoning": {"effort": "nonsense"}},
                                upstream_model="m")
        self.assertNotIn("reasoning_effort", r.body)

    def test_user_image_becomes_image_url_part(self):
        r = T.responses_to_chat({
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "what color"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"}]}],
        }, upstream_model="m")
        parts = r.body["messages"][0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")


class TestPitfallReasoningContent(unittest.TestCase):
    """坑 1：带 tool_calls 的 assistant 必须回传 reasoning_content。"""

    def _items(self):
        return [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "do it"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]

    def test_cached_reasoning_replayed(self):
        cache = T.ReasoningCache()
        cache.put("c1", "I should run the shell command")
        r = T.responses_to_chat({"input": self._items()}, upstream_model="m",
                                reasoning_cache=cache)
        asst = [m for m in r.body["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(asst["reasoning_content"], "I should run the shell command")
        self.assertIsNone(asst["content"])
        self.assertEqual(asst["tool_calls"][0]["id"], "c1")

    def test_cache_miss_uses_placeholder(self):
        r = T.responses_to_chat({"input": self._items()}, upstream_model="m",
                                reasoning_cache=T.ReasoningCache())
        asst = [m for m in r.body["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(asst["reasoning_content"], T.NO_REASONING_PLACEHOLDER)


class TestPitfallParallelTools(unittest.TestCase):
    """chat 要求多个 tool_calls 合并在同一条 assistant 消息里。"""

    def test_parallel_calls_merged_into_one_assistant(self):
        r = T.responses_to_chat({"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "two things"}]},
            {"type": "function_call", "call_id": "c1", "name": "a", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "b", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "r1"},
            {"type": "function_call_output", "call_id": "c2", "output": "r2"},
        ]}, upstream_model="m")
        msgs = r.body["messages"]
        assistants = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1, "并行调用必须合并成一条 assistant")
        self.assertEqual([c["id"] for c in assistants[0]["tool_calls"]], ["c1", "c2"])
        tools = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["c1", "c2"])
        self.assertNotIn("placeholder", json.dumps(r.warnings))


class TestPitfallToolImages(unittest.TestCase):
    """坑 2：tool 消息不能带图片，图片要拆到整组之后的 user 消息。"""

    def test_tool_images_split_after_group(self):
        img = "data:image/png;base64,ZZZ"
        r = T.responses_to_chat({"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "look"}]},
            {"type": "function_call", "call_id": "c1", "name": "shot", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "shot2", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "screenshot 1"},
                        {"type": "input_image", "image_url": img}]},
            {"type": "function_call_output", "call_id": "c2", "output": "plain"},
        ]}, upstream_model="m")
        msgs = r.body["messages"]
        # tool 消息一律不能含图片
        for m in msgs:
            if m["role"] == "tool":
                self.assertNotIn("image_url", json.dumps(m))
        # 图片必须出现在整组 tool 结果之后的 user 消息里
        tool_pos = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
        img_pos = [i for i, m in enumerate(msgs)
                   if m["role"] == "user" and "image_url" in json.dumps(m)]
        self.assertEqual(len(img_pos), 1)
        self.assertGreater(img_pos[0], max(tool_pos),
                           "图片消息必须排在整组 tool 结果之后")
        self.assertTrue(any("split" in w for w in r.warnings))


class TestPitfallPairing(unittest.TestCase):
    """坑 3：tool_calls 与 tool 结果必须配对。"""

    def test_missing_tool_result_gets_placeholder(self):
        r = T.responses_to_chat({"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c1", "name": "a", "arguments": "{}"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "done"}]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "next"}]},
        ]}, upstream_model="m")
        kinds = [(m["role"], m.get("content")) for m in r.body["messages"]]
        self.assertIn(("tool", T.MISSING_TOOL_RESULT), kinds)
        tool_idx = [i for i, m in enumerate(r.body["messages"]) if m["role"] == "tool"][0]
        asst_idx = [i for i, m in enumerate(r.body["messages"])
                    if m["role"] == "assistant"][0]
        self.assertEqual(tool_idx, asst_idx + 1, "tool 结果必须紧跟 assistant")

    def test_orphan_tool_result_becomes_user_message(self):
        r = T.responses_to_chat({"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "call_id": "ghost", "output": "stray"},
        ]}, upstream_model="m")
        msgs = r.body["messages"]
        self.assertFalse([m for m in msgs if m["role"] == "tool"])
        last = msgs[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("ghost", last["content"])
        self.assertTrue(any("orphan" in w for w in r.warnings))


class TestChatResponseToResponses(unittest.TestCase):
    def test_text_and_usage(self):
        out = T.chat_response_to_responses({
            "created": 1, "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi there",
                                     "reasoning_content": "thought"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                      "completion_tokens_details": {"reasoning_tokens": 3}},
        })
        self.assertEqual(out["status"], "completed")
        kinds = [o["type"] for o in out["output"]]
        self.assertEqual(kinds, ["reasoning", "message"])
        self.assertEqual(out["usage"]["input_tokens"], 10)
        self.assertEqual(out["usage"]["output_tokens_details"]["reasoning_tokens"], 3)

    def test_length_finish_is_incomplete(self):
        out = T.chat_response_to_responses({
            "choices": [{"finish_reason": "length", "message": {"content": "cut"}}]})
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["incomplete_details"]["reason"], "max_output_tokens")

    def test_tool_calls_become_function_call_items(self):
        out = T.chat_response_to_responses({
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "c9", "type": "function",
                                "function": {"name": "shell", "arguments": '{"a":1}'}}]}}]})
        items = [o for o in out["output"] if o["type"] == "function_call"]
        self.assertEqual(items[0]["call_id"], "c9")
        self.assertEqual(items[0]["arguments"], '{"a":1}')


class TestStreamStateMachine(unittest.TestCase):
    def _chunks(self):
        return [
            {"created": 111, "model": "deepseek-flash", "choices": [
                {"delta": {"reasoning_content": "let me think"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "shell", "arguments": '{"cmd"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ':1}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}},
        ]

    def test_event_sequence(self):
        seen: list[tuple[str, str]] = []
        stream = T.ChatToResponsesStream("deepseek-flash")
        for c in self._chunks():
            for ev, data in events(stream.feed(c)):
                seen.append((ev, json.dumps(data)))
        names = [n for n, _ in seen]
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names[1], "response.in_progress")
        self.assertIn("response.output_text.delta", names)
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.function_call_arguments.done", names)
        self.assertIn("response.output_item.done", names)
        self.assertEqual(names[-1], "response.completed")
        # 文本增量拼接正确
        deltas = [json.loads(d)["delta"] for n, d in seen
                  if n == "response.output_text.delta"]
        self.assertEqual("".join(deltas), "Hello")
        # 工具参数增量拼接正确
        args = "".join(json.loads(d)["delta"] for n, d in seen
                       if n == "response.function_call_arguments.delta")
        self.assertEqual(args, '{"cmd":1}')
        # completed 带 usage 与 output
        completed = json.loads([d for n, d in seen if n == "response.completed"][0])
        self.assertEqual(completed["response"]["status"], "completed")
        self.assertEqual(completed["response"]["usage"]["input_tokens"], 7)
        kinds = [o["type"] for o in completed["response"]["output"]]
        self.assertIn("message", kinds)
        self.assertIn("function_call", kinds)

    def test_reasoning_cached_against_call_id(self):
        cached: list[tuple[str, str]] = []
        stream = T.ChatToResponsesStream(
            "deepseek-flash", on_tool_call=lambda cid, r: cached.append((cid, r)))
        for c in self._chunks():
            stream.feed(c)
        self.assertIn(("c1", "let me think"), cached)

    def test_fail_emits_response_failed(self):
        stream = T.ChatToResponsesStream("deepseek-flash")
        stream.feed(self._chunks()[0])
        evs = events(stream.fail("upstream died"))
        self.assertEqual(evs[-1][0], "response.failed")
        self.assertEqual(evs[-1][1]["response"]["status"], "failed")

    def test_no_events_after_finish(self):
        stream = T.ChatToResponsesStream("deepseek-flash")
        for c in self._chunks():
            stream.feed(c)
        self.assertEqual(stream.feed({"choices": [{"delta": {"content": "late"}}]}), [])


class TestNormalizeResponsesInput(unittest.TestCase):
    """Codex 定时任务心跳会把 function_call_output 写成没有 call_id 的形状，
    上游反序列化直接 400。归一化必须把这种条目降级成 user 消息。"""

    def test_wellformed_passthrough(self):
        body = {"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]}
        out, warns = T.normalize_responses_input(body)
        self.assertEqual(out, body)
        self.assertEqual(warns, [])

    def test_non_list_input_untouched(self):
        body = {"input": "hello"}
        out, warns = T.normalize_responses_input(body)
        self.assertIs(out, body)
        self.assertEqual(warns, [])

    def test_heartbeat_orphan_output_without_call_id(self):
        # 复刻实际坏数据：id / name / namespace，就是没有 call_id
        body = {"input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "继续"}]},
            {"type": "function_call_output", "id": "fco_01a09138-7577-7813",
             "name": "automation_update", "namespace": "codex_app",
             "output": "<heartbeat>检查 DYC</heartbeat>"},
        ]}
        out, warns = T.normalize_responses_input(body)
        self.assertEqual(len(out["input"]), 2)
        converted = out["input"][1]
        self.assertEqual(converted["type"], "message")
        self.assertEqual(converted["role"], "user")
        text = converted["content"][0]["text"]
        self.assertIn(T.ORPHAN_OUTPUT_LABEL, text)
        self.assertIn("codex_app.automation_update", text)
        self.assertIn("<heartbeat>", text)
        self.assertTrue(any("missing call_id" in w for w in warns))
        # 上游不接受没有 call_id 的工具条目
        self.assertNotIn("call_id", json.dumps(converted))

    def test_orphan_output_with_dangling_call_id(self):
        body = {"input": [
            {"type": "function_call_output", "call_id": "ghost", "output": "stray"},
        ]}
        out, warns = T.normalize_responses_input(body)
        self.assertEqual(out["input"][0]["type"], "message")
        self.assertTrue(any("orphan" in w for w in warns))

    def test_call_declaration_without_call_id_gets_id_as_call_id(self):
        body = {"input": [
            {"type": "function_call", "id": "fc_abc", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "fc_abc", "output": "ok"},
        ]}
        out, warns = T.normalize_responses_input(body)
        self.assertEqual(out["input"][0]["call_id"], "fc_abc")
        # 补上 call_id 后配对成立，输出条目原样保留
        self.assertEqual(out["input"][1]["type"], "function_call_output")
        self.assertTrue(any("missing call_id" in w for w in warns))

    def test_normalized_body_has_no_dangling_tool_items(self):
        """归一化之后，任何工具条目都必须能通过上游的两条校验。"""
        body = {"input": [
            {"type": "function_call_output", "id": "fco_1", "name": "a", "output": "x"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "id": "fco_2", "name": "b", "output": "y"},
        ]}
        out, _ = T.normalize_responses_input(body)
        declared = {i.get("call_id") for i in out["input"]
                    if i.get("type") in T.CALL_ITEM_TYPES}
        for item in out["input"]:
            t = item.get("type")
            if t in T.CALL_ITEM_TYPES:
                self.assertTrue(item.get("call_id"), f"声明型条目仍缺 call_id: {item}")
            if t in T.OUTPUT_ITEM_TYPES:
                self.assertIn(item.get("call_id"), declared,
                              f"输出条目仍无配对调用: {item}")


class TestClineWireCompat(unittest.TestCase):
    """Cline 与 opencode 的两处差异（都是实测得来的）：
       1. 推理字段叫 `reasoning` 而不是 `reasoning_content`（另有 reasoning_details）
       2. 非流式响应包一层 {"data": {...}, "success": true}，流式不包
    """

    def test_delta_reasoning_reads_both_names(self):
        self.assertEqual(T._delta_reasoning({"reasoning_content": "oc"}), "oc")
        self.assertEqual(T._delta_reasoning({"reasoning": "cli"}), "cli")
        self.assertEqual(T._delta_reasoning({"content": "x"}), "")

    def test_delta_reasoning_from_details_array(self):
        delta = {"reasoning_details": [
            {"type": "reasoning.text", "text": "part1"},
            {"type": "reasoning.text", "text": "part2"}]}
        self.assertEqual(T._delta_reasoning(delta), "part1part2")

    def test_message_reasoning_from_details_array(self):
        msg = {"reasoning_details": [{"text": "hello"}]}
        self.assertEqual(T._message_reasoning(msg), "hello")

    def test_unwrap_cline_payload(self):
        wrapped = {"data": {"choices": [{"message": {"content": "OK"}}]}, "success": True}
        self.assertIn("choices", T.unwrap_chat_payload(wrapped))

    def test_unwrap_leaves_plain_payload(self):
        plain = {"choices": [{"message": {"content": "OK"}}]}
        self.assertIs(T.unwrap_chat_payload(plain), plain)

    def test_chat_response_to_responses_handles_wrapper(self):
        out = T.chat_response_to_responses({
            "data": {"choices": [{"finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "OK",
                                              "reasoning": "thought it through"}}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            "success": True})
        self.assertEqual(out["status"], "completed")
        kinds = [o["type"] for o in out["output"]]
        self.assertEqual(kinds, ["reasoning", "message"])

    def test_stream_surfaces_cline_reasoning(self):
        """Cline 的流式 delta.reasoning 必须被翻成 responses 的 reasoning 事件。"""
        stream = T.ChatToResponsesStream("deepseek/deepseek-v4.1-flash")
        blobs = stream.feed({"created": 1, "choices": [{"delta": {"reasoning": "thinking..."}}]})
        names = [n for n, _ in ev(blobs)]
        self.assertIn("response.reasoning_summary_text.delta", names)
        blobs += stream.feed({"choices": [{"delta": {"content": "hi"}}]})
        blobs += stream.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        done = [d for n, d in ev(blobs) if n == "response.completed"][0]
        kinds = [o["type"] for o in done["response"]["output"]]
        self.assertEqual(kinds, ["reasoning", "message"])

    def test_stream_handles_cline_tool_calls(self):
        """实测 Cline 流式工具调用形状：delta.tool_calls[{index,id,function}]。"""
        stream = T.ChatToResponsesStream("deepseek/deepseek-v4.1-flash")
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_8e11", "function": {"name": "get_weather",
                                                             "arguments": '{"city":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ' "Beijing"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        blobs = []
        for c in chunks:
            blobs += stream.feed(c)
        parsed = ev(blobs)
        args = "".join(d["delta"] for n, d in parsed
                       if n == "response.function_call_arguments.delta")
        self.assertEqual(args, '{"city": "Beijing"}')
        done = [d for n, d in parsed if n == "response.completed"][0]
        calls = [o for o in done["response"]["output"] if o["type"] == "function_call"]
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["call_id"], "call_8e11")


class TestParseChatSse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(T.parse_chat_sse('data: {"a":1}'), {"a": 1})

    def test_done_and_junk(self):
        self.assertIsNone(T.parse_chat_sse("data: [DONE]"))
        self.assertIsNone(T.parse_chat_sse("event: ping"))
        self.assertIsNone(T.parse_chat_sse(""))
        self.assertIsNone(T.parse_chat_sse("data: {broken"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
