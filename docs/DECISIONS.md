# Decisions & deviations

Running log of choices made against [SPEC.md](SPEC.md), with the reasoning.
Newest first.

## Phase 6 — deployment (2026-09-09)

- Three **systemd user units** (`deploy/systemd/`): `llama-swap` (`127.0.0.1:8091`,
  `-watch-config`), `llamacracy` (`127.0.0.1:8000`, `.venv/bin/uvicorn`),
  `llamacracy-auth` (oauth2-proxy). `install.sh` enables linger so they run
  without a login session.
- **oauth2-proxy** is the only public-facing process. `ExecStartPre` reads the
  `wt0` address at start and writes `OAUTH2_PROXY_HTTP_ADDRESS` +
  `OAUTH2_PROXY_REDIRECT_URL` to `%t/llamacracy-auth.env` — so a NetBird
  reconnect just needs `systemctl --user restart llamacracy-auth`, no config
  edit. `ConditionPathExists=/sys/class/net/wt0` keeps it from flapping when
  NetBird is down.
- oauth2-proxy config split: non-secret `deploy/oauth2-proxy.cfg` (in git) +
  `deploy/oauth2-proxy.env` (gitignored: client id/secret, cookie secret which
  `install.sh` generates). `cookie_secure = false` — plain HTTP is fine inside
  the WireGuard tunnel.
- oauth2-proxy binary comes from the GitHub release (`v7.6.0`), not pacman
  (not currently in the Arch repos).
- **Verified** the production identity path (no `DEV_MODE`): missing headers →
  503 + loud log; `X-Forwarded-User/Email` → user upserted on `sub`; admin
  gate honours `ADMIN_EMAILS` (non-admin → 403, admin → 200).
- `jobs.picked_at` added in Phase 5 for the queue-impact metric.

### Auth: our own Dex, not NetBird's (2026-09-09, revised)

