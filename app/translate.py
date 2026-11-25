"""Dialect translation between the Anthropic Messages API and the OpenAI ChatGPT API.

Both directions are lossless for the fields that matter: text, images, tools, tool
results, tool_choice, stop sequences, usage and finish reasons.
"""
import json
import uuid

STOP_TO_OPENAI = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "tool_use": "tool_calls", "pause_turn": "stop"}
STOP_TO_ANTHROPIC = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                     "function_call": "tool_use", "content_filter": "end_turn"}


def estimate_tokens(body):
    return len(json.dumps(body)) // 4 + 1


def _text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


# ---------------------------------------------------------------- OpenAI -> Anthropic

def openai_to_anthropic(body):
    """Accept an OpenAI chat body, produce (anthropic_path_payload)."""
    out = {"messages": []}
    system = [m for m in body.get("messages", []) if m.get("role") in ("system", "developer")]
    if system:
        out["system"] = "\n\n".join(_text_of(m.get("content")) for m in system)

    for m in body.get("messages", []):
        role = m.get("role")
        if role in ("system", "developer"):
            continue
        if role == "tool":
            out["messages"].append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": _text_of(m.get("content")),
            }]})
            continue
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": _text_of(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": tc.get("id") or _id("toolu"),
                               "name": fn.get("name", ""), "input": args})
            if blocks:
                out["messages"].append({"role": "assistant", "content": blocks})
            continue
        parts = []
        for part in (m.get("content") if isinstance(m.get("content"), list) else
                     [{"type": "text", "text": m.get("content") or ""}]):
            if part.get("type") == "text":
                parts.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    media, _, data = url.partition(";base64,")
                    parts.append({"type": "image", "source": {
                        "type": "base64", "media_type": media[5:], "data": data}})
                else:
                    parts.append({"type": "image", "source": {"type": "url", "url": url}})
        out["messages"].append({"role": "user", "content": parts or [{"type": "text", "text": ""}]})

    if body.get("tools"):
        out["tools"] = [{"name": t["function"]["name"],
                         "description": t["function"].get("description", ""),
                         "input_schema": t["function"].get("parameters") or {"type": "object"}}
                        for t in body["tools"] if t.get("type", "function") == "function"]
        choice = body.get("tool_choice", "auto")
        if choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif choice == "none":
            out["tool_choice"] = {"type": "none"}
        elif isinstance(choice, dict) and choice.get("function"):
            out["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}

    out["max_tokens"] = body.get("max_completion_tokens") or body.get("max_tokens") or 4096
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("stop", "stop_sequences")):
        if body.get(src) is not None:
            out[dst] = body[src]
    if not isinstance(out.get("stop_sequences", []), list):
        out["stop_sequences"] = [out["stop_sequences"]]
    return out


def anthropic_to_openai_response(resp, model):
    blocks = resp.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    tool_calls = [{"id": b.get("id") or _id("call"), "type": "function",
                   "function": {"name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {}))}}
                  for b in blocks if b.get("type") == "tool_use"]
    usage = resp.get("usage", {})
    msg = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": _id("chatcmpl"),
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "logprobs": None,
                     "finish_reason": STOP_TO_OPENAI.get(resp.get("stop_reason"), "stop")}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
    }


# ---------------------------------------------------------------- Anthropic -> OpenAI

def anthropic_to_openai_request(body):
    out = {"messages": []}
    if body.get("system"):
        out["messages"].append({"role": "system", "content": _text_of(body["system"])})

    for m in body.get("messages", []):
        role = m.get("role")
        blocks = _blocks(m.get("content"))
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id") or _id("call"), "type": "function",
                      "function": {"name": b.get("name", ""),
                                   "arguments": json.dumps(b.get("input", {}))}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out["messages"].append(msg)
            continue
        pending_text, images = [], []
        for b in blocks:
            if b.get("type") == "tool_result":
                out["messages"].append({"role": "tool",
                                        "tool_call_id": b.get("tool_use_id", ""),
                                        "content": _text_of(b.get("content"))})
            elif b.get("type") == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type')};base64,{src.get('data')}"
                else:
                    url = src.get("url", "")
                images.append({"type": "image_url", "image_url": {"url": url}})
            else:
                pending_text.append(b.get("text", ""))
        if pending_text or images:
            parts = [{"type": "text", "text": "".join(pending_text)}] if pending_text else []
            out["messages"].append({"role": "user", "content": parts + images})

    if body.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object"}}} for t in body["tools"]]
        choice = body.get("tool_choice", {"type": "auto"})
        kind = choice.get("type") if isinstance(choice, dict) else "auto"
        out["tool_choice"] = {"any": "required", "tool": {"type": "function", "function": {
            "name": choice.get("name", "")}} if isinstance(choice, dict) else "auto"}.get(kind, kind)

    out["max_tokens"] = body.get("max_tokens") or 4096
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop")):
        if body.get(src) is not None:
            out[dst] = body[src]
    return out


