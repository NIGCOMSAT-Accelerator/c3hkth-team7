"use client";

import { useEffect, useRef, useState } from "react";

import type { BBox } from "@/lib/types";

/**
 * A working map for browsers that cannot run MapLibre. Plain `<img>` tiles, no WebGL, no library.
 *
 * ## Why this exists rather than just falling back to a text field
 *
 * MapLibre 6 requires **WebGL 2**, and that is genuinely absent on real installs — a default
 * Firefox profile on the developer's own machine reports `webgl2=false`, as do older Android
 * WebView builds and desktops whose drivers are blocklisted for WebGL 2 but not WebGL 1. For
 * those users the alternative was "type your village name", which loses the one thing the map
 * is for: seeing whether the square actually covers your land before committing to it.
 *
 * This renders OpenStreetMap raster tiles as images in a CSS grid. It needs nothing but `<img>`
 * and `position: absolute`, so it works in every browser that can render this page at all —
 * which is what makes the cross-browser promise real rather than aspirational, and what keeps
 * the flow intact when this becomes a PWA on a low-end Android.
 *
 * ## What it deliberately does not do
 *
 * No pinch-zoom, no rotation, no vector styling. Two zoom buttons and tap-to-move-the-pin, and
 * that is the whole interaction. A hand-rolled gesture layer would be a large amount of code
 * to reimplement badly, and every one of these users is on the fallback path precisely because
 * their browser is limited — spending their bandwidth on interaction polish is the wrong trade.
 *
 * ## Attribution is a licence condition, not decoration
 *
 * ODbL requires OpenStreetMap credit wherever the data appears. It is rendered inside the map
 * frame and is not removable.
 *
 * ## Tile usage
 *
 * `tile.openstreetmap.org` is used, which the OSMF Tile Usage Policy permits for modest
 * volumes. Only the ~12 tiles covering one viewport are ever requested and the browser caches
 * them, so a subscriber picking one plot costs a handful of requests. If this path ever carries
 * real volume it should move to the same self-hosted raster we would serve from MinIO — the
 * same sovereignty argument the vector style already makes.
 */

/** Tile edge in CSS pixels. 256 is the OSM standard and what the URL scheme assumes. */
const TILE = 256;

/** Zoom bounds. z5 shows Nigeria whole; z16 is the finest zoom OSM raster has for rural areas. */
const MIN_ZOOM = 5;
const MAX_ZOOM = 16;

/** Web Mercator: longitude → fractional tile x at this zoom. */
function lonToTileX(lon: number, zoom: number): number {
  return ((lon + 180) / 360) * 2 ** zoom;
}

/** Web Mercator: latitude → fractional tile y at this zoom. */
function latToTileY(lat: number, zoom: number): number {
  const radians = (lat * Math.PI) / 180;
  return (
    ((1 - Math.log(Math.tan(radians) + 1 / Math.cos(radians)) / Math.PI) / 2) *
    2 ** zoom
  );
}

function tileXToLon(x: number, zoom: number): number {
  return (x / 2 ** zoom) * 360 - 180;
}

