# Core Discovery Cron Installation

**Date:** 8 June 2026
**Branch:** `discovery-pipeline-fixes`

## Cron line added

```
0 10 * * 6 cd /opt/tradingagents && /opt/tradingagents/.venv/bin/python -c 'from tradingagents.default_config import DEFAULT_CONFIG; from tradingagents.portfolio_advisor.core_discovery import run_core_discovery; print(run_core_discovery(DEFAULT_CONFIG.copy()))' >> /var/log/tradingagents/core_discovery.log 2>&1
```

Runs every Saturday at 10:00 UTC. US markets are closed, so yfinance calls complete without throttling.

## Smoke test

Ran with a 24-name fallback universe (NVDA, MSFT, GOOGL, AMZN, META, AVGO, TSM, AAPL, CRM, ADBE, INTU, SNOW, DDOG, NET, CRWD, PANW, SHOP, UBER, ABNB, ALGN, ABBV, NOW, ZS, FTNT) to avoid blowing the yfinance budget on a weekday.

**Result:** 1 candidate produced and sent via Telegram (dry run suppressed the actual message).

Full output: `Core discovery: 1 candidates sent via Telegram`

## First scheduled run

Saturday 14 June 2026 at 10:00 UTC.

## Log file

```
/var/log/tradingagents/core_discovery.log
```

To verify the first run: `tail -30 /var/log/tradingagents/core_discovery.log` on the server.

## Cron verification

Full crontab confirmed — no duplicates, no other entries modified:

```
0 10 * * 6 cd /opt/tradingagents && ... core_discovery ...
```

Existing entries (action-check, ep-scan, macro-review, outcome-tracking, dead-man's-switch) untouched.
