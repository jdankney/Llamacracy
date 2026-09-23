# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal GGUF metadata reader (stdlib only).

We only need a handful of scalar values to plan a benchmark run:
  - general.architecture
  - <arch>.block_count      -> number of transformer layers (for --n-cpu-moe sweeps)
  - <arch>.context_length   -> training context
  - <arch>.expert_count     -> >0 means MoE
  - general.size_label / general.name (nice to have)

Arrays and tensor info are skipped. This avoids a throwaway multi-GB model
load just to discover the layer count.
"""

from __future__ import annotations

import struct
from pathlib import Path

_GGUF_MAGIC = 0x46554747  # "GGUF" little-endian

# value type enum -> (struct format, size) for scalars
_SCALAR = {
    0: ("<B", 1),   # uint8
    1: ("<b", 1),   # int8
    2: ("<H", 2),   # uint16
    3: ("<h", 2),   # int16
    4: ("<I", 4),   # uint32
    5: ("<i", 4),   # int32
    6: ("<f", 4),   # float32
    7: ("<?", 1),   # bool
    10: ("<Q", 8),  # uint64
    11: ("<q", 8),  # int64
    12: ("<d", 8),  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9


class _Reader:
    """Reads sequentially from an open file handle, buffering in chunks so a
    multi-hundred-thousand-entry tokenizer array doesn't need a fixed window."""

    def __init__(self, fh, chunk: int = 4 << 20):
        self.fh = fh
        self.chunk = chunk
        self.buf = b""
        self.pos = 0

    def take(self, n: int) -> bytes:
        while self.pos + n > len(self.buf):
            more = self.fh.read(self.chunk)
            if not more:
                raise EOFError("GGUF header truncated")
            # drop consumed prefix to keep the buffer bounded
            self.buf = self.buf[self.pos :] + more
            self.pos = 0
        b = self.buf[self.pos : self.pos + n]
        self.pos += n
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def gstr(self) -> str:
        n = self.u64()
        return self.take(n).decode("utf-8", "replace")

    def value(self, vtype: int):
        if vtype in _SCALAR:
            fmt, size = _SCALAR[vtype]
            return struct.unpack(fmt, self.take(size))[0]
        if vtype == _TYPE_STRING:
            return self.gstr()
        if vtype == _TYPE_ARRAY:
            elem_type = self.u32()
            count = self.u64()
            # We don't need array values; skip them precisely.
            if elem_type in _SCALAR:
                self.take(_SCALAR[elem_type][1] * count)
                return f"<array:{count}>"
            if elem_type == _TYPE_STRING:
                for _ in range(count):
                    self.gstr()
                return f"<array:str:{count}>"
            raise ValueError(f"nested/unknown array elem type {elem_type}")
        raise ValueError(f"unknown gguf value type {vtype}")


def read_metadata(path: str | Path) -> dict:
    """Return the GGUF key/value metadata as a plain dict.

    GGUF stores all metadata up front, so this streams just the header region
    and stops once every KV pair has been read.
    """
    path = Path(path)
    with path.open("rb") as fh:
        r = _Reader(fh)
        if r.u32() != _GGUF_MAGIC:
            raise ValueError(f"{path} is not a GGUF file")
        version = r.u32()
        if version not in (2, 3):
            raise ValueError(f"unsupported GGUF version {version}")
        _tensor_count = r.u64()
        kv_count = r.u64()

        meta: dict = {"_gguf_version": version}
        for _ in range(kv_count):
            key = r.gstr()
            vtype = r.u32()
            meta[key] = r.value(vtype)
    return meta


def summarize(path: str | Path) -> dict:
    """Pull the fields the benchmark planner cares about."""
    m = read_metadata(path)
    arch = m.get("general.architecture", "unknown")
    g = lambda suffix, default=None: m.get(f"{arch}.{suffix}", default)  # noqa: E731
    return {
        "path": str(path),
        "architecture": arch,
        "name": m.get("general.name"),
        "size_label": m.get("general.size_label"),
        "quant_version": m.get("general.file_type"),
        "n_layer": g("block_count"),
        "n_ctx_train": g("context_length"),
        "n_embd": g("embedding_length"),
        "n_head": g("attention.head_count"),
        "n_head_kv": g("attention.head_count_kv"),
        "expert_count": g("expert_count", 0) or 0,
        "expert_used_count": g("expert_used_count", 0) or 0,
        "rope_freq_base": g("rope.freq_base"),
        "is_moe": bool(g("expert_count", 0)),
    }


if __name__ == "__main__":
    import json
    import sys

    for p in sys.argv[1:]:
        print(json.dumps(summarize(p), indent=2, default=str))
