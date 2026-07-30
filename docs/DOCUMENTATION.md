# Large Prompt Collider (LPC) - Documentation

Complete reference for setup, configuration, and operation. For a quick overview, see the top-level `README.md`; this document goes deeper on running the pieces individually, configuration, and troubleshooting.

## Table of contents

1. [What this project does](#what-this-project-does)
2. [Components](#components)
3. [The GATP protocol](#the-gatp-protocol)
4. [Quick start (Docker Compose)](#quick-start-docker-compose)
5. [Running components individually (no Docker)](#running-components-individually-no-docker)
6. [Configuration reference](#configuration-reference)
7. [REST API reference](#rest-api-reference)
8. [New features (July 2026 update)](#new-features-july-2026-update)
9. [Deploying to real devices](#deploying-to-real-devices)
10. [Security notes](#security-notes)
11. [Troubleshooting](#troubleshooting)

---

## What this project does

Teams running GenAI calls across many machines (edge devices, workstations, services) often have no central visibility into who is calling which model, how often, how much it costs (in tokens), or how slow/error-prone it is.

This project inserts a small transparent proxy in front of each device's GenAI calls. The proxy needs **no application code changes** you just repoint the `base_url` your app already uses. Every call is forwarded unmodified, while a copy of the metadata (never full bodies, by default) is shipped to a central collector over MQTT.

## Components

| Component | Runs where | Purpose |
|---|---|---|
| `proxy/` | One per device | Reverse proxy; forwards calls untouched, emits GATP events |
| `mosquitto/` | Master machine | MQTT broker; receives events from all proxies |
| `collector/` | Master machine | Subscribes to all events, persists to SQLite, exposes REST API |
| `mock-genai/` | Test only | Fake OpenAI-style `/v1/chat/completions` endpoint, no API key needed |

### Proxy (`proxy/app.py`)

A FastAPI app that catch-all-routes every path/method (`/{path:path}`), forwards it to `TARGET_BASE_URL` with headers intact (so your existing `Authorization` header still works), and publishes a `request` event before forwarding and a `response` event after the upstream reply lands. It reads the JSON body only to pull out `model` and, from the response, the `usage` block everything else passes through as raw bytes.

Health check: `GET /_proxy/health`.

### Mosquitto

Off-the-shelf `eclipse-mosquitto:2` broker. Configured (see `mosquitto/config/mosquitto.conf`) for plain `1883` with anonymous access fine for a closed test network, **not** fine for anything exposed (see [Security notes](#security-notes)).

### Collector (`collector/app.py`)

Subscribes to `genai/track/#`, writes every event into a SQLite table (`events`), and serves:
- `GET /events`: recent raw events, optionally filtered by `device_id`
- `GET /stats`: aggregated per-device/provider stats (count, avg latency,
  total tokens, error count)
- `GET /health`

The whole table schema is derived from the `COLUMNS` list in `app.py`, so adding a field to the protocol means adding it there too (see [Extending the protocol](#extending-the-protocol) below).

### mock-genai

A trivial stand-in for a real provider, accepts `POST /v1/chat/completions`, sleeps briefly to simulate latency, and returns an OpenAI-shaped response with a fabricated `usage` block. Only used in the Compose test setup; swap it for a real `TARGET_BASE_URL` in production (see [Pointing a device at a real provider](#pointing-a-device-at-a-real-provider)).

## The GATP protocol

Defined in `proxy/protocol.py` as a Pydantic model (`GATPEvent`), published as JSON to MQTT topic `genai/track/{device_id}`.

Every logical API call produces **two** events sharing a `correlation_id`: a `request` event fired the instant the call arrives at the proxy, and a `response` event fired once the upstream reply comes back (or fails). This lets the collector reconstruct full traces, compute latency, and flag calls that never got a response (device went offline mid-call).

| Field | Type | Set on | Notes |
|---|---|---|---|
| `protocol_version` | string | both | currently `"1.0"` |
| `event_id` | string (UUID) | both | unique per event |
| `correlation_id` | string (UUID) | both | same value links request ↔ response |
| `device_id` | string | both | from `DEVICE_ID` env var |
| `direction` | `"request"` \| `"response"` | both | |
| `timestamp` | ISO 8601 | both | UTC |
| `provider` | string | both | e.g. `openai`, `mock-openai` |
| `model` | string \| null | both | parsed from request body's `model` field |
| `endpoint` | string | both | path called, e.g. `/v1/chat/completions` |
| `method` | string | both | HTTP method |
| `status_code` | int \| null | response only | 502 if upstream call itself failed |
| `latency_ms` | float \| null | response only | round-trip time |
| `request_bytes` / `response_bytes` | int | both | body size |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | int \| null | response only | pulled from provider's `usage` field, if present |
| `body_redacted` | bool | both | always `true` unless `REDACT_BODY=false` |
| `body_preview` | string \| null | both | redacted (or, if disabled, truncated plaintext) snippet |
| `error` | string \| null | response only | set if the upstream call raised an exception |
| `rate_limited` | bool | response only | `true` if this call was rejected by the proxy's rate limiter |
| `full_body_encrypted` | string \| null | request only | base64 Fernet ciphertext of the full body; only set if `REPLAY_CAPTURE=true` |

Protocol version is `1.1` (bumped from `1.0` when `rate_limited` and `full_body_encrypted` were added both are additive, so `1.0` consumers that ignore unknown fields still work).

> **Redaction is now a real (regex-based) scrubber**, not a placeholder, see [New features](#new-features-july-2026-update) below. It still isn't a full PII-detection model; for regulated data, pair it with a proper DLP tool.

## Quick start (Docker Compose)

**Prerequisites:** Docker + Docker Compose plugin.

```bash
docker compose up --build
```

This brings up 6 containers:

| Service | Container | Exposed port |
|---|---|---|
| mosquitto | `mosquitto` | `1883` |
| collector | `collector` | `9000` → container `8000` |
| mock-genai | `mock-genai` | *(internal only)* |
| device-1 | `device-1` | `8101` → container `8000` |
| device-2 | `device-2` | `8102` → container `8000` |
| device-3 | `device-3` | `8103` → container `8000` |

Send a test call through device-1's proxy:

```bash
curl http://localhost:8101/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-model","messages":[{"role":"user","content":"Hello there"}]}'
```

Check what the collector captured:

```bash
curl http://localhost:9000/events | jq
curl http://localhost:9000/stats  | jq
```

Or skip curl entirely and open **http://localhost:9000/demo** in a browser, pick a device, type a message, hit Send. There's also a live dashboard at **http://localhost:9000/dashboard**.

Tear down (and wipe stored data):

```bash
docker compose down -v
```

## Running components individually (no Docker)

Useful for local development/debugging one piece at a time.

**1. Start Mosquitto** (or use any MQTT broker you already have):

```bash
docker run --rm -p 1883:1883 \
  -v $(pwd)/mosquitto/config/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro \
  eclipse-mosquitto:2
```

**2. Run the collector:**

```bash
cd collector
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
MQTT_BROKER_HOST=localhost DB_PATH=./events.db \
  uvicorn app:app --host 0.0.0.0 --port 8000
```

**3. Run the mock provider:**

```bash
cd mock-genai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8010
```

**4. Run a proxy pointed at the mock provider:**

```bash
cd proxy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DEVICE_ID=device-local \
MQTT_BROKER_HOST=localhost \
TARGET_BASE_URL=http://localhost:8010 \
PROVIDER_NAME=mock-openai \
  uvicorn app:app --host 0.0.0.0 --port 8080
```

Then hit `http://localhost:8080/v1/chat/completions` as above, and query `http://localhost:8000/events`.

## Configuration reference

All configuration is via environment variables no config files to edit beyond `mosquitto.conf`.

### Proxy

| Variable | Default | Purpose |
|---|---|---|
| `DEVICE_ID` | `device-unknown` | Identifies this proxy in events and the MQTT topic |
| `TARGET_BASE_URL` | `https://api.openai.com` | Real (or mock) upstream API base URL |
| `PROVIDER_NAME` | `openai` | Free-text label stored in events |
| `MQTT_BROKER_HOST` | `mosquitto` | Broker hostname |
| `MQTT_BROKER_PORT` | `1883` | Broker port |
| `MQTT_TOPIC_BASE` | `genai/track` | Events publish to `{base}/{device_id}` |
| `BODY_PREVIEW_CHARS` | `200` | Preview length when redaction is off |
| `REDACT_BODY` | `true` | Set `false` to store truncated plaintext previews instead of scrubbed ones |
| `RATE_LIMIT_ENABLED` | `false` | Enable per-device token-bucket rate limiting |
| `RATE_LIMIT_RPS` / `RATE_LIMIT_BURST` | `5` / `10` | Rate limiter tuning |
| `REPLAY_CAPTURE` | `false` | Encrypt + store full request bodies for later replay |
| `REPLAY_ENCRYPTION_KEY` | *(none)* | Fernet key, required if `REPLAY_CAPTURE=true` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(none)* | MQTT auth, if enabled on the broker |
| `MQTT_USE_TLS` / `MQTT_CA_CERT` | `false` / *(none)* | MQTT TLS, if enabled on the broker |

### Collector

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_BROKER_HOST` | `mosquitto` | Broker hostname |
| `MQTT_BROKER_PORT` | `1883` | Broker port |
| `MQTT_TOPIC_SUBSCRIBE` | `genai/track/#` | Subscription wildcard |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(none)* | MQTT auth, if enabled on the broker |
| `MQTT_USE_TLS` / `MQTT_CA_CERT` | `false` / *(none)* | MQTT TLS, if enabled on the broker |
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgres` |
| `DB_PATH` | `/data/events.db` | SQLite file location (sqlite backend only) |
| `PG_HOST` / `PG_PORT` / `PG_DB` / `PG_USER` / `PG_PASSWORD` | see `docker-compose.yml` | Postgres connection (postgres backend only) |
| `COLLECTOR_API_KEY` | *(none)* | If set, required on all endpoints except `/health` |
| `ALERTING_ENABLED` | `false` | Enable background error-rate/latency alerting |
| `ALERT_WEBHOOK_URL` | *(none)* | Webhook target for alerts (e.g. Slack incoming webhook) |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.25` | Fraction of failed calls (0–1) to trigger on |
| `ALERT_LATENCY_MS_THRESHOLD` | `5000` | Avg latency (ms) to trigger on |
| `ALERT_WINDOW_MINUTES` | `10` | Rolling window for alert checks |
| `PRICING_OVERRIDES_JSON` | *(none)* | Override/extend the cost-estimation pricing table |

## REST API reference

All served by the **collector**, base URL `http://<master>:9000` in the Compose setup (internal port `8000`).

### `GET /events`

Query params:
- `device_id` *(optional)* — filter to one device
- `limit` *(default 50, max 1000)*

Returns: `{ "count": <int>, "events": [ <GATPEvent as JSON>, ... ] }`, newest first.

### `GET /stats`

No params. Returns per `(device_id, provider)` aggregates computed only over `response` events:

```json
{
  "by_device": [
    {
      "device_id": "device-1",
      "provider": "mock-openai",
      "responses": 12,
      "avg_latency_ms": 143.2,
      "total_tokens": 980,
      "estimated_cost_usd": 0.0021,
      "error_count": 0,
      "rate_limited_count": 0
    }
  ]
}
```

### `GET /health`

`{ "status": "ok", "db_backend": "sqlite" }` — for liveness checks. Never requires an API key even when `COLLECTOR_API_KEY` is set.

### `GET /traces/{correlation_id}`

Returns `{ "correlation_id", "request", "response", "complete", "estimated_cost_usd" }` the matched request+response pair for one call.

### `GET /anomalies`

Runs the model-switch and token-spike heuristics on demand. Returns `{ "count": <int>, "findings": [ { "device_id", "kind", "detail" }, ... ] }`.

### `GET /dashboard`

Serves the live HTML dashboard. Append `?api_key=...` if `COLLECTOR_API_KEY` is set.

### `GET /demo`, `GET /demo/devices`, `POST /demo/send`

The demo sender page and its two supporting endpoints  see [New features](#new-features-july-2026-update) for details.

### `WS /ws/events`

Broadcasts every event as it's stored. Auth via `?api_key=...` (same key as REST).

### Proxy's own endpoint

`GET /_proxy/health` on each proxy → `{ "status": "ok", "device_id": ..., "target": ..., "rate_limit_enabled": ..., "replay_capture": ... }`.

## Pointing a device at a real provider

Edit the relevant service block in `docker-compose.yml` (or the env vars when running standalone):

```yaml
environment:
  TARGET_BASE_URL: https://api.openai.com
  PROVIDER_NAME: openai
```

Your app then calls the proxy exactly as it would call the real API same path, same `Authorization` header — and the proxy forwards headers through unchanged. It only inspects the JSON body to pull `model` and `usage`.

## New features (July 2026 update)

Everything below was added on top of the original pass-through-and-log system. All of it is off or backward-compatible by default, so the original `docker compose up --build` quick start still works unchanged. Every feature listed here was exercised against a live proxy → Mosquitto → collector pipeline (and, where noted, a real Postgres instance) while building it, not just written and assumed to work.

### Streaming responses (proxy)

The proxy used to buffer the entire upstream response before returning it, which breaks real chat completion APIs using SSE/chunked streaming.

It now detects `"stream": true` in the request body and streams the response through chunk-by-chunk via `StreamingResponse`, while still capturing latency, byte count, and a preview for tracking. Token counts aren't published for streaming responses (most providers don't include a `usage` block until/unless the final chunk, and not all send one at all).

### Per-device rate limiting (proxy)

A token-bucket limiter, off by default. Enable per-device:

| Variable | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `false` | turn on |
| `RATE_LIMIT_RPS` | `5` | sustained requests/sec |
| `RATE_LIMIT_BURST` | `10` | burst capacity |

Rejected calls get a `429` with a `Retry-After` header and are still
logged as a GATP event (`rate_limited: true`), so you can see rate-limit
hits in `/stats`.

### Real body redaction (proxy)

`shared/redact.py` replaces the old byte-count-only placeholder with an actual regex-based scrubber for API keys/bearer tokens, emails, phone numbers, credit card numbers, SSNs, and IPv4 addresses matches are replaced with `[REDACTED:<label>]` so the shape of the text stays visible without leaking the value. It's still not a full PII-detection model; for regulated data, swap in something like Microsoft Presidio.

### Encrypted replay capture (proxy)

Off by default. When enabled, the proxy Fernet-encrypts the *full* raw request body (not just the redacted preview) and includes it in the `request` event as `full_body_encrypted`. This lets someone with the key decrypt and manually resend a specific failing call for debugging, without storing plaintext bodies in the events DB.

| Variable | Default | Purpose |
|---|---|---|
| `REPLAY_CAPTURE` | `false` | turn on |
| `REPLAY_ENCRYPTION_KEY` | *(none)* | Fernet key; generate with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

Decrypt manually:
```python
from cryptography.fernet import Fernet
import base64
f = Fernet(REPLAY_ENCRYPTION_KEY.encode())
plaintext = f.decrypt(base64.urlsafe_b64decode(event["full_body_encrypted"]))
```
Treat the key like any other secret, anyone with it can read every captured body. There's no dedicated `/replay` API endpoint; decrypt out of band and resend with `curl`/your own script.

### Cost tracking (`shared/pricing.py`, collector)

A small hand-maintained USD-per-1k-token table for common OpenAI/Anthropic models (see the file for the full list). The collector computes `estimated_cost_usd` per response event and aggregates it in `/stats`.

Unknown provider/model pairs return `null` rather than `0.0`, so you can tell "known free" apart from "not priced yet." Override or extend the table via:

```bash
PRICING_OVERRIDES_JSON='{"openai":{"gpt-4o":{"input_per_1k":0.0025,"output_per_1k":0.01,"context_window":128000}}}'
```

These are approximate defaults for relative cost tracking across a fleet, not exact enough for invoicing, check current provider pricing if you need precise numbers.

### Trace pairing endpoint

`GET /traces/{correlation_id}` returns the matched request+response pair for one call as a single object (`request`, `response`, `complete`, `estimated_cost_usd`), instead of making you reconstruct it from two separate `/events` rows.

### Collector API key auth

`COLLECTOR_API_KEY`, unset by default (open API, fine for closed test networks only matches the original behavior). When set, every REST endpoint except `/health` requires it via the `X-API-Key` header or `?api_key=` query param (the dashboard's WebSocket connection uses the query-param form since browsers can't set custom headers on a raw WebSocket handshake).

### Alerting (collector)

A background thread checks each device's error rate and average latency over a rolling window and POSTs a webhook (e.g. a Slack incoming webhook) when a threshold is crossed, with a cooldown so it doesn't spam.

| Variable | Default | Purpose |
|---|---|---|
| `ALERTING_ENABLED` | `false` | turn on |
| `ALERT_WEBHOOK_URL` | *(none)* | POST target; without it, alerts just log |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.25` | fraction (0–1) of failed calls to trigger on |
| `ALERT_LATENCY_MS_THRESHOLD` | `5000` | avg latency (ms) to trigger on |
| `ALERT_WINDOW_MINUTES` | `10` | rolling window size |
| `ALERT_CHECK_INTERVAL_SEC` | `60` | how often to check |
| `ALERT_MIN_SAMPLES` | `5` | don't alert on tiny sample sizes |
| `ALERT_COOLDOWN_SEC` | `900` | minimum gap between repeat alerts for the same device+kind |

### Anomaly detection

`GET /anomalies` runs two on-demand heuristics per device (not a continuous background job, cheap enough to run on request or poll from the dashboard):

- **Model switch**: latest call used a different model than the device's recent dominant model.
- **Token spike**: latest call's `total_tokens` is more than 2.5 standard deviations above the device's recent mean.

Both are explainable rule-based checks, not ML they flag "something's different," not "something's malicious."

### Live dashboard

`GET /dashboard` a single-page HTML dashboard (no build step) showing summary cards, per-device stats with cost, current anomalies, and a live event feed over WebSocket. Pass `?api_key=...` in the URL if `COLLECTOR_API_KEY` is set.

### Events view

`GET /events/view` a filterable table of raw events (device filter, adjustable limit, optional auto-refresh). Click any row to load that call's full request/response pair via `/traces/{correlation_id}` in a detail panel below the table. Linked from the dashboard ("View events").

### Demo sender

`GET /demo` a small page for sending a test call through any configured device's proxy without needing `curl` in another terminal. Pick a device from the dropdown (populated from `GET /demo/devices`), type a message, hit Send. It's linked from the top of the dashboard ("Send a test request").

Under the hood, `POST /demo/send` runs server-side in the collector: it looks up the device's proxy URL from `DEVICE_PROXIES_JSON` (defaults to the `device-1`/`device-2`/`device-3` Compose service names) and relays the call there directly over the internal Docker network so there's no CORS setup needed, and the call is tracked exactly like any other request through that proxy (it'll show up in `/events`, `/stats`, the dashboard, etc. immediately after).

```bash
curl -X POST http://localhost:9000/demo/send \
  -H "Content-Type: application/json" \
  -d '{"device_id":"device-1","message":"Hello!","model":"mock-model"}'
```

If you rename or add devices, override the mapping:
```yaml
DEVICE_PROXIES_JSON: '{"device-1":"http://device-1:8000","device-4":"http://device-4:8000"}'
```

### WebSocket live-tail

`WS /ws/events` broadcasts every stored event to connected clients in real time (bridges from the MQTT callback thread into the async event loop via `call_soon_threadsafe`). Same auth as REST, via `?api_key=`.

### Postgres backend (collector)

SQLite remains the default. Switch with:

| Variable | Default | Purpose |
|---|---|---|
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgres` |
| `PG_HOST` / `PG_PORT` / `PG_DB` / `PG_USER` / `PG_PASSWORD` | see `docker-compose.yml` | connection details |

Bring up the optional Postgres service with:
```bash
docker compose --profile postgres up --build
```
`store_event()`/`query()` in `collector/db.py` are the only places with backend-specific SQL (mostly placeholder style and one boolean-column quirk Postgres stores `rate_limited` as a real `BOOLEAN`, SQLite as `INTEGER`, handled via `db.rate_limited_true()`).

### MQTT auth and TLS

Both off by default (anonymous, plaintext — same as the original setup, intended for a closed test network only). To enable:

- **Auth**: copy `mosquitto/config/mosquitto.auth.conf.example` over `mosquitto.conf`, generate a password file with `./mosquitto/gen-passwd.sh <user> <pass>`, then set `MQTT_USERNAME` /  `MQTT_PASSWORD` on every proxy and the collector.
- **TLS**: copy `mosquitto/config/mosquitto.tls.conf.example` over `mosquitto.conf`, generate a self-signed CA + broker cert with `./mosquitto/gen-certs.sh <hostname>` (use a real CA in production), then set `MQTT_USE_TLS=true` and `MQTT_CA_CERT=<path to ca.crt>` on every proxy and the collector. The cert's CN must match the hostname clients connect with (`mosquitto`, the Compose service name, by default) or TLS verification will fail.
- Both can be combined by merging the two example configs.

### A pre-existing bug fixed along the way

The proxy's catch-all route (`/{path:path}`) was originally registered *before* `/_proxy/health`. Starlette matches routes in registration order, not by specificity, so health checks were silently being forwarded upstream instead of answered locally (confirmed: hitting `/_proxy/health` returned the upstream's 404, not a health status). Fixed by moving the health route above the catch-all.

- **More devices:** add `device-N` blocks to `docker-compose.yml`, or deploy the `proxy` image standalone on real edge machines, pointed at the master's MQTT broker address (`MQTT_BROKER_HOST` /  `MQTT_BROKER_PORT`).

- **Scale storage:** SQLite is fine for testing; swap it for Postgres/ClickHouse once event volume grows. `store_event()` in `collector/app.py` is the only place that touches the database, so this is a contained change.

- **Add MQTT auth/TLS:** required before running outside a closed test network, see below.

## Security notes

The setup as shipped **defaults to** a closed-test-network posture (sameas before), but auth/TLS/redaction are now real, implemented options not just warnings. Before using this anywhere beyond a closed test
network:

1. **MQTT is anonymous and unencrypted by default.** Auth and TLS are now implemented (see [New features](#new-features-july-2026-update)) but off by default turn them on via `mosquitto.auth.conf.example` /   `mosquitto.tls.conf.example` plus the matching `MQTT_USERNAME`/ `MQTT_PASSWORD`/`MQTT_USE_TLS` env vars before any real deployment.
2. **Body redaction is real but not exhaustive.** `shared/redact.py` now does regex-based scrubbing of common secret/PII shapes (API keys, emails, phone numbers, credit cards, SSNs, IPs) instead of just reporting byte counts  but it's still not a full PII-detection model. Don't rely on `REDACT_BODY=false` outside a trusted test environment.
3. **Collector REST/WebSocket auth is now implemented but off by default.** Set `COLLECTOR_API_KEY` before exposing the collector beyond localhost, otherwise `/events`, `/stats`, `/traces`, and the dashboard are open to anyone who can reach the port.
4. **The proxy forwards `Authorization` headers through unchanged** this is by design (so apps need no code change), but it means the proxy itself becomes a sensitive component: anyone who can reach it can use your upstream API key via it. Rate limiting (`RATE_LIMIT_ENABLED`) reduces blast radius but doesn't replace proper network isolation.
5. **Replay capture stores full request bodies (encrypted).** Only enable `REPLAY_CAPTURE` if you're prepared to manage the `REPLAY_ENCRYPTION_KEY` as a real secret anyone with it can decrypt everything captured.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl` to a device port hangs / connection refused | Container not up yet, or wrong port | `docker compose ps`; confirm the `810X` mapping in `docker-compose.yml` |
| Events never show up in `/events` | Proxy can't reach Mosquitto | Check proxy logs for `Failed to publish event`; confirm `MQTT_BROKER_HOST` resolves inside the Compose network |
| `/stats` shows 0 responses but `/events` has data | You're only sending requests, not getting responses (upstream unreachable) | Check `error` field on the `response` events for that `correlation_id` |
| Collector restarts / loses data | `DB_PATH` not on a persisted volume | Confirm `collector-data` volume is mounted (`docker-compose.yml`) |
| Real provider calls fail through the proxy | Headers or query params dropped, or timeout too short | Proxy uses a 120s httpx timeout; check `TARGET_BASE_URL` has no trailing path issues |
| Two events with same `correlation_id` but mismatched `device_id` | Shouldn't happen — `correlation_id` is generated per-request inside a single proxy instance | Check you're not sharing a `DEVICE_ID` across multiple containers |
| `/stats` returns 500 with `no such column: ...` | An existing `collector-data` volume from before that column existed | Fixed automatically as of this version — `init_db()` migrates missing columns on startup. If you're still on an older image, either rebuild (see next row) or run `docker compose down -v` to drop the volume and start fresh (loses stored events) |
| A new endpoint (e.g. `/demo`) 404s even though it's in the code | Docker reused an old container/image instead of rebuilding | `docker compose down` then `docker compose up --build --force-recreate`; confirm you're running from the latest extracted zip, not an older copy |

### Extending the protocol

To add a new field to GATP:
1. Add it to `GATPEvent` in `proxy/protocol.py` (keep `collector/protocol.py` in sync too, it's a duplicate reference copy, not actually imported by the collector, which stores whatever JSON arrives).
2. Add it to the `COLUMNS` list in `collector/db.py`, and update `store_event()` if the value doesn't come straight from the payload (e.g. `estimated_cost_usd` is computed separately, not read off the event).
3. That's it for storage `init_db()` automatically adds any column in `COLUMNS` that an existing table doesn't have yet (see `_migrate_missing_columns()` in `collector/db.py`), so upgrading with an existing `collector-data` volume just works; you don't need to wipe it or write a manual `ALTER TABLE`. Existing rows get `NULL` for the new column, same as before.
