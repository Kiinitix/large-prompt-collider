# GenAI Activity Tracker

A middleware system for tracking every GenAI request/response flowing through a private setup, built around a small custom protocol (**GATP - GenAI Activity Tracking Protocol**) carried over MQTT.

## Architecture
<img width="657" height="867" alt="image" src="https://github.com/user-attachments/assets/a47e7a39-ce36-4497-a273-4648426fed96" />


**1. Proxy** (one per device): a drop-in reverse proxy. Point your app's `base_url` at it instead of the real provider, no code changes needed. It forwards the call unchanged (streaming responses included) and publishes two GATP events (request + response) over MQTT. Can optionally rate-limit a device, redact sensitive data in stored previews, and capture encrypted full bodies for later replay.

**2. Mosquitto**: lightweight MQTT broker, runs on the master machine. Chosen because it scales cleanly to many lightweight/edge devices. Anonymous/plaintext by default for local testing; username/password auth and TLS are available as opt-in config.

**3. Collector**: subscribes to `genai/track/#`, persists every event (SQLite by default, Postgres optional), and exposes a REST + WebSocket API (`/events`, `/traces/{id}`, `/stats`, `/anomalies`) to query and watch activity across the whole fleet. Also serves a few built-in web pages (see below) and can fire alert webhooks on error-rate or latency spikes.

**4. mock-genai**: a stand-in OpenAI-style endpoint (including streaming) so you can run the entire pipeline in Docker without any real API keys.

## Web pages

Once the stack is running, the collector serves:

- `http://localhost:9000/dashboard` - live summary stats, per-device cost/latency/error breakdown, current anomalies, and a live event feed
- `http://localhost:9000/events/view` - a filterable table of raw events; click a row to see the full request/response pair
- `http://localhost:9000/demo` - send a test call through any device's proxy from the browser, no `curl` needed

## The protocol (GATP v1.1)

Every request produces a `request` event immediately, and a matching `response` event once the upstream reply lands, tied together by `correlation_id`. See `proxy/protocol.py` for the full schema.

Key fields:

| field | meaning |
|---|---|
| `device_id` | which device/proxy emitted the event |
| `correlation_id` | links a request event to its response event |
| `direction` | `"request"` or `"response"` |
| `provider` / `model` | which GenAI backend and model was called |
| `latency_ms` | round-trip time (response event only) |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | usage, if the provider returns it |
| `body_preview` | redacted/truncated snippet; bodies are redacted by default (`REDACT_BODY=true`) |
| `error` | set if the upstream call failed |
| `rate_limited` | true if the proxy rejected this call under its rate limit |
| `full_body_encrypted` | encrypted full request body, only present if `REPLAY_CAPTURE=true` |

`body_preview` redaction is a real (regex-based) scrubber for common secret/PII shapes - API keys, emails, phone numbers, credit cards, SSNs - not just a byte-count placeholder. It's still not a full PII-detection model, so pair it with a proper DLP tool if you're handling regulated data. `estimated_cost_usd` is computed per response event from a small pricing table (`shared/pricing.py`), overridable via `PRICING_OVERRIDES_JSON`.

## Running the test environment

```bash
docker compose up --build
```

This starts: the Mosquitto broker, the collector (on the "master"), a mock GenAI provider, and **3 simulated devices** (`device-1`, `device-2`, `device-3`), each with its own tracking proxy on ports 8101-8103.

Trigger some traffic from a "device" - either via curl:

```bash
curl http://localhost:8101/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-model","messages":[{"role":"user","content":"Hello there"}]}'
```

or from a browser at `http://localhost:9000/demo`.

Check what the master machine captured:

```bash
curl http://localhost:9000/events | jq
curl http://localhost:9000/stats | jq
```

or open the dashboard at `http://localhost:9000/dashboard`.

## Cleaning up / starting fresh

```bash
docker compose down -v
```

The `-v` flag drops the stored-events volume along with the containers. Without `-v`, a normal `docker compose down` / `up` leaves your event history intact.

## Pointing a device at a real provider

Edit `docker-compose.yml` (or run a proxy container standalone) and set:

