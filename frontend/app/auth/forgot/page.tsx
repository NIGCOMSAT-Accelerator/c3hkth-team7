import type { Metadata } from "next";

import ForgotPanel from "./ForgotPanel";

export const metadata: Metadata = {
  title: "Reset your password",
  robots: { index: false, follow: false },
};

export default function ForgotPage() {
  return <ForgotPanel />;
}
