import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

beforeEach(() => tokenStore.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("a deployment that requires signing in", () => {
  it("sends an anonymous visitor to the sign-in page instead of showing an error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "unauthenticated", message: "A bearer token is required." } }, 401),
    );
    renderApp("/");
    expect(await screen.findByRole("form", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/Something went wrong/)).not.toBeInTheDocument();
  });

  it("leaves local development alone, where anonymous requests are served", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "not_found", message: "nothing here" } }, 404),
    );
    renderApp("/");
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(tokenStore.signInRequired()).toBe(false);
    expect(screen.queryByRole("form", { name: "Sign in" })).not.toBeInTheDocument();
  });
});
