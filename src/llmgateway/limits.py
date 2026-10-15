"""Per-tenant token-bucket rate limiting (CONCEPTS.md section 2). Time is passed in
explicitly so the logic is testable without sleeping."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate: float            # tokens per second
    capacity: float        # burst size
    tokens: float = field(init=False)
    last: float | None = field(default=None, init=False)

    def __post_init__(self):
        self.tokens = float(self.capacity)

    def _refill(self, now: float) -> None:
        if self.last is not None and now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now if self.last is None else max(self.last, now)

    def take(self, now: float, n: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def retry_after(self, now: float, n: float = 1.0) -> float:
        self._refill(now)
        deficit = n - self.tokens
        return 0.0 if deficit <= 0 else deficit / self.rate


class TenantLimiter:
    def __init__(self, default_rate: float, default_capacity: float, overrides: dict[str, tuple[float, float]] | None = None):
        self.default = (default_rate, default_capacity)
        self.overrides = overrides or {}
        self.buckets: dict[str, TokenBucket] = {}
        self.stats: dict[str, dict[str, int]] = {}

    def _bucket(self, tenant: str) -> TokenBucket:
        if tenant not in self.buckets:
            rate, cap = self.overrides.get(tenant, self.default)
            self.buckets[tenant] = TokenBucket(rate, cap)
            self.stats[tenant] = {"allowed": 0, "rejected": 0}
        return self.buckets[tenant]

    def allow(self, tenant: str, now: float) -> bool:
        ok = self._bucket(tenant).take(now)
        self.stats[tenant]["allowed" if ok else "rejected"] += 1
        return ok

    def retry_after(self, tenant: str, now: float) -> float:
        return self._bucket(tenant).retry_after(now)
