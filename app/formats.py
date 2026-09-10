"""四种客户端格式 <-> 统一 OpenAI Chat Completions 格式的翻译。

统一格式就是 OpenAI 的 /v1/chat/completions 请求体与响应体，
所有入口先翻译成它，缓存和路由都只认这一种。
"""

import json
import time

ENDPOINTS = ("openai", "responses", "anthropic", "gemini")


# ------------------------------------------------------------------ 公共小工具

def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def _anthropic_blocks_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") in ("text", None)
    )


def _gemini_parts_text(parts):
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out)


def _first_choice(u):
    choices = u.get("choices") if isinstance(u, dict) else None
    if isinstance(choices, list) and choices:
        return choices[0] if isinstance(choices[0], dict) else {}
    return {}


# ================================================================== 请求： -> OpenAI

def openai_to_openai(body):
    return dict(body or {})


def anthropic_to_openai(body):
    out = {"model": body.get("model"), "messages": []}

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        out["messages"].append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = _anthropic_blocks_text(system)
        if text.strip():
            out["messages"].append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")

        if isinstance(content, str):
            out["messages"].append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        texts, tool_calls, tool_results = [], [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or "call_1",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": _dumps(block.get("input") or {}),
                    },
                })
            elif btype == "tool_result":
                c = block.get("content")
                if isinstance(c, list):
                    c = _anthropic_blocks_text(c)
                elif c is None:
                    c = ""
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "call_1",
                    "content": c if isinstance(c, str) else _dumps(c),
                })

        out["messages"].extend(tool_results)
        if texts or tool_calls:
            m = {"role": role}
            m["content"] = "".join(texts) if texts else (None if tool_calls else "")
            if tool_calls:
                m["tool_calls"] = tool_calls
            out["messages"].append(m)

    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop")):
        if body.get(src) is not None:
            out[dst] = body[src]

    tools = []
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("name"):
            tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
    if tools:
        out["tools"] = tools

    return out


def gemini_to_openai(body, model):
    out = {"model": model, "messages": []}

    sysinstr = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(sysinstr, dict):
        text = _gemini_parts_text(sysinstr.get("parts"))
        if text.strip():
            out["messages"].append({"role": "system", "content": text})
    elif isinstance(sysinstr, str) and sysinstr.strip():
        out["messages"].append({"role": "system", "content": sysinstr})

    for c in body.get("contents") or []:
        if not isinstance(c, dict):
            continue
        role = "assistant" if c.get("role") == "model" else "user"
        text = _gemini_parts_text(c.get("parts"))
        out["messages"].append({"role": role, "content": text})

    gc = body.get("generationConfig") or body.get("generation_config") or {}
    if isinstance(gc, dict):
        if gc.get("temperature") is not None:
            out["temperature"] = gc["temperature"]
        if gc.get("topP") is not None:
            out["top_p"] = gc["topP"]
        if gc.get("maxOutputTokens") is not None:
            out["max_tokens"] = gc["maxOutputTokens"]
        if gc.get("stopSequences"):
            out["stop"] = gc["stopSequences"]

    return out


def responses_to_openai(body):
    out = {"model": body.get("model"), "messages": []}

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        out["messages"].append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        out["messages"].append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type") or "message"
            if itype == "message":
                role = item.get("role") or "user"
                content = item.get("content")
                if isinstance(content, str):
                    out["messages"].append({"role": role, "content": content})
                elif isinstance(content, list):
                    text = "".join(
                        x.get("text", "") for x in content
                        if isinstance(x, dict) and x.get("type") in
                        ("input_text", "output_text", "text", None)
                    )
                    out["messages"].append({"role": role, "content": text})
            elif itype == "function_call_output":
                out["messages"].append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "call_1",
                    "content": item.get("output") or "",
                })
            elif itype == "function_call":
                out["messages"].append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or "call_1",
                        "type": "function",
                        "function": {"name": item.get("name") or "",
                                     "arguments": item.get("arguments") or "{}"},
                    }],
                })

    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for src in ("temperature", "top_p"):
        if body.get(src) is not None:
            out[src] = body[src]

    tools = []
    for t in body.get("tools") or []:
        if isinstance(t, dict) and (t.get("type") == "function" or t.get("name")):
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name") or "",
                    "description": t.get("description") or "",
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            })
    if tools:
        out["tools"] = tools

    return out


CONVERTERS = {
    "openai": openai_to_openai,
    "anthropic": anthropic_to_openai,
    "responses": responses_to_openai,
}


# ================================================================== 响应：OpenAI ->

