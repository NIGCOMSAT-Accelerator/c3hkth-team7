"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  adminCentre,
  adminLgas,
  adminStates,
  adminWards,
  previewRing,
  resolveArea,
  searchPlaces,
  type ResolvedArea,
} from "@/app/subscribe/area-actions";
import type { PlaceResult } from "@/lib/types";

import MapCanvas from "./MapCanvas";

/**
 * Choose the area to monitor — without knowing what a bounding box is.
 *
 * ## The three ways in, in the order a person reaches for them
 *
 *   1. **"Use my location"** — the phone's GPS. One tap, most accurate, and the pattern
 *      every ride-hailing and delivery app has already taught. This is the default path.
 *   2. **Search a place** — "Argungu". For someone setting up on a laptop, or registering a
 *      field they are not standing in.
 *   3. **Tap the map** — for correcting either of the above, which is the common case: GPS
 *      puts you at the house, and the field is 300 m up the road.
 *
 * Size is a *separate* question asked in words ("5 hectares", "12 acres", "medium"), because
 * that is how people know it. `POST /places/resolve` turns the pair into a validated area;
 * this component never computes a bbox.
 *
 * ## Why the confirmation is a shape on a map and not a number
 *
 * Nobody can check whether "4.86 hectares" is right. Everybody can look at a square drawn
 * over their own village and say "that is too big" or "that is my field". So the resolved
 * area is drawn, and the size is described as football pitches beside it. The number is
 * shown, but it is not what the confirmation rests on.
 *
 * ## Progressive enhancement
 *
 * The parent form renders plain inputs that work with no JavaScript at all; this component
 * mounts over them. If the bundle fails on a weak connection, the fallback still submits a
 * place name and a size — the same two fields `resolve` needs — so the map is an
 * enhancement rather than a requirement. That matters on the devices this is for.
 */

type Mode = "locate" | "search" | "draw";

