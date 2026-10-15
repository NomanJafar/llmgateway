"""`llmgateway --config gw.yaml`: run the gateway from a YAML file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

from llmgateway.app import GatewayConfig, ProviderConfig, create_app


def load_config(path: str | Path) -> GatewayConfig:
    doc = yaml.safe_load(Path(path).read_text())
    providers = {}
    for name, p in doc["providers"].items():
        key = os.environ.get(p["api_key_env"]) if p.get("api_key_env") else p.get("api_key")
        providers[name] = ProviderConfig(name=name, base_url=p["base_url"].rstrip("/"), model=p["model"], api_key=key)
    tenants = doc.get("tenants", {})
    default = tenants.get("default", {"rate": 50, "burst": 100})
    overrides = {t: (float(v["rate"]), float(v["burst"])) for t, v in tenants.items() if t != "default"}
    b = doc.get("breaker", {})
    kw = {k: doc[k] for k in ("max_attempts_per_provider", "backoff_base_s", "connect_timeout_ms", "first_token_timeout_ms",
                              "stream_idle_timeout_ms", "slo_ttft_ms") if k in doc}
    return GatewayConfig(providers=providers, chain=list(doc["chain"]), tenant_rate=float(default["rate"]),
                         tenant_burst=float(default["burst"]), tenant_overrides=overrides,
                         breaker_window_s=b.get("window_s", 30), breaker_min_samples=b.get("min_samples", 20),
                         breaker_max_error_rate=b.get("max_error_rate", 0.5), breaker_cooldown_s=b.get("cooldown_s", 15),
                         breaker_probe_successes=b.get("probe_successes", 3), **kw)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="LLM gateway with rate limits, retries, fallback chains and TTFT-SLO degradation")
    p.add_argument("--config", required=True)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    a = p.parse_args(argv)
    uvicorn.run(create_app(load_config(a.config)), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
