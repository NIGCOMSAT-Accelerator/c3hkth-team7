"use client";

import { useEffect, useRef, useState } from "react";

import type { BBox } from "@/lib/types";

import RasterMap from "./RasterMap";

/**
 * The map itself. The only module that touches MapLibre.
 *
 * ## Why MapLibre is imported dynamically inside an effect
 *
 * `maplibre-gl` is ~230 KB gzipped — comfortably the heaviest thing this application would
 * ship. A static `import` at the top of a client component pulls it into the route's bundle,
 * so it would download on the way to the *form*, before anyone has decided to pick an area.
 * A dynamic import inside the mount effect means it is fetched when the map is actually
 * rendered, and never at all for a subscriber who registers by place name alone.
 *
 * On a metered connection in rural Nigeria that difference is the product's own argument.
 *
 * ## Vector tiles from OpenFreeMap, and the sovereignty path
 *
 * No API key, no account, ODbL. Coverage over the target region was verified before
 * committing to it — a z12 tile over Argungu, Kebbi returned 11 KB of real vector data, which
 * matters because "global coverage" often means detailed Europe and empty Sahel.
 *
 * Vector rather than raster for three reasons: crisp on the high-DPI phones this serves,
 * themeable to brand colours in-place, and a single style swap moves the whole thing to
 * self-hosted PMTiles on MinIO later — the same argument SearXNG and MinIO already make in
 * this codebase.
 *
 * ## Attribution is a licence condition
 *
 * MapLibre's own AttributionControl renders it from the style, and it is not removed. ODbL
 * requires it wherever the data appears.
 */

const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";

/**
 * Where MapLibre's worker is served from.
 *
 * ## Why this must be set explicitly
 *
 * MapLibre 6 derives the worker URL from `import.meta.url` and **returns an empty string** when
 * that is not an `http(s):` URL — which it is not once Turbopack has bundled the dynamic
 * import. `new Worker("")` then resolves against the current document, so the browser fetches
 * the *page* and refuses it:
 *
 *     Loading Worker from "http://localhost:3000/subscribe" was blocked because of a
 *     disallowed MIME type ("text/html")
 *
 * The map hangs on "Loading the map" indefinitely, and — this is the part that made it hard to
 * see — it never fires maplibre's `error` event, so the failure path below did not run either.
 * A user got a spinner with no explanation and no way forward.
 *
 * `scripts/copy-map-worker.mjs` puts the file in `public/` at predev/prebuild, so this is a
 * real URL served with the correct type. Absolute from the site root, not relative: this
 * component renders on `/subscribe` and on `/portal/areas`, and a relative path would resolve
 * differently on each.
 */
const WORKER_URL = "/maplibre-gl-worker.mjs";

/**
 * How long to wait for the style and first tiles before calling it a failure.
 *
 * There has to be a ceiling. MapLibre reports a *failed* style fetch through `error`, but a
 * worker that never answers produces no event at all — the map simply never loads, which is
 * exactly the bug above. Without a timeout the UI has no way to distinguish "slow rural
 * connection" from "broken", so it shows a spinner forever and the subscriber cannot proceed.
 *
 * 15 seconds is deliberately generous: this is a 2G-and-worse audience, and cutting a slow but
 * working map off at 5 seconds would hand people the fallback they do not need. The form works
 * without the map either way, which is what makes erring long safe.
 */
const LOAD_TIMEOUT_MS = 15_000;

/** Brand purple, matching `--brand-600`. Inline rather than read from CSS: MapLibre paint
 *  properties take colour values, not custom properties. */
const ACCENT = "#6a0dad";

