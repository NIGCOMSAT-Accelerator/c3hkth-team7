"use client";

import { useActionState, useEffect, useState } from "react";
import { useFormStatus } from "react-dom";

import AreaPicker from "@/components/AreaPicker/AreaPicker";
import EmptyState from "@/components/EmptyState";
import {
  HAZARD_TRACK,
  INTELLIGENCE,
  TRACK_META,
  confidenceBand,
} from "@/lib/intelligence";
import {
  HAZARD_LABEL,
  type AreaOfInterest,
  type ResolvedArea,
  type RiskAssessment,
  type Severity,
} from "@/lib/types";

import {
  addArea,
  reassessArea,
  removeArea,
  renameArea,
  type AreaState,
} from "./actions";

const INITIAL: AreaState = { ok: false, message: "" };

/**
 * Manage monitored plots: add another, rename one, stop monitoring one.
 *
 * ## Why there is no "move this plot" control
 *
 * The API supports a geometry change, for correcting a mis-dropped pin. This UI does not offer it,
 * because for the case a subscriber actually has — "this is a different field" — it is the wrong
 * answer. Past assessments measured the old footprint, so afterwards one timeline mixes readings
 * of two pieces of ground under a single name, and a "65% under standing water" from last week
 * would describe land they no longer monitor.
 *
 * Adding a new area is strictly better: there is no limit on how many you hold, each keeps a clean
 * history from its first satellite pass, and the old one can be removed. So that is what the page
 * offers, and it says why rather than leaving the absence to be discovered.
 */

function Submitting({
  children,
  blocked,
  blockedReason,
}: {
  children: string;
  /** Disable submission until the form is genuinely complete. */
  blocked?: boolean;
  /** Shown as a tooltip and announced, so a disabled button explains itself. */
  blockedReason?: string;
}) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      className="btn btn--primary"
      disabled={pending || blocked}
      title={blocked ? blockedReason : undefined}
      aria-describedby={blocked && blockedReason ? "submit-blocked" : undefined}
    >
      {pending ? "Saving…" : children}
    </button>
  );
}

/**
 * One plot's identity: read by default, editable on demand.
 *
 * ## Why this is not two text inputs and a Save button
 *
 * It was, and that was the bug. Two filled input boxes and a permanent "Save name" read as a form
 * left half-finished — the page looked like it was *always in edit mode*, so the honest state
 * ("this plot is called X and it is being watched") was never actually displayed. Worse, the
 * commonest interaction on this page is reading, and the rarest is renaming, so the layout
 * optimised for the exception.
 *
 * Now the name and crop are **text** until the subscriber asks to change them. Edit mode is
 * entered deliberately, and left automatically the moment a save succeeds — the two halves of
 * "on-demand" that make it feel finished rather than abandoned.
 *
 * ## Why the state indicator is a glass pill
 *
 * A plot is either being scanned or it is not, and that is the single most important fact about it.
 * `--liquid-*` tokens give it a translucent, lit surface that reads as *live* — an active pill has
 * a soft pulse; an inactive one is flat and unlit. Both carry a text label, because a colour and a
 * glow alone would fail a colourblind reader and a screenshot.
 */
