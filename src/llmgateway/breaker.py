"""Rolling TTFT / error window and a circuit breaker per provider (CONCEPTS.md sections 1, 4)."""

from __future__ import annotations

from collections import deque


class RollingWindow:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, t: float, value: float) -> None:
        self._samples.append((t, value))

    def _prune(self, t: float) -> None:
        while self._samples and self._samples[0][0] < t - self.seconds:
            self._samples.popleft()

    def n(self, t: float) -> int:
        self._prune(t)
        return len(self._samples)

    def values(self, t: float) -> list[float]:
        self._prune(t)
        return [v for _, v in self._samples]

    def p95(self, t: float) -> float | None:
        vals = sorted(self.values(t))
        if not vals:
            return None
        k = (len(vals) - 1) * 0.95
        lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


class CircuitBreaker:
    """closed -> open when p95 TTFT > SLO or error rate > max (given min_samples);
    open -> half_open after cooldown; half_open -> closed after probe_successes good probes,
    or -> open on a failed probe."""

    def __init__(self, slo_ttft_s: float, window_s: float = 30, min_samples: int = 20, max_error_rate: float = 0.5,
                 cooldown_s: float = 15, probe_successes: int = 3):
        self.slo, self.min_samples, self.max_error_rate = slo_ttft_s, min_samples, max_error_rate
        self.cooldown_s, self.probe_successes = cooldown_s, probe_successes
        self.ttft = RollingWindow(window_s)
        self.outcomes = RollingWindow(window_s)   # 1.0 = error, 0.0 = success
        self._state = "closed"
        self.opened_at: float | None = None
        self._probe_in_flight = False
        self._probe_ok = 0
        self.transitions: list[tuple[float, str]] = []

    def _set(self, t: float, state: str) -> None:
        if state != self._state:
            self._state = state
            self.transitions.append((t, state))
            if state == "open":
                self.opened_at = t
                self._probe_ok = 0
                self._probe_in_flight = False

    def _evaluate(self, t: float) -> None:
        if self._state != "closed":
            return
        n = self.outcomes.n(t)
        if n < self.min_samples:
            return
        p95 = self.ttft.p95(t)
        err = sum(self.outcomes.values(t)) / n
        if (p95 is not None and p95 > self.slo) or err > self.max_error_rate:
            self._set(t, "open")

    def record_ttft(self, t: float, ttft_s: float) -> None:
        self.ttft.add(t, ttft_s)
        self.outcomes.add(t, 0.0)
        if self._state == "half_open":
            self._probe_in_flight = False
            if ttft_s <= self.slo:
                self._probe_ok += 1
                if self._probe_ok >= self.probe_successes:
                    self._reset_windows()
                    self._set(t, "closed")
            else:
                self._set(t, "open")
        else:
            self._evaluate(t)

    def record_error(self, t: float) -> None:
        self.outcomes.add(t, 1.0)
        if self._state == "half_open":
            self._set(t, "open")
        else:
            self._evaluate(t)

    def _reset_windows(self) -> None:
        self.ttft = RollingWindow(self.ttft.seconds)
        self.outcomes = RollingWindow(self.outcomes.seconds)

    def state(self, t: float) -> str:
        if self._state == "open" and self.opened_at is not None and t - self.opened_at >= self.cooldown_s:
            self._set(t, "half_open")
        return self._state

    def allow_probe(self, t: float) -> bool:
        if self.state(t) != "half_open" or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True
