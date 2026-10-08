from fin.config import CONFIG_DIR, load_cards
from fin.normalize.people import PeopleIndex, normalize_name

CARDS = load_cards(CONFIG_DIR / "cards.example.json")


def test_attribution():
    idx = PeopleIndex(CARDS)
    spouse = next(p for p in CARDS.people if p.relationship == "spouse")
    assert idx.attribute(None).person_id == "self"
    assert idx.attribute("   ").source == "default_self"
    a = idx.attribute(spouse.full_name.lower())
    assert a.person_id == spouse.id and a.source == "name"
    assert idx.attribute(spouse.name_aliases[0]).person_id == spouse.id
    me = next(p for p in CARDS.people if p.relationship == "self")
    assert idx.attribute(me.full_name.upper()).person_id == "self"
    unknown = idx.attribute("SOMEONE ELSE")
    assert unknown.person_id is None and unknown.source == "unknown_name"


def test_normalize_name():
    assert normalize_name("  sam  q. example ") == "SAM Q EXAMPLE"
