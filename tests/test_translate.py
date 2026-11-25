import json

import pytest

from app import translate


def test_openai_to_anthropic_maps_system_tools_and_tool_results():
    body = {
        "model": "x", "max_tokens": 50, "stop": ["END"],
        "tools": [{"type": "function", "function": {
            "name": "weather", "description": "d",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
        "tool_choice": {"type": "function", "function": {"name": "weather"}},
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "weather", "arguments": json.dumps({"city": "Paris"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "18C"},
        ],
    }
    out = translate.openai_to_anthropic(body)
    assert out["system"] == "be terse"
    assert out["tools"][0]["input_schema"]["properties"]["city"]["type"] == "string"
    assert out["tool_choice"] == {"type": "tool", "name": "weather"}
    assert out["stop_sequences"] == ["END"]
    assistant = out["messages"][1]
    assert assistant["content"][0]["type"] == "tool_use"
    assert assistant["content"][0]["input"] == {"city": "Paris"}
    assert out["messages"][2]["content"][0]["type"] == "tool_result"
    assert out["messages"][2]["content"][0]["tool_use_id"] == "c1"


def test_anthropic_to_openai_maps_tools_and_images():
    body = {
        "model": "x", "max_tokens": 50, "system": "sys",
        "tools": [{"name": "weather", "description": "d",
                   "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "image", "source": {"type": "base64",
                                                                      "media_type": "image/png",
                                                                      "data": "AAA"}}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                               "name": "weather", "input": {"city": "Rome"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "20C"}]},
        ],
    }
    out = translate.anthropic_to_openai_request(body)
    assert out["messages"][0] == {"role": "system", "content": "sys"}
    assert out["tool_choice"] == "required"
    assert out["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"city": "Rome"}'
    assert out["messages"][3]["tool_call_id"] == "t1"


def test_response_shapes_roundtrip():
    openai = {"choices": [{"message": {"role": "assistant", "content": "ok",
                                       "tool_calls": [{"id": "c", "type": "function",
                                                       "function": {"name": "f",
                                                                    "arguments": "{}"}}]},
                          "finish_reason": "tool_calls"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    anthropic = translate.openai_to_anthropic_response(openai, "m")
    assert anthropic["stop_reason"] == "tool_use"
    assert anthropic["usage"] == {"input_tokens": 3, "output_tokens": 2}
    back = translate.anthropic_to_openai_response(anthropic, "m")
    assert back["choices"][0]["finish_reason"] == "tool_calls"
    assert back["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "f"
    assert back["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_anthropic_stream_to_openai_emits_tool_deltas():
    async def events():
        yield "message_start", {"type": "message_start"}
        yield "content_block_start", {"content_block": {"type": "tool_use", "id": "t1",
                                                        "name": "weather"}}
        yield "content_block_delta", {"delta": {"type": "input_json_delta",
                                                "partial_json": '{"city":'}}
        yield "message_delta", {"delta": {"stop_reason": "tool_use"},
                                "usage": {"input_tokens": 5, "output_tokens": 2}}

    chunks = [c async for c in translate.anthropic_stream_to_openai(events(), "m")]
    assert chunks[-1].startswith("data: [DONE]")
    joined = "".join(chunks)
    assert '"name":"weather"' in joined
    assert '"finish_reason":"tool_calls"' in joined


@pytest.mark.asyncio
async def test_openai_stream_to_anthropic_emits_message_events():
    async def events():
        yield None, {"choices": [{"delta": {"content": "he"}}]}
        yield None, {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]}
        yield None, "[DONE]"

    out = "".join([c async for c in translate.openai_stream_to_anthropic(events(), "m")])
    assert "content_block_delta" in out and '"text":"he"' in out
    assert '"stop_reason":"end_turn"' in out
    assert "message_stop" in out
