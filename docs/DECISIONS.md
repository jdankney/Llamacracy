# Decisions & deviations

Running log of choices made against [SPEC.md](SPEC.md), with the reasoning.
Newest first.

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
