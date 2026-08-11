import { safePortal } from "@/lib/portal";
import type { TrustedDevice } from "@/lib/types";
import { getAccount } from "@/lib/session";

export const metadata = { title: "Security" };
export const dynamic = "force-dynamic";

/**
 * Two-factor state, session policy, and what protects this account.
 *
 * Read-only for now: enrolling TOTP needs a QR code and a confirm step
 * (`POST /iam/auth/totp/enrol` → `/confirm`), which is a flow rather than a toggle. What
 * this page does today is tell the truth about the current state — including the recovery
 * code count, which nothing else surfaces and which is the number that decides whether
 * someone is one lost phone away from being locked out.
 */
/** A timestamp a person can act on. "3 days ago" answers "is that recent?" without arithmetic. */
function relative(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";

  const mins = Math.round((Date.now() - then) / 60_000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins} minutes ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.round(hours / 24);
  if (days < 31) return `${days} ${days === 1 ? "day" : "days"} ago`;
  // Past a month the exact date is more useful than a large relative number.
  return new Date(iso).toLocaleDateString();
}

export default async function SecurityPage() {
  const [account, totp, devices] = await Promise.all([
    getAccount(),
    safePortal.totpState(),
    safePortal.trustedDevices(),
  ]);

  const remaining = totp?.recovery_codes_remaining ?? 0;

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Security</h1>
        <p className="portal__lede">
          What protects this account, and how your session behaves.
        </p>
      </header>

      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Two-factor authentication</h2>
          <span className={`statuspill${totp?.enabled ? " statuspill--on" : ""}`}>
            {totp?.enabled ? "Enabled" : "Not enabled"}
          </span>
        </div>

        {totp?.enabled ? (
          <>
            <p className="pcard__sub">
              A code from your authenticator app is required at every sign-in.
            </p>
            {/* The recovery-code count is the actionable part. Down to one, the user is a
                lost phone away from lockout and nothing else would tell them. */}
            <p
              className={
                remaining <= 1 ? "pcard__warn" : "muted"
              }
              style={{ fontSize: 13.5 }}
            >
              {remaining} recovery {remaining === 1 ? "code" : "codes"} remaining
              {remaining <= 1 &&
                " — regenerate now. With none left, a lost device means contacting support."}
            </p>
          </>
        ) : (
          <p className="pcard__sub">
            Your account is protected by a password and by email sign-in links. Adding an
            authenticator app means a stolen password alone is not enough to sign in.
          </p>
        )}

        {/* Honest about what is not built rather than showing a button that does nothing.
            A dead control is worse than an absent one. */}
        <p className="authform__hint">
          {totp?.enabled
            ? "To regenerate recovery codes or remove two-factor authentication, contact hi@freepass.africa. Self-service management is coming next."
            : "Self-service enrolment is coming next. To enable it now, contact hi@freepass.africa."}
        </p>
      </section>

      <section className="pcard">
        <h2 className="pcard__title">Session</h2>
        <dl className="deflist">
          <div>
            <dt>Idle timeout</dt>
            <dd>
              15 minutes of no activity
              <span className="muted"> — you are warned 2 minutes before</span>
            </dd>
          </div>
          <div>
            <dt>Maximum session</dt>
            <dd>12 hours, regardless of activity</dd>
          </div>
          <div>
            <dt>Session storage</dt>
            {/* Stated because it is a real protection a security-minded user will look
                for, and because it explains why there is no "remember me". */}
            <dd>
              A cookie that JavaScript cannot read
              <span className="muted"> — so a script injected into the page cannot steal it</span>
            </dd>
          </div>
          <div>
            <dt>Email address</dt>
            <dd>
              {account?.email_verified ? (
                <span className="statuspill statuspill--on">Confirmed</span>
              ) : (
                <a href="/auth/pending">Not confirmed — finish now</a>
              )}
            </dd>
          </div>
        </dl>

        <p className="authform__hint">
          Idle timeout is enforced by the server, not just the browser — closing the tab
          without signing out still ends the session within 15 minutes.
        </p>

        {/*
          Trusted devices.

          ## Why this belongs in the Session section

          "How does my session behave" and "where has my session been" are the same question asked
          two ways, and the second is the one that catches a compromise. The policy facts above are
          static; this is the only part of the page that could tell someone something is wrong.

          ## Why every row carries the email

          A device does not identify a person. A shared handset in a household, or an office
          machine in a cooperative, produces rows that are indistinguishable by user agent and
          location alone — so the account each sign-in belongs to is stated outright rather than
          inferred from the fact that this is "your" page.
        */}
        <div className="devices">
          <h3 className="devices__title">Your devices</h3>
          <p className="devices__notice">
            {devices?.notice ??
              "Your trusted devices are listed below. They will remain trusted devices unless there is a period of inactivity on your SHELTER account."}
          </p>

          {devices === null ? (
            <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
              Temporarily unavailable.
            </p>
          ) : devices.devices.length === 0 ? (
            <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
              No sign-ins recorded yet. This device will appear here shortly after you next
              sign in.
            </p>
          ) : (
            <div className="devices__scroll">
              <table className="devices__table">
                <thead>
                  <tr>
                    <th scope="col">Device</th>
                    <th scope="col">User</th>
                    <th scope="col">IP address</th>
                    <th scope="col">Location</th>
                    <th scope="col">Last login</th>
                  </tr>
                </thead>
                <tbody>
                  {devices.devices.map((d: TrustedDevice, i: number) => (
                    <tr
                      key={`${d.user_agent ?? "ua"}-${d.ip ?? "ip"}-${i}`}
                      data-current={d.is_current ? "true" : undefined}
                    >
                      <td>
                        <span className="devices__name">
                          {/* Active/inactive as a glass pill, matching the Areas page. Text
                              carries the meaning; the glow is reinforcement only. */}
                          <span
                            className={`glasspill ${
                              d.is_current ? "glasspill--on" : "glasspill--off"
                            }`}
                          >
                            <span className="glasspill__dot" aria-hidden="true" />
                            {d.is_current ? "This device" : "Trusted"}
                          </span>
                          <strong>{d.browser}</strong>
                        </span>
                        <span className="devices__sub">
                          {d.device}
                          {d.sign_ins > 1 ? ` · ${d.sign_ins} sign-ins` : ""}
                        </span>
                      </td>
                      <td className="devices__email">{d.email}</td>
                      <td className="mono">{d.ip ?? "—"}</td>
                      <td>{d.location ?? "—"}</td>
                      <td>
                        {relative(d.last_login)}
                        {d.first_seen && d.sign_ins > 1 && (
                          <span className="devices__sub">
                            first seen {relative(d.first_seen)}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="authform__hint">
            A device you do not recognise means someone else has signed in with your password.
            Change it in <a href="/portal/settings">Settings</a> — that ends every other session
            — then check your <a href="/portal/activity">activity log</a>. Read the note on
            location accuracy below before worrying about an unfamiliar city.
          </p>
        </div>
      </section>

      <section className="pcard">
        <h2 className="pcard__title">Something look wrong?</h2>
        <p className="pcard__sub">
          Every sign-in, preference change and organisation access is recorded with a
          timestamp and IP address.
        </p>
        <a href="/portal/activity" className="btn btn--ghost">
          Review the activity log
        </a>

        {/*
          Attribution, and a caveat that belongs beside it.

          DB-IP City Lite is CC-BY 4.0, which REQUIRES visible credit — so this is a licence
          obligation, not a courtesy. Stating the accuracy limit in the same breath is the
          honest framing: a subscriber who sees an unfamiliar city should suspect the
          database before they suspect a break-in, because in this region the lookup often
          resolves to a carrier's gateway rather than the person's town.
        */}
        <p className="authform__hint">
          Locations are approximate, derived from the IP address by an offline database —
          your address is never sent to a third party. In Africa a lookup often resolves to
          your mobile carrier&rsquo;s gateway rather than your town, so an unfamiliar city is
          usually the database, not a stranger. IP geolocation by{" "}
          <a href="https://db-ip.com" rel="noopener noreferrer" target="_blank">
            DB-IP
          </a>
          .
        </p>
      </section>
    </>
  );
}
