# Risk Categories and Rules (`rm-1`)

| Category | Role | Weight | Data today |
|---|---|---|---|
| CONCENTRATION | risk | 0.15 | Relationship supply shares |
| DEPENDENCY | risk | 0.15 | Downstream reliance on this supplier |
| SINGLE_SOURCE | risk | 0.10 | Nodes with one confirmed supplier |
| GEOGRAPHIC | risk | 0.10 | Registry region labels (free text) |
| OPERATIONAL | risk | 0.10 | **No source: always UNAVAILABLE** |
| DELIVERY | risk | 0.15 | Delivery observations (stale ones not scored) |
| FINANCIAL | risk | 0.15 | **No source: always UNAVAILABLE** |
| COMPLIANCE | risk | 0.10 | **No source: always UNAVAILABLE** |
| DATA_QUALITY | reliability | — | scores; never changes the score |
| RELATIONSHIP_UNCERTAINTY | reliability | — | Proposed/unconfirmed edges; never changes the score |

Each factor has a state (SCORED / UNAVAILABLE / ...), a basis (DOCUMENTED / INFERRED), an explanation, missing inputs, evidence rows and freshness. Maximum reachable coverage today is **0.65**.
