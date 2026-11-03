import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { NetworkGraph, computeLayers } from "../components/NetworkGraph";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { SuppliersPage } from "../pages/SuppliersPage";
import { dashboard, facility, jsonResponse, network, page, relationship, supplier } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

function mockApi(routes: Record<string, () => Response>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const path = new URL(String(input)).pathname.replace(/^\/api\/v1/, "");
    const handler = routes[path];
    return Promise.resolve(handler ? handler() : jsonResponse({ error: { code: "not_found", message: "nope" } }, 404));
  });
}

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("suppliers page states", () => {
  const renderPage = () =>
    render(
      <MemoryRouter>
        <Routes>
          <Route path="*" element={<SuppliersPage />} />
        </Routes>
      </MemoryRouter>,
    );

  it("shows a loading indicator, then real API rows with provenance", async () => {
    mockApi({ "/suppliers": () => jsonResponse(page([supplier])) });
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent(/loading suppliers/i);
    expect(await screen.findByRole("link", { name: "Synthetic Supplier Alpha" })).toHaveAttribute("href", "/suppliers/1");
    expect(within(screen.getByRole("table")).getByText("Synthetic demo")).toBeInTheDocument();
  });

  it("shows an empty state that points at the seed workflow", async () => {
    mockApi({ "/suppliers": () => jsonResponse(page([])) });
    renderPage();
    expect(await screen.findByText("No suppliers yet")).toBeInTheDocument();
    expect(screen.getByText(/seed_demo\.py/)).toBeInTheDocument();
  });

  it("shows a connection error with retry and never falls back to fake rows", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot connect to the atlas backend/i);
    expect(screen.queryByText(/Synthetic Supplier/)).not.toBeInTheDocument();
    spy.mockResolvedValue(jsonResponse(page([supplier])));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Synthetic Supplier Alpha")).toBeInTheDocument();
  });

  it("shows server errors with the request id", async () => {
    mockApi({ "/suppliers": () => jsonResponse({ error: { code: "internal_error", message: "An internal error occurred.", request_id: "abc123" } }, 500) });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("abc123");
  });

  it("sends the search term to the backend", async () => {
    const spy = mockApi({ "/suppliers": () => jsonResponse(page([supplier])) });
    renderPage();
    await screen.findByText("Synthetic Supplier Alpha");
    await userEvent.type(screen.getByLabelText(/search name or code/i), "alpha");
    await waitFor(() => expect(spy.mock.calls.some((c) => new URL(String(c[0])).searchParams.get("q") === "alpha")).toBe(true));
  });
});

