# SPDX-License-Identifier: AGPL-3.0-or-later
"""llama-server flags whose spelling depends on the llama.cpp build."""

from __future__ import annotations

import functools
import subprocess


@functools.cache
def _help(server: str) -> str:
    try:
        return subprocess.run([server, "--help"], capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def no_mmap(server: str) -> list[str]:
    """Load the weights into RAM instead of memory-mapping them. Newer builds
    replaced `--no-mmap` with `--load-mode none` and reject the old flag
    (llama.cpp #28334), so ask the binary which one it speaks."""
    return ["--load-mode", "none"] if "--load-mode" in _help(server) else ["--no-mmap"]