def _stop_reason(choice):
    return choice.get("finish_reason") or "stop"


def openai_to_openai_response(u, model=None):
    """把回包里的 model 字段改写成客户端请求的统一模型名。

    客户端请求的是 auto，后台真实可能用掉了 C站/gemini-2.5-pro；客户端
    没必要知道，让它始终只看到 auto。真实站点与真实模型在面板日志里能看到。
    """
    if not isinstance(u, dict) or not model:
        return u
    out = dict(u)
    out["model"] = model
    return out


def openai_to_anthropic(u, model=None):
    choice = _first_choice(u)
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or "toolu_1",
            "name": fn.get("name") or "",
            "input": _loads(fn.get("arguments"), {}),
        })
    if not content:
        content = [{"type": "text", "text": ""}]

    usage = u.get("usage") or {}
    stop = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }.get(_stop_reason(choice), "end_turn")

    return {
        "id": u.get("id") or "msg_relayhub",
        "type": "message",
        "role": "assistant",
        "model": model or u.get("model") or "",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def openai_to_gemini(u, model=None):
    choice = _first_choice(u)
    msg = choice.get("message") or {}
    parts = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        parts.append({"functionCall": {"name": fn.get("name") or "",
                                         "args": _loads(fn.get("arguments"), {})}})
    if not parts:
        parts = [{"text": ""}]

    usage = u.get("usage") or {}
    finish = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "tool_calls": "STOP",
        "content_filter": "SAFETY",
    }.get(_stop_reason(choice), "STOP")

    return {
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": finish,
            "index": 0,
            "safetyRatings": [],
        }],
        "usageMetadata": {
            "promptTokenCount": int(usage.get("prompt_tokens") or 0),
            "candidatesTokenCount": int(usage.get("completion_tokens") or 0),
            "totalTokenCount": int(usage.get("total_tokens") or 0),
        },
        "modelVersion": model or u.get("model") or "",
    }


def openai_to_responses(u, model=None):
    choice = _first_choice(u)
    msg = choice.get("message") or {}
    output = []

    if msg.get("content"):
        output.append({
            "id": "msg_relayhub",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": msg["content"], "annotations": []}],
        })
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        output.append({
            "id": tc.get("id") or f"fc_{i}",
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id") or f"call_{i}",
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
        })
    if not output:
        output.append({
            "id": "msg_relayhub",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })

    usage = u.get("usage") or {}
    return {
        "id": u.get("id") or "resp_relayhub",
        "object": "response",
        "created_at": u.get("created") or int(time.time()),
        "status": "completed",
        "model": model or u.get("model") or "",
        "output": output,
        "output_text": msg.get("content") or "",
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "previous_response_id": None,
        "reasoning": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "truncation": "disabled",
        "user": None,
    }


def upstream_error_payload(body, kind, status, site_name=""):
    """把上游的错误正文尽可能原样还给客户端。

    上游本来就是 OpenAI 兼容的，它的 {"error": {...}} 直接透传对客户端最友好
    （Cherry Studio / RikkaHub 这类客户端能正确显示）；
    解析不出来才退化成我们自己包的壳，避免客户端拿到一坨转义字符串。
    """
    obj = None
    if isinstance(body, str) and body.strip():
        try:
            obj = json.loads(body)
        except Exception:
            obj = None
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = dict(obj["error"])
        err.setdefault("type", kind)
        if site_name:
            err.setdefault("upstream_site", site_name)
        return {"error": err}
    return {"error": {"message": body or "上游拒绝了这次请求",
                      "type": kind, "code": status,
                      "upstream_site": site_name}}


RENDERERS = {
    "openai": openai_to_openai_response,
    "anthropic": openai_to_anthropic,
    "gemini": openai_to_gemini,
    "responses": openai_to_responses,
}


# ================================================================== 流式渲染

def sse(event, data):
    """构造一段 SSE。event 为空则不带 event 行。"""
    out = ""
    if event:
        out += f"event: {event}\n"
    out += "data: " + (data if isinstance(data, str) else _dumps(data)) + "\n\n"
    return out.encode("utf-8")


def sse_data(data):
    return ("data: " + (data if isinstance(data, str) else _dumps(data)) + "\n\n").encode("utf-8")


def chunk_text(u):
    """从统一响应里取出正文与工具调用。"""
    choice = _first_choice(u)
    msg = choice.get("message") or {}
    return msg.get("content") or "", msg.get("tool_calls") or []


def split_pieces(text, size=24):
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def gemini_finish(choice):
    return {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "tool_calls": "STOP",
    }.get(choice.get("finish_reason") or "stop", "STOP")
