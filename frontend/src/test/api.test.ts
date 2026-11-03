import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { jsonResponse, page, supplier } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

describe("api client", () => {
  it("builds query strings and omits empty parameters", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(page([supplier])));
    const result = await api.suppliers({ limit: 20, offset: 0, q: "alpha", status: undefined });
    expect(result.items[0]?.supplier_code).toBe("DEMO-SUP-001");
    const url = new URL(String(spy.mock.calls[0]?.[0]));
    expect(url.pathname.endsWith("/suppliers")).toBe(true);
    expect(url.searchParams.get("q")).toBe("alpha");
    expect(url.searchParams.has("status")).toBe(false);
  });

  it("normalises the backend error envelope into ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "not_found", message: "Supplier 9 not found.", request_id: "req-1" } }, 404),
    );
    const err = await api.supplier(9).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", requestId: "req-1", message: "Supplier 9 not found." });
  });

  it("reports connection failures explicitly instead of faking data", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const err = (await api.dashboard().catch((e: unknown) => e)) as ApiError;
    expect(err.isConnectionError).toBe(true);
    expect(err.message).toMatch(/Cannot reach the ATLAS API/);
  });

  it("handles non-JSON error bodies", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    const err = (await api.network().catch((e: unknown) => e)) as ApiError;
    expect(err).toMatchObject({ status: 502, code: "http_error" });
  });

  it("rethrows aborts untouched", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new DOMException("aborted", "AbortError"));
    await expect(api.network()).rejects.toMatchObject({ name: "AbortError" });
  });
});
