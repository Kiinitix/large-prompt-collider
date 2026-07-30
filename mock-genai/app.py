"""
Mock GenAI provider - mimics an OpenAI-style chat completions endpoint so
the whole tracking pipeline can be exercised in Docker without any real
API keys or external network access.
"""
import asyncio
import json
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="Mock GenAI Provider")

_MOCK_REPLY = "This is a mock GenAI response used for testing the tracking pipeline."


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    prompt_tokens = sum(len(m.get("content", "").split()) for m in body.get("messages", []))
    completion_tokens = random.randint(10, 60)

    if body.get("stream"):
        return StreamingResponse(
            _stream_chunks(body.get("model", "mock-model")),
            media_type="text/event-stream",
        )

    time.sleep(random.uniform(0.05, 0.3))  # simulate model latency

    return {
        "id": f"mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "model": body.get("model", "mock-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _MOCK_REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _stream_chunks(model: str):
    """Emit an OpenAI-style SSE stream, one word at a time."""
    chunk_id = f"mock-{uuid.uuid4().hex[:12]}"
    for word in _MOCK_REPLY.split(" "):
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        await asyncio.sleep(0.02)
    final = {
        "id": chunk_id, "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@app.get("/health")
def health():
    return {"status": "ok"}
