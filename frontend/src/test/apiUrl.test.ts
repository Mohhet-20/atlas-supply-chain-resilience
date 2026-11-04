import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("API URLs", () => {
  it("resolve a same-origin base such as /api/v1 (the hosted demo) against the page", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "/api/v1");
    const { apiUrl } = await import("../lib/config");
    expect(apiUrl("/suppliers").toString()).toBe(`${window.location.origin}/api/v1/suppliers`);
  });

  it("send requests to the same origin with a relative base instead of failing as a network error", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "/api/v1");
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const { api } = await import("../lib/api");
    await api.suppliers({ limit: 20, offset: 0 });
    expect(String(spy.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/v1/suppliers?limit=20&offset=0`);
  });

  it("keep an absolute base unchanged", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.test/api/v1");
    const { apiUrl } = await import("../lib/config");
    expect(apiUrl("/suppliers").toString()).toBe("https://api.example.test/api/v1/suppliers");
  });
});
