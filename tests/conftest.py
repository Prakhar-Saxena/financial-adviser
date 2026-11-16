# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"
FAKE_BIN = TESTS / "fake_bin"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch) -> None:
    """No test touches the real data dir (locks, ai-runs, card-docs, ...)."""
    monkeypatch.setenv("FIN_DATA_DIR", str(tmp_path / "FinancialAdviserData"))


@pytest.fixture(autouse=True)
def _cli_config_untouched(tmp_path, monkeypatch) -> None:
    """`fin init` writes into the CLI's config folder (it renames an old cards.json and copies
    the example). Tests point it at a copy of config/ without the real file, so the real
    config/accounts.json is never moved or written. Tests that need their own config patch
    fin.cli.CONFIG_DIR again."""
    import shutil

    from fin.config import CONFIG_DIR

    cfg = tmp_path / "cli-config"
    shutil.copytree(CONFIG_DIR, cfg, ignore=shutil.ignore_patterns("accounts.json", "cards.json"))
    monkeypatch.setattr("fin.cli.CONFIG_DIR", cfg)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "FinancialAdviserData"
    monkeypatch.setenv("FIN_DATA_DIR", str(d))
    return d


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> Path:
    """Put the fake `claude` first on PATH. Returns the call log path."""
    log = tmp_path / "fake_claude.jsonl"
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_AUTH", raising=False)
    return log
