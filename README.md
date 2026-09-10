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
| 3 | Metering — credits, session/weekly limits, cost model, tests | next |
| 4 | Frontend — chat UI, model picker, live queue, usage page | not started |
| 5 | Admin dashboard | not started |
| 6 | Deployment — systemd units, oauth2-proxy, NetBird binding | not started |

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

## Running (dev)

```bash
~/.local/bin/llama-swap -config config/llama-swap.yaml -listen 127.0.0.1:8091 &
uv run uvicorn llamacracy.app:app --host 127.0.0.1 --port 8000   # DEV_MODE=1 in .env
uv run pytest -q
```
