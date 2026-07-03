from __future__ import annotations

import threading
import time


class TokenBucketRateLimiter:
    """Rate limiter thread-safe simple, à taux constant.

    Volontairement minimaliste : Florentin a déjà une brique adaptive
    rate limiter / circuit breaker pour le scraper lindustrie-recrute ;
    ce module n'a pas vocation à la dupliquer, juste à donner un
    composant autonome et testable pour ce package.
    """

    def __init__(self, requests_per_second: float, burst: int | None = None):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second doit être > 0")
        self._rate = requests_per_second
        self._capacity = burst if burst is not None else max(1, int(requests_per_second))
        self._tokens = float(self._capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait_time = (1 - self._tokens) / self._rate
            time.sleep(max(wait_time, 0.01))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


class DailyQuota:
    """Compteur de quota journalier simple, thread-safe."""

    def __init__(self, limit: int):
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._limit - self._used

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def consume(self, n: int = 1) -> None:
        with self._lock:
            self._used += n
