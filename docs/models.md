# Model inventory - what is machine learning, what is a rule, what is optimisation

Summarised from the code. No accuracy figure is quoted
because none was measured on real data: evaluation in this repository uses synthetic fixtures that verify
the software, not predictive performance.

| Capability | Kind | Algorithm (as implemented) | Evaluation in repo | Key limitation |
|---|---|---|---|---|
| Supplier risk `rm-1` | **Rule-based score** (human-configured weights/thresholds) | Weighted category factors, evidence coverage, reliability band | Hand-computed unit tests | Not a probability, not trained, not validated against outcomes |
| Demand forecasting | **Statistical time-series** | naive, seasonal naive, mean, moving average, SES, damped Holt, Holt-Winters, ARIMA-like (OLS), Croston-type for intermittent | Rolling-origin backtests, MAE/MASE-style metrics, leakage tests, prediction intervals | Pure Python; short synthetic series only; no external regressors |
| Lead-time estimates | Statistical + **ridge regression** | Global/supplier/lane/hierarchical medians; ridge on delivery features | Time-split validation in tests | Needs enough recorded deliveries; otherwise reports insufficient data |
| Late-delivery risk | **Base-rate, supplier-rate, logistic regression** | Pure-Python logistic fit | Calibration/discrimination metrics computed by model-ops on fixtures | Probabilities are calibrated only if held-out evidence says so; otherwise a score |
| Explainability | Model-intrinsic | Coefficients, permutation importance (fixed seed); transparent weights | Tests assert unsupported methods return `unsupported` | SHAP, LIME, counterfactual, causal are **not implemented** |
| Route optimization | **Optimisation** (graph algorithms) | Shortest/constrained paths on a transport network | Compared with exhaustive enumeration on random small graphs | Costs/transit times are user-registered data |
| Supplier substitution | **Optimisation** (exact) | Branch-and-bound / dynamic programming, statuses OPTIMAL / INFEASIBLE / BOUND_EXCEEDED | Compared with exhaustive enumeration on random problems | Bounded search; reports BOUND_EXCEEDED rather than guessing |
| Inventory optimisation | **Simulation + policy search** | Safety stock formulas, (s,Q)/(s,S) policies, seeded Monte-Carlo | Math tests, engine tests | "Best" means lowest simulated cost among listed candidates, not a global optimum |
| Resilience optimisation | **Multi-objective candidate evaluation** | Six objectives, constraint filtering, Pareto set, sensitivity | Engine and API tests | Candidate set is enumerated/bounded, not a continuous solver |
| Disruption simulation | **Simulation** (deterministic) | Rule-driven propagation over graph with documented assumptions | Engine tests | Results are scenario outcomes, not forecasts |
| Cascading impact | **Structural analysis** | Tiered exposure, criticality, alternatives | Engine tests | Potential exposure, not predicted loss |
| Thresholds | **Human-configured** | Alert rules, stockout thresholds, service targets | Config validation tests | Defaults are examples, not industry benchmarks |

Not present: deep learning, gradient boosting, clustering, NLP, computer vision, SHAP/LIME, any
externally trained or downloaded model, any model-serving infrastructure.

Reproducibility: forecasting, replenishment simulation and permutation importance take explicit seeds and the
tests assert repeatable output; model runs store version, configuration and input fingerprints. Cross-platform
bitwise reproducibility was not tested.

Leakage and validation: rolling-origin splits and a leakage check for engineered features exist for
forecasting; lead-time models use time-ordered splits. They have been exercised only on synthetic data.
