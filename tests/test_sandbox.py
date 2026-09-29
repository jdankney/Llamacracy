# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real sandbox, where one is built (bwrap + deploy/setup-sandbox.sh).
Skipped elsewhere, CI included: these check the walls, not the plumbing
(tests/test_api.py covers that with a fake)."""

from __future__ import annotations

import pytest

from llamacracy import sandbox
from llamacracy.config import REPO_ROOT

PY = str(REPO_ROOT / "data" / "sandbox" / "bin" / "python")
pytestmark = pytest.mark.skipif(sandbox.available(PY) is not None,
                                reason="no sandbox built here (deploy/setup-sandbox.sh)")


async def _run(code: str, **kw) -> sandbox.RunResult:
    return await sandbox.run(code, python=PY, **{"timeout_s": 10, "memory_mb": 512, **kw})


async def test_it_computes_and_shows_the_last_expression():
    r = await _run("import sympy as sp\nx = sp.symbols('x')\nsp.integrate(x**2, (x, 0, 3))")
    assert r.ok and r.output == "9"


async def test_it_brings_back_pictures():
    r = await _run("import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\n"
                   "plt.savefig('/tmp/out/line.png')")
    assert r.ok and [n for n, _ in r.figures] == ["line.png"]
    assert r.figures[0][1].startswith(b"\x89PNG")


async def test_it_cannot_reach_the_network_or_the_host():
    net = await _run("import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=3)")
    assert not net.ok and "unreachable" in net.output.lower()
    host = await _run(f"open({str(REPO_ROOT / '.env')!r})")
    assert not host.ok and "FileNotFoundError" in host.output
    look = await _run("import os\nsorted(os.listdir('/'))")
    assert "home" not in look.output and "root" not in look.output
    own = await _run("open('/opt/py/x', 'w')")
    assert not own.ok and "Read-only" in own.output


async def test_it_is_stopped_at_its_limits():
    loop = await _run("while True: pass", timeout_s=2)
    assert loop.timed_out and not loop.ok
    mem = await _run("x = bytearray(2 * 1024**3)")
    assert not mem.ok
    err = await _run("1/0")
    assert not err.ok and err.output.splitlines()[-1] == "ZeroDivisionError: division by zero"
