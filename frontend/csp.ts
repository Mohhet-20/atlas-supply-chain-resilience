// The one Content-Security-Policy for the built app. nginx.conf serves it with connect-src 'self' (the API is proxied on
// the same origin); `vite preview` serves it with the API origin added, so the browser E2E suite runs under the same
// policy and fails on any violation. A unit test keeps nginx.conf in step with this list.
export const CSP_DIRECTIVES: readonly string[] = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
];

export function contentSecurityPolicy(connectOrigins: readonly string[] = []): string {
  return [...CSP_DIRECTIVES, ["connect-src 'self'", ...connectOrigins].join(" ")].join("; ");
}

export const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
} as const;
