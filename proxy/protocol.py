"""
GATP - GenAI Activity Tracking Protocol (v1.0)

A minimal, transport-agnostic event schema for recording every GenAI request
and response that flows through a tracking proxy. Events are published as
JSON over MQTT on topic:  genai/track/{device_id}

Each logical API call produces TWO events sharing the same correlation_id:
  - one with direction="request"  (emitted the instant the call is received)
  - one with direction="response" (emitted once the upstream reply arrives)

This lets the collector reconstruct full call traces, measure latency, and
detect calls that never got a response (e.g. device went offline).
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone
import uuid


PROTOCOL_VERSION = "1.1"


class GATPEvent(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str  # ties the request event to its response event

    device_id: str
    direction: Literal["request", "response"]
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    provider: str            # e.g. "openai", "anthropic", "mock-openai"
    model: Optional[str] = None
    endpoint: str            # path called, e.g. "/v1/chat/completions"
    method: str

    status_code: Optional[int] = None
    latency_ms: Optional[float] = None   # only set on the response event

    request_bytes: Optional[int] = None
    response_bytes: Optional[int] = None

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    body_redacted: bool = True
    body_preview: Optional[str] = None   # truncated / redacted snippet
    error: Optional[str] = None

    rate_limited: bool = False           # true if this call was rejected by the proxy's rate limiter
    full_body_encrypted: Optional[str] = None  # base64 Fernet ciphertext; only set if REPLAY_CAPTURE=true