def openai_to_anthropic_response(resp, model):
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    blocks = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        blocks.append({"type": "tool_use", "id": tc.get("id") or _id("toolu"),
                       "name": fn.get("name", ""), "input": args})
    usage = resp.get("usage", {})
    return {
        "id": _id("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": STOP_TO_ANTHROPIC.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# ---------------------------------------------------------------- streaming

def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def sse(event, data):
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, separators=(',', ':'))}\n\n"


def openai_chunk(model, delta, finish=None, usage=None, cid=None):
    chunk = {"id": cid or _id("chatcmpl"), "object": "chat.completion.chunk",
             "created": int(__import__("time").time()), "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return sse(None, chunk)


async def anthropic_stream_to_openai(events, model):
    """events yields (event_name, data_dict) parsed from the upstream Anthropic SSE."""
    cid = _id("chatcmpl")
    yield openai_chunk(model, {"role": "assistant", "content": ""}, cid=cid)
    tool_index = -1
    finish = None
    async for name, data in events:
        if name == "content_block_start":
            block = data.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_index += 1
                yield openai_chunk(model, {"tool_calls": [{
                    "index": tool_index, "id": block.get("id"), "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": ""}}]}, cid=cid)
        elif name == "content_block_delta":
            d = data.get("delta", {})
            if d.get("type") == "text_delta" and d.get("text"):
                yield openai_chunk(model, {"content": d["text"]}, cid=cid)
            elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                yield openai_chunk(model, {"tool_calls": [{
                    "index": max(tool_index, 0),
                    "function": {"arguments": d["partial_json"]}}]}, cid=cid)
        elif name == "message_delta":
            finish = STOP_TO_OPENAI.get((data.get("delta") or {}).get("stop_reason"), finish)
            usage = data.get("usage") or {}
            if usage:
                yield openai_chunk(model, {}, finish=finish or "stop", usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
                    cid=cid)
                finish = None
    if finish:
        yield openai_chunk(model, {}, finish=finish, cid=cid)
    yield "data: [DONE]\n\n"


async def openai_stream_to_anthropic(events, model):
    """events yields (None, data_dict) parsed from the upstream OpenAI SSE; data may be [DONE]."""
    mid = _id("msg")
    yield sse("message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    index, tool_block, usage, finish, started = 0, None, {}, None, False
    async for _, data in events:
        if data == "[DONE]":
            break
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if not started:
            yield sse("content_block_start", {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": "text", "text": ""}})
            started = True
        if delta.get("content"):
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                              "delta": {"type": "text_delta",
                                                        "text": delta["content"]}})
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function", {})
            if tool_block is None or tc.get("id"):
                if started:
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    started = False
                index += 1
                tool_block = {"id": tc.get("id") or _id("toolu"), "name": fn.get("name", "")}
                yield sse("content_block_start", {"type": "content_block_start", "index": index,
                                                  "content_block": {"type": "tool_use",
                                                                    "id": tool_block["id"],
                                                                    "name": tool_block["name"],
                                                                    "input": {}}})
            if fn.get("arguments"):
                yield sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                  "delta": {"type": "input_json_delta",
                                                            "partial_json": fn["arguments"]}})
        if choice.get("finish_reason"):
            finish = STOP_TO_ANTHROPIC.get(choice["finish_reason"], "end_turn")
        if data.get("usage"):
            usage = {"input_tokens": data["usage"].get("prompt_tokens", 0),
                     "output_tokens": data["usage"].get("completion_tokens", 0)}
    if started or tool_block:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
    yield sse("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": finish or "end_turn"},
                                "usage": usage or {"input_tokens": 0, "output_tokens": 0}})
    yield sse("message_stop", {"type": "message_stop"})
