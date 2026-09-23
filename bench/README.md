# bench/ — your models, measured

Everything machine-specific about models lives here, and none of it is
committed:

| File | Tracked? | What |
|---|---|---|
| `inventory.json` | no | **Your** models: GGUF paths, serving flags, tiers, blurbs. The one file you edit. |
| `inventory.example.json` | yes | Starting point. Copy it to `inventory.json`. |
| `bench-results.json` | no | Measurements written by `phase0_bench.py` (cold load, tok/s, VRAM, watts). |
| `raw/` | no | Per-run `llama-server` logs. |
| `phase0_bench.py` | yes | The benchmark. Drives a real `llama-server` per model, stdlib only. |
| `gen_llamaswap_config.py` | yes | Turns inventory + results into `config/llama-swap.yaml` and `config/models.json`. |
| `gguf_meta.py` | yes | Tiny GGUF header reader (layer count, MoE, context) used by the benchmark. |

The generated `config/` files are gitignored too. The app refuses to start
without `config/models.json` and tells you to run the generator.

## Workflow

```bash
cp bench/inventory.example.json bench/inventory.json      # once
$EDITOR bench/inventory.json                              # your models
python3 bench/phase0_bench.py --dry-run                   # shows what would run
python3 bench/phase0_bench.py                             # measures every model (resumable)
python3 bench/gen_llamaswap_config.py                     # writes config/
llama-swap -config config/llama-swap.yaml -validate
```

Benchmarking is optional per model: an entry with a `seed` block is served
from those numbers until you measure it. Measured results always win over
seeds.

## Inventory schema

Top level:

| Key | Default | Meaning |
|---|---|---|
| `llama_server` | `~/.local/bin/llama-server` | Absolute path is written into the llama-swap config (llama-swap execs it directly, no shell). |
| `llama_cpp_dir` | `~/llama.cpp` | Only used to record the llama.cpp commit in the results. |
| `models_dir` | `~/models` | Relative model paths resolve under this. |
| `threads` | `6` | `-t` for llama-server. |
| `vram_total_mib` | | Informational. |
| `ttl_backstop_s` | `1200` | llama-swap's own idle unload; the app's `IDLE_TTL_*` is the real one. |
| `health_timeout_s` | `480` | How long llama-swap waits for a cold `--no-mmap` load. |

Each entry under `models` (the key is the model id used everywhere: picker,
API, billing rows):

| Key | Meaning |
|---|---|
| `display` | Name shown in the UI. |
| `path` | GGUF file, relative to `models_dir` or absolute. `~` is fine. |
| `kind` | `chat` (default) or `fim`. `fim` models are hidden from the picker and unlisted in llama-swap. |
| `tier` | Free-form label shown next to the name: `fast`, `daily`, `heavy`, … The picker defaults to the first `daily` model. |
| `in_picker` | `false` hides a chat model without removing it. |
| `blurb` | One line under the composer when the model is selected. |
| `reasoning` | `"off"` (can think), `"none"` (can't), or `null`. A label only; `thinking` is what turns the Think toggle on. |
| `thinking` | `true` for a model that can reason and whose chat template takes `enable_thinking` (Qwen3 and later, among others; try one prompt with Think on to confirm). Thinking is off by default; users turn it on per message with the Think toggle, and the app sends `chat_template_kwargs.enable_thinking` on every request. `nothink: true` is the older name for the same thing, from when these models had thinking forced off. |
| `sampling` | Per-model request defaults the app sends (`temperature`, `top_p`, `top_k`, `min_p`). |
| `max_tokens` | Per-model reply cap in the web chat, default 2048 (also capped by `MAX_TOKENS_PER_REQUEST`). Think turns and `/v1` calls use `THINKING_MAX_TOKENS` and `API_MAX_TOKENS_PER_REQUEST` instead. |
| `serve` | How llama-server runs it: `ctx`, `kv_type` (`f16`/`q8_0`), `n_cpu_moe` (MoE expert offload, or omit), `no_mmap`, `fa` (`on`), `args` (extra CLI flags). |
| `bench` | Benchmark-only overrides: `args` (replaces `serve.args` for the benchmark run), `no_mmap`, `reasoning_budget`, and `ctx_ladder` for `--ctx-sweep`: a list of `[ctx, kv_type, n_cpu_moe]` rungs walked low to high. |
| `group` | llama-swap swap group. Anything, `standard` / `heavyweights` are just conventions. |
| `vision` | Adds a hidden `<key>-vision` variant: `mmproj` (path), optional `display`, `blurb`, `args`. It inherits everything in `serve`, including `args`, so a custom chat template carries over; set `vision.args` only to override that list. The app routes a message with an image to it. |
| `seed` | Numbers to serve from before benchmarking: `cold_load_s`, `tg_tok_s`, `pp_tok_s`, `vram_used_mib`, optional `gpu_gen_w_mean`. |
| `note` | Free text, kept in the results file. |

## Adding, changing, removing a model

- **Add**: drop the GGUF under `models_dir`, add an entry with a `seed`,
  regenerate, restart `llamacracy` (llama-swap runs with `-watch-config` and
  picks up the new config by itself). Benchmark it later with
  `phase0_bench.py --only <key>` and regenerate again.
- **Change** serving flags: edit `serve`, regenerate, restart `llamacracy`. If the change
  affects speed or VRAM, re-benchmark with `--only <key> --fresh`.
- **Remove**: delete the entry, regenerate, restart `llamacracy`. Its billing history
  stays in the database under the old key.
- **Hide** temporarily: `"in_picker": false`.

## Benchmark options

```
--only KEY        one model (repeatable)
--fresh           re-measure models that already have results
--no-fa-off       skip the flash-attention off/on comparison
--ctx-sweep       walk each model's ctx_ladder, keep the largest that fits
--headroom-mib N  VRAM to leave free before calling a config "doesn't fit" (default 400)
--inventory PATH  use a different inventory file
```

Cold-load timing evicts the model file from the page cache first, so numbers
reflect a real cold start from disk.