- The spec assumed a reusable OIDC IdP behind the NetBird dashboard. Reality:
  this NetBird install is the **combined `netbird-server`** image, whose
  embedded Dex (`/oauth2` issuer) only registers the dashboard + CLI clients
  and exposes **no config hook for a third client**
  ([netbirdio/netbird#5335](https://github.com/netbirdio/netbird/issues/5335)).
  So Llamacracy can't ride NetBird's IdP.
- Decision: run **our own Dex** (`deploy/dex/`, Docker, `ghcr.io/dexidp/dex`)
  on myhost, published on `wt0` only. Issuer
  `http://myhost.netbird.selfhosted:5556`. Users are a `staticPasswords`
  list in `deploy/dex/config.yaml` (gitignored) — email + bcrypt, ~5 people,
  no external dependency. Whole chain (browser → Dex → oauth2-proxy → app)
  stays inside WireGuard; nothing added to the public internet.
- Issuer + `redirect_url` pinned to the NetBird **FQDN**, which is stable
  across reconnects; only the Dex container's port binding is IP-literal and
  `install.sh` rewrites it each run. **Users must hit the FQDN**, not the raw
  `100.x` — a bare-IP visit gets a cookie-host mismatch and loops.
- `scope` gains `offline_access` so oauth2-proxy gets a refresh token for its
  1 h `cookie_refresh`.
- Rejected: Pocket-ID on the public Traefik (nice UI, but a public login page +
  a DNS record, and passkeys need HTTPS); GitHub as the provider (least infra,
  but an external auth dependency and everyone needs a GitHub account).

- **Owner still needs to:** `cp deploy/dex/config.yaml.example config.yaml`,
  set the client `secret:` + a `staticPasswords` hash per user
  (`deploy/dex/gen-hash.sh`), `docker compose up -d`, then `./deploy/install.sh`.
  Fill the rate values in `.env`.

---

## Phase 3 — metering (2026-09-09)

- **`credits = occupancy_seconds - load_seconds * (1 - LOAD_TIME_MULTIPLIER)`**
  — full exclusive box-time held, with the cold-start portion discounted to
  half rate. Always from our own wall-clock timers, never token-derived.
  `state in (error, limit_exceeded)` → 0. Cancelled → the seconds actually
  consumed. Fast lane → 0 (not implemented; everything is `exclusive`).
- **`cost_usd = credits * watts * rate * MARKUP`** (spec's formula), watts =
  measured mean GPU draw (nvidia-smi sampler in the queue worker) +
  `NON_GPU_LOAD_WATTS`; falls back to the bench's per-model GPU mean, then
  180 W, if nvidia-smi is unavailable. `rate` from the TOU table by the local
  hour the job ran. `rate_used` + `cost_usd` frozen on the row at finalisation
  — historical cost is never recomputed.
- **Sessions** open on the user's first request (enqueue), fixed 5 h, never
  extended by activity; the next request after expiry opens a fresh one.
- **Limits checked at enqueue**, reject only if *already* at/over a cap
  (overshoot allowed; bounded by `MAX_TOKENS_PER_REQUEST`). 429 carries
  `{limit, used, cap, reset_at}`. Weekly `reset_at` = the Nth-oldest in-window
  job's `finished_at + 7d` (the moment the rolling sum drops back under cap).
  Per-user overrides on both caps.
- **`/api/usage`** returns session/weekly used+cap+pct+reset, 30-day daily
  series, per-model breakdown, and `estimated_fraction` (share of credits on
  rows where the token count was estimated — should stay near 0).
- **28 tests** (`tests/test_metering.py` + `test_queue.py`): credit formula
  incl. load-clamp and lane, cost + TOU-by-hour, session window boundaries
  and no-extension, expired→fresh, admit-under-cap / deny-at-cap, overshoot
  recorded not truncated, rolling-weekly window edge (7d ± 60s), weekly
  reset timestamp, per-user overrides, 75% warn flag.

---

## Phase 2 — backend (2026-09-09)

- `llamacracy/` package on FastAPI + a single SQLite connection (WAL, one
  asyncio lock — writes are tiny and the queue is serial anyway).
- **queue.py**: one `_worker` task drains a `list[Job]`; `_run` streams from
  llama-swap, detects cold start via `/running`, measures load vs generation
  (load = wall-to-first-token minus llama.cpp's `prompt_ms`), samples GPU
  watts during generation, emits SSE token/reasoning/done events, and on
  cancel `break`s the stream (the httpx context manager closes the upstream
  connection — verified the box doesn't wedge).
- Identity: trusts `X-Forwarded-*`, keys on `sub`, 503 + loud log if headers
  absent and `DEV_MODE` unset.
- Timestamps are epoch REAL throughout, so the rolling-weekly query is a plain
  indexed `SUM ... WHERE finished_at >= ?`.

---

## Phase 1 — inference layer (2026-09-09)

- **llama-swap v255** installed at `~/.local/bin/llama-swap`. Chosen over
  `llama-server` router mode: `groups` for co-residency control, explicit
  `/running` + unload endpoints the queue needs, and `filters.setParams` for
  clean server-side request shaping.
- **`bench/gen_llamaswap_config.py`** generates both `config/llama-swap.yaml`
  (llama-swap schema only) and `config/models.json` (the app's registry: tier,
  blurb, sampling defaults, and the measured load/throughput/VRAM seeds).
  Re-run whenever `bench-results.json` changes.
- **llama-swap fork/execs directly** — no shell, no `~` expansion. The
  generator writes absolute paths (`/home/you/.local/bin/llama-server`).
- **Reasoning off**: `--reasoning-budget 0` does NOT stop FamilyA (or
  moe-26b QAT, or FamilyC) emitting a full `<think>` block — verified 102
  completion tokens for a one-word answer. The fix is
  `chat_template_kwargs: {enable_thinking: false}`, injected per-request via
  llama-swap `filters.setParams`. Verified per model (102 → 4 tokens).
  FamilyB **4B does not think**; FamilyB **26B QAT does**.
- **FamilyC** runs at `--n-cpu-moe 30` (bench's 28 left only ~0.8 GB VRAM free).
- **FIM model** is `unlisted` in llama-swap (absent from `/v1/models`) and
  `kind=fim` / `in_picker=false` in the registry — two independent guards
  against it being chatted with.
- **Verified end-to-end through llama-swap** (load → stream → unload → swap):
  all 7 models. Streaming with `stream_options:{include_usage:true}` returns a
  final `usage` + `timings` chunk — the metering hook. Heavyweight RAM under
  load: moe-26b swap→5.5 GB, FamilyC→5.2 GB, FamilyA-35B→5.0 GB with
  ~11 GB still available. All usable with the desktop running.
- **App-side unload TTL** (`IDLE_TTL_MINUTES`, default 15) is authoritative;
  llama-swap `ttl: 1200` is only a backstop.

### IdP identified

OIDC discovery: **`https://netbird.21stgalleryportal.uk/oauth2/.well-known/openid-configuration`**

```
issuer:                        https://netbird.21stgalleryportal.uk/oauth2
authorization_endpoint:        .../oauth2/auth
token_endpoint:                .../oauth2/token
device_authorization_endpoint: .../oauth2/device/code
jwks_uri:                      .../oauth2/keys
userinfo_endpoint:             .../oauth2/userinfo
scopes:      openid email profile groups offline_access
PKCE:        S256
claims:      sub, email, email_verified, preferred_username, name, locale
```

Looks like **Pocket ID** (or possibly Dex) behind the NetBird dashboard —
vendor doesn't matter, it's standard OIDC. Auth-code + PKCE, `sub` claim
present (billing keys on `sub`). **Owner action for Phase 6:** create a new
OIDC client for Llamacracy in that IdP's admin UI, redirect URI
`https://<app-host>/oauth2/callback`, and drop client id/secret into `.env`.

---

## Phase 0 — owner answers + follow-up recon (2026-09-09)

- **Users:** owner + 3–4 friends (4–5 total). Small, casual. Owner still uses
  the box himself sometimes.
- **Session limit:** **SESSION_CREDIT_LIMIT = 3600** (60 min of continuous 35B
  generation) — owner picked the tighter option so a heavy friend frees the box
  sooner. **WEEKLY_CREDIT_LIMIT = 12000** (≈3.3× session; a casual user won't
  reach it).
- **Billing rate:** **standard** the utility the TOU plan + the CCA rates (any household discount is not passed through). Summer marginal: On-Peak ~$0.665,
  Off-Peak ~$0.456, Super-Off-Peak ~$0.374 per kWh.
- **Reasoning:** owner chose the "token / energy saver" default → **reasoning
  disabled by default on every model** (`--reasoning-budget 0` for FamilyA /
  FamilyA; equivalent for FamilyC where supported). Keeps per-answer cost
  predictable.
- **NetBird:** box is joined. `wt0` = **100.x.y.z/16**. Management
  `https://netbird.21stgalleryportal.uk:443/`, NetBird 0.78.1, FQDN
  `myhost.netbird.selfhosted`. oauth2-proxy will bind `100.x.y.z`.
- **IdP:** NetBird 0.78 self-hosted *requires* an OIDC IdP, so one exists
  behind that dashboard — but it is not at the dashboard root and not on an
  `auth.` / `id.` / `zitadel.` subdomain. Identity blocked until the owner
  pastes the client's IdP config (`sudo cat /var/lib/netbird/default.json`,
  filtered). Llamacracy will register its own client in that same IdP.
- **Wattage:** no wall meter, and this box exposes **no whole-system power
  sensor** — `intel-rapl` energy counters are empty, `k10temp` is temperature
  only, `amd_energy` not loaded. `nvidia-smi` GPU `power.draw` is the only real
  number. **Refinement to the cost model:** record measured mean `gpu_watts`
  per job and compute `system_watts = gpu_watts + NON_GPU_LOAD_WATTS`
  (default 110 W: Ryzen 3600 + board + RAM + NVMe + fans + PSU loss under
  load), instead of one flat `LOAD_WATTS`. MoE jobs (GPU ~140 W) then price
  below dense jobs (GPU ~230 W), both from live data.

### the utility rate (from the a recent statement)

Rate **the TOU plan, climate zone**, household is on a discount program, generation
via **the local generation provider** CCA ("the TOU plan, 2022 vintage"). *(Account
holder PII is deliberately NOT stored in this repo — only the rate structure.)*

TOU periods (from the statement):

| Period | Weekday | Weekend / holiday |
|---|---|---|
| On-Peak | 16:00–21:00 | 16:00–21:00 |
| Super Off-Peak | 00:00–06:00, 10:00–14:00 | 00:00–14:00 |
| Off-Peak | all other hours | all other hours |

Summer (Jun 1 – Oct 31) marginal $/kWh, built from delivery + CCA generation +
PCIA 2022 + surcharges:

| Period | standard | discounted (≈0.56×, empirical from the bill) |
|---|---|---|
| On-Peak | ~$0.665 | ~$0.37 |
| Off-Peak | ~$0.456 | ~$0.25 |
| Super Off-Peak | ~$0.374 | ~$0.21 |

- the utility delivery is flat **$0.32948/kWh** (not TOU-differentiated on this rate);
  all TOU variation is in the CCA generation ($0.30138 / $0.09194 / $0.01000
  on/off/super summer). PCIA 2022 $0.03005/kWh flat.
- Bill cross-check: $299.46 for 1,059 kWh (ex. one-time climate credit) =
  **$0.283/kWh** all-in discounted blended.
- **Decided:** bill at **standard** rates (above). The discounted column is kept
  for reference only.
- **Open:** winter (Nov 1 – May 31) generation rates not in this statement;
  seed with summer (slightly conservative) and update from the next bill.

---

## Phase 0 — benchmark results (2026-09-09)

llama.cpp `2d8d612e4`, `GGML_CUDA_FORCE_MMQ=ON`. All runs `-np 1` (strict FIFO,
one slot). VRAM baseline 1193 MiB (desktop). RAM ~24.4 GiB available with the
owner's normal apps up. Full data: `bench/bench-results.json`.

| Model | serve ctx | n_cpu_moe | KV | fa | cold load | prompt t/s | gen t/s | VRAM used | VRAM free | RAM + | GPU W (gen) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Coder 1.5B (FIM) | 8192 | – | f16 | on | 1.5 s | 3784 | **110** | 2.0 GB | 8.0 GB | 0.3 GB | 194 |
| FamilyA 4B (reasoning off) | 32768 | – | q8_0 | on | 2.8 s | 1269 | **44** | 5.2 GB | 4.8 GB | 1.0 GB | 209 |
| FamilyA 9B Q6_K | 32768 | – | q8_0 | on | 3.9 s | 705 | **30** | 7.1 GB | 2.8 GB | 1.0 GB | 262 |
| FamilyB 4B (alt) | 32768 | – | f16 | on | 4.5 s | 1140 | **40** | 5.6 GB | 4.3 GB | 0.3 GB | 208 |
| FamilyB 26B QAT (MoE) | 16384 | 18 | f16 | on | 12.2 s | 417 | **34** | 7.6 GB | 2.4 GB | 8.0 GB | 144 |
| FamilyC Flash (MoE) | 16384 | 28 | f16 | on | 13.6 s | 288 | **29** | 9.1 GB | **0.8 GB** | 9.5 GB | 138 |
| FamilyA 35B (MoE) | 16384 | 28 | f16 | on | 25.4 s | 300 | **32** | 8.5 GB | 1.5 GB | 12.4 GB | 139 |

Findings vs the owner's estimates:

- **Generation is much faster than the spec's "8–15 tok/s" guess** — all three
  MoE heavyweights land at 29–34 tok/s (MoE = only 3–4 B active params at
  Q4). moe-26b QAT (34) actually beats the dense FamilyA 9B (30).
- **`-fa on` is mandatory, not optional, on this stack:** quantized (q8_0) KV
  requires flash attention in llama.cpp (FamilyA 4B/9B fail to start with
  `-fa off`), and for FamilyC `-fa off` doesn't fit in VRAM. `-fa on` is also
  faster for prompt processing on every model tested. So we don't expose a
  toggle; `-fa on` everywhere.
- **FamilyC at n_cpu_moe=28 / ctx 16384 leaves only ~0.8 GB VRAM free** — too
  little for compute-buffer growth. Phase 1 should run it at **n_cpu_moe=30**
  (the owner's own "coding" variant) or ctx 12288 for headroom.
- **FamilyA-35B works with the desktop running** (~32 tok/s) but pushed zram
  swap from 1.5 GB to 3.6 GB during load. Fine solo; risky if other big apps
  are open. `--no-mmap` + n_cpu_moe=28 puts ~12 GB in RAM, ~8.5 GB in VRAM.
- Cold loads (model-file page cache evicted first): 1.5–4.5 s for the small
  models, 12–25 s for the heavyweights. These seed the queue's load-time
  estimates.
- Serving contexts chosen: 32768 for the small models (room to spare),
  **16384 for the MoE heavyweights** (the owner runs `-c 32768` but with
  llama-server's default 4 slots that is ~8 k effective per request; at `-np 1`
  we give a real 16 k and keep KV off the tight VRAM budget).

### Proposed credit limits (owner to confirm)

`1 credit = 1 second of exclusive box time.`

**SESSION_CREDIT_LIMIT = 5400** (5-hour window)
- Spec target: a heavy user on the 35B hits the cap in ~90 min of continuous
  generation. FamilyA-35B measured at 31.8 tok/s → 90 min × 60 = **5400 s**.
- = ~171,700 generated tokens ≈ 84 max-length (2048-tok) responses.
- Model-load surcharge is rounding noise (10 cold 35B loads × 25.4 s × 0.5 =
  127 credits).
- A casual user on the 9B (30 tok/s) would need ~270 six-hundred-token replies
  in one 5-hour window to reach it — it only ever bites a heavy 35B user.

**WEEKLY_CREDIT_LIMIT = 12000** (rolling 7-day) — *needs headcount to finalise*
- ≈ 2.2 × the session cap: a heavy user gets ~2 big sessions a week then waits
  for the rolling window to clear.
- = ~5.7 h of 35B generation, or ~100 max-length 35B answers, per week.
- For a casual user that is 10+ evenings of chat — effectively unlimited.
- Revisit once real usage data exists; it is a one-line config change.

**Cost model** — *needs the real the utility rate*
- Measured GPU-only draw (nvidia-smi): idle 20–62 W; MoE generation ~138–144 W
  (GPU waits on CPU experts); dense generation 208–262 W.
- Whole-system estimate (GPU + Ryzen 3600 + board/RAM/NVMe/fans): idle ~95 W,
  generation ~230–350 W depending on model, load ~160 W.
- Proposed defaults: `IDLE_WATTS=95`, `LOAD_WATTS=300` (blended generation).
- `cost_usd = credits × (LOAD_WATTS/1000) × ELECTRICITY_RATE × MARKUP`
- Worked example at a **placeholder** $0.45/kWh: a full 5400-credit session =
  1.5 h × 0.30 kW × $0.45 = **$0.20**. The weekly cap ≈ **$0.45/week** for the
  single heaviest user. the utility on-peak (~$0.80/kWh) roughly doubles that.
- Even the heaviest friend costs well under $1/week in electricity; set
  `MARKUP` to 2–3× if invoices should feel non-trivial.

---

## Phase 0 — environment recon (2026-09-09)

### Confirmed from the box

- **`~/models` disk:** NVMe (Crucial T500 2 TB, PCIe 4.0), btrfs on `/home`,
  mounted `noatime,compress=zstd:3,ssd,discard=async`, ~1.1 TB free. Cold loads
  are fast; UI estimates and load-time billing assume NVMe.
- **llama.cpp build:** was `~/llama.cpp` build 10724 (`2d8d612e4`, 2026-08-31)
  with `GGML_CUDA=ON`, `CMAKE_CUDA_ARCHITECTURES=61`, but
  `GGML_CUDA_FORCE_MMQ=OFF`. **Rebuilt** with `-DGGML_CUDA_FORCE_MMQ=ON`
  (owner approved). CUDA 12.9 toolkit, driver 580, `nvcc` present. Clean build.
- **Usable VRAM:** 11264 MiB total, but the Wayland desktop (Xorg + kwin) holds
  ~1.17 GB, so llama-server sees ~9.8 GB free. Ryzen 3600 has no iGPU, so the
  display can't move off the 1080 Ti short of running headless. All fit
  calculations budget ~9.3–9.8 GB.
- **New arch strings:** FamilyA = `familya`, FamilyA-35B = `moemodel`,
  moe-30b = `deepseek2`, FamilyB = `familyb`. `familya` loads and runs on the
  rebuilt binary (validated with FamilyA 4B). Others verified in the full sweep.
- **`-np 1` matters:** llama-server defaults to 4 parallel slots and splits KV
  across them. Under strict FIFO we run one slot, so both the benchmark and the
  llama-swap config pass `-np 1` — KV cache = 1 × ctx.
- **NetBird not joined:** `netbird status` = NeedsLogin, no `wt0` interface. The
  IdP (Dex vs Zitadel) and the `wt0` address can't be discovered until the box
  joins the owner's NetBird instance. Blocked pending owner input.
- **Not installed yet:** `llama-swap`, `oauth2-proxy`. `llama-bench` /
  `llama-cli` exist in the build dir (Phase 0 drives `llama-server` directly).
- **Python:** system is 3.14.7 (no 3.11/3.12). Plan: `venv` + pinned deps,
  watch for any C-extension without 3.14 wheels.
- **systemd linger** is off — Phase 6 needs `loginctl enable-linger j4mes`.
- **Docker** already runs a searxng stack. Our app stays Docker-free (spec
  non-goal is about our app). `~/models/ds.json` is an unrelated MCP config —
  ignored.

### Inventory deltas vs SPEC

| Model | Spec | Reality |
|---|---|---|
| FamilyA 9B | Q6_K ~7.5 GB | Q6_K (7.56 GB) present **plus** a redundant 18.4 GB F16 GGUF in the same dir. Owner is cleaning up `~/models` and will give the final layout before Phase 1. |
| FamilyB 4B | "FamilyB 4B" | On disk it is `alt-4b` (8.13 GB), an alt finetune. **Decision:** expose it, labelled clearly in the picker as a finetune. Key: `alt-4b`. |
| FamilyA 35B | "~20 GB" | 22.1 GB (`Q4_K_M`). |
| moe-26b, FamilyC, FamilyA-35B | — | Ship `mmproj` vision projectors; FamilyA 4B/9B look like VL variants. **Decision:** serve all models text-only, ignore `mmproj` (matches the "no image input" non-goal). |
| Heavyweights vs RAM | 32 GB | 31 GiB total, ~24 GiB available with the desktop up. FamilyC (18 GB) and FamilyA-35B (22 GB) as CPU-offload MoE are very tight. **Decision:** benchmark all three in Phase 0; drop or mark headless-only any that swap-thrash. Owner reviews the numbers. |

### Still needed from the owner

1. the utility rate: flat $/kWh, plus the TOU table + which schedule (the TOU plan /
   other plans) if time-of-use costing is wanted.
2. OIDC issuer URL, client ID, client secret (into gitignored `.env`).
3. NetBird management/dashboard URL, and `netbird up` on the box, so the IdP
   and `wt0` address can be read.
4. Rough headcount of friends + expected usage, to size `WEEKLY_CREDIT_LIMIT`.
5. Whether a wall wattage meter is available (else use spec defaults
   ~100 W idle / ~350 W load).
