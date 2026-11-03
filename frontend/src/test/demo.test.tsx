import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function renderLogin() {
  const { LoginPage } = await import("../pages/auth/LoginPage");
  render(<MemoryRouter><LoginPage /></MemoryRouter>);
}

describe("public demo account", () => {
  it("is hidden unless both build variables are set", async () => {
    vi.stubEnv("VITE_DEMO_USERNAME", "demo");
    vi.stubEnv("VITE_DEMO_PASSWORD", "");
    await renderLogin();
    expect(screen.queryByRole("complementary", { name: "Demo account" })).toBeNull();
  });

  it("shows the credentials and fills the form", async () => {
    vi.stubEnv("VITE_DEMO_USERNAME", "demo");
    vi.stubEnv("VITE_DEMO_PASSWORD", "Synthetic-Visitor-Pass");
    await renderLogin();
    expect(screen.getByRole("complementary", { name: "Demo account" })).toHaveTextContent("Synthetic-Visitor-Pass");
    await userEvent.click(screen.getByRole("button", { name: "Use demo account" }));
    expect(screen.getByLabelText("Username")).toHaveValue("demo");
    expect(screen.getByLabelText("Password")).toHaveValue("Synthetic-Visitor-Pass");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});
