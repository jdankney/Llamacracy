#!/usr/bin/env python3
"""Phase 0 -- measure reality.

The owner already has a tuned `llama-server` invocation for every model (see
their ~/.bashrc). So Phase 0 is not a config search: it validates each
known-good config under our actual serving mode (`-np 1`, strict FIFO, one
model at a time) and records what it costs and how fast it runs.

Per model we measure:
  - cold load time from disk (page cache evicted first)
  - resident VRAM (nvidia-smi delta over the desktop baseline)
  - prompt-processing tok/s and generation tok/s (llama-server's own timings)
  - GPU power draw (nvidia-smi) at idle and during generation
  - fa=on (owner's setting) vs fa=off, since Pascal FA is not a given

Output feeds the llama-swap config (Phase 1), the queue's load-time estimates
(Phase 2), and the credit-limit arithmetic (Phase 3).

Stdlib only. Drives the real `llama-server` over HTTP.

    python3 bench/phase0_bench.py --dry-run
    python3 bench/phase0_bench.py --only daily-9b
    python3 bench/phase0_bench.py                 # every model, resumable
    python3 bench/phase0_bench.py --no-fa-off     # skip the fa=off comparison
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
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
M = Path(os.path.expanduser("~/models"))
LLAMA_SERVER = Path(os.path.expanduser("~/.local/bin/llama-server"))
LLAMA_CPP_DIR = Path(os.path.expanduser("~/llama.cpp"))
OUT_PATH = REPO / "bench" / "bench-results.json"
LOG_DIR = REPO / "bench" / "raw"

# 1080 Ti reports ~11264 MiB; desktop (Xorg + kwin) holds ~1.2 GB.
VRAM_TOTAL_MIB = 11264
VRAM_HEADROOM_MIB = 400          # keep this much unused or we call it "does not fit"

GEN_TOKENS = 320
BENCH_REPS = 3
HEALTH_TIMEOUT_S = 420           # --no-mmap heavyweights load ~20 GB from disk
THREADS = 6

_PARA = (
    "The quick brown fox jumps over the lazy dog while the committee debates "
    "the merits of a strictly first-in-first-out scheduling policy for a "
    "single shared accelerator. Each participant weighs latency against "
    "fairness, and the discussion returns repeatedly to how one should price "
    "exclusive wall-clock occupancy of a scarce resource. "
)
BENCH_PROMPT = (_PARA * 12).strip()   # ~1000 tokens


# --------------------------------------------------------------------------- #
# model inventory -- the owner's tuned configs, minus their personal
# web-UI / MCP flags (we proxy the OpenAI endpoint, no tool use in v1).
# --------------------------------------------------------------------------- #
@dataclass
class ModelSpec:
    key: str
    display: str
    path: Path
    kind: str                       # "chat" | "fim"
    serve_ctx: int                  # -c we serve at, with -np 1
    extra: list[str] = field(default_factory=list)  # sampling / batch / template
    n_cpu_moe: int | None = None
    kv_type: str = "f16"
    no_mmap: bool = False
    reasoning_budget: int | None = None
    note: str = ""

    def base_cmd(self, fa: str, ctx: int, n_cpu_moe: int | None) -> list[str]:
        cmd = [
            str(LLAMA_SERVER),
            "-m", str(self.path),
            "--host", "127.0.0.1",
            "-c", str(ctx),
            "-ngl", "99",
            "-fa", fa,
            "-t", str(THREADS),
            "-np", "1",                       # strict FIFO: single slot
            "--no-webui", "--no-warmup",
        ]
        if n_cpu_moe is not None:
            cmd += ["--n-cpu-moe", str(n_cpu_moe)]
        if self.kv_type != "f16":
            cmd += ["-ctk", self.kv_type, "-ctv", self.kv_type]
        if self.no_mmap:
            cmd += ["--no-mmap"]
        if self.reasoning_budget is not None:
            cmd += ["--reasoning-budget", str(self.reasoning_budget)]
        cmd += self.extra
        return cmd


def inventory() -> list[ModelSpec]:
    qwen_sampling = ["--jinja", "--temp", "0.6", "--top-p", "0.95",
                     "--top-k", "20", "--min-p", "0", "-b", "2048", "-ub", "512"]
    gemma_batch = ["-b", "2048", "-ub", "512"]
    specs = [
        ModelSpec(
            "coder-1.5b-fim", "Coder Coder 1.5B (FIM / infill only)",
            M / "coder-1.5b-fim" / "coder-1.5b-fim.gguf",
            kind="fim", serve_ctx=8192,
            note="FIM/infill endpoint only. MUST NOT appear in the chat picker."),
        ModelSpec(
            "fast-4b", "FamilyA 4B (Q8_0)",
            M / "FamilyA" / "fast-4b" / "fast-4b.gguf",
            kind="chat", serve_ctx=32768, kv_type="q8_0", no_mmap=True,
            reasoning_budget=0, extra=list(qwen_sampling),
            note="Tier 3. Owner runs reasoning disabled."),
        ModelSpec(
            "daily-9b", "FamilyA 9B (Q6_K)",
            M / "FamilyA" / "daily-9b" / "daily-9b.gguf",
            kind="chat", serve_ctx=32768, kv_type="q8_0", no_mmap=True,
            extra=list(qwen_sampling),
            note="Tier 2 daily driver. Fully in VRAM."),
        ModelSpec(
            "alt-4b",
            "FamilyB 4B (alt finetune, Q8_0)",
            M / "FamilyB" / "alt-4b"
            / "alt-4b.gguf",
            kind="chat", serve_ctx=32768,
            extra=["--chat-template-file",
                   str(M / "FamilyB" / "alt-4b" / "chat_template.jinja")],
            note="Not stock Gemma. Owner runs c=225280; we serve less. "
                 "Label as a finetune in the picker."),
        ModelSpec(
            "moe-26b", "FamilyB 26B QAT (Q4_0, MoE)",
            M / "FamilyB" / "moe-26b" / "moe-26b.gguf",
            kind="chat", serve_ctx=16384, n_cpu_moe=18, no_mmap=True,
            extra=list(gemma_batch), note="MoE, expert offload. Owner n_cpu_moe=18."),
        ModelSpec(
            "moe-30b", "FamilyC Flash (Q4_K_M, MoE)",
            M / "moe-30b" / "moe-30b.gguf",
            kind="chat", serve_ctx=16384, n_cpu_moe=28, no_mmap=True,
            extra=["--jinja", "--temp", "1.0", "--top-p", "0.95"],
            note="MoE, expert offload. Owner n_cpu_moe=28 @ c=16384."),
        ModelSpec(
            "moe-35b", "FamilyA 35B (Q4_K_M, MoE)",
            M / "moe-35b" / "moe-35b.gguf",
            kind="chat", serve_ctx=16384, n_cpu_moe=28, no_mmap=True,
            extra=list(qwen_sampling),
            note="MoE, expert offload. Owner n_cpu_moe=28 @ c=32768/4-slot; "
                 "tightest on system RAM (~22 GB weights, --no-mmap)."),
    ]
    missing = [s.key for s in specs if not s.path.exists()]
    if missing:
        print(f"WARNING missing GGUFs: {missing}")
    return [s for s in specs if s.path.exists()]


# --------------------------------------------------------------------------- #
# result records
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    label: str
    fa: str
    ctx: int
    n_cpu_moe: int | None
    kv_type: str
    cmd: list[str] = field(default_factory=list)
    ok: bool = False
    fits: bool = False
    error: str = ""
    cold_load_s: float | None = None
    vram_used_mib: int | None = None
    vram_free_after_load_mib: int | None = None
    pp_tok_s: float | None = None
    tg_tok_s: float | None = None
    gpu_idle_w: float | None = None
    gpu_gen_w_mean: float | None = None
    gpu_gen_w_max: float | None = None
    gpu_temp_max: float | None = None
    ram_used_delta_mib: int | None = None
    swap_used_mib: int | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def nvidia_sample() -> dict:
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=memory.used,memory.total,memory.free,power.draw,temperature.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[0]
    used, total, free, power, temp = (x.strip() for x in out.split(","))
    return {"mem_used_mib": int(float(used)), "mem_total_mib": int(float(total)),
            "mem_free_mib": int(float(free)), "power_w": float(power),
            "temp_c": float(temp)}


def mem_sample() -> dict:
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        d[k] = int(v.strip().split()[0]) // 1024  # kB -> MiB
    return {"mem_avail_mib": d.get("MemAvailable", 0),
            "swap_used_mib": d.get("SwapTotal", 0) - d.get("SwapFree", 0),
            "committed_mib": d.get("Committed_AS", 0)}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def evict_page_cache(path: Path) -> None:
    try:
        subprocess.run(["sync"], check=True)
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except OSError as e:
        print(f"    ! page-cache evict failed ({e}); load time will read warm")


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


def http_json(url: str, payload: dict | None = None, timeout: float = 900) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wait_healthy(proc, base_url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"exited rc={proc.returncode} before healthy (see log)"
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.5)
    return f"not healthy within {timeout:.0f}s"


def _kill(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait_vram_free(baseline_mib, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nvidia_sample()["mem_used_mib"] <= baseline_mib + 300:
            return
        time.sleep(1)


# --------------------------------------------------------------------------- #
# one run
# --------------------------------------------------------------------------- #
def run_once(spec: ModelSpec, r: RunResult, vram_base: int, ram_base: int,
             idle_w: float) -> RunResult:
    port = free_port()
    cmd = spec.base_cmd(r.fa, r.ctx, r.n_cpu_moe) + ["--port", str(port)]
    r.cmd = cmd
    r.gpu_idle_w = idle_w
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{spec.key}__{r.label}.log"

    evict_page_cache(spec.path)
    logf = log_path.open("w")
    logf.write("+ " + " ".join(shlex.quote(c) for c in cmd) + "\n\n")
    logf.flush()

    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            preexec_fn=os.setsid)
    base_url = f"http://127.0.0.1:{port}"
    try:
        ready = _wait_healthy(proc, base_url, HEALTH_TIMEOUT_S)
        if ready is not True:
            r.error = ready
            return r
        r.cold_load_s = round(time.monotonic() - t0, 2)
        time.sleep(1.5)

        g = nvidia_sample()
        m = mem_sample()
        r.vram_used_mib = g["mem_used_mib"] - vram_base
        r.vram_free_after_load_mib = g["mem_free_mib"]
        r.ram_used_delta_mib = ram_base - m["mem_avail_mib"]
        r.swap_used_mib = m["swap_used_mib"]
        r.fits = g["mem_free_mib"] >= VRAM_HEADROOM_MIB

        _bench(spec, base_url, r)
        r.ok = r.pp_tok_s is not None and r.tg_tok_s is not None
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    finally:
        _kill(proc)
        logf.close()
        _wait_vram_free(vram_base)
    return r


def _bench(spec: ModelSpec, base_url: str, r: RunResult) -> None:
    endpoint = "/v1/completions" if spec.kind == "fim" else "/v1/chat/completions"

    def one(text: str) -> dict:
        if spec.kind == "fim":
            body = {"prompt": text, "n_predict": GEN_TOKENS, "temperature": 0,
                    "cache_prompt": False}
        else:
            body = {"messages": [{"role": "user", "content": text}],
                    "max_tokens": GEN_TOKENS, "temperature": 0, "stream": False,
                    "cache_prompt": False}
        return http_json(base_url + endpoint, body)

    try:
        one("Reply with the single word: ready.")
    except Exception as e:  # noqa: BLE001
        r.error = f"warmup request failed: {e}"
        return

    pp, tg = [], []
    sampler = PowerSampler()
    sampler.start()
    for i in range(BENCH_REPS):
        try:
            resp = one(f"[{i}] {BENCH_PROMPT}\n\nSummarise the paragraph above "
                       f"in two sentences.")
        except Exception as e:  # noqa: BLE001
            r.error = f"bench request {i} failed: {e}"
            break
        tm = resp.get("timings") or {}
        if "prompt_per_second" in tm:
            pp.append(tm["prompt_per_second"])
        if "predicted_per_second" in tm:
            tg.append(tm["predicted_per_second"])
    sampler.stop()

    if pp:
        r.pp_tok_s = round(statistics.median(pp), 1)
    if tg:
        r.tg_tok_s = round(statistics.median(tg), 1)
    if sampler.power:
        r.gpu_gen_w_mean = round(statistics.mean(sampler.power), 1)
        r.gpu_gen_w_max = round(max(sampler.power), 1)
    if sampler.temp:
        r.gpu_temp_max = round(max(sampler.temp), 1)


# --------------------------------------------------------------------------- #
# per-model plan: primary (owner config, fa=on) + fa=off + fit fallbacks
# --------------------------------------------------------------------------- #
def plan(spec: ModelSpec) -> list[RunResult]:
    runs = [RunResult(label="primary_fa-on", fa="on", ctx=spec.serve_ctx,
                      n_cpu_moe=spec.n_cpu_moe, kv_type=spec.kv_type)]
    return runs


def fit_fallbacks(spec: ModelSpec, failed: RunResult) -> list[RunResult]:
    out = []
    if spec.n_cpu_moe is not None:
        out.append(RunResult(label=f"ncmoe-{spec.n_cpu_moe + 6}_fa-on", fa="on",
                             ctx=spec.serve_ctx, n_cpu_moe=spec.n_cpu_moe + 6,
                             kv_type="q8_0"))
    if spec.serve_ctx > 8192:
        out.append(RunResult(label="ctx-8192_fa-on", fa="on", ctx=8192,
                             n_cpu_moe=(spec.n_cpu_moe + 6) if spec.n_cpu_moe
                             else None, kv_type="q8_0"))
    return out


def bench_model(spec: ModelSpec, vram_base, ram_base, idle_w, do_fa_off) -> dict:
    meta = gguf_summarize(spec.path)
    size_gb = round(spec.path.stat().st_size / 1e9, 2)
    print(f"\n=== {spec.key} :: {spec.display} ===")
    print(f"    arch={meta['architecture']} layers={meta['n_layer']} "
          f"moe={meta['is_moe']} size={size_gb}GB serve_ctx={spec.serve_ctx} "
          f"n_cpu_moe={spec.n_cpu_moe} kv={spec.kv_type}")

    runs: list[RunResult] = []
    primary = run_once(spec, plan(spec)[0], vram_base, ram_base, idle_w)
    runs.append(primary)
    _report(primary)

    working = primary if (primary.ok and primary.fits) else None

    if working is None:
        for fb in fit_fallbacks(spec, primary):
            print(f"    fallback: {fb.label}")
            res = run_once(spec, fb, vram_base, ram_base, idle_w)
            runs.append(res)
            _report(res)
            if res.ok and res.fits:
                working = res
                break

    if working is not None and do_fa_off:
        off = RunResult(label="fa-off", fa="off", ctx=working.ctx,
                        n_cpu_moe=working.n_cpu_moe, kv_type=working.kv_type)
        print("    comparison: fa=off")
        res = run_once(spec, off, vram_base, ram_base, idle_w)
        runs.append(res)
        _report(res)
        # keep whichever generates faster as the recommendation
        if res.ok and res.fits and res.tg_tok_s and working.tg_tok_s \
                and res.tg_tok_s > working.tg_tok_s * 1.03:
            working = res

    rec = None
    if working is not None:
        rec = {
            "fa": working.fa, "ctx": working.ctx, "n_cpu_moe": working.n_cpu_moe,
            "kv_type": working.kv_type, "cold_load_s": working.cold_load_s,
            "pp_tok_s": working.pp_tok_s, "tg_tok_s": working.tg_tok_s,
            "vram_used_mib": working.vram_used_mib,
            "vram_free_after_load_mib": working.vram_free_after_load_mib,
            "ram_used_delta_mib": working.ram_used_delta_mib,
            "swap_used_mib": working.swap_used_mib,
            "gpu_gen_w_mean": working.gpu_gen_w_mean,
            "cmd": working.cmd,
        }

    return {
        "key": spec.key, "display": spec.display, "path": str(spec.path),
        "kind": spec.kind, "note": spec.note, "size_gb": size_gb,
        "reasoning_budget": spec.reasoning_budget,
        "meta": {k: v for k, v in meta.items() if not k.startswith("_")},
        "runs": [vars(x) for x in runs],
        "recommended": rec, "usable": rec is not None,
    }


def _report(r: RunResult) -> None:
    if r.error:
        print(f"       FAIL [{r.label}]: {r.error}")
        return
    print(f"       [{r.label}] load={r.cold_load_s}s vram={r.vram_used_mib}MiB "
          f"free={r.vram_free_after_load_mib}MiB fits={r.fits} "
          f"ram+={r.ram_used_delta_mib}MiB swap={r.swap_used_mib}MiB "
          f"pp={r.pp_tok_s} tg={r.tg_tok_s} t/s gpuW={r.gpu_gen_w_mean}")


# --------------------------------------------------------------------------- #
def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(LLAMA_CPP_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def load_existing() -> dict:
    return json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--no-fa-off", action="store_true",
                    help="skip the fa=off comparison run")
    args = ap.parse_args()

    if not LLAMA_SERVER.exists():
        print(f"llama-server not found at {LLAMA_SERVER}")
        return 1

    specs = inventory()
    if args.only:
        specs = [s for s in specs if s.key in args.only]
        if not specs:
            print(f"no model matched {args.only}")
            return 1

    if args.dry_run:
        for s in specs:
            print(f"\n{s.key}  ({s.kind}, serve_ctx={s.serve_ctx})")
            print("  " + " ".join(shlex.quote(c) for c in
                                  s.base_cmd("on", s.serve_ctx, s.n_cpu_moe)
                                  + ["--port", "PORT"]))
        return 0

    results = {} if args.fresh else load_existing()
    if "models" not in results:
        base = nvidia_sample()
        mbase = mem_sample()
        results = {
            "schema": 2,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname(),
            "llama_cpp_commit": git_commit(),
            "force_mmq": True,
            "gpu": {"name": "NVIDIA GeForce GTX 1080 Ti",
                    "vram_total_mib": base["mem_total_mib"],
                    "vram_baseline_mib": base["mem_used_mib"],
                    "note": "baseline = desktop before any model load"},
            "system": {"ram_avail_baseline_mib": mbase["mem_avail_mib"],
                       "note": "measured with the owner's normal desktop apps running"},
            "bench_params": {"gen_tokens": GEN_TOKENS, "reps": BENCH_REPS,
                             "threads": THREADS, "np": 1},
            "power_note": ("GPU watts are nvidia-smi power.draw only. Whole-system "
                           "idle/load watts must be set in .env from a wall meter."),
            "models": {},
        }

    vram_base = results["gpu"]["vram_baseline_mib"]
    ram_base = results["system"]["ram_avail_baseline_mib"]
    idle = nvidia_sample()
    print(f"baseline: VRAM {vram_base} MiB used, {idle['power_w']} W idle; "
          f"RAM {ram_base} MiB available")

    for spec in specs:
        done = results["models"].get(spec.key, {})
        if not args.fresh and done.get("runs"):
            print(f"skip {spec.key} (done; --fresh to redo)")
            continue
        try:
            results["models"][spec.key] = bench_model(
                spec, vram_base, ram_base, idle["power_w"], not args.no_fa_off)
        except KeyboardInterrupt:
            print("\ninterrupted; writing partial results")
            OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {spec.key} errored: {e}")
            results["models"][spec.key] = {"key": spec.key, "error": str(e)}
        OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"  -> wrote {OUT_PATH}")

    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    _summary(results)
    return 0


def _summary(results: dict) -> None:
    print("\n" + "=" * 78)
    print(f"{'model':24}{'ok':4}{'load s':8}{'pp t/s':9}{'tg t/s':8}"
          f"{'vram MiB':10}{'ram+ MiB':10}{'fa':4}")
    print("-" * 78)
    for k, m in results.get("models", {}).items():
        rec = m.get("recommended")
        if not rec:
            print(f"{k:24}NO   {m.get('error', 'no config fit')}")
            continue
        print(f"{k:24}{'y':4}{str(rec['cold_load_s']):8}"
              f"{str(rec['pp_tok_s']):9}{str(rec['tg_tok_s']):8}"
              f"{str(rec['vram_used_mib']):10}{str(rec['ram_used_delta_mib']):10}"
              f"{rec['fa']:4}")
    print("\nnext: review these numbers + the proposed credit limits before Phase 1")


if __name__ == "__main__":
    sys.exit(main())
