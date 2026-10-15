from llmgateway.limits import TenantLimiter, TokenBucket


def test_bucket_allows_burst_then_refills_at_rate():
    b = TokenBucket(rate=2.0, capacity=4)
    now = 100.0
    assert [b.take(now) for _ in range(4)] == [True] * 4
    assert b.take(now) is False                 # empty
    assert b.take(now + 0.5) is True            # 0.5 s x 2/s = 1 token refilled
    assert b.take(now + 0.5) is False
    assert b.take(now + 10) is True and b.tokens <= 3.0   # capped at capacity


def test_bucket_retry_after_says_when_the_next_token_arrives():
    b = TokenBucket(rate=2.0, capacity=1)
    b.take(0.0)
    assert b.retry_after(0.0) == 0.5


def test_tenant_limiter_isolates_tenants_and_counts_rejections():
    lim = TenantLimiter(default_rate=1.0, default_capacity=2, overrides={"gold": (10.0, 20)})
    t = 0.0
    assert lim.allow("free", t) and lim.allow("free", t) and not lim.allow("free", t)
    assert all(lim.allow("gold", t) for _ in range(20))
    assert lim.stats["free"] == {"allowed": 2, "rejected": 1} and lim.stats["gold"]["rejected"] == 0