function tileYToLat(y: number, zoom: number): number {
  const n = Math.PI - 2 * Math.PI * (y / 2 ** zoom);
  return (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}

export default function RasterMap({
  centre,
  bbox,
  onMapTap,
  locating,
}: {
  centre: { lat: number; lon: number } | null;
  bbox: BBox | null;
  onMapTap: (lat: number, lon: number) => void;
  locating: boolean;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(13);
  const [size, setSize] = useState({ width: 0, height: 0 });

  // Measured rather than assumed: the frame is responsive, and computing the tile grid from a
  // hardcoded width would leave gaps on a wide screen and overflow on a narrow one.
  useEffect(() => {
    const element = frame.current;
    if (!element) return;

    const measure = () =>
      setSize({ width: element.clientWidth, height: element.clientHeight });
    measure();

    // ResizeObserver is in every browser this targets (Safari 13.1+, Firefox 69+, Chrome 64+).
    // Guarded anyway so an absence degrades to the initial measurement instead of throwing.
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Nigeria's centroid until a real position arrives — the same first frame MapLibre uses, so
  // the two paths do not look like different products.
  const lat = centre?.lat ?? 9.0;
  const lon = centre?.lon ?? 8.0;

  const centreX = lonToTileX(lon, zoom);
  const centreY = latToTileY(lat, zoom);

  // Which tiles cover the viewport, plus one ring beyond it so a small pan does not reveal
  // white space before the images load.
  const across = Math.ceil(size.width / TILE) + 2;
  const down = Math.ceil(size.height / TILE) + 2;
  const firstX = Math.floor(centreX - across / 2);
  const firstY = Math.floor(centreY - down / 2);

  const tiles: { key: string; x: number; y: number; left: number; top: number }[] = [];
  const limit = 2 ** zoom;
  for (let dx = 0; dx < across; dx++) {
    for (let dy = 0; dy < down; dy++) {
      const x = firstX + dx;
      const y = firstY + dy;
      // Out-of-range y is empty space above the pole; wrap x so panning across the antimeridian
      // does not request a negative tile.
      if (y < 0 || y >= limit) continue;
      tiles.push({
        key: `${zoom}/${x}/${y}`,
        x: ((x % limit) + limit) % limit,
        y,
        left: (x - centreX) * TILE + size.width / 2,
        top: (y - centreY) * TILE + size.height / 2,
      });
    }
  }

  /** Screen point → coordinates, so a tap can move the pin. */
  function pointToLatLon(clientX: number, clientY: number) {
    const rect = frame.current?.getBoundingClientRect();
    if (!rect) return null;
    const x = centreX + (clientX - rect.left - rect.width / 2) / TILE;
    const y = centreY + (clientY - rect.top - rect.height / 2) / TILE;
    return { lat: tileYToLat(y, zoom), lon: tileXToLon(x, zoom) };
  }

  // The resolved rectangle, in screen pixels.
  const overlay = bbox
    ? (() => {
        const west = (lonToTileX(bbox.west, zoom) - centreX) * TILE + size.width / 2;
        const east = (lonToTileX(bbox.east, zoom) - centreX) * TILE + size.width / 2;
        const north = (latToTileY(bbox.north, zoom) - centreY) * TILE + size.height / 2;
        const south = (latToTileY(bbox.south, zoom) - centreY) * TILE + size.height / 2;
        return { left: west, top: north, width: east - west, height: south - north };
      })()
    : null;

  return (
    <div className="mapcanvas">
      <div
        ref={frame}
        className="rastermap"
        onClick={(event) => {
          const point = pointToLatLon(event.clientX, event.clientY);
          if (point) onMapTap(point.lat, point.lon);
        }}
        // Keyboard-reachable: this is the only way to set a location on this path, and a
        // click-only control would exclude anyone not using a pointer.
        role="button"
        tabIndex={0}
        aria-label="Map. Tap to place your location pin."
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          const rect = frame.current?.getBoundingClientRect();
          if (!rect) return;
          // Enter places the pin at the centre, which is the only unambiguous choice without
          // a pointer position.
          const point = pointToLatLon(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2,
          );
          if (point) onMapTap(point.lat, point.lon);
        }}
      >
        {tiles.map((tile) => (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={tile.key}
            src={`https://tile.openstreetmap.org/${zoom}/${tile.x}/${tile.y}.png`}
            alt=""
            width={TILE}
            height={TILE}
            // Lazy would delay tiles already inside the viewport, which is every tile here.
            loading="eager"
            // Tiles are decorative individually; the map as a whole is labelled above.
            aria-hidden="true"
            draggable={false}
            style={{
              position: "absolute",
              left: tile.left,
              top: tile.top,
              width: TILE,
              height: TILE,
              // Hides the seam that subpixel positioning can leave between adjacent tiles.
              outline: "0.5px solid transparent",
            }}
          />
        ))}

        {overlay && (
          <div
            className="rastermap__area"
            style={{
              left: overlay.left,
              top: overlay.top,
              width: overlay.width,
              height: overlay.height,
            }}
            aria-hidden="true"
          />
        )}

        {centre && <div className="rastermap__pin" aria-hidden="true" />}

        {locating && <div className="mapcanvas__veil">Finding your location…</div>}
      </div>

      <div className="rastermap__controls">
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => setZoom((z) => Math.min(MAX_ZOOM, z + 1))}
          disabled={zoom >= MAX_ZOOM}
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => setZoom((z) => Math.max(MIN_ZOOM, z - 1))}
          disabled={zoom <= MIN_ZOOM}
          aria-label="Zoom out"
        >
          −
        </button>
      </div>

      {/* ODbL requires this wherever the data appears. Inside the frame, not removable. */}
      <p className="rastermap__credit">
        ©{" "}
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
          OpenStreetMap
        </a>{" "}
        contributors
      </p>
    </div>
  );
}
