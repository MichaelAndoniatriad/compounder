# Recommendation Log Bridge Report

**Date:** 7 June 2026
**Branch:** `rec-log-bridge` (off main, not merged)
**Server:** Compounder (116.203.153.58)

## What changed

**`pm_tools.py`:** After every successful `proposals.add()` call inside `propose_trade()`, the code now also calls `log_recommendation()` to write an entry to `recommendation_log.jsonl`. The call is wrapped in its own try/except — a log failure does NOT break the propose_trade tool. Mapping rules:

| Proposal field | Recommendation field | Rule |
|---|---|---|
| ticker | ticker | Preserved |
| action | action | Preserved |
| shares | shares | Preserved if > 0, else None |
| target_price | entry_price | Preserved if > 0, else None |
| reason | rationale | Truncated to 600 chars (existing logic in rec log) |
| sleeve | rule_ref | "core" or "catalyst" only; anything else → None |
| (generated) | trigger | Hardcoded to "action_check" |
| (generated) | type | Hardcoded to "trade_proposal" |
| (generated) | status | Hardcoded to "pending" |

## Backfill

**`scripts/backfill_proposed_trades_to_rec_log.py`:** One-shot, idempotent. Reads `proposed_trades.jsonl`, checks each entry's `(ts, ticker, action)` tuple against existing `recommendation_log.jsonl` rows, appends only new entries. Supports `--dry-run`.

**Run results on server:**
- Source: 41 entries in `proposed_trades.jsonl`
- Existing in rec log: 0 (file did not exist)
- Backfilled: 41 entries
- Second run: 0 new, 41 skipped (idempotent confirmed)

## Sample backfilled entry

```json
{
  "id": "0193b131b70c4307",
  "ts": "2026-05-30T00:46:02.392235+00:00",
  "trigger": "action_check",
  "type": "trade_proposal",
  "ticker": "DASH",
  "action": "buy",
  "rationale": "Strongest fundamentals in watchlist: 33% rev growth...",
  "rule_ref": "core",
  "entry_price": null,
  "stop_price": null,
  "shares": null,
  "status": "pending"
}
```

## Unit tests

**`tests/test_propose_trade_bridge.py`:** 5 tests, all passing.

| Test | What it verifies |
|---|---|
| `test_propose_trade_calls_log_recommendation` | Full mapping: ticker, action, shares, entry_price, sleeve→rule_ref |
| `test_propose_trade_with_catalyst_sleeve` | "catalyst" sleeve maps to rule_ref="catalyst" |
| `test_propose_trade_unknown_sleeve_maps_to_none` | "growth" (unrecognised) → rule_ref=None |
| `test_propose_trade_handles_log_failure_gracefully` | OSError in log_recommendation does not break propose_trade |
| `test_propose_trade_zero_shares_and_price` | 0.0 values map to None, not 0.0 |

## Confirmation

- `recommendation_log.jsonl` exists at `/home/ubuntu/.tradingagents/portfolio_advisor/` with 41 entries
- Every future `propose_trade` call will append to both `proposed_trades.jsonl` and `recommendation_log.jsonl`
- The bridge is live on the `rec-log-bridge` branch on the server
