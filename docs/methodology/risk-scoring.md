# Risk Scoring Methodology (`rm-1`)

`score = Σ(W_i · s_i) / Σ(W_i)` over available risk-role factors. CONCENTRATION, DEPENDENCY and SINGLE_SOURCE form one **correlation group**: only the highest member is counted, so one fact is not triple-counted.

Thresholds: MODERATE ≥ 30, HIGH ≥ 55, CRITICAL ≥ 75.

Minimums: a score needs ≥ 2 scored factors and ≥ 0.30 evidence coverage; a classification needs ≥ 0.50 coverage. A score without a class is `PARTIAL`; below the score minimum the status is `INSUFFICIENT_EVIDENCE` and no score is emitted.

Shares: documented shares are used only if every supplier has one and they total 100% (±2 points); otherwise an equal-share approximation is used and labelled INFERRED (can be switched off). HHI is computed only from documented shares.

The parameters are analytical choices, **not calibrated** to outcomes. A methodology change gets a new version label; the run hash records it and mixed-methodology portfolios are flagged. Hand-computed expectations are in the tests.
