"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import {
  CHANNEL_LABEL,
  type AreaOfInterest,
  type Channel,
  type ChannelBinding,
  type DeliveryMode,
} from "@/lib/types";

import { replaceChannels, setAreaDelivery, type ChannelState } from "./actions";

const INITIAL: ChannelState = { ok: false, message: "" };

/**
 * Where alerts arrive, and who sends them.
 *
 * ## Why this exists
 *
 * Channels were settable **only at signup**. There was no way to correct a mistyped phone number,
 * switch from email to WhatsApp, or raise a threshold after too many advisories — this page could
 * display the preferred channel and not change it. For a farmer who entered the wrong number,
 * alerts went nowhere permanently.
 *
 * ## Two independent controls, and the distinction matters
 *
 *   * **Channels** — the address and the severity floor. Per plot, or for all of them.
 *   * **Delivery mode** — whether SHELTER contacts the subscriber at all, or the aggregator who
 *     onboarded them relays it. Only meaningful for an aggregator-managed plot, so the selector
 *     appears only where the backend would accept it.
 *
 * They are separate forms rather than one, because they fail separately: a rejected address must
 * not discard a delivery-mode change the subscriber already made.
 *
 * ## Why the severity floor is a select and not a slider
 *
 * The ladder is five named categories with defined meanings, not a continuum. A slider would imply
 * intermediate values and would be unusable on a phone with a thumb.
 */

/**
 * Channels a subscriber may set for themselves.
 *
 * **Email only at this stage**, mirroring `MVP_CHANNELS` in the backend. WhatsApp, Telegram and
 * Signal are implemented and registered, but no real message has ever been delivered on them —
 * they carry no credentials, so a dispatch returns SKIPPED and the subscriber receives nothing. A
 * picker that accepts a phone number and then delivers silence is worse than one that does not
 * offer it, so the API refuses those bindings with a 422 and this list matches.
 *
 * `webhook` is in `MVP_CHANNELS` but deliberately NOT here: a per-subscriber webhook is an
 * integration needing a signing secret, configured under Webhooks. Offering it as a free-text
 * field would invite an unsigned URL.
 *
 * `nigcomsat_broadcast` is never a preference — it is the escalation the router fires when
 * terrestrial delivery fails, and it reaches a district rather than a person.
 *
 * Widening this means widening `MVP_CHANNELS` first, and only after a real message has landed.
 */
const OFFERED: Channel[] = ["email"];

const SEVERITIES: { value: string; label: string }[] = [
  { value: "advisory", label: "Advisory and up — everything" },
  { value: "watch", label: "Watch and up" },
  { value: "warning", label: "Warning and up" },
  { value: "emergency", label: "Emergency only" },
];

/**
 * The sensitivity dial — `min_score`, the continuous filter the severity ladder cannot express.
 *
 * ## Why this exists alongside "Send from"
 *
 * Severity has five steps, so between WATCH (score 0.40) and WARNING (0.60) sits a 0.20-wide band
 * in which every subscriber is treated identically — and that is exactly the band they disagree
 * about. An irrigated commercial farm wants everything from 0.30 up; a smallholder who loses a day's
 * labour reacting to a false alarm wants nothing under 0.55. Both are "Watch and up" today.
 *
 * ## Why named steps and not a free number input
 *
 * The stored value is continuous (the API accepts any 0–1 float), but a text field asking a farmer
 * for "0.55" is a field that receives `55`, `0,55` and `85%`. Worse, `min_score` above the score a
 * plot can realistically reach silences the channel permanently while looking like a valid setting.
 * Four named steps cannot express that mistake.
 *
 * A `<select>` rather than a range slider for the same reason `min_severity` is: a slider gives no
 * readable current value on a phone, and the numbers here need to be legible to be trusted.
 *
 * ## What it does NOT do
 *
 * It filters **delivery**, not assessment. Every reading is still measured, still stored, and still
 * shown in the portal — this decides whether we message you about it. Said on screen, because a
 * control that appeared to stop monitoring would be a reason not to touch it.
 */
const SENSITIVITIES: { value: string; label: string; help: string }[] = [
  {
    value: "",
    label: "Standard — follow the category above",
    help: "No extra filter. The severity setting decides on its own.",
  },
  {
    value: "0.3",
    label: "More sensitive — from 0.30",
    help: "Early, weaker signals included. Best if acting is cheap for you.",
  },
  {
    value: "0.45",
    label: "Balanced — from 0.45",
    help: "Skips the faintest readings inside a category.",
  },
  {
    value: "0.6",
    label: "Only strong signals — from 0.60",
    help: "Fewer messages. Some real but moderate risks will not reach you.",
  },
];

