# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate config/cards.example.json from config/cards.json with fake personal values.

Structure, ids, card issuers, item sources and card products are kept; names and card endings
are replaced, and `private_words` is emptied. Every bank site and account is replaced by the
fictional Example Bank (docs/SPEC-checking.md §1.1, §5), and one is added when there is none,
so the example always shows the shape. Usage: uv run python scripts/make_cards_example.py
"""

import copy
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"
FAKE_PEOPLE = {
    "self": ("Me", "Alex Example"),
    "spouse": ("Sam", "Sam Q Example"),
    "father": ("Dad", "Pat Example"),
    "mother": ("Mom", "Robin Example"),
}
FAKE_BANK = {"name": "Example Bank", "start_url": "https://online.bank.example/",
             "allowed_domains": ["bank.example"]}
FAKE_ACCOUNT = {"product": "Example Bank Checking", "account_type": "checking",
                "account_ending": "", "statement_markers": []}
FIXED_NOTES = [
    "private_words: anything else that would identify you (your bank, your street). The git "
    "hooks refuse any commit or push that contains one.",
    "bank_accounts: your bank account(s). The bank is named only here, in private/ and in the "
    "data dir (docs/SPEC-checking.md section 1.1).",
]


def make_example(real: dict) -> dict:
    cards = copy.deepcopy(real)
    cards["_notes"] = [
        "Example with fake values. Copy to config/cards.json (gitignored) and fill in real ones."
    ] + [n for n in cards.get("_notes", []) if not n.startswith(("Real config", "Example with"))
         and n not in FIXED_NOTES] + FIXED_NOTES
    new_ids = {}
    for i, person in enumerate(cards["people"]):
        new_ids[person["id"]] = person["relationship"]
        person["id"] = new_ids[person["id"]]
        fallback = (f"Person{i}", f"Person{i} Example")
        display, full = FAKE_PEOPLE.get(person["relationship"], fallback)
        person["display_name"], person["full_name"] = display, full
        person["name_aliases"] = [full.upper()] if person["name_aliases"] else []
    n = 0
    for card in cards["cards"]:
        for holder in card["cardholders"]:
            holder["person"] = new_ids[holder["person"]]
            n += 1
            width = 5 if card["network"] == "amex" else 4
            holder["card_ending"] = str(n).zfill(width)

    accounts = cards.get("bank_accounts") or [{"id": "checking", "site": "bank"}]
    bank_sites = list(dict.fromkeys(a["site"] for a in accounts))
    site_ids = {s: "bank" if i == 0 else f"bank{i + 1}" for i, s in enumerate(bank_sites)}
    cards["issuers"] = [s for s in cards["issuers"] if s["id"] not in site_ids] + [
        {"id": sid, **FAKE_BANK} for sid in site_ids.values()]
    fake_accounts = []
    for i, acct in enumerate(accounts):
        fake = {"id": "checking" if i == 0 else f"checking{i + 1}",
                "site": site_ids[acct["site"]], **FAKE_ACCOUNT}
        if acct.get("account_ending"):
            n += 1
            fake["account_ending"] = str(n).zfill(4)
        fake_accounts.append(fake)
    cards["bank_accounts"] = fake_accounts
    cards["private_words"] = []
    return cards


def main() -> None:
    real = json.loads((CONFIG / "cards.json").read_text())
    (CONFIG / "cards.example.json").write_text(json.dumps(make_example(real), indent=2) + "\n")
    print(f"wrote {CONFIG / 'cards.example.json'}")


if __name__ == "__main__":
    main()
