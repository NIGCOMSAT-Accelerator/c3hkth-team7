import pytest

# --------------------------------------------------------------------------- #
# ClimateSERV response parsing
#
# Three defects made the two KEYLESS rungs of a four-rung chain silently unanswerable, so every
# assessment reported "Rainfall data was unavailable for this cycle" while the upstream was working
# perfectly. All three were on our side.
# --------------------------------------------------------------------------- #


def test_progress_is_parsed_as_a_number_not_compared_as_a_string():
    """**The bug that broke the chain.**

    ClimateSERV returns `[100.0]` — a JSON array holding a FLOAT. The old check was
    `text.strip().strip('"') == "100"`, which is False for `[100.0]`, so a job that had ALREADY
    COMPLETED was never recognised and every request burned its poll budget and raised TimeoutError.

    Measured live: eleven consecutive polls all returned `[100.0]` and every one was treated as
    incomplete.
    """
    from app.eo.rainfall import _climateserv_progress

    # The real wire format.
    assert _climateserv_progress("[100.0]") == 100.0
    # And the forms the old string comparison happened to handle, which must keep working.
    assert _climateserv_progress("100") == 100.0
    assert _climateserv_progress('"100"') == 100.0
    # Mid-flight.
    assert _climateserv_progress("[42.5]") == 42.5
    # Explicit server-side rejection — distinct from slow, so the caller can fail fast.
    assert _climateserv_progress("[-1]") == -1.0
    # Unreadable is None, never 0.0 — 0% progress and "no answer" are different facts.
    assert _climateserv_progress("") is None
    assert _climateserv_progress("<html>error</html>") is None


def test_the_job_id_is_unwrapped_from_its_jsonp_envelope():
    """ClimateSERV answers `cb(["<uuid>"])`, and the brackets must not travel.

    Proven by the server's own error message when they did:

        "No such file or directory: '/mnt/cs-temp/request_out/[\\"b0470e2b-...\\"].txt'"

    It used the literal brackets and quotes as a filename. `text.strip().strip('"')` left the whole
    envelope intact, so every progress poll asked about a job that did not exist.
    """
    from app.eo.rainfall import _climateserv_job_id

    uuid = "f80ffd87-52fe-41f5-b2b4-d06a96177aaa"
    assert _climateserv_job_id(f'cb(["{uuid}"])') == uuid
    # No callback parameter — still a JSON array.
    assert _climateserv_job_id(f'["{uuid}"]') == uuid
    # A bare id must survive untouched.
    assert _climateserv_job_id(uuid) == uuid
    assert _climateserv_job_id("") is None


def test_the_antecedent_window_clears_the_publication_lag():
    """CHIRPS is a reanalysis product published in ARREARS, not a live feed.

    Measured against ClimateSERV on 2026-08-11: data exists through June 2026 and returns an EMPTY
    series for July onward. A 7-day window ending today therefore sat entirely inside the
    unpublished gap and every request came back with zero entries — the third reason the chain
    never answered.

    The lag must exceed the observed ~6-week publication delay, and must not be so large that the
    reading stops describing anything recent enough to act on.
    """
    from app.eo.rainfall import ANTECEDENT_DAYS, ANTECEDENT_LAG_DAYS

    assert ANTECEDENT_LAG_DAYS >= 42, "must clear the observed ~6-week CHIRPS publication lag"
    assert ANTECEDENT_LAG_DAYS <= 90, (
        "beyond ~3 months an antecedent reading describes a different season, not current ground "
        "wetness"
    )
    assert ANTECEDENT_DAYS < ANTECEDENT_LAG_DAYS


