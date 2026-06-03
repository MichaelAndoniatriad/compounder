"""Backfill core plans for ANET and MNDY (the 2 live tickers without one).

Mirrors the existing 8: strategy=core, target_horizon=3-5 years, entry_price
derived from weighted-avg open rate across all lots (same method as the
original backfill run that seeded the other plans).
"""
from datetime import datetime, timezone

from tradingagents.default_config import DEFAULT_CONFIG as cfg
from tradingagents.portfolio_advisor import etoro_scan
from tradingagents.portfolio_advisor.plan_validation import (
    group_position_rows_by_ticker,
    weighted_avg_open_for_lots,
)
from tradingagents.portfolio_advisor.position_plans import (
    PositionPlan,
    load_position_plans,
    upsert_position_plan,
)

_payload, _text, _tk, rows = etoro_scan.fetch_portfolio_rows()
by_ticker = group_position_rows_by_ticker(rows)
existing = load_position_plans(cfg)
missing = [t for t in sorted(by_ticker.keys()) if t not in existing]
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
note = (
    f"Backfilled {today} from eToro weighted-avg open rate; "
    "reclassify sleeve (core/catalyst) via adjust_position_plan if needed."
)

for tk in missing:
    lots = by_ticker[tk]
    entry = float(weighted_avg_open_for_lots(lots))
    plan = PositionPlan(
        ticker=tk,
        entry_price=entry,
        strategy="core",
        target_horizon="3-5 years",
        notes=note,
    )
    upsert_position_plan(cfg, plan)
    print(f"  upserted {tk}: entry=${entry:.4f}, strategy=core, horizon=3-5 years")

if not missing:
    print("  (no missing plans -- nothing to do)")
else:
    print(f"backfilled {len(missing)} plan(s): {', '.join(missing)}")
