"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { updatePreferences, type PrefState } from "./actions";

const INITIAL: PrefState = { ok: false, message: "" };

/**
 * Language and channel.
 *
 * ## The language list carries an honest caveat
 *
 * `advisory/generator.py:_template` is **English-only by explicit design** — a
 * machine-translated safety instruction is worse than an English one the reader can seek
 * help with. So when generation is unavailable, a Hausa-speaking subscriber receives
 * English. Saying so here is the difference between a setting and a promise.
 */
const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "ha", label: "Hausa" },
  { code: "yo", label: "Yorùbá" },
  { code: "ig", label: "Igbo" },
  { code: "pcm", label: "Nigerian Pidgin" },
  { code: "fr", label: "Français" },
];

/**
 * Only channels that work without a separate verification step are offered.
 *
 * Email is always safe — it is the address the account was confirmed with. WhatsApp and
 * SMS need a verified phone number, and offering them before that check would let someone
 * point another person's number at their own alerts.
 */
/**
 * Only channels the backend's `Channel` enum actually accepts.
 *
 * **`sms` was listed here and is not a real channel.** Selecting it produced a 422 from
 * `PATCH /iam/me/preferences` — an option that cannot be chosen is worse than a missing
 * one, because the user blames themselves. The seven real members are `whatsapp`,
 * `telegram`, `signal`, `email`, `slack`, `webhook`, `nigcomsat_broadcast`.
 *
 * Of those, only three make sense as a personal default: `slack` and `webhook` are
 * integration targets an aggregator configures, and `nigcomsat_broadcast` is not chosen —
 * the Herald escalates to it automatically when terrestrial channels fail or at
 * `NIGCOMSAT_ALWAYS_BROADCAST_AT` severity.
 */
const CHANNELS = [
  { value: "email", label: "Email", note: "Always available" },
  { value: "whatsapp", label: "WhatsApp", note: "Needs a confirmed phone number" },
  { value: "telegram", label: "Telegram", note: "Needs your Telegram chat ID" },
];

function Save() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Saving…" : "Save preferences"}
    </button>
  );
}

export default function PreferencesForm({
  language,
  preferredChannel,
}: {
  language: string;
  preferredChannel: string;
}) {
  const [state, action] = useActionState(updatePreferences, INITIAL);

  return (
    <section className="pcard">
      <h2 className="pcard__title">Delivery preferences</h2>

      <form action={action} className="prefform">
        {state.message && (
          <div
            className="authform__message"
            data-tone={state.ok ? "ok" : "error"}
            role={state.ok ? "status" : "alert"}
          >
            {state.message}
          </div>
        )}

        <div className="authform__field">
          <label htmlFor="p-language" className="authform__label">
            Advisory language
          </label>
          <select
            id="p-language"
            name="language"
            className="authform__input"
            defaultValue={language}
          >
            {LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>
                {l.label}
              </option>
            ))}
          </select>
          <p className="authform__hint">
            Advisories are written in this language. If translation is briefly unavailable
            you will receive English rather than an unreviewed translation — a mistranslated
            safety instruction is more dangerous than one you can seek help reading.
          </p>
        </div>

        <div className="authform__field">
          <label htmlFor="p-channel" className="authform__label">
            Preferred channel
          </label>
          <select
            id="p-channel"
            name="preferred_channel"
            className="authform__input"
            defaultValue={preferredChannel}
          >
            {CHANNELS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label} — {c.note}
              </option>
            ))}
          </select>
          <p className="authform__hint">
            At <strong>emergency</strong> severity, or when every other channel fails, you
            are also reached by NIGCOMSAT-1R satellite broadcast — which needs no mobile
            network at all.
          </p>
        </div>

        <Save />
      </form>
    </section>
  );
}
