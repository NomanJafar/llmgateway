# llmgateway

AI gateway routing across self-hosted and API providers with TTFT SLOs, rate limits, retries and degradation chains.

Project 13 of 15 in [inference-infra-portfolio](https://github.com/NomanJafar/inference-infra-portfolio).

## What this builds

A small OpenAI-compatible gateway that makes four promises to the application in front of it: per-tenant rate limits (token bucket, 429 with `Retry-After`), retries with exponential backoff and jitter on retryable failures, an ordered fallback chain across providers, and per-provider circuit breakers driven by a rolling TTFT p95 against an SLO (and by error rate), with a first-token timeout so a slow provider cannot hold a request hostage. [CONCEPTS.md](CONCEPTS.md) explains each mechanism and what it costs.

## Why it matters

Provider outages are guaranteed. User-facing errors are optional.

## Usage

```bash
pip install "git+https://github.com/NomanJafar/llmgateway"
llmgateway --config gw.yaml --port 8090
```

```yaml
# gw.yaml
chain: [local, hosted]                      # tried in order; a request moves down on retryable failure
providers:
  local:  {base_url: http://127.0.0.1:8000, model: Qwen/Qwen2.5-1.5B-Instruct}
  hosted: {base_url: https://api.example.com, model: some-model, api_key_env: EXAMPLE_KEY}
tenants:
  default: {rate: 20, burst: 40}            # requests per second, burst size; tenant = x-tenant header
  gold:    {rate: 200, burst: 400}
slo_ttft_ms: 600                            # rolling p95 above this opens the provider's breaker
first_token_timeout_ms: 2000                # give up on an attempt that shows no content by then
breaker: {window_s: 30, min_samples: 10, cooldown_s: 20}
```

Every response carries `x-llmgateway-provider`, `x-llmgateway-attempts` and `x-llmgateway-ttft-ms`; `/metrics` exposes per-provider requests, errors, retries, fallbacks, skips while open, breaker state (0 closed / 1 open / 2 half-open), rolling TTFT p95, in-flight, per-tenant allowed/rejected, and chain exhaustions.

## What is in here

| Module | Purpose |
|---|---|
| `llmgateway.limits` | `TokenBucket`, `TenantLimiter` (time passed explicitly; no sleeping in tests) |
| `llmgateway.breaker` | `RollingWindow` (p95 over the last N seconds), `CircuitBreaker` (closed → open on SLO or error rate, → half-open after cooldown, probes → closed) |
| `llmgateway.app` | The FastAPI gateway: chain walk with retries, first-token timeout that covers connect-to-first-content, streaming passthrough, metrics |
| `llmgateway.cli` | YAML config → `GatewayConfig`, `llmgateway` command |

## Tests

19 tests. The gateway is exercised end to end against three live [tokenbench](https://github.com/NomanJafar/tokenbench) mock providers (healthy; slow with 400 ms TTFT; flaky with 50% 500s): rate limiting per tenant, retry-then-fallback absorbing every failure, breaker opening on TTFT p95 after `min_samples` and traffic moving, first-token timeout forcing fallback in under a second, clean 502 on chain exhaustion, and concurrent streams staying separate.

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest
```

## Results

The gateway is measured by [inferchaos](https://github.com/NomanJafar/inferchaos), which injects faults into providers behind it. Mock suite (two providers, 6 req/s, SLO: success and TTFT ≤ 800 ms at 99 %; gateway: first-token timeout 1.6 s, breaker on TTFT p95 / error rate over a 15 s window with 10 samples minimum, 10 s cooldown; 20 s baseline, 60 s fault on `p0`, 60 s recovery):

| fault on `p0` | requests | user errors | 429s | error budget consumed | breaker opened after | slow requests before it | fallbacks | recovery |
|---|---|---|---|---|---|---|---|---|
| none | 840 | 0 | 0 | 0 % | – | 0 | 0 | – |
| throttle (2 s TTFT) | 841 | 0 | 0 | **523 %** | 7.1 s | 40 | 44 | 3.2 s |
| kill | 842 | 0 | 0 | **0 %** | 5.6 s | 0 | 36 | 3.2 s |
| 10x spike | 3,361 | 0 | 2,324 | 0 % | – | 0 | 0 | 3.0 s |

![inferchaos mock faults](https://raw.githubusercontent.com/NomanJafar/inferchaos/main/results/mock/faults.png)

A dead provider cost nothing: every attempt failed instantly, the retry went to the next provider, and the error-rate breaker opened within 6 s. A *slow* provider cost five runs' worth of error budget, because each of the first 40 requests waited the full first-token timeout before falling back and the TTFT breaker needed ten samples before it could act. The spike became 2,324 rejections at the token bucket and no budget burn.

### Against a real provider

Done in [inferchaos](https://github.com/NomanJafar/inferchaos) (run 4, 18 Oct 2026): a real vLLM replica on a T4 as the first provider, a mock as the fallback, 4 req/s. The replica was killed mid-load and restarted: **0 user-visible errors** out of 1,827 requests, the error-rate breaker opened 5.1 s after the kill, retries and fallbacks covered the 60 s fault window with zero SLO misses, and the breaker's half-open probe put traffic back on the replica 14 s after it came back. The same run showed the breaker's TTFT rule opening on a *freshly started* replica whose first requests took 3.5 s (Triton JIT compilation): the gateway treated a cold replica as a sick one, which is correct behaviour and an argument for warming a replica before admitting it to the chain. Full timeline and plot in inferchaos's README.

The mock-fault suite in the same project found one bug here: the TTFT response header reported the successful attempt's time (40 ms on the fallback) rather than the user's end-to-end wait (1.6 s on the throttled provider). It is now end-to-end, with the per-attempt figure in a separate header.

## Not included

Caching, prompt rewriting, cost-based routing, authentication. The chain order is the only policy; a cost-aware policy is the obvious extension.

## License

MIT