/**
 * Whether this browser can run the map.
 *
 * ## It must test WebGL **2**, not WebGL 1
 *
 * MapLibre 6 is WebGL 2 only — it calls `getContext("webgl2")` and throws
 * `GPUInitializationError` when that returns null, with its own "WebGL2 is required to display
 * this map" text. An earlier version of this probe tested `getContext("webgl")`, which is
 * WebGL **1**, so it passed on browsers that cannot run the map and the map hung silently
 * afterwards.
 *
 * That is not a hypothetical: a default Firefox profile on this machine reports
 * `webgl=true webgl2=false`, which is exactly the combination the old probe let through. It is
 * also the combination on older Android WebView builds and on desktops whose drivers are
 * blocklisted for WebGL 2 but not WebGL 1 — so testing the wrong one fails open on real users
 * rather than on an edge case.
 *
 * ## Why probing at all, when MapLibre throws anyway
 *
 * Two reasons. It saves the 966 KB download for a library that cannot run, which on a metered
 * rural connection is the whole argument for the dynamic import. And a thrown constructor
 * leaves the map half-built with no `error` event, so without this the UI could only fall back
 * via the timeout — fifteen seconds of a spinner before the user is told anything.
 *
 * The context is released immediately: browsers cap simultaneous WebGL contexts (often 8-16),
 * and a leaked probe context would eventually starve the real map on a page that mounts this
 * repeatedly.
 *
 * Returns true on an unexpected throw — better to attempt the map and let the error path handle
 * it than to deny a working browser because the probe itself misbehaved.
 */
function webglAvailable(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("webgl2");
    if (!context) return false;
    const lose = context.getExtension("WEBGL_lose_context");
    lose?.loseContext();
    return true;
  } catch {
    return true;
  }
}

