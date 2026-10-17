import asyncio
import json
import time

import httpx

from llmgateway.app import GatewayConfig, ProviderConfig, create_app


def _cfg(providers, chain, **kw):
    pcfg = {name: ProviderConfig(name=name, base_url=url, model="m") for name, url in providers.items()}
    return GatewayConfig(providers=pcfg, chain=chain, **kw)


def _body(stream=False, max_tokens=3):
    return {"model": "default", "messages": [{"role": "user", "content": "hi"}], "max_tokens": max_tokens, "stream": stream, "ignore_eos": True}


def test_healthy_chain_serves_and_attributes_provider(providers, live_gateway):
    with live_gateway(create_app(_cfg(providers, ["healthy", "slow"]))) as url:
        r = httpx.post(f"{url}/v1/chat/completions", json=_body(), headers={"x-tenant": "t1"}, timeout=30)
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 3
        assert r.headers["x-llmgateway-provider"] == "healthy" and r.headers["x-llmgateway-attempts"] == "1"


def test_streaming_passthrough(providers, live_gateway):
    with live_gateway(create_app(_cfg(providers, ["healthy"]))) as url:
        with httpx.stream("POST", f"{url}/v1/chat/completions", json=_body(stream=True), timeout=30) as r:
            assert r.status_code == 200
            data = [json.loads(ln[5:]) for ln in r.iter_lines() if ln.startswith("data:") and "[DONE]" not in ln]
        assert sum(1 for c in data if c["choices"] and c["choices"][0]["delta"].get("content")) == 3


def test_rate_limit_returns_429_with_retry_after_for_that_tenant_only(providers, live_gateway):
    cfg = _cfg(providers, ["healthy"], tenant_rate=1.0, tenant_burst=2)
    with live_gateway(create_app(cfg)) as url:
        codes = [httpx.post(f"{url}/v1/chat/completions", json=_body(), headers={"x-tenant": "a"}, timeout=30).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        r = httpx.post(f"{url}/v1/chat/completions", json=_body(), headers={"x-tenant": "a"}, timeout=30)
        assert r.status_code == 429 and float(r.headers["retry-after"]) > 0
        assert httpx.post(f"{url}/v1/chat/completions", json=_body(), headers={"x-tenant": "b"}, timeout=30).status_code == 200


def test_flaky_provider_is_retried_then_falls_back(providers, live_gateway):
    cfg = _cfg(providers, ["flaky", "healthy"], max_attempts_per_provider=2)
    with live_gateway(create_app(cfg)) as url:
        results = [httpx.post(f"{url}/v1/chat/completions", json=_body(), timeout=30) for _ in range(30)]
        assert all(r.status_code == 200 for r in results)              # chain absorbs every failure
        provs = [r.headers["x-llmgateway-provider"] for r in results]
        assert "healthy" in provs and "flaky" in provs
        m = httpx.get(f"{url}/metrics").text
        assert 'llmgateway_retries_total{provider="flaky"}' in m and 'llmgateway_fallbacks_total{from="flaky"' in m


def test_slow_provider_is_degraded_by_ttft_slo_and_traffic_moves(providers, live_gateway):
    cfg = _cfg(providers, ["slow", "healthy"], slo_ttft_ms=200, breaker_window_s=30, breaker_min_samples=5, breaker_cooldown_s=60)
    with live_gateway(create_app(cfg)) as url:
        provs = [httpx.post(f"{url}/v1/chat/completions", json=_body(), timeout=30).headers["x-llmgateway-provider"] for _ in range(12)]
        assert provs[:5] == ["slow"] * 5 and provs[-1] == "healthy"   # breaker opened after min_samples
        m = httpx.get(f"{url}/metrics").text
        assert 'llmgateway_breaker_state{provider="slow"} 1' in m      # 0 closed, 1 open, 2 half-open


def test_first_token_timeout_triggers_fallback(providers, live_gateway):
    cfg = _cfg(providers, ["slow", "healthy"], first_token_timeout_ms=100)
    with live_gateway(create_app(cfg)) as url:
        t0 = time.monotonic()
        r = httpx.post(f"{url}/v1/chat/completions", json=_body(), timeout=30)
        assert r.status_code == 200 and r.headers["x-llmgateway-provider"] == "healthy"
        assert time.monotonic() - t0 < 1.0


def test_chain_exhausted_is_a_fast_clean_error(providers, live_gateway):
    cfg = _cfg(providers, ["flaky"], max_attempts_per_provider=1)
    with live_gateway(create_app(cfg)) as url:
        codes = [httpx.post(f"{url}/v1/chat/completions", json=_body(), timeout=30).status_code for _ in range(20)]
        assert 502 in codes and 200 in codes
        m = httpx.get(f"{url}/metrics").text
        assert "llmgateway_chain_exhausted_total" in m


def test_concurrent_streams_do_not_cross(providers, live_gateway):
    with live_gateway(create_app(_cfg(providers, ["healthy"]))) as url:
        async def go():
            async with httpx.AsyncClient(base_url=url, timeout=30) as c:
                rs = await asyncio.gather(*[c.post("/v1/chat/completions", json=_body(max_tokens=n)) for n in (2, 5, 9)])
                return [r.json()["usage"]["completion_tokens"] for r in rs]
        assert asyncio.run(go()) == [2, 5, 9]


def test_reported_ttft_is_end_to_end_including_failed_attempts(providers, live_gateway):
    # A throttled first provider times out at 100 ms and the healthy one answers in ~5 ms; the user
    # waited ~105 ms, and that is what the header must say (the breaker still sees per-attempt TTFT).
    cfg = _cfg(providers, ["slow", "healthy"], first_token_timeout_ms=100)
    with live_gateway(create_app(cfg)) as url:
        r = httpx.post(f"{url}/v1/chat/completions", json=_body(), timeout=30)
        assert r.headers["x-llmgateway-provider"] == "healthy"
        assert float(r.headers["x-llmgateway-ttft-ms"]) >= 100
