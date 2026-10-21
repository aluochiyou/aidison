import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  experimental: {
    // Module discovery and research-strategy planning may deliberately use the
    // backend's 600-second model deadline.  Next's rewrite proxy defaults to
    // 30 seconds, which would otherwise turn a still-running request into a
    // misleading 500 before the API can return its typed result.
    proxyTimeout: 615_000,
  },
  async rewrites() {
    const apiUrl =
      process.env.AIDISON_API_URL ||
      process.env.NEXT_PUBLIC_API_URL ||
      "http://127.0.0.1:8000";
    return [
      { source: "/health", destination: `${apiUrl}/health` },
      { source: "/api/:path*", destination: `${apiUrl}/api/:path*` },
    ];
  },
};

export default nextConfig;