```yaml
environment:
  TARGET_BASE_URL: https://api.openai.com   # or your provider's base URL
  PROVIDER_NAME: openai
```

Your application then calls the proxy exactly as it would call the real API (e.g. `http://device-1:8000/v1/chat/completions`), including its normal `Authorization` header - the proxy forwards headers through unchanged, it only reads the body to extract `model` and token usage.

## Output screenshot
### Test Request
<img width="981" height="862" alt="test_req" src="https://github.com/user-attachments/assets/f8b8e5c1-ac57-4da6-b550-fd62bfd3022c" />

### Events
<img width="1917" height="1007" alt="events-1" src="https://github.com/user-attachments/assets/b7f09cd7-297d-450c-ada1-101bf3d0ae0f" />
<img width="1912" height="1017" alt="events-2" src="https://github.com/user-attachments/assets/e481a054-89fd-4ad9-ad88-05e81aec977d" />

### Dashboard
<img width="1917" height="931" alt="dashboard" src="https://github.com/user-attachments/assets/cc570481-0cbf-438a-b298-559420941c3e" />





## Optional features

All off by default, matching the original closed-test-network setup. Turn on via env vars in `docker-compose.yml`:

**1. Rate limiting** (`RATE_LIMIT_ENABLED`) - per-device token bucket, rejects with 429 past the configured rate

**2. Replay capture** (`REPLAY_CAPTURE` + `REPLAY_ENCRYPTION_KEY`) - stores full request bodies, Fernet-encrypted, for later manual replay/debugging

**3. MQTT auth/TLS** - see `mosquitto/config/mosquitto.auth.conf.example` and `mosquitto.tls.conf.example`, plus `mosquitto/gen-passwd.sh` and `mosquitto/gen-certs.sh`

**4.Collector API key** (`COLLECTOR_API_KEY`) - required on all REST/WebSocket endpoints except `/health` once set

**5.Alerting** (`ALERTING_ENABLED` + `ALERT_WEBHOOK_URL`) - posts to a webhook (e.g. Slack) when a device's error rate or latency crosses a threshold

**6. Postgres backend** (`docker compose --profile postgres up`, then `DB_BACKEND=postgres` on the collector) - schema auto-migrates on startup either way, including for existing SQLite/Postgres data from before a given column existed

Full config reference, including every env var above, is in `docs/DOCUMENTATION.md`.

## Scaling beyond the test setup

1. Add more `device-N` services in `docker-compose.yml`, or deploy the `proxy` image directly on real edge machines pointed at the master's MQTT broker address.
2. Switch to the Postgres backend once event volume grows past what SQLite handles comfortably (see above).
3. Turn on MQTT auth/TLS (see above) before using this outside a closed test network - the current config allows anonymous, unencrypted connections by default.

## ## If you made it this far...

First of all... respect. If you survived all the architecture diagrams, MQTT, GATP, reverse proxies, event schemas, correlation IDs, and the alphabet soup of configuration options, you've officially earned my respect. At this point I'd probably hand you the keys to the project and ask you to maintain it while I grab a coffee.

So here's the project in plain English.

Imagine you have a bunch of applications talking to ChatGPT or any other GenAI service. Instead of letting them call the AI directly, you place a tiny middleman in between. That middleman quietly watches every request and response, notes down useful information like which model was used, how long it took, how many tokens it consumed, whether it failed, and sends those details to one central place.

That's it. Your applications don't know it's there. The AI provider doesn't know it's there. Nobody has to change their code. LPC just sits in the middle, takes notes, and gives you dashboards, traces, analytics, and a much better idea of what's happening across your GenAI ecosystem.

Sometimes the simplest explanation is the best one.


## Why "Large Prompt Collider"?

Inspired by CERN's Large Hadron Collider (LHC), where physicists collide particles to observe fundamental interactions that are otherwise invisible.

Similarly, Large Prompt Collider "collides" prompts with AI models and captures everything that happens during the interaction, making the invisible observable.

Just as the LHC doesn't change the laws of physics but reveals them through instrumentation, LPC doesn't modify AI applications or models. Instead, it acts as an observability layer that records every request, response, latency, token usage, cost, and anomaly flowing through the system.
