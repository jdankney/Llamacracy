<p align="center">
  <img src="llamacracy/static/assets/icon-192.png" width="96" alt="Llamacracy">
</p>

<h1 align="center">Llamacracy</h1>

<p align="center">
  A self-hosted, multi-model LLM chat hub for you and a few friends.<br>
  One GPU, a strict FIFO queue, per-person credit metering, and an admin dashboard that knows what everyone's usage costs in electricity.
</p>

<p align="center">
  <img src="docs/screenshot.png" alt="Llamacracy chat UI" width="900">
</p>

## What it is

Llamacracy turns a single box with one consumer GPU into a small, private
"ChatGPT for the group chat". Friends sign in over a WireGuard mesh (NetBird),
pick a local GGUF model, and get streaming answers with markdown, code
highlighting and LaTeX. Every second the GPU spends on someone is measured and
charged to them as a credit, with session and weekly limits, so one heavy
user can never monopolise the machine.

It is deliberately small: a FastAPI backend, SQLite, a vanilla-JS front-end
with no build step, and [llama-swap](https://github.com/mostlygeek/llama-swap)
doing the model loading. No Docker for the app itself, no cloud APIs, no
payment processing.

## Features

- **Multi-model chat** with a model picker, per-model sampling defaults, and
  live "loaded / cold start ~4s" hints so people know what a switch costs.
- **Strict FIFO queue**, exactly one inference at a time, with a live queue
  view for everyone, cancellation that really aborts the upstream request, and
  an idle TTL that frees VRAM when nobody is around.
- **Credit metering**: `1 credit = 1 second of exclusive GPU time`, measured
  from the app's own timers, never estimated. Rolling 5-hour session and
  7-day weekly limits, per-user overrides, overshoot allowed, and a cost
  model that uses measured GPU watts and your real electricity tariff
  (time-of-use aware).
- **Rich rendering**: markdown, syntax-highlighted code, KaTeX math, a copy
  button that copies the raw source, and a context-window ring that shows how
  full the prompt is before you send.
- **Web search on demand**: flip a toggle and that one message gets the top
  SearXNG snippets prepended, with sources cited under it. Never a tool loop.
- **Vision on demand**: attach an image and the turn routes to the model's
  vision variant. Images live on disk, never base64 in the database.
- **Compact history**: fold the older part of a long conversation into a
  running summary, on request, billed like any other reply.
- **Usage page** per user with a 30-day chart and self-service API keys.
- **OpenAI-compatible `/v1` endpoint** for IDE tools such as Continue.dev,
  sharing the same queue and limits.
- **Admin dashboard**: live GPU stats, users, per-model performance, "queue
  impact" fairness, billing with draft invoices and CSV export, API-key
  revocation, and per-user controls.
- **Installable PWA** on phones, over plain HTTP inside the tunnel.

## How it works

```
 browser ──HTTP over NetBird──▶ oauth2-proxy ──▶ FastAPI app ──▶ llama-swap ──▶ llama-server
                                     │              │  ▲               (one model resident)
                                     ▼              ▼  │
                                    Dex           SQLite (WAL)
                              (users + passwords)  jobs · credits · chats
```

- **Identity** comes from oauth2-proxy's forwarded headers. The app never
  writes auth code and refuses to serve if the headers are missing (unless
  `DEV_MODE=1`). Users are keyed on the OIDC `sub`, not the email.
- **The queue** (`llamacracy/queue.py`) is the core: a single worker task
  drains jobs in order, streams tokens back over SSE, learns per-model load
  times, and asks llama-swap which model is resident rather than guessing.
- **Metering** (`llamacracy/metering.py`) bills at finalisation and freezes
  the rate on each job row, so historical costs never change.

## Requirements

- Linux with an NVIDIA GPU (`nvidia-smi` is used for power sampling; the app
  falls back to a per-model estimate without it).
- Python 3.14+ and [uv](https://docs.astral.sh/uv/).
- A `llama.cpp` build and [llama-swap](https://github.com/mostlygeek/llama-swap).
- GGUF models on disk.
- For multi-user use: NetBird (or any private network), Docker for Dex, and
  oauth2-proxy (installed by `deploy/install.sh`).

The reference box is a GTX 1080 Ti (11 GB), Ryzen 5 3600 and 32 GB RAM. The
shipped `config/` was generated from benchmarks on that hardware; see
[bench/](bench/) for how to regenerate it for yours.

## Quick start (single user, no auth)

```bash
uv sync
cp .env.example .env               # set DEV_MODE=1 and your rates
llama-swap -config config/llama-swap.yaml -listen 127.0.0.1:8091 &
uv run uvicorn llamacracy.app:app --host 127.0.0.1 --port 8000
uv run pytest -q                   # 36 tests
```

Open <http://127.0.0.1:8000>. In dev mode you are a fake user with the email
from `DEV_EMAIL`; add that email to `ADMIN_EMAILS` to see the admin dashboard.

## Adding your own models

1. Put the GGUF under `~/models/<Name>/`.
2. Benchmark it so the queue's estimates and the credit limits are real:
   `python3 bench/phase0_bench.py --only <key>` (add it to the inventory in
   that script first, or hand-edit `bench/bench-results.json`).
3. Regenerate `config/llama-swap.yaml` and `config/models.json` with
   `python3 bench/gen_llamaswap_config.py`, then restart.

Display names, tiers, blurbs and sampling defaults live in `REGISTRY_META`
inside the generator.

## Production (friends over NetBird)

```bash
./deploy/install.sh                            # units, oauth2-proxy, Dex scaffold, `llamacracy` CLI
cd deploy/dex && ./gen-hash.sh 'a-password'    # one staticPasswords entry per user
cd ../.. && llamacracy up
```

Four systemd user units run the stack: `llama-swap`, `llamacracy`,
`llamacracy-dex` and `llamacracy-auth`. `llamacracy {up,down,restart,status,logs}`
drives them together. Users sign in at `http://<your-netbird-fqdn>:4180`.

Full runbook: [deploy/OPERATIONS.md](deploy/OPERATIONS.md). Identity provider
notes: [deploy/dex/README.md](deploy/dex/README.md). The onboarding note sent
to users: [docs/WELCOME.md](docs/WELCOME.md).

## Configuration

Everything is an environment variable, documented with defaults in
[`.env.example`](.env.example): credit limits, session window, load-time
multiplier, electricity rate or time-of-use table, idle TTL, search, uploads,
and compaction. Per-user overrides are set from the admin dashboard.

## Project layout

```
llamacracy/        FastAPI app: queue, metering, identity, admin, OpenAI-compatible API
llamacracy/static/ the SPA (index.html, assets/app.js, assets/styles.css), no build step
config/            generated llama-swap config + model registry
bench/             benchmark harness and results that seed config/ and the credit math
deploy/            systemd units, install script, oauth2-proxy + Dex config, the CLI
docs/              SPEC.md (the brief), DECISIONS.md (every non-obvious choice, with the why)
tests/             pytest suite for the queue, metering and search
```

## Design notes

- [docs/SPEC.md](docs/SPEC.md) is the original brief and the source of truth
  for scope.
- [docs/DECISIONS.md](docs/DECISIONS.md) is a dated log of every deviation and
  non-obvious decision, newest first.
