"""
Simple in-memory token-bucket rate limiter.

Scoped per-proxy-process (i.e. per device), since each device already gets
its own proxy container. Not distributed -- if you run multiple proxy
replicas for the same device_id behind a load balancer, each replica has
its own independent bucket. Good enough to stop a single misbehaving
device from blowing through your upstream API budget; not a substitute for
provider-side quota enforcement.
"""
import threading
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate_per_sec = rate_per_sec
        self.capacity = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
        self._last_refill = now

    def try_consume(self, n: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def retry_after_seconds(self) -> float:
        """Rough estimate of how long until at least one token is available."""
        with self._lock:
            self._refill()
            if self._tokens >= 1:
                return 0.0
            return round((1 - self._tokens) / self.rate_per_sec, 2)
