"""Pure geometry helpers.

Deliberately dependency-free — no rasterio, no numpy. Several modules need AOI
area and GeoJSON serialisation (`exposure`, `rainfall`, the API layer), and
routing them through a module that imports the geospatial stack would make the
Oracle unimportable without GDAL installed.
"""

from __future__ import annotations

import json
import math

from app.models.schemas import BBox

#: Metres per degree of latitude. Longitude shrinks by cos(latitude).
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQUATOR = 111_320.0


def area_hectares(bbox: BBox) -> float:
    """AOI area in hectares.

    Cosine-latitude approximation rather than a full geodesic solve — at AOI
    scale (tens of km) the error is well under a percent, and it avoids a
    geodesy dependency for a number that only scales exposure weighting.

    The cosine term matters: without it a degree-squared area would report an
    AOI at 60°N as the same size as one on the equator, overstating it by ~2x.
    """
    _lon, lat = bbox.centroid
    height_m = (bbox.north - bbox.south) * M_PER_DEG_LAT
    width_m = (
        (bbox.east - bbox.west) * M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))
    )
    return max(0.0, (height_m * width_m) / 10_000.0)


def bbox_geojson(bbox: BBox) -> str:
    """Serialise the AOI as a GeoJSON Feature string.

    Built with `json.dumps` rather than interpolation — several of these
    endpoints take the geometry as a *query parameter*, and hand-formatted JSON
    breaks on the first float that repr's unexpectedly.
    """
    ring = [
        [bbox.west, bbox.south],
        [bbox.east, bbox.south],
        [bbox.east, bbox.north],
        [bbox.west, bbox.north],
        [bbox.west, bbox.south],
    ]
    return json.dumps(
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}},
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- #
# Polygon AOIs
#
# The EO layer searches STAC and windows COGs by BBOX — that does not change, because
# both interfaces are rectangular. What a polygon adds is a MASK applied before the
# fractions are computed, so a field reads its own pixels rather than its envelope's.
#
# Why that matters, measured on realistic shapes:
#
#   square 1 km field     polygon / envelope = 100%   bbox over-reads 1.0x
#   L-shaped field                             69%                    1.4x
#   riverside strip                            33%                    3.0x
#
# The strip is the shape most flood-exposed smallholdings actually have — a plot running
# along a watercourse. Assessing its envelope means two-thirds of the pixels feeding
# `inundated_fraction` are somebody else's land, which dilutes a real flood signal toward
# the threshold and can turn a WARNING into a WATCH.
#
# These helpers are deliberately dependency-free for the same reason as the rest of this
# module: `app/agents/oracle.py` must stay importable and unit-testable without GDAL.
# Rasterisation itself lives in `app/eo/indices.py`, which already imports numpy.
# --------------------------------------------------------------------------- #

#: Maximum vertices in an AOI ring.
#:
#: A hand-drawn field needs a dozen; a hundred is generous. The cap exists because the ring
#: is rasterised per band per scene, and because an unbounded vertex list is a cheap way to
#: make one request expensive for everyone — a 50,000-point ring from a shapefile export
#: would be accepted, stored, and then rasterised on every pass forever.
MAX_RING_VERTICES = 200

#: Minimum vertices for a closed ring: three corners plus the repeated first point.
MIN_RING_VERTICES = 4

#: Largest AOI we accept, in hectares.
#:
#: 250,000 ha is a 50x50 km box — about an average Nigerian LGA. Beyond that a single
#: "inundated fraction" stops describing anything actionable: a 5% reading over a state
#: could be one flooded town or a hundred, and the advisory cannot say which. Aggregators
#: wanting district-level cover should register several AOIs, which is also what makes the
#: per-area alerting useful to them.
MAX_AOI_HECTARES = 250_000.0

#: Smallest AOI we accept, in hectares.
#:
#: Sentinel-2 is 10 m/pixel, so one hectare is ~100 pixels and Sentinel-1 GRD at 10 m is
#: similar. Below about 0.5 ha (~50 pixels) a "fraction of pixels flooded" is dominated by
#: edge effects and geolocation error — the number would look precise and mean nothing.
MIN_AOI_HECTARES = 0.5


class GeometryError(ValueError):
    """An AOI polygon that cannot be monitored, with a reason a subscriber can act on."""


