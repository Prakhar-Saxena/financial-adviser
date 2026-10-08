"""Make redacted fixtures from real files (SPEC §18). You run this; Claude never does.

    uv run python scripts/redact.py FILE [FILE ...] --out-dir tests/fixtures/incoming

- Names from config/cards.json -> a distinct fake name per person, matching
  cards.example.json (self -> ALEX EXAMPLE, spouse -> SAM Q EXAMPLE, ...), so cardholder
  attribution can still be tested. Leftover single name parts -> FAKE.
- Card endings (from --endings and cards.json) everywhere, plus "ending in 1234",
  "x1234", "****1234", "XXXX XXXX XXXX 1234" -> 0000. Output file names are redacted too.
- Digit runs of 7+ (account, reference and order numbers) -> other digits, same length.
  The mapping is consistent within one run (so IDs stay distinct and repeated IDs still
  match across files) but uses a random salt that is never saved.
- Email addresses -> fake@example.com
- Address blocks (street lines, "City, ST 12345" lines) -> [ADDRESS]  (PDF text and HTML)

Inputs: .csv (stays CSV), .html/.htm (stays HTML; scripts and styles removed),
.pdf (becomes .txt of pdfplumber text, pages separated by form feeds, then a
"[LINKS]" section listing each page's link targets, redacted), .txt/.json.
Review every output before committing it. The summary lists lines worth a second look.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "cards.json"

STREET = re.compile(
    r"^\s*\d{1,6}\s+[A-Za-z0-9 .'#-]{2,40}\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|"
    r"drive|ln|lane|ct|court|way|pl|place|ter|terrace|pkwy|parkway|cir|circle|hwy|highway)\b"
    r".*$",
    re.IGNORECASE | re.MULTILINE,
)
UNIT = re.compile(r"^\s*(apt|apartment|suite|ste|unit)\.?\s*#?\s*\w+\s*$", re.I | re.M)
# "City, ST 12345[-6789]" anywhere in a line: two-column PDF layouts put other text after it.
CITY_ZIP = re.compile(
    r"\b[A-Za-z][A-Za-z.'-]*(?:[ ]+[A-Za-z][A-Za-z.'-]*){0,3},?[ ]+[A-Z]{2}[ ]+\d{5}(?:-\d{4})?\b"
)
# Amazon's header: "Deliver to <name>" over "<City> <ZIP>" (no state).
CITY_ZIP_NO_STATE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,30}[ ]\d{5}(?:-\d{4})?\b", re.MULTILINE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ENDING = re.compile(
    r"(?i)(ending\s+(in:?\s*)?|(?:x{4}[\s-]*){1,3}|x{1,4}|\*{1,4}|\.{3,4}|#\s?|-)(\d{4,5})\b"
)
FAKE_PEOPLE = {  # keep in step with scripts/make_cards_example.py
    "self": "Alex Example", "spouse": "Sam Q Example", "father": "Pat Example",
    "mother": "Robin Example",
}
LONG_DIGITS = re.compile(r"\d{7,}")
SUSPECT = re.compile(r"(?i)\b(account|acct|member|card\s*(no|number)|ssn)\b.*\d{4}")


class Redactor:
    def __init__(self, cards: dict, salt: bytes, endings: set[str] | None = None):
        self.salt = salt
        self.counts: dict[str, int] = {}
        # Full names and aliases map to that person's fake name; leftover parts to FAKE.
        self.fake_for: dict[str, str] = {}
        parts: set[str] = set()
        for i, p in enumerate(cards.get("people", [])):
            fake = FAKE_PEOPLE.get(p.get("relationship", ""), f"Person{i} Example")
            for n in [p.get("full_name", ""), *p.get("name_aliases", [])]:
                n = " ".join(n.split())
                if n:
                    self.fake_for[n.upper()] = fake
                    parts.update(part for part in n.split() if len(part) >= 3)
        ordered = sorted(set(self.fake_for) | {x.upper() for x in parts}, key=len, reverse=True)
        self.name_re = (
            re.compile(r"\b(" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in ordered)
                       + r")\b", re.I)
            if ordered else None
        )
        endings = set(endings or ()) | {
            h["card_ending"] for c in cards.get("cards", []) for h in c.get("cardholders", [])
            if h.get("card_ending")
        }
        self.endings_re = (
            # Not inside money: skip "$4321", "1,4321" or "4321.00".
            re.compile(r"(?<![\d$,.])(" + "|".join(sorted(endings, key=len, reverse=True))
                       + r")(?!\d|\.\d)")
            if endings else None
        )

    def _count(self, what: str, n: int) -> None:
        if n:
            self.counts[what] = self.counts.get(what, 0) + n

    def _fake_name(self, m: re.Match) -> str:
        found = m.group(0)
        fake = self.fake_for.get(" ".join(found.split()).upper(), "Fake")
        return fake.upper() if found.isupper() else fake

    def _digits(self, m: re.Match) -> str:
        h = hashlib.sha256(self.salt + m.group(0).encode()).hexdigest()
        out = "".join(str(int(c, 16) % 10) for c in h)
        while len(out) < len(m.group(0)):
            out += out
        return out[: len(m.group(0))]

    def text(self, s: str, addresses: bool = True) -> str:
        if self.name_re:
            s, n = self.name_re.subn(self._fake_name, s)
            self._count("names", n)
        s, n = EMAIL.subn("fake@example.com", s)
        self._count("emails", n)
        if addresses:
            for rx in (STREET, UNIT, CITY_ZIP, CITY_ZIP_NO_STATE):
                s, n = rx.subn("[ADDRESS]", s)
                self._count("address lines", n)
        s, n = LONG_DIGITS.subn(self._digits, s)
        self._count("long digit runs", n)
        if self.endings_re:
            s, n = self.endings_re.subn(lambda m: "0" * len(m.group(1)), s)
            self._count("card endings", n)
        s, n = ENDING.subn(lambda m: m.group(1) + "0" * len(m.group(3)), s)
        self._count("ending patterns", n)
        return s


def redact_html(r: Redactor, raw: str) -> str:
    from bs4 import BeautifulSoup, Comment, NavigableString

    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for node in soup.find_all(string=True):
        if isinstance(node, NavigableString) and node.strip():
            node.replace_with(r.text(str(node)))
    for tag in soup.find_all(True):
        for attr, val in list(tag.attrs.items()):
            if isinstance(val, str):
                tag[attr] = r.text(val, addresses=False)
            elif isinstance(val, list):
                tag[attr] = [r.text(v, addresses=False) for v in val]
    return str(soup)


def redact_pdf(r: Redactor, path: Path) -> str:
    import pdfplumber

    pages, links = [], []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            pages.append(r.text(page.extract_text() or ""))
            for link in page.hyperlinks or []:
                uri = link.get("uri")
                if uri:
                    links.append(f"page {i}: {r.text(uri, addresses=False)}")
    return "\f".join(pages) + "\f[LINKS]\n" + "\n".join(links) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--cards", type=Path, default=CONFIG)
    ap.add_argument("--endings", default="", help="comma-separated card endings to mask")
    args = ap.parse_args()

    cards = json.loads(args.cards.read_text()) if args.cards.exists() else {}
    endings = {e.strip() for e in args.endings.split(",") if e.strip()}
    r = Redactor(cards, secrets.token_bytes(16), endings)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for f in args.files:
        ext = f.suffix.lower()
        stem = r.text(f.stem, addresses=False)
        if ext == ".pdf":
            out, dest = redact_pdf(r, f), args.out_dir / (stem + ".txt")
        elif ext in (".html", ".htm"):
            out, dest = redact_html(r, f.read_text(errors="replace")), args.out_dir / (stem + ext)
        elif ext == ".csv":
            out = r.text(f.read_text(errors="replace"), addresses=False)
            dest = args.out_dir / (stem + ext)
        elif ext in (".txt", ".json"):
            out, dest = r.text(f.read_text(errors="replace")), args.out_dir / (stem + ext)
        else:
            print(f"skip {f}: unsupported type", file=sys.stderr)
            continue
        dest.write_text(out)
        suspects = [ln.strip()[:100] for ln in out.splitlines() if SUSPECT.search(ln)]
        print(f"{f.name} -> {dest}")
        for s in suspects[:10]:
            print(f"   check: {s}")

    print("replacements:", json.dumps(r.counts))
    print("Review every output by eye before committing it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
