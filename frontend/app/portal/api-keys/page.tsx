import { DEV_DOCS_URL } from "@/lib/links";
import { safePortal } from "@/lib/portal";

import KeyMinter from "./KeyMinter";
import RevokeKey from "./RevokeKey";
import { getAccount } from "@/lib/session";
import { redirect } from "next/navigation";

export const metadata = { title: "API keys" };
export const dynamic = "force-dynamic";

/**
 * An aggregator's API keys.
 *
 * ## Individuals are redirected, not shown a 403
 *
 * `POST /iam/api-keys` checks `account.can_use_api` and refuses individuals — deliberate
 * product design as much as security: a farmer has nothing to integrate, and a credential
 * they cannot use is one that can only be phished out of them. So this page sends them back
 * rather than showing an error for a feature that will never apply to them.
 *
 * ## Minting lives here now
 *
 * This page was read-only, on the reasoning that a show-once secret needs careful handling and
 * listing carries no such risk. True as far as it went, and it left the Partner API effectively
 * unreachable: the key is the only way to authenticate against it, `POST /iam/api-keys` was the only
 * way to get one, and no portal surface called it. An aggregator wanting to integrate had to
 * hand-craft an HTTP request carrying a session token they had no way to read.
 *
 * So `KeyMinter` owns creation and revocation. The show-once problem is solved rather than avoided:
 * the plaintext renders in a copyable block with the warning attached, and it is never persisted —
 * not in a cookie, not in the audit detail, not in the cache.
 */
export default async function ApiKeysPage() {
  const account = await getAccount();

  if (account && account.kind !== "commercial") {
    redirect("/portal");
  }

  // Both in parallel: the form needs the workspace list, the revoke section needs the keys.
  const [keys, workspaces] = await Promise.all([
    safePortal.apiKeys(),
    safePortal.workspaces(),
  ]);

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">API keys</h1>
        <p className="portal__lede">
          Credentials for your integration. Each key carries only the scopes you grant it.
        </p>
      </header>

      {/* Minting first: an aggregator arriving here with no key needs the form, not an empty list.
          The existing-keys table below is the reference once they have one. */}
      <KeyMinter
        workspaces={workspaces ?? []}
        keys={keys ?? []}
        docsUrl={DEV_DOCS_URL}
      />

      {keys !== null && keys.length > 0 && (
      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Active keys</h2>
        </div>

        {keys === null ? (
          <p className="muted" style={{ margin: 0, fontSize: 14 }}>
            Temporarily unavailable. This is a read failure &mdash; your keys still work.
          </p>
        ) : (
          <ul className="keylist">
            {keys.map((k) => (
              <li key={k.id} className="keylist__row">
                <div className="keylist__main">
                  <strong>{k.name}</strong>
                  {/* Prefix only. The secret was shown once at creation and is stored as a
                      hash — there is no way to display it again, which is the point. */}
                  <span className="mono keylist__prefix">{k.prefix}…</span>
                  {k.revoked && <span className="chip chip--bad">Revoked</span>}
                </div>
                <div className="pcard__chips">
                  {k.scopes.map((s) => (
                    <span key={s} className="chip chip--quiet">
                      {s}
                    </span>
                  ))}
                </div>
                <div className="auditrow__meta">
                  <span>Created {k.created_at.slice(0, 10)}</span>
                  <span>
                    {k.last_used_at
                      ? `Last used ${k.last_used_at.slice(0, 10)}`
                      : "Never used"}
                  </span>
                  <span>
                    {k.expires_at ? `Expires ${k.expires_at.slice(0, 10)}` : "No expiry"}
                  </span>
                </div>
                {/* Beside the key, not in a separate card: revocation is irreversible and the
                    decision depends on the scopes and last-used date right above it. */}
                {!k.revoked && <RevokeKey keyId={k.id} />}
              </li>
            ))}
          </ul>
        )}
      </section>
      )}

      <section className="pcard">
        <h2 className="pcard__title">How keys work</h2>
        <ul className="evidence">
          <li>
            The secret is shown <strong>once</strong>, at creation. It is stored as a hash,
            so it cannot be shown again — losing it means rotating, not recovering.
          </li>
          <li>
            Scopes cannot be added to an existing key. Widening one in place would silently
            grant new powers to every system already holding it, so a wider key is a new key.
          </li>
          <li>
            Rotation keeps the old key working for a short grace period, so you can deploy
            the replacement and verify it before the previous one stops.
          </li>
          <li>
            A key is <strong>scoped to one workspace</strong>, so it reaches only the
            intelligence tracks that workspace has activated. The scopes you may put on it come
            from your role <em>on that workspace</em> — a role you hold on another project does
            not widen it.
          </li>
          <li>
            Set an expiry, or choose never to expire. An expiring key is safer; a
            never-expiring one is honest about what most integrations actually do.
          </li>
        </ul>
        <p className="authform__hint">
          Creating and rotating keys from the portal is the next piece of work — it needs a
          show-once screen built carefully. For now use{" "}
          <span className="mono">POST /iam/api-keys</span> with your portal session, passing{" "}
          <span className="mono">workspace_id</span> for the project the key is for (omit it and
          it belongs to your default workspace). See the{" "}
          <a href="/shelter/v1/api/dev-docs">developer reference</a>.
        </p>
      </section>
    </>
  );
}
