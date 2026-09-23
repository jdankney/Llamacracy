# SPDX-License-Identifier: AGPL-3.0-or-later
"""GPU power sampling via nvidia-smi. This box has no whole-system power meter
and no usable CPU RAPL, so nvidia-smi's GPU draw is the only measured number;
the cost model adds a fixed NON_GPU_LOAD_WATTS on top.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("llamacracy.power")

_HAVE_NVIDIA_SMI = shutil.which("nvidia-smi") is not None


async def _sample_once() -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return float(out.decode().strip().splitlines()[0])
    except (TimeoutError, OSError, ValueError, IndexError):
        return None


class GpuPowerSampler:
    """Background poller. Start it when generation begins, stop it at the end,
    then read `.mean` (None if nvidia-smi was unavailable)."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._samples: list[float] = []

    async def _loop(self) -> None:
        while True:
            v = await _sample_once()
            if v is not None:
                self._samples.append(v)
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if _HAVE_NVIDIA_SMI and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="gpu-power-sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def mean(self) -> float | None:
        if not self._samples:
            return None
        return round(sum(self._samples) / len(self._samples), 1)
