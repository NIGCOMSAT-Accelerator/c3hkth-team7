"""SMAP soil moisture — the grid maths, the fill rule, and the irrigation bands.

Every test here is about a way this adapter can be WRONG WITHOUT FAILING, which is the whole hazard
of a gridded read: an incorrect index still returns a plausible number from the wrong place on Earth,
and a fill value still parses as a float. Neither raises, so nothing downstream can notice.
"""

import pytest

# --------------------------------------------------------------------------- #
# The projection
#
# This is the defect that motivated the module. SMAP is on EASE-Grid 2.0, an equal-AREA projection,
# so latitude is NOT linear in row index. The obvious linear formula put Kano's cell 4.35 degrees
# (~480 km) away and returned 0.264 m3/m3 — an entirely believable figure, from Ibadan.
# --------------------------------------------------------------------------- #


def test_the_grid_is_not_indexed_linearly_in_latitude():
    """The linear approximation must not accidentally still be in use.

    Verified live against the 2026-08-09 granule: the correct row for Kano (11.96N) is 643, and the
    naive `(90 - lat) / 180 * n_rows` gives 704. If someone "simplifies" `_grid_cell` back to
    arithmetic, this fails.
    """
    from app.eo.soil_moisture import EASE2_ROWS, _grid_cell

    row, column = _grid_cell(11.96, 8.5)

    naive_row = int((90.0 - 11.96) / 180.0 * EASE2_ROWS)
    assert naive_row == 704, "the naive formula changed; update what this test contrasts against"
    assert row == 643, f"expected the EASE2 row 643 for Kano, got {row}"
    assert row != naive_row


@pytest.mark.parametrize(
    ("latitude", "longitude", "row", "column"),
    [
        # All three verified live: the granule's own latitude/longitude arrays read back within
        # 0.05 degrees of each of these, which is half a 9 km cell — pure quantisation.
        (11.96, 8.5, 643, 2019),   # Kano, Sahel
        (6.62, 3.51, 718, 1965),   # Ikorodu, Lagos
        (4.93, 6.33, 742, 1995),   # Yenagoa, Niger Delta
    ],
)
def test_known_cells_match_the_live_verified_indices(latitude, longitude, row, column):
    from app.eo.soil_moisture import _grid_cell

    assert _grid_cell(latitude, longitude) == (row, column)


def test_indices_stay_inside_the_grid_at_the_extremes():
    """A pole or an antimeridian AOI must not index out of bounds.

    EASE-Grid 2.0 does not reach the poles — the projection is undefined there — so the clamp is
    what stops a `[1624:1625]` constraint that would either error or, worse, wrap.
    """
    from app.eo.soil_moisture import EASE2_COLUMNS, EASE2_ROWS, _grid_cell

    for latitude, longitude in ((89.9, 179.9), (-89.9, -179.9), (0.0, 0.0), (85.0, 180.0)):
        row, column = _grid_cell(latitude, longitude)
        assert 0 <= row < EASE2_ROWS
        assert 0 <= column < EASE2_COLUMNS


def test_the_location_tolerance_admits_quantisation_but_not_a_projection_error():
    """The guard must be loose enough for a correct read and tight enough to catch the real bug.

    A correct lookup can sit up to half a cell away (~0.06 deg). The linear-index bug was 4.35 deg.
    A tolerance anywhere between those two works; one outside them breaks in one direction or the
    other, so both bounds are asserted.
    """
    from app.eo.soil_moisture import MAX_LOCATION_ERROR_DEG

    half_cell_deg = 4.5 / 111.0
    assert MAX_LOCATION_ERROR_DEG > half_cell_deg, (
        "tolerance is tighter than the grid's own quantisation; correct reads would be rejected"
    )
    assert MAX_LOCATION_ERROR_DEG < 4.35, (
        "tolerance would have admitted the 480 km linear-index error this guard exists to catch"
    )


# --------------------------------------------------------------------------- #
# Fill values
# --------------------------------------------------------------------------- #


def test_the_parser_keeps_fill_values_for_the_caller_to_judge():
    """Unlike the rainfall parser, this one must NOT filter.

    `rainfall._parse_dap_csv_mean` drops fill because for rainfall a fill and a zero both mean "no
    rain measured". Here they do not: 0.0 m3/m3 is oven-dry soil and -9999 is no observation, and
    collapsing them would report a desert where the satellite simply did not look.
    """
    from app.eo.soil_moisture import _parse_dap_csv

    body = (
        "Dataset: SMAP_L3_SM_P_E_20260809_R19240_001.h5\n"
        "/Soil_Moisture_Retrieval_Data_AM/soil_moisture[643][2019], 0.311, -9999\n"
        "/Soil_Moisture_Retrieval_Data_AM/soil_moisture[644][2019], 0.298, 0.0\n"
    )
    values = _parse_dap_csv(body)

    assert -9999.0 in values, "fill was dropped; the caller can no longer tell dry from unobserved"
    assert 0.0 in values
    assert 0.311 in values


