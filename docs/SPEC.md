# Llamacracy — design brief

> Verbatim brief from the project owner (the owner). Source of truth for scope.
> Implementation notes and deviations agreed during Phase 0 are tracked in
> [DECISIONS.md](DECISIONS.md).

---

## Goal

A small web service that lets the owner and a few friends chat with any of the
local GGUF models on this machine. Pick a model in the UI, send a prompt, the
right model gets loaded, the response streams back. Usage is metered per person
against session and weekly limits. Admin dashboard shows who is burning what,
for billing a few cents.

## Hardware constraints

- GPU: GTX 1080 Ti, 11 GB VRAM, compute capability 6.1 (Pascal)
- CPU: Ryzen 5 3600 (6c/12t)
- RAM: 32 GB DDR4-3600 dual channel, ~50 GB/s real bandwidth
- Models: GGUF files in `~/models`
- Access: self-hosted NetBird only. Never exposed to the public internet.

Pascal specifics:

- FP16 throughput is 1/64 rate — cuBLAS FP16 paths are a trap.
- Build llama.cpp with `-DCMAKE_CUDA_ARCHITECTURES=61 -DGGML_CUDA_FORCE_MMQ=ON`.
- Flash attention is not well optimised for Pascal — benchmark `-fa on` vs
  `-fa off` per model.
- Consider `--cache-type-k q8_0 --cache-type-v q8_0` to buy context length back.

## Model inventory (owner's estimates; Phase 0 measures real numbers)

| Model | Quant | Approx weights | Notes |
|---|---|---|---|
| Coder Coder 1.5B | Q8_0 | ~1.7 GB | FIM only. Not a chat model. |
| FamilyA 4B | Q8_0 | ~4.3 GB | Fits fully in VRAM |
| FamilyB 4B | Q8_0 | ~5–8 GB | Fits, tight |
| FamilyA 9B | Q6_K | ~7.5 GB | Fits, ~3 GB left for KV cache |
| FamilyB 26B QAT | Q4_0 | ~15 GB | MoE, needs CPU offload |
| FamilyC Flash 30B | Q4_K_M | ~18 GB | MoE, needs CPU offload |
| FamilyA 35B | Q4_K_M | ~20 GB | MoE, needs CPU offload |

MoE heavyweights: attention + shared layers on GPU, expert tensors in system
RAM via `--n-cpu-moe N`. Expect ~8–15 tok/s generation.

The Coder Coder FIM model must not appear in the chat model picker. It
targets `/infill`. Hide it or give it a separate, clearly-labelled mode.

## Architecture

- Do not write process management. Use **llama-swap** (or `llama-server` router
  mode — evaluate both, Phase 1). Our app talks to it over HTTP as an
  OpenAI-compatible endpoint and never spawns `llama-server` directly.
- Backend: Python 3.11+, FastAPI, SQLite (WAL), `httpx` for upstream streaming.
- Frontend: static SPA served by FastAPI. Vanilla JS or Preact + Tailwind via
  CDN. No build step if avoidable.
- Auth edge: oauth2-proxy.
- Deployment: systemd user units for llama-swap, the app, and oauth2-proxy.

## The queue (core of the project)

Strict global FIFO. Exactly one inference at a time. No parallelism,
reordering, priority, or coalescing. Deliberate.

Job lifecycle: `queued -> loading_model -> generating -> done | error | cancelled | limit_exceeded`

- Single async worker task drains the queue. All submissions go through it.
- Every connected client gets live queue state over SSE: full ordered list of
  jobs with owner, model name, state, position.
- Currently loaded model always visible, sourced from llama-swap `/running`.
- Per-model estimated load time, learned from history (Phase 0 seeds it).
- A user may cancel their own job while `queued` or `generating`. Cancelling a
  `generating` job must actually abort the upstream request.
- Idle TTL: after N minutes with an empty queue, unload the resident model.
  Default 15 minutes.
- Config flag `SMALL_MODEL_FAST_LANE` (default `false`): models under a VRAM
  threshold run in a second concurrent lane. Wire it up, leave it off. Record a
  `lane` on each job; only exclusive-lane seconds are full price.

## Auth and identity

Identity from OIDC against the NetBird IdP (embedded Dex on v0.62+, or Zitadel
on older quickstart deployments).

- Preferred: `oauth2-proxy` in front of the app, configured against the OIDC
  issuer's `.well-known/openid-configuration`. Forwards `X-Forwarded-Email`,
  `X-Forwarded-User`, `X-Forwarded-Preferred-Username`. App trusts those
  headers and writes no auth code.
- Fallback: OIDC auth code flow in FastAPI with Authlib, sessions in SQLite.
- App binds to `127.0.0.1` only.
- oauth2-proxy binds to the NetBird interface address (`wt0`, a `100.x.x.x`).
  Discover at startup, don't hardcode.
