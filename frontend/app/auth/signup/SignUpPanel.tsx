"use client";

import { useState } from "react";

import { signUpCommercial, signUpIndividual } from "@/app/auth/actions";
import { AuthForm, Field } from "@/components/AuthForm";
import PasswordField from "@/components/PasswordField";
import PhoneField from "@/components/PhoneField";

/**
 * Sign-up for both account kinds.
 *
 * ## Why one page with a switch rather than two routes
 *
 * Most people arriving do not yet know which they are — a cooperative officer may think
 * "individual" until they see that the commercial account manages many farmers. Two
 * separate routes would make that a navigation problem; a switch makes the distinction
 * visible at the moment of choosing, with one line explaining each.
 *
 * ## Why the individual form is this short
 *
 * Name, email, password, language. Every additional required field measurably reduces
 * completion, and the plot is collected *after* signup — the account is useful without it
 * and asking for a map interaction before an account exists loses people who are not sure
 * yet.
 */

type Kind = "individual" | "commercial";

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "ha", label: "Hausa" },
  { code: "yo", label: "Yorùbá" },
  { code: "ig", label: "Igbo" },
  { code: "pcm", label: "Nigerian Pidgin" },
  { code: "fr", label: "Français" },
];

/**
 * Aggregator sectors, grouped by the intelligence track each one buys.
 *
 * The previous list had six entries and every one of them was agricultural or generic,
 * which quietly told a state emergency agency or a malaria programme that the platform
 * was not for them — the narrowest possible read of a product that covers three tracks.
 * Grouping by track makes the breadth structural rather than a claim in a paragraph: a
 * public health ministry finds itself under a heading with its own name on it.
 *
 * `<optgroup>` rather than a flat list because a flat 18-item select is a scroll, and the
 * group headings do the positioning work on their own. Native select on Android renders
 * optgroup labels as non-selectable headers, which is the correct affordance here.
 *
 * ## Environmental and Public Health appear even though those tracks are next phase
 *
 * Deliberate, and the reason matters: this field records *who the organisation is*, not
 * what is switched on for them. A flood agency signing up today is a real flood agency
 * whose account runs on Agricultural Intelligence until the Environmental track ships.
 * Hiding their sector would lose exactly the demand signal that sequences the roadmap.
 * The group labels carry the state so nothing is oversold — see `NEXT_PHASE_NOTE` below,
 * which is rendered under the field.
 *
 * Values are stable slugs, so relabelling for clarity never rewrites stored data. The
 * backend takes `sector` as a free-form string (`max_length=60`), so adding a group needs
 * no migration and no enum change — but keep slugs under that ceiling.
 */
const SECTOR_GROUPS = [
  {
    label: "Agricultural Intelligence — live now",
    options: [
      { value: "cooperative", label: "Farming cooperative or farmer union" },
      { value: "agribusiness", label: "Agribusiness or commodity buyer" },
      { value: "agri_finance", label: "Agricultural lender or credit provider" },
      { value: "insurer", label: "Insurer or index-insurance provider" },
      { value: "input_supplier", label: "Seed, fertiliser or input supplier" },
      { value: "irrigation", label: "Irrigation scheme or water-user association" },
      { value: "extension", label: "Agricultural extension service" },
    ],
  },
  {
    label: "Environmental Intelligence — next phase",
    options: [
      { value: "emergency_agency", label: "Emergency management or civil protection" },
      { value: "flood_authority", label: "Flood, river basin or water authority" },
      { value: "environment_ministry", label: "Environment or climate ministry" },
      { value: "infrastructure", label: "Infrastructure, energy or transport operator" },
      { value: "local_government", label: "State or local government" },
    ],
  },
  {
    label: "Public Health Intelligence — next phase",
    options: [
      { value: "health_ministry", label: "Health ministry or public health agency" },
      { value: "disease_surveillance", label: "Disease surveillance or vector control" },
      { value: "health_facility", label: "Hospital, clinic network or health system" },
      { value: "humanitarian_health", label: "Humanitarian health responder" },
    ],
  },
  {
    label: "Across all three tracks",
    options: [
      { value: "ngo", label: "NGO or development programme" },
      { value: "research", label: "Research institute or university" },
      { value: "development_finance", label: "Development finance or donor programme" },
      { value: "reseller", label: "Technology reseller or systems integrator" },
      { value: "other", label: "Other" },
    ],
  },
];

