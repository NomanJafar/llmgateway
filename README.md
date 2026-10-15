# llmgateway

AI gateway routing across self-hosted and API providers with TTFT SLOs, rate limits, retries and degradation chains.

Project 13 of 15 in [inference-infra-portfolio](https://github.com/NomanJafar/inference-infra-portfolio).

## What this builds

A router across self-hosted and API providers with TTFT SLOs, rate limits, retries and degradation chains.

## Why it matters

Provider outages are guaranteed. User-facing errors are optional.

## Status

Concept primer written (`CONCEPTS.md`). The gateway is next; it is developed against mock providers and validated once against a real vLLM replica.

## Layout

```
CONCEPTS.md     the theory behind the project, written before the code
src/llmgateway/   implementation
tests/          CPU-only tests, runnable on a laptop
notebooks/      GPU notebooks (Kaggle / Colab) that run the experiments and write results/
results/        measured numbers and plots
```

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

GPU experiments run from `notebooks/`; each notebook clones this repo, installs it and writes its outputs to `results/`.

## Hardware notes

Developed against free-tier GPUs (Kaggle and Colab T4, 16 GB). Where a T4 limits what can be measured (no FP8, Turing-era kernels), the README says so rather than papering over it.

## License

MIT
