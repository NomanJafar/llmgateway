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

17 tests. The gateway is exercised end to end against three live [tokenbench](https://github.com/NomanJafar/tokenbench) mock providers (healthy; slow with 400 ms TTFT; flaky with 50% 500s): rate limiting per tenant, retry-then-fallback absorbing every failure, breaker opening on TTFT p95 after `min_samples` and traffic moving, first-token timeout forcing fallback in under a second, clean 502 on chain exhaustion, and concurrent streams staying separate.

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest
```

## Validation against a real provider

One run with a vLLM replica as the first provider (and mocks as fallbacks) is part of the chaos project (14), which breaks the replica on purpose and watches this gateway's breaker react.

## Not included

Caching, prompt rewriting, cost-based routing, authentication. The chain order is the only policy; a cost-aware policy is the obvious extension.

## License

MIT
