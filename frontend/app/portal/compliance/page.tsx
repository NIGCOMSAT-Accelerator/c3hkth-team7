import { requirePermission } from "@/lib/session";

export const metadata = { title: "Compliance" };
export const dynamic = "force-dynamic";

/**
 * Compliance — verification review and blacklist operations.
 *
 * Gated on `compliance:manage`, which only the Compliance role and an Owner hold.
 *
 * The data behind this is Fahis, the accountability agent: it searches independent reporting
 * days after a warning and records whether it was right. `GET /verification/metrics` already
 * serves the aggregate figures.
 */
export default async function CompliancePage() {
  await requirePermission("compliance:manage", "/portal/compliance");

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Compliance</h1>
        <p className="portal__lede">
          Review how accurate past warnings turned out to be, and manage the sources
          verification is allowed to trust.
        </p>
      </header>

      <section className="pcard">
        <h2 className="pcard__title">How verification works</h2>
        <ul className="evidence">
          <li>
            Fahis runs <strong>days after</strong> a warning and searches outside reporting for
            the hazard we predicted, in that place, at that time.
          </li>
          <li>
            Five verdicts, not three: <span className="mono">confirmed</span>,{" "}
            <span className="mono">partial</span>, <span className="mono">refuted</span>,{" "}
            <span className="mono">unverified</span>,{" "}
            <span className="mono">not_attempted</span>. A flood in a remote LGA may never be
            reported by anything indexable, so <span className="mono">unverified</span> is the
            default — reading that silence as a false alarm would misrecord correct warnings.
          </li>
          <li>
            Precision is computed over <strong>confirmed and refuted only</strong>, with
            coverage reported beside it. Including unverified would measure news coverage
            rather than model accuracy.
          </li>
          <li>
            Verification <strong>can never change an advisory</strong>. Fahis writes to its own
            records and nothing else — enforced structurally, so unattributed web prose stays
            one hop away from a number a farmer acts on.
          </li>
        </ul>
        <p className="authform__hint">
          Blacklist management and per-assessment review screens are the next piece of work.
          Aggregate figures are available now at{" "}
          <span className="mono">GET /verification/metrics</span>.
        </p>
      </section>
    </>
  );
}
