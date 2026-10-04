#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Builds the Python environment that the run_python tool runs model-written
# code with: data/sandbox, separate from the app's own .venv, holding only
# deploy/sandbox-requirements.txt. The code runs inside bubblewrap (see
# llamacracy/sandbox.py), which sees this directory read-only and nothing
# else of the checkout. Idempotent: re-run to pick up new requirements.
#
#   ./deploy/setup-sandbox.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${SANDBOX_DIR:-$REPO/data/sandbox}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

command -v bwrap >/dev/null 2>&1 || warn "bubblewrap isn't installed: run_python stays off until it is (package: bubblewrap)"

say "building the sandbox Python in $DEST"
uv venv --quiet --allow-existing "$DEST"
uv pip install --quiet --python "$DEST/bin/python" -r "$REPO/deploy/sandbox-requirements.txt"
# crisper chart defaults; the sandbox points MATPLOTLIBRC at this copy
cp "$REPO/deploy/sandbox-matplotlibrc" "$DEST/matplotlibrc"
"$DEST/bin/python" -c "import sympy, numpy, scipy, pint, matplotlib, networkx, pandas, seaborn, openpyxl, PIL; print('    ok: sympy', sympy.__version__, '· numpy', numpy.__version__, '· pandas', pandas.__version__, '· seaborn', seaborn.__version__)"
