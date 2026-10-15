from llmgateway.cli import load_config

YAML = """
chain: [local, hosted]
providers:
  local:  {base_url: http://127.0.0.1:8000, model: Qwen/Qwen2.5-1.5B-Instruct}
  hosted: {base_url: https://api.example.com, model: some-model, api_key_env: EXAMPLE_KEY}
tenants:
  default: {rate: 20, burst: 40}
  gold: {rate: 200, burst: 400}
slo_ttft_ms: 600
first_token_timeout_ms: 2000
breaker: {window_s: 30, min_samples: 10, cooldown_s: 20}
"""


def test_load_config_builds_gateway_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "sk-test")
    p = tmp_path / "gw.yaml"; p.write_text(YAML)
    cfg = load_config(p)
    assert cfg.chain == ["local", "hosted"] and cfg.providers["hosted"].api_key == "sk-test"
    assert cfg.providers["local"].api_key is None and cfg.providers["local"].model.startswith("Qwen")
    assert (cfg.tenant_rate, cfg.tenant_burst) == (20, 40) and cfg.tenant_overrides["gold"] == (200, 400)
    assert cfg.slo_ttft_ms == 600 and cfg.first_token_timeout_ms == 2000
    assert cfg.breaker_window_s == 30 and cfg.breaker_min_samples == 10 and cfg.breaker_cooldown_s == 20
