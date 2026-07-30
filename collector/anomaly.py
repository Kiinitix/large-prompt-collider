"""
Lightweight anomaly detection over stored events.

Not a real ML model -- two simple, explainable heuristics that catch the
most common "something's off" signals for a device:

  1. Model switch: the device's most recent call used a different model
     than what it had been using in its prior N calls. Could be benign
     (someone changed the app's config on purpose) or could indicate a
     leaked API key being used from an unexpected client.
  2. Token spike: the most recent call's total_tokens is more than
     `z_threshold` standard deviations above that device's recent mean.
     Flags unusually large prompts/completions.

Both run on-demand via GET /anomalies rather than continuously, since the
dataset is typically small enough that this is cheap; call it from a cron
job or dashboard poll if you want continuous monitoring.
"""
import statistics
from typing import Optional

import db

MODEL_SWITCH_LOOKBACK = 10
TOKEN_SPIKE_LOOKBACK = 20
TOKEN_SPIKE_Z_THRESHOLD = 2.5
TOKEN_SPIKE_MIN_SAMPLES = 5


def _device_ids() -> list[str]:
    rows = db.query("SELECT DISTINCT device_id FROM events WHERE direction = 'response'")
    return [r["device_id"] for r in rows if r["device_id"]]


def detect_model_switch(device_id: str) -> Optional[dict]:
    rows = db.query(
        """
        SELECT model, timestamp FROM events
        WHERE device_id = ? AND direction = 'request' AND model IS NOT NULL
        ORDER BY received_at DESC LIMIT ?
        """,
        (device_id, MODEL_SWITCH_LOOKBACK + 1),
    )
    if len(rows) < 3:
        return None
    latest_model = rows[0]["model"]
    prior_models = [r["model"] for r in rows[1:]]
    if not prior_models:
        return None
    dominant = statistics.mode(prior_models)
    if latest_model != dominant and prior_models.count(dominant) >= len(prior_models) * 0.7:
        return {
            "device_id": device_id,
            "kind": "model_switch",
            "detail": f"latest call used '{latest_model}', prior calls predominantly used '{dominant}'",
        }
    return None


def detect_token_spike(device_id: str) -> Optional[dict]:
    rows = db.query(
        """
        SELECT total_tokens FROM events
        WHERE device_id = ? AND direction = 'response' AND total_tokens IS NOT NULL
        ORDER BY received_at DESC LIMIT ?
        """,
        (device_id, TOKEN_SPIKE_LOOKBACK + 1),
    )
    values = [r["total_tokens"] for r in rows if r["total_tokens"] is not None]
    if len(values) < TOKEN_SPIKE_MIN_SAMPLES + 1:
        return None
    latest, history = values[0], values[1:]
    mean = statistics.mean(history)
    stdev = statistics.pstdev(history)
    if stdev == 0:
        return None
    z = (latest - mean) / stdev
    if z >= TOKEN_SPIKE_Z_THRESHOLD:
        return {
            "device_id": device_id,
            "kind": "token_spike",
            "detail": f"latest call used {latest:.0f} tokens vs recent mean {mean:.0f} (z={z:.1f})",
        }
    return None


def run_detection() -> list[dict]:
    findings = []
    for device_id in _device_ids():
        for detector in (detect_model_switch, detect_token_spike):
            result = detector(device_id)
            if result:
                findings.append(result)
                db.record_anomaly(device_id, result["kind"], result["detail"])
    return findings
