# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run model-written Python where it can't reach anything.

The code comes from a model, and the model's input can come from a web page,
so this treats every snippet as hostile. Each run is a fresh bubblewrap
sandbox:

- its own user, PID, network, IPC and UTS namespaces: no network at all,
  and it can't see or signal anything else on the machine;
- a read-only view of /usr and of the sandbox's own Python environment
  (data/sandbox, built by deploy/setup-sandbox.sh), and nothing else of the
  host: not the repo, not .env, not the database, not /home;
- a small private /tmp (tmpfs, size-capped) as its only writable place;
- limits: wall clock (killed, with everything it started), CPU seconds,
  file size, and, when systemd can give it a scope, a memory cap and a cap
  on the number of processes. Without systemd it falls back to an
  address-space rlimit.

Nothing survives a run. Its output comes back on stdout; pictures it saved
under /tmp/out come back at the end of that stream, after a marker line
carrying a random nonce, as base64 PNG, and are checked to be PNGs here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import signal
import time
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

log = logging.getLogger("llamacracy.sandbox")

MAX_FIGURES = 4
MAX_FIGURE_BYTES = 2 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"

# Runs inside the sandbox as `python -I -c RUNNER nonce`, with the model's code
# on stdin. Notebook-style: the value of a final bare expression is printed.
RUNNER = r'''
import ast, base64, json, os, sys, traceback
nonce = sys.argv[1]
src = sys.stdin.read()
os.makedirs("/tmp/out", exist_ok=True)
g = {"__name__": "__main__"}
try:
    tree = ast.parse(src, "<code>")
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, "<code>", "exec"), g)
    if last is not None:
        v = eval(compile(ast.Expression(last.value), "<code>", "eval"), g)
        if v is not None:
            print(repr(v) if isinstance(v, str) else v)
    status = 0
except SystemExit as e:
    status = e.code if isinstance(e.code, int) else 0
except BaseException:
    t, v, tb = sys.exc_info()
    # the runner's own frame is noise: start at the model's code
    while tb is not None and tb.tb_frame.f_code.co_filename != "<code>":
        tb = tb.tb_next
    traceback.print_exception(t, v, tb)
    status = 1
figs = []
try:
    names = sorted(n for n in os.listdir("/tmp/out") if n.lower().endswith(".png"))
except OSError:
    names = []
for n in names[:MAX_FIGURES]:
    p = os.path.join("/tmp/out", n)
    if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) <= MAX_FIGURE_BYTES:
        with open(p, "rb") as f:
            figs.append({"name": n, "png": base64.b64encode(f.read()).decode()})
sys.stdout.flush(); sys.stderr.flush()
sys.stdout.write("\n" + nonce + "\n" + json.dumps({"status": status, "figures": figs}) + "\n")
sys.stdout.flush()
'''.replace("MAX_FIGURES", str(MAX_FIGURES)).replace("MAX_FIGURE_BYTES", str(MAX_FIGURE_BYTES))


@dataclass
class RunResult:
    output: str                    # stdout and stderr, interleaved, capped
    ok: bool                       # exited cleanly, no exception
    timed_out: bool = False
    seconds: float = 0.0
    figures: list[tuple[str, bytes]] = field(default_factory=list)   # (name, png bytes)
    note: str = ""                 # why it stopped, when it wasn't the code's doing


class SandboxUnavailable(RuntimeError):
    pass


@cache
def _base_prefix(python: str) -> str:
    """Where the sandbox Python's standard library lives (a venv only links
    to it), so that directory can be mounted read-only too."""
    import subprocess
    out = subprocess.run([python, "-I", "-c", "import sys; print(sys.base_prefix)"],
                         capture_output=True, text=True, timeout=20, check=True)
    return out.stdout.strip()


def available(python: str) -> str | None:
    """None when run_python can work here, else the reason it can't."""
    if shutil.which("bwrap") is None:
        return "bubblewrap (bwrap) isn't installed"
    if shutil.which("prlimit") is None:
        return "prlimit (util-linux) isn't installed"
    if not Path(python).is_file():
        return f"the sandbox Python isn't built ({python}); run deploy/setup-sandbox.sh"
    try:
        _base_prefix(python)
    except Exception as e:  # noqa: BLE001
        return f"the sandbox Python doesn't start ({type(e).__name__})"
    return None


@cache
def _scope_works() -> bool:
    """Whether `systemd-run --user --scope` can cap memory and tasks here
    (it can't in most containers and CI runners)."""
    if shutil.which("systemd-run") is None:
        return False
    import subprocess
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "-p", "TasksMax=8",
                               "true"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


INNER_ENV = "/opt/py"     # where the sandbox environment appears inside: no host paths


