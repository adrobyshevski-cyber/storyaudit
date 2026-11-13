from decimal import Decimal

from app.calculations import calculate_values, weighted_labels
from app.utils import canonical_json, normalize_label


def test_canonical_json_is_stable():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_normalize_label():
    assert normalize_label("  Hello\n WORLD ") == "hello world"


def test_weighted_mode_has_deterministic_tie_break():
    result = weighted_labels([
        {"label": "Zulu", "confidence": 0.5},
        {"label": "Alpha", "confidence": 0.5},
    ])
    assert result["label"] == "alpha"
    assert result["weight"] == 0.5


def test_formula_golden_path():
    values = calculate_values({key: Decimal("0.5") for key in ("customer", "problem", "object", "promise", "coherence", "depth", "before", "after")})
    assert values["narrative_clarity"] == Decimal("50.000000")
    assert values["narrative_coherence"] == Decimal("50.000000")
    assert values["meaning_capital"] == Decimal("50.000000")
    assert values["storyaudit_index"] == Decimal("50.000000")
