import { SEVERITY_META, type Severity } from "@/lib/types";

/**
 * Severity is a reserved status color. It always ships with an icon AND a text
 * label so it is never conveyed by color alone — required for colorblind
 * readers, forced-colors mode, and print.
 */
export default function SeverityBadge({ severity }: { severity: Severity }) {
  const meta = SEVERITY_META[severity];
  return (
    <span className={`sev sev--${severity}`}>
      <span className="sev__icon" aria-hidden="true">
        {meta.icon}
      </span>
      {meta.label}
    </span>
  );
}
