"use client";

import { useEffect, useState } from "react";

type Mode = "light" | "dark" | "system";

/**
 * Stamps `data-theme` on the root element. The CSS declares dark values under
 * both the media query and the `data-theme` scope so this beats the OS setting
 * in both directions.
 */
export default function ThemeToggle() {
  const [mode, setMode] = useState<Mode>("system");

  useEffect(() => {
    const stored = window.localStorage.getItem("shelter-theme") as Mode | null;
    if (stored) applyMode(stored, setMode);
  }, []);

  const next: Record<Mode, Mode> = {
    system: "light",
    light: "dark",
    dark: "system",
  };

  const icon = mode === "light" ? "☀" : mode === "dark" ? "☾" : "◐";

  return (
    <button
      type="button"
      onClick={() => applyMode(next[mode], setMode)}
      aria-label={`Theme: ${mode}. Click to switch.`}
      title={`Theme: ${mode}`}
      style={{
        background: "none",
        border: "1px solid var(--hairline-strong)",
        borderRadius: 8,
        width: 34,
        height: 34,
        cursor: "pointer",
        color: "var(--text-secondary)",
        fontSize: 14,
        lineHeight: 1,
      }}
    >
      <span aria-hidden="true">{icon}</span>
    </button>
  );
}

function applyMode(mode: Mode, setMode: (m: Mode) => void) {
  const root = document.documentElement;
  if (mode === "system") {
    root.removeAttribute("data-theme");
    window.localStorage.removeItem("shelter-theme");
  } else {
    root.setAttribute("data-theme", mode);
    window.localStorage.setItem("shelter-theme", mode);
  }
  setMode(mode);
}
