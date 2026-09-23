#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the inference-layer config from your model inventory.

Inputs
  bench/inventory.json       -- YOUR models: paths, serving config, tiers, blurbs
                                (gitignored; start from inventory.example.json,
                                schema in bench/README.md)
  bench/bench-results.json   -- measurements from phase0_bench.py (optional per
                                model: an entry with a `seed` block in the
                                inventory can be served without a benchmark)

Outputs
  config/llama-swap.yaml     -- one llama-server command per model, TTL backstop,
                                swap groups
  config/models.json         -- the app's registry: display name, kind (chat/fim),
                                tier, sampling defaults, and the cold-load /
                                throughput / VRAM seeds the queue estimates from

Serving choices baked in:
  - `-np 1` so KV cache = 1 x context (strict FIFO, one inference at a time)
  - `thinking: true` (older name: `nothink: true`) marks a model that can
    reason. No llama-swap filter is emitted for it: the app sends
    `chat_template_kwargs.enable_thinking` on every request, off unless the
    user flips the Think toggle (a setParams filter would override that)
  - kind=fim models are `unlisted` in llama-swap and hidden from the picker
  - a model's `vision` block adds a second, unlisted `<key>-vision` entry:
    same weights + --mmproj, dispatched to only when a message carries an image

    python3 bench/gen_llamaswap_config.py
    python3 bench/gen_llamaswap_config.py --inventory other.json --out /tmp/cfg
    llama-swap -config config/llama-swap.yaml -validate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = REPO / "bench" / "inventory.json"
DEFAULT_BENCH = REPO / "bench" / "bench-results.json"
DEFAULT_OUT = REPO / "config"

SEED_KEYS = ("cold_load_s", "tg_tok_s", "pp_tok_s", "vram_used_mib")


def expand(p: str, models_dir: Path) -> str:
    """Absolute path: `~` expanded, relative paths resolved under models_dir.
    llama-swap fork/execs llama-server directly (no shell), so nothing here may
    rely on later expansion."""
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else str(models_dir / p)


def load_inventory(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"inventory not found: {path}\n"
                 f"  cp {path.parent / 'inventory.example.json'} {path}   # then edit")
    inv = json.loads(path.read_text())
    inv.setdefault("llama_server", "~/.local/bin/llama-server")
    inv.setdefault("models_dir", "~/models")
    inv.setdefault("threads", 6)
    inv.setdefault("ttl_backstop_s", 1200)
    inv.setdefault("health_timeout_s", 480)
    if not inv.get("models"):
        sys.exit(f"{path}: no models defined")
    return inv


def recommended(key: str, m: dict, bench: dict) -> dict | None:
    """The (ctx, kv_type, n_cpu_moe, fa) to serve at plus the measured seeds.
    Bench results win; otherwise the inventory's `serve` + `seed` blocks."""
    b = bench.get("models", {}).get(key, {})
    if b.get("recommended"):
        return b["recommended"]
    serve = m.get("serve", {})
    seed = m.get("seed")
    if not seed or any(k not in seed for k in SEED_KEYS):
        return None
    return {
        "ctx": serve.get("ctx", 8192),
        "kv_type": serve.get("kv_type", "f16"),
        "n_cpu_moe": serve.get("n_cpu_moe"),
        "fa": serve.get("fa", "on"),
        **seed,
    }


def build_cmd(path: str, rec: dict, serve: dict, threads: int, *,
              mmproj: str | None = None) -> list[str]:
    cmd = ["${server}", "-m", path,
           "--host", "127.0.0.1", "--port", "${PORT}",
           "-c", str(rec["ctx"]), "-ngl", "99", "-fa", rec.get("fa", "on"),
           "-t", str(threads), "-np", "1", "--no-webui", "--metrics"]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    if rec.get("n_cpu_moe") is not None:
        cmd += ["--n-cpu-moe", str(rec["n_cpu_moe"])]
    if rec.get("kv_type", "f16") != "f16":
        cmd += ["-ctk", rec["kv_type"], "-ctv", rec["kv_type"]]
    if serve.get("no_mmap"):
        cmd += ["--no-mmap"]
    cmd += list(serve.get("args", []))
    return cmd


def emit_yaml_entry(y: list[str], key: str, argv: list[str], comment: str, *,
                    ttl: int, unlisted: bool = False) -> None:
    y.append(f'  "{key}":')
    y.append(f"    # {comment}")
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
    y.append(f"    ttl: {ttl}")
    if unlisted:
        y.append("    unlisted: true")
    y.append("")


def registry_entry(m: dict, rec: dict, path: str, *, display: str, blurb: str,
                   in_picker: bool, vision_key: str | None) -> dict:
    is_fim = m.get("kind", "chat") == "fim"
    return {
        "display": display,
        "kind": m.get("kind", "chat"),
        "path": path,
        "tier": m.get("tier"),
        "in_picker": in_picker,
        "blurb": blurb,
        "reasoning": m.get("reasoning", None if is_fim else "off"),
        # can reason on request (the Think toggle). `nothink` is the older
        # name for the same fact: "reasons, but keep it off by default".
        "thinking": bool(m.get("thinking", m.get("nothink", False))) and not is_fim,
        "sampling": m.get("sampling", {}),
        "ctx": rec["ctx"],
        "seed_cold_load_s": rec["cold_load_s"],
        "seed_tg_tok_s": rec["tg_tok_s"],
        "seed_pp_tok_s": rec["pp_tok_s"],
        "vram_used_mib": rec["vram_used_mib"],
        "gpu_gen_watts": rec.get("gpu_gen_w_mean"),   # cost-model fallback if nvidia-smi is unavailable
        "max_tokens_default": m.get("max_tokens", 2048),
        "vision_key": vision_key,
    }


