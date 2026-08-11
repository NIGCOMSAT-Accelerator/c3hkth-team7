"""When place search finds nothing, the UI must offer a way through.

## The incident this closes

A subscriber registering "Alspecs Farms in Kobape, Ogun State" searched, got **zero results**,
and the panel rendered nothing at all — no message, no alternative. Browser geolocation had
already run on mount, so the only location the form held was the device's, and the device
reported Warrington, England. The farm was created and activated there.

Verified against Nominatim directly: OSM has no entry for "Kobape" at all, with or without
country hints, while its LGA (Obafemi Owode) resolves fine. So an empty result set is the
**normal rural case**, not an error — SHELTER's subscribers are mostly not in cities, and OSM's
Nigerian village coverage is thin.

The backend already had everything needed (GRID3 ships all 774 LGAs and was integrated for
reverse lookup). What was missing was the browse direction and the UI offering it.

These are source assertions rather than a rendered-DOM test: the failure being prevented is
someone removing the fallback or the wiring, and there is no JS test runner in this project.
`npm run build` covers whether it compiles.
"""

from __future__ import annotations

import pathlib

PICKER = pathlib.Path("../frontend/components/AreaPicker/AreaPicker.tsx")
ACTIONS = pathlib.Path("../frontend/app/subscribe/area-actions.ts")
API = pathlib.Path("../frontend/lib/api.ts")


def _read(path: pathlib.Path) -> str | None:
    """Source text, or None on a backend-only checkout."""
    return path.read_text() if path.exists() else None


def test_a_failed_search_is_distinguished_from_not_having_searched():
    """`results.length === 0` is also true before anyone types.

    Conflating the two would either show the fallback immediately (noise on every visit) or
    never (the bug). The distinction is a separate flag set only after a completed search.
    """
    source = _read(PICKER)
    if source is None:
        return

    assert "searchedInVain" in source, (
        "no state distinguishing 'searched and found nothing' from 'has not searched yet'"
    )
    # Set after the await, so the panel does not flash mid-request.
    assert "setSearchedInVain(found.length === 0)" in source, (
        "the empty-result flag must be set from a COMPLETED search's result"
    )


def test_the_fallback_offers_the_admin_cascade():
    """State → LGA is the path that works when a village is unfindable by name."""
    source = _read(PICKER)
    if source is None:
        return

    assert "adminStates" in source and "adminLgas" in source, (
        "the picker does not offer the State/LGA browse path"
    )
    assert "Find it by State and LGA instead" in source, (
        "no visible affordance for the fallback — the panel that rendered nothing is what "
        "let a Nigerian farm be registered in England"
    )


def test_the_fallback_does_not_blame_the_subscriber():
    """The map data is missing the village; the subscriber did not mistype.

    Wording matters at this step more than anywhere else in the product: it is the moment
    someone decides whether to finish signing up, and telling them their own farm's name is
    wrong is both untrue and demoralising.
    """
    source = _read(PICKER)
    if source is None:
        return

    assert "not on the map" in source, (
        "the empty-result message should explain that map coverage is thin, not imply the "
        "query was wrong"
    )

    # Scoped to the fallback panel. Elsewhere "try again" is correct advice — a failed network
    # call genuinely is worth retrying, while a missing village never will be.
    # Bounded by the panel's own class name at the start and the end of the search block. A
    # looser split ran on into the drawn-outline error copy, where "error" is correct.
    panel = source.split('className="picker__fallback"')[1].split("</div>\n      )}")[0]
    for blaming in ("invalid", "incorrect", "did not recognise", "try again", "error"):
        assert blaming not in panel.lower(), (
            f"the no-results copy uses {blaming!r}, which reads as the subscriber's fault "
            f"when the map data is what is missing"
        )


def test_choosing_an_lga_moves_the_map_and_does_not_resolve_an_area():
    """**An LGA is not a monitoring footprint.**

    Obafemi Owode spans roughly 58 x 63 km. Submitting that as an AOI would average an entire
    district into one reading and report nothing meaningful about a 140-hectare farm — and
    `POST /risk/assess` rejects anything over ~4 deg squared outright.

    So the cascade sets the map centre and stops. The pin is the next step, deliberately.
    """
    source = _read(PICKER)
    if source is None:
        return

    # The LGA handler must move the centre...
    assert "setCentre({ lat: centred.lat, lon: centred.lon })" in source, (
        "choosing an LGA does not reposition the map"
    )
    # ...and must not hand the parent form an area straight from the LGA choice.
    lga_handler = source.split("Choose an LGA")[0].split("onChange={async (e) => {")[-1]
    assert "onResolved(" not in lga_handler, (
        "the LGA choice resolves an area directly; an LGA-sized AOI is not monitorable and "
        "would produce a confident district-average reading"
    )


