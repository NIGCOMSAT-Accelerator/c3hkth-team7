"use client";

import { useState } from "react";

import { SOURCE_LABEL, type AssessmentTrack } from "@/lib/types";

/**
 * The per-track modules of a report card — one openable card per measured dimension.
 *
 * ## Why the card was split up
 *
 * The report card answers *what should I do*, in six rows. That is deliberately short: a farmer
 * decides in the first few seconds. It is the wrong shape for the question that arrives a moment
 * later — *what is actually happening on my field?*
 *
 * An alert classified as a vegetation anomaly still measured standing water, soil moisture, rainfall
 * and a malaria baseline. Those numbers existed and the only place they surfaced was a flat list of
 * English evidence sentences. Apple Weather is the reference here, and the pattern worth borrowing
 * is not the animation: it is that the summary is one glanceable answer and each contributing
 * measurement is its own module you can open. Precipitation, UV and wind are separate cards because
 * they are separate physical quantities with their own units and baselines. So are these.
 *
 * ## This component contains no agronomy, and that is the point
 *
 * Every threshold, every band, every sentence comes from `backend/app/dispatch/tracks.py`. The same
 * list renders as tables in the alert email. Duplicating a single number here — "warn above 25%" —
 * would let the email and this page describe one plot differently, which is exactly the drift the
 * shared email layout and `card_fields` were each written to end.
 *
 * ## Tap to open, and the first one starts open
 *
 * The server returns them most-relevant-first, so the leading module is the one that explains the
 * alert. Opening it by default means the important detail needs no interaction at all, while the
 * rest stay collapsed so the page remains scannable.
 *
 * A `<button>` per header rather than `<details>`: `<details>` gives no control over the summary's
 * focus ring or keyboard semantics on older Safari, and the disclosure state is needed in React
 * anyway to set the first module open.
 */
export default function TrackModules({ tracks }: { tracks?: AssessmentTrack[] }) {
  // The server's order is the ranking. Never re-sorted here — `weight` is carried for reference,
  // not for the client to reinterpret.
  const [open, setOpen] = useState<string | null>(tracks?.[0]?.key ?? null);

  // Absent means "we could not look", which is a real outcome of a fully clouded cycle. Rendering
  // an empty section header would read as a broken page; saying nothing is honest, and the card
  // above already carries the freshness caveat.
  if (!tracks?.length) return null;

  return (
    <section className="tracks">
      <h3 className="tracks__title">What we measured</h3>

      {tracks.map((track) => {
        const isOpen = open === track.key;
        return (
          <div className="track" key={track.key} data-open={isOpen}>
            <button
              type="button"
              className="track__head"
              aria-expanded={isOpen}
              aria-controls={`track-body-${track.key}`}
              onClick={() => setOpen(isOpen ? null : track.key)}
            >
              <span className="track__label">{track.label}</span>
              <span className="track__reading">{track.reading}</span>
              <span className="track__chevron" aria-hidden="true">
                {isOpen ? "−" : "+"}
              </span>
            </button>

            <div
              className="track__body"
              id={`track-body-${track.key}`}
              hidden={!isOpen}
            >
              <p className="track__meaning">{track.meaning}</p>

              {track.detail.length > 0 && (
                <dl className="track__detail">
                  {track.detail.map(([label, value]) => (
                    <div className="track__row" key={label}>
                      <dt>{label}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              )}

              {track.sources.length > 0 && (
                <p className="track__sources">
                  {/* Provenance a subscriber can check. `SOURCE_LABEL` falls through to the raw
                      key rather than hiding an unlabelled source — an unmapped dataset should look
                      like a missing label, not like no source at all. */}
                  Measured from{" "}
                  {track.sources.map((s) => SOURCE_LABEL[s] ?? s).join(", ")}
                </p>
              )}
            </div>
          </div>
        );
      })}
    </section>
  );
}
