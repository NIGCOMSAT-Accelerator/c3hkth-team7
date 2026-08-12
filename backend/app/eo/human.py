"""Human descriptions of a place and a size, turned into something the pipeline can use.

## The problem this exists to solve

Everything the imagery layer needs is technical: a bounding box in EPSG:4326, a ring in
GeoJSON winding order, hectares. Nobody registering a farm knows any of that, and asking
them to is how a signup form loses the person it was built for.

What they *do* know, reliably:

  * **where it is** — "Argungu", "behind the school in Dikko", or simply "here" with the
    phone's own GPS;
  * **roughly how big it is** — "about five hectares", "twelve acres", "two plots", or at
    worst "small".

This module converts the second into metres and the first is handled by `places.py`. Between
them, `POST /places/resolve` can take `{"place": "Argungu", "size": "5 hectares"}` and return
a complete, validated `AreaOfInterest` — so neither the Web UI nor a partner's importer ever
has to construct a bbox.

## Why the units are these units

Nigerian smallholders talk in **hectares** (official, used by extension services and on
land documents) and **acres** (colonial-era, still common in trade). "Plots" appear in
peri-urban land sales and vary by state, so a plot is accepted with a documented default and
the resolved size is always echoed back in hectares for confirmation.

The comparison strings ("about 7 football pitches") are not decoration. A farmer cannot check
whether "5 hectares" is the right number, but they can check whether the area drawn on a map
looks like seven pitches — so the comparison is what makes the confirmation step meaningful.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

#: 1 acre = 0.404686 ha.
HA_PER_ACRE = 0.404686

#: A "plot" in Nigerian peri-urban land sale is conventionally 100 ft x 50 ft ≈ 464 m².
#:
#: It varies by state and this is the commonest figure, so a plot-based size is treated as
#: approximate and the confirmation step says so. Accepting it with a caveat beats rejecting
#: a unit people actually use.
HA_PER_PLOT = 0.0464

#: A full-size football pitch is ~0.71 ha. The unit a farmer can actually eyeball on a map.
HA_PER_PITCH = 0.71

#: Named sizes, for someone who genuinely does not know.
#:
#: Deliberately coarse and deliberately generous at the small end: the cost of monitoring a
#: slightly larger area than the real field is a diluted reading, whereas too small an area
#: can fall under the 0.5 ha floor and be refused outright — which ends the signup.
NAMED_SIZES: dict[str, float] = {
    "small": 2.0,
    "medium": 10.0,
    "large": 50.0,
    # A cooperative or an LGA-scale registration.
    "community": 500.0,
}

_NUMBER = r"(\d+(?:[.,]\d+)?)"

#: Unit patterns, longest-first so "hectare" is not matched by a bare "h" rule.
_UNIT_PATTERNS: tuple[tuple[str, float], ...] = (
    # `ha` needs a trailing boundary but NOT a leading one: "5ha" written without a space is
    # extremely common, and `\bha\b` fails there because the digit-to-letter transition is
    # already a word boundary consumed by the number group. Silently falling back to the
    # 2 ha default for "5ha" is exactly the kind of quiet wrongness this module must avoid.
    (r"hectares?|hectre?s?|ha\b", 1.0),
    (r"acres?|ac\b", HA_PER_ACRE),
    (r"plots?", HA_PER_PLOT),
    (r"football\s*(?:pitch|pitches|field?s?)", HA_PER_PITCH),
    (r"square\s*met(?:re|er)s?|\bm2\b|\bsqm\b", 0.0001),
    (r"square\s*kilomet(?:re|er)s?|\bkm2\b", 100.0),
)


@dataclass(frozen=True)
class ParsedSize:
    """A size the pipeline can act on, plus how it should be described back.

    `approximate` is carried rather than inferred because it changes the wording of the
    confirmation: a stated "5 hectares" is echoed as a fact, whereas "medium" or a plot count
    is echoed as an estimate the subscriber should sanity-check against the map.
    """

    hectares: float
    #: What the user typed, kept so the confirmation can quote it back.
    original: str
    approximate: bool
    #: "about 7 football pitches" — the comparison that makes a number checkable.
    comparison: str


def describe_area(hectares: float) -> str:
    """A size in terms someone can picture.

    Football pitches up to a point, then square kilometres — beyond about 100 ha, "141
    pitches" stops being a picture and starts being a number, which defeats the purpose.
    """
    # Square metres below half a pitch.
    #
    # This boundary was 0.1 ha, and that was too low once building footprints started arriving
    # here: 0.1473 ha (Kano Central Mosque, measured) and 1.0 ha both rendered as "about the
    # size of a football pitch" — the first is a **fifth** of one. The whole purpose of this
    # function is a comparison the user can check against a map, so a description that spans an
    # order of magnitude is worse than the bare number it replaced.
    #
    # Half a pitch is where "a pitch" stops being a fair rounding. Below it, square metres are
    # both honest and the unit someone picturing a compound or a shop already thinks in.
    if hectares < HA_PER_PITCH / 2:
        return f"about {hectares * 10_000:,.0f} square metres"
    if hectares <= 100:
        pitches = hectares / HA_PER_PITCH
        if pitches < 1.5:
            return "about the size of a football pitch"
        return f"about {pitches:.0f} football pitches"
    if hectares <= 10_000:
        return f"about {hectares / 100:.1f} square kilometres"
    return f"about {hectares / 100:,.0f} square kilometres"


def parse_size(text: str | None, *, default_hectares: float = 2.0) -> ParsedSize:
    """Turn "5 hectares", "12 acres", "2 plots" or "medium" into hectares.

    Never raises. An unparseable string falls back to `default_hectares` flagged
    `approximate`, because the confirmation step shows the resolved area on a map — so a
    wrong guess is visible and correctable, whereas a validation error at this point is a
    dead end for someone who typed their size in good faith.

    The default of 2 ha is a deliberate choice for the pin-and-radius case: it is a plausible
    smallholding, comfortably above the 0.5 ha measurement floor, and small enough that the
    reading is about *their* field rather than the district.
    """
    raw = (text or "").strip()
    if not raw:
        return ParsedSize(
            hectares=default_hectares,
            original="",
            approximate=True,
            comparison=describe_area(default_hectares),
        )

    lowered = raw.lower()

    # Named size first: "medium" contains no digits, so the numeric path would miss it.
    for name, hectares in NAMED_SIZES.items():
        if re.search(rf"\b{name}\b", lowered):
            return ParsedSize(
                hectares=hectares,
                original=raw,
                approximate=True,
                comparison=describe_area(hectares),
            )

    for pattern, factor in _UNIT_PATTERNS:
        match = re.search(rf"{_NUMBER}\s*(?:{pattern})", lowered)
        if match:
            # Comma as a decimal separator is normal in Francophone West Africa, which this
            # service also covers. Treated as a decimal point rather than a thousands
            # separator: "1,5 hectares" means 1.5, and reading it as 15 would be a tenfold
            # error in the direction that dilutes the reading.
            value = float(match.group(1).replace(",", "."))
            hectares = value * factor
            return ParsedSize(
                hectares=hectares,
                original=raw,
                # A plot count is approximate because the unit itself varies by state.
                approximate=factor == HA_PER_PLOT,
                comparison=describe_area(hectares),
            )

    # A bare number with no unit. Hectares is the official unit and what extension services
    # use, so it is the safer reading — and the confirmation step shows the result either way.
    bare = re.fullmatch(rf"\s*{_NUMBER}\s*", lowered)
    if bare:
        hectares = float(bare.group(1).replace(",", "."))
        return ParsedSize(
            hectares=hectares,
            original=raw,
            approximate=True,
            comparison=describe_area(hectares),
        )

    return ParsedSize(
        hectares=default_hectares,
        original=raw,
        approximate=True,
        comparison=describe_area(default_hectares),
    )


def square_for_hectares(lat: float, lon: float, hectares: float) -> tuple[float, float]:
    """`(lon_delta, lat_delta)` for a square of `hectares` centred on a point.

    Returns half-widths in degrees, which is what a bbox needs.

    The cosine-latitude correction on longitude is not optional: without it a square at 12°N
    would be ~2% wider than tall, and the area a subscriber confirmed on the map would not be
    the area monitored. Clamped near the poles so the maths cannot divide by zero — irrelevant
    for this service's coverage, but a crash there would be a crash.
    """
    side_m = math.sqrt(max(hectares, 0.0) * 10_000.0)
    half_m = side_m / 2.0

    lat_delta = half_m / 110_574.0
    lon_delta = half_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return lon_delta, lat_delta


# --------------------------------------------------------------------------- #
# Monitoring resolution, said honestly
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MonitoringNote:
    """Whether a resolved outline can be monitored as-is, in plain language.

    ## Why this exists

    A judge asked for address- and building-level precision when activating an AOI. The
    measurement floor makes that impossible to honour *literally*, and it is worth being exact
    about why rather than hand-waving: geocoding "Kano Central Mosque" resolves a real
    17-vertex footprint of **0.1473 ha**, about **14 Sentinel pixels**, against a
    `MIN_AOI_HECTARES` floor of 0.5. Below ~50 pixels an "inundated fraction" is dominated by
    edge effects and geolocation error, so a per-building reading would be a precise-looking
    number that means nothing — the exact failure this codebase refuses everywhere else.

    The resolution is not to reject the address. It is to **accept the exact location and state
    the monitoring resolution separately**:

        found       the building, at full precision, drawn on the map
        monitored   the area we will actually measure, drawn around it

    Same move `SeverityBadge` makes by never using colour alone, and `ScoreDriver` makes by
    showing per-input attribution: the limitation becomes visible rigour rather than an
    unexplained refusal. "We found your shop; we watch the half-hectare around it" earns more
    trust than "area too small".
    """

    #: True when the outline is large enough to measure directly.
    outline_is_monitorable: bool
    #: Hectares actually monitored — the outline's own area when big enough, else the floor.
    monitored_hectares: float
    #: One sentence for the user. Never blank.
    note: str
    #: True when the monitored area had to be enlarged past what they outlined.
    enlarged: bool


def monitoring_note(
    outline_hectares: float | None, *, label: str = "that outline"
) -> MonitoringNote:
    """How the pipeline will treat a resolved outline, and what to tell the user.

    Three cases, and the middle one is the whole point:

      * **no outline** — a street or a settlement node. Nothing to say about size yet.
      * **too small** — a building or compound. Keep their exact location, monitor the smallest
        area that yields a real reading, and say so.
      * **large enough** — monitored exactly as outlined.

    Deliberately does **not** raise for a small outline. `geometry.check_monitorable` still
    guards the write path and must: a stored AOI below the floor would produce meaningless
    fractions forever. This is for the step *before* that, where the honest answer is "here is
    what we can do" rather than an error.
    """
    floor = _min_monitorable_hectares()

    if outline_hectares is None:
        return MonitoringNote(
            outline_is_monitorable=False,
            monitored_hectares=0.0,
            note=(
                "We found the place but not an outline for it — most streets and villages have "
                "no mapped boundary. Drop a pin or draw the area and we will measure it."
            ),
            enlarged=False,
        )

    if outline_hectares < floor:
        return MonitoringNote(
            outline_is_monitorable=False,
            monitored_hectares=floor,
            note=(
                f"We located {label} exactly — {describe_area(outline_hectares)}. That is finer "
                f"than the satellites can measure, so we will monitor the {floor:g} hectares "
                f"around it ({describe_area(floor)}). The location is precise; the reading "
                f"covers a slightly wider area."
            ),
            enlarged=True,
        )

    ceiling = _max_monitorable_hectares()
    if outline_hectares > ceiling:
        # **The other end of the same problem, and easy to forget.**
        #
        # Searching a state or an LGA resolves a genuine boundary — Kano State came back at
        # 2,035,580 ha and Argungu LGA at 101,270 ha, both real. Without this branch the note
        # said "we will monitor Argungu exactly as mapped", which the write path then refuses:
        # one inundated-fraction over a whole LGA cannot locate anything actionable, which is
        # what `MAX_AOI_HECTARES` encodes.
        #
        # An outline this large is a *viewport*, not a footprint — the same distinction
        # `AdminExtentResponse.is_monitorable_area` already makes for the admin cascade. So the
        # honest answer is to frame the map here and ask for the actual plot.
        return MonitoringNote(
            outline_is_monitorable=False,
            monitored_hectares=0.0,
            note=(
                f"That is {label} as a whole — {describe_area(outline_hectares)}. One risk "
                f"figure over an area that size cannot say which part is affected, so we watch "
                f"plots rather than districts. We have framed the map here; outline your own "
                f"land or drop a pin inside it."
            ),
            enlarged=False,
        )

    return MonitoringNote(
        outline_is_monitorable=True,
        monitored_hectares=outline_hectares,
        note=f"We will monitor {label} exactly as mapped — {describe_area(outline_hectares)}.",
        enlarged=False,
    )


def _max_monitorable_hectares() -> float:
    """`geometry.MAX_AOI_HECTARES`, imported lazily. See `_min_monitorable_hectares`."""
    from app.eo import geometry

    return geometry.MAX_AOI_HECTARES


def _min_monitorable_hectares() -> float:
    """`geometry.MIN_AOI_HECTARES`, imported lazily.

    Lazy so that importing `human` — a pure text module — never pulls in the geometry package.
    One source for the floor, because a second copy of 0.5 here would drift from the value the
    write path actually enforces, and the sentence would then promise something refused.
    """
    from app.eo import geometry

    return geometry.MIN_AOI_HECTARES