def test_the_server_actions_degrade_to_empty_rather_than_throwing():
    """A picker whose fallback fails must still allow a pin drop.

    Every other path in this component already returns null or [] on failure — the failure
    states are part of the design, not an afterthought.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    for fn in ("adminStates", "adminLgas", "adminCentre"):
        assert f"export async function {fn}" in source, f"{fn} is not exposed as an action"

    block = source.split("export async function adminStates")[1]
    assert "catch" in block, "the admin actions can throw, which would break the picker"


def test_the_client_only_returns_the_centroid_not_the_extent():
    """`adminCentre` deliberately drops the bbox.

    Handing a 58 x 63 km envelope back to a caller invites treating it as the area to monitor.
    The endpoint says `is_monitorable_area: false`, but the safest shape is not to return the
    tempting value at all.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    centre = source.split("export async function adminCentre")[1]
    assert "centroid_lat" in centre and "centroid_lon" in centre
    assert "bbox" not in centre, (
        "adminCentre returns a bbox; an LGA envelope must not be reachable as a submittable area"
    )


# --------------------------------------------------------------------------- #
# The confirmation card
#
# The England incident was survivable at three points and survived none of them: nothing on
# screen named the country, nothing required an acknowledgement, and no server-side check
# existed. The guard now covers the third. This covers the first two — because a guard that
# REFUSES is a worse experience than a card that lets someone notice, and because the guard
# only knows about geography while a person can also spot the wrong LGA in the right country.
# --------------------------------------------------------------------------- #


def test_the_card_states_the_country_and_district():
    """These are the two fields whose absence let a Nigerian farm be registered in England.

    Asserted as rendered LABELS rather than the underlying variables: `resolved.country` being
    referenced somewhere proves nothing about whether a person can see it.
    """
    source = _read(PICKER)
    if source is None:
        return

    # Anchored on the card's own title: `picker__confirm` is also used by the drawn-outline
    # panel, and that one comes first in the file.
    card = source.split("Check this is the right place")[1]

    assert "<dt>Country</dt>" in card, "the card does not show the country"
    assert "<dt>District</dt>" in card, "the card does not show the state/LGA"
    assert "resolved.country" in card and "resolved.admin1" in card

    # A null admin name must read as "not identified" rather than vanishing: an area with no
    # resolved LGA is itself worth seeing, since Fahis searches on those names to verify.
    assert "not identified" in card, (
        "a missing country or district renders as nothing, which hides the fact it is missing"
    )


def test_the_area_reaches_the_form_only_once_confirmed():
    """**This is what makes the card load-bearing rather than decorative.**

    If `resolve()` still called `onResolved(next)`, the card would be a notice someone could
    ignore — and the incident shows that a notice nobody must act on is a notice nobody reads.
    """
    source = _read(PICKER)
    if source is None:
        return

    import re

    resolve_fn = source.split("const resolve = useCallback(")[1].split("[onResolved],")[0]
    # Comments stripped: the code carries a comment explaining why `onResolved(next)` is absent,
    # and matching that would make the test contradict the thing it documents.
    code_only = re.sub(r"//[^\n]*", "", resolve_fn)

    assert "setResolved(next)" in code_only, "the resolved area is not stored"
    assert "onResolved(next)" not in code_only, (
        "resolve() hands the area to the parent form directly, so the confirmation card can be "
        "bypassed by simply submitting"
    )
    # The tick is what forwards it.
    assert "onResolved(e.target.checked ? resolved : null)" in source, (
        "the confirmation checkbox does not forward the area"
    )


def test_changing_the_pin_or_size_withdraws_the_confirmation():
    """A tick must not survive the thing it was ticked about changing.

    Confirming a 2-hectare plot and then nudging the pin 40 km would otherwise leave the old
    confirmation standing — the same class of staleness as a cached scene href outliving its
    token.

    Cleared BEFORE the request, not after: clearing on the response leaves a window in which a
    stale confirmation is still submittable.
    """
    source = _read(PICKER)
    if source is None:
        return

    resolve_fn = source.split("const resolve = useCallback(")[1].split("[onResolved],")[0]
    before_await = resolve_fn.split("await resolveArea")[0]

    assert "setConfirmed(false)" in before_await, (
        "the confirmation is not withdrawn before re-resolving, so a stale tick stays valid "
        "while the new area is being fetched"
    )
    assert "onResolved(null)" in before_await, (
        "the parent form keeps the previous area during a re-resolve"
    )


def test_a_drawn_outline_needs_no_confirmation_card():
    """Drawing is self-confirming, and asking twice teaches people to tick past it.

    The card exists because a GPS fix or a geocoded name can be 2,000 km wrong while still
    looking like a plausible number. Tapping corners on a visible map cannot be wrong in that
    way — the subscriber watched it happen.
    """
    source = _read(PICKER)
    if source is None:
        return

    drawn = source.split("if (preview.monitorable && preview.ring) {")[1].split("} else {")[0]

    assert "onResolved(area)" in drawn, (
        "a drawn outline no longer submits; it is self-confirming and should not be gated"
    )
    # And the reasoning must be recorded, since the asymmetry looks like an oversight.
    assert "no confirmation card" in drawn.lower(), (
        "the deliberate asymmetry between drawn and resolved areas is undocumented"
    )


