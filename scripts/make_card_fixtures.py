# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regenerate the fictional card fixture PDF (docs/SPEC-cards.md §17).

    uv run python scripts/make_card_fixtures.py

Prints tests/fixtures/cards/docs/*.src.html to PDF with headless Chrome, like
`make screenshots`. Everything in those files is fictional ("Example Bank").
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cards" / "docs"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def main() -> None:
    if not Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME} (set CHROME=...)")
    for src in sorted(DOCS.glob("*.src.html")):
        out = src.with_name(src.name.replace(".src.html", ".pdf"))
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([
                CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--no-pdf-header-footer",
                "--print-to-pdf-no-header", f"--print-to-pdf={out}", src.as_uri(),
            ], check=True, capture_output=True, timeout=120)
        print(f"{out.relative_to(DOCS.parents[3])}: {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
