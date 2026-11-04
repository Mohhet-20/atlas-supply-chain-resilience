import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "./fixtures";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.resetModules();
});

async function renderLogin() {
  const { LoginPage } = await import("../pages/auth/LoginPage");
  const { tokenStore } = await import("../lib/registryApi");
  tokenStore.clear();
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<p>Overview page</p>} />
      </Routes>
    </MemoryRouter>,
  );
  return tokenStore;
}

describe("guest access on the sign-in page", () => {
  it("is hidden unless the build enables it", async () => {
    await renderLogin();
    expect(screen.queryByRole("button", { name: "Explore the live demo" })).toBeNull();
  });

  it("never shows a username or password for the demo", async () => {
    vi.stubEnv("VITE_GUEST_ACCESS", "true");
    await renderLogin();
    const panel = screen.getByRole("region", { name: "Live demo" });
    expect(panel.textContent).not.toMatch(/password/i);
  });

  it("starts a guest session and opens the app", async () => {
    vi.stubEnv("VITE_GUEST_ACCESS", "true");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        token: "atl_s_synthetic-guest",
        token_type: "bearer",
        expires_at: "2026-11-05T12:00:00+00:00",
        idle_timeout_minutes: 60,
        user: { username: "guest", display_name: "Guest", role: "editor", platform_admin: false, organization: null, must_change_password: false },
      }),
    );
    const tokenStore = await renderLogin();
    await userEvent.click(screen.getByRole("button", { name: "Explore the live demo" }));
    expect(await screen.findByText("Overview page")).toBeInTheDocument();
    expect(String(fetchSpy.mock.calls[0]?.[0])).toMatch(/\/auth\/guest$/);
    expect(tokenStore.get()).toBe("atl_s_synthetic-guest");
  });
});
