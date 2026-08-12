"""Intelligence-track activation, and the honesty rule that governs it.

Distinct from `test_tracks.py`, which covers `app/dispatch/tracks.py` — the per-measurement report
card modules. This one covers `app/iam/tracks.py`: what a workspace can *activate*, and whether
activating it delivers anything.

## The invariant

**A track that cannot deliver must say so, in every surface that offers it.** The failure this
prevents is not a crash: it is an aggregator flipping a switch labelled "Public Health
Intelligence", receiving nothing for a season, and concluding the platform is broken rather than
that the capability was never claimed. `docs/frontend-journey-review.md` §2.2 recorded that as a
real defect, which is why activation carries a `deliverable` flag rather than a boolean.

The Financial track raises the stakes twice over. It is the broadest — eight distinct capabilities
at four different stages — and it is the one whose outputs affect **credit decisions**, where being
wrong is harder for the subject to appeal than a flood alert that did not arrive. So it carries a
per-capability breakdown, and these tests keep that breakdown from decaying into marketing.
"""

from __future__ import annotations

import pathlib

import pytest

from app.iam import tracks as tracks_mod
from app.iam.tracks import (
    FINANCIAL_CAPABILITIES,
    TRACK_DELIVERABLE,
    TRACK_HAZARDS,
    TRACK_INFO,
    Track,
    hazards_for,
    undeliverable,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"


# --------------------------------------------------------------------------- #
# Every track is fully declared
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("track", list(Track))
def test_every_track_is_declared_in_every_table(track):
    """A member added to the enum and nowhere else would `KeyError` at request time.

    `list_tracks` iterates the enum and indexes all three tables, so a partial addition breaks the
    endpoint for every caller rather than degrading the new track.
    """
    assert track in TRACK_HAZARDS
    assert track in TRACK_DELIVERABLE
    assert track in TRACK_INFO
    for field in ("label", "summary", "notes"):
        assert TRACK_INFO[track][field].strip(), f"{track.value} has an empty {field}"


@pytest.mark.parametrize("track", list(Track))
def test_deliverable_agrees_with_the_hazard_set(track):
    """**The two must not disagree**, in either direction.

    `deliverable` is a separate flag rather than `bool(TRACK_HAZARDS[track])` so a track can be
    marked undeliverable for a reason *other* than an empty hazard set — an unwired upstream, say.
    That freedom is what makes this test necessary: the flag could drift into claiming delivery for
    a track with nothing to alert on.

    A track with hazards may still be undeliverable. A track with NO hazards can never deliver.
    """
    if not TRACK_HAZARDS[track]:
        assert TRACK_DELIVERABLE[track] is False, (
            f"{track.value} has no primary hazard, so it cannot deliver alerts"
        )


def test_an_undeliverable_track_says_so_in_its_notes():
    """The caveat must be in the text a human reads, not only in a boolean.

    The portal renders `notes` inline under the switch. A flag with no explanation leaves the UI to
    invent the wording, which is how "not delivering yet" becomes "coming soon" and then disappears.
    """
    for track in Track:
        if TRACK_DELIVERABLE[track]:
            continue
        notes = TRACK_INFO[track]["notes"]
        assert "NOT YET DELIVERABLE" in notes, (
            f"{track.value} is undeliverable; its notes must state that plainly"
        )


def test_hazards_for_ignores_an_unknown_track():
    """A workspace document written by a later build must not break alerting for this one.

    Dropping an unknown track degrades coverage; raising would stop delivery entirely.
    """
    assert hazards_for(["agricultural", "not_a_real_track"]) == TRACK_HAZARDS[Track.AGRICULTURAL]


def test_the_financial_track_delivers_nothing_yet():
    """Explicit, because this is the track most likely to be quietly promoted.

    Two of its capabilities are measurable today, which makes "just switch it on" tempting. Until
    there is a lender-facing surface and a reviewed scoring composition, activating it must change
    nothing — and `hazards_for` must not return anything for it, or the Herald would start filtering
    alerts into a track that has no output.
    """
    assert TRACK_DELIVERABLE[Track.FINANCIAL] is False
    assert TRACK_HAZARDS[Track.FINANCIAL] == frozenset()
    assert hazards_for(["financial"]) == frozenset()
    assert undeliverable(["financial"]) == [Track.FINANCIAL]


# --------------------------------------------------------------------------- #
# The Financial capability breakdown
# --------------------------------------------------------------------------- #

VALID_STATUSES = {"ready", "feasible", "partial", "blocked"}


def test_every_capability_is_fully_stated():
    """No placeholder entries. Each needs a status a reader can act on and a stated blocker.

    An entry with an empty `blocked_by` is the failure mode here: it reads as complete while saying
    nothing about what stands in the way, which is the only part that carries information for a
    capability that is not `ready`.
    """
    assert FINANCIAL_CAPABILITIES, "the Financial track must declare its capabilities"

    keys = [c["key"] for c in FINANCIAL_CAPABILITIES]
    assert len(keys) == len(set(keys)), f"duplicate capability keys: {keys}"

    for cap in FINANCIAL_CAPABILITIES:
        assert cap["status"] in VALID_STATUSES, f"{cap['key']}: bad status {cap['status']!r}"
        for field in ("key", "label", "detail", "blocked_by"):
            assert cap[field].strip(), f"{cap['key']} has an empty {field}"


def test_no_capability_claims_to_be_live():
    """**`ready` means "measurable today, needs a surface" — never "you can use it".**

    The whole track is `deliverable: False`, so a capability status must not contradict that. There
    is deliberately no `live` status in the vocabulary: adding one would let this list assert
    delivery that `TRACK_DELIVERABLE` denies, and the two would disagree with no test to catch it.
    """
    assert "live" not in VALID_STATUSES
    for cap in FINANCIAL_CAPABILITIES:
        assert cap["status"] != "live"


def test_the_consent_dependent_capabilities_are_blocked():
    """**The one that must never quietly flip.**

    Residency timelines and HR-tech support both need consented location streams, and there is no
    consent primitive in this platform — no consent record, no lawful basis, no purpose limitation,
    no per-subject retention. Under the NDPR that layer is built *before* ingest: ingest-then-harden
    produces a compliance problem that cannot be retrofitted.

    So these two are asserted `blocked` by name. If someone ships GPS ingest, this test fails and
    the failure is the conversation about consent that has to happen first.
    """
    by_key = {c["key"]: c for c in FINANCIAL_CAPABILITIES}

    for key in ("residency_timeline", "hr_background_support"):
        assert key in by_key, f"{key} must remain declared"
        assert by_key[key]["status"] == "blocked", (
            f"{key} depends on consented location data; the consent layer does not exist yet"
        )
        assert "consent" in by_key[key]["blocked_by"].lower()


def test_the_hr_capability_records_its_safeguards_with_itself():
    """Not in a separate document nobody reads at build time.

    The brief's safeguards are a schema, not a policy appendix: explicit consent, minimum necessary
    data, no inference of sensitive characteristics, human review of every anomaly, and never a
    standalone basis for an employment decision. Keeping them beside the capability is what makes
    them visible to whoever eventually implements it.
    """
    hr = next(c for c in FINANCIAL_CAPABILITIES if c["key"] == "hr_background_support")
    blocked_by = hr["blocked_by"].lower()

    for requirement in ("consent", "human review", "standalone"):
        assert requirement in blocked_by, f"the HR safeguards must mention {requirement!r}"


def test_the_measurement_floor_is_stated_rather_than_worked_around():
    """`MIN_AOI_HECTARES` is physics, and the track notes must not imply otherwise.

    Sentinel is 10 m/pixel, so 0.5 ha is ~50 pixels and a typical urban plot (0.02-0.05 ha) is an
    order of magnitude below it. "Verify this house" is therefore not a pixel measurement. The
    honest framing — vector and contextual data rather than imagery — belongs in the notes, because
    a reader who does not see it will reasonably assume we monitor buildings.
    """
    notes = TRACK_INFO[Track.FINANCIAL]["notes"]
    assert "MIN_AOI_HECTARES" in notes
    assert "0.5" in notes


# --------------------------------------------------------------------------- #
# The frontend must not render a raw key
# --------------------------------------------------------------------------- #


def test_the_frontend_knows_every_track():
    """A backend member the frontend has never heard of renders as `financial` in the UI.

    `Track` in `lib/types.ts` is a server-driven interface, so the *payload* needs no change — but
    `lib/intelligence.ts` keeps a hardcoded `Track` union and a `TRACK_META` table keyed on it, and
    an unlisted member makes `TRACK_META[track]` undefined at the two call sites in `AreaManager`.
    """
    if not FRONTEND.exists():  # pragma: no cover - backend-only checkout
        pytest.skip("no frontend checkout")

    source = (FRONTEND / "lib/intelligence.ts").read_text()
    for track in Track:
        assert f'"{track.value}"' in source, (
            f"{track.value} is missing from the Track union in lib/intelligence.ts"
        )
        assert f"{track.value}: {{" in source, (
            f"{track.value} is missing from TRACK_META, so the portal renders its raw key"
        )


def test_the_frontend_carries_no_copy_of_the_capability_breakdown():
    """Server-driven, for the same reason `Track` itself is.

    A TypeScript copy would drift from `FINANCIAL_CAPABILITIES` the moment one capability ships, and
    the drift reads as a working feature. The portal renders `track.capabilities` from the payload.
    """
    if not FRONTEND.exists():  # pragma: no cover
        pytest.skip("no frontend checkout")

    source = (FRONTEND / "lib/intelligence.ts").read_text()
    for cap in FINANCIAL_CAPABILITIES:
        assert cap["key"] not in source, (
            f"{cap['key']} is duplicated in the frontend; render it from the API instead"
        )


def test_the_api_publishes_the_capabilities():
    """End to end, because the model could carry them and the route could forget to populate it."""
    from fastapi.testclient import TestClient

    from app.api.routes import iam as iam_routes
    from app.iam.deps import current_account
    from app.main import app

    class _Account:
        id = "acct_test"

    app.dependency_overrides[current_account] = lambda: _Account()
    original = iam_routes._require_store
    iam_routes._require_store = lambda: None
    try:
        payload = TestClient(app).get("/shelter/v1/api/iam/tracks").json()
    finally:
        iam_routes._require_store = original
        app.dependency_overrides.pop(current_account, None)

    by_value = {t["value"]: t for t in payload}
    assert set(by_value) == {t.value for t in Track}

    financial = by_value["financial"]
    assert financial["deliverable"] is False
    assert len(financial["capabilities"]) == len(FINANCIAL_CAPABILITIES)

    # Every other track sends an empty list rather than omitting the field, so a client never has
    # to guard before iterating.
    for value, track in by_value.items():
        if value != "financial":
            assert track["capabilities"] == []


def test_tracks_mod_exports_what_the_route_imports():
    """Guards the import surface the API depends on."""
    for name in ("Track", "TRACK_INFO", "TRACK_HAZARDS", "TRACK_DELIVERABLE", "FINANCIAL_CAPABILITIES"):
        assert hasattr(tracks_mod, name), f"app.iam.tracks must export {name}"


# --------------------------------------------------------------------------- #
# CBN PoS geo-fencing — the arithmetic that decides whether this is buildable
# --------------------------------------------------------------------------- #


def test_the_cbn_geofence_radius_is_measurable_only_at_70_metres():
    """**The number the whole capability rests on**, asserted rather than trusted to a docstring.

    The Central Bank of Nigeria requires PoS terminals to be geo-fenced to their registered
    address, enforcement from 2026-08-01. The radius was originally **10 m** and was relaxed to
    **70 m** after operator feedback about practical hurdles.

    That relaxation is what makes satellite verification of the anchor possible at all:

        10 m ->     314 m2 = 0.0314 ha ->   ~3 Sentinel pixels   BELOW the floor
        70 m ->  15,394 m2 = 1.5394 ha -> ~153 Sentinel pixels   above the floor

    Below ~50 pixels a fraction is dominated by edge effects and geolocation error, so a 10 m disc
    could only ever yield a precise-looking meaningless number. If `MIN_AOI_HECTARES` is ever
    raised above ~1.5 ha, this capability stops being measurable and this test is where that
    surfaces — rather than in a demo that quietly reports built-up fraction over three pixels.
    """
    import math

    from app.eo.geometry import MIN_AOI_HECTARES, GeometryError, check_monitorable

    ten_m_ha = math.pi * 10**2 / 10_000
    seventy_m_ha = math.pi * 70**2 / 10_000

    assert ten_m_ha < MIN_AOI_HECTARES, "the original 10 m rule was below the measurement floor"
    with pytest.raises(GeometryError):
        check_monitorable(ten_m_ha)

    assert seventy_m_ha > MIN_AOI_HECTARES
    check_monitorable(seventy_m_ha)  # must not raise

    # ~153 pixels at 10 m/pixel. Stated so a change to the floor shows its consequence in pixels,
    # which is the unit the limit is actually about.
    assert int(seventy_m_ha * 100) == 153


def test_the_pos_capability_does_not_claim_transaction_time_enforcement():
    """**Scope discipline, and here it protects the OPERATOR rather than us.**

    The directive needs three things: the terminal transmits dual-frequency GPS at transaction
    time, the coordinates are compared against the registered address, and the PSP's switch flags
    or declines outside the radius. The first is hardware we do not make; the third is a payment
    path we must not sit in; the second is a haversine any PSP writes themselves.

    What nobody answers at national scale is whether the ANCHOR is genuine — and geofencing a
    terminal against a fake address enforces nothing, since the terminal sits happily within 70 m
    of a coordinate in the middle of a swamp. Ghost and cloned terminals are the stated reason for
    the directive, so the anchor is the gap worth filling.

    A fintech that believed we provided the enforcement leg could fail an audit on our wording.
    So the limit is asserted in the shipped text, not left to a sales conversation.
    """
    cap = next(c for c in FINANCIAL_CAPABILITIES if c["key"] == "pos_terminal_anchor")
    blocked_by = cap["blocked_by"].lower()

    assert "anchor, not the transaction" in blocked_by
    for expected in ("dual-frequency", "haversine", "payment path"):
        assert expected in blocked_by, f"the scope limit must name {expected!r}"

    # And it must not describe itself as the compliance service.
    assert "compliance service" not in cap["detail"].lower()


def test_the_pos_capability_records_the_enforcement_date():
    """A dated regulatory deadline belongs beside the capability it drives."""
    cap = next(c for c in FINANCIAL_CAPABILITIES if c["key"] == "pos_terminal_anchor")
    assert "2026-08-01" in cap["blocked_by"]
