"""
GenAI Tracking Proxy
---------------------
Sits in front of a real (or mock) GenAI API. Every request that hits this
proxy is transparently forwarded to TARGET_BASE_URL, and both the request
and the response are logged as GATP events published over MQTT to the
master collector. The calling application needs zero code changes -- it
just points its base_url at this proxy instead of the real provider.

Features beyond the original pass-through-and-log behavior:
  - streaming responses (SSE / chunked) are streamed through, not buffered
  - per-device token-bucket rate limiting
  - real body redaction (shared/redact.py) instead of a byte-count placeholder
  - optional MQTT username/password auth (+ TLS)
  - optional encrypted full-body capture for later replay (off by default)
"""
import os
import sys
import time
import json
import uuid
import base64
import logging
from typing import Optional

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from protocol import GATPEvent
from ratelimit import TokenBucket

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
import redact  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("genai-proxy")

DEVICE_ID = os.getenv("DEVICE_ID", "device-unknown")
TARGET_BASE_URL = os.getenv("TARGET_BASE_URL", "https://api.openai.com").rstrip("/")
PROVIDER_NAME = os.getenv("PROVIDER_NAME", "openai")

MQTT_HOST = os.getenv("MQTT_BROKER_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "genai/track")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")  # optional
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")  # optional
MQTT_USE_TLS = os.getenv("MQTT_USE_TLS", "false").lower() == "true"
MQTT_CA_CERT = os.getenv("MQTT_CA_CERT")  # path, required if MQTT_USE_TLS

PREVIEW_CHARS = int(os.getenv("BODY_PREVIEW_CHARS", "200"))
REDACT_BODY = os.getenv("REDACT_BODY", "true").lower() == "true"

# --- Rate limiting -----------------------------------------------------
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "false").lower() == "true"
RATE_LIMIT_RPS = float(os.getenv("RATE_LIMIT_RPS", "5"))
RATE_LIMIT_BURST = int(os.getenv("RATE_LIMIT_BURST", "10"))
_bucket = TokenBucket(RATE_LIMIT_RPS, RATE_LIMIT_BURST) if RATE_LIMIT_ENABLED else None

# --- Replay capture (off by default; stores full bodies, encrypted) ----
REPLAY_CAPTURE = os.getenv("REPLAY_CAPTURE", "false").lower() == "true"
REPLAY_ENCRYPTION_KEY = os.getenv("REPLAY_ENCRYPTION_KEY")  # Fernet key, base64 urlsafe 32 bytes
_fernet = None
if REPLAY_CAPTURE:
    if not REPLAY_ENCRYPTION_KEY:
        logger.warning("REPLAY_CAPTURE=true but REPLAY_ENCRYPTION_KEY is unset -- disabling replay capture")
        REPLAY_CAPTURE = False
    else:
        from cryptography.fernet import Fernet
        _fernet = Fernet(REPLAY_ENCRYPTION_KEY.encode())

app = FastAPI(title=f"GenAI Tracking Proxy [{DEVICE_ID}]")

mqtt_client = mqtt.Client(client_id=f"proxy-{DEVICE_ID}", clean_session=True)
if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
if MQTT_USE_TLS:
    mqtt_client.tls_set(ca_certs=MQTT_CA_CERT)


@app.on_event("startup")
def startup() -> None:
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    logger.info(
        "Connected to MQTT broker at %s:%s (tls=%s, auth=%s)",
        MQTT_HOST, MQTT_PORT, MQTT_USE_TLS, bool(MQTT_USERNAME),
    )
    if RATE_LIMIT_ENABLED:
        logger.info("Rate limiting enabled: %.2f req/s, burst %d", RATE_LIMIT_RPS, RATE_LIMIT_BURST)
    if REPLAY_CAPTURE:
        logger.info("Replay capture enabled -- full request bodies will be stored encrypted")


@app.on_event("shutdown")
def shutdown() -> None:
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


