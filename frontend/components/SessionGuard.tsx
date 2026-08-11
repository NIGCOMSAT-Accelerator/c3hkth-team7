"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  endSession,
  extendSession,
  reportActivity,
  readSessionState,
} from "@/app/auth/session-actions";

/**
 * Idle-session management for the signed-in portal.
 *
 * ## The requirement this is built around
 *
 * **An active user must never be logged out.** Everything below follows from that, and it
 * is why the design is more careful than a `setTimeout`.
 *
 * ## Why activity is reported explicitly rather than inferred from traffic
 *
 * The naive approach refreshes the session on every API call. That cannot work here: the
 * dashboard already polls `/api/status` for the service banner, so request traffic
 * continues whether or not a human is present — the timeout would never fire on the one
 * page it matters most.
 *
 * So the browser reports *real input* — pointer, key, scroll, touch, tab focus — and only
 * that extends the window. The events are the ones a person cannot produce without being
 * there.
 *
 * ## Why the countdown is server-authoritative
 *
 * A local `setTimeout` is wrong in the exact case an idle timeout exists for: a sleeping
 * laptop. Timers do not run while suspended, so a machine asleep for an hour wakes with a
 * timer that thinks two minutes have passed. The remaining time therefore comes from
 * `GET /iam/session`, which computes it from a server-side timestamp, and the local ticker
 * only interpolates between polls.
 *
 * `document.visibilitychange` triggers an immediate re-read for the same reason — the
 * first thing a returning user should see is the truth, not a stale interpolation.
 *
 * ## Three-tier cadence, to keep the cost honest
 *
 * This runs on a metered connection, so the polling is deliberately cheap:
 *
 *   * **activity ping** — at most one per `ACTIVITY_THROTTLE_MS` (60s), and only if real
 *     input happened since the last one. Idle costs nothing.
 *   * **state poll** — every `POLL_INTERVAL_MS` (60s) while the tab is visible. Skipped
 *     entirely when hidden: a background tab does not need a countdown.
 *   * **warning tick** — 1s, but only while the modal is showing.
 *
 * A typical active session therefore sends ~1 request/minute, and an idle one sends only
 * the poll until the warning appears.
 */

/** Minimum gap between activity pings. One per minute is enough to hold a 15-minute window. */
const ACTIVITY_THROTTLE_MS = 60_000;

/** How often to re-read authoritative state while visible. */
const POLL_INTERVAL_MS = 60_000;

/** Input that counts as a human being present. */
const ACTIVITY_EVENTS = [
  "pointerdown",
  "keydown",
  "scroll",
  "touchstart",
  "focus",
] as const;

