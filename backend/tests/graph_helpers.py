"""Synthetic fixtures for network graph tests (invented names only)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.registry_helpers import mk

G = "/api/v1/graph"
SINGULAR = {"suppliers": "SUPPLIER", "facilities": "FACILITY", "products": "PRODUCT"}


def rel(client: TestClient, typ: str, src: dict, dst: dict, *, status: str = "CONFIRMED", **kw: Any):
    """POST a relationship; ``src``/``dst`` are registry records (dicts with ``id`` and ``type`` from mk)."""
    body = {
        "type": typ,
        "source_type": src["type"], "source_id": src["id"],
        "destination_type": dst["type"], "destination_id": dst["id"],
        "status": status, "justification": "synthetic test evidence",
    } | kw  # fmt: skip
    return client.post(f"{G}/relationships", json=body, headers=kw.pop("headers", None) or {})


def ok(client: TestClient, typ: str, src: dict, dst: dict, **kw: Any) -> dict:
    r = rel(client, typ, src, dst, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def node(client: TestClient, kind: str, name: str, **kw: Any) -> dict:
    rec = mk(client, kind, name=name, **({"facility_type": "MANUFACTURING"} if kind == "facilities" else {}) | kw)
    return rec | {"type": SINGULAR[kind]}


def world(client: TestClient) -> dict[str, dict]:
    """s1,s2 -> f1 -> f2 -> f3 ; f1 produces p1 ; f3 stores p1 ; s1 supplies p2 ; s3 and f4 stay isolated."""
    w = {
        "s1": node(client, "suppliers", "Northwind Metals"),
        "s2": node(client, "suppliers", "Contoso Plastics"),
        "s3": node(client, "suppliers", "Lonely Supplier"),
        "f1": node(client, "facilities", "Plant One"),
        "f2": node(client, "facilities", "Hub Two"),
        "f3": node(client, "facilities", "Depot Three"),
        "f4": node(client, "facilities", "Unused Site"),
        "p1": node(client, "products", "Widget"),
        "p2": node(client, "products", "Gadget"),
    }
    ok(client, "SUPPLIES", w["s1"], w["f1"])
    ok(client, "SUPPLIES", w["s2"], w["f1"])
    ok(client, "TRANSFERS_TO", w["f1"], w["f2"])
    ok(client, "TRANSFERS_TO", w["f2"], w["f3"])
    ok(client, "PRODUCES", w["f1"], w["p1"])
    ok(client, "STORES", w["f3"], w["p1"])
    ok(client, "SUPPLIES_PRODUCT", w["s1"], w["p2"])
    return w


def q(key: dict) -> dict:
    return {"start_type": key["type"], "start_id": key["id"]}


def refs(graph: dict) -> set[tuple[str, int]]:
    return {(n["type"], n["id"]) for n in graph["nodes"]}
