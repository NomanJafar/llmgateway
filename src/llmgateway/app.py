"""The gateway (CONCEPTS.md sections 2-6): per-tenant rate limit, provider chain with
retries, first-token timeout, per-provider circuit breaker on TTFT p95 / error rate,
streaming passthrough, /metrics."""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from llmgateway.breaker import CircuitBreaker
from llmgateway.limits import TenantLimiter

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
_STATE_CODE = {"closed": 0, "open": 1, "half_open": 2}


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str | None = None


@dataclass
class GatewayConfig:
    providers: dict[str, ProviderConfig]
    chain: list[str]
    tenant_rate: float = 50.0
    tenant_burst: float = 100.0
    tenant_overrides: dict[str, tuple[float, float]] = field(default_factory=dict)
    max_attempts_per_provider: int = 2
    backoff_base_s: float = 0.2
    connect_timeout_ms: float = 1000
    first_token_timeout_ms: float = 5000
    stream_idle_timeout_ms: float = 30000
    slo_ttft_ms: float = 800
    breaker_window_s: float = 30
    breaker_min_samples: int = 20
    breaker_max_error_rate: float = 0.5
    breaker_cooldown_s: float = 15
    breaker_probe_successes: int = 3


def _has_content(chunk: bytes) -> bool:
    """True if an SSE chunk carries a non-empty content delta."""
    return b'"content":"' in chunk.replace(b" ", b"") and b'"content":""' not in chunk.replace(b" ", b"") or b"[DONE]" in chunk


class _Attempt:
    """One upstream attempt: opens the stream and waits for the first content chunk."""

    def __init__(self, client: httpx.AsyncClient, p: ProviderConfig, body: dict, cfg: GatewayConfig):
        self.client, self.p, self.body, self.cfg = client, p, body, cfg
        self.resp: httpx.Response | None = None
        self.first_chunks: list[bytes] = []
        self.ttft_s: float | None = None

    async def open(self) -> str | None:
        """Returns an error string, or None on success with the bytes up to the first content buffered.

        The first-token timeout covers everything from connect to the first chunk that carries
        content (a role-only SSE chunk does not count); for non-streaming requests that is the
        whole response, which is correct: the client sees nothing before it either."""
        headers = {"authorization": f"Bearer {self.p.api_key}"} if self.p.api_key else {}
        body = {**self.body, "model": self.p.model}
        t0 = time.monotonic()
        try:
            return await asyncio.wait_for(self._open(body, headers, t0), timeout=self.cfg.first_token_timeout_ms / 1000)
        except TimeoutError:
            if self.resp is not None:
                await self.resp.aclose()
            return "first-token timeout"

    async def _open(self, body: dict, headers: dict, t0: float) -> str | None:
        try:
            req = self.client.build_request("POST", f"{self.p.base_url}/v1/chat/completions", json=body, headers=headers)
            self.resp = await self.client.send(req, stream=True)
        except httpx.HTTPError as e:
            return f"connect: {type(e).__name__}"
        if self.resp.status_code != 200:
            data = await self.resp.aread()
            await self.resp.aclose()
            return f"HTTP {self.resp.status_code}: {data[:120].decode(errors='replace')}"
        self._iter = self.resp.aiter_raw()
        streaming = "text/event-stream" in self.resp.headers.get("content-type", "")
        try:
            while True:
                chunk = await self._iter.__anext__()
                self.first_chunks.append(chunk)
                if not streaming or _has_content(chunk):
                    break
        except StopAsyncIteration:
            pass
        except httpx.HTTPError as e:
            await self.resp.aclose()
            return f"stream: {type(e).__name__}"
        self.ttft_s = time.monotonic() - t0
        return None

    async def rest(self):
        for c in self.first_chunks:
            yield c
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self._iter.__anext__(), timeout=self.cfg.stream_idle_timeout_ms / 1000)
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            await self.resp.aclose()


