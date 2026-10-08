"""Generate config/cards.example.json from config/cards.json with fake personal values.

Structure, ids, issuers, sites and products are kept; names and card endings are replaced.
Usage: uv run python scripts/make_cards_example.py
"""

import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"
FAKE_PEOPLE = {
    "self": ("Me", "Alex Example"),
    "spouse": ("Sam", "Sam Q Example"),
    "father": ("Dad", "Pat Example"),
    "mother": ("Mom", "Robin Example"),
}


def main() -> None:
    cards = json.loads((CONFIG / "cards.json").read_text())
    cards["_notes"] = [
        "Example with fake values. Copy to config/cards.json (gitignored) and fill in real ones."
    ] + [n for n in cards.get("_notes", []) if not n.startswith("Real config")]
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
    (CONFIG / "cards.example.json").write_text(json.dumps(cards, indent=2) + "\n")
    print(f"wrote {CONFIG / 'cards.example.json'}")


if __name__ == "__main__":
    main()