export default function AreaPicker({
  onResolved,
}: {
  /** Called whenever a valid area is settled, so the parent form can submit it. */
  onResolved: (area: ResolvedArea | null) => void;
}) {
  const [mode, setMode] = useState<Mode>("locate");

  /**
   * Whether the map is usable at all.
   *
   * Drives two changes, both necessary rather than cosmetic:
   *
   *   * **"Draw the outline" is withdrawn.** The raster fallback supports tap-to-place, but not
   *     the ring rendering that makes drawing an outline legible — you would be tapping corners
   *     you cannot see joined up. Placing a pin and stating a size still works, so the mode
   *     that degrades badly is removed and the two that degrade well are kept.
   *   * **The flow moves to "Search a place"**, which needs no map at all: a place name
   *     resolves to coordinates server-side through Nominatim.
   */
  const [mapDown, setMapDown] = useState(false);
  const [centre, setCentre] = useState<{ lat: number; lon: number } | null>(null);
  const [sizeText, setSizeText] = useState("");
  const [resolved, setResolved] = useState<ResolvedArea | null>(null);

  /**
   * Whether the subscriber has ticked "yes, this is the right place".
   *
   * **The parent form receives the area only while this is true.** Without that the card would
   * be decoration — and the failure it exists to prevent is precisely one that looked fine to
   * everything except a human reading the place name.
   *
   * Cleared by `resolve()` on every re-resolution, so a tick cannot survive a change to the pin
   * or the size. That is the part worth getting right: confirming a 2-hectare plot and then
   * nudging the pin 40 km must not leave the old confirmation standing.
   */
  const [confirmed, setConfirmed] = useState(false);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PlaceResult[]>([]);
  const [searching, setSearching] = useState(false);
  /**
   * Whether a search has completed and found nothing.
   *
   * Distinct from `results.length === 0`, which is also true before anyone has typed. The
   * difference is the whole point: "nothing yet" must stay silent, and "we looked and there is
   * nothing" must offer the way out.
   */
  const [searchedInVain, setSearchedInVain] = useState(false);

  // ---- the administrative fallback (state -> LGA) ------------------------- //
  //
  // Reached when a place name finds nothing, which for rural Nigeria is the NORMAL case rather
  // than an error: OSM has no entry for "Kobape, Ogun State" at all, though its LGA resolves
  // and GRID3 places the coordinates correctly.
  //
  // Before this existed, a failed search left an empty panel with no next step — and browser
  // geolocation silently filled the gap, which is how a farm in Ogun State came to be
  // registered in Warrington, England.
  const [browsing, setBrowsing] = useState(false);
  const [states, setStates] = useState<string[]>([]);
  const [pickedState, setPickedState] = useState("");
  const [lgas, setLgas] = useState<string[]>([]);
  const [pickedLga, setPickedLga] = useState("");
  /**
   * Wards in the chosen LGA, and the one selected.
   *
   * The ward tier is what makes the cascade genuinely useful: measured, Kajola ward is 18x16 km
   * inside Obafemi Owode's 58x63 km, which moves the reported farm from 22.4 km off map-centre
   * to 5.9 km. At the LGA extent the plot is not on screen at all.
   *
   * **Optional by necessity.** GRID3 has wards for 24 of 37 states; Lagos, Rivers, FCT and 11
   * others have none, and geoBoundaries publishes no ADM3 for Nigeria to fall back on. So an
   * empty list means "skip to the pin", never "something failed".
   */
  const [wards, setWards] = useState<string[]>([]);
  const [pickedWard, setPickedWard] = useState("");
  const [browseNote, setBrowseNote] = useState<string | null>(null);

  const [ring, setRing] = useState<[number, number][]>([]);
  const [ringInfo, setRingInfo] = useState<{
    hectares: number;
    ratio: number | null;
    reason: string | null;
  } | null>(null);

  const [busy, setBusy] = useState(false);
  const [locating, setLocating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Guards a slow response for a stale input from overwriting a newer one — without it,
  // typing a size then changing the pin can leave the older area on screen as if accepted.
  const requestId = useRef(0);

  // ---- resolve a pin + size into an area ---------------------------------- //
  const resolve = useCallback(
    async (lat: number, lon: number, size: string) => {
      const id = ++requestId.current;
      setBusy(true);
      // **Withdraw any previous confirmation immediately**, before the request completes.
      //
      // The pin or the size has changed, so whatever was ticked no longer describes what is on
      // screen. Clearing it here rather than on the response means there is no window in which
      // a stale confirmation is still submittable — and `onResolved(null)` retracts the area
      // from the parent form in the same breath, so the submit button disables until the new
      // area is confirmed in turn.
      setConfirmed(false);
      onResolved(null);

      const next = await resolveArea({ lat, lon, size });
      if (id !== requestId.current) return; // superseded
      setBusy(false);

      if (!next) {
        setNotice(
          "We could not work out that area just now. Check your connection and try again.",
        );
        return;
      }
      setNotice(null);
      setResolved(next);
      // Deliberately NOT `onResolved(next)`. The area reaches the form only once the
      // subscriber ticks the confirmation card — see `confirmed`.
    },
    [onResolved],
  );

  // Re-resolve when the pin or the size changes. Debounced, because the size is typed and a
  // request per keystroke would be pure cost on a metered connection.
  useEffect(() => {
    if (!centre || mode === "draw") return;
    const timer = window.setTimeout(() => {
      void resolve(centre.lat, centre.lon, sizeText);
    }, 450);
    return () => window.clearTimeout(timer);
  }, [centre, sizeText, mode, resolve]);

  // ---- 1. GPS ------------------------------------------------------------- //
  const useMyLocation = useCallback(() => {
    if (!navigator.geolocation) {
      setNotice(
        "This device cannot share its location. Search for your village instead, or tap the map.",
      );
      setMode("search");
      return;
    }

    setLocating(true);
    setNotice(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setLocating(false);
        setCentre({ lat: pos.coords.latitude, lon: pos.coords.longitude });
      },
      (err) => {
        setLocating(false);
        // Each failure needs a different next step, so they are not collapsed into one
        // message. "Denied" is a permission the user can grant; "unavailable" indoors is
        // not, and telling someone to retry when they cannot succeed is worse than useless.
        setNotice(
          err.code === err.PERMISSION_DENIED
            ? "Location permission was declined. You can search for your village instead, or tap the map."
            : "We could not get your location — this often fails indoors. Search for your village, or tap the map.",
        );
        setMode("search");
      },
      // 20s: a cold GPS fix on a low-end handset outdoors genuinely takes that long, and a
      // 5s timeout would report failure to someone who was about to succeed.
      { enableHighAccuracy: true, timeout: 20_000, maximumAge: 60_000 },
    );
  }, []);

  // ---- 2. place search ---------------------------------------------------- //
  useEffect(() => {
    if (mode !== "search" || query.trim().length < 3) {
      setResults([]);
      setSearchedInVain(false);
      return;
    }
    // 500ms: the backend serialises upstream calls to one per second to honour Nominatim's
    // policy, so a shorter debounce only queues requests it will then have to wait on.
    const timer = window.setTimeout(async () => {
      setSearching(true);
      const found = await searchPlaces(query);
      setResults(found);
      // Only now is an empty list meaningful. Set after the await, so the fallback does not
      // flash into view during the request.
      setSearchedInVain(found.length === 0);
      setSearching(false);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [query, mode]);

  // ---- 2b. the state list, loaded once the fallback is opened ------------- //
  //
  // Lazily, not on mount: most subscribers find their place by name or GPS and never see this,
  // so loading 37 states for everyone would be a request that usually buys nothing.
  useEffect(() => {
    if (!browsing || states.length > 0) return;
    let cancelled = false;
    (async () => {
      const loaded = await adminStates();
      if (!cancelled) setStates(loaded);
    })();
    return () => {
      cancelled = true;
    };
  }, [browsing, states.length]);

  // ---- 2c. LGAs for the chosen state -------------------------------------- //
  useEffect(() => {
    if (!pickedState) {
      setLgas([]);
      return;
    }
    let cancelled = false;
    setPickedLga("");
    (async () => {
      const loaded = await adminLgas(pickedState);
      if (!cancelled) setLgas(loaded);
    })();
    return () => {
      cancelled = true;
    };
  }, [pickedState]);

  // ---- 2d. wards for the chosen LGA, where they exist --------------------- //
  //
  // Loaded as soon as an LGA is picked, so the third dropdown appears without a second wait.
  // The empty case is silent: 13 states have no ward layer, and announcing "no wards available"
  // would read as a fault rather than the normal state it is.
  useEffect(() => {
    if (!pickedState || !pickedLga) {
      setWards([]);
      setPickedWard("");
      return;
    }
    let cancelled = false;
    setPickedWard("");
    (async () => {
      const loaded = await adminWards(pickedState, pickedLga);
      if (!cancelled) setWards(loaded);
    })();
    return () => {
      cancelled = true;
    };
  }, [pickedState, pickedLga]);

  // ---- 3. drawn outline --------------------------------------------------- //
  useEffect(() => {
    if (mode !== "draw" || ring.length < 3) {
      setRingInfo(null);
      return;
    }
    const timer = window.setTimeout(async () => {
      const closed: [number, number][] = [...ring, ring[0]];
      const preview = await previewRing(closed.map(([lon, lat]) => [lon, lat]));
      if (!preview) return;

      setRingInfo({
        hectares: preview.hectares ?? 0,
        ratio: preview.envelope_ratio ?? null,
        reason: preview.reason ?? null,
      });

      if (preview.monitorable && preview.ring) {
        // A drawn outline goes straight to the parent — no `resolve` call, because the
        // shape IS the answer and there is no size to guess.
        //
        // **And no confirmation card, deliberately.** The card exists because a resolved area
        // can land somewhere the subscriber never looked — a GPS fix or a geocoded name can be
        // 2,000 km wrong and still look like a number. A drawn outline cannot: they tapped each
        // corner on a map they were watching, so the geometry is already confirmed by the act of
        // producing it. Asking again would train people to tick past the one place it matters.
        //
        // `admin1`/`admin2` are null here because no reverse lookup ran; the backend fills them
        // through `eo/admin.enrich` when the scan is queued.
        const area: ResolvedArea = {
          area: {
            name: "",
            geometry: preview.ring,
            bbox: preview.bbox!,
            hectares: preview.hectares ?? null,
          },
          resolved_place: null,
          size_description: `${(preview.hectares ?? 0).toFixed(2)} hectares`,
          hectares: preview.hectares ?? 0,
          size_is_estimate: false,
          country: null,
          admin1: null,
          admin2: null,
          monitoring_cadence: "",
          attribution: "",
        };
        setResolved(area);
        onResolved(area);
      } else {
        onResolved(null);
      }
    }, 400);
    return () => window.clearTimeout(timer);
  }, [ring, mode, onResolved]);

  // GPS on mount: the fastest path to a correct answer, and asking is what every app the
  // user already knows does. If it is declined the handler falls back to search.
  useEffect(() => {
    useMyLocation();
    // Once, deliberately — re-prompting on every render would be hostile.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="picker">
      {/* Mode switch. Three real tabs rather than a hidden menu: on a phone every option
          must be one tap, and "I cannot find the button" is the commonest reason someone
          abandons a form. */}
      <div className="picker__modes" role="tablist" aria-label="How to choose your area">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "locate"}
          className="picker__mode"
          onClick={() => {
            setMode("locate");
            useMyLocation();
          }}
        >
          Use my location
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "search"}
          className="picker__mode"
          onClick={() => setMode("search")}
        >
          Search a place
        </button>
        {/*
          Withdrawn when the map is unavailable rather than disabled. A greyed-out tab invites
          the question "why can I not use this?" on the one screen where the answer does not
          help — and drawing an outline without a map to tap is not something we can offer at
          reduced quality, it is simply impossible.
        */}
        {!mapDown && (
          <button
            type="button"
            role="tab"
            aria-selected={mode === "draw"}
            className="picker__mode"
            onClick={() => {
              setMode("draw");
              setRing([]);
              onResolved(null);
            }}
          >
            Draw the outline
          </button>
        )}
      </div>

      {mode === "search" && (
        <div className="picker__search">
          <label htmlFor="picker-q" className="authform__label">
            Village, town or district
          </label>
          <input
            id="picker-q"
            className="authform__input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Argungu"
            autoComplete="off"
            // `search` so mobile keyboards show a search key rather than a newline.
            type="search"
          />
          {searching && <p className="authform__hint">Searching…</p>}
          {results.length > 0 && (
            <ul className="picker__results">
              {results.map((r, i) => (
                <li key={`${r.lat}-${r.lon}-${i}`}>
                  <button
                    type="button"
                    className="picker__result"
                    onClick={() => {
                      setCentre({ lat: r.lat, lon: r.lon });
                      setResults([]);
                      setQuery(r.label.split(",")[0]);
                    }}
                  >
                    {r.label}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/*
            Nothing found — offer the way out instead of an empty panel.

            **The wording does not apologise or blame the query.** The map data genuinely does
            not list most Nigerian villages: "Kobape, Ogun State" returns zero results while its
            LGA resolves fine. Telling someone their farm's name is wrong would be both untrue
            and demoralising at the one step that decides whether they finish signing up.

            This is the state that used to render nothing at all, which is how a subscriber fell
            through to browser geolocation and registered a Nigerian farm in England.
          */}
          {searchedInVain && !searching && (
            <div className="picker__fallback">
              <p className="authform__hint">
                No match for <strong>{query.trim()}</strong>. Smaller villages and farm names
                often are not on the map — that is normal and not a problem with the name.
              </p>

              {!browsing ? (
                <div className="picker__fallback-actions">
                  <button
                    type="button"
                    className="btn btn--ghost btn--small"
                    onClick={() => setBrowsing(true)}
                  >
                    Find it by State and LGA instead
                  </button>
                  {!mapDown && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--small"
                      onClick={() => {
                        setSearchedInVain(false);
                        setMode("locate");
                      }}
                    >
                      Drop a pin on the map
                    </button>
                  )}
                </div>
              ) : (
                <div className="picker__browse">
                  <label htmlFor="picker-state" className="authform__label">
                    State
                  </label>
                  <select
                    id="picker-state"
                    className="authform__input"
                    value={pickedState}
                    onChange={(e) => setPickedState(e.target.value)}
                  >
                    <option value="">Choose a state…</option>
                    {states.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>

                  {/* The LGA list only appears once a state is chosen — 774 LGAs in one
                      dropdown would be unusable on a phone. */}
                  {pickedState && (
                    <>
                      <label htmlFor="picker-lga" className="authform__label">
                        Local Government Area
                      </label>
                      <select
                        id="picker-lga"
                        className="authform__input"
                        value={pickedLga}
                        onChange={async (e) => {
                          const lga = e.target.value;
                          setPickedLga(lga);
                          if (!lga) return;
                          const centred = await adminCentre(pickedState, lga);
                          if (!centred) {
                            setBrowseNote(
                              "Could not load that boundary. Drop a pin on the map instead.",
                            );
                            return;
                          }
                          // Moves the MAP, and deliberately does not resolve an area: an LGA is
                          // tens of kilometres across, so treating it as the monitored footprint
                          // would average a whole district into one reading. The pin is next.
                          setCentre({ lat: centred.lat, lon: centred.lon });
                          setBrowseNote(centred.note);
                        }}
                      >
                        <option value="">Choose an LGA…</option>
                        {lgas.map((l) => (
                          <option key={l} value={l}>
                            {l}
                          </option>
                        ))}
                      </select>
                    </>
                  )}

                  {/*
                    Ward — the tier that makes this worth doing, and only shown where it exists.

                    Measured: Kajola ward is 18x16 km inside Obafemi Owode's 58x63 km, so choosing
                    it moves the reported farm from 22.4 km off map-centre to 5.9 km. At the LGA
                    extent the plot is simply not on screen.

                    Rendered only when `wards.length > 0`. GRID3 covers 24 of 37 states — Lagos,
                    Rivers, FCT and 11 others have none, and geoBoundaries publishes no ADM3 for
                    Nigeria to fall back on. In those states the step vanishes and the pin follows
                    the LGA directly, which is why nothing here announces "no wards found": that
                    would report a fault where there is only missing upstream data.
                  */}
                  {pickedLga && wards.length > 0 && (
                    <>
                      <label htmlFor="picker-ward" className="authform__label">
                        Ward <span className="authform__hint">(narrows the map)</span>
                      </label>
                      <select
                        id="picker-ward"
                        className="authform__input"
                        value={pickedWard}
                        onChange={async (e) => {
                          const ward = e.target.value;
                          setPickedWard(ward);
                          const centred = await adminCentre(
                            pickedState,
                            pickedLga,
                            ward || undefined,
                          );
                          if (!centred) return;
                          // Same rule as the LGA: recentre only. A ward is still ~18 km across,
                          // so it is where to look, never what to monitor.
                          setCentre({ lat: centred.lat, lon: centred.lon });
                          setBrowseNote(centred.note);
                        }}
                      >
                        <option value="">All of {pickedLga}</option>
                        {wards.map((w) => (
                          <option key={w} value={w}>
                            {w}
                          </option>
                        ))}
                      </select>
                    </>
                  )}

                  {browseNote && (
                    <p className="authform__hint" role="status">
                      {browseNote} Then tap your plot on the map below and enter its size.
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <MapCanvas
        onUnavailable={() => {
          setMapDown(true);
          // Move off any map-dependent mode. Done here rather than inside MapCanvas because
          // the mode is this component's state, and a child reaching up to change it would
          // make the flow's control flow impossible to follow.
          setMode((current) => (current === "draw" ? "search" : current));
          setRing([]);
        }}
        centre={centre}
        bbox={mode !== "draw" ? resolved?.area.bbox ?? null : null}
        ring={mode === "draw" ? ring : null}
        drawing={mode === "draw"}
        onMapTap={(lat, lon) => {
          if (mode === "draw") {
            setRing((prev) => [...prev, [lon, lat]]);
          } else {
            // Tapping in locate/search mode moves the pin. This is the correction path, and
            // it is the common case — GPS lands on the house and the field is up the road.
            setCentre({ lat, lon });
          }
        }}
        locating={locating}
      />

      {mode === "draw" ? (
        <div className="picker__panel">
          <p className="authform__hint">
            Tap each corner of your field. Three corners or more.{" "}
            {ring.length > 0 && `${ring.length} tapped.`}
          </p>
          {ring.length > 0 && (
            <button
              type="button"
              className="linkbutton"
              onClick={() => {
                setRing((prev) => prev.slice(0, -1));
                onResolved(null);
              }}
            >
              Undo last corner
            </button>
          )}
          {ringInfo?.reason && (
            <p className="authform__message" data-tone="error" role="alert">
              {ringInfo.reason}
            </p>
          )}
          {ringInfo && !ringInfo.reason && (
            <div className="picker__confirm">
              <strong>{ringInfo.hectares.toFixed(2)} hectares</strong>
              {ringInfo.ratio && ringInfo.ratio > 1.4 && (
                // Worth saying: this is exactly the case where the outline changed the
                // measurement, and a subscriber who understands that is more likely to draw
                // carefully next time.
                <span className="authform__hint">
                  Your field is an irregular shape, so drawing it means we measure your land
                  and not the ground around it.
                </span>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="picker__panel">
          <label htmlFor="picker-size" className="authform__label">
            About how big is it?
          </label>
          <input
            id="picker-size"
            className="authform__input"
            value={sizeText}
            onChange={(e) => setSizeText(e.target.value)}
            placeholder="5 hectares"
            autoComplete="off"
            aria-describedby="picker-size-hint"
          />
          <p id="picker-size-hint" className="authform__hint">
            Hectares, acres, plots — or just &ldquo;small&rdquo;, &ldquo;medium&rdquo; or
            &ldquo;large&rdquo; if you are not sure. We will show you the area on the map.
          </p>

          {notice && (
            <p className="authform__message" data-tone="error" role="status">
              {notice}
            </p>
          )}

          {busy && <p className="authform__hint">Working out the area…</p>}

          {resolved && !busy && (
            /*
              ## The confirmation card

              A subscriber registered a farm in "Kobape, Ogun State" and it was activated at
              Warrington, England — 2 hectares instead of 140. The old panel showed the size and
              the resolved place label, but **never the country or the LGA**, so there was nothing
              on screen contradicting what had happened.

              This states WHERE in administrative terms, because that is the claim a person can
              actually check: "Ogun, Nigeria" is either right or obviously wrong, whereas
              "-2.58, 53.41" means nothing to anyone. The geography guard in
              `api/area_input.py` now refuses the England case server-side, but a guard that
              rejects is a worse experience than a card that lets you notice.

              Confirmation is an explicit tick, not implicit in submitting the form. The parent
              form only receives the area once it is ticked — see `confirmed` below.
            */
            <div className="picker__confirm">
              <p className="picker__confirm-title">Check this is the right place</p>

              <dl className="picker__facts">
                {resolved.resolved_place && (
                  <>
                    <dt>Place</dt>
                    <dd>{resolved.resolved_place}</dd>
                  </>
                )}
                {/* Country and LGA are the fields whose absence let England through. Rendered
                    even when null, as "not identified" — a missing administrative name is
                    itself worth seeing, because Fahis searches on these to verify a warning. */}
                <dt>District</dt>
                <dd>
                  {[resolved.admin2, resolved.admin1].filter(Boolean).join(", ") ||
                    "not identified"}
                </dd>
                <dt>Country</dt>
                <dd>{resolved.country || "not identified"}</dd>
                <dt>Size</dt>
                <dd>
                  {resolved.hectares} hectares
                  <span className="picker__facts-aside"> · {resolved.size_description}</span>
                </dd>
              </dl>

              {resolved.size_is_estimate && (
                <p className="authform__hint">
                  The size is an estimate. If the square on the map looks wrong, change the size
                  above.
                </p>
              )}
              {resolved.monitoring_cadence && (
                <p className="authform__hint">{resolved.monitoring_cadence}</p>
              )}

              {/*
                An explicit tick rather than a passive "looks fine".

                Checkbox and not a button because the state persists visibly: someone who ticks,
                then nudges the pin, sees it clear and knows the confirmation lapsed. A button
                that had already been pressed leaves no such trace.
              */}
              <label className="picker__attest">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => {
                    setConfirmed(e.target.checked);
                    // The parent form receives the area only while it is ticked. This is what
                    // makes the confirmation load-bearing rather than decorative.
                    onResolved(e.target.checked ? resolved : null);
                  }}
                />
                <span>
                  Yes — this is {resolved.admin2 ? <strong>{resolved.admin2}</strong> : "the area"}
                  {resolved.country ? (
                    <>
                      {" "}
                      in <strong>{resolved.country}</strong>
                    </>
                  ) : null}
                  , and the shape on the map covers my land.
                </span>
              </label>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
