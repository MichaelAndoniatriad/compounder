# First Outcome Measurement Run Report

**Date:** 7 June 2026
**Branch:** `rec-log-bridge`
**Server:** Compounder (116.203.153.58)

## Pre flight

`outcome_tracker.py` was present on disk but not committed to the branch. Committed in ffcf0e3. `load_due_for_measurement()` was missing from `recommendation_log.py` — it was referenced in the outcome tracker spec but never implemented. Added in d243b95. Both are now on `rec-log-bridge`.

## Run Result

```
{'due': 0, 'measured': 0, 'skipped_no_ticker': 0, 'skipped_no_price': 0, 'good': 0, 'bad': 0, 'neutral': 0}
```

**Zero entries were due.** This is expected, not a bug. All 41 entries in `recommendation_log.jsonl` were backfilled from `proposed_trades.jsonl` with timestamps between 30 May and 4 June 2026. Today is 7 June. The default exit horizon is 30 days. The oldest entry is only 8 days old — well inside the window.

## Outcomes file

`outcomes.jsonl` was not created because no outcomes were measured. The file is only written on first measurement.

## Rule performance

```json
{"top": [], "bottom": [], "total_rules": 0, "total_outcomes": 0}
```

Empty — zero outcomes means zero rules with measured performance.

## Sample entries (oldest first)

```
2026-05-30 DASH buy
2026-05-30 DDOG sell
2026-05-31 DDOG trim
2026-05-31 ANET buy
2026-05-31 DDOG trim
...
2026-06-04 NVDA add
2026-06-04 ORCL trim
2026-06-04 ORCL trim
```

## First real measurement

The first batch of 5 entries (May 30) will become due on 29 June 2026. After that, new entries will age in daily. When the Sunday 29 June cron fires, it should measure approximately 5-10 entries depending on how many more proposals are logged between now and then.

## Cron entry added

`deploy/crontab.example` now contains:

```
0 6 * * 0 cd /opt/tradingagents && /opt/tradingagents/.venv/bin/python -c "from tradingagents.default_config import DEFAULT_CONFIG; from tradingagents.portfolio_advisor.outcome_tracker import compute_recommendation_outcomes; compute_recommendation_outcomes(DEFAULT_CONFIG.copy())"
```

Sunday 06:00 UTC. Not installed — documented only. User flips when ready.

## Unexpected findings

1. `load_due_for_measurement()` was specified but never built. The outcome tracker would have crashed on first use without it.
2. The 41 backfilled entries have no `exit_horizon_days` field, so they all use the 30-day default. The `propose_trade` bridge does not yet pass `exit_horizon_days` to `log_recommendation()`. A future patch should add this so catalyst-sleeve proposals get a shorter horizon (e.g. 14 days) and core-sleeve proposals get longer (e.g. 60 days).
3. `yfinance` is available on the server — no dependency issue.
