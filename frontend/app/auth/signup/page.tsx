import type { Metadata } from "next";

import SignUpPanel from "./SignUpPanel";

export const metadata: Metadata = {
  title: "Create an account",
  description:
    "Onboard in 60 seconds: sign up, drop a pin on your plot, pick a language. No data cost, no app install.",
};

export default async function SignUpPage({
  searchParams,
}: {
  searchParams: Promise<{ type?: string }>;
}) {
  const params = await searchParams;
  // Deep-linkable, so the footer's "For aggregators" link lands on the right tab
  // rather than making a cooperative hunt for it.
  return <SignUpPanel initialKind={params.type === "commercial" ? "commercial" : "individual"} />;
}
