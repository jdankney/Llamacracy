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
    # saved somewhere else under /tmp, or drawn and never saved: still shown
    r = await _run("import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.savefig('/tmp/out.png')")
    assert [n for n, _ in r.figures] == ["out.png"]
    r = await _run("import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()")
    assert [n for n, _ in r.figures] == ["figure-1.png"]


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


async def test_it_reads_attached_data_files_but_cannot_change_them(tmp_path):
    host = tmp_path / "upload-1234.csv"
    host.write_text("x,y\n1,2\n3,5\n")
    files = sandbox.data_files([("My data.csv", str(host)), ("notes.pdf", str(host))])
    assert files == [("/data/My_data.csv", str(host))]
    r = await _run("import pandas as pd\nint(pd.read_csv('/data/My_data.csv')['y'].sum())", files=files)
    assert r.ok and r.output == "7"
    w = await _run("open('/data/My_data.csv', 'a').write('9,9')", files=files)
    assert not w.ok and "Read-only" in w.output
    assert host.read_text().endswith("3,5\n")


async def test_it_has_the_charting_libraries_and_crisp_defaults():
    r = await _run("import seaborn as sns, pandas as pd, openpyxl, PIL\n"
                   "import matplotlib as mpl\nmpl.rcParams['savefig.dpi']")
    assert r.ok and r.output == "150.0"


async def test_an_svg_comes_back_as_a_png():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
           '<rect width="120" height="80" fill="teal"/></svg>')
    r = await _run(f"open('/tmp/out/box.svg', 'w').write({svg!r})")
    assert r.ok and [n for n, _ in r.figures] == ["box.png"]
    assert r.figures[0][1].startswith(b"\x89PNG")


async def test_it_has_the_diagram_libraries():
    r = await _run("import schemdraw, matplotlib_venn, squarify, wordcloud\n"
                   "import schemdraw.elements as elm\n"
                   "d = schemdraw.Drawing(show=False)\nd += elm.Resistor().label('2.2 kΩ')\n"
                   "d += elm.Capacitor().label('470 µF')\nd.save('/tmp/out/rc.png')")
    assert r.ok and [n for n, _ in r.figures] == ["rc.png"], r.output
