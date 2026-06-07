# Consensus Factor Backtest Report

**Date:** 6 June 2026
**Status:** BLOCKED — insufficient data access

## Sample size

The recommendation log lives at `~/.tradingagents/portfolio_advisor/recommendation_log.jsonl` on the trading server (116.203.153.58). The server is currently unreachable via SSH. No local copy of the log exists on the Mac.

Without access to the recommendation log, we cannot:

- Count historical trades
- Bucket them by consensus alignment
- Compute realised returns
- Run threshold sensitivity sweeps
- Determine whether the consensus factor adds alpha

## What we know

The recommendation log was built during Phase 2 of the advisory system implementation (5 June 2026). At time of deployment, it had zero entries. Since then, no recommendation generating event has occurred that would write to the log:

- No EP scan has found qualifying candidates (the catalyst sleeve remains empty)
- No macro alerts have been triggered through the pre event alert pipeline
- The PM has answered direct queries but those are not logged as recommendations

The log likely contains fewer than 30 trades, which is the minimum sample for the backtest to produce statistically meaningful results. Even with server access, the backtest plan's stop condition would likely fire.

## Recommendation

1. Bring the server back online
2. Confirm the recommendation log has sufficient trades. If fewer than 30, the backtest is premature — run it after 90 days of live recommendation data
3. The consensus factor scoring infrastructure is built and tagged on every new recommendation regardless of feature flag. Once 30 or more trades accumulate, rerun the backtest

## What was built regardless

The branch `consensus-guardrails-v1` contains the full consensus factor infrastructure:

- `llm_consensus.py` — scrape based consensus snapshot with historical mode support
- `consensus_score.py` — entry, divergence, retail flow, and composite scoring
- `recommendation_log.py` — consensus tags on every trade
- `research_and_execution.py` — sizing modulation via `_apply_consensus_factor`
- Feature flag `CONSENSUS_FACTOR_LIVE` defaulting to false

When the server and data are ready, the backtest script can be written in under 30 minutes using these existing modules.

## Next step

Restart the trading server, then:

```bash
ssh trading-server "wc -l ~/.tradingagents/portfolio_advisor/recommendation_log.jsonl"
```

If the count exceeds 30, reissue the backtest command.
