"""
GenAI Tracking Collector (runs on the master machine)
------------------------------------------------------
Subscribes to genai/track/# on the MQTT broker, persists every GATP event
(SQLite or Postgres -- see db.py), and exposes a REST + WebSocket API plus
a small dashboard to query and monitor activity across all devices.
"""
import os
import sys
import json
import asyncio
import logging
from typing import Optional, Set

import paho.mqtt.client as mqtt
import httpx
from fastapi import FastAPI, Query, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import alerts
import anomaly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
import pricing  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("genai-collector")

MQTT_HOST = os.getenv("MQTT_BROKER_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC_SUBSCRIBE", "genai/track/#")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_USE_TLS = os.getenv("MQTT_USE_TLS", "false").lower() == "true"
MQTT_CA_CERT = os.getenv("MQTT_CA_CERT")

COLLECTOR_API_KEY = os.getenv("COLLECTOR_API_KEY")  # if unset, API is open (fine for closed test networks only)
ALERTING_ENABLED = os.getenv("ALERTING_ENABLED", "false").lower() == "true"

# --- Demo sender -----------------------------------------------------------
# Maps device_id -> that device's proxy base URL, reachable from inside the
# collector container. Lets the /demo page send a test call through any
# device's proxy without needing curl in another terminal. Defaults match
# the docker-compose service names; override with DEVICE_PROXIES_JSON if
# you add/rename devices.
DEFAULT_DEVICE_PROXIES = {
    "device-1": "http://device-1:8000",
    "device-2": "http://device-2:8000",
    "device-3": "http://device-3:8000",
}
try:
    DEVICE_PROXIES = json.loads(os.getenv("DEVICE_PROXIES_JSON", "")) or DEFAULT_DEVICE_PROXIES
except json.JSONDecodeError:
    logger.warning("DEVICE_PROXIES_JSON is set but not valid JSON -- falling back to defaults")
    DEVICE_PROXIES = DEFAULT_DEVICE_PROXIES

app = FastAPI(title="GenAI Tracking Collector")

# --- Auth ----------------------------------------------------------------
def require_api_key(x_api_key: Optional[str] = Header(None), api_key: Optional[str] = Query(None)):
    """No-op if COLLECTOR_API_KEY is unset (dev/test mode). When set,
    accepts the key via X-API-Key header or ?api_key= query param (the
    latter so the dashboard's WebSocket, which can't set headers from a
    <script> tag easily, can still authenticate)."""
    if not COLLECTOR_API_KEY:
        return
    supplied = x_api_key or api_key
    if supplied != COLLECTOR_API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# --- WebSocket broadcast ---------------------------------------------------
