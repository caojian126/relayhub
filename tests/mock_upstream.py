"""端到端测试用的假中转站（OpenAI 兼容）。

故意支持几种“坏行为”，用来验证网关的故障切换判定：

  gpt-5 / gpt-5-mini / claude-sonnet-4-5 / gemini-2.5-pro   正常模型
  X-fail-500     永远 503                 -> 应该切下一个节点（可重试）
  X-missing      永远 404 模型不存在        -> 应该切下一个节点（可重试）
  X-fail-400     永远 400 参数错误          -> 不该切节点，错误原样回给客户端（不可重试）
  X-stream-break 流式吐 2 个 chunk 后粗暴断开 -> 应该「不重试、不切换」

回答正文里带端口号，方便确认到底是哪个站点接的。
"""

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

PORT = int(os.getenv("MOCK_PORT", "8101"))

MODELS = [
    "gpt-5",
    "gpt-5-mini",
    "claude-sonnet-4-5",
    "gemini-2.5-pro",
    "X-fail-500",
    "X-missing",
    "X-fail-400",
    "X-stream-break",
]

app = FastAPI()


def _err(status, msg, typ="invalid_request_error"):
    return JSONResponse(
        {"error": {"message": msg, "type": typ, "code": typ}}, status_code=status
    )


@app.get("/v1/models")
async def models(request: Request):
    auth = request.headers.get("authorization", "") or ""
    if not auth.lower().startswith("bearer ") or len(auth) < 12:
        return _err(401, "missing or bad api key", "invalid_api_key")
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]}


def _chunk(model, text):
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body is not json")
    model = str(body.get("model") or "")

    # ---- 故意制造的坏行为 ----
    if model == "X-fail-500":
        return _err(503, "upstream temporarily unavailable", "server_error")
    if model == "X-missing":
        return _err(404, "The model `X-missing` does not exist", "model_not_found")
    if model == "X-fail-400":
        return _err(400, "invalid parameter: temperature must be <= 2", "invalid_request_error")

    text = f"[端口{PORT} 的 {model} 回答]"

    if body.get("stream"):
        async def gen():
            yield _sse({"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model,
                        "choices": [{"index": 0,
                                     "delta": {"role": "assistant", "content": ""},
                                     "finish_reason": None}]})
            if model == "X-stream-break":
                yield _sse(_chunk(model, "已经开始输出了"))
                await asyncio.sleep(0.05)
                # 粗暴断开：模拟上游说了一半就挂掉
                raise RuntimeError("upstream socket died mid-stream")
            for piece in ["你好", "，这是", f" {model} ", "的流式回答"]:
                yield _sse(_chunk(model, piece))
                await asyncio.sleep(0.02)
            yield _sse({**_chunk(model, ""),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                yield _sse({"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model, "choices": [],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                      "total_tokens": 18}})
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
