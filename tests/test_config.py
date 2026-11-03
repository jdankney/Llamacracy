# SPDX-License-Identifier: AGPL-3.0-or-later
"""The committed .env.example is what the quick start and install.sh copy to
.env unchanged: it must load, and say what the code's defaults are."""

from __future__ import annotations

from pathlib import Path

from llamacracy.config import Settings

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_env_example_loads_as_is(monkeypatch):
    for k in ("DEV_MODE", "DEV_EMAIL", "ADMIN_EMAILS", "IDLE_TTL_SECONDS", "SMART_TITLES"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=EXAMPLE)
    assert s.idle_ttl_effective_seconds == 15 * 60
    assert s.smart_titles is True


def test_an_empty_value_means_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("IDLE_TTL_SECONDS", raising=False)
    env = tmp_path / ".env"
    env.write_text("IDLE_TTL_SECONDS=\nSTUDY_MODEL=\nSOURCE_URL=\n")
    for k in ("IDLE_TTL_SECONDS", "STUDY_MODEL", "SOURCE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=env)
    assert s.idle_ttl_seconds is None and s.study_model is None
    assert s.source_url == ""          # blank still hides the source link
