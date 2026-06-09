# Recommendation Log Audit

**Date:** 7 June 2026
**Server:** Compounder (116.203.153.58), `/opt/tradingagents/`

## Files Compared

| File | Entries | Status |
|---|---|---|
| `portfolio_advisor/recommendation_log.jsonl` | 0 | File does not exist |
| `portfolio_advisor/proposed_trades.jsonl` | 41 | Active, written daily |

These are separate data streams with different schemas and different writers. The 41-entry file is not a substitute for the empty one. They track different things.

## Schema Comparison

### recommendation_log.jsonl

Written by `log_recommendation()` in `recommendation_log.py`. Purpose: log every piece of advice the PM sends to the human before the Telegram message goes out. Designed for outcome measurement (was the advice correct? what was the P&L impact?).

```
{id, ts, trigger, type, ticker, action, rationale, rule_ref,
 entry_price, stop_price, shares, status, human_response,
 outcome_measured_at, was_correct, pnl_impact_est, outcome_note}
```

### proposed_trades.jsonl

Written by `proposals.add()` in `proposals.py`, called from the `propose_trade` tool (`pm_tools.py:296`). Purpose: record trade proposals the PM agent generates during its decision loop. These are buy/sell actions with a status lifecycle.

```
{ts, ticker, action, shares, approx_usd, target_price, sleeve,
 reason, status, status_set_at, status_note}
```

## How log_recommendation Is Wired

`log_recommendation()` is called from `messaging.send_advisor_message()` when `log_as_recommendation=True`. Two code paths set this flag:

1. **`pm_tools.py:392`** — inside `log_market_event()`. When the PM observes a macro event and calls `log_market_event`, it checks `check_pre_event_alert()`. If an existing macro rule matches the event, it sends a "MACRO ALERT" message with `log_as_recommendation=True`.

2. **`pm_tools.py:648`** — inside `emit_ep_recommendation()`. When the PM agent finds a qualifying earnings/pivot setup during an EP scan, it sends an "EP RECOMMENDATION" message with `log_as_recommendation=True`.

## Why recommendation_log.jsonl Is Empty

Neither call path has ever been reached in production.

**Path 1 (pre-event alert):** The `check_pre_event_alert()` function in `macro_learning.py:172` has a hard gate at line 179: `if not existing_rules or len(existing_rules) < 50: return None`. This reads the "Macro-learned rules" section from `_portfolio.md`. If fewer than 50 characters of rules text exist, no alert ever fires. The server has 11 per-ticker rule files and a strategies directory, but the extracted macro section is either short or empty. The system is too new to have accumulated enough macro patterns.

Additionally, the `log_market_event` tool requires the PM agent to explicitly call it. EP scan logs show `push=False` on every run — the PM agent completed its cycle but called no notification tools.

**Path 2 (EP recommendation):** The EP scan cron fires at 08:30 ET on weekdays. The logs show two completed runs (4 June and 5 June), both with `push=False`. The PM agent ran through its analysis but found no setups that qualified for a human recommendation. The `emit_ep_recommendation` tool was never invoked, so `log_as_recommendation=True` was never reached.

In summary: the code is wired correctly. The file is empty because production PM agents have not yet produced a qualifying macro alert or EP setup. The `proposed_trades.jsonl` has 41 entries because the `propose_trade` tool is called during different PM cycles (action-check, watchdog) that do produce output, but those cycles do not route through `send_advisor_message` with `log_as_recommendation=True`.

## Verification From Server Logs

- `grep "MACRO ALERT\|EP RECOMMENDATION" logs/portfolio-advisor-*.log` — zero matches across all log files
- EP scan runs: `push=False` on every completed run
- `ep_trades.jsonl` (tracks executed EP trades): 0 lines
- No errors referencing `log_recommendation` or `recommendation_log` in any log file

## Recommended Fix

The root issue is not a bug. It is a cold-start problem: the recommendation log is empty because the system has not yet triggered its first human-facing recommendation. However, waiting silently is bad. Three changes:

1. **Lower the macro gate.** The 50-character minimum on `_load_existing_rules()` is arbitrary. Drop it to 1 character or remove it entirely. Even a single learned rule should be able to trigger a pre-event alert. If the concern is false positives, gate on the LLM match quality instead.

2. **Log "no recommendation" runs.** After each EP scan or action-check that produces `push=False`, write a lightweight entry to a `recommendation_log.jsonl` (or a separate `advisor_runs.jsonl`) recording that the system ran and found nothing. Right now, `push=False` means silence — you cannot distinguish "everything is fine" from "the system is broken." A no-op entry like `{"ts": "...", "trigger": "ep_scan", "type": "no_signal", "action": "hold", "rationale": "no qualifying setups found"}` would close the observability gap.

3. **Bridge propose_trade to recommendation_log.** When `propose_trade` is called (41 times and counting), the PM is making a recommendation, just not through the Telegram messaging path. Add a `log_recommendation()` call inside `proposals.add()` or immediately after it in `pm_tools.py:296`, using `trigger="action_check"` and `type="trade_proposal"`. This immediately backfills the recommendation log with the 41 existing proposals and ensures future proposals are tracked from day one.