function PlotIdentity({
  area,
  rename,
  renameState,
}: {
  area: AreaOfInterest;
  rename: (formData: FormData) => void;
  renameState: AreaState;
}) {
  const [editing, setEditing] = useState(false);

  // Leave edit mode when the save that just succeeded was OURS.
  //
  // `renameState` is shared across every plot on the page — one action, one state — so keying the
  // exit on `ok` alone would close whichever row happened to be open when a *different* row saved.
  // The action returns the id it changed; comparing it is what makes the exit specific.
  const savedThisOne = renameState.ok && renameState.savedAoiId === area.id;
  if (editing && savedThisOne) setEditing(false);

  const centre = `${((area.bbox.south + area.bbox.north) / 2).toFixed(4)}, ${(
    (area.bbox.west + area.bbox.east) /
    2
  ).toFixed(4)}`;

  if (!editing) {
    return (
      <div className="plot">
        <div className="plot__ident">
          <div>
            <h3 className="plot__name">{area.name}</h3>
            <p className="plot__crop">
              {area.crop ? (
                area.crop
              ) : (
                // Stated, not blank. A missing crop is normal and supported; an empty line reads
                // as a load failure, and the prompt is also where someone learns they can add it.
                <span className="muted">No crop recorded</span>
              )}
            </p>
          </div>

          {/* Live state, as a glass pill. Text + shape, never colour alone. */}
          <span className="glasspill glasspill--on" title="Scanned on every satellite pass">
            <span className="glasspill__dot" aria-hidden="true" />
            Active
          </span>
        </div>

        <p className="plot__facts">
          {/* Centre point, not the four corners — one lat/long can be checked against a phone's
              map app, which is the entire reason for showing it. Corner numbers cannot. */}
          Centre <span className="mono">{centre}</span>
          {area.hectares ? ` · about ${area.hectares} ha` : ""} ·{" "}
          <span className="mono">{area.id}</span>
        </p>

        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => setEditing(true)}
        >
          Edit name or crop
        </button>
      </div>
    );
  }

  return (
    <form action={rename} className="plot plot--editing">
      <input type="hidden" name="aoi_id" value={area.id} />

      <div className="plot__ident">
        <span className="plot__editing">Editing</span>
        <span className="glasspill glasspill--on" title="Monitoring continues while you edit">
          <span className="glasspill__dot" aria-hidden="true" />
          Still active
        </span>
      </div>

      <div className="arearow__grid">
        <div>
          <label className="authform__label" htmlFor={`name-${area.id}`}>
            Plot name
          </label>
          <input
            id={`name-${area.id}`}
            name="name"
            className="authform__input"
            defaultValue={area.name}
            maxLength={120}
            required
            // Focus lands in the field the button promised to edit, so a keyboard or screen-reader
            // user is not dropped at the top of a changed form.
            autoFocus
          />
        </div>
        <div>
          <label className="authform__label" htmlFor={`crop-${area.id}`}>
            Crop (optional)
          </label>
          <input
            id={`crop-${area.id}`}
            name="crop"
            className="authform__input"
            defaultValue={area.crop ?? ""}
            placeholder="maize, rice, oil palm…"
            maxLength={60}
          />
        </div>
      </div>

      {/* The reassurance a cautious subscriber wants BEFORE pressing save, not after. */}
      <p className="authform__hint">
        The ground being watched does not change, so every past assessment stays attached to this
        plot.
      </p>

      <div className="wsform__row">
        <Submitting>Save changes</Submitting>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setEditing(false)}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

/**
 * "Check now" — assess this plot against live satellite data, on demand.
 *
 * ## Why the pending label says what it is doing
 *
 * This is a 10-40 second wait, which is far too long for a spinner and a disabled button: at
 * that length silence reads as a hang, and the subscriber presses again or leaves. So the label
 * names the work — the request really is fetching imagery and running inference, and saying so
 * is both honest and the reassurance that something is happening.
 *
 * `useFormStatus` rather than the action's own state, because `pending` has to be read from
 * inside the form element to be scoped to *this* row. One `useActionState` serves every plot,
 * so its state cannot tell one row's in-flight submission from another's.
 *
 * ## Why it is a form and not an onClick
 *
 * A Server Action invoked through a form works with JavaScript still loading, which is the
 * common case on the connections this product serves — the whole portal is SSR for that reason.
 * An onClick handler would be dead until hydration finishes.
 */
function CheckNow({ area, reassess }: { area: AreaOfInterest; reassess: (formData: FormData) => void }) {
  return (
    <form action={reassess} className="monpanel__refresh">
      <input type="hidden" name="aoi_id" value={area.id} />
      <CheckNowButton />
      <span className="monpanel__refreshHint">
        Queries Sentinel-1, Sentinel-2 and the rainfall chain for this plot right now. Takes up
        to a minute, and sends no alert — it only updates the reading above.
      </span>
    </form>
  );
}

function CheckNowButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      className="btn btn--ghost btn--small"
      disabled={pending}
      // Announced rather than merely visual: the label change is the only feedback for the
      // ~40 seconds this runs, so a screen-reader user needs it spoken.
      aria-live="polite"
    >
      {pending ? "Reading satellite data…" : "Check now"}
    </button>
  );
}

