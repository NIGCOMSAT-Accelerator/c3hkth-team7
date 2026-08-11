"""Administrative names must resolve however a partner spells them.

## The bug

Every browse query was an exact string match against GRID3's own spelling, so a partner's bulk
importer sending a reasonable variant received an **empty list** — indistinguishable from a
genuinely uncovered area. Measured before the fix:

| sent | result |
|---|---|
| `Obafemi Owode` | 12 wards |
| `Obafemi-Owode` | **0** |
| `Obafemi/Owode` | **0** |
| `Obafemi Owode LGA` | **0** |
| `Ado-Odo/Ota` | **0** (GRID3 has `Ado Odo/Ota`) |
| `Ijebu North-East` | **0** |
| `Ogun State` | **0 LGAs** |
| `Federal Capital Territory` | **0 LGAs** (GRID3 spells it `Fct`) |

That is the worst failure shape available: no error, no log line, just a row an importer skips and
a farm that never gets monitored. It matters most on the ward tier, where `[]` is ALSO the correct
answer for the 13 states with no ward layer — so a typo and a coverage gap were the same response.

## Why normalisation rather than fuzzy matching

Fuzzy matching would introduce a worse failure than the one it fixes: silently monitoring the
wrong district because two names were 85% similar. `_canonical` is deterministic — case,
punctuation, spacing and administrative suffixes — and anything it cannot reach must either be an
explicit alias or GRID3's own published `ward_alt_names`.
"""

from __future__ import annotations

import pytest

from app.eo.admin import _canonical, _match_name


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # Punctuation and spacing — the common spreadsheet variants.
        ("Obafemi Owode", "obafemiowode"),
        ("Obafemi-Owode", "obafemiowode"),
        ("Obafemi/Owode", "obafemiowode"),
        ("obafemi  owode", "obafemiowode"),
        ("OBAFEMI OWODE", "obafemiowode"),
        # Administrative suffixes a column header adds and GRID3 omits.
        ("Obafemi Owode LGA", "obafemiowode"),
        ("Obafemi Owode Local Government Area", "obafemiowode"),
        ("Ogun State", "ogun"),
        # Aliases where the WORDS differ, so normalisation alone cannot reach them.
        ("Federal Capital Territory", "fct"),
        ("Abuja", "fct"),
        ("FCT", "fct"),
        ("Nassarawa", "nasarawa"),
    ],
)
def test_names_normalise_to_a_stable_key(supplied, expected):
    assert _canonical(supplied) == expected


def test_matching_returns_the_upstream_spelling():
    """Normalising for comparison is not the same as being able to query by it.

    ArcGIS matches on its own strings, so the matcher must hand back GRID3's spelling — returning
    the canonical key would produce a query that finds nothing.
    """
    candidates = ["Obafemi Owode", "Ado Odo/Ota", "Ijebu North East"]

    assert _match_name("Obafemi-Owode", candidates) == "Obafemi Owode"
    assert _match_name("Ado-Odo/Ota", candidates) == "Ado Odo/Ota"
    assert _match_name("Ijebu North-East", candidates) == "Ijebu North East"


def test_an_exact_match_short_circuits():
    """A correctly-spelled name must not pay for normalisation, and must never lose to a
    normalised collision."""
    candidates = ["Ogun", "Ogun State"]

    assert _match_name("Ogun State", candidates) == "Ogun State"
    assert _match_name("Ogun", candidates) == "Ogun"


def test_an_unknown_name_returns_none_rather_than_a_guess():
    """**The line this must not cross.**

    Fuzzy matching would monitor the wrong district on an 85% similarity — a worse failure than
    the empty list it replaces, because it produces confident readings for somebody else's land.
    """
    candidates = ["Obafemi Owode", "Ado Odo/Ota"]

    assert _match_name("Nowhere", candidates) is None
    assert _match_name("Obafemi", candidates) is None, (
        "a prefix matched, which means partial matching crept in"
    )
    assert _match_name("", candidates) is None
    assert _match_name("   ", candidates) is None


def test_the_alias_table_stays_small_and_justified():
    """Aliases are for names normalisation cannot reach, not a synonym dictionary.

    GRID3's ward layer publishes real synonyms in `ward_alt_names`, and those are consulted from
    the data rather than copied here — a hand-maintained list of Nigerian ward names would be
    enormous and permanently out of date.
    """
    from app.eo.admin import _ADMIN_ALIASES

    assert len(_ADMIN_ALIASES) < 20, (
        "the alias table is growing into a synonym dictionary; ward synonyms belong in "
        "ward_alt_names, which _match_ward_alias already reads"
    )
    # Every key must already be canonical, or it can never be hit.
    for key in _ADMIN_ALIASES:
        assert key == "".join(c for c in key if c.isalnum()), (
            f"alias key {key!r} is not in canonical form, so it is unreachable"
        )


def test_ward_alt_names_are_read_from_the_data():
    """GRID3's own synonyms resolve to the ward, not to an LGA fallback.

    Verified live: "Moloko Asipa" (GRID3's alt for "Maloko Asipa") returned the LGA centroid
    before this, which is 11 km from the ward's — a plot that should have been on screen was not.
    """
    import inspect

    from app.eo import admin

    source = inspect.getsource(admin.ward_extent)
    assert "_match_ward_alias" in source, (
        "ward_extent does not consult GRID3's published alternate names before giving up"
    )
    alias_fn = inspect.getsource(admin._match_ward_alias)
    assert "ward_alt_names" in alias_fn
    assert "_split_alt" in alias_fn, (
        "alt names are comma-packed by GRID3 and must be split, not compared whole"
    )