/** Shown under the select. Names the sequencing so a next-phase sector is not misled. */
const NEXT_PHASE_NOTE =
  "Environmental and Public Health Intelligence run on the same satellite pipeline and " +
  "arrive next phase. Tell us your sector now and your account is activated for them on " +
  "release — Agricultural Intelligence is live today.";

export default function SignUpPanel({ initialKind }: { initialKind: Kind }) {
  const [kind, setKind] = useState<Kind>(initialKind);

  return (
    <>
      <h1 className="authpanel__title">Get started</h1>
      <p className="authpanel__lede">
        Onboard in 60 seconds. No data cost, no app install — we contact you on a
        channel you already use.
      </p>

      <div className="authtabs" role="tablist" aria-label="Account type">
        <button
          type="button"
          role="tab"
          aria-selected={kind === "individual"}
          className="authtabs__tab"
          onClick={() => setKind("individual")}
        >
          For myself
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={kind === "commercial"}
          className="authtabs__tab"
          onClick={() => setKind("commercial")}
        >
          For an organisation
        </button>
      </div>

      <p className="authpanel__kindnote">
        {kind === "individual"
          ? "Monitor your own plot. Alerts arrive by email, WhatsApp or SMS — and by satellite broadcast when the network is down."
          : "Onboard and manage many subscribers, with a REST API and scoped keys. For cooperatives, insurers, agencies and NGOs."}
      </p>

      {kind === "individual" ? (
        <AuthForm
          action={signUpIndividual}
          submitLabel="Create my account"
          pendingLabel="Creating your account…"
        >
          <div className="authform__row">
            <Field name="first_name" label="First name" autoComplete="given-name" />
            <Field name="last_name" label="Last name" autoComplete="family-name" />
          </div>
          <Field
            name="email"
            label="Email address"
            type="email"
            autoComplete="email"
            inputMode="email"
            hint="Where your alerts go by default. You can add more channels later."
          />
          {/* Country picker plus local number. A single free-text field silently
              misrouted every non-Nigerian number to a +234 prefix — see PhoneField. */}
          <PhoneField hint="For WhatsApp or SMS alerts. Pick your country, then enter your number without the leading zero." />

          <div className="authform__field">
            <label htmlFor="f-language" className="authform__label">
              Alert language
            </label>
            <select
              id="f-language"
              name="language"
              className="authform__input"
              defaultValue="en"
            >
              {LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
            <p className="authform__hint">
              Advisories are written in this language, not translated after the fact.
            </p>
          </div>

          {/* Renders both the password and confirm fields, with the backend's two rules
              checked live. Replaces a pair of plain inputs whose only feedback was a raw
              Pydantic error after a round trip. */}
          <PasswordField />
        </AuthForm>
      ) : (
        <AuthForm
          action={signUpCommercial}
          submitLabel="Create organisation account"
          pendingLabel="Creating your account…"
        >
          <Field
            name="organisation"
            label="Organisation name"
            autoComplete="organization"
          />

          <div className="authform__field">
            <label htmlFor="f-sector" className="authform__label">
              Sector
            </label>
            <select
              id="f-sector"
              name="sector"
              className="authform__input"
              defaultValue="cooperative"
              aria-describedby="f-sector-note"
            >
              {SECTOR_GROUPS.map((group) => (
                <optgroup key={group.label} label={group.label}>
                  {group.options.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <p id="f-sector-note" className="authform__hint">
              {NEXT_PHASE_NOTE}
            </p>
          </div>

          <div className="authform__row">
            <Field name="first_name" label="Your first name" autoComplete="given-name" />
            <Field name="last_name" label="Your last name" autoComplete="family-name" />
          </div>
          <Field
            name="email"
            label="Work email"
            type="email"
            autoComplete="email"
            inputMode="email"
          />
          <PhoneField
            label="Contact phone"
            hint="Used for account security notices, not for alert delivery."
          />
          <PasswordField hint="Three unrelated words is strong and easy. You can enable two-factor authentication after signing in." />

          <p className="authform__hint" style={{ marginTop: 4 }}>
            API keys are created after sign-in, from the portal — never at signup. That
            way an account that never integrates is not left holding a live credential.
          </p>
        </AuthForm>
      )}

      <p className="authpanel__alt">
        Already have an account? <a href="/auth/login">Sign in</a>
      </p>
    </>
  );
}
