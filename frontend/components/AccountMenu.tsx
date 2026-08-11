"use client";

import { useEffect, useRef, useState } from "react";

import { signOut } from "@/app/auth/actions";
import type { Account } from "@/lib/types";

/**
 * The signed-in identity, and the way out.
 *
 * ## Why this needs to exist at all
 *
 * Before it, the topbar showed "Sign in" and "Get started" to *everyone* — including
 * someone already signed in. Three things were wrong with that:
 *
 *   1. **No way to sign out.** On a shared handset in a cooperative office that is not a
 *      missing convenience, it is the security control the whole session design assumes.
 *   2. **No indication of who was signed in.** A shared device means the previous person's
 *      session may still be live, and a farmer had no way to notice they were looking at
 *      someone else's plots.
 *   3. **"Get started" invited an existing user to create a second account** — the most
 *      confusing possible call to action for someone who already has one.
 *
 * ## Why a disclosure button and not a hover menu
 *
 * Hover has no touch equivalent. This is a click/tap target with proper `aria-expanded`,
 * Escape-to-close and click-outside-to-close, so it works identically on a phone and with
 * a keyboard.
 *
 * ## Sign-out is a form POST, not a link
 *
 * A `GET` sign-out link can be triggered by anything that fetches a URL — an `<img>` tag
 * in an email, a prefetcher, a link scanner — which would sign users out at random. It is a
 * Server Action submit, so only a deliberate form submission ends the session.
 */
export default function AccountMenu({ account }: { account: Account }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };

    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // The account's avatar: a chosen emoji, or one derived from the account id.
  //
  // Emoji rather than initials because two subscribers named Amina Bello and Adamu Bala
  // both render "AB" — indistinguishable on a shared handset, which is the case where
  // recognising your own session matters. The emoji is stable for the life of the account,
  // so a *different* one is a visible signal that this is not the expected session.
  //
  // Falls back to initials where an emoji font is missing (notably older Android and
  // Windows Chrome), so nothing depends on the glyph rendering.
  const initials =
    `${account.first_name?.[0] ?? ""}${account.last_name?.[0] ?? ""}`.toUpperCase() ||
    account.email[0]?.toUpperCase() ||
    "?";
  const avatarEmoji = account.avatar_emoji;
  const avatarColor = account.avatar_color ?? undefined;

  const displayName =
    account.organisation ||
    `${account.first_name} ${account.last_name}`.trim() ||
    account.email;

  return (
    <div className="acctmenu" ref={wrapRef}>
      <button
        type="button"
        className="acctmenu__trigger"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span
          className="acctmenu__avatar"
          style={avatarColor ? { background: avatarColor } : undefined}
          aria-hidden="true"
        >
          {avatarEmoji ?? initials}
        </span>
        {/* The name is hidden below 640px but the avatar stays — on a 360px screen the
            name would push the theme toggle off the row, and the avatar plus the panel
            still identify the account. */}
        <span className="acctmenu__name">{displayName}</span>
        <svg
          className="acctmenu__chev"
          width="10"
          height="10"
          viewBox="0 0 10 10"
          aria-hidden="true"
        >
          <path
            d="M1 3l4 4 4-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div className="acctmenu__panel" role="menu">
          {/*
            The full identity, including the account id. The id is shown because it is the
            thing support will ask for, and a subscriber who cannot find it has to describe
            themselves by name — which is ambiguous.
          */}
          <div className="acctmenu__head">
            <p className="acctmenu__headname">{displayName}</p>
            <p className="acctmenu__headmail">{account.email}</p>
            <p className="acctmenu__headid mono">
              {account.id}
              {account.kind === "commercial" && (
                <span className="acctmenu__badge">Aggregator</span>
              )}
            </p>
          </div>

          <div className="acctmenu__group">
            <a href="/portal" role="menuitem" className="acctmenu__item">
              Overview
            </a>
            <a href="/portal/alerts" role="menuitem" className="acctmenu__item">
              My alerts
            </a>
            <a href="/portal/settings" role="menuitem" className="acctmenu__item">
              Settings
            </a>
            <a href="/portal/security" role="menuitem" className="acctmenu__item">
              Security
            </a>
            <a href="/portal/activity" role="menuitem" className="acctmenu__item">
              Activity log
            </a>
            {account.kind === "commercial" && (
              <a href="/portal/api-keys" role="menuitem" className="acctmenu__item">
                API keys
              </a>
            )}
          </div>

          {/*
            Sign-out as a form POST. A GET link would let any prefetcher, link scanner or
            image tag end someone's session.
          */}
          <form action={signOut} className="acctmenu__group">
            <button type="submit" role="menuitem" className="acctmenu__item acctmenu__item--danger">
              Sign out
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
