import { VERDICT_META } from "@/lib/intelligence";
import type { VerdictSummary } from "@/lib/types";

/**
 * A Fahis verdict with its citations, beside the alert it judges.
 *
 * ## Why the sources are listed and not counted
 *
 * "Independently confirmed by Fahis-AI · 2 sources cited" asks to be believed. The claim is about
 * the outside world, and the only thing that turns it from an assertion into evidence is the
 * reader's ability to go and look — so every source is listed with a link they can open, who
 * published it, how much weight it carries, and when.
 *
 * A count cannot be checked. That matters more here than anywhere else in the product: every other
 * number a subscriber sees traces to a satellite measurement we can defend, whereas a verdict rests
 * on outside reporting. Showing the citations is what makes the accountability agent accountable in
 * turn, rather than a second opinion delivered with the same authority as the first.
 *
 * ## What is deliberately NOT shown
 *
 * The **snippet** — the raw web prose the model read. It is provenance for an operator, and putting
 * unattributed outside text beside a measured advisory is the adjacency the grounding rule exists to
 * prevent. The link is strictly better for the reader anyway: our excerpt is one sentence we chose.
 *
 * ## Why the date is prominent
 *
 * A 2019 drought article cannot corroborate a 2026 flood warning. Fahis reasons about recency
 * itself and downgrades a verdict whose sources all predate the window, but showing the date lets a
 * reader reach that conclusion independently — which is the point of citing anything at all.
 */

/** Weight of a source, worded for a reader rather than for the search index. */
const TIER_LABEL: Record<string, string> = {
  official: "Government or agency",
  media: "News media",
  low: "Unverified source",
  other: "Other source",
};

/**
 * Tier drives a text label, never colour alone — same rule as `SeverityBadge`. A reader who cannot
 * distinguish the hues still needs to know an agency bulletin outweighs a blog.
 */
function SourceTier({ tier }: { tier: string }) {
  return (
    <span className="vsource__tier" data-tier={tier}>
      {TIER_LABEL[tier] ?? TIER_LABEL.other}
    </span>
  );
}

/** Hostname alone. The full URL is unreadable on a phone and the domain is the recognisable part —
 *  "nema.gov.ng" tells a subscriber more at a glance than a 90-character path does. */
function displayHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    // A malformed URL is still shown rather than dropped: a citation we cannot parse is a citation
    // the reader should still see, and silently omitting it would understate the evidence.
    return url;
  }
}

export default function VerdictPanel({ verdict }: { verdict: VerdictSummary }) {
  const meta = VERDICT_META[verdict.verdict];
  const sources = verdict.sources ?? [];

  return (
    <div className="verdict" data-tone={meta.tone}>
      <p className="verdict__head">
        <span aria-hidden="true">{meta.icon}</span> {meta.label}
        {verdict.verified_at && (
          <span className="verdict__when">
            checked {new Date(verdict.verified_at).toLocaleDateString()}
          </span>
        )}
      </p>

      <p className="verdict__note">{meta.note}</p>

      {verdict.rationale && <p className="verdict__rationale">{verdict.rationale}</p>}

      {sources.length > 0 ? (
        <details className="verdict__evidence">
          <summary>
            {sources.length} independent {sources.length === 1 ? "source" : "sources"} — check
            them yourself
          </summary>

          <ul className="vsources">
            {sources.map((source, i) => (
              <li className="vsource" key={`${source.url}-${i}`}>
                <a
                  href={source.url}
                  // `noopener noreferrer` because these are third-party links we did not vet, and
                  // `_blank` so opening one does not lose the alert they are checking it against.
                  rel="noopener noreferrer nofollow"
                  target="_blank"
                  className="vsource__title"
                >
                  {source.title || displayHost(source.url)}
                </a>
                <span className="vsource__meta">
                  <SourceTier tier={source.tier} />
                  <span className="vsource__host">{displayHost(source.url)}</span>
                  {/* Absence is stated rather than left blank. A source with no date is weaker
                      evidence for a time-bounded claim, and a reader should be able to see that
                      rather than assume we simply did not display it. */}
                  <span className="vsource__date">
                    {source.published ? (
                      new Date(source.published).toLocaleDateString()
                    ) : (
                      <span className="muted">no date given</span>
                    )}
                  </span>
                </span>
              </li>
            ))}
          </ul>

          <p className="verdict__foot">
            Found by searching outside reporting, independently of our satellite data. SHELTER
            excludes its own site from these searches, so a verdict can never confirm itself.
          </p>
        </details>
      ) : (
        verdict.source_count > 0 && (
          // A count with no list: an older backend. Better than showing nothing, and it does not
          // pretend to be checkable.
          <p className="verdict__sources">
            {verdict.source_count} {verdict.source_count === 1 ? "source" : "sources"} cited ·
            checked independently of the satellite data
          </p>
        )
      )}
    </div>
  );
}
