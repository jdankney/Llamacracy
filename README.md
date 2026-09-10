# Llamacracy

Self-hosted multi-model LLM chat hub for a handful of trusted users, with
per-person credit metering and an admin billing dashboard. Runs against the
local GGUF models on one box, behind NetBird + OIDC. Never exposed publicly.

See [docs/SPEC.md](docs/SPEC.md) for the full design brief.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Measure reality — benchmark every GGUF | **done** (`bench-results.json`) |
| 1 | Inference layer — llama-swap config from bench data | **done** (`config/`) |
| 2 | Backend — FastAPI, FIFO queue worker, SSE proxy, cancellation, SQLite | **done** (`llamacracy/`) |
| 3 | Metering — credits, session/weekly limits, cost model, tests | **done** (`metering.py`, 28 tests) |
| 4 | Frontend — chat UI, model picker, live queue, usage page | **done** (`static/`) |
| 5 | Admin dashboard | **done** (`admin.py`) |
| 6 | Deployment — systemd units, oauth2-proxy, NetBird binding | next |

## Hardware

GTX 1080 Ti (11 GB, Pascal sm_61), Ryzen 5 3600, 32 GB DDR4-3600, models on
an NVMe Crucial T500 (btrfs, zstd:3). Desktop session holds ~1.2 GB VRAM, so
llama-server sees ~9.8 GB free.

`llama.cpp` is built at `~/llama.cpp` with
`-DCMAKE_CUDA_ARCHITECTURES=61 -DGGML_CUDA_FORCE_MMQ=ON` (int8 MMQ kernels;
the FP16 cuBLAS path is 1/64 rate on Pascal).

## Phase 0

```bash
python3 bench/phase0_bench.py --dry-run          # show the plan
python3 bench/phase0_bench.py                    # full sweep, resumable
python3 bench/phase0_bench.py --only fast-4b   # one model
```

Output: `bench/bench-results.json` (committed — it seeds the llama-swap
config, the queue's load-time estimates, and the credit limits).

## Running

**Dev** (single user, fake identity):
```bash
uv sync
~/.local/bin/llama-swap -config config/llama-swap.yaml -listen 127.0.0.1:8091 &
uv run uvicorn llamacracy.app:app --host 127.0.0.1 --port 8000   # DEV_MODE=1 in .env
uv run pytest -q
```

**Production** (systemd user units + oauth2-proxy on the NetBird interface):
```bash
./deploy/install.sh
```
See [deploy/OPERATIONS.md](deploy/OPERATIONS.md) for restarts, adding a model,
adjusting limits, and backup.

## Design

- [docs/SPEC.md](docs/SPEC.md) — the brief
- [docs/DECISIONS.md](docs/DECISIONS.md) — every non-obvious choice, with the why
- [bench/bench-results.json](bench/bench-results.json) — measured per-model
  load / throughput / VRAM / GPU watts (seeds the config, the queue estimates,
  and the credit limits)
