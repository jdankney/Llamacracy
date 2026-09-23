# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration. All values come from environment / .env (see
.env.example). Nothing here is a secret at rest -- secrets stay in .env, which
is gitignored.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- identity / auth -----------------------------------------------------
    # Auth itself lives in oauth2-proxy + Dex (deploy/); the app only trusts
    # the forwarded headers. ADMIN_EMAILS is the one identity setting here.
    admin_emails: str = ""
    dev_mode: str = ""                       # "1"/"true" -> trust a fake identity
    dev_email: str = "dev@localhost"
    dev_sub: str = "dev-local-sub"
    dev_name: str = "Dev User"

    # --- source code offer (AGPL-3.0-or-later, section 13) -------------------
    # Shown to every signed-in user as a "Source code" link. If you run a
    # MODIFIED Llamacracy for other people, the AGPL requires you to offer
    # them your modified source: point this at your own fork.
    source_url: str = "https://github.com/jdankney/Llamacracy"

    # --- upstream / binding ------------------------------------------------
    llamaswap_url: str = "http://127.0.0.1:8091"
    app_bind_host: str = "127.0.0.1"
    app_bind_port: int = 8000
    database_path: str = str(REPO_ROOT / "data" / "llamacracy.db")
    models_registry_path: str = str(REPO_ROOT / "config" / "models.json")

    # --- search (Phase 8) -- SearXNG, injected on request, never model-driven -
    searxng_url: str = "http://127.0.0.1:8085"
    search_max_results: int = 4

    # --- vision uploads (Phase 8.4) -- on-disk, never base64-in-DB -----------
    upload_dir: str = str(REPO_ROOT / "data" / "uploads")
    upload_max_mb: float = 8.0

    # --- compact context -- summarize older turns to free the context window -
    compact_keep_recent: int = 6          # most-recent messages always kept verbatim
    compact_summary_max_tokens: int = 600  # cap on the generated summary itself

    # --- queue -----------------------------------------------------------
    idle_ttl_minutes: int = 15
    idle_ttl_seconds: float | None = None    # if set, wins over idle_ttl_minutes
    small_model_fast_lane: bool = False
    fast_lane_vram_threshold_gb: float = 6.0

    # --- metering ------------------------------------------------------------
    session_credit_limit: float = 3600
    weekly_credit_limit: float = 12000
    session_window_hours: float = 5
    load_time_multiplier: float = 0.5
    max_tokens_per_request: int = 2048

    # --- cost model --------------------------------------------------------
    non_gpu_load_watts: float = 110
    idle_watts: float = 95
    markup: float = 1.0
    electricity_rate: float = 0.456          # flat fallback ($/kWh)
    tou_schedule: str = ""                   # JSON: {"0": 0.37, ... "23": 0.46}

    # ----------------------------------------------------------------------
    @property
    def is_dev(self) -> bool:
        return self.dev_mode.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def session_window_seconds(self) -> float:
        return self.session_window_hours * 3600

    @property
    def idle_ttl_effective_seconds(self) -> float:
        """Seconds a model may sit idle in VRAM before it's unloaded.
        IDLE_TTL_SECONDS wins if set; otherwise IDLE_TTL_MINUTES * 60."""
        if self.idle_ttl_seconds is not None:
            return max(0.0, self.idle_ttl_seconds)
        return self.idle_ttl_minutes * 60

    @property
    def tou_table(self) -> dict[int, float] | None:
        if not self.tou_schedule.strip():
            return None
        raw = json.loads(self.tou_schedule)
        return {int(k): float(v) for k, v in raw.items()}

    def rate_for_hour(self, hour_local: int) -> float:
        table = self.tou_table
        if table is None:
            return self.electricity_rate
        return table.get(hour_local % 24, self.electricity_rate)

    @field_validator("tou_schedule")
    @classmethod
    def _validate_tou(cls, v: str) -> str:
        if v.strip():
            json.loads(v)  # fail fast on malformed JSON
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
