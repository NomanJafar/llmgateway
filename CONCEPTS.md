# Concepts: a gateway that turns provider failures into nothing the user notices

`llmgateway` sits between applications and several model backends (a self-hosted vLLM, one or more hosted APIs) and makes four promises: requests are rate-limited per tenant, transient failures are retried, a failing provider is swapped for the next one in a chain, and a provider whose latency drifts past its target is demoted before users feel it. This primer is about what each promise costs and how to tell whether it is being kept.

## 1. What an SLO is, and why TTFT is the one to watch

A **service level objective** is a target for a measured quantity over a window: "p95 time-to-first-token under 800 ms over 5 minutes", "99.5% of requests succeed over 30 days". Its purpose is to decide, mechanically, when a dependency is unhealthy: not when it returns errors (that is the easy case) but when it is *slow*, which hurts users just as much and shows up in no error log.

For LLM traffic, TTFT is the right signal for routing decisions:

- It is measured on every request, early, so a degradation is visible within seconds; end-to-end latency depends on output length and arrives only when the request finishes.
- It reflects the provider's queue: a provider under load shows rising TTFT long before it starts failing.
- It is what a user perceives as "is it responding?".

Inter-token latency matters for stream quality but rarely drives failover; errors and timeouts drive it, but late. The gateway keeps a rolling TTFT distribution per provider and compares its p95 to the provider's target.

## 2. Rate limiting: the token bucket

A tenant may send N requests per second with bursts up to B. The standard mechanism is a **token bucket**: a counter that refills at N per second up to capacity B; each request takes one token or is rejected (HTTP 429) when the bucket is empty. Bursts up to B pass immediately; sustained load is capped at N; the bucket is a few floats per tenant, updated lazily on each request (`tokens = min(B, tokens + (now − last) × N)`).

Two choices to make explicit: whether to limit by requests or by tokens (LLM cost is in tokens; a token bucket per *model tokens* needs the prompt token count up front, which the gateway can estimate from characters or read from the first provider response), and what to return on rejection (429 with a `Retry-After`, never a silent queue, because queueing inside the gateway is how latency SLOs die).

## 3. Retries: when they help and when they hurt

Retry only what is safe and likely to succeed: connection failures, 5xx, 429 from the provider, and timeouts *before the first token*. Do not retry after streaming has begun (the user already saw partial output), and do not retry 4xx client errors (they will fail again).

Retries multiply load on a struggling provider. The guards against that are **exponential backoff with jitter** (wait 0.2 s, 0.4 s, 0.8 s, each ± random, so retries from many clients do not synchronise into a thundering herd), a small **retry budget** (two attempts, not ten), and **retrying on a different provider** when a chain exists, which is the next section.

## 4. Fallback chains and degradation

Each route names an ordered chain, for example `[self-hosted vLLM, provider A, provider B]`. A request tries the first; on a retryable failure it moves down the chain. Two kinds of movement:

- **Per-request fallback**: this request failed here, try the next. Cheap, local, no state.
- **Degradation**: the gateway has decided, from the rolling SLO, that a provider is unhealthy, and stops sending it *any* traffic for a cooldown period, then probes it with a trickle before restoring it. This is the circuit-breaker pattern: closed (traffic flows), open (traffic diverted), half-open (probing).

Degradation is what keeps a slow provider from making every request slow: without it, each request would wait out the slow provider's timeout before falling back. The trigger is the SLO: p95 TTFT over the last window above the target, or error rate above a threshold. The cooldown and the probe rate are the knobs; too eager and the gateway flaps between providers, too lazy and users sit on a dead one.

"Degradation" can also mean a *lesser* fallback: a smaller model, a shorter max output, a cached answer. The chain's order encodes that preference; the README says so when a fallback is worse, not just different.

## 5. Timeouts are part of the contract

Every provider call needs a **connect timeout** (fast: a provider that will not accept a connection in a second is down), a **first-token timeout** (the TTFT SLO plus slack: if the first token has not arrived by then, this attempt is abandoned and the chain continues), and a **stream idle timeout** (if the stream stalls mid-way, end it; retrying is not possible, but hanging forever is worse). Without a first-token timeout the fallback chain is theoretical.

## 6. What the gateway measures about itself

Per provider: requests, errors by class, retries, fallbacks taken, rolling TTFT p50/p95, breaker state and transitions. Per tenant: admitted and rejected requests. Globally: requests that exhausted the chain (the only ones users see as errors). These are the numbers the chaos project (14) reads while it breaks things, and they are exposed on `/metrics`.

## 7. What the experiment will show

With three providers simulated by tokenbench mock servers (one healthy, one slow, one failing) and a steady load:

1. **Rate limiting**: a tenant above its limit gets 429s at exactly the configured rate; others are unaffected.
2. **Retries**: a provider with 30% injected 5xx errors shows user-visible errors near 0.3² ≈ 9% with one retry on the same provider, and near 0 with fallback.
3. **Degradation**: the slow provider's TTFT is pushed past the SLO mid-run; the gateway opens the breaker within one window, user TTFT recovers, and the probe restores the provider when it recovers. The timeline shows the breaker state against user-visible TTFT.
4. **Chain exhaustion**: all providers failing produces clean errors, fast, not timeouts.

The mock servers make this cheap and deterministic; one run against a real vLLM replica as the first provider confirms nothing in the gateway depends on the mock.

## Further reading

- Beyer et al., *Site Reliability Engineering*, chapters on SLOs and handling overload; and *The Site Reliability Workbook*, "Implementing SLOs".
- Nygard, *Release It!*, on circuit breakers, bulkheads and timeouts.
- Marc Brooker, *Exponential Backoff and Jitter* (AWS Architecture Blog, 2015).
- The Envoy proxy documentation on outlier detection, for a production version of the degradation logic.