const MODES: { value: DeliveryMode; label: string; help: string }[] = [
  {
    value: "direct",
    label: "SHELTER contacts them",
    help: "We send to the channels below. The default.",
  },
  {
    value: "webhook",
    label: "We relay it ourselves",
    help: "SHELTER sends nothing directly — your webhook is the delivery, so yours is the only voice reaching this subscriber.",
  },
  {
    value: "both",
    label: "Both",
    help: "We contact them, and your webhook fires too — for your own record.",
  },
];

function Saving({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Saving…" : children}
    </button>
  );
}

function Notice({ state }: { state: ChannelState }) {
  if (!state.message) return null;
  return (
    <p
      className="authform__message"
      data-tone={state.ok ? "ok" : "error"}
      role="status"
      aria-live="polite"
    >
      {state.message}
    </p>
  );
}

/** One editable row. */
type Row = {
  channel: Channel;
  address: string;
  min_severity: string;
  aoi_id: string;
  /** `min_score` as a form string. `""` means no filter — see `SENSITIVITIES`. */
  min_score: string;
};

function toRows(bindings: ChannelBinding[]): Row[] {
  return bindings.map((b) => ({
    channel: b.channel,
    address: b.address,
    min_severity: b.min_severity,
    aoi_id: b.aoi_id ?? "",
    // A stored dial that is not one of the four presets (set through the API, or a preset we later
    // retune) must still round-trip rather than silently resetting to "Standard" on the next save.
    // `String(0.45)` matches the option value exactly; anything else falls through to the extra
    // option rendered below.
    min_score: b.min_score === null || b.min_score === undefined ? "" : String(b.min_score),
  }));
}

