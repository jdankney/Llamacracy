#!/usr/bin/env python3
"""Generate the inference-layer config from bench/bench-results.json (Phase 1).

Emits two files:

  config/llama-swap.yaml  -- llama-swap schema only (one llama-server command
                             per model, TTL backstop, groups)
  config/models.json      -- the app's model registry: display name, kind
                             (chat/fim), tier/group, and the measured
                             cold-load / throughput / VRAM seeds the queue
                             uses for its estimates

The benchmark already found a VRAM-safe config for every model. Serving choices
layered on top:
  - reasoning OFF by default everywhere (owner's "token/energy saver" choice)
  - `-np 1` so KV cache = 1 x context (strict FIFO, one inference at a time)
  - FamilyC nudged to n_cpu_moe=30 (bench left only ~0.8 GB VRAM free at 28)
  - the FIM model is `unlisted` in llama-swap and kind=fim in the registry, so
    it can never surface as a chat model

    python3 bench/gen_llamaswap_config.py
    ~/.local/bin/llama-swap -config config/llama-swap.yaml -validate
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "bench" / "bench-results.json"
OUT_YAML = REPO / "config" / "llama-swap.yaml"
OUT_REGISTRY = REPO / "config" / "models.json"

HOME = str(Path.home())     # llama-swap fork/execs directly -- no ~ or shell expansion
LLAMA_SERVER = f"{HOME}/.local/bin/llama-server"
THREADS = 6
TTL_BACKSTOP_S = 1200        # app enforces the real IDLE_TTL_MINUTES; this is a safety net
HEALTH_TIMEOUT_S = 480       # cold --no-mmap load of the 22 GB MoE

# serving overrides on top of bench-results "recommended"
OVERRIDES: dict[str, dict] = {
    "moe-30b": {"n_cpu_moe": 30},
}

# Models whose chat template has a thinking toggle. `--reasoning-budget 0` does
# NOT stop FamilyA emitting a full think block (verified: 102 completion
# tokens -> 4 once thinking is off). We disable it via llama-swap's
# `filters.setParams`, which injects `chat_template_kwargs` into every request
# body server-side -- cleaner than a CLI arg with JSON quoting.
REASONING_MODELS = {"fast-4b", "daily-9b", "moe-35b", "moe-30b"}

# human-facing metadata + sampling the app should apply as per-model defaults
REGISTRY_META: dict[str, dict] = {
    "coder-1.5b-fim": dict(
        tier="fim", picker=False,
        blurb="Inline code completion (FIM). Not a chat model."),
    "fast-4b": dict(
        tier="fast", picker=True, reasoning="off",
        sampling=dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0),
        blurb="Fastest chat model. Summaries, reformatting, quick questions."),
    "daily-9b": dict(
        tier="daily", picker=True, reasoning="off",
        sampling=dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0),
        blurb="Daily driver. Fully on GPU, ~30 tok/s."),
    "alt-4b": dict(
        tier="fast", picker=True, reasoning="none",
        sampling=dict(),
        blurb="FamilyB 4B — alt community finetune, not stock Gemma."),
    "moe-26b": dict(
        tier="heavy", picker=True, reasoning="none",
        sampling=dict(),
        blurb="MoE, experts on CPU. Strong quality, ~34 tok/s, ~12 s cold start."),
    "moe-30b": dict(
        tier="heavy", picker=True, reasoning="off",
        sampling=dict(temperature=1.0, top_p=0.95),
        blurb="MoE, accuracy-first general Q&A. ~29 tok/s, ~14 s cold start."),
    "moe-35b": dict(
        tier="heavy", picker=True, reasoning="off",
        sampling=dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0),
        blurb="Biggest model, long documents. ~32 tok/s, ~25 s cold start."),
}

EXTRA_ARGS: dict[str, list[str]] = {
    "fast-4b": ["--temp", "0.6", "--top-p", "0.95", "--top-k", "20",
                  "--min-p", "0", "-b", "2048", "-ub", "512"],
    "daily-9b": ["--temp", "0.6", "--top-p", "0.95", "--top-k", "20",
                  "--min-p", "0", "-b", "2048", "-ub", "512"],
    "moe-35b": ["--temp", "0.6", "--top-p", "0.95", "--top-k", "20",
                       "--min-p", "0", "-b", "2048", "-ub", "512"],
    "moe-30b": ["--temp", "1.0", "--top-p", "0.95"],
    "moe-26b": ["-b", "2048", "-ub", "512"],
    "alt-4b": [
        "--chat-template-file",
        f"{HOME}/models/alt-4b.chat_template.jinja"],
}

GROUPS = {
    "heavyweights": ["moe-26b", "moe-30b", "moe-35b"],
    "standard": ["fast-4b", "daily-9b", "alt-4b"],
    "fim": ["coder-1.5b-fim"],
}


def build_cmd(key: str, m: dict) -> list[str]:
    rec = m["recommended"]
    ov = OVERRIDES.get(key, {})
    n_cpu_moe = ov.get("n_cpu_moe", rec["n_cpu_moe"])
    is_fim = m["kind"] == "fim"

    cmd = ["${server}", "-m", m["path"],
           "--host", "127.0.0.1", "--port", "${PORT}",
           "-c", str(rec["ctx"]), "-ngl", "99", "-fa", rec["fa"],
           "-t", str(THREADS), "-np", "1", "--no-webui", "--metrics"]
    if n_cpu_moe is not None:
        cmd += ["--n-cpu-moe", str(n_cpu_moe)]
    if rec["kv_type"] != "f16":
        cmd += ["-ctk", rec["kv_type"], "-ctv", rec["kv_type"]]
    if m["size_gb"] > 12:
        cmd += ["--no-mmap"]
    cmd += EXTRA_ARGS.get(key, [])
    return cmd


def main() -> None:
    data = json.loads(BENCH.read_text())
    models = data["models"]

    y: list[str] = [
        "# GENERATED by bench/gen_llamaswap_config.py -- do not hand-edit.",
        f"# from bench-results.json {data['generated_at']} "
        f"(llama.cpp {data['llama_cpp_commit']}, force_mmq={data['force_mmq']})",
        "",
        f"healthCheckTimeout: {HEALTH_TIMEOUT_S}",
        "logLevel: info",
        "startPort: 10800",
        "",
        "macros:",
        f'  server: "{LLAMA_SERVER}"',
        "",
        "models:",
    ]
    registry: dict = {"generated_at": data["generated_at"],
                      "llama_cpp_commit": data["llama_cpp_commit"], "models": {}}

    for key, m in models.items():
        rec = m.get("recommended")
        if not rec:
            y.append(f"  # {key}: SKIPPED (no working config)")
            continue
        argv = build_cmd(key, m)
        meta = REGISTRY_META.get(key, {})
        is_fim = m["kind"] == "fim"

        y.append(f'  "{key}":')
        y.append(f'    # {m["display"]} | {rec["tg_tok_s"]} tok/s | '
                 f'{rec["cold_load_s"]}s cold | {rec["vram_used_mib"]} MiB VRAM')
        # cmd as a literal block: one flag (+ its value) per line for readability
        y.append("    cmd: |")
        cur: list[str] = []
        for tok in argv:
            if tok.startswith("-") and cur:
                y.append("      " + " ".join(cur))
                cur = [tok]
            else:
                cur.append(tok)
        if cur:
            y.append("      " + " ".join(cur))
        y.append('    proxy: "http://127.0.0.1:${PORT}"')
        y.append('    checkEndpoint: "/health"')
        y.append(f"    ttl: {TTL_BACKSTOP_S}")
        if key in REASONING_MODELS:
            y.append("    filters:")
            y.append("      setParams:")
            y.append("        chat_template_kwargs:")
            y.append("          enable_thinking: false")
        if is_fim:
            y.append("    unlisted: true")
        y.append("")

        registry["models"][key] = {
            "display": m["display"],
            "kind": m["kind"],
            "path": m["path"],
            "tier": meta.get("tier"),
            "in_picker": meta.get("picker", m["kind"] != "fim"),
            "blurb": meta.get("blurb", ""),
            "reasoning": meta.get("reasoning", "off" if m["kind"] != "fim" else None),
            "sampling": meta.get("sampling", {}),
            "ctx": rec["ctx"],
            "seed_cold_load_s": rec["cold_load_s"],
            "seed_tg_tok_s": rec["tg_tok_s"],
            "seed_pp_tok_s": rec["pp_tok_s"],
            "vram_used_mib": rec["vram_used_mib"],
            "max_tokens_default": 2048,
        }

    y.append("groups:")
    y.append("  # v1: SMALL_MODEL_FAST_LANE off -> app serialises everything, one")
    y.append("  # model loaded at a time. Groups make that explicit + ready the fast lane.")
    for gname, members in GROUPS.items():
        members = [k for k in members if models.get(k, {}).get("recommended")]
        if not members:
            continue
        y.append(f'  "{gname}":')
        y.append("    swap: true")
        y.append("    exclusive: true")
        y.append(f"    members: [{', '.join(f'\"{k}\"' for k in members)}]")

    OUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    OUT_YAML.write_text("\n".join(y) + "\n")
    OUT_REGISTRY.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"wrote {OUT_YAML}")
    print(f"wrote {OUT_REGISTRY}  ({len(registry['models'])} models)")


if __name__ == "__main__":
    main()
