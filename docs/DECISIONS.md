# Decisions & deviations

Running log of choices made against [SPEC.md](SPEC.md), with the reasoning.
Newest first.

## Phase 0 — owner answers + follow-up recon (2026-09-09)

- **Users:** owner + 3–4 friends (4–5 total). Small, casual. Owner still uses
  the box himself sometimes.
- **Session limit:** owner asked to lower it "a bit" → **SESSION_CREDIT_LIMIT =
  4500** (75 min of continuous 35B generation). WEEKLY stays **12000** (≈2.7×
  session; a casual user won't reach it).
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
- **Open:** bill friends at discounted (owner's true cost) or standard standard
  rates (a discount is household-specific; standard is the defensible
  "cost to run it" and survives a discount-status change). Recommend **standard**.
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
