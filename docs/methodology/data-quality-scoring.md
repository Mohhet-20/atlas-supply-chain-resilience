# Scoring methodology (formula `qs-1`)

## Record score
```
score = 100 x (1 - failed_weight / applicable_weight)
```
- A **unit** is one rule applied to one field. Several failures of the same rule on the same base field (for example several duplicate candidates) count once, so one underlying issue is not counted repeatedly.
- `applicable_weight` = sum of severity weights of weighted units evaluated; `failed_weight` = weights of failed units. Weights: INFO 0, WARNING 1, ERROR 3, CRITICAL 5. INFO checks never change a score.
- **Accepted exceptions** are not counted as failures; the count is stored as `exceptions_excluded`.
- **Insufficient evidence:** fewer than 3 weighted units -> state `INSUFFICIENT_EVIDENCE`, no number. A record never evaluated is `NOT_EVALUATED` (also no number). Neither is ever shown as 0.
- The breakdown stores per-dimension units/failures/scores plus counts of *missing* (completeness) and *invalid* (validity) units, so "absent" and "wrong" are distinguishable.

## Aggregates (batch, source, record type, network)
Mean over **scored** records only, reported with `scored`, `insufficient_evidence`, `not_evaluated`, `coverage = scored / subjects`, min and a 4-bucket distribution (<50, 50-70, 70-90, >=90). A network mean of three records is shown with its 3-of-N coverage. Merged-away records are excluded from the denominator.

## Reproducibility and comparability
Every run stores `catalog_hash` (rules, versions, severities, enabled flags, config hash, formula version), `formula_version`, `config_version/hash` and the rule versions used; every score snapshot stores the hash. Score history and trends flag `comparable_with_previous/latest = false` when the hash differs. The same data and the same catalog always produce the same score (tested: repeated runs give identical findings and means).

## Interpretation
A high score means few of the existing checks failed. It does not prove correctness, completeness of the real world, or anything about supplier risk. Source indicators (`/quality/sources`) are measured counts and rates with fixed documented concern thresholds (>=20% of imported rows rejected, >=2 batches ended with errors, or mean record score below `low_score_threshold`); no single "source reliability" number is derived.
