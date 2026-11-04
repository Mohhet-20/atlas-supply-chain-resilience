/**
 * Bearer token handling, shared by every API client. The token lives in memory and, optionally, sessionStorage
 * (tab-scoped; cleared when the tab closes). It is never put in a URL, localStorage, logs or error messages.
 */
const STORAGE_KEY = "atlas.registry.token";
let token = "";
let expired = false;
/** True once the API refused a request that carried no token: this deployment requires signing in. */
let signInRequired = false;
const listeners = new Set<() => void>();

function readStored(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}
token = readStored();

export const tokenStore = {
  get: () => token,
  /** True once a request with a token was refused with 401 (session ended, expired or revoked). */
  expired: () => expired,
  signInRequired: () => signInRequired,
  /** Drop a token the server no longer accepts and tell the UI, so it can ask the person to sign in again. */
  expire() {
    expired = true;
    token = "";
    try {
      window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* storage unavailable */
    }
    listeners.forEach((l) => l());
  },
  set(value: string, remember: boolean) {
    expired = false;
    token = value.trim();
    try {
      if (remember && token) window.sessionStorage.setItem(STORAGE_KEY, token);
      else window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* storage unavailable: keep in memory only */
    }
    listeners.forEach((l) => l());
  },
  clear() {
    signInRequired = false;
    this.set("", false);
  },
  subscribe(l: () => void) {
    listeners.add(l);
    return () => void listeners.delete(l);
  },
};

/** Authorization header for the current session, or none. */
export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** A 401 to a request that carried a token means the session ended. */
export function noteUnauthorized(status: number, sentToken: boolean): void {
  if (status !== 401) return;
  if (sentToken && token) {
    tokenStore.expire();
  } else if (!sentToken && !signInRequired) {
    // Local development serves anonymous requests; a deployment that refuses them needs the visitor to sign in.
    signInRequired = true;
    listeners.forEach((l) => l());
  }
}

/** Fetch a file with the bearer token (never in the URL) and hand it to the browser as a download. */
export async function downloadFile(url: string, fileName: string): Promise<void> {
  const headers = authHeaders();
  const r = await fetch(url, { headers });
  noteUnauthorized(r.status, Boolean(headers.Authorization));
  if (!r.ok) {
    let msg = `Download failed with status ${r.status}.`;
    try {
      msg = ((await r.json()) as { error?: { message?: string } }).error?.message ?? msg;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  const blob = await r.blob();
  if (typeof URL.createObjectURL === "function") {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = fileName;
    a.click();
    URL.revokeObjectURL(a.href);
  }
}
