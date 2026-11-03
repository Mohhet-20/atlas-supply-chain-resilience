# Simulation Methodology (`sim-1`)

## Inputs
Frozen scenario definition (events + assumptions) and the current confirmed network. Assumptions: `downstream_depth` (default 3, server max 4), `upstream_depth` (1), `include_upstream` (false), `include_ownership` (false), `include_alternatives` (true), `include_proposed_alternatives` (false), `as_of_date` (optional relationship effectiveness date), `assume_no_buffer` (true). The engine has no inventory data, so buffers are never assumed to exist; this is stated in every run.

## Steps
1. Resolve each event to seed entities/relationships and a failed fraction f (1.0 for unavailable/shutdown/relationship loss).
2. Directly affected entities are recorded as `DIRECT`.
3. Downstream expansion with `expand_multi` (level-synchronous, bounded). An entity's **rank** is its hop count from the nearest disrupted entity; a failed relationship's destination has rank 1.
4. Parents of a node are inbound edges from rank-(r-1) nodes plus forced failed/delayed edges. Back and cross edges are ignored, so cycles terminate.
5. Each entity appears **once**; `path_count` and `parent_edges` record the multi-path merge.
6. Optional upstream expansion (default depth 1) is qualitative only.
7. Alternatives are collected (see ALTERNATIVE_SUPPLY_PATHS).

## Loss rule
`loss(v) = min(100, Σ share_e × lost_fraction(source_e))` over parent edges, **only when every parent edge has a known supply share**. Otherwise the result is `LOWER_BOUND` (sum of known shares, flagged) or `UNQUANTIFIED`. `STORES` and `OWNS` edges are qualitative. Shares are the recorded `supply_share_pct`, never inferred.

## Delay rule
`delay(v) = max over parents (delay(parent) + added days on edge)`, no buffer. Edges with unknown lead time still propagate the added days; absolute lead time is shown as unknown.

## Capacity
Facility seed: recorded capacity × fraction. Edge capacity at risk is summed **per unit** and never converted between units. Suppliers have no capacity field, so none is claimed. Totals appear only when recorded values exist.

## Exposure levels
`DIRECT`, `HIGH` (loss or lower bound ≥ 50%), `PARTIAL`, `UNQUANTIFIED`, `DELAY`, `QUALITATIVE`. These are descriptive bands of potential exposure, not probabilities and not severity scores.

## Determinism and versioning
Same definition + same network + same engine/config → identical results (tested, including ordering). `engine_version`, `config_hash`, `definition_hash` and `graph_hash` are stored on each run.

## What is deliberately absent
Probabilities, expected values, cost or revenue, inventory depletion, substitution decisions, demand effects, recovery curves.