def test_a_fill_cell_never_averages_into_the_result():
    """One -9999 in a 2x2 block would report roughly -2500 m3/m3 if averaged.

    The bound is the physical one — volumetric water content is m3 of water per m3 of soil, so it
    cannot exceed 1 — rather than a magic-number comparison against -9999.
    """
    from app.eo.soil_moisture import _VALID_MAX, _VALID_MIN

    block = [0.311, -9999.0, 0.298, -9999.0]
    measured = [v for v in block if _VALID_MIN <= v <= _VALID_MAX]

    assert measured == [0.311, 0.298]
    assert abs(sum(measured) / len(measured) - 0.3045) < 1e-6


# --------------------------------------------------------------------------- #
# The advisory semantics — what a farmer is actually told
# --------------------------------------------------------------------------- #


def test_unknown_wetness_yields_no_instruction_at_all():
    """The rule this whole module is subordinate to: absence must not become a claim.

    An unavailable reading defaults `volumetric` to 0.0, which if read naively is "bone dry" and
    would produce a confident `irrigate` from no measurement. `available` gates it.
    """
    from app.models.schemas import SoilMoisture

    unknown = SoilMoisture()

    assert unknown.available is False
    assert unknown.status == "unknown"
    assert unknown.irrigation_advice is None, (
        "an unmeasured field was given an irrigation instruction"
    )


@pytest.mark.parametrize(
    ("volumetric", "status", "advice"),
    [
        (0.05, "very_dry", "irrigate"),
        (0.15, "dry", "irrigate"),
        (0.28, "adequate", "hold"),
        # Kano, measured 2026-08-09 — mid-wet-season Sahel, no irrigation needed.
        (0.311, "adequate", "hold"),
        (0.38, "wet", "hold"),
        # Yenagoa, measured the same day. Irrigating this would drown the roots.
        (0.593, "saturated", "drain"),
    ],
)
def test_measured_wetness_maps_to_the_expected_advice(volumetric, status, advice):
    from app.models.schemas import SoilMoisture

    reading = SoilMoisture(volumetric=volumetric, observed_date="2026-08-09", available=True)

    assert reading.status == status
    assert reading.irrigation_advice == advice


def test_saturation_advises_draining_rather_than_holding():
    """Saturated is not just "very wet" — it is a different instruction.

    At saturation the pore space is full and roots are anaerobic, which is the same waterlogging
    damage a flood causes. `hold` would let a farmer keep a field in that state; `drain` is an action.
    """
    from app.eo.soil_moisture import SATURATION
    from app.models.schemas import SoilMoisture

    assert SoilMoisture(volumetric=SATURATION, available=True).irrigation_advice == "drain"
    assert SoilMoisture(volumetric=SATURATION - 0.01, available=True).irrigation_advice == "hold"


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def test_soil_moisture_has_no_fallback_source_declared():
    """Nothing else measures soil water, so a fallback would be a substitution.

    Every other source in the registry chains to a backup. This one must not: rainfall is a
    different physical quantity, and quietly answering "how wet is the soil" with "how much rain
    fell" is the implied-claim failure the platform has already committed twice.
    """
    from app.eo import sources

    assert sources.BY_KEY["smap-l3"].falls_back_to is None
    assert sources.BY_KEY["smap-l3"].credential_key == "nasa_earthdata_token"


def test_the_module_does_not_import_the_raster_stack_at_module_scope():
    """`pyproj` is imported inside `_grid_cell`, matching the `eo/exposure.py` rule.

    The Oracle imports this module, and the Oracle must stay importable — and unit-testable —
    without GDAL present. A top-level `from pyproj import Transformer` compiles fine and then
    breaks `test_oracle.py` on any machine without the geospatial stack.
    """
    import pathlib

    source = pathlib.Path("app/eo/soil_moisture.py").read_text()
    header = source.split("async def _latest_granule")[0]
    module_level = [
        line
        for line in header.splitlines()
        if line.startswith(("import ", "from ")) and "pyproj" in line
    ]
    assert not module_level, f"pyproj imported at module scope: {module_level}"
    assert "from pyproj import Transformer" in source, "the lazy import went missing"