def _ring_area_deg2(ring: list[list[float]]) -> float:
    """Signed shoelace area in square degrees. Sign gives winding direction."""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _segments_intersect(
    p1: list[float], p2: list[float], p3: list[float], p4: list[float]
) -> bool:
    """True if segment p1-p2 properly crosses p3-p4.

    Orientation test rather than solving for an intersection point: no division, so it
    cannot blow up on parallel or degenerate segments — which is exactly the case a
    hand-drawn ring produces when someone double-taps.
    """

    def orient(a: list[float], b: list[float], c: list[float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def validate_ring(ring: list[list[float]]) -> list[list[float]]:
    """Validate and normalise one exterior ring. Returns it closed and counter-clockwise.

    ## Why each check exists

    Every one of these produced a real failure mode somewhere downstream, and rejecting at
    the edge with a readable message is far better than a rasterisation error five minutes
    into a worker job:

      * **unclosed ring** — GeoJSON requires the first and last point to be identical.
        `rasterio.features.rasterize` accepts an unclosed ring and silently closes it, so
        the stored geometry and the mask would disagree.
      * **self-intersection** — a bow-tie has no well-defined interior. Rasterising one
        produces a mask that depends on the fill rule, so the same field could read
        differently between library versions.
      * **winding** — normalised to counter-clockwise (RFC 7946). Not cosmetic: PostGIS
        and some tile pipelines treat a clockwise exterior ring as a hole.
      * **vertex count** — see MAX_RING_VERTICES.

    A polygon with holes is NOT supported, and that is deliberate rather than an omission:
    the pipeline reports one fraction per AOI, so a field with an excluded pond inside it is
    better expressed as the field it is. Accepting holes would imply a precision the
    single-number output cannot carry.
    """
    if not isinstance(ring, list) or len(ring) < MIN_RING_VERTICES:
        raise GeometryError(
            "An area needs at least three corners. Tap three or more points on the map to "
            "outline it."
        )

    if len(ring) > MAX_RING_VERTICES:
        raise GeometryError(
            f"That outline has {len(ring)} points, more than the {MAX_RING_VERTICES} we "
            f"can monitor. A simpler shape around the same ground works better — the "
            f"satellite reads 10-metre pixels, so fine detail does not change the result."
        )

    for point in ring:
        if not isinstance(point, list | tuple) or len(point) < 2:
            raise GeometryError("Each corner must be a [longitude, latitude] pair.")
        lon, lat = float(point[0]), float(point[1])
        if not (-180 <= lon <= 180):
            raise GeometryError(f"Longitude {lon} is outside -180..180.")
        if not (-90 <= lat <= 90):
            raise GeometryError(f"Latitude {lat} is outside -90..90.")

    normalised = [[float(p[0]), float(p[1])] for p in ring]

    # Close it if the caller did not. Tolerant rather than strict: a UI that drops the
    # closing point is a common client bug, and closing it here is unambiguous.
    if normalised[0] != normalised[-1]:
        normalised.append(list(normalised[0]))

    if len(normalised) - 1 < 3:
        raise GeometryError("An area needs at least three distinct corners.")

    # Self-intersection. O(n²), which is fine at 200 vertices and is why the cap exists.
    count = len(normalised) - 1
    for i in range(count):
        for j in range(i + 2, count):
            # Skip the adjacent pair that shares the closing vertex.
            if i == 0 and j == count - 1:
                continue
            if _segments_intersect(
                normalised[i], normalised[i + 1], normalised[j], normalised[j + 1]
            ):
                raise GeometryError(
                    "That outline crosses itself, so it has no clear inside. Redraw it "
                    "without the edges overlapping."
                )

    # Normalise winding to counter-clockwise (positive shoelace area).
    if _ring_area_deg2(normalised) < 0:
        normalised.reverse()

    return normalised


def polygon_bbox(ring: list[list[float]]) -> BBox:
    """The envelope of a ring — what STAC search and COG windowing consume.

    The polygon never replaces the bbox; it narrows what is counted *inside* it. Both are
    stored because they answer different questions, and deriving one per read would mean
    the imagery layer paying for a geometry parse on every scene.
    """
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return BBox(west=min(lons), south=min(lats), east=max(lons), north=max(lats))


def polygon_area_hectares(ring: list[list[float]]) -> float:
    """True polygon area, not its envelope's.

    Shoelace in degrees scaled by the cosine of the centroid latitude — the same
    approximation `area_hectares` uses, and accurate to well under a percent at field
    scale. This is the number a subscriber recognises as their farm size, so it must
    describe the shape they drew rather than the box around it.
    """
    if len(ring) < MIN_RING_VERTICES:
        return 0.0

    lats = [p[1] for p in ring]
    mid_lat = (min(lats) + max(lats)) / 2.0

    deg2 = abs(_ring_area_deg2(ring))
    m_per_deg_lon = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(mid_lat))
    return max(0.0, deg2 * M_PER_DEG_LAT * m_per_deg_lon / 10_000.0)


def check_monitorable(hectares: float) -> None:
    """Reject an AOI the pipeline cannot say anything useful about.

    Both bounds are about the *meaning* of the output rather than about compute:

      * too small — fewer than ~50 Sentinel pixels, so a "fraction" is edge noise;
      * too large — one number over a whole state cannot locate anything actionable.

    Raised as `GeometryError` so the API surfaces the sentence unchanged.
    """
    if hectares < MIN_AOI_HECTARES:
        raise GeometryError(
            f"That area is about {hectares:.2f} hectares, too small for satellite "
            f"monitoring — the sensors read 10-metre pixels, so anything under "
            f"{MIN_AOI_HECTARES} ha is smaller than a reliable reading. Outline the whole "
            f"field, or drop a pin and we will watch a small area around it."
        )
    if hectares > MAX_AOI_HECTARES:
        raise GeometryError(
            f"That area is about {hectares:,.0f} hectares, larger than the "
            f"{MAX_AOI_HECTARES:,.0f} ha we monitor as one unit. A single risk figure over "
            f"an area that size cannot tell you *where* the problem is. Register several "
            f"smaller areas instead — each one gets its own assessment and its own alert."
        )


def ring_geojson(ring: list[list[float]]) -> str:
    """A ring as a GeoJSON Feature string, for the query-parameter APIs.

    Mirrors `bbox_geojson` so callers that already pass a geometry string need no branch —
    `exposure` and `rainfall` take whichever is available.
    """
    return json.dumps(
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}},
        separators=(",", ":"),
    )