function Notice({ state }: { state: AreaState }) {
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

/**
 * What monitoring is currently saying about one plot.
 *
 * ## Why this is a panel rather than a badge
 *
 * A severity badge alone tells a subscriber which colour their situation is, not what to do about
 * it. "Watch" is not a self-explanatory word, and someone deciding whether to move stored grain
 * tonight cannot act on a purple triangle.
 *
 * So each plot states four things: which intelligence track the finding belongs to, the category
 * and what it means, how confident the measurement is *and why that matters*, and the evidence the
 * conclusion rests on. That last one is the product's own argument — every figure comes from a
 * measurement, so showing them is what separates this from a guess.
 *
 * ## The "no assessment yet" case is deliberately distinct
 *
 * A plot added two minutes ago has no reading, and that is success — the scan is queued. Rendering
 * it as an error, or as an empty severity, would tell a new subscriber their monitoring is broken
 * at exactly the moment it started working.
 */
function MonitoringPanel({
  area,
  assessment,
  reassess,
}: {
  area: AreaOfInterest;
  assessment: RiskAssessment | null;
  reassess: (formData: FormData) => void;
}) {
  if (!assessment) {
    return (
      <div className="monpanel monpanel--pending">
        <span className="monpanel__pill">First scan queued</span>
        <p className="monpanel__meaning">
          This plot is in the watch loop. The first reading arrives on the next satellite pass —
          usually within hours, and at most a few days if cloud is heavy.
        </p>
        {/*
          Offered here too, and this is the state where it matters most: a subscriber who has
          just added a plot wants to see monitoring work now, not on a cycle boundary hours
          away. It is also the honest demonstration that the reading is fetched rather than
          stored — there is nothing cached for this plot yet, so a result can only come from a
          live query.
        */}
        <CheckNow area={area} reassess={reassess} />
      </div>
    );
  }

  const category = INTELLIGENCE[assessment.severity];
  const track = HAZARD_TRACK[assessment.hazard];
  const trackMeta = TRACK_META[track];
  const band = confidenceBand(assessment.confidence);

  return (
    <div className="monpanel" data-tone={category.tone}>
      <div className="monpanel__head">
        <span className="monpanel__pill" data-tone={category.tone}>
          {/* Icon AND text, never colour alone — the severity palette is a reserved status
              palette and has to survive a colourblind reader and a monochrome screenshot. */}
          {category.label}
        </span>
        <span className="monpanel__hazard">{HAZARD_LABEL[assessment.hazard]}</span>
        <span className="monpanel__track">
          {trackMeta.short}
          {trackMeta.status === "next" && (
            <span className="monpanel__phase" title={trackMeta.scope}>
              next phase
            </span>
          )}
        </span>
      </div>

      <p className="monpanel__meaning">{category.meaning}</p>

      <div className="monpanel__facts">
        <span>
          <strong>What to do</strong>
          {category.response}
        </span>
        <span>
          <strong>How soon</strong>
          {category.urgency}
        </span>
        <span>
          <strong>{band.label}</strong>
          {Math.round(assessment.confidence * 100)}% — {band.detail}
        </span>
      </div>

      {/*
        The report card — Apple's overview-plus-drilldown, applied here.

        Everything above answers "how bad"; these rows answer the questions that decide whether to
        act TODAY: is it moving, is it unusual for this field, when was it last actually seen, and
        what should I do about the water. The evidence stays one tap away in the <details> below,
        which is the whole pattern: answer first, reasoning on demand.

        Each row is omitted when its input is unknown. A first assessment has no previous run and a
        new plot has no fitted baseline — "no change" and "normal" would both be claims we cannot
        make, and the same rule governs the email card.
      */}
      <dl className="glance">
        {assessment.change?.direction && assessment.change.previous_severity && (
          <>
            <dt>Since last check</dt>
            <dd data-trend={assessment.change.direction}>
              {assessment.change.direction === "up"
                ? "Rising"
                : assessment.change.direction === "down"
                  ? "Easing"
                  : "Unchanged"}{" "}
              <span className="glance__aside">
                was {assessment.change.previous_severity.toUpperCase()}
              </span>
            </dd>
          </>
        )}

        {assessment.change?.vs_seasonal && (
          <>
            <dt>Compared with normal</dt>
            <dd>
              {assessment.change.vs_seasonal === "normal"
                ? "About usual for this field at this time of year"
                : `${assessment.change.vs_seasonal[0].toUpperCase()}${assessment.change.vs_seasonal.slice(1)} than this field usually is now`}
            </dd>
          </>
        )}

        {assessment.soil_moisture?.available &&
          assessment.soil_moisture.irrigation_advice && (
            <>
              <dt>Soil water</dt>
              <dd>
                {assessment.soil_moisture.irrigation_advice === "irrigate"
                  ? "Irrigate"
                  : assessment.soil_moisture.irrigation_advice === "drain"
                    ? "Do not irrigate — drain if you can"
                    : "No irrigation needed"}{" "}
                <span className="glance__aside">
                  {assessment.soil_moisture.volumetric.toFixed(2)} m³/m³
                </span>
              </dd>
            </>
          )}

        {/* Freshness. A "no flooding detected" from a six-day-old pass is a different claim
            from one taken this morning, and nothing else on this card says which it is. */}
        {assessment.freshness?.observed_at && (
          <>
            <dt>Last look</dt>
            <dd>
              {new Date(assessment.freshness.observed_at).toLocaleString()}
              {assessment.freshness.platform && (
                <span className="glance__aside"> · {assessment.freshness.platform}</span>
              )}
            </dd>
          </>
        )}

        {assessment.freshness?.next_expected && (
          <>
            <dt>Next expected</dt>
            <dd>
              Around{" "}
              {new Date(assessment.freshness.next_expected).toLocaleDateString()}
            </dd>
          </>
        )}
      </dl>

      {/* An absent measurement is a fact about the reading, so it is stated rather than left to
          be inferred from a missing number. */}
      {assessment.freshness?.caveat && (
        <p className="glance__caveat">{assessment.freshness.caveat}</p>
      )}

      {assessment.evidence.length > 0 && (
        <details className="monpanel__evidence">
          <summary>What this is based on</summary>
          <ul>
            {assessment.evidence.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
          {assessment.data_sources.length > 0 && (
            <p className="monpanel__sources">
              Measured from {assessment.data_sources.join(", ")}. Last checked{" "}
              {new Date(assessment.assessed_at).toLocaleString()}.
            </p>
          )}
        </details>
      )}

      {assessment.cascade.length > 0 && (
        <p className="monpanel__cascade">
          Could lead to:{" "}
          {assessment.cascade.map((h) => HAZARD_LABEL[h]).join(", ")}
        </p>
      )}

      {/*
        At the FOOT of the panel, below the evidence.

        The reading is what someone came for, and "Last look" in the glance rows already says how
        fresh it is — so the refresh belongs after the answer, as the thing you reach for when the
        timestamp is older than you want. Putting it at the top would offer work before
        information.
      */}
      <CheckNow area={area} reassess={reassess} />
    </div>
  );
}

/**
 * The confirmation after activating a plot.
 *
 * ## Why a panel and not a sentence
 *
 * The commonest real failure here is not an error — it is a **correct-looking success over the
 * wrong piece of ground**. A mis-typed place name or a pin dropped one village over produces a
 * green message and monitoring of somebody else's field, and the subscriber finds out weeks later
 * when an alert makes no sense.
 *
 * So the confirmation echoes back what was actually registered: the name, the size, the centre
 * point they can check against their phone's map, and what happens next. That check is only
 * possible in the one moment they are still looking at the screen, which is why it belongs here
 * rather than in an email.
 */
function ActivationSummary({
  state,
  onAddAnother,
  formOpen,
}: {
  state: AreaState;
  /** Reopens the form. Passed in because the form's open/closed state lives in the parent. */
  onAddAnother: () => void;
  /** True while the add form is open, which hides this panel's own action. */
  formOpen: boolean;
}) {
  if (!state.ok || !state.activated) return null;
  const a = state.activated;

  return (
    <div className="actsummary" role="status" aria-live="polite">
      <div className="actsummary__head">
        <span className="actsummary__tick" aria-hidden="true">
          ✓
        </span>
        <strong>Monitoring active — {a.name}</strong>
      </div>

      <dl className="actsummary__grid">
        <div>
          <dt>Area</dt>
          <dd>{a.hectares ? `about ${a.hectares} hectares` : "recorded"}</dd>
        </div>
        <div>
          <dt>Centre point</dt>
          {/* Offered for checking, not for decoration — the one field that catches a plot
              registered over the wrong ground. */}
          <dd className="mono">{a.centre ?? "recorded"}</dd>
        </div>
        {a.crop && (
          <div>
            <dt>Crop</dt>
            <dd>{a.crop}</dd>
          </div>
        )}
        <div>
          <dt>Reference</dt>
          <dd className="mono">{a.aoiId}</dd>
        </div>
      </dl>

      <p className="actsummary__next">
        <strong>What happens now:</strong> the first scan is queued and usually lands within
        hours. After that SHELTER checks this plot on every satellite pass, around every six
        hours, and keeps doing so whether or not you are signed in. You will only hear from us
        when something needs action — silence means nothing was found.
      </p>
      <p className="actsummary__check">
        Does the centre point look right? If not, add the correct plot and stop monitoring this
        one — it takes a moment now and saves a season of alerts about the wrong field.
      </p>

      {/*
        An explicit way forward, because the form is now closed.

        Hidden once the form reopens: `useActionState` has no reset, so this summary stays mounted
        while the next plot is entered. Keeping the panel is useful — the subscriber can still see
        and check what they just created — but leaving a live "add another" button on it while the
        form is already open would be a control that does nothing.

        Reported: after adding a plot the form stayed open with the previous entry still in it and
        this summary rendered underneath — so it read as though nothing had happened. Closing the
        form fixes that but leaves a dead end, and "add another" is the one thing a subscriber is
        most likely to want next: several scattered plots is the normal case, not an edge one.

        A button rather than a link: it reopens the form in place, so the summary stays visible
        above it and the subscriber can still check what they just created.
      */}
      {!formOpen && (
        <div className="actsummary__actions">
          <button type="button" className="btn btn--ghost btn--small" onClick={onAddAnother}>
            + Add another plot
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * What SHELTER can tell you, and which categories of intelligence exist.
 *
 * ## Why this is on the page at all
 *
 * A subscriber cannot decide how to treat an alert without knowing what the categories mean, and
 * they cannot set a sensible delivery threshold without seeing the ladder. Both are decisions the
 * product asks them to make, so the vocabulary belongs where they make them — not only in an
 * onboarding email nobody re-reads.
 *
 * ## And why the tracks are named honestly
 *
 * SHELTER turns Earth Observation into intelligence across three tracks. Agricultural is live in
 * this MVP; Environmental and Public Health follow. Marking them "next phase" rather than
 * presenting three equal options is the difference between a roadmap and a promise — a track shown
 * as available that delivers nothing is the worse failure, and it is the one this product is
 * careful about everywhere else.
 */
function IntelligenceLegend() {
  const order: Severity[] = ["info", "advisory", "watch", "warning", "emergency"];

  return (
    <section className="pcard">
      <div className="pcard__head">
        <h2 className="pcard__title">How to read your intelligence</h2>
        <p className="pcard__sub">
          Every reading carries a category and a confidence level, so you decide how to treat it.
          Set which categories reach you, and on which channel, in{" "}
          <a href="/portal/settings">Settings</a>.
        </p>
      </div>

      <ul className="legend">
        {order.map((severity) => {
          const c = INTELLIGENCE[severity];
          return (
            <li className="legend__row" key={severity} data-tone={c.tone}>
              <span className="monpanel__pill" data-tone={c.tone}>
                {c.label}
              </span>
              <div>
                <p className="legend__meaning">{c.meaning}</p>
                <p className="legend__response">
                  <strong>{c.urgency}.</strong> {c.response}
                </p>
              </div>
            </li>
          );
        })}
      </ul>

      <div className="legend__tracks">
        <h3 className="legend__tracksTitle">Intelligence tracks</h3>
        <p className="authform__hint">
          SHELTER turns satellite Earth Observation into intelligence across three tracks. This
          release delivers Agricultural Intelligence; the others follow.
        </p>
        <ul className="legend__trackList">
          {(["agricultural", "environmental", "public_health"] as const).map((track) => {
            const meta = TRACK_META[track];
            return (
              <li key={track}>
                <span className="legend__trackName">
                  {meta.label}
                  <span
                    className={
                      meta.status === "live"
                        ? "chip chip--on"
                        : "chip chip--quiet"
                    }
                  >
                    {meta.status === "live" ? "Live now" : "Next phase"}
                  </span>
                </span>
                <span className="legend__trackScope">{meta.scope}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}

const PLOT_ICON = (
  <svg
    width="52"
    height="52"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.3"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M4 9.5 12 4l8 5.5v10H4z" />
    <path d="M9 19.5v-6h6v6" opacity="0.6" />
  </svg>
);

export default function AreaManager({
  areas,
  assessments,
  canRemove,
}: {
  areas: AreaOfInterest[];
  /** Latest reading per `aoi_id`. Null for a plot whose first scan has not landed yet. */
  assessments: Record<string, RiskAssessment | null>;
  /** False when this is the only area — removing it would leave the subscription watching
   *  nowhere, which the backend refuses with a 409. The control is absent rather than
   *  disabled, since it will never be available while one area remains. */
  canRemove: boolean;
}) {
  const [renameState, rename] = useActionState(renameArea, INITIAL);
  const [removeState, remove] = useActionState(removeArea, INITIAL);
  const [addState, add] = useActionState(addArea, INITIAL);
  const [reassessState, reassess] = useActionState(reassessArea, INITIAL);

  const [resolved, setResolved] = useState<ResolvedArea | null>(null);
  const [adding, setAdding] = useState(false);

  /**
   * Close the form once a plot is actually created.
   *
   * ## Why an effect rather than a callback
   *
   * `useActionState` gives no success hook — the result arrives as new state on the next render,
   * so watching it is the only place that knows. Reported: after adding a plot the form stayed
   * open with the previous plot's name and location still in it, and the "Monitoring active"
   * summary rendered *underneath* the open form. It read as though nothing had happened, or worse,
   * as though a second plot were half-entered.
   *
   * Keyed on `addState.activated?.aoiId` rather than on `addState.ok`, so adding two plots in a row
   * collapses the form both times. A boolean would stay true after the first, and the effect
   * would not fire again.
   *
   * The resolved area is cleared too. Leaving it would let a stale location submit against a new
   * plot name — the same staleness the picker's confirmation tick guards against.
   */
  const createdId = addState.ok ? addState.activated?.aoiId : undefined;
  useEffect(() => {
    if (!createdId) return;
    setAdding(false);
    setResolved(null);
  }, [createdId]);

  return (
    <>
      <section className="pcard">
        {/*
          With nothing monitored, "0 plots monitored" above an empty list reads as broken rather
          than as new — so the heading is replaced by an explanation and one action.

          The picker is NOT moved into a modal, unlike the create flows on Webhooks, API keys, Team
          and Workspace. It carries a map and a state → LGA → ward cascade, and a map inside a
          scrolling dialog on a phone is materially worse than a dedicated panel: the two compete
          for the same drag gesture. The panel is already collapsed behind "+ Add a plot", which is
          what the modal pattern is for.
        */}
        {areas.length === 0 ? (
          <EmptyState
            icon={PLOT_ICON}
            title="No plots monitored yet"
            body={
              <>
                Add your first field and SHELTER starts watching it on the next satellite pass
                &mdash; radar for standing water, optical for crop stress, with rainfall and soil
                moisture alongside. You describe where it is by name, by dropping a pin, or by
                tracing its outline. There is no limit on how many you add.
              </>
            }
            actionLabel="+ Add your first plot"
            onAction={() => setAdding(true)}
          />
        ) : (
          <div className="pcard__head">
            <h2 className="pcard__title">
              {areas.length} {areas.length === 1 ? "plot" : "plots"} monitored
            </h2>
            <p className="pcard__sub">
              Each is assessed independently on every satellite pass. You can monitor as many as
              you farm.
            </p>
          </div>
        )}

        {areas.map((area) => (
          <div className="arearow" key={area.id}>
            {/* Status first, form second. Someone opening this page wants to know what is
                happening before they want to edit anything. */}
            <MonitoringPanel
              area={area}
              assessment={assessments[area.id] ?? null}
              reassess={reassess}
            />

            {/*
              The reassessment result, on the row it belongs to.

              One `useActionState` serves every plot, so rendering this in the shared notice block
              at the foot of the card would put "Riverside field reassessed" under a list of five
              plots with nothing tying it to one of them. `savedAoiId` is the same mechanism
              `PlotIdentity` uses to close the right editor — reused here rather than adding a
              second convention for the same problem.
            */}
            {reassessState.message && reassessState.savedAoiId === area.id && (
              <Notice state={reassessState} />
            )}

            {/* Read by default; edit on request. See `PlotIdentity` for why. */}
            <PlotIdentity area={area} rename={rename} renameState={renameState} />

            {canRemove && (
              <form action={remove} className="wsform__danger">
                <input type="hidden" name="aoi_id" value={area.id} />
                <input type="hidden" name="name" value={area.name} />
                <button type="submit" className="btn btn--ghost btn--small">
                  Stop monitoring this plot
                </button>
                <span className="wsform__dangerHint">
                  Future scans stop. Past alerts stay in your history.
                </span>
              </form>
            )}
          </div>
        ))}

        <Notice state={renameState} />
        <Notice state={removeState} />

        {/*
          The honest note about what renaming does and does not do. Placed here rather than in a
          tooltip because it answers the question a cautious subscriber actually has before
          pressing Save: "will this lose my history?"
        */}
        <p className="authform__hint">
          Renaming a plot or changing its crop keeps every past assessment — the ground being
          watched has not changed. To monitor a <em>different</em> piece of land, add it below
          rather than editing this one, so each plot keeps its own clear history.
        </p>
      </section>

      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Add another plot</h2>
          <p className="pcard__sub">
            There is no limit. A new plot is scanned as soon as you add it, rather than waiting
            for the next cycle.
          </p>
        </div>

        {/*
          Three states, not two: closed, open, and "one was just created".

          The third matters. After a successful add the summary below carries its own
          "+ Add another plot", so showing this button too would put two identical controls on the
          card — and the summary is where the eye already is, because it is what just appeared.
        */}
        {!adding && !addState.ok ? (
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => setAdding(true)}
          >
            + Add a plot
          </button>
        ) : !adding ? null : (
          <form
            action={add}
            className="wsform"
            // **Enter must not submit this form.**
            //
            // Reported: typing a plot name and pressing Enter submitted the form before a
            // location had been chosen. The server rejects an empty `resolved_area`, so nothing
            // invalid was saved — but the picker's own inputs (place search especially) live
            // inside this form, and Enter in a search box is the natural way to run a search,
            // not to finish the whole setup.
            //
            // Suppressed only for single-line inputs, so a textarea keeps its newlines and the
            // submit BUTTON still works normally. That is the standard exception: blocking Enter
            // outright would break keyboard-only submission, which is an accessibility
            // regression traded for a papercut.
            onKeyDown={(event) => {
              const target = event.target as HTMLElement;
              if (
                event.key === "Enter" &&
                target.tagName === "INPUT" &&
                (target as HTMLInputElement).type !== "submit"
              ) {
                event.preventDefault();
              }
            }}
          >
            <label className="authform__label" htmlFor="new-area-name">
              What do you call this plot?
            </label>
            <input
              id="new-area-name"
              name="area_name"
              className="authform__input"
              placeholder="Riverside field"
              maxLength={120}
              required
            />

            <label className="authform__label" htmlFor="new-area-crop">
              Crop (optional)
            </label>
            <input
              id="new-area-crop"
              name="crop"
              className="authform__input"
              placeholder="maize"
              maxLength={60}
            />

            {/* The same picker as signup, so "where is it" works identically in both places —
                GPS, place search, or drawing the outline. The resolved area travels as JSON so
                the server never re-derives a bbox, which is what keeps a bbox and its ring from
                disagreeing. */}
            <AreaPicker onResolved={setResolved} />
            <input
              type="hidden"
              name="resolved_area"
              value={resolved ? JSON.stringify(resolved.area) : ""}
            />

            {/* Says WHY the button is disabled, rather than leaving a dead control. */}
            {!resolved && (
              <p id="submit-blocked" className="authform__hint" role="status">
                Choose where this plot is — use your current location, search for the place, or
                drop a pin on the map — and the button below will enable.
              </p>
            )}

            <div className="wsform__row">
              <Submitting
                blocked={!resolved}
                blockedReason="Choose where this plot is first."
              >
                Start monitoring this plot
              </Submitting>
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  setAdding(false);
                  setResolved(null);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {/* The summary replaces the plain notice on success, so the two do not repeat each
            other. Errors still use the notice, which is the right shape for one sentence. */}
        {addState.ok ? (
          <ActivationSummary
            state={addState}
            onAddAnother={() => setAdding(true)}
            formOpen={adding}
          />
        ) : (
          <Notice state={addState} />
        )}
      </section>

      <IntelligenceLegend />
    </>
  );
}