def generate(inv: dict, bench: dict) -> tuple[str, dict]:
    models_dir = Path(os.path.expanduser(inv["models_dir"]))
    server = os.path.expanduser(inv["llama_server"])
    threads, ttl = inv["threads"], inv["ttl_backstop_s"]
    built: set[str] = set()
    groups: dict[str, list[str]] = {}

    source = "bench/inventory.json"
    if bench:
        source += (f" + bench-results.json {bench.get('generated_at', '?')} "
                   f"(llama.cpp {bench.get('llama_cpp_commit', '?')})")
    y: list[str] = [
        "# GENERATED by bench/gen_llamaswap_config.py -- do not hand-edit.",
        f"# from {source}",
        "",
        f"healthCheckTimeout: {inv['health_timeout_s']}",
        "logLevel: info",
        "startPort: 10800",
        "",
        "macros:",
        f'  server: "{server}"',
        "",
        "models:",
    ]
    registry: dict = {
        "generated_at": bench.get("generated_at", ""),
        "llama_cpp_commit": bench.get("llama_cpp_commit", ""),
        "models": {},
    }

    vision_specs: list[tuple[str, dict, dict, str]] = []   # (vkey, model, rec, path)

    for key, m in inv["models"].items():
        rec = recommended(key, m, bench)
        if rec is None:
            print(f"  ! {key}: no benchmark result and no `seed` block -- skipped "
                  f"(run phase0_bench.py --only {key}, or add seed numbers)")
            y.append(f"  # {key}: SKIPPED (not benchmarked, no seed)")
            continue
        path = expand(m["path"], models_dir)
        serve = dict(m.get("serve", {}))
        serve["args"] = [expand(a, models_dir) if a.startswith("~") else a
                         for a in serve.get("args", [])]
        is_fim = m.get("kind", "chat") == "fim"
        vision = m.get("vision")
        vkey = f"{key}-vision" if vision else None

        emit_yaml_entry(
            y, key, build_cmd(path, rec, serve, threads),
            f'{m["display"]} | {rec["tg_tok_s"]} tok/s | '
            f'{rec["cold_load_s"]}s cold | {rec["vram_used_mib"]} MiB VRAM',
            ttl=ttl, unlisted=is_fim)
        built.add(key)
        groups.setdefault(m.get("group", "default"), []).append(key)
        registry["models"][key] = registry_entry(
            m, rec, path, display=m["display"], blurb=m.get("blurb", ""),
            in_picker=m.get("in_picker", not is_fim), vision_key=vkey)
        if vision:
            vision_specs.append((vkey, m, rec, path))

    # vision variants: same weights + serving config as the base model, plus
    # --mmproj. unlisted (like FIM) -- never in the picker, only dispatched to
    # internally when a chat turn carries an image.
    for vkey, m, rec, path in vision_specs:
        key = vkey[: -len("-vision")]
        v = m["vision"]
        mmproj = expand(v["mmproj"], models_dir)
        # A vision variant is the same llama-server invocation with --mmproj
        # bolted on, so it inherits the base model's ctx/kv/offload AND its
        # CLI args -- a custom --chat-template-file matters just as much to
        # the vision entry. `vision.args` overrides outright when a model
        # genuinely needs different flags with the projector loaded.
        serve = dict(m.get("serve", {}))
        serve["args"] = [expand(a, models_dir) if a.startswith("~") else a
                         for a in v.get("args", serve.get("args", []))]
        display = v.get("display", f'{m["display"]} -- vision')
        emit_yaml_entry(
            y, vkey, build_cmd(path, rec, serve, threads, mmproj=mmproj),
            f"{display} | mmproj: {Path(mmproj).name}",
            ttl=ttl, unlisted=True)
        built.add(vkey)
        groups.setdefault(m.get("group", "default"), []).append(vkey)
        registry["models"][vkey] = registry_entry(
            m, rec, path, display=display,
            blurb=v.get("blurb", "Same model, with image understanding. "
                                 "Loaded only when a message includes an image."),
            in_picker=False, vision_key=None)

    y.append("groups:")
    y.append("  # SMALL_MODEL_FAST_LANE is off -> the app serialises everything and one")
    y.append("  # model is loaded at a time. Groups make that explicit for llama-swap.")
    for gname, members in groups.items():
        y.append(f'  "{gname}":')
        y.append("    swap: true")
        y.append("    exclusive: true")
        y.append(f"    members: [{', '.join(f'\"{k}\"' for k in members)}]")

    return "\n".join(y) + "\n", registry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    ap.add_argument("--bench", type=Path, default=DEFAULT_BENCH,
                    help="bench-results.json (missing file = seeds only)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="directory for llama-swap.yaml + models.json")
    args = ap.parse_args()

    inv = load_inventory(args.inventory)
    bench = json.loads(args.bench.read_text()) if args.bench.exists() else {}
    if not bench:
        print(f"  (no {args.bench.name}; serving from inventory seeds)")

    yaml_text, registry = generate(inv, bench)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "llama-swap.yaml").write_text(yaml_text)
    (args.out / "models.json").write_text(json.dumps(registry, indent=2) + "\n")
    print(f"wrote {args.out / 'llama-swap.yaml'}")
    print(f"wrote {args.out / 'models.json'}  ({len(registry['models'])} models)")


if __name__ == "__main__":
    main()