def _bwrap_args(python: str) -> list[str]:
    venv = str(Path(python).parent.parent)       # .../data/sandbox
    base = _base_prefix(python)
    args = ["bwrap", "--unshare-all", "--unshare-user", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr"]
    for top in ("bin", "sbin", "lib", "lib64", "lib32"):
        p = Path("/" + top)
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), str(p)]
        elif p.is_dir():
            args += ["--ro-bind", str(p), str(p)]
    # the environment at a neutral path (a venv finds itself from its python's
    # location); the base interpreter where the venv's links expect it,
    # which on most systems is under /usr already
    args += ["--ro-bind", venv, INNER_ENV]
    if not (base == "/usr" or base.startswith("/usr/")):
        args += ["--ro-bind", base, base]
    # enough of /etc for the dynamic loader and for Python's startup
    for f in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/alternatives", "/etc/localtime",
              "/etc/fonts"):
        if os.path.exists(f):
            args += ["--ro-bind", f, f]
    args += ["--proc", "/proc", "--dev", "/dev",
             "--size", str(64 * 1024 * 1024), "--tmpfs", "/tmp",
             "--chdir", "/tmp", "--disable-userns", "--clearenv",
             "--setenv", "HOME", "/tmp", "--setenv", "PATH", "/usr/bin",
             "--setenv", "MPLBACKEND", "Agg", "--setenv", "MPLCONFIGDIR", "/tmp/.mpl",
             "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "PYTHONUNBUFFERED", "1",
             # one BLAS thread: many threads each reserve a big buffer
             "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
             "--setenv", "MKL_NUM_THREADS", "1"]
    return args


async def run(code: str, *, python: str, timeout_s: float = 20, memory_mb: int = 1024,
              output_chars: int = 8000) -> RunResult:
    """Run `code` in a fresh sandbox and return what it printed."""
    python = os.path.abspath(python)       # not resolved: a venv's python is a link
    why = available(python)
    if why:
        raise SandboxUnavailable(why)
    nonce = "=" * 8 + secrets.token_hex(16)
    inner = INNER_ENV + "/" + str(Path(python).relative_to(Path(python).parent.parent))
    cmd = [*_bwrap_args(python), inner, "-I", "-c", RUNNER, nonce]
    scoped = _scope_works()
    # rlimits via prlimit(1), not preexec_fn, which isn't safe in a threaded server
    cpu = int(timeout_s) + 5
    rl = [f"--cpu={cpu}", f"--fsize={32 * 1024 * 1024}", "--core=0"]
    if not scoped:
        # no cgroup to cap real memory: cap address space instead (looser for
        # numpy, which reserves more than it touches)
        rl.append(f"--as={(memory_mb + 1024) * 1024 * 1024}")
    cmd = ["prlimit", *rl, "--", *cmd]
    if scoped:
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
               "-p", f"MemoryMax={memory_mb}M", "-p", "MemorySwapMax=0",
               "-p", "TasksMax=64", "-p", "CPUQuota=200%",
               "-p", f"RuntimeMaxSec={int(timeout_s) + 10}", *cmd]

    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    cap = output_chars * 4 + MAX_FIGURES * MAX_FIGURE_BYTES * 2
    timed_out = False

    async def read_all() -> bytes:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(code.encode("utf-8", "replace"))
        await proc.stdin.drain()
        proc.stdin.close()
        buf = bytearray()
        while chunk := await proc.stdout.read(65536):
            if len(buf) < cap:
                buf += chunk[:cap - len(buf)]
        await proc.wait()
        return bytes(buf)

    try:
        raw = await asyncio.wait_for(read_all(), timeout_s)
    except TimeoutError:
        timed_out = True
        _kill(proc)
        await proc.wait()
        raw = b""
        assert proc.stdout is not None
        try:
            raw = await asyncio.wait_for(proc.stdout.read(output_chars * 4), 2)
        except (TimeoutError, Exception):  # noqa: BLE001
            pass
    except asyncio.CancelledError:
        _kill(proc)
        raise
    seconds = time.monotonic() - t0

    text = raw.decode("utf-8", "replace")
    out, _, tail = text.partition("\n" + nonce + "\n")
    status, figures = None, []
    if tail:
        try:
            meta = json.loads(tail.strip().splitlines()[0])
            status = meta.get("status")
            for f in meta.get("figures", [])[:MAX_FIGURES]:
                data = base64.b64decode(f.get("png", ""), validate=True)
                if data.startswith(_PNG) and len(data) <= MAX_FIGURE_BYTES:
                    figures.append((str(f.get("name", "figure.png"))[:80], data))
        except (ValueError, TypeError, IndexError):
            status = None
    note = ""
    if timed_out:
        note = f"stopped after {timeout_s:g}s (time limit)"
    elif status is None:
        rc = proc.returncode
        note = ("killed: out of memory or CPU time" if rc in (-9, 137, -24, 152)
                else f"the sandbox ended unexpectedly (exit {rc})")
    if len(out) > output_chars:
        out = out[:output_chars] + f"\n[... {len(out) - output_chars:,} more characters cut]"
    return RunResult(output=out.rstrip("\n"), ok=(status == 0 and not timed_out),
                     timed_out=timed_out, seconds=seconds, figures=figures, note=note)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
