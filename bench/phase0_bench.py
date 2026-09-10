#!/usr/bin/env python3
"""Phase 0 -- measure reality.

For every GGUF we intend to serve, find a working llama-server config and
record what it costs and how fast it runs. Output feeds:
  - the llama-swap config (Phase 1): per-model -ngl / --n-cpu-moe / -fa / KV type
  - the UI cold-start estimates and load-time billing (the queue, Phase 2)
  - the credit-limit arithmetic (metering, Phase 3)

Stdlib only, so it runs on the system Python with nothing installed. Drives
the real `llama-server` binary over HTTP rather than llama-bench, because we
need cold load time and resident VRAM for the exact flag set we'll deploy.

Usage:
    python3 bench/phase0_bench.py                 # full sweep, resumable
    python3 bench/phase0_bench.py --only fast-4b
    python3 bench/phase0_bench.py --dry-run       # print the plan, run nothing
    python3 bench/phase0_bench.py --fresh         # ignore existing results

Results stream to bench/bench-results.json after every model so a heavyweight
that OOM-kills the box doesn't lose the earlier data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from gguf_meta import summarize as gguf_summarize

REPO = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.path.expanduser("~/models"))
LLAMA_SERVER = Path(os.path.expanduser("~/.local/bin/llama-server"))
LLAMA_CPP_DIR = Path(os.path.expanduser("~/llama.cpp"))
OUT_PATH = REPO / "bench" / "bench-results.json"
LOG_DIR = REPO / "bench" / "raw"

# VRAM budget. The 1080 Ti reports ~11162 MiB total; the desktop (Xorg +
# kwin_wayland) holds ~1.1 GB, so llama-server sees ~9.8 GB free. Leave a
# safety margin below that for compute-buffer growth at longer contexts.
VRAM_TOTAL_MIB = 11162
VRAM_HEADROOM_MIB = 450

# Benchmark shapes.
CHAT_CTX = 8192          # context we plan to serve chat models at
HEAVY_CTX = 4096         # MoE heavyweights: keep KV small
GEN_TOKENS = 256
BENCH_REPS = 3
HEALTH_TIMEOUT_S = 360
THREADS = 6             # Ryzen 5 3600 physical cores

# ~1000 tokens of filler so prompt-processing throughput is measurable.
_PARA = (
    "The quick brown fox jumps over the lazy dog while the committee debates "
    "the merits of a strictly first-in-first-out scheduling policy for a "
    "single shared accelerator. Each participant weighs latency against "
    "fairness, and the discussion returns repeatedly to the question of how "
    "to price exclusive occupancy of a scarce resource. "
)
BENCH_PROMPT = (_PARA * 12).strip()


@dataclass
class ModelSpec:
    key: str                 # short id used in configs and results
    display: str
    path: Path
    kind: str                # "chat" | "fim"
    note: str = ""


@dataclass
class RunResult:
    fa: str
    n_gpu_layers: int
    n_cpu_moe: int | None
    cache_type_k: str
    cache_type_v: str
    ctx: int
    ok: bool = False
    fits: bool = False
    error: str = ""
    cold_load_s: float | None = None
    warm_load_s: float | None = None
    vram_used_mib: int | None = None            # nvidia-smi delta over baseline
    vram_reported_mib: int | None = None        # summed CUDA0 buffers from server log
    pp_tok_s: float | None = None
    tg_tok_s: float | None = None
    gpu_idle_w: float | None = None
    gpu_gen_w_mean: float | None = None
    gpu_gen_w_max: float | None = None
    gpu_temp_max: float | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def nvidia_sample() -> dict:
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=memory.used,memory.total,power.draw,temperature.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[0]
    used, total, power, temp = (x.strip() for x in out.split(","))
    return {
        "mem_used_mib": int(float(used)),
        "mem_total_mib": int(float(total)),
        "power_w": float(power),
        "temp_c": float(temp),
    }


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def evict_page_cache(path: Path) -> bool:
    """Drop this file from the page cache so the next load is genuinely cold."""
    try:
        subprocess.run(["sync"], check=True)
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
        return True
    except OSError as e:
        print(f"    ! could not evict page cache: {e}")
        return False


class PowerSampler(threading.Thread):
    def __init__(self, interval: float = 0.4):
        super().__init__(daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self.power: list[float] = []
        self.temp: list[float] = []

    def run(self):
        while not self._stop.is_set():
            try:
                s = nvidia_sample()
                self.power.append(s["power_w"])
                self.temp.append(s["temp_c"])
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


_LOG_PATTERNS = {
    "cuda_buffers": re.compile(r"CUDA0.*?buffer size\s*=\s*([\d.]+)\s*MiB"),
    "load_time_ms": re.compile(r"load time\s*=\s*([\d.]+)\s*ms"),
    "n_layer": re.compile(r"n_layer\s*=\s*(\d+)"),
}


def parse_server_log(text: str) -> dict:
    cuda = [float(x) for x in _LOG_PATTERNS["cuda_buffers"].findall(text)]
    out: dict = {}
    if cuda:
        out["vram_reported_mib"] = int(sum(cuda))
    m = _LOG_PATTERNS["load_time_ms"].search(text)
    if m:
        out["load_time_ms"] = float(m.group(1))
    return out


def http_json(url: str, payload: dict | None = None, timeout: float = 600) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------- #
# one llama-server run
# --------------------------------------------------------------------------- #
def run_config(spec: ModelSpec, cfg: RunResult, baseline_mib: int,
               idle_w: float) -> RunResult:
    port = free_port()
    log_path = LOG_DIR / f"{spec.key}_fa-{cfg.fa}_ncmoe-{cfg.n_cpu_moe}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(LLAMA_SERVER),
        "-m", str(spec.path),
        "--host", "127.0.0.1", "--port", str(port),
        "-c", str(cfg.ctx),
        "-ngl", str(cfg.n_gpu_layers),
        "-fa", cfg.fa,
        "-ctk", cfg.cache_type_k, "-ctv", cfg.cache_type_v,
        "-t", str(THREADS),
        "-np", "1",              # strict FIFO: one slot, KV cache = 1 * ctx
        "--no-webui", "--no-warmup",
    ]
    if cfg.n_cpu_moe is not None:
        cmd += ["--n-cpu-moe", str(cfg.n_cpu_moe)]

    cfg.gpu_idle_w = idle_w
    evict_page_cache(spec.path)

    logf = log_path.open("w")
    logf.write("+ " + " ".join(cmd) + "\n\n")
    logf.flush()
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            preexec_fn=os.setsid)

    base_url = f"http://127.0.0.1:{port}"
    try:
        ready = _wait_healthy(proc, base_url, HEALTH_TIMEOUT_S)
        if ready is not True:
            cfg.error = ready or "server exited before ready"
            return cfg
        cfg.cold_load_s = round(time.monotonic() - t0, 2)

        time.sleep(1.0)
        smp = nvidia_sample()
        cfg.vram_used_mib = smp["mem_used_mib"] - baseline_mib
        cfg.fits = smp["mem_used_mib"] <= VRAM_TOTAL_MIB - VRAM_HEADROOM_MIB

        _bench_throughput(spec, base_url, cfg)
        cfg.ok = cfg.pp_tok_s is not None and cfg.tg_tok_s is not None
    except Exception as e:  # noqa: BLE001
        cfg.error = f"{type(e).__name__}: {e}"
    finally:
        _kill(proc)
        logf.close()
        try:
            cfg2 = parse_server_log(log_path.read_text(errors="replace"))
            cfg.vram_reported_mib = cfg2.get("vram_reported_mib")
        except OSError:
            pass
        _wait_vram_free(baseline_mib)

    return cfg


def _wait_healthy(proc, base_url: str, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"exit code {proc.returncode} (see log)"
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, socket.timeout, ConnectionError):
            pass
        time.sleep(0.5)
    return f"not healthy within {timeout:.0f}s"


def _bench_throughput(spec: ModelSpec, base_url: str, cfg: RunResult) -> None:
    # warmup
    endpoint = "/v1/completions" if spec.kind == "fim" else "/v1/chat/completions"

    def one(prompt: str) -> dict:
        if spec.kind == "fim":
            body = {"prompt": prompt, "n_predict": GEN_TOKENS,
                    "temperature": 0, "cache_prompt": False}
        else:
            body = {"messages": [{"role": "user", "content": prompt}],
                    "max_tokens": GEN_TOKENS, "temperature": 0,
                    "stream": False, "cache_prompt": False}
        return http_json(base_url + endpoint, body)

    try:
        one("Say hello.")
    except Exception as e:  # noqa: BLE001
        cfg.error = f"warmup failed: {e}"
        return

    pp, tg = [], []
    sampler = PowerSampler()
    sampler.start()
    for i in range(BENCH_REPS):
        # perturb the prompt so nothing is cached
        r = one(f"[{i}] {BENCH_PROMPT}\n\nSummarise the paragraph above.")
        tm = r.get("timings") or {}
        if "prompt_per_second" in tm:
            pp.append(tm["prompt_per_second"])
        if "predicted_per_second" in tm:
            tg.append(tm["predicted_per_second"])
    sampler.stop()

    if pp:
        cfg.pp_tok_s = round(statistics.median(pp), 1)
    if tg:
        cfg.tg_tok_s = round(statistics.median(tg), 1)
    if sampler.power:
        cfg.gpu_gen_w_mean = round(statistics.mean(sampler.power), 1)
        cfg.gpu_gen_w_max = round(max(sampler.power), 1)
    if sampler.temp:
        cfg.gpu_temp_max = round(max(sampler.temp), 1)


def _kill(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait_vram_free(baseline_mib: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nvidia_sample()["mem_used_mib"] <= baseline_mib + 300:
            return
        time.sleep(1)


# --------------------------------------------------------------------------- #
# per-model plan
# --------------------------------------------------------------------------- #
def plan_configs(spec: ModelSpec, meta: dict) -> list[RunResult]:
    n_layer = meta["n_layer"] or 48
    is_moe = meta["is_moe"]
    ctx = HEAVY_CTX if is_moe else CHAT_CTX
    # Tight-fit models get q8_0 KV; roomy small ones keep f16 for quality.
    tight = is_moe or meta.get("_est_weights_gb", 0) > 6
    ctk = ctv = "q8_0" if tight else "f16"

    configs: list[RunResult] = []
    if is_moe:
        # All non-expert weights on GPU (-ngl 999); sweep how many layers' worth
        # of experts to keep in system RAM. Higher n_cpu_moe = less VRAM, slower.
        for frac in (1.0, 0.8, 0.65, 0.5, 0.4, 0.3):
            n = max(1, min(n_layer, round(n_layer * frac)))
            configs.append(RunResult(
                fa="off", n_gpu_layers=999, n_cpu_moe=n,
                cache_type_k=ctk, cache_type_v=ctv, ctx=ctx))
        # de-dupe n values, keep order
        seen, uniq = set(), []
        for c in configs:
            if c.n_cpu_moe not in seen:
                seen.add(c.n_cpu_moe)
                uniq.append(c)
        return uniq
    else:
        # Dense: try full offload first, then partial if it won't fit.
        for ngl in (999, n_layer - 8, n_layer - 16, n_layer // 2):
            if ngl <= 0:
                continue
            configs.append(RunResult(
                fa="off", n_gpu_layers=ngl, n_cpu_moe=None,
                cache_type_k=ctk, cache_type_v=ctv, ctx=ctx))
        return configs


def bench_model(spec: ModelSpec, baseline_mib: int, idle_w: float) -> dict:
    meta = gguf_summarize(spec.path)
    meta["_est_weights_gb"] = round(spec.path.stat().st_size / 1e9, 2)
    print(f"\n=== {spec.key} :: {spec.display} ===")
    print(f"    arch={meta['architecture']} layers={meta['n_layer']} "
          f"moe={meta['is_moe']} size={meta['_est_weights_gb']}GB")

    planned = plan_configs(spec, meta)
    runs: list[RunResult] = []

    # Phase A: sweep the primary axis with fa=off.
    best: RunResult | None = None
    for cfg in planned:
        print(f"    -> fa=off ngl={cfg.n_gpu_layers} "
              f"n_cpu_moe={cfg.n_cpu_moe} kv={cfg.cache_type_k}")
        res = run_config(spec, cfg, baseline_mib, idle_w)
        runs.append(res)
        _report(res)
        if res.ok and res.fits:
            best = res
            break  # first config that both loads and fits is our pick
        if res.error and "exit code" in res.error and cfg.n_cpu_moe is None:
            continue  # OOM on dense -> try next smaller ngl

    # Phase B: at the winning split, does fa=on help on Pascal?
    if best is not None:
        fa_on = RunResult(
            fa="on", n_gpu_layers=best.n_gpu_layers, n_cpu_moe=best.n_cpu_moe,
            cache_type_k=best.cache_type_k, cache_type_v=best.cache_type_v,
            ctx=best.ctx)
        print(f"    -> fa=on  ngl={fa_on.n_gpu_layers} n_cpu_moe={fa_on.n_cpu_moe}")
        res = run_config(spec, fa_on, baseline_mib, idle_w)
        runs.append(res)
        _report(res)
        if res.ok and res.fits and res.tg_tok_s and best.tg_tok_s:
            if res.tg_tok_s >= best.tg_tok_s:
                best = res

    recommended = None
    if best is not None:
        recommended = {
            "fa": best.fa,
            "n_gpu_layers": best.n_gpu_layers,
            "n_cpu_moe": best.n_cpu_moe,
            "cache_type_k": best.cache_type_k,
            "cache_type_v": best.cache_type_v,
            "ctx": best.ctx,
            "cold_load_s": best.cold_load_s,
            "pp_tok_s": best.pp_tok_s,
            "tg_tok_s": best.tg_tok_s,
            "vram_used_mib": best.vram_used_mib,
        }

    return {
        "key": spec.key,
        "display": spec.display,
        "path": str(spec.path),
        "kind": spec.kind,
        "note": spec.note,
        "meta": {k: v for k, v in meta.items() if not k.startswith("_")},
        "size_gb": meta["_est_weights_gb"],
        "runs": [vars(r) for r in runs],
        "recommended": recommended,
        "usable": recommended is not None,
    }


def _report(r: RunResult) -> None:
    if r.error:
        print(f"       FAIL: {r.error}")
        return
    print(f"       load={r.cold_load_s}s vram={r.vram_used_mib}MiB "
          f"fits={r.fits} pp={r.pp_tok_s} tg={r.tg_tok_s} tok/s "
          f"gpu_w={r.gpu_gen_w_mean}")


# --------------------------------------------------------------------------- #
# model inventory
# --------------------------------------------------------------------------- #
def discover_models() -> list[ModelSpec]:
    m = MODELS_DIR
    specs = [
        ModelSpec("coder-1.5b-fim",
                  "Coder Coder 1.5B (FIM / infill only)",
                  m / "coder-1.5b-fim" / "coder-1.5b-fim.gguf",
                  kind="fim",
                  note="FIM/infill endpoint only. Must NOT appear in the chat picker."),
        ModelSpec("fast-4b",
                  "FamilyA 4B (Q8_0)",
                  m / "FamilyA" / "fast-4b" / "fast-4b.gguf",
                  kind="chat"),
        ModelSpec("daily-9b",
                  "FamilyA 9B (Q6_K)",
                  m / "FamilyA" / "daily-9b" / "daily-9b.gguf",
                  kind="chat"),
        ModelSpec("alt-4b",
                  "FamilyB 4B (alt finetune, Q8_0)",
                  m / "FamilyB" / "alt-4b"
                  / "alt-4b.gguf",
                  kind="chat",
                  note="Not stock Gemma. Label clearly in the picker."),
        ModelSpec("moe-26b",
                  "FamilyB 26B QAT (Q4_0, MoE)",
                  m / "FamilyB" / "moe-26b"
                  / "moe-26b.gguf",
                  kind="chat", note="MoE, CPU expert offload."),
        ModelSpec("moe-30b",
                  "FamilyC Flash (Q4_K_M, MoE)",
                  m / "moe-30b" / "moe-30b.gguf",
                  kind="chat", note="MoE, CPU expert offload."),
        ModelSpec("moe-35b",
                  "FamilyA 35B (Q4_K_M, MoE)",
                  m / "moe-35b"
                  / "moe-35b.gguf",
                  kind="chat", note="MoE, CPU expert offload. Tightest on RAM."),
    ]
    missing = [s.key for s in specs if not s.path.exists()]
    if missing:
        print(f"WARNING: missing GGUFs: {missing}")
    return [s for s in specs if s.path.exists()]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def load_existing() -> dict:
    if OUT_PATH.exists():
        return json.loads(OUT_PATH.read_text())
    return {}


def git_commit(short: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(LLAMA_CPP_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[],
                    help="model key(s) to run; repeatable")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore and overwrite existing results")
    args = ap.parse_args()

    if not LLAMA_SERVER.exists():
        print(f"llama-server not found at {LLAMA_SERVER}")
        return 1

    specs = discover_models()
    if args.only:
        specs = [s for s in specs if s.key in args.only]
        if not specs:
            print(f"no model matched {args.only}")
            return 1

    if args.dry_run:
        for s in specs:
            meta = gguf_summarize(s.path)
            print(f"{s.key:28} {s.kind:5} moe={meta['is_moe']!s:5} "
                  f"layers={meta['n_layer']} :: {s.display}")
            for c in plan_configs(s, meta):
                print(f"    fa={c.fa} ngl={c.n_gpu_layers} "
                      f"n_cpu_moe={c.n_cpu_moe} kv={c.cache_type_k} ctx={c.ctx}")
        return 0

    results = {} if args.fresh else load_existing()
    if "models" not in results:
        base = nvidia_sample()
        results = {
            "schema": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname(),
            "llama_cpp_commit": git_commit("HEAD"),
            "force_mmq": True,
            "gpu": {
                "name": "NVIDIA GeForce GTX 1080 Ti",
                "vram_total_mib": base["mem_total_mib"],
                "vram_baseline_mib": base["mem_used_mib"],
                "note": "baseline = desktop (Xorg + compositor) before any model load",
            },
            "bench_params": {
                "chat_ctx": CHAT_CTX, "heavy_ctx": HEAVY_CTX,
                "gen_tokens": GEN_TOKENS, "reps": BENCH_REPS, "threads": THREADS,
            },
            "system_power_note": (
                "GPU watts are nvidia-smi power.draw. Whole-system idle/load "
                "watts are NOT measured here -- set IDLE_WATTS / LOAD_WATTS in "
                ".env from a wall meter if available."
            ),
            "models": {},
        }

    baseline_mib = results["gpu"]["vram_baseline_mib"]
    idle = nvidia_sample()
    print(f"GPU baseline: {baseline_mib} MiB used, {idle['power_w']} W idle")

    for spec in specs:
        if not args.fresh and spec.key in results["models"] \
                and results["models"][spec.key].get("runs"):
            print(f"skip {spec.key} (already done; use --fresh to redo)")
            continue
        try:
            results["models"][spec.key] = bench_model(spec, baseline_mib,
                                                      idle["power_w"])
        except KeyboardInterrupt:
            print("\ninterrupted; writing partial results")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  model {spec.key} errored: {e}")
            results["models"][spec.key] = {"key": spec.key, "error": str(e)}
        OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"  wrote {OUT_PATH}")

    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nDONE -> {OUT_PATH}")
    _print_summary(results)
    return 0


def _print_summary(results: dict) -> None:
    print("\n" + "=" * 72)
    print(f"{'model':30} {'usable':6} {'load s':7} {'pp t/s':7} {'tg t/s':7} "
          f"{'vram MiB':9} {'fa':3}")
    print("-" * 72)
    for key, m in results.get("models", {}).items():
        rec = m.get("recommended")
        if not rec:
            print(f"{key:30} {'NO':6} {m.get('error', 'no working config')}")
            continue
        print(f"{key:30} {'yes':6} {rec['cold_load_s']!s:7} "
              f"{rec['pp_tok_s']!s:7} {rec['tg_tok_s']!s:7} "
              f"{rec['vram_used_mib']!s:9} {rec['fa']:3}")


if __name__ == "__main__":
    sys.exit(main())
