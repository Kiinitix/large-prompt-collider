"""
Threshold-based alerting.

Runs on a background timer inside the collector process. Every
ALERT_CHECK_INTERVAL_SEC seconds, it looks at each device's recent error
rate and average latency (over the last ALERT_WINDOW_MINUTES) and fires a
webhook POST if either crosses the configured threshold. Simple by design
-- no alert de-duplication/state machine beyond a basic cooldown, so if you
need paging-grade alerting, feed ALERT_WEBHOOK_URL into something like
Slack's incoming webhook or a generic alerting gateway that handles
dedup/escalation itself.
"""
import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import httpx

import db

logger = logging.getLogger("genai-collector.alerts")

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")  # e.g. a Slack incoming webhook
ALERT_ERROR_RATE_THRESHOLD = float(os.getenv("ALERT_ERROR_RATE_THRESHOLD", "0.25"))  # 25%
ALERT_LATENCY_MS_THRESHOLD = float(os.getenv("ALERT_LATENCY_MS_THRESHOLD", "5000"))
ALERT_WINDOW_MINUTES = int(os.getenv("ALERT_WINDOW_MINUTES", "10"))
ALERT_CHECK_INTERVAL_SEC = int(os.getenv("ALERT_CHECK_INTERVAL_SEC", "60"))
ALERT_MIN_SAMPLES = int(os.getenv("ALERT_MIN_SAMPLES", "5"))  # don't alert on tiny sample sizes
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "900"))  # don't re-fire the same alert for 15 min

_last_fired: dict[str, float] = {}


def _send_webhook(text: str) -> None:
    if not ALERT_WEBHOOK_URL:
        logger.warning("Alert triggered but ALERT_WEBHOOK_URL is unset -- logging only: %s", text)
        return
    try:
        httpx.post(ALERT_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        logger.error("Failed to send alert webhook: %s", e)


def _cooldown_ok(key: str) -> bool:
    last = _last_fired.get(key, 0)
    if time.time() - last >= ALERT_COOLDOWN_SEC:
        _last_fired[key] = time.time()
        return True
    return False


def check_once() -> None:
    since = (datetime.now(timezone.utc) - timedelta(minutes=ALERT_WINDOW_MINUTES)).isoformat()
    rows = db.query(
        """
        SELECT device_id,
               COUNT(*) AS n,
               AVG(latency_ms) AS avg_latency_ms,
               SUM(CASE WHEN status_code >= 400 OR status_code IS NULL THEN 1 ELSE 0 END) AS errors
        FROM events
        WHERE direction = 'response' AND received_at >= ?
        GROUP BY device_id
        """,
        (since,),
    )
    for row in rows:
        n = row["n"] or 0
        if n < ALERT_MIN_SAMPLES:
            continue
        error_rate = (row["errors"] or 0) / n
        device = row["device_id"]

        if error_rate >= ALERT_ERROR_RATE_THRESHOLD and _cooldown_ok(f"{device}:error_rate"):
            _send_webhook(
                f"[genai-tracker] {device}: error rate {error_rate:.0%} over last "
                f"{ALERT_WINDOW_MINUTES}m ({row['errors']}/{n} calls failed)"
            )
        avg_latency = row["avg_latency_ms"] or 0
        if avg_latency >= ALERT_LATENCY_MS_THRESHOLD and _cooldown_ok(f"{device}:latency"):
            _send_webhook(
                f"[genai-tracker] {device}: avg latency {avg_latency:.0f}ms over last "
                f"{ALERT_WINDOW_MINUTES}m (threshold {ALERT_LATENCY_MS_THRESHOLD:.0f}ms)"
            )


def _loop():
    while True:
        try:
            check_once()
        except Exception as e:
            logger.error("Alert check failed: %s", e)
        time.sleep(ALERT_CHECK_INTERVAL_SEC)


def start_background_thread() -> None:
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info(
        "Alerting started: window=%dm interval=%ds error_threshold=%.0f%% latency_threshold=%.0fms webhook=%s",
        ALERT_WINDOW_MINUTES, ALERT_CHECK_INTERVAL_SEC, ALERT_ERROR_RATE_THRESHOLD * 100,
        ALERT_LATENCY_MS_THRESHOLD, "set" if ALERT_WEBHOOK_URL else "unset (log-only)",
    )