export default function AlertDelivery({
  subscriberId,
  bindings,
  areas,
  canRelay,
}: {
  subscriberId: string;
  bindings: ChannelBinding[];
  areas: AreaOfInterest[];
  /**
   * Whether any plot is aggregator-managed.
   *
   * Drives whether the delivery-mode section renders at all. Showing it to an individual would
   * offer a control the backend refuses — `webhook` needs an aggregator to relay, and without one
   * it would silence their alerts entirely.
   */
  canRelay: boolean;
}) {
  const [chState, saveChannels] = useActionState(replaceChannels, INITIAL);
  const [modeState, saveMode] = useActionState(setAreaDelivery, INITIAL);

  const [rows, setRows] = useState<Row[]>(
    // At least one row, so a subscriber with no channels is not shown an empty box with no
    // affordance — the commonest reason to open this page is that nothing is reaching them.
    bindings.length ? toRows(bindings) : [
      { channel: "email", address: "", min_severity: "advisory", aoi_id: "", min_score: "" },
    ],
  );

  const update = (index: number, patch: Partial<Row>) =>
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));

  return (
    <>
      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Where alerts arrive</h2>
          <p className="pcard__sub">
            One row per way of reaching you. Leave the area as{" "}
            <strong>All plots</strong> unless you want a different channel for one field —
            a row naming a plot <em>replaces</em> the general ones for that plot rather than
            adding to them, so you never get the same alert twice.
          </p>
        </div>

        <form action={saveChannels} className="wsform">
          <input type="hidden" name="subscriber_id" value={subscriberId} />

          <div className="chanrows">
            {rows.map((row, i) => (
              <div className="chanrow" key={i}>
                <label className="chanrow__cell">
                  <span className="authform__label">Channel</span>
                  <select
                    className="authform__input"
                    name={`ch_${i}_channel`}
                    value={row.channel}
                    onChange={(e) => update(i, { channel: e.target.value as Channel })}
                  >
                    {OFFERED.map((c) => (
                      <option key={c} value={c}>
                        {CHANNEL_LABEL[c] ?? c}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="chanrow__cell chanrow__cell--wide">
                  <span className="authform__label">
                    {row.channel === "email"
                      ? "Email address"
                      : row.channel === "telegram"
                        ? "Telegram chat id"
                        : "Phone number"}
                  </span>
                  <input
                    className="authform__input"
                    name={`ch_${i}_address`}
                    value={row.address}
                    onChange={(e) => update(i, { address: e.target.value })}
                    placeholder={
                      row.channel === "email" ? "you@example.com" : "+2348012345678"
                    }
                    autoComplete="off"
                  />
                </label>

                <label className="chanrow__cell">
                  <span className="authform__label">Send from</span>
                  <select
                    className="authform__input"
                    name={`ch_${i}_min`}
                    value={row.min_severity}
                    onChange={(e) => update(i, { min_severity: e.target.value })}
                  >
                    {SEVERITIES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="chanrow__cell">
                  <span className="authform__label">For</span>
                  <select
                    className="authform__input"
                    name={`ch_${i}_aoi`}
                    value={row.aoi_id}
                    onChange={(e) => update(i, { aoi_id: e.target.value })}
                  >
                    <option value="">All plots</option>
                    {areas.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                </label>

                {/*
                  The sensitivity dial. AFTER "Send from" and the plot, because it refines a
                  category rather than replacing it — someone reads "Watch and up, more sensitive"
                  in that order.
                */}
                <label className="chanrow__cell chanrow__cell--dial">
                  <span className="authform__label">Sensitivity</span>
                  <select
                    className="authform__input"
                    name={`ch_${i}_score`}
                    value={row.min_score}
                    onChange={(e) => update(i, { min_score: e.target.value })}
                  >
                    {SENSITIVITIES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                    {/*
                      A stored value outside the four presets — set through the Partner API, or left
                      behind if a preset is retuned. Rendered so it displays and round-trips instead
                      of silently snapping to "Standard" the next time this form is saved.
                    */}
                    {row.min_score !== "" &&
                      !SENSITIVITIES.some((s) => s.value === row.min_score) && (
                        <option value={row.min_score}>
                          Custom — from {Number(row.min_score).toFixed(2)}
                        </option>
                      )}
                  </select>
                  <span className="chanrow__help">
                    {SENSITIVITIES.find((s) => s.value === row.min_score)?.help ??
                      "Set through the API. Kept as-is unless you change it."}
                  </span>
                </label>

                {/* Clearing the address removes the row on save — see `replaceChannels`. Stated,
                    because a delete button that looks destructive gets avoided, and an empty
                    field that silently means "remove" gets misunderstood. */}
                <button
                  type="button"
                  className="linkbutton chanrow__drop"
                  onClick={() => update(i, { address: "" })}
                  disabled={!row.address}
                >
                  Clear
                </button>
              </div>
            ))}
          </div>

          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={() =>
              setRows((prev) => [
                ...prev,
                { channel: "email", address: "", min_severity: "advisory", aoi_id: "", min_score: "" },
              ])
            }
          >
            + Add another way to reach me
          </button>

          <p className="authform__hint">
            A cleared address is removed when you save. Keep at least one — with none, your
            plots are still watched and you are never told.
          </p>

          {/*
            The one thing a subscriber must understand before touching the dial, and the reason it
            is safe to offer at all: it changes what we SEND, never what we measure. Someone who
            believed it stopped monitoring would leave it alone; someone who believed it silenced
            everything would be wrong about the broadcast.
          */}
          <p className="authform__hint">
            <strong>Sensitivity changes what reaches you, not what we watch.</strong> Every plot is
            still assessed on every satellite pass whatever you choose here, and every reading stays
            on your <a href="/portal/areas">plots page</a> and in{" "}
            <a href="/portal/alerts">your alerts</a> — the dial only decides when we message you. At
            emergency level, satellite broadcast can still reach your district regardless, because it
            addresses an area rather than a person.
          </p>

          <div className="wsform__row">
            <Saving>Save delivery settings</Saving>
          </div>
          <Notice state={chState} />
        </form>
      </section>

      {canRelay && (
        <section className="pcard">
          <div className="pcard__head">
            <h2 className="pcard__title">Who sends the alert</h2>
            <p className="pcard__sub">
              For plots you manage on someone&rsquo;s behalf, you can be the only voice that
              reaches them. Choose <strong>We relay it ourselves</strong> and SHELTER sends
              nothing directly — your webhook receives the alert and you pass it on in your own
              words, on your own channel.
            </p>
          </div>

          {areas.map((area) => (
            <form action={saveMode} className="wsform delivrow" key={area.id}>
              <input type="hidden" name="subscriber_id" value={subscriberId} />
              <input type="hidden" name="aoi_id" value={area.id} />

              <div className="delivrow__head">
                <strong>{area.name}</strong>
                <span className="muted">
                  {area.hectares ? `about ${area.hectares} ha` : "monitored"}
                </span>
              </div>

              <div className="delivrow__modes">
                {MODES.map((m) => (
                  <label className="delivmode" key={m.value}>
                    <input
                      type="radio"
                      name="mode"
                      value={m.value}
                      defaultChecked={(area.delivery_mode ?? "direct") === m.value}
                    />
                    <span>
                      <strong>{m.label}</strong>
                      <span className="delivmode__help">{m.help}</span>
                    </span>
                  </label>
                ))}
              </div>

              <div className="wsform__row">
                <Saving>Save for this plot</Saving>
              </div>
            </form>
          ))}

          <Notice state={modeState} />

          <p className="authform__hint">
            Relaying needs a webhook that is receiving events. Set one up under{" "}
            <a href="/portal/webhooks">Webhooks</a> first — otherwise choosing it would leave
            nobody delivering the alert, and SHELTER will refuse the change.
          </p>
        </section>
      )}
    </>
  );
}
