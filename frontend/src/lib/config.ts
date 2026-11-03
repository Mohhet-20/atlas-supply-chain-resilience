const raw = import.meta.env.VITE_API_BASE_URL as string | undefined;

/** API base URL (no trailing slash). Configure with VITE_API_BASE_URL. */
export const API_BASE_URL: string = (raw && raw.trim() !== "" ? raw.trim() : "http://127.0.0.1:8000/api/v1").replace(/\/+$/, "");

/**
 * Public demo account, shown on the sign-in page only when both are set at build time (VITE_DEMO_USERNAME,
 * VITE_DEMO_PASSWORD). It is meant for the hosted synthetic-data demo; never set these for a real deployment.
 */
const demoUser = (import.meta.env.VITE_DEMO_USERNAME as string | undefined)?.trim();
const demoPassword = (import.meta.env.VITE_DEMO_PASSWORD as string | undefined)?.trim();
export const DEMO_ACCOUNT: { username: string; password: string } | undefined =
  demoUser && demoPassword ? { username: demoUser, password: demoPassword } : undefined;
