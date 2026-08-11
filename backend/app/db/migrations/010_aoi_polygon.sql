-- AOI polygons: the true field outline, alongside the envelope.
--
-- ## Why a NEW column rather than changing `geom`
--
-- `areas_of_interest.geom` is `GENERATED ALWAYS AS ST_MakeEnvelope(west, south, east,
-- north)` — by design, so the bbox and the indexed geometry can never disagree. That
-- guarantee is worth keeping: `subscribers_intersecting` and every spatial query resolve
-- against it, and they want the envelope, because an alert should reach a farmer whose
-- field is anywhere inside a flood footprint.
--
-- A generated column also cannot hold user-supplied geometry. So the drawn outline goes in
-- its own column, and the two answer different questions:
--
--   geom          the envelope. What STAC search and spatial intersection use.
--   outline       the true ring, when drawn. What the EO layer MASKS with.
--
-- ## Why the area is stored rather than computed per read
--
-- `hectares_true` is the polygon's own area, which is what a subscriber recognises as their
-- farm size. Computing `ST_Area(outline::geography)` per read is cheap individually but sits
-- in the hot path of every assessment and every portal render, and the value only changes
-- when the outline does.
--
-- Nullable throughout: a pin-and-radius AOI has no outline, and that is a fully supported
-- first-class case rather than incomplete data.

ALTER TABLE areas_of_interest
    ADD COLUMN IF NOT EXISTS outline GEOGRAPHY(POLYGON, 4326),
    ADD COLUMN IF NOT EXISTS hectares_true DOUBLE PRECISION;

-- The outline must actually sit inside its own bbox. Without this, a client could send a
-- ring and an unrelated envelope; the imagery layer would window one place and mask with
-- another, producing an all-zero mask and a silent 0% reading — which reads as "no flood"
-- rather than as a bug. That is the worst possible failure for this product.
--
-- ST_CoveredBy rather than ST_Within: a ring touching its envelope's edge is normal, since
-- the envelope is derived from the ring's own extremes.
ALTER TABLE areas_of_interest
    DROP CONSTRAINT IF EXISTS aoi_outline_within_bbox;
ALTER TABLE areas_of_interest
    ADD CONSTRAINT aoi_outline_within_bbox CHECK (
        outline IS NULL
        OR ST_CoveredBy(
               outline::geometry,
               ST_Expand(ST_MakeEnvelope(west, south, east, north, 4326), 0.0001)
           )
    );

-- GiST on the outline as well as the envelope.
--
-- Not redundant: a "which fields does this flood footprint actually touch?" query wants the
-- outline, and answering it from `geom` would over-report every AOI whose envelope clips the
-- footprint while its real field sits outside — the same 3x over-read, this time in the
-- dispatch decision rather than in the measurement.
CREATE INDEX IF NOT EXISTS aoi_outline_gix ON areas_of_interest USING GIST (outline);

COMMENT ON COLUMN areas_of_interest.outline IS
    'True field ring when drawn. NULL for pin-and-radius areas, where geom is the geometry.';
COMMENT ON COLUMN areas_of_interest.hectares_true IS
    'Area of `outline` when present, else of the bbox. What the subscriber recognises.';