def test_only_the_past_window_is_lagged():
    """A forecast is published ahead of time by definition.

    Shifting the FUTURE window back by the publication lag would ask GEFS about days that have
    already happened — turning a forecast into a stale observation and silently breaking the
    forecast/antecedent distinction `RainfallOutlook.forecast_available` exists to preserve.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _climateserv(")
    body = source[start : source.index("\ndef _climateserv_job_id", start)]

    window = body[body.index("if future:") : body.index("params = {")]
    future_branch, past_branch = window.split("else:", 1)
    assert "ANTECEDENT_LAG_DAYS" not in future_branch, (
        "the forecast window must not be lagged — it would ask for days already past"
    )
    assert "ANTECEDENT_LAG_DAYS" in past_branch


def test_the_spi_baseline_is_bounded_in_time_and_years():
    """The parse fix revealed a latency cost that had been hidden by the bug.

    Each baseline year is one sequential ClimateSERV job (~7s measured). At 20 years that is ~140s
    on the first assessment per AOI — and while the progress parse was broken every job failed
    instantly, so nobody saw it. Once fixed, the Oracle stage began holding queue entries long
    enough to look stalled.

    SPI is an ENRICHMENT: without it the Oracle still has the antecedent total and every hazard
    term. So both the year count and the wall-clock budget are bounded, and a partial baseline is
    preferred to a blocked pipeline.
    """
    from app.eo.rainfall import CLIMATOLOGY_BUDGET_SECONDS, SPI_HISTORY_YEARS

    assert SPI_HISTORY_YEARS >= 10, "too few samples and the gamma fit is not meaningful"
    assert SPI_HISTORY_YEARS <= 15, (
        "each year is a sequential upstream job; beyond ~15 the first run blocks the pipeline"
    )
    assert CLIMATOLOGY_BUDGET_SECONDS <= 90


def test_the_climatology_stops_early_rather_than_blocking():
    """A partial series is used; the fetch is never allowed to run unbounded."""
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _rainfall_climatology(")
    body = source[start : source.index("\nasync def ", start + 10)]

    assert "CLIMATOLOGY_BUDGET_SECONDS" in body
    assert "break" in body
    # And the result is cached, so the cost is paid once per AOI rather than per cycle.
    assert "climatology_key" in body


def test_era5_requests_a_format_the_cds_actually_offers():
    """`data_format: "json"` was never valid.

    The CDS process schema declares `data_format: {"enum": ["grib", "netcdf"]}` — verified live
    2026-08-11. Asking for `json` meant the request could never succeed, which conveniently masked
    the result-parsing bug below.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _era5_antecedent(")
    body = source[start : source.index("\ndef _sum_numeric", start)]

    assert '"data_format": "grib"' in body
    assert '"data_format": "json"' not in body


def test_era5_never_reports_an_asset_descriptor_as_rainfall():
    """**The worst class of bug in this codebase: a fabricated number.**

    `/results` returns an asset DESCRIPTOR, not data:

        {"asset": {"value": {"type": "application/x-grib", "file:size": 550, ...}}}

    The old code called `_sum_numeric(results.json())`, which recursively totals every number in a
    nested structure — so it would have summed `file:size` and any digits in the checksum and
    reported that as MILLIMETRES OF RAINFALL. Only the invalid `data_format` kept it from firing.

    Decoding GRIB needs `cfgrib`/`eccodes`, which this module must not carry. So the rung declines
    honestly instead.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _era5_antecedent(")
    body = source[start : source.index("\ndef _sum_numeric", start)]

    # Comments are stripped before matching: the fix documents the old call by name, and a naive
    # substring search would find it in the explanation and fail on correct code.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "_sum_numeric(results.json())" not in code, (
        "summing an asset descriptor would report a byte count as rainfall"
    )
    # It must decline rather than guess.
    assert "return None" in code.split("results.raise_for_status()")[1]


def test_sum_numeric_is_no_longer_used_on_untrusted_shapes():
    """`_sum_numeric` totals every number it finds, which is only safe on a known series.

    Kept for any caller that has genuinely validated its input, but it must not be pointed at an
    arbitrary API envelope — that is precisely how a byte count becomes a rainfall figure.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # No remaining CALL passes a raw `.json()` straight into it.
    assert "_sum_numeric(results.json())" not in code
    assert "_sum_numeric(response.json())" not in code


# --------------------------------------------------------------------------- #
# IMERG — the only antecedent source with a ~2-day lag
# --------------------------------------------------------------------------- #


def test_imerg_granule_names_are_not_constructed():
    """The processing-version suffix changes without notice.

    The old adapter hard-coded `V07B`. Verified live 2026-08-11: the current granule is
    `...20260809-S000000-E235959.V07C.nc4`. Every hand-built URL eventually 404s, and it failed
    SILENTLY because the adapter swallowed per-day exceptions and returned None — indistinguishable
    from "no token configured".

    CMR publishes the exact OPeNDAP href per granule, so the filename is never guessed.
    """
    import ast
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()

    # Parse and strip every docstring, then look at what is left. The fix documents the old `V07B`
    # suffix by name in prose, so a substring search over the raw file finds it there and fails on
    # correct code — which is the trap this test fell into first time.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ) and ast.get_docstring(node):
            node.body = node.body[1:]

    code = ast.unparse(tree)
    assert "V07B" not in code, (
        "a hard-coded processing suffix will 404 when it advances"
    )
    assert "_imerg_granules" in code
    assert "cmr_search_url" in code


def test_imerg_excludes_fill_values_rather_than_averaging_them():
    """**The NaN-is-not-zero rule, applied to a text format.**

    IMERG marks no-data as a large negative fill (-9999.9). Averaging it in would report a large
    NEGATIVE rainfall, and summing that across a week would swamp the total — the same class of
    error `eo/indices.py` prevents for rasters.
    """
    from app.eo.rainfall import _parse_dap_csv_mean

    body = (
        "Dataset: 3B-DAY-L.MS.MRG.3IMERG.20260809-S000000-E235959.V07C.nc4\n"
        "/precipitation[0][0], 4.0, -9999.9, 8.0\n"
    )
    # Mean of the two REAL values, not of all three.
    assert _parse_dap_csv_mean(body) == pytest.approx(6.0)


def test_imerg_parses_the_measured_zero_case():
    """`0, 0, 0` is a measured dry day — the exact response recorded over Kano on 2026-08-09."""
    from app.eo.rainfall import _parse_dap_csv_mean

    body = (
        "Dataset: 3B-DAY-L.MS.MRG.3IMERG.20260809-S000000-E235959.V07C.nc4\n"
        "/precipitation[0][0], 0, 0, 0\n"
        "/precipitation[0][1], 0, 0, 0\n"
    )
    assert _parse_dap_csv_mean(body) == 0.0, "measured zero must be 0.0, never None"


def test_imerg_returns_none_when_there_is_nothing_to_parse():
    """No values at all is None — distinct from a measured zero, which is a finding."""
    from app.eo.rainfall import _parse_dap_csv_mean

    assert _parse_dap_csv_mean("") is None
    assert _parse_dap_csv_mean("Dataset: x.nc4\n") is None
    # All fill: nothing usable, so None rather than a negative number.
    assert _parse_dap_csv_mean("/precipitation[0][0], -9999.9, -9999.9\n") is None


def test_imerg_queries_a_range_not_a_single_cell():
    """A single-index DAP4 constraint returns a header with NO data row on this server.

    Verified live: `[0][1885][1019]` yields only `/precipitation[0][0], ` while
    `[0][1885:1887][1019:1021]` yields `0, 0, 0`. So the query must ask for a block.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _imerg_antecedent(")
    body = source[start : source.index("\ndef _parse_dap_csv_mean", start)]

    assert "xi + 2" in body and "yi + 2" in body, "must request a range, not one cell"
    # `.dap.csv` is the only format that works — .ascii/.dods return 400, .dap.json returns 500.
    assert ".dap.csv" in body

# --------------------------------------------------------------------------- #
# The forecast rung — the only forward-looking source
# --------------------------------------------------------------------------- #


def test_climateserv_is_not_used_for_the_forecast():
    """ClimateSERV serves NO GEFS, and datatype 35 was never valid.

    Verified live 2026-08-11: `getClimateScenarioInfo` lists only CCSM4 and CFSv2 ensembles, 35 is
    absent from the valid numbers, and submitting it returns progress `-1` — an explicit rejection.
    So `forecast_available` was permanently False and every assessment took the 0.75
    no-forecast confidence penalty because of our configuration, not the weather.

    The CFSv2/CCSM4 ensembles that DO exist are seasonal: asked for a 7-day window they return zero
    entries (measured on 9, 43, 51), so they cannot serve a short-range forecast either.
    """
    from app.config import Settings

    # The setting was DELETED, not repointed at a seasonal ensemble — an unread setting that looks
    # like a forecast control is how the original defect survived.
    assert "climateserv_forecast_datatype" not in Settings.model_fields

    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def rainfall_outlook(")
    body = source[start : source.index("\ndef _flat_series", start)]
    assert "_gfs_forecast" in body, "the forecast rung must call the GFS adapter"


def test_the_forecast_refuses_unexpected_units():
    """Treating inches as millimetres would understate a flood by 25x.

    The API reports its own units, so this is checkable rather than assumable — and an unchecked
    unit is exactly the kind of silent scale error that produced the Landsat reflectance bug.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _gfs_forecast(")
    body = source[start : source.index("\nasync def _climateserv(", start)]

    assert "daily_units" in body
    assert 'unit != "mm"' in body
    assert "refusing to guess" in body


def test_a_null_forecast_day_is_skipped_not_zeroed():
    """A gap in the model output is not a dry day.

    Substituting 0.0 would understate a forecast total, which is the same NaN-is-not-zero rule the
    raster path enforces — a missing measurement must never read as a measured absence.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _gfs_forecast(")
    body = source[start : source.index("\nasync def _climateserv(", start)]

    assert "if amount is None:" in body
    assert "continue" in body.split("if amount is None:")[1][:200]


def test_the_forecast_leads_the_chain():
    """Order matters: a forecast answers a different question from an antecedent total.

    The chain must try the forward-looking source FIRST, because `forecast_available=True` unlocks
    `_forecast_term` in the Oracle and removes the 0.75 confidence penalty. An antecedent rung that
    answered first would permanently suppress both.
    """
    from app.eo import sources
    from app.eo.sources import Kind

    chain = [s.key for s in sources.for_kind(Kind.RAINFALL)]
    assert chain[0] == "gfs-forecast", f"forecast must lead, got {chain}"
    # And IMERG (2-day lag) must precede CHIRPS (~6-week lag) — currency over cost.
    assert chain.index("gpm-imerg") < chain.index("climateserv-chirps")


# --------------------------------------------------------------------------- #
# ERA5 GRIB decoding — rung 4, now genuinely working
# --------------------------------------------------------------------------- #


def test_grib_decoding_needs_no_new_dependency():
    """GDAL already ships the GRIB driver, via the rasterio we already have.

    The obvious route was `cfgrib`/`eccodes` — a heavyweight addition to an image that already takes
    minutes to build. Measured instead: GDAL 3.6.2 via rasterio 1.4.3 reads it, verified end to end
    on a real ERA5 response (`driver=GRIB`, 3 bands, values in metres, 330-byte payload).
    """
    import pathlib

    requirements = pathlib.Path("requirements.txt").read_text().lower()
    for heavy in ("cfgrib", "eccodes", "pygrib"):
        assert heavy not in requirements, (
            f"{heavy} is not needed — GDAL's GRIB driver is already present via rasterio"
        )


def test_rainfall_does_not_import_the_geospatial_stack_at_module_scope():
    """**The import boundary this whole module arrangement exists to protect.**

    `eo/rainfall.py` must be importable without GDAL — that is what keeps `test_oracle.py` runnable
    with no geospatial stack, the same property `eo/exposure.py` protects with function-scope
    imports. A GRIB reader at the top of `rainfall.py` would drag rasterio into the risk layer.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.eo.rainfall; "
            "print('rasterio' in sys.modules or 'numpy.f2py' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.stdout.strip() == "False", (
        "importing eo.rainfall must not load rasterio — the decoder is imported lazily, "
        f"inside the one function that needs it. stdout={result.stdout!r}"
    )


def test_grib_totals_bands_and_excludes_nan():
    """One band per time step, so the SUM is the window total.

    Averaging the bands instead would give a per-step mean and understate a week sevenfold. NaN
    cells leave the denominator rather than reading as measured zero — the same rule
    `eo/indices.py` enforces for optical rasters.
    """
    import pathlib

    source = pathlib.Path("app/eo/grib.py").read_text()
    start = source.index("def total_from_grib(")
    body = source[start : source.index("\ndef available(", start)]

    assert "total +=" in body, "bands must be summed, not averaged"
    assert "np.isfinite" in body
    assert "if measured == 0:" in body and "return None" in body


def test_era5_requests_every_hour_not_just_midnight():
    """ERA5 `total_precipitation` is an HOURLY accumulation.

    Requesting only `00:00` captures 1/24 of each day. Measured over Kano that gave **0.0076 mm for
    a week** — not a rainfall figure at all. With all 24 steps it gives 6.2 mm, which is plausible.
    """
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _era5_antecedent(")
    body = source[start : source.index("\ndef _sum_numeric", start)]

    assert '"time": ["00:00"]' not in body, "one hour per day is 1/24 of the rainfall"
    assert "range(24)" in body


def test_era5_converts_metres_to_millimetres():
    """ERA5 is in METRES; the pipeline works in millimetres. A missing x1000 understates 1000-fold."""
    import pathlib

    source = pathlib.Path("app/eo/rainfall.py").read_text()
    start = source.index("async def _era5_antecedent(")
    body = source[start : source.index("\ndef _sum_numeric", start)]

    assert "1_000.0" in body or "1000.0" in body