- Fail loudly on boot if the identity header is absent and `DEV_MODE` is unset.
- Issuer URL, client ID, client secret from a gitignored `.env`. Never commit.
- On first sight of a user, create a `users` row keyed on the OIDC `sub` claim,
  not the email.

## Metering

**1 credit = 1 second of exclusive box time.** Record it, bill on it, enforce
limits on it. Also record raw `prompt_tokens` / `completion_tokens` per message
for display.

Charging rules:

- Generation time: always charged to the requesting user.
- Model load time: charged to the user who triggered the load, at
  `LOAD_TIME_MULTIPLIER` (default `0.5`).
- Failed loads and upstream errors: charged at zero.
- Cancelled jobs: charged for seconds actually consumed up to the abort.
- Queue waiting time: never charged.

Capturing usage:

- Send `stream_options: {"include_usage": true}` on every streaming call.
- If the usage block is missing, fall back to counting chunks and estimating
  from measured tok/s in `bench-results.json`. Flag rows `usage_estimated =
  true`. If the estimated fraction exceeds a few percent, surface it.
- Credits are always measured directly from our own timers. Never estimated.

Limits:

- Session limit: starts on a user's first request with no active session, runs
  exactly 5 hours. Cap `SESSION_CREDIT_LIMIT`. No extension on activity.
- Weekly limit: rolling 7-day sum, cap `WEEKLY_CREDIT_LIMIT`. Rolling, not
  calendar.
- Both per-user, overridable per-user by admin.
- Checked at enqueue time, not generation time.
- On rejection, return which limit was hit and the exact UTC reset timestamp.
- Allow overshoot: a job admitted then running past the cap finishes; record
  the overage. Bounded by `MAX_TOKENS_PER_REQUEST` (hard default 2048, per-model
  override).
- Warn in the UI at 75% and 90% of either limit.

Cost model (config block, all editable without code):

- `IDLE_WATTS`, `LOAD_WATTS` — whole system at the wall.
- `ELECTRICITY_RATE` in $/kWh — the utility Portland, ask owner. Do not hardcode a
  national average.
- Optional `TOU_SCHEDULE`: hour-of-day → rate. If present, cost each job at the
  rate in effect when it ran.
- Derived: `cost_usd` per job = credits × watts × rate, and a `MARKUP` (default
  1.0). Store `cost_usd` at write time using the rate then in effect. Never
  recompute historical costs.

## Persistence (SQLite, WAL)

- `users` — id, oidc_sub (unique), email, display_name, is_admin,
  session_credit_limit_override, weekly_credit_limit_override, created_at
- `conversations` — id, user_id, title, model_id, created_at, updated_at
- `messages` — id, conversation_id, role, content, model_id, prompt_tokens,
  completion_tokens, usage_estimated, created_at
- `jobs` — id, user_id, conversation_id, model_id, state, lane, queued_at,
  load_started_at, gen_started_at, finished_at, load_seconds, gen_seconds,
  credits, cost_usd, rate_used, error
- `sessions` — id, user_id, started_at, expires_at, credits_used
- `invoices` — id, user_id, period_start, period_end, total_credits,
  total_cost_usd, status (draft/sent/paid), created_at

Index `jobs` on `(user_id, finished_at)` — the rolling weekly query runs on
every enqueue.

Conversation titles: first 50 chars of the first user message. No model call.

## User-facing usage page

Every user gets `/usage`: current session credits and time remaining, weekly
credits vs cap, a 30-day chart, per-model breakdown, running cost for the
current billing period.

## Admin dashboard

Admin from an `ADMIN_EMAILS` list in config, checked against OIDC identity.

- **Live** — queue with owners/models, loaded model, VRAM/temp/power via
  `nvidia-smi`, session occupancy.
- **Users** — session credits + % cap, weekly credits + % cap, all-time
  tokens, all-time cost, last active. Sortable. Highlight >90%.
- **Usage** — time series of credits by user and model. Per-model totals:
  request count, total occupancy, mean tokens/request, mean tok/s.
- **Queue impact** — per user, total seconds of wait inflicted on others.
- **Billing** — cost per user per period, draft invoice, mark sent, mark paid,
  export CSV.
- **Controls** — per-user limit overrides, force-unload model, kill a job,
  disable a user.

## Explicit non-goals

- No Ollama, no LM Studio, no cloud API calls.
- No Docker unless justified. Single box.
- No payment processing. Invoices are a CSV and a status field.
- No user-facing sampling parameter controls in v1. Per-model defaults.
- No RAG, no tool use, no attachments, no image input.
- No rate limiting beyond the credit limits.
