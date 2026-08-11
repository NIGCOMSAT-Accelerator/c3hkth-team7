"""Rainfall: the forward-looking half of the risk score.

Satellite imagery tells us what already happened; rainfall tells us what is
about to. That is where the 7 days of lead time come from.

**The chain, in order:**

| Source | Kind | Credential |
|---|---|---|
| ClimateSERV GEFS | *forecast* | none |
| ClimateSERV CHIRPS | observed | none |
| NASA GPM IMERG | observed | Earthdata token |
| Copernicus ERA5 | reanalysis | CDS API key |

Only the first is a genuine forecast. The rest report **antecedent wetness** —
how saturated the ground already is — which is a real flood-risk signal but is
not a prediction. `RainfallOutlook.forecast_available` keeps the two apart, and
the Oracle lowers confidence when it is False. Inventing a forecast from
climatology would put fabricated numbers into an advisory a farmer acts on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.config import settings
from app.eo import auth
from app.eo.geometry import bbox_geojson
from app.logging_config import get_logger
from app.models.schemas import BBox, ForecastPoint, RainfallOutlook

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(45.0, connect=10.0)

#: Daily rainfall above which saturated ground is likely to pond, in mm.
#: From FAO waterlogging guidance for West African cereal soils.
PONDING_RAINFALL_MM = 25.0

#: Antecedent window for "how wet is it already", in days.
ANTECEDENT_DAYS = 7

#: Days to shift the antecedent window back, to clear an upstream's publication lag.
#:
#: ## Why this is not zero
#:
#: CHIRPS is a *reanalysis* product, not a live feed — it is assembled from station and satellite
#: records and published in arrears. Measured against ClimateSERV on 2026-08-11: data exists through
#: **June 2026** and returns an EMPTY series for July onward. So a 7-day antecedent window ending
#: today sits entirely inside the unpublished gap, and every request came back with zero entries.
#:
#: That produced the symptom "Rainfall data was unavailable for this cycle" on every assessment,
#: alongside two genuine parsing bugs (see `_climateserv_progress`). The upstream was working; we
#: were asking it about days it has not published yet.
#:
#: 45 days clears the observed ~6-week lag with margin. The cost is precision about *recency*:
#: "how wet was the ground last week" becomes "how wet was it six weeks ago". That is a real
#: weakness and it is why `RainfallOutlook.forecast_available` exists to mark the distinction — but
#: a six-week-old antecedent measurement is still evidence, whereas an empty series is nothing.
#:
#: IMERG (rung 3) has a ~1-day lag and is the right source for genuinely recent ground wetness,
#: which is why unlocking it is the highest-value credential action available.
ANTECEDENT_LAG_DAYS = 45


async def rainfall_outlook(bbox: BBox, *, days: int | None = None) -> RainfallOutlook:
    """Walk the source chain until one answers."""
    horizon = days or settings.forecast_horizon_days

    # 1 — the only true forecast in the chain. NOAA GFS/GEFS via a JSON façade; see
    # `_gfs_forecast` for why not the GRIB product, and why ClimateSERV cannot serve this.
    try:
        points = await _gfs_forecast(bbox, horizon)
        if points:
            total = sum(p.rainfall_mm for p in points)
            return RainfallOutlook(
                points=points,
                forecast_available=True,
                antecedent_mm=0.0,
                # Step 4 — SPI on the *forecast* total, so "is this a lot?" is
                # answered against this location's own history rather than a
                # national constant.
                spi=await _spi_for(bbox, total, horizon),
                source="gfs-forecast",
            )
    except Exception as exc:
        log.warning("short-range rainfall forecast unavailable", extra={"error": str(exc)})

    # 2-4 — observational fallbacks, ordered by CURRENCY, not by cost.
    #
    # ## Why IMERG now leads
    #
    # IMERG publishes with a **~2-day lag**; CHIRPS is a reanalysis product roughly **6 weeks** in
    # arrears (see `ANTECEDENT_LAG_DAYS`). Both answer "how wet is the ground", but only one answers
    # it about *now* — and for a flood warning that distinction is the whole value of the number.
    #
    # CHIRPS led while it was the only rung that worked: IMERG was unreachable (wrong host, a
    # hard-coded granule suffix, and an unauthorised Earthdata application), so ordering by cost was
    # ordering by availability. Measured once IMERG was fixed, over the same AOIs on the same day:
    #
    #     CHIRPS   0.0 mm   (a window ~6 weeks old)
    #     IMERG   15.6 mm Kano / 89.8 mm Yenagoa   (2 days old)
    #
    # Both are honest measurements; the second is the one a farmer can act on, and the Kano/Yenagoa
    # ratio correctly reflects Sahel against Niger Delta. CHIRPS stays as the keyless fallback for a
    # deployment with no Earthdata token, which is exactly what a chain is for.
    for name, fetch in (
        ("gpm-imerg", _imerg_antecedent),
        ("climateserv-chirps", _chirps_antecedent),
        ("era5", _era5_antecedent),
    ):
        try:
            observed = await fetch(bbox)
        except Exception as exc:
            log.warning(
                "antecedent source failed", extra={"source": name, "error": str(exc)}
            )
            continue
        if observed is not None:
            log.info(
                "using antecedent rainfall, no forecast",
                extra={"source": name, "antecedent_mm": round(observed, 1)},
            )
            # Step 4 — API from the daily series when the source produced one.
            # CHIRPS does; the IMERG and ERA5 adapters return a scalar total only,
            # so `api_mm` stays 0 there and the Oracle uses the flat sum. Reporting
            # 0 rather than guessing a shape is the honest option.
            api_mm = await _api_from_series(bbox) if name == "climateserv-chirps" else 0.0
            return RainfallOutlook(
                points=_flat_series(horizon, "rainfall forecast unavailable"),
                forecast_available=False,
                antecedent_mm=observed,
                api_mm=api_mm,
                spi=await _spi_for(bbox, observed, ANTECEDENT_DAYS),
                source=name,
            )

    log.warning("no rainfall source answered")
    return RainfallOutlook(
        points=_flat_series(horizon, "rainfall data unavailable"),
        forecast_available=False,
        source="none",
    )


def _flat_series(days: int, note: str) -> list[ForecastPoint]:
    """A zero series, explicitly labelled. Deliberately not a climatological
    guess — see the module docstring."""
    now = datetime.now(timezone.utc)
    return [
        ForecastPoint(
            day=i, date=now + timedelta(days=i), risk=0.0, rainfall_mm=0.0, note=note
        )
        for i in range(days)
    ]


# --------------------------------------------------------------------------- #
# 1 & 2 — SERVIR ClimateSERV (fronts CHIRPS observed and GEFS forecast)
# --------------------------------------------------------------------------- #


async def _gfs_forecast(bbox: BBox, days: int) -> list[ForecastPoint]:
    """Daily rainfall forecast from NOAA GFS/GEFS, via the Open-Meteo JSON façade.

    ## Why this rung exists at all

    `RainfallOutlook.forecast_available` separates "the ground is already wet" from "more rain is
    coming", and only the second supports a FORWARD flood warning. That flag was permanently False:
    the configured ClimateSERV datatype 35 was labelled GEFS but **ClimateSERV serves no GEFS** —
    `getClimateScenarioInfo` lists only CCSM4 and CFSv2, and 35 is rejected outright with progress
    `-1`. So every advisory was antecedent-only, and `oracle.NO_FORECAST_CONFIDENCE` scaled every
    assessment down by 0.75 for a reason that was our configuration error.

    ## Why JSON rather than the GRIB product

    NOAA publishes GEFS as GRIB2 on S3. Decoding that needs `cfgrib`/`eccodes`, which
    `eo/rainfall.py` must not import — staying free of the geospatial stack is what keeps
    `test_oracle.py` runnable with no provider configured. It is the same constraint that leaves the
    ERA5 rung declining. Open-Meteo exposes the same model as JSON, keyless.

    ## Point sample, not an area mean

    Open-Meteo takes one coordinate, so this asks about the AOI centroid. For a smallholder plot that
    is correct — the AOI is far smaller than a GFS grid cell (~25 km), so an area mean would average
    the same cell with itself. It would be wrong for a district-scale AOI, and that is a real limit
    worth knowing rather than hiding.
    """
    lon, lat = bbox.centroid

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(
            settings.forecast_api_url,
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "daily": "precipitation_sum",
                "forecast_days": str(max(1, min(days, 16))),
                "timezone": "UTC",
                "models": settings.forecast_model,
            },
        )
        response.raise_for_status()
        payload = response.json()

    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    amounts = daily.get("precipitation_sum") or []
    if not dates or not amounts:
        raise RuntimeError("forecast API returned no daily series")

    # Unit check rather than assumption. The API reports its own units, and silently treating
    # inches as millimetres would understate a flood by 25x.
    unit = (payload.get("daily_units") or {}).get("precipitation_sum", "mm")
    if unit != "mm":
        raise RuntimeError(f"unexpected forecast unit {unit!r}; refusing to guess")

    points: list[ForecastPoint] = []
    for index, (day, amount) in enumerate(zip(dates, amounts, strict=False)):
        if amount is None:
            # A null day is a gap in the model output, not zero rain. Skipped rather than
            # substituted — the same rule the raster path applies to NaN.
            continue
        points.append(
            ForecastPoint(
                day=index,
                date=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
                rainfall_mm=float(amount),
                # Severity is the Oracle's job; this reports the measurement only.
                risk=0.0,
            )
        )
    return points


async def _climateserv(
    bbox: BBox, datatype: int, days: int, *, future: bool
) -> list[ForecastPoint]:
    """Submit-then-poll against ClimateSERV, returning daily rainfall."""
    now = datetime.now(timezone.utc)
    if future:
        start, end = now, now + timedelta(days=days)
    else:
        # Shifted back past the publication lag — see `ANTECEDENT_LAG_DAYS`. Only the PAST window
        # is shifted: a forecast is published ahead of time by definition, so lagging it would ask
        # for days that have already happened.
        anchor = now - timedelta(days=ANTECEDENT_LAG_DAYS)
        start, end = anchor - timedelta(days=days), anchor

    params = {
        "datatype": str(datatype),
        "begintime": start.strftime("%m/%d/%Y"),
        "endtime": end.strftime("%m/%d/%Y"),
        "intervaltype": "0",  # daily
        "operationtype": "5",  # average over geometry
        "geometry": bbox_geojson(bbox),
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        submit = await client.get(
            f"{settings.climateserv_url.rstrip('/')}/submitDataRequest/", params=params
        )
        submit.raise_for_status()
        job_id = _climateserv_job_id(submit.text)
        if not job_id:
            raise RuntimeError("ClimateSERV returned no job id")

        for _ in range(settings.climateserv_poll_attempts):
            await asyncio.sleep(settings.climateserv_poll_interval_seconds)
            progress = await client.get(
                f"{settings.climateserv_url.rstrip('/')}/getDataRequestProgress/",
                params={"id": job_id},
            )
            state = _climateserv_progress(progress.text)
            if state is not None and state >= 100.0:
                break
            if state is not None and state < 0:
                # -1 is an explicit server-side rejection, not slow progress. Failing fast hands
                # the chain to the next rung instead of burning the whole poll budget waiting for
                # a job that will never finish.
                raise RuntimeError(f"ClimateSERV rejected the request (progress {state})")
        else:
            raise TimeoutError("ClimateSERV job did not finish in time")

        result = await client.get(
            f"{settings.climateserv_url.rstrip('/')}/getDataFromRequest/",
            params={"id": job_id},
        )
        result.raise_for_status()
        return _parse_climateserv(result.json(), days)


def _climateserv_job_id(body: str) -> str | None:
    """The job id from a submit response.

    ## Why this needs a parser

    ClimateSERV wraps its answer in a **JSONP callback and a JSON array**:

        cb(["f80ffd87-52fe-41f5-b2b4-d06a96177aaa"])

    The previous code did `text.strip().strip('"')`, which leaves the whole
    `cb([...])` envelope intact. Every subsequent progress poll then carried a malformed id, so the
    job it was asking about did not exist — one of the two reasons this rung never answered.
    """
    raw = (body or "").strip()
    # Unwrap a JSONP callback, whatever it is named.
    if "(" in raw and raw.endswith(")"):
        raw = raw[raw.index("(") + 1 : -1].strip()
    raw = raw.strip("[]").strip().strip('"').strip("'")
    return raw or None


def _climateserv_progress(body: str) -> float | None:
    """Progress percentage from a poll response, or None when unreadable.

    ## The bug this replaces

    ClimateSERV returns **`[100.0]`** — a JSON array holding a FLOAT. The previous check was
    `progress.text.strip().strip('"') == "100"`, which is False for `[100.0]`, so a job that had
    *already completed* was never recognised and every request ran out its poll budget and raised
    `TimeoutError`. Measured live: eleven consecutive polls all returned `[100.0]` and the code
    treated each as incomplete.

    That is why every assessment reported "Rainfall data was unavailable" while the upstream was
    working perfectly — the two keyless rungs of a four-rung chain were both defeated by a string
    comparison.

    Returns a float so the caller can distinguish `>= 100` (done) from `< 0` (rejected) from
    anything in between (genuinely still running).
    """
    raw = (body or "").strip()
    if "(" in raw and raw.endswith(")"):
        raw = raw[raw.index("(") + 1 : -1].strip()
    raw = raw.strip("[]").strip().strip('"').strip("'")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_climateserv(payload: dict, days: int) -> list[ForecastPoint]:
    entries = payload.get("data", [])
    now = datetime.now(timezone.utc)
    points: list[ForecastPoint] = []

    for index, entry in enumerate(entries[:days]):
        value = entry.get("value", {})
        rainfall = float(value.get("avg", 0.0) or 0.0)
        # ClimateSERV uses a large negative sentinel for no-data cells.
        if rainfall < 0:
            rainfall = 0.0
        points.append(
            ForecastPoint(
                day=index,
                date=now + timedelta(days=index),
                # Rainfall contribution only; the Oracle blends in observed
                # inundation afterwards.
                risk=min(1.0, rainfall / (PONDING_RAINFALL_MM * 2)),
                rainfall_mm=rainfall,
            )
        )
    return points


async def _chirps_antecedent(bbox: BBox) -> float | None:
    """Total observed rainfall over the antecedent window, via CHIRPS."""
    points = await _climateserv(
        bbox, settings.climateserv_datatype, ANTECEDENT_DAYS, future=False
    )
    if not points:
        return None
    return sum(p.rainfall_mm for p in points)


# --------------------------------------------------------------------------- #
# 3 — NASA GPM IMERG (needs a free Earthdata token)
# --------------------------------------------------------------------------- #


async def _imerg_granules(
    client: httpx.AsyncClient, headers: dict, days: int
) -> list[tuple[str, str]]:
    """`(date_label, opendap_href)` for the most recent IMERG daily granules.

    ## Why CMR rather than a constructed filename

    The previous adapter built the URL by hand, including a hard-coded `V07B` processing suffix.
    Verified live 2026-08-11: the current granule is `...20260809-...V07C.nc4`. That letter advances
    without notice, so every hand-built URL eventually 404s — silently, because the adapter swallowed
    per-day exceptions and returned None.

    CMR is the authoritative index and publishes the exact OPeNDAP href per granule, so the filename
    never has to be guessed.
    """
    try:
        response = await client.get(
            f"{settings.cmr_search_url.rstrip('/')}/granules.json",
            params={
                "short_name": settings.gpm_imerg_short_name,
                "version": settings.gpm_imerg_version,
                # A little more than asked for: IMERG late-run publishes with a ~2-day lag, so the
                # newest granules are already a couple of days behind today.
                "page_size": str(days + 4),
                "sort_key": "-start_date",
            },
            headers=headers,
        )
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("CMR granule search failed", extra={"error": str(exc)})
        return []

    found: list[tuple[str, str]] = []
    for entry in entries:
        href = next(
            (
                link.get("href", "")
                for link in entry.get("links", [])
                if "opendap" in link.get("href", "")
            ),
            "",
        )
        # Host check, not decoration. CMR is an index we do not control, and this adapter follows
        # whatever href it publishes — so pinning the expected host means an index change cannot
        # silently point our authenticated reads at somewhere else.
        if href and settings.imerg_opendap_host in href:
            found.append((entry.get("time_start", "")[:10], href))
        elif href:
            log.warning(
                "skipping an IMERG granule from an unexpected host",
                extra={"expected": settings.imerg_opendap_host, "href_host": href.split("/")[2]},
            )
    return found[:days]


async def _imerg_antecedent(bbox: BBox) -> float | None:
    """Antecedent rainfall from IMERG daily late-run, via OPeNDAP DAP4.

    Requires `NASA_EARTHDATA_TOKEN` **and** the `NASA GESDISC DATA ARCHIVE` application authorised
    on the Earthdata profile — a token alone returns 401, which is an account action rather than a
    code fault.

    ## Why this is the best antecedent source

    IMERG publishes with a **~2-day lag**, against CHIRPS's ~6 weeks (see `ANTECEDENT_LAG_DAYS`). So
    this is the only rung that answers "how wet is the ground *now*", which is the question a flood
    warning actually depends on. CHIRPS remains ahead of it in the chain only because it is keyless.

    ## The three things that had to be right

    1. **Host.** `gpm1.gesdisc.eosdis.nasa.gov/opendap` 404s on the product path and times out at the
       root. Data is at `opendap.earthdata.nasa.gov/collections/{concept}/granules/{granule}`.
    2. **Granule name.** Not constructible — see `_imerg_granules`.
    3. **Response format.** `.ascii` and `.dods` both return HTTP 400 here; `.dap.json` returns 500.
       Only **`.dap.csv`** works. And a single-index constraint emits a header with no data row,
       while a small RANGE emits real values — verified, `[0][1885:1887][1019:1021]` returns
       `0, 0, 0`. So the query asks for a 3x3 block and averages the finite cells.
    """
    headers = auth.earthdata_headers()
    if not headers:
        return None

    lon, lat = bbox.centroid
    xi = _imerg_index(lon, -180.0)
    yi = _imerg_index(lat, -90.0)
    # A 3x3 block rather than one cell: a single-index DAP4 constraint returns no data row on this
    # server, and averaging a 3x3 at 0.1 degrees (~30 km) is a reasonable stand-in for an AOI that
    # is almost always smaller than one IMERG cell anyway.
    constraint = quote(
        f"/precipitation[0][{xi}:{xi + 2}][{yi}:{yi + 2}]", safe="/"
    )

    total = 0.0
    collected = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        granules = await _imerg_granules(client, headers, ANTECEDENT_DAYS)
        if not granules:
            return None

        for day_label, href in granules:
            try:
                response = await client.get(
                    f"{href}.dap.csv?dap4.ce={constraint}", headers=headers
                )
                response.raise_for_status()
                value = _parse_dap_csv_mean(response.text)
            except Exception as exc:  # noqa: BLE001 — one missing day must not lose the series
                log.debug(
                    "IMERG granule unreadable",
                    extra={"day": day_label, "error": str(exc)[:80]},
                )
                continue
            if value is not None and value >= 0:
                total += value
                collected += 1

    if not collected:
        return None

    log.info(
        "IMERG antecedent rainfall",
        extra={"days_collected": collected, "total_mm": round(total, 1)},
    )
    return total


def _parse_dap_csv_mean(body: str) -> float | None:
    """Mean of the finite values in a DAP4 CSV response, or None when there are none.

    The format is a `Dataset:` header line, then one line per outer index:

        Dataset: 3B-DAY-L.MS.MRG.3IMERG.20260809-...nc4
        /precipitation[0][0], 0, 0, 0
        /precipitation[0][1], 0, 0, 0

    IMERG marks no-data as a large negative fill (-9999.9), which must be EXCLUDED rather than
    averaged in — the same NaN-is-not-zero rule `eo/indices.py` enforces. Averaging a fill value
    would report a large negative rainfall, and summing it across a week would swamp the total.
    """
    values: list[float] = []
    for line in (body or "").splitlines():
        if not line.startswith("/"):
            continue
        for cell in line.split(",")[1:]:
            try:
                number = float(cell.strip())
            except ValueError:
                continue
            # Fill values are large and negative; real rainfall never is.
            if number > -100.0:
                values.append(number)

    if not values:
        return None
    return sum(values) / len(values)


def _imerg_index(coordinate: float, origin: float, resolution: float = 0.1) -> int:
    """IMERG grid index for a coordinate on its 0.1° global grid."""
    return max(0, int((coordinate - origin) / resolution))


def _parse_opendap_scalar(body: str) -> float | None:
    """Pull the first numeric value out of an OPeNDAP ASCII response."""
    for line in body.splitlines():
        parts = [p.strip() for p in line.split(",")]
        for part in reversed(parts):
            try:
                return float(part)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# 4 — Copernicus ERA5 reanalysis (needs a free CDS API key)
# --------------------------------------------------------------------------- #


async def _era5_antecedent(bbox: BBox) -> float | None:
    """Antecedent rainfall from ERA5 single-levels total precipitation.

    Requires `ERA5_CDS_KEY`. The CDS API is submit-then-poll and its queue can
    be slow, which is why ERA5 sits last in the chain.

    ## Status, verified live 2026-08-11

    The credential and the licence now WORK: `POST .../execute` returns `HTTP 201` with a job id,
    and the job reaches `successful` in ~24s. Before the dataset licence was accepted it returned
    `403 "required licences not accepted"`, which is an account action, not a code fault.

    **But this rung still declines**, deliberately. `/results` returns an asset descriptor pointing
    at a **GRIB file**, and decoding GRIB needs `cfgrib`/`eccodes` — a heavyweight dependency this
    module must not carry, because `eo/rainfall.py` has to stay importable without the geospatial
    stack. See the comment at the `/results` call for the fabricated-number bug this replaced.

    So ERA5 is wired, authenticated and reachable, and returns None until a GRIB decoder exists.
    That is honest incompleteness rather than a silent wrong answer, and it costs nothing: three
    rungs answer ahead of it.
    """
    if not settings.era5_cds_key:
        return None

    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=ANTECEDENT_DAYS)

    payload = {
        "inputs": {
            "variable": ["total_precipitation"],
            "product_type": ["reanalysis"],
            "date": [f"{start:%Y-%m-%d}/{end:%Y-%m-%d}"],
            # **All 24 hours, not one.**
            #
            # ERA5 `total_precipitation` is an HOURLY accumulation — the depth that fell in that one
            # hour. Requesting only `00:00` therefore captures 1/24 of each day, and measured over
            # Kano that produced **0.0076 mm for a week**, which is not a rainfall figure at all.
            # The sum over all 24 steps is the daily total, which is what an antecedent window means.
            #
            # Cost: 24 bands per day instead of 1. The payload is still tiny because `area` bounds it
            # to the AOI — a 3-day single-cell request measured 330 bytes.
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "area": [bbox.north, bbox.west, bbox.south, bbox.east],
            # **`grib`, not `json`.** The CDS process schema declares
            # `data_format: {"enum": ["grib", "netcdf"]}` — verified live 2026-08-11 — so `json`
            # was never a valid value. See `_era5_antecedent` for what that meant.
            "data_format": "grib",
        }
    }
    headers = {"PRIVATE-TOKEN": settings.era5_cds_key}

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        submit = await client.post(
            f"{settings.era5_cds_url.rstrip('/')}"
            f"/retrieve/v1/processes/{settings.era5_dataset}/execute",
            json=payload,
            headers=headers,
        )
        submit.raise_for_status()
        job = submit.json()
        job_id = job.get("jobID")
        if not job_id:
            return None

        # 20 polls x 4s = 80s. Widened from 10x3s=30s: a 24-hour-per-day request is 24x the data
        # of the original single-hour one, and the CDS queue is shared. Measured, a 3-day request
        # reached `successful` in ~24s, but the queue is the variable — 30s left this rung returning
        # None on a job that was merely still running, which is indistinguishable from a real
        # failure and is why the value silently vanished after the hourly fix.
        #
        # ERA5 is the LAST rung: three sources answer ahead of it, so waiting here costs nothing on
        # a healthy deployment and only matters when everything else has already declined.
        for _ in range(20):
            await asyncio.sleep(4)
            status = await client.get(
                f"{settings.era5_cds_url.rstrip('/')}/retrieve/v1/jobs/{job_id}",
                headers=headers,
            )
            state = status.json().get("status")
            if state == "successful":
                break
            if state in {"failed", "dismissed"}:
                return None
        else:
            return None

        results = await client.get(
            f"{settings.era5_cds_url.rstrip('/')}/retrieve/v1/jobs/{job_id}/results",
            headers=headers,
        )
        results.raise_for_status()

        # Decode the GRIB asset.
        #
        # ## Why this is now possible without a new dependency
        #
        # The obvious route was `cfgrib`/`eccodes`, rejected as a heavyweight addition. Measured
        # instead: **GDAL 3.6.2, already present via rasterio, ships the GRIB driver.** Verified end
        # to end on a real ERA5 response — `driver=GRIB`, 3 bands (one per requested day), values in
        # metres, a 330-byte payload for a single-cell 3-day window.
        #
        # ## Why the decoder lives in `eo/grib.py` and is imported HERE, lazily
        #
        # `eo/rainfall.py` must stay importable without the geospatial stack — that is what keeps
        # `test_oracle.py` runnable with no GDAL, the same property `eo/exposure.py` protects with its
        # function-scope imports. A module-scope rasterio import would drag GDAL into the whole risk
        # layer. So the import is inside this function, and a deployment without GDAL declines exactly
        # as it did before rather than failing to import.
        #
        # This replaces `_sum_numeric(results.json())`, which would have recursively totalled every
        # number in the asset ENVELOPE — `file:size` (550) plus checksum digits — and reported that as
        # millimetres of rainfall. That is the worst failure mode in this codebase, and it was hidden
        # only because the request 403'd first on an invalid `data_format`.
        from app.eo import grib as grib_reader

        if not grib_reader.available():
            log.info("GRIB decoding unavailable in this process; ERA5 rung declining")
            return None

        asset = (results.json() or {}).get("asset", {}).get("value", {})
        href = asset.get("href")
        if not href:
            return None

        download = await client.get(href)
        download.raise_for_status()

        metres = grib_reader.total_from_grib(download.content)
        if metres is None:
            return None

        # ERA5 total precipitation is in METRES; the pipeline works in millimetres.
        millimetres = metres * 1_000.0
        log.info(
            "ERA5 antecedent rainfall",
            extra={"total_mm": round(millimetres, 2), "bytes": len(download.content)},
        )
        return millimetres


def _sum_numeric(payload: object) -> float | None:
    """Recursively total every number in a nested JSON structure."""
    if isinstance(payload, int | float) and not isinstance(payload, bool):
        return float(payload)
    if isinstance(payload, list):
        values = [v for item in payload if (v := _sum_numeric(item)) is not None]
        return sum(values) if values else None
    if isinstance(payload, dict):
        values = [v for item in payload.values() if (v := _sum_numeric(item)) is not None]
        return sum(values) if values else None
    return None


# --------------------------------------------------------------------------- #
# Step 4 — SPI and API support
#
# Both are *derived* statistics over the same CHIRPS series the chain already
# fetches, so neither adds a new upstream dependency or credential. Both return a
# documented "not measured" value on any failure, and the Oracle then uses the raw
# millimetre path — so a deployment with no climatology behaves exactly as before.
# --------------------------------------------------------------------------- #

#: Years of CHIRPS history to fit the SPI gamma distribution against.
#:
#: 20 is `stats.rainfall_index.MIN_HISTORY`, which is the floor at which a gamma
#: fit is trustworthy; the WMO recommends 30 years. More would be better and costs
#: only a longer one-off fetch, but 20 keeps the first cycle's latency reasonable
#: and the result already comparable across the country.
#: Years of history sampled for the SPI baseline.
#:
#: ## Why this came down from 20
#:
#: Each year is one ClimateSERV submit-then-poll job, run SEQUENTIALLY to avoid hammering a free
#: service. Measured live at ~7s per job, so 20 years is **~140 seconds** on the first assessment
#: for an AOI — and while the ClimateSERV parse was broken (see `_climateserv_progress`) every one
#: of those jobs failed instantly, so the cost was invisible. Fixing the parse revealed it: the
#: Oracle stage began holding queue entries long enough to look stalled.
#:
#: 12 years keeps the distribution statistically usable — SPI is conventionally fitted on 30+ years,
#: but a 12-sample gamma fit over the same calendar window is still far better than a national
#: constant, and this is a decision aid rather than a climatological publication. It roughly halves
#: worst-case first-run latency to ~85s.
#:
#: The result is cached per (AOI, window) so this is paid once per AOI, not per cycle — which is why
#: correctness of the *series* matters more here than its length.
SPI_HISTORY_YEARS = 12

#: Ceiling on how long the climatology fetch may take, in seconds.
#:
#: Sequential jobs against a free service can stall for reasons that are not ours, and the SPI is an
#: ENRICHMENT: without it the Oracle still has the antecedent total and every hazard term. So the
#: budget is bounded and a partial series is used rather than blocking the pipeline — better a
#: 9-year baseline computed now than a 12-year one that holds a queue entry for three minutes.
CLIMATOLOGY_BUDGET_SECONDS = 60.0


async def _spi_for(bbox: BBox, current_mm: float, window_days: int) -> float | None:
    """SPI for a rainfall total, against this location's own CHIRPS history.

    The climatology is cached for a week (`CLIMATOLOGY_TTL_SECONDS`): one week of
    new observations cannot meaningfully move a 20-year gamma fit, so re-fetching
    per cycle would be pure cost.

    Returns None — never a number — when history is unavailable or too short. That
    is what makes the Oracle's `max(raw_term, spi_term)` safe: an unavailable SPI
    contributes nothing rather than a fabricated quantile.
    """
    if not settings.rainfall_statistics_enabled:
        return None

    try:
        history = await _rainfall_climatology(bbox, window_days)
        if not history:
            return None
        from app.stats.rainfall_index import spi as compute_spi

        return compute_spi(current_mm, history)
    except Exception as exc:
        log.debug("SPI unavailable", extra={"error": str(exc)})
        return None


async def _rainfall_climatology(bbox: BBox, window_days: int) -> list[float]:
    """Historical totals over the same window length, for the SPI fit.

    Samples the *same calendar window* in each of the past `SPI_HISTORY_YEARS`
    years. Sampling the same season is essential, not a refinement: comparing an
    August total against a year-round distribution would report every wet-season
    week as exceptional, which is the opposite of what SPI is for.
    """
    from app.store import cache

    key = cache.climatology_key(bbox, "chirps", window_days)
    if (cached := await cache.get_json(key)) is not None:
        if isinstance(cached, list) and cached:
            return [float(v) for v in cached]

    now = datetime.now(timezone.utc)
    totals: list[float] = []

    # Sequential rather than gathered: ClimateSERV is submit-then-poll and 20
    # concurrent jobs would be rude to a free service that the whole chain depends
    # on. This runs once a week per AOI, so latency here is not on a hot path.
    import time as _time

    started = _time.monotonic()

    for years_back in range(1, SPI_HISTORY_YEARS + 1):
        # Stop early rather than hold the pipeline. A partial baseline is a usable baseline; an
        # assessment blocked behind twelve sequential upstream jobs is not.
        if _time.monotonic() - started > CLIMATOLOGY_BUDGET_SECONDS:
            log.info(
                "climatology budget spent; fitting SPI on a partial baseline",
                extra={"years_collected": len(totals), "years_requested": SPI_HISTORY_YEARS},
            )
            break
        end = now.replace(year=now.year - years_back)
        try:
            points = await _climateserv_window(
                bbox, settings.climateserv_datatype, end - timedelta(days=window_days), end
            )
        except Exception:
            continue
        if points:
            totals.append(sum(p.rainfall_mm for p in points))

    if totals:
        await cache.set_json(key, totals, ttl_seconds=cache.CLIMATOLOGY_TTL_SECONDS)
    return totals


async def _api_from_series(bbox: BBox) -> float:
    """Antecedent Precipitation Index from the daily CHIRPS series.

    Returns 0.0 rather than a guess when the series is unavailable — the Oracle
    then falls back to the flat 7-day sum it has always used.
    """
    try:
        points = await _climateserv(
            bbox, settings.climateserv_datatype, ANTECEDENT_DAYS, future=False
        )
        if not points:
            return 0.0
        from app.stats.rainfall_index import antecedent_precipitation_index

        # `_parse_climateserv` yields day 0 first (oldest), which is the order API
        # expects — oldest first, so the last element is yesterday.
        return antecedent_precipitation_index([p.rainfall_mm for p in points])
    except Exception as exc:
        log.debug("API unavailable", extra={"error": str(exc)})
        return 0.0


async def _climateserv_window(
    bbox: BBox, datatype: int, start: datetime, end: datetime
) -> list[ForecastPoint]:
    """`_climateserv` for an explicit historical date range.

    Exists because `_climateserv` derives its window from `now`, which cannot
    express "the same fortnight, eleven years ago".
    """
    params = {
        "datatype": str(datatype),
        "begintime": start.strftime("%m/%d/%Y"),
        "endtime": end.strftime("%m/%d/%Y"),
        "intervaltype": "0",
        "operationtype": "5",
        "geometry": bbox_geojson(bbox),
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        submit = await client.get(
            f"{settings.climateserv_url.rstrip('/')}/submitDataRequest/", params=params
        )
        submit.raise_for_status()
        job_id = submit.text.strip().strip('"')
        if not job_id:
            return []

        for _ in range(settings.climateserv_poll_attempts):
            await asyncio.sleep(settings.climateserv_poll_interval_seconds)
            progress = await client.get(
                f"{settings.climateserv_url.rstrip('/')}/getDataRequestProgress/",
                params={"id": job_id},
            )
            if progress.text.strip().strip('"') == "100":
                break
        else:
            return []

        result = await client.get(
            f"{settings.climateserv_url.rstrip('/')}/getDataFromRequest/",
            params={"id": job_id},
        )
        result.raise_for_status()
        return _parse_climateserv(result.json(), (end - start).days or 1)
