import sys, time

sys.path.insert(0, ".")
from datetime import timezone
from app.leadtime.config import LeadTimeConfig, LeadTimeLimits
from app.leadtime.pipeline import run_pipeline
from tests.leadtime_helpers import *

rows = gen_rows(600) + list(special_rows().values())
ds = to_deliveries(rows)
for cfgd in ({"target": "LEAD_TIME"}, {"target": "LATE_RISK"}, {"target": "LATE_RISK", "prediction_point": "DISPATCH"}):
    cfg = LeadTimeConfig(**cfgd)
    t = time.time()
    r = run_pipeline(ds, cfg, LeadTimeLimits(), NOW, lambda: None)
    print(cfgd, r["status"], round(time.time() - t, 2), r.get("decline_reason"))
    print(
        " splits",
        r["splits"]["train"]["n"],
        r["splits"]["validation"]["n"],
        r["splits"]["test"]["n"],
        "purged",
        r["splits"]["purged_from_train"],
    )
    print(" cands", r["candidates"])
    print(" sel", r["selection"]["status"], r["selection"]["selected"], r["selection"].get("rationale"))
    if cfg.target == "LEAD_TIME":
        for k, v in r["evaluation"]["models"].items():
            print("  ", k, round(v["validation"]["mae"]["value"], 3), round(v["test"]["mae"]["value"], 3))
        print(" interval", {k: v for k, v in r["intervals"].items() if k != "note"})
    else:
        for k, v in r["evaluation"]["models"].items():
            print("  ", k, v["validation"]["pr_auc"], v["test"]["pr_auc"], v["test"]["roc_auc"])
        c = r["calibration"]
        print(" cal", c and (c["decision"], c["reasons_not_calibrated"]), r["score_kind"])
