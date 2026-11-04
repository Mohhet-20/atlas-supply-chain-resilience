const raw = import.meta.env.VITE_API_BASE_URL as string | undefined;

/** API base URL (no trailing slash). Configure with VITE_API_BASE_URL. */
export const API_BASE_URL: string = (raw && raw.trim() !== "" ? raw.trim() : "http://127.0.0.1:8000/api/v1").replace(/\/+$/, "");

/**
 * A URL on the API. Resolved against the page's origin, so a same-origin base such as "/api/v1" (the hosted demo,
 * where the static site proxies /api/*) works as well as an absolute one; new URL() alone throws on a relative base.
 */
export function apiUrl(path: string): URL {
  return new URL(`${API_BASE_URL}${path}`, window.location.origin);
}

/**
 * Hosted demo: the sign-in page offers "Explore the live demo", which starts a guest session through POST /auth/guest
 * (a non-admin account; no password involved). Set VITE_GUEST_ACCESS=true at build time, together with
 * ATLAS_GUEST_ACCESS_ENABLED on the API.
 */
export const GUEST_ACCESS: boolean = (import.meta.env.VITE_GUEST_ACCESS as string | undefined)?.trim() === "true";
