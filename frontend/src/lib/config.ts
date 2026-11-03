const raw = import.meta.env.VITE_API_BASE_URL as string | undefined;

/** API base URL (no trailing slash). Configure with VITE_API_BASE_URL. */
export const API_BASE_URL: string = (raw && raw.trim() !== "" ? raw.trim() : "http://127.0.0.1:8000/api/v1").replace(/\/+$/, "");
