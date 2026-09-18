"""The model registry -- config/models.json, generated from bench-results.json
by bench/gen_llamaswap_config.py. Holds display names, tiers, sampling
defaults, and the measured cold-load / throughput / VRAM seeds the queue uses
for its "cold start ~25s" estimates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import get_settings


@dataclass(frozen=True)
class ModelInfo:
    key: str
    display: str
    kind: str                 # "chat" | "fim"
    tier: str | None
    in_picker: bool
    blurb: str
    reasoning: str | None
    sampling: dict = field(default_factory=dict)
    ctx: int = 8192
    seed_cold_load_s: float = 10.0
    seed_tg_tok_s: float = 20.0
    seed_pp_tok_s: float = 300.0
    vram_used_mib: int = 0
    gpu_gen_watts: float | None = None
    max_tokens_default: int = 2048
    vision_key: str | None = None    # paired -vision llama-swap entry, if any (Phase 8.4)


class Registry:
    def __init__(self, models: dict[str, ModelInfo], generated_at: str):
        self._models = models
        self.generated_at = generated_at

    def get(self, key: str) -> ModelInfo | None:
        return self._models.get(key)

    def require(self, key: str) -> ModelInfo:
        m = self._models.get(key)
        if m is None:
            raise KeyError(f"unknown model {key!r}")
        return m

    def all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def chat_models(self) -> list[ModelInfo]:
        return [m for m in self._models.values() if m.kind == "chat" and m.in_picker]

    def vision_variant(self, key: str) -> ModelInfo | None:
        """The unlisted -vision llama-swap entry paired with `key`, if any."""
        base = self._models.get(key)
        if base is None or not base.vision_key:
            return None
        return self._models.get(base.vision_key)


@lru_cache
def get_registry() -> Registry:
    path = Path(get_settings().models_registry_path)
    if not path.exists():
        raise SystemExit(
            f"model registry not found at {path}\n"
            "Generate it from your model inventory first:\n"
            "  cp bench/inventory.example.json bench/inventory.json   # then edit\n"
            "  python3 bench/gen_llamaswap_config.py"
        )
    data = json.loads(path.read_text())
    models = {}
    for key, m in data["models"].items():
        models[key] = ModelInfo(
            key=key,
            display=m["display"],
            kind=m["kind"],
            tier=m.get("tier"),
            in_picker=m.get("in_picker", m["kind"] != "fim"),
            blurb=m.get("blurb", ""),
            reasoning=m.get("reasoning"),
            sampling=m.get("sampling", {}),
            ctx=m.get("ctx", 8192),
            seed_cold_load_s=m.get("seed_cold_load_s", 10.0),
            seed_tg_tok_s=m.get("seed_tg_tok_s", 20.0),
            seed_pp_tok_s=m.get("seed_pp_tok_s", 300.0),
            vram_used_mib=m.get("vram_used_mib", 0),
            gpu_gen_watts=m.get("gpu_gen_watts"),
            max_tokens_default=m.get("max_tokens_default", 2048),
            vision_key=m.get("vision_key"),
        )
    return Registry(models, data.get("generated_at", ""))
