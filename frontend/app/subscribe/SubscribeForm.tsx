"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import AreaPicker from "@/components/AreaPicker/AreaPicker";
import { CHANNEL_LABEL, type Channel } from "@/lib/types";
import type { ResolvedArea } from "./area-actions";

import { subscribe, type SubscribeState } from "./actions";

const INITIAL: SubscribeState = { ok: false, message: "" };

/** Placeholder text per channel — the address format differs for each. */
const CHANNEL_HINT: Record<Channel, string> = {
  whatsapp: "+234…  (international format)",
  telegram: "Numeric chat ID from @userinfobot",
  signal: "+234…  (international format)",
  email: "you@example.org",
  slack: "#channel or a channel ID",
  webhook: "https://your-system.example/hooks/shelter",
  nigcomsat_broadcast: "Terminal group ID, or ALL for the whole beam",
};

const CHANNELS = Object.keys(CHANNEL_HINT) as Channel[];

export default function SubscribeForm() {
  const [state, formAction] = useActionState(subscribe, INITIAL);
  // The picker's output. Null until an area is settled — which gates submit, because a
  // subscription with no area cannot be monitored.
  const [area, setArea] = useState<ResolvedArea | null>(null);

  if (state.ok) {
    return (
      <div className="card" role="status">
        <h2 className="card__title" style={{ color: "var(--viz-rain)" }}>
          You&rsquo;re registered
        </h2>
        <p style={{ margin: "8px 0 16px", color: "var(--text-secondary)" }}>
          {state.message}
        </p>
        {state.subscriberId && (
          <p className="mono muted" style={{ margin: "0 0 18px" }}>
            Subscriber ID: {state.subscriberId}
          </p>
        )}
        <a className="btn btn--primary" href="/dashboard">
          Open the dashboard
        </a>
      </div>
    );
  }

  return (
    <form action={formAction} className="card">
      {state.message && (
        <div
          role="alert"
          className="notice"
          style={{
            marginBottom: 20,
            borderColor: "var(--sev-warning)",
            color: "var(--sev-warning)",
          }}
        >
          {state.message}
        </div>
      )}

      <h2 className="card__title">About you</h2>
      <p className="card__sub">
        Your role decides how the advisory is written — a farmer and a district
        health officer need different instructions from the same hazard.
      </p>

      <label className="field">
        <span className="field__label">Name</span>
        <input className="input" name="name" required maxLength={120} />
      </label>

      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr" }}>
        <label className="field">
          <span className="field__label">I am a…</span>
          <select className="select" name="kind" defaultValue="farmer">
            <option value="farmer">Farmer</option>
            <option value="cooperative">Cooperative</option>
            <option value="government">Government agency</option>
            <option value="emergency_responder">Emergency responder</option>
            <option value="public_health">Public health</option>
            <option value="insurer">Insurer</option>
          </select>
        </label>

        <label className="field">
          <span className="field__label">Language</span>
          <select className="select" name="language" defaultValue="en">
            <option value="en">English</option>
            <option value="ha">Hausa</option>
            <option value="yo">Yoruba</option>
            <option value="ig">Igbo</option>
            <option value="fr">French</option>
            <option value="pcm">Nigerian Pidgin</option>
          </select>
          <span className="field__hint">
            Advisories are written in this language when generation is enabled.
          </span>
        </label>
      </div>

      <hr style={{ border: 0, borderTop: "1px solid var(--hairline)", margin: "8px 0 24px" }} />

      <h2 className="card__title">Area to watch</h2>
      <p className="card__sub">
        Show us where. Use your phone&rsquo;s location, search for your village, or draw the
        outline of your field &mdash; whichever is easiest.
      </p>

      <label className="field">
        <span className="field__label">What should we call it?</span>
        <input
          className="input"
          name="area_name"
          required
          placeholder="e.g. Rice plots by the river"
        />
      </label>

      {/*
        The picker replaces three numeric inputs — latitude, longitude and radius — that a
        farmer had no way to fill in. Everything is resolved server-side, so this form never
        computes a bounding box.

        The result is written into a hidden field rather than held in state alone, so a
        submit cannot outrun a re-render.
      */}
      <AreaPicker onResolved={setArea} />
      <input
        type="hidden"
        name="resolved_area"
        value={area ? JSON.stringify(area.area) : ""}
      />

      <label className="field">
        <span className="field__label">Main crop (optional)</span>
        <input className="input" name="crop" placeholder="rice, maize, sorghum…" />
        <span className="field__hint">
          Only changes the wording of an advisory &mdash; never a measurement.
        </span>
      </label>

      <hr style={{ border: 0, borderTop: "1px solid var(--hairline)", margin: "8px 0 24px" }} />

      <h2 className="card__title">How to reach you</h2>
      <p className="card__sub">
        Fill in any you want. Leave the rest blank. Broadcast is the one that
        still works when the network doesn&rsquo;t.
      </p>

      {CHANNELS.map((channel) => (
        <div
          key={channel}
          style={{
            display: "grid",
            gap: 10,
            gridTemplateColumns: "1.6fr 1fr",
            alignItems: "end",
            marginBottom: 12,
          }}
        >
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field__label">{CHANNEL_LABEL[channel]}</span>
            <input
              className="input"
              name={`addr_${channel}`}
              placeholder={CHANNEL_HINT[channel]}
              autoComplete="off"
            />
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field__label">Send from</span>
            <select
              className="select"
              name={`min_${channel}`}
              defaultValue={channel === "nigcomsat_broadcast" ? "warning" : "advisory"}
            >
              <option value="advisory">Advisory and up</option>
              <option value="watch">Watch and up</option>
              <option value="warning">Warning and up</option>
              <option value="emergency">Emergency only</option>
            </select>
          </label>
        </div>
      ))}

      <Submit blocked={!area} />
    </form>
  );
}

function Submit({ blocked }: { blocked?: boolean }) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      className="btn btn--primary"
      // Disabled until the area is chosen AND confirmed on the picker's card.
      //
      // The server action already refuses an empty `resolved_area` with a readable message, so
      // this is not the safety mechanism — it is the difference between "the button did nothing
      // useful" and "the button told me what is missing before I pressed it".
      disabled={pending || blocked}
      title={blocked ? "Choose and confirm the area you want monitored first." : undefined}
      style={{ marginTop: 12, width: "100%" }}
    >
      {pending ? "Registering and queueing first scan…" : "Activate my alerts"}
    </button>
  );
}