def publish_event(event: GATPEvent) -> None:
    topic = f"{MQTT_TOPIC_BASE}/{DEVICE_ID}"
    payload = event.model_dump_json()
    result = mqtt_client.publish(topic, payload, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning("Failed to publish event %s (rc=%s)", event.event_id, result.rc)


def make_preview(body: bytes) -> Optional[str]:
    return redact.make_preview(body, redact=REDACT_BODY, preview_chars=PREVIEW_CHARS)


def encrypt_body(body: bytes) -> Optional[str]:
    if not REPLAY_CAPTURE or not body:
        return None
    return base64.urlsafe_b64encode(_fernet.encrypt(body)).decode()


def _is_streaming_request(body: bytes) -> bool:
    try:
        if body:
            return bool(json.loads(body).get("stream"))
    except Exception:
        pass
    return False


@app.get("/_proxy/health")
def health():
    return {
        "status": "ok",
        "device_id": DEVICE_ID,
        "target": TARGET_BASE_URL,
        "rate_limit_enabled": RATE_LIMIT_ENABLED,
        "replay_capture": REPLAY_CAPTURE,
    }


# NOTE: this catch-all MUST be registered after /_proxy/health above --
# Starlette matches routes in registration order, not by specificity, so a
# catch-all registered first would silently swallow /_proxy/health and
# forward it upstream instead of answering locally (that was a latent bug
# in the original version of this file).
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    correlation_id = str(uuid.uuid4())
    body = await request.body()
    target_url = f"{TARGET_BASE_URL}/{path}"

    # --- Rate limiting ---------------------------------------------------
    if _bucket is not None and not _bucket.try_consume():
        publish_event(GATPEvent(
            correlation_id=correlation_id,
            device_id=DEVICE_ID,
            direction="response",
            provider=PROVIDER_NAME,
            endpoint=f"/{path}",
            method=request.method,
            status_code=429,
            rate_limited=True,
            error="rate_limited",
        ))
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "retry_after_seconds": _bucket.retry_after_seconds()},
            headers={"Retry-After": str(max(1, int(_bucket.retry_after_seconds())))},
        )

    forward_headers = dict(request.headers)
    forward_headers.pop("host", None)

    model_name = None
    try:
        if body:
            parsed = json.loads(body)
            model_name = parsed.get("model")
    except Exception:
        pass

    publish_event(GATPEvent(
        correlation_id=correlation_id,
        device_id=DEVICE_ID,
        direction="request",
        provider=PROVIDER_NAME,
        model=model_name,
        endpoint=f"/{path}",
        method=request.method,
        request_bytes=len(body) if body else 0,
        body_preview=make_preview(body),
        body_redacted=REDACT_BODY,
        full_body_encrypted=encrypt_body(body),
    ))

    streaming = _is_streaming_request(body)
    start = time.perf_counter()

    if streaming:
        return await _proxy_streaming(request, path, target_url, body, forward_headers, correlation_id, model_name, start)
    return await _proxy_buffered(request, path, target_url, body, forward_headers, correlation_id, model_name, start)


async def _proxy_buffered(request, path, target_url, body, forward_headers, correlation_id, model_name, start):
    error_msg = None
    status_code = None
    resp_body = b""
    resp_headers: dict = {}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            upstream_resp = await client.request(
                request.method, target_url, headers=forward_headers,
                content=body, params=request.query_params,
            )
        status_code = upstream_resp.status_code
        resp_body = upstream_resp.content
        resp_headers = dict(upstream_resp.headers)
    except Exception as e:
        error_msg = str(e)
        status_code = 502
        logger.error("Upstream call failed: %s", error_msg)

    latency_ms = (time.perf_counter() - start) * 1000

    usage = {}
    try:
        if resp_body:
            parsed_resp = json.loads(resp_body)
            usage = parsed_resp.get("usage", {}) or {}
    except Exception:
        pass

    publish_event(GATPEvent(
        correlation_id=correlation_id,
        device_id=DEVICE_ID,
        direction="response",
        provider=PROVIDER_NAME,
        model=model_name,
        endpoint=f"/{path}",
        method=request.method,
        status_code=status_code,
        latency_ms=round(latency_ms, 2),
        response_bytes=len(resp_body) if resp_body else 0,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        body_preview=make_preview(resp_body),
        body_redacted=REDACT_BODY,
        error=error_msg,
    ))

    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    return Response(content=resp_body, status_code=status_code or 500, headers=resp_headers)


async def _proxy_streaming(request, path, target_url, body, forward_headers, correlation_id, model_name, start):
    """Stream the upstream response through chunk-by-chunk (for SSE/chunked
    completions) instead of buffering it, while still capturing byte count,
    latency, and a preview of the first chunk for tracking purposes."""
    collected = bytearray()
    error_msg = None
    status_holder = {"code": None}
    resp_headers_holder: dict = {}

    async def event_stream():
        nonlocal error_msg
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    request.method, target_url, headers=forward_headers,
                    content=body, params=request.query_params,
                ) as upstream_resp:
                    status_holder["code"] = upstream_resp.status_code
                    resp_headers_holder.update(dict(upstream_resp.headers))
                    async for chunk in upstream_resp.aiter_bytes():
                        if len(collected) < PREVIEW_CHARS * 4:  # cap what we buffer for preview/replay
                            collected.extend(chunk)
                        yield chunk
        except Exception as e:
            error_msg = str(e)
            status_holder["code"] = 502
            logger.error("Upstream streaming call failed: %s", error_msg)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            publish_event(GATPEvent(
                correlation_id=correlation_id,
                device_id=DEVICE_ID,
                direction="response",
                provider=PROVIDER_NAME,
                model=model_name,
                endpoint=f"/{path}",
                method=request.method,
                status_code=status_holder["code"],
                latency_ms=round(latency_ms, 2),
                response_bytes=len(collected),
                body_preview=make_preview(bytes(collected)),
                body_redacted=REDACT_BODY,
                error=error_msg,
                # NOTE: prompt/completion token counts usually aren't
                # available for streaming responses until the final SSE
                # chunk (or aren't sent at all by some providers); left
                # null here rather than guessed.
            ))

    media_type = "text/event-stream"
    return StreamingResponse(event_stream(), media_type=media_type)
