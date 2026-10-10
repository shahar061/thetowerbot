import type { NextConfig } from "next";

// `output: "export"` is production-only on purpose. Static export does not
// support rewrites, and the dev proxy below is how `next dev` on :3000 reaches
// the bot's API on :8765. Leaving `output` unset in development gives us a real
// dev server (HMR, rewrites); setting it for the build gives us plain files
// FastAPI can serve with no node at runtime.
const isProd = process.env.NODE_ENV === "production";

const nextConfig: NextConfig = {
  env: {
    NEXT_PUBLIC_UI_HASH: process.env.NEXT_PUBLIC_UI_HASH,
    NEXT_PUBLIC_BACKEND_HASH: process.env.NEXT_PUBLIC_BACKEND_HASH,
    NEXT_PUBLIC_DEV_UI: process.env.NEXT_PUBLIC_DEV_UI ?? "false",
  },
  output: isProd ? "export" : undefined,
  // Emits `out/runs/index.html` rather than `out/runs.html`, which is the
  // layout Starlette's StaticFiles(html=True) resolves for the URL `/runs/`.
  trailingSlash: true,
  // No node at runtime means no image optimisation server.
  images: { unoptimized: true },
  async rewrites() {
    if (isProd) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.TOWER_API_URL ?? "http://127.0.0.1:8765"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
