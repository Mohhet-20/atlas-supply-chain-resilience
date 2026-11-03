# Cascading impact analysis: architecture, definitions and algorithm

cascading impact, algorithm version `ci-1`. Everything here is a **structural, hypothetical analysis** of a finished simulation run. It is not an observed event, a forecast or a guaranteed outcome.

## Architecture

| Layer | Module | Responsibility |
|---|---|---|
| Config | `app/impact/config.py` | `ImpactConfig` (bounded, unknown fields rejected), `config_hash`, scope and caveat texts |
| Structure | `app/impact/structure.py` | Pure functions: bounded path enumeration, directed reachability, cut tests. No database |
| Engine | `app/impact/engine.py` | Reads the run's frozen scenario, reuses the scenario engine (once for the combined view, once per initiating event), builds findings, criticality, alternatives and edge snapshots |
| Lifecycle | `app/impact/runs.py` | Create (idempotent), execute in the background, cancel, safe failure, active-run cap |
| Output | `app/impact/service.py`, `compare.py` | Response shaping, comparison |
| API | `app/api/v1/impact.py` | `/api/v1/impact/*` |
| Storage | Alembic `0008` | `impact_analyses`, `impact_findings`, `impact_edge_snapshots`, `impact_criticality`, `impact_alternatives` |

The analysis only **reads** canonical data (registry, relationships, quality scores). It writes only to its own five tables. The four result tables are insert-only (an ORM guard rejects update and delete), and a completed analysis is never overwritten: re-analysing creates a new row.

inventory (inventory and stockout) does not exist. Nothing here models inventory, demand, bills of materials or money, and nothing pretends to.

## Domain definitions

- **Initiating**: a named target of the scenario (for example the disrupted supplier). Its loss is the scenario's own assumption.
- **Direct**: one hop downstream of an initiating entity or relationship.
- **Propagated**: two or more hops downstream.
- **Potential exposure**: a dependency path exists, but the data does not support a quantity.
- **Quantified**: a number was computed from recorded supply shares (a share of recorded inbound supply, not an absolute quantity).
- **Unknown / unavailable**: the needed input is not recorded. It is shown as unknown, never as zero.
- **Unaffected within scope**: not reached inside the stated scope (confirmed relationships, downstream, within depth and limits). It does **not** mean safe.

Impact states on a finding: `SCENARIO_ASSUMED`, `QUANTIFIED`, `ASSUMPTION_DEPENDENT`, `POTENTIAL_EXPOSURE`, `INDETERMINATE`.

## Graph direction and relationship semantics

Edges are read in their canonical direction: supplier to facility (`SUPPLIES`), facility to facility (`TRANSFERS_TO`), and facility or supplier to product (`PRODUCT` links). Impact flows **downstream along edge direction only**. Ownership edges are excluded unless the run's assumptions include them. Only `CONFIRMED` relationships propagate; `PROPOSED` ones appear only as clearly labelled alternatives when asked for. `REJECTED` and `INACTIVE` edges never propagate. Edge types are not treated as interchangeable: a transport edge and a production link carry different meanings and each is stored with its type.

## Propagation algorithm

1. Take the run's frozen scenario snapshot (not the live scenario).
2. Run the scenario engine for the combined scenario and once per initiating event. Traversal is breadth-first with a deterministic order, bounded by depth, nodes, edges and a time budget. Cycles are safe: a node is expanded once per shortest depth.
3. Enumerate simple paths per event with a hard step cap (`max_path_steps`) and a per-entity storage cap. If the cap is hit, `paths_complete` is false and the path count is reported as a lower bound.
4. Classify each reached entity and attach per-event contributions, evidence, assumptions and rules.
5. Compute structural criticality on the directed affected subgraph and structurally possible alternates.
6. Persist everything with the algorithm version, config hash and graph hash.

## Aggregation and deduplication

- Every headline total counts each entity **once**, however many paths or events reach it.
- Paths and per-event contributions are preserved on the finding and are **never summed into totals**.
- When several events reach one entity: `SINGLE_EVENT`; `INDEPENDENT` (disjoint paths and sources, so lost shares add, capped at 100%); `OVERLAPPING` (shared paths or sources, so the value is assumption-dependent and bounds are given, not a sum); or `UNDETERMINED` when the path limit prevents the check.

## Worked example (synthetic)

Synthetic network: suppliers S1, S2, S3; facility F1 supplied 60% by S1 and 40% by S2; facility F2 supplied 100% by F1; product P1 made at F2. Scenario: S1 unavailable.

- S1 is **Initiating**, assumed loss 100%.
- F1 is **Direct**. Loss 60% of recorded inbound supply: **Quantified** (both shares are recorded).
- F2 is **Propagated** (depth 2), receiving all supply from F1. If the F1 to F2 share is not recorded, F2 is **Potential exposure**, loss **Unknown**, not 0 and not 100.
- P1 is **Propagated** (depth 3), potential exposure.
- S3, which is not connected, is *unaffected within scope*. That says nothing about whether S3 is safe.

Add a second event, S2 unavailable. F1 is reached by two events through different suppliers: `INDEPENDENT`, 60% + 40% = 100% (capped). If both events shared an upstream source the entry would be `OVERLAPPING`, shown with bounds and flagged "not additive".

## Why reachability is not shortage

A path from a disrupted supplier to a product means the disruption **could** travel there. Whether it does depends on things ATLAS does not hold: inventory buffers, safety stock, order timing, contractual commitments, demand, substitution, expediting, and the duration of the disruption relative to all of those. A reachable product may never run short; a product with no recorded path may still be exposed through an unrecorded dependency. So the analysis reports reachability as *potential exposure*, quantifies only what recorded shares support, and never states a stockout, delay date, lost quantity, revenue loss or insolvency.

## Structural, operational and financial bottlenecks

- **Structural** (what this module computes): a node whose removal disconnects downstream entities in the affected subgraph, found with directed cut tests. It is a candidate from graph shape alone.
- **Operational**: a node that actually constrains throughput (capacity, lead time, queueing, qualification). Needs capacity and flow data.
- **Financially material**: a node whose loss costs significant money. Needs revenue, margin and contract data.

A structural candidate may be operationally irrelevant (ample alternative capacity) or financially trivial. All criticality output carries the caveat "structural candidate only, needs validation".