describe("navigation and detail pages", () => {
  it("dashboard renders backend numbers, synthetic banner and insufficient-data metrics", async () => {
    mockApi({
      "/dashboard/summary": () => jsonResponse(dashboard),
      "/network/summary": () => jsonResponse(network),
      "/readiness": () => jsonResponse({ status: "ready", checks: { database: "ok", schema: "ok" } }),
    });
    renderApp("/");
    expect(await screen.findByText("Synthetic demo data.")).toBeInTheDocument();
    expect(screen.getByText("Data coverage").nextElementSibling).toHaveTextContent("n/a");
    expect(screen.getByTestId("metric-data_coverage_indicator")).toHaveTextContent("Insufficient data");
    expect(await screen.findByRole("group", { name: /supply-chain network: 2 nodes/i })).toBeInTheDocument();
    expect(await screen.findByText("API ready")).toBeInTheDocument();
  });

  it("dashboard shows empty-database guidance when nothing is seeded", async () => {
    mockApi({
      "/dashboard/summary": () => jsonResponse({ ...dashboard, supplier_count: 0, facility_count: 0, contains_synthetic_demo: false }),
      "/network/summary": () => jsonResponse({ ...network, nodes: [], edges: [] }),
      "/readiness": () => jsonResponse({ status: "ready", checks: {} }),
    });
    renderApp("/");
    expect(await screen.findByText("No data loaded yet")).toBeInTheDocument();
    expect(screen.queryByText("Synthetic demo data.")).not.toBeInTheDocument();
  });

  it("supplier detail links through to related facilities", async () => {
    mockApi({
      "/suppliers/1": () => jsonResponse({ ...supplier, relationships: [relationship], recent_observations: [] }),
      "/readiness": () => jsonResponse({ status: "ready", checks: {} }),
    });
    renderApp("/suppliers/1");
    expect(await screen.findByRole("heading", { name: "Synthetic Supplier Alpha" })).toBeInTheDocument();
    const link = screen.getAllByRole("link").find((a) => a.getAttribute("href") === "/facilities/1");
    expect(link).toBeDefined();
  });

  it("facility detail lists inbound and outbound relationships", async () => {
    mockApi({
      "/facilities/1": () =>
        jsonResponse({ ...facility, inbound_relationships: [relationship], outbound_relationships: [], recent_observations: [] }),
      "/readiness": () => jsonResponse({ status: "ready", checks: {} }),
    });
    renderApp("/facilities/1");
    expect(await screen.findByText("Inbound relationships")).toBeInTheDocument();
    expect(screen.getByText("Outbound relationships")).toBeInTheDocument();
    expect(screen.getByText("5,000 units/week")).toBeInTheDocument();
  });

  it("missing record shows a not-found error without a retry button", async () => {
    mockApi({
      "/suppliers/99": () => jsonResponse({ error: { code: "not_found", message: "Supplier 99 not found." } }, 404),
      "/readiness": () => jsonResponse({ status: "ready", checks: {} }),
    });
    renderApp("/suppliers/99");
    expect(await screen.findByRole("alert")).toHaveTextContent("Supplier 99 not found.");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("invalid ids never hit the API", async () => {
    const spy = mockApi({ "/readiness": () => jsonResponse({ status: "ready", checks: {} }) });
    renderApp("/suppliers/abc");
    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid supplier id/i);
    expect(spy.mock.calls.every((c) => !String(c[0]).includes("/suppliers/"))).toBe(true);
  });

  it("unknown routes render not-found", async () => {
    mockApi({ "/readiness": () => jsonResponse({ status: "ready", checks: {} }) });
    renderApp("/nowhere");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("reports an unreachable backend in the sidebar status", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    renderApp("/nowhere");
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });
});

describe("components", () => {
  it("provenance badge uses text, not colour alone", () => {
    render(<ProvenanceBadge provenance="MODEL_ESTIMATE" />);
    expect(screen.getByText("Model estimate")).toBeInTheDocument();
  });

  it("network graph layers suppliers left of facilities and tolerates cycles", () => {
    const nodes = [
      ...network.nodes,
      { type: "FACILITY" as const, id: 2, code: "DEMO-FAC-002", name: "B", status: "ACTIVE" as const, provenance: "SYNTHETIC_DEMO" as const },
    ];
    const transfer = (from: number, to: number, id: number) => ({
      ...relationship,
      id,
      relationship_type: "TRANSFERS_TO" as const,
      source: { type: "FACILITY" as const, id: from, code: `F${from}`, name: "" },
      target: { type: "FACILITY" as const, id: to, code: `F${to}`, name: "" },
    });
    const layers = computeLayers(nodes, [relationship, transfer(1, 2, 2)]);
    expect(layers.get("SUPPLIER:1")).toBe(0);
    expect(layers.get("FACILITY:1")).toBe(1);
    expect(layers.get("FACILITY:2")).toBe(2);
    expect(() => computeLayers(nodes, [relationship, transfer(1, 2, 2), transfer(2, 1, 3)])).not.toThrow();
  });

  it("network graph renders keyboard-focusable nodes", () => {
    render(
      <MemoryRouter>
        <NetworkGraph nodes={network.nodes} edges={network.edges} />
      </MemoryRouter>,
    );
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("tabindex", "0");
  });
});