# --------------------------------------------------------------------------- #
# The ward tier
#
# The LGA cascade located the right district but not the right FIELD: Obafemi Owode is 58 x 63 km
# and the reported farm sat 22.4 km from its centre, so centring there left the plot off-screen.
# Kajola ward is 18 x 16 km — a 12.9x smaller search area, 5.9 km from centre.
#
# Optional by necessity: GRID3 has wards for 24 of 37 states, and geoBoundaries publishes no ADM3
# for Nigeria, so there is nothing to fall back to in the other 13.
# --------------------------------------------------------------------------- #


def test_the_ward_step_appears_only_when_wards_exist():
    """**The constraint that shapes this whole tier.**

    Requiring a ward would make the picker unusable in Lagos, Rivers and the FCT — the opposite
    of the problem the cascade solves. So the dropdown is conditional on the list being non-empty,
    and nothing announces its absence.
    """
    source = _read(PICKER)
    if source is None:
        return

    # The GUARD itself, not a mention of it — `wards.length > 0` also appears in the comment
    # explaining why the gate exists, so matching the bare string passed even with the gate
    # removed. Verified by tampering.
    assert "{pickedLga && wards.length > 0 && (" in source, (
        "the ward dropdown is not gated on wards existing; in the 13 states with no coverage it "
        "would render an empty, unusable select"
    )
    # And absence must be silent — a "no wards found" notice would report missing upstream data
    # as though the subscriber had done something wrong.
    import re

    browse = source.split("Ward <span")[0].split('className="picker__browse"')[1]
    # JSX comments stripped: the code carries one explaining why "no wards found" copy is
    # deliberately absent, and matching that would make the test contradict what it documents.
    browse = re.sub(r"\{/\*.*?\*/\}", "", browse, flags=re.S)
    for alarming in ("no wards", "unavailable", "not found"):
        assert alarming not in browse.lower(), (
            f"the browse panel says {alarming!r}; an absent ward layer is normal for 13 states"
        )


def test_choosing_a_ward_recentres_and_does_not_resolve_an_area():
    """A ward is ~18 km across — still where to look, never what to monitor.

    Same rule as the LGA. The pin remains the step that defines the AOI, and a test pins that
    because the temptation to "just use the ward boundary" grows as the boundary gets smaller.
    """
    source = _read(PICKER)
    if source is None:
        return

    handler = source.split('id="picker-ward"')[1].split("</select>")[0]

    assert "setCentre(" in handler, "choosing a ward does not move the map"
    assert "onResolved(" not in handler, (
        "the ward choice resolves an area; an 18 x 16 km AOI would average a whole ward into "
        "one reading"
    )


def test_the_ward_layer_uses_its_own_field_names():
    """The two GRID3 layers disagree, and using the wrong one fails loudly but unhelpfully.

    Verified live: querying the ward layer with `lganame` returns "Invalid query parameters",
    not an empty result — so a shared query builder would have looked like a coverage gap rather
    than a field-name mistake.
    """
    admin_source = pathlib.Path("app/eo/admin.py").read_text()

    wards = admin_source.split("async def list_wards")[1].split("async def ward_extent")[0]
    assert "state='" in wards and "lga='" in wards, (
        "the ward query does not use the ward layer's own field names (state/lga/ward)"
    )
    assert "statename" not in wards and "lganame" not in wards, (
        "the ward query uses the LGA layer's field names, which ArcGIS rejects outright"
    )


def test_an_empty_ward_list_is_cached_too():
    """13 states will always answer "none", and re-asking ArcGIS each time is pure cost.

    Note the deliberate `isinstance` without a truthiness check — the states and LGA caches use
    truthiness because an empty answer there means a failure, while here it is the real answer.
    """
    admin_source = pathlib.Path("app/eo/admin.py").read_text()
    wards = admin_source.split("async def list_wards")[1].split("async def ward_extent")[0]

    assert "isinstance(cached, list)" in wards
    assert "isinstance(cached, list) and cached" not in wards, (
        "an empty ward list is treated as a cache miss, so the 13 uncovered states re-query "
        "ArcGIS on every visit to be told 'none' again"
    )


def test_arcgis_values_are_escaped():
    """ArcGIS takes a SQL-ish WHERE string with no parameter binding.

    GRID3 has no apostrophes in its names today, but African place names containing them exist,
    and a rename must not be able to truncate a clause.
    """
    from app.eo.admin import _sql_quote

    assert _sql_quote("N'Djamena") == "N''Djamena"
    assert _sql_quote("Ogun") == "Ogun"