class Broadcaster:
    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def register(self, ws: WebSocket):
        self._clients.add(ws)

    def unregister(self, ws: WebSocket):
        self._clients.discard(ws)

    def publish_threadsafe(self, event: dict):
        """Called from the MQTT thread (not the asyncio loop) -- hop over
        via call_soon_threadsafe."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(asyncio.create_task, self._broadcast(event))

    async def _broadcast(self, event: dict):
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)


broadcaster = Broadcaster()


# --- MQTT ingest -----------------------------------------------------------
def on_connect(client, userdata, flags, rc):
    logger.info("Collector connected to MQTT broker (rc=%s), subscribing to %s", rc, MQTT_TOPIC)
    client.subscribe(MQTT_TOPIC, qos=1)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        cost = None
        if payload.get("direction") == "response":
            cost = pricing.estimate_cost_usd(
                payload.get("provider"), payload.get("model"),
                payload.get("prompt_tokens"), payload.get("completion_tokens"),
            )
        db.store_event(payload, cost)
        logger.info(
            "Stored %s event %s from %s", payload.get("direction"),
            payload.get("event_id"), payload.get("device_id"),
        )
        broadcaster.publish_threadsafe(payload)
    except Exception as e:
        logger.error("Failed to process message on topic %s: %s", msg.topic, e)


mqtt_client = mqtt.Client(client_id="genai-collector")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
if MQTT_USE_TLS:
    mqtt_client.tls_set(ca_certs=MQTT_CA_CERT)


@app.on_event("startup")
def startup():
    db.init_db()
    broadcaster.bind_loop(asyncio.get_event_loop())
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    if ALERTING_ENABLED:
        alerts.start_background_thread()
    if not COLLECTOR_API_KEY:
        logger.warning("COLLECTOR_API_KEY is unset -- REST/WebSocket API is open to anyone who can reach this port")


@app.on_event("shutdown")
def shutdown():
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


# --- REST API ----------------------------------------------------------------
@app.get("/events", dependencies=[Depends(require_api_key)])
def list_events(device_id: Optional[str] = None, limit: int = Query(50, le=1000)):
    if device_id:
        rows = db.query(
            "SELECT raw_json FROM events WHERE device_id=? ORDER BY received_at DESC LIMIT ?",
            (device_id, limit),
        )
    else:
        rows = db.query("SELECT raw_json FROM events ORDER BY received_at DESC LIMIT ?", (limit,))
    events = [json.loads(r["raw_json"]) for r in rows]
    return {"count": len(events), "events": events}


@app.get("/traces/{correlation_id}", dependencies=[Depends(require_api_key)])
def get_trace(correlation_id: str):
    """Return the matched request+response pair for one call, plus
    derived latency/cost, rather than making the caller reconstruct it
    from two separate /events rows."""
    rows = db.query(
        "SELECT raw_json FROM events WHERE correlation_id=? ORDER BY direction",
        (correlation_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="no events found for that correlation_id")
    events = [json.loads(r["raw_json"]) for r in rows]
    request_ev = next((e for e in events if e.get("direction") == "request"), None)
    response_ev = next((e for e in events if e.get("direction") == "response"), None)
    cost = None
    if response_ev:
        cost = pricing.estimate_cost_usd(
            response_ev.get("provider"), response_ev.get("model"),
            response_ev.get("prompt_tokens"), response_ev.get("completion_tokens"),
        )
    return {
        "correlation_id": correlation_id,
        "request": request_ev,
        "response": response_ev,
        "complete": bool(request_ev and response_ev),
        "estimated_cost_usd": cost,
    }


@app.get("/stats", dependencies=[Depends(require_api_key)])
def stats():
    rows = db.query(f"""
        SELECT device_id, provider,
               COUNT(*) AS responses,
               AVG(latency_ms) AS avg_latency_ms,
               SUM(total_tokens) AS total_tokens,
               SUM(estimated_cost_usd) AS estimated_cost_usd,
               SUM(CASE WHEN status_code >= 400 OR status_code IS NULL THEN 1 ELSE 0 END) AS error_count,
               SUM(CASE WHEN {db.rate_limited_true()} THEN 1 ELSE 0 END) AS rate_limited_count
        FROM events
        WHERE direction = 'response'
        GROUP BY device_id, provider
    """)
    return {"by_device": rows}


@app.get("/anomalies", dependencies=[Depends(require_api_key)])
def get_anomalies():
    findings = anomaly.run_detection()
    return {"count": len(findings), "findings": findings}


@app.get("/health")
def health():
    return {"status": "ok", "db_backend": db.DB_BACKEND}


@app.get("/dashboard")
def dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))


@app.get("/events/view")
def events_view():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "events.html"))


# --- Demo sender -------------------------------------------------------------
# Lets someone send a test GenAI call through a chosen device's proxy from a
# browser, instead of running curl in another terminal. The collector relays
# the call server-side (it's already on the same Docker network as every
# proxy), so there's no CORS setup needed on the proxy itself.
class DemoSendRequest(BaseModel):
    device_id: str
    message: str
    model: str = "mock-model"
    stream: bool = False


@app.get("/demo")
def demo_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "demo.html"))


@app.get("/demo/devices", dependencies=[Depends(require_api_key)])
def demo_devices():
    return {"devices": sorted(DEVICE_PROXIES.keys())}


@app.post("/demo/send", dependencies=[Depends(require_api_key)])
async def demo_send(req: DemoSendRequest):
    base_url = DEVICE_PROXIES.get(req.device_id)
    if not base_url:
        raise HTTPException(
            status_code=404,
            detail=f"unknown device_id '{req.device_id}'; known devices: {sorted(DEVICE_PROXIES.keys())}",
        )

    body = {
        "model": req.model,
        "messages": [{"role": "user", "content": req.message}],
        "stream": req.stream,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{base_url}/v1/chat/completions", json=body)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not reach {req.device_id}'s proxy: {e}")

    try:
        response_json = resp.json()
    except Exception:
        response_json = {"raw_text": resp.text}

    return {
        "device_id": req.device_id,
        "status_code": resp.status_code,
        "response": response_json,
    }


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, api_key: Optional[str] = Query(None)):
    if COLLECTOR_API_KEY and api_key != COLLECTOR_API_KEY:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await broadcaster.register(websocket)
    try:
        while True:
            # We don't expect inbound messages, but need to await something
            # to detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unregister(websocket)
