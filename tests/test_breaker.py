import pytest

from llmgateway.breaker import CircuitBreaker, RollingWindow


def test_rolling_window_keeps_only_recent_samples_and_reports_p95():
    w = RollingWindow(seconds=10)
    for i in range(100):
        w.add(t=float(i) / 10, value=float(i))          # 0..99 over 10 s
    assert w.n(t=9.9) == 100 and w.p95(t=9.9) == pytest.approx(94.05, abs=0.2)
    assert w.n(t=15.0) == 50                            # samples older than 10 s dropped
    assert w.p95(t=100.0) is None


def test_breaker_opens_when_p95_ttft_exceeds_slo_with_enough_samples():
    cb = CircuitBreaker(slo_ttft_s=0.5, window_s=10, min_samples=20, cooldown_s=5)
    t = 0.0
    for i in range(19):
        cb.record_ttft(t + i * 0.1, 0.9)                 # bad, but below min_samples
    assert cb.state(t + 2) == "closed"
    cb.record_ttft(t + 2, 0.9)
    assert cb.state(t + 2.1) == "open"


def test_breaker_opens_on_error_rate():
    cb = CircuitBreaker(slo_ttft_s=1.0, window_s=10, min_samples=10, max_error_rate=0.3, cooldown_s=5)
    for i in range(10):
        cb.record_error(float(i) * 0.1) if i < 4 else cb.record_ttft(float(i) * 0.1, 0.1)
    assert cb.state(1.0) == "open"                       # 4/10 = 40% errors


def test_breaker_half_opens_after_cooldown_and_closes_on_good_probe():
    cb = CircuitBreaker(slo_ttft_s=0.5, window_s=10, min_samples=5, cooldown_s=5, probe_successes=2)
    for i in range(5):
        cb.record_ttft(float(i), 2.0)
    assert cb.state(5.0) == "open"
    assert cb.state(10.1) == "half_open"
    assert cb.allow_probe(10.1) is True and cb.allow_probe(10.1) is False   # one probe in flight at a time
    cb.record_ttft(10.2, 0.1); cb.allow_probe(10.3); cb.record_ttft(10.4, 0.1)
    assert cb.state(10.5) == "closed"


def test_breaker_reopens_if_probe_fails():
    cb = CircuitBreaker(slo_ttft_s=0.5, window_s=10, min_samples=5, cooldown_s=5)
    for i in range(5):
        cb.record_ttft(float(i), 2.0)
    assert cb.state(10.1) == "half_open"
    cb.allow_probe(10.1); cb.record_error(10.2)
    assert cb.state(10.3) == "open"
    assert cb.transitions[-1][1] == "open"
