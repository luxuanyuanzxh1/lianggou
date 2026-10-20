import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Docker 打包用：产出自包含的 .next/standalone（只带被引用到的 node_modules）
  output: "standalone",
  async rewrites() {
    const apiPort = process.env.RUIPAN_BACKTEST_API_PORT || "8767";
    return [
      {
        source: "/api/v1/:path*",
        destination: `http://127.0.0.1:${apiPort}/api/v1/:path*`,
      },
    ];
  },
  experimental: {
    cpus: 1,
    memoryBasedWorkersCount: false,
    parallelServerCompiles: false,
    parallelServerBuildTraces: false,
    webpackBuildWorker: false,
    webpackMemoryOptimizations: true,
  },
  turbopack: {
    root: path.resolve(process.cwd()),
  },
};

export default nextConfig;