def create_app(cfg: GatewayConfig) -> FastAPI:
    app = FastAPI(title="llmgateway")
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=cfg.connect_timeout_ms / 1000, read=None, write=30, pool=None))
    limiter = TenantLimiter(cfg.tenant_rate, cfg.tenant_burst, cfg.tenant_overrides)
    breakers = {name: CircuitBreaker(cfg.slo_ttft_ms / 1000, cfg.breaker_window_s, cfg.breaker_min_samples,
                                     cfg.breaker_max_error_rate, cfg.breaker_cooldown_s, cfg.breaker_probe_successes)
                for name in cfg.providers}
    counters: Counter = Counter()
    in_flight: Counter = Counter()
    rng = random.Random(0)

    def now() -> float:
        return time.monotonic()

    async def try_chain(body: dict) -> tuple[_Attempt | None, str, int, list[str]]:
        """Returns (successful attempt, provider name, attempts, errors)."""
        attempts, errors = 0, []
        for i, name in enumerate(cfg.chain):
            p, cb = cfg.providers[name], breakers[name]
            state = cb.state(now())
            if state == "open":
                counters[("skipped_open", name)] += 1
                continue
            if state == "half_open" and not cb.allow_probe(now()):
                counters[("skipped_probe_busy", name)] += 1
                continue
            for k in range(cfg.max_attempts_per_provider):
                attempts += 1
                in_flight[name] += 1
                a = _Attempt(client, p, body, cfg)
                err = await a.open()
                if err is None:
                    cb.record_ttft(now(), a.ttft_s or 0.0)
                    counters[("requests", name)] += 1
                    return a, name, attempts, errors
                in_flight[name] -= 1
                cb.record_error(now())
                counters[("errors", name)] += 1
                errors.append(f"{name}: {err}")
                if k + 1 < cfg.max_attempts_per_provider and cb.state(now()) != "open":
                    counters[("retries", name)] += 1
                    await asyncio.sleep(cfg.backoff_base_s * (2 ** k) * (0.5 + rng.random()))
                else:
                    break
            if i + 1 < len(cfg.chain):
                counters[("fallbacks", name)] += 1
        return None, "", attempts, errors

    @app.get("/health")
    async def health():
        return {"status": "ok", "chain": cfg.chain, "breakers": {n: b.state(now()) for n, b in breakers.items()}}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        lines = []
        for (kind, name), v in sorted(counters.items()):
            label = "from" if kind == "fallbacks" else "provider"
            lines.append(f'llmgateway_{kind}_total{{{label}="{name}"}} {v}')
        for name, cb in breakers.items():
            lines.append(f'llmgateway_breaker_state{{provider="{name}"}} {_STATE_CODE[cb.state(now())]}')
            p95 = cb.ttft.p95(now())
            lines.append(f'llmgateway_ttft_p95_seconds{{provider="{name}"}} {p95 if p95 is not None else "NaN"}')
            lines.append(f'llmgateway_in_flight{{provider="{name}"}} {in_flight[name]}')
        for tenant, st in limiter.stats.items():
            lines.append(f'llmgateway_tenant_allowed_total{{tenant="{tenant}"}} {st["allowed"]}')
            lines.append(f'llmgateway_tenant_rejected_total{{tenant="{tenant}"}} {st["rejected"]}')
        lines.append(f"llmgateway_chain_exhausted_total {counters[('chain_exhausted', '')]}")
        return "\n".join(lines) + "\n"

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        tenant = request.headers.get("x-tenant", "anonymous")
        if not limiter.allow(tenant, now()):
            ra = limiter.retry_after(tenant, now())
            return JSONResponse({"error": "rate limited", "tenant": tenant}, status_code=429, headers={"retry-after": f"{ra:.3f}"})
        body = await request.json()
        t_req = now()
        attempt, name, attempts, errors = await try_chain(body)
        if attempt is None:
            counters[("chain_exhausted", "")] += 1
            return JSONResponse({"error": "all providers failed", "attempts": attempts, "errors": errors}, status_code=502,
                                headers={"x-llmgateway-attempts": str(attempts)})
        # User-facing TTFT runs from request arrival and includes retries and failed attempts;
        # the breaker uses the per-attempt TTFT (provider health), recorded inside try_chain.
        headers = {"x-llmgateway-provider": name, "x-llmgateway-attempts": str(attempts),
                   "x-llmgateway-ttft-ms": f"{(now() - t_req) * 1000:.1f}",
                   "x-llmgateway-attempt-ttft-ms": f"{(attempt.ttft_s or 0) * 1000:.1f}"}

        async def gen():
            try:
                async for chunk in attempt.rest():
                    yield chunk
            finally:
                in_flight[name] -= 1

        if body.get("stream"):
            return StreamingResponse(gen(), media_type=attempt.resp.headers.get("content-type", "text/event-stream"), headers=headers)
        data = b"".join([c async for c in gen()])
        return JSONResponse(httpx.Response(200, content=data).json(), headers=headers)

    app.state.breakers, app.state.counters, app.state.limiter = breakers, counters, limiter
    return app
