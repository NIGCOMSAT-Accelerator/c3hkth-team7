/**
 * The "nothing here yet" panel, with the action that fixes it.
 *
 * ## Why every list page needs one
 *
 * A page whose create form is always open is busy before you have done anything, and a page that
 * renders an empty table is worse — it reads as broken rather than as new. The Webhooks page had
 * the right shape and every other portal page had neither, so this lifts that shape out to be
 * used by all of them.
 *
 * ## Learn-more before create, deliberately
 *
 * Someone reading an empty state usually does not yet know what the thing *is*. Sending them
 * straight to a form produces a half-configured object: a webhook endpoint that rejects every
 * delivery because they had not read the signature docs, an API key with no scopes. So where a
 * reference exists it comes first, and the primary action sits to its right.
 *
 * Where no reference exists, `learnMore` is omitted rather than pointed at something vaguely
 * related — a "Learn more" that teaches nothing trains people to ignore the pair.
 *
 * ## A server-rendered variant is not possible here
 *
 * `onAction` opens a modal, which is client state, so this is used inside a client component. The
 * icon is passed in rather than chosen from a map, so a page can use its own glyph without this
 * file growing a registry of every module's iconography.
 */
export default function EmptyState({
  icon,
  title,
  body,
  actionLabel,
  onAction,
  learnMore,
  learnMoreLabel = "Learn more",
}: {
  /** Inline SVG at 40px. Decorative — `aria-hidden` is applied here, not by the caller. */
  icon: React.ReactNode;
  title: string;
  body: React.ReactNode;
  actionLabel: string;
  onAction: () => void;
  /** Optional reference URL. Opens in a new tab — it is consulted *while* filling the form. */
  learnMore?: string;
  learnMoreLabel?: string;
}) {
  return (
    <section className="emptystate">
      <div className="emptystate__art" aria-hidden="true">
        {icon}
      </div>

      <h2 className="emptystate__title">{title}</h2>
      <p className="emptystate__body">{body}</p>

      <div className="emptystate__actions">
        {learnMore && (
          <a
            href={learnMore}
            target="_blank"
            rel="noopener noreferrer"
            className="btn btn--ghost"
          >
            {learnMoreLabel}
          </a>
        )}
        <button type="button" className="btn btn--primary" onClick={onAction}>
          {actionLabel}
        </button>
      </div>
    </section>
  );
}
