from __future__ import annotations

import os
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"
FAKE_BIN = TESTS / "fake_bin"


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