function formatCountdown(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

export default function SessionGuard() {
  const [remaining, setRemaining] = useState<number | null>(null);
  const [warningAt, setWarningAt] = useState(120);
  const [showWarning, setShowWarning] = useState(false);
  const [extending, setExtending] = useState(false);

  // Refs, not state: these are written by event handlers many times a second and must not
  // trigger a re-render. Putting `lastPing` in state would re-render the whole subtree on
  // every scroll event, which is exactly the jank this page cannot afford on a low-end phone.
  const lastPing = useRef(0);
  const activitySinceLastPing = useRef(false);
  const endedRef = useRef(false);

  /** Hard stop: tell the backend why, then leave. */
  const forceLogout = useCallback(async (reason: "idle" | "user") => {
    // Guarded so a race between the ticker and a click cannot double-submit and produce
    // two audit entries for one event.
    if (endedRef.current) return;
    endedRef.current = true;

    await endSession(reason);
    // A full navigation rather than a router push: the session cookie is gone, so every
    // cached RSC payload for a gated route is now invalid. Replacing the document is the
    // only way to be sure nothing stale is rendered from memory.
    window.location.href =
      reason === "idle" ? "/auth/login?ended=idle" : "/auth/login";
  }, []);

  /** Read authoritative state. Also the mechanism that detects a server-side expiry. */
  const syncState = useCallback(async () => {
    const state = await readSessionState();

    if (state === null) {
      // Session already gone server-side — expired, revoked, or the account was
      // suspended while the tab sat open. Treat as an idle end so the user gets an
      // explanation rather than a bare login form.
      void forceLogout("idle");
      return;
    }

    setRemaining(state.seconds_remaining);
    setWarningAt(state.warning_at_seconds);
    setShowWarning(state.seconds_remaining <= state.warning_at_seconds);
  }, [forceLogout]);

  /** Report real input, throttled. */
  const pingActivity = useCallback(async () => {
    const now = Date.now();
    if (now - lastPing.current < ACTIVITY_THROTTLE_MS) {
      // Remember that something happened, so the next scheduled ping is not skipped just
      // because the input landed inside the throttle window.
      activitySinceLastPing.current = true;
      return;
    }
    lastPing.current = now;
    activitySinceLastPing.current = false;

    const state = await reportActivity();
    if (state === null) {
      void forceLogout("idle");
      return;
    }
    setRemaining(state.seconds_remaining);
    setWarningAt(state.warning_at_seconds);
    setShowWarning(false);
  }, [forceLogout]);

  // ---- activity listeners ------------------------------------------------- //
  useEffect(() => {
    const onActivity = () => {
      // While the warning is up, ordinary input must NOT silently dismiss it. The user is
      // being asked a question; answering it by moving the mouse would make the modal feel
      // like a glitch, and on a shared machine a passer-by's stray click would extend
      // someone else's session. They press the button.
      if (showWarning) return;
      void pingActivity();
    };

    for (const evt of ACTIVITY_EVENTS) {
      // `passive` so scroll handling is never blocked by this; `capture` so the handler
      // still sees events that a child component stops propagating.
      window.addEventListener(evt, onActivity, { passive: true, capture: true });
    }
    return () => {
      for (const evt of ACTIVITY_EVENTS) {
        window.removeEventListener(evt, onActivity, { capture: true });
      }
    };
  }, [pingActivity, showWarning]);

  // ---- initial read, polling, and visibility ------------------------------ //
  useEffect(() => {
    void syncState();

    const poll = window.setInterval(() => {
      // A hidden tab needs no countdown, and polling one wastes a subscriber's data. The
      // visibility handler below re-syncs the moment they come back.
      if (document.visibilityState === "visible") void syncState();
    }, POLL_INTERVAL_MS);

    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      // Re-read immediately. This is the sleeping-laptop case: local timers did not run,
      // so the interpolated value is meaningless and only the server knows the truth.
      void syncState();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [syncState]);

  // ---- the 1s ticker, only while warning ---------------------------------- //
  useEffect(() => {
    if (!showWarning || remaining === null) return;

    const tick = window.setInterval(() => {
      setRemaining((prev) => {
        if (prev === null) return prev;
        const next = prev - 1;
        if (next <= 0) {
          void forceLogout("idle");
          return 0;
        }
        return next;
      });
    }, 1000);

    return () => window.clearInterval(tick);
  }, [showWarning, remaining, forceLogout]);

  // ---- flush a pending activity ping on a schedule ------------------------ //
  useEffect(() => {
    const flush = window.setInterval(() => {
      // Input that landed inside the throttle window still counts — without this, someone
      // reading a long advisory who scrolls once a minute could be timed out despite
      // being demonstrably present.
      if (activitySinceLastPing.current && !showWarning) void pingActivity();
    }, ACTIVITY_THROTTLE_MS);
    return () => window.clearInterval(flush);
  }, [pingActivity, showWarning]);

  async function onStaySignedIn() {
    setExtending(true);
    const state = await extendSession();
    setExtending(false);

    if (state === null) {
      void forceLogout("idle");
      return;
    }
    lastPing.current = Date.now();
    setRemaining(state.seconds_remaining);
    setShowWarning(false);
  }

  if (!showWarning || remaining === null) return null;

  return (
    // `role="alertdialog"` rather than `dialog`: this interrupts to warn, and screen
    // readers announce it immediately rather than waiting to be navigated to.
    <div className="idlemodal" role="alertdialog" aria-modal="true" aria-labelledby="idle-title" aria-describedby="idle-body">
      <div className="idlemodal__card">
        <h2 id="idle-title" className="idlemodal__title">
          Still there?
        </h2>

        <p id="idle-body" className="idlemodal__body">
          You have been inactive for a while. For your security we will sign you out in{" "}
          <strong className="idlemodal__count">{formatCountdown(remaining)}</strong>.
        </p>

        {/* A progress bar as well as the number, because a bare digit changing gives no
            sense of how much time is left relative to the whole warning. `aria-hidden`
            since the countdown text already carries the information. */}
        <div className="idlemodal__track" aria-hidden="true">
          <span
            className="idlemodal__fill"
            style={{ width: `${Math.max(0, Math.min(100, (remaining / warningAt) * 100))}%` }}
          />
        </div>

        <p className="idlemodal__note">
          Your areas are still being monitored — signing out only closes this dashboard.
          Alerts continue regardless.
        </p>

        <div className="idlemodal__actions">
          <button
            type="button"
            onClick={onStaySignedIn}
            disabled={extending}
            className="btn btn--primary idlemodal__stay"
            // Focused on mount so Enter resolves the dialog — the fastest possible path
            // for someone returning to their machine.
            autoFocus
          >
            {extending ? "Keeping you signed in…" : "I'm still here"}
          </button>
          <button
            type="button"
            onClick={() => void forceLogout("user")}
            className="btn btn--ghost"
          >
            Sign out now
          </button>
        </div>
      </div>
    </div>
  );
}