export default function MapCanvas({
  centre,
  bbox,
  ring,
  drawing,
  locating,
  onMapTap,
  onUnavailable,
}: {
  centre: { lat: number; lon: number } | null;
  /** The resolved area, drawn as a filled rectangle. */
  bbox: BBox | null;
  /** The outline being drawn, as `[lon, lat]` pairs. */
  ring: [number, number][] | null;
  drawing: boolean;
  locating: boolean;
  onMapTap: (lat: number, lon: number) => void;
  /**
   * Called once if the map cannot be used.
   *
   * Reported upward because the *parent* owns the consequences: "Draw the outline" needs a map
   * to tap, so offering it after this fires would hand the user a mode that cannot complete.
   * The map knowing it failed is not enough — the flow has to change.
   */
  onUnavailable?: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  // `any` because the type only exists once the dynamic import resolves, and importing the
  // type statically would defeat the code-splitting this component exists for.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const map = useRef<any>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  // Distinguished from a generic failure so the copy can be accurate: "your browser cannot
  // show the map" is actionable (open it in Chrome) where "the map could not load" is not.
  const [noWebgl, setNoWebgl] = useState(false);

  // Ref, not a dependency: notifying the parent must not re-run the mount effect, which would
  // tear down and rebuild the map — and on a flaky connection that turns one failure into a
  // loop of retries.
  const notifyUnavailable = useRef(onUnavailable);
  notifyUnavailable.current = onUnavailable;

  // Held in a ref so the tap handler registered once at mount always calls the CURRENT
  // callback. Without this, the handler would close over the first render's `onMapTap` and
  // silently stop working after a mode change.
  const tapHandler = useRef(onMapTap);
  tapHandler.current = onMapTap;

  // ---- mount ------------------------------------------------------------- //
  useEffect(() => {
    let cancelled = false;
    // Declared before the async work so the closures below can clear it. Assigning it after
    // the IIFE would leave it unassigned at the point they capture it — TypeScript catches
    // this, and at runtime a fast `load` would fail to cancel a timer that had not been set.
    let timer = 0;

    // Checked before the import so an unsupported browser never pays for the download.
    if (!webglAvailable()) {
      console.warn(
        "[AreaPicker] no WebGL 2 context — skipping the map and using the name-and-size " +
          "path. MapLibre 6 requires WebGL 2. Seen on Firefox profiles with WebGL 2 " +
          "disabled or driver-blocklisted, older Android webviews, and in-app browsers.",
      );
      setFailed(true);
      setNoWebgl(true);
      notifyUnavailable.current?.();
      return;
    }

    (async () => {
      try {
        const maplibre = await import("maplibre-gl");
        // The stylesheet is imported here too, so it is not in the route's CSS either.
        await import("maplibre-gl/dist/maplibre-gl.css");
        if (cancelled || !holder.current) return;

        // BEFORE constructing the map: the worker is spawned during construction, so setting
        // this afterwards is too late and the empty-string URL is already in flight.
        maplibre.setWorkerUrl(WORKER_URL);

        const instance = new maplibre.Map({
          container: holder.current,
          style: STYLE_URL,
          // Centred on Nigeria until a real position arrives. Not [0,0] — an empty ocean is
          // a confusing first frame, and it makes the first flyTo look like a bug.
          center: [8.0, 9.0],
          zoom: 5,
          // Pitch and rotation removed: this is a "which piece of ground" tool, and a
          // rotated map makes north-up assumptions wrong for no benefit.
          pitchWithRotate: false,
          dragRotate: false,
          attributionControl: { compact: true },
          /*
            Cooperative gestures: a one-finger drag scrolls the PAGE, two fingers pan the map.
            This is the single most important mobile behaviour here.

            Without it, a map placed mid-form is a scroll trap — a thumb that lands inside it
            while scrolling towards the size field pans the map instead of the page, and on a
            small screen the map fills enough of the viewport that the user cannot get past it.
            They conclude the form is broken.

            MapLibre shows its own "use two fingers" hint when a blocked gesture occurs, so the
            behaviour explains itself the first time it happens rather than needing instructions
            nobody reads. On a desktop with a mouse this changes nothing — drag still pans.
          */
          cooperativeGestures: true,
          /*
            Cap the zoom. Beyond ~z17 OpenFreeMap has no more detail for rural Nigeria, so
            further zooming produces a blurry, stretched tile that reads as a broken map rather
            than as "this is all the data there is". It also keeps a pinch gesture from
            overshooting into emptiness on a touch screen, which is easy to do and hard to
            recover from without a reset control.
          */
          maxZoom: 17,
        });

        instance.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
        instance.on("click", (e: { lngLat: { lat: number; lng: number } }) => {
          tapHandler.current(e.lngLat.lat, e.lngLat.lng);
        });
        instance.on("load", () => {
          if (cancelled) return;
          window.clearTimeout(timer);
          setReady(true);
        });
        // A failed style load leaves a grey box with no explanation. Surfaced instead, so the
        // fallback text tells the user the map is unavailable but the form still works.
        //
        // Not sufficient on its own — see `timer` below. A worker that never answers raises
        // nothing here, so this catches a bad style URL but not a bad worker.
        instance.on("error", (event: { error?: { message?: string } }) => {
          if (cancelled) return;
          window.clearTimeout(timer);
          // Logged, because "the map did not load" is unactionable without the reason and this
          // is the one screen where a subscriber defines what gets monitored. The user sees the
          // fallback copy; whoever they report it to needs the cause.
          console.error("[AreaPicker] map failed:", event?.error?.message ?? event);
          setFailed(true);
          notifyUnavailable.current?.();
        });

        map.current = instance;
      } catch (error) {
        if (cancelled) return;
        window.clearTimeout(timer);
        console.error("[AreaPicker] could not load MapLibre:", error);
        setFailed(true);
        notifyUnavailable.current?.();
      }
    })();

    // The backstop for a failure that emits no event at all. Cleared by `load`, `error`, and
    // the catch above, so it only fires when nothing else did.
    timer = window.setTimeout(() => {
      if (cancelled) return;
      console.error(
        `[AreaPicker] map did not load within ${LOAD_TIMEOUT_MS / 1000}s — falling back to ` +
          "the form. Check that /maplibre-gl-worker.mjs is served (scripts/copy-map-worker.mjs).",
      );
      setFailed(true);
      notifyUnavailable.current?.();
    }, LOAD_TIMEOUT_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      map.current?.remove();
      map.current = null;
    };
  }, []);

  // ---- follow the centre -------------------------------------------------- //
  useEffect(() => {
    if (!ready || !map.current || !centre) return;
    map.current.flyTo({
      center: [centre.lon, centre.lat],
      // z14 shows a village and its surrounding fields — close enough to recognise your own
      // ground, wide enough to see where it sits. z16 loses the context that makes it
      // recognisable.
      zoom: Math.max(map.current.getZoom(), 14),
      duration: 800,
    });
  }, [centre, ready]);

  // ---- draw the resolved rectangle ---------------------------------------- //
  useEffect(() => {
    if (!ready || !map.current) return;
    const m = map.current;

    const data = bbox
      ? {
          type: "Feature" as const,
          properties: {},
          geometry: {
            type: "Polygon" as const,
            coordinates: [
              [
                [bbox.west, bbox.south],
                [bbox.east, bbox.south],
                [bbox.east, bbox.north],
                [bbox.west, bbox.north],
                [bbox.west, bbox.south],
              ],
            ],
          },
        }
      : { type: "FeatureCollection" as const, features: [] };

    if (m.getSource("aoi")) {
      m.getSource("aoi").setData(data);
      return;
    }

    m.addSource("aoi", { type: "geojson", data });
    m.addLayer({
      id: "aoi-fill",
      type: "fill",
      source: "aoi",
      // Low opacity: the point is to see YOUR LAND through the shape. An opaque overlay hides
      // the very features — a river, a road, a rooftop — someone uses to recognise the place.
      paint: { "fill-color": ACCENT, "fill-opacity": 0.18 },
    });
    m.addLayer({
      id: "aoi-line",
      type: "line",
      source: "aoi",
      paint: { "line-color": ACCENT, "line-width": 2.5 },
    });
  }, [bbox, ready]);

  // ---- draw the outline in progress --------------------------------------- //
  useEffect(() => {
    if (!ready || !map.current) return;
    const m = map.current;

    const closed = ring && ring.length >= 3 ? [...ring, ring[0]] : null;
    const data = closed
      ? {
          type: "Feature" as const,
          properties: {},
          geometry: { type: "Polygon" as const, coordinates: [closed] },
        }
      : ring && ring.length > 0
        ? {
            // Fewer than three corners is not yet a polygon, so it is drawn as points —
            // rendering an invalid polygon makes MapLibre log errors and shows nothing,
            // which reads as "my taps did nothing".
            type: "FeatureCollection" as const,
            features: ring.map((c) => ({
              type: "Feature" as const,
              properties: {},
              geometry: { type: "Point" as const, coordinates: c },
            })),
          }
        : { type: "FeatureCollection" as const, features: [] };

    if (m.getSource("draw")) {
      m.getSource("draw").setData(data);
      return;
    }

    m.addSource("draw", { type: "geojson", data });
    m.addLayer({
      id: "draw-fill",
      type: "fill",
      source: "draw",
      paint: { "fill-color": ACCENT, "fill-opacity": 0.22 },
    });
    m.addLayer({
      id: "draw-line",
      type: "line",
      source: "draw",
      paint: { "line-color": ACCENT, "line-width": 2.5 },
    });
    m.addLayer({
      id: "draw-points",
      type: "circle",
      source: "draw",
      paint: {
        "circle-radius": 6,
        "circle-color": "#ffffff",
        "circle-stroke-color": ACCENT,
        "circle-stroke-width": 2.5,
      },
    });
  }, [ring, ready]);

  // No WebGL 2: render raster tiles instead of apologising.
  //
  // The subscriber still gets a map they can look at and tap, which is the whole point of this
  // screen — seeing whether the square covers their land. Falling back to a text field would
  // technically work and would lose exactly the thing the map is for.
  if (noWebgl) {
    return (
      <>
        <RasterMap
          centre={centre}
          bbox={bbox}
          onMapTap={onMapTap}
          locating={locating}
        />
        <p className="authform__hint">
          Showing a simplified map — this browser does not support the detailed one. Tap to move
          the pin; it works the same way.
        </p>
      </>
    );
  }

  // A different failure: WebGL 2 exists but the style, tiles or worker did not arrive. Raster
  // tiles come from a different host, so they are worth trying rather than giving up — a blocked
  // or slow vector endpoint says nothing about whether OSM's raster tiles are reachable.
  if (failed) {
    return (
      <>
        <RasterMap
          centre={centre}
          bbox={bbox}
          onMapTap={onMapTap}
          locating={locating}
        />
        <p className="authform__hint">
          The detailed map could not load, so this is the simpler version. Tap to move the pin —
          monitoring works exactly the same.
        </p>
      </>
    );
  }

  return (
    <div className="mapcanvas">
      <div ref={holder} className="mapcanvas__holder" />
      {/* Two distinct waiting states. "Loading the map" and "finding your location" are
          different things and take different amounts of time; one spinner for both leaves the
          user unsure what is happening. */}
      {!ready && <div className="mapcanvas__veil">Loading the map…</div>}
      {ready && locating && <div className="mapcanvas__veil">Finding your location…</div>}
      {ready && drawing && (
        <p className="mapcanvas__hint">Tap each corner of your field</p>
      )}
    </div>
  );
}
