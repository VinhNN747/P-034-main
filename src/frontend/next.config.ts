import path from "node:path";
import { loadEnvConfig } from "@next/env";
import type { NextConfig } from "next";

loadEnvConfig(path.resolve(process.cwd(), "../.."));

const allowedDevOrigins = (process.env.DEV_ALLOWED_ORIGINS ?? "172.16.18.55")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  allowedDevOrigins,
  devIndicators: false,
};

export default nextConfig;
