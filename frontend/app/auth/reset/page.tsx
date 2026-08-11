import type { Metadata } from "next";

import ResetPanel from "./ResetPanel";

export const metadata: Metadata = {
  title: "Choose a new password",
  robots: { index: false, follow: false },
};

export default async function ResetPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;

  // No token means the user reached this page directly rather than from the email, so
  // say so rather than showing a form that cannot possibly work.
  if (!token) {
    return (
      <>
        <h1 className="authpanel__title">Link not recognised</h1>
        <p className="authpanel__lede">
          This page needs the link from your reset email. If the link has expired, you
          can request a new one.
        </p>
        <p className="authpanel__alt">
          <a href="/auth/forgot">Request a new reset link</a>
        </p>
      </>
    );
  }

  return <ResetPanel token={token} />;
}
