import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Emits a self-contained server bundle with only the node_modules it actually
  // imports, which is what makes the container image viable (~150 MB rather than
  // shipping the full dependency tree). Required by frontend/Dockerfile.
  //
  // Harmless for Netlify — @netlify/plugin-nextjs handles its own packaging and
  // ignores this — so both deployment paths work from one config.
  output: "standalone",

  // Pin the workspace root. Without this Turbopack walks up looking for a
  // lockfile and can pick one up from outside the repository.
  turbopack: {
    root: path.join(process.cwd(), ".."),
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(self)",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