def normalise_aoi(aoi):  # noqa: ANN001, ANN201 — typed by the caller to avoid a cycle
    """Validate and complete an `AreaOfInterest` on the way in. Returns a corrected copy.

    ## Why this exists rather than validators on the model

    `AreaOfInterest` is constructed from the DATABASE as well as from a request, and the
    ring there is already known good. Running an O(n^2) self-intersection test on every read
    would be waste, and a Pydantic validator cannot tell the two cases apart. So validation
    lives at the write boundary, where it belongs.

    ## What it corrects, and why each correction is safer than trusting the client

      * **bbox recomputed from the ring.** A client that sends a ring and a stale or
        unrelated bbox would otherwise have the imagery layer window one place and mask with
        another — producing an all-NaN array and a silent `0%` reading, which the Oracle
        reads as "no hazard". That is the worst failure this system can have, so the
        envelope is derived rather than believed. There is a Postgres CHECK constraint as a
        second line, but reaching it means a 500 rather than a readable message.
      * **hectares recomputed.** It is displayed to the subscriber as their farm size and
        feeds exposure weighting; a client-supplied value could disagree with the geometry.
      * **ring closed and rewound.** Callers legitimately differ on both conventions.

    Raises `GeometryError` with a sentence written for the subscriber, which the API surfaces
    unchanged.
    """
    if not getattr(aoi, "geometry", None):
        # Pin-and-radius: no ring to check, but the size limits still apply — a 1 m box and
        # a whole-country box are both unmonitorable however they were drawn.
        hectares = area_hectares(aoi.bbox)
        check_monitorable(hectares)
        return aoi.model_copy(update={"hectares": round(hectares, 2)})

    ring = validate_ring(aoi.geometry)
    hectares = polygon_area_hectares(ring)
    check_monitorable(hectares)

    return aoi.model_copy(
        update={
            "geometry": ring,
            "bbox": polygon_bbox(ring),
            "hectares": round(hectares, 2),
        }
    )
