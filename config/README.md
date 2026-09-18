# config/

Generated, machine-specific, gitignored:

- `llama-swap.yaml` — one `llama-server` command per model, swap groups.
- `models.json` — the app's model registry (names, tiers, sampling defaults,
  load/throughput/VRAM seeds).

Both are written by `python3 bench/gen_llamaswap_config.py` from your
`bench/inventory.json` (and `bench/bench-results.json` when you have
benchmarked). Never hand-edit them; edit the inventory and regenerate. See
[../bench/README.md](../bench/README.md).
