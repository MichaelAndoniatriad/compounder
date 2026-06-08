# Core Discovery — Alpha Vantage Migration Report

**Date:** 8 June 2026
**Branch:** discovery-pipeline-fixes
**Status:** Code complete, blocked on server recovery for verification

## What changed

### New module: `alpha_vantage_fundamentals_cached.py`
- Wraps the existing `get_fundamentals()` (AV OVERVIEW endpoint)
- Returns a dict with yfinance-compatible field names so `_score_quantitative()` needs zero changes
- 7-day disk cache at `~/.tradingagents/cache/fundamentals/<TICKER>.json`
- Atomic writes (tmp file + rename) to survive crashes
- Rate limit delay: 0.3s sleep every 10 tickers (~200 calls/min)

### Field mapping

| yfinance field | AV source | Cast |
|---|---|---|
| marketCap | MarketCapitalization | str → float |
| revenueGrowth | QuarterlyRevenueGrowthYOY | str → float |
| pegRatio | PEGRatio | str → float |
| returnOnEquity | ReturnOnEquityTTM | str → float |
| grossMargins | GrossProfitTTM / RevenueTTM | computed |
| shortName | Name | str |
| sector | Sector | str |
| industry | Industry | str |
| forwardPE | ForwardPE | str → float |
| debtToEquity | DebtToEquityRatio | str → float (None → 0) |

### Known imperfections

- **No currentPrice.** AV OVERVIEW doesn't include price. Set to 0.0. Only affects display, not scoring.
- **Gross margin computed from TTM totals** (GrossProfitTTM / RevenueTTM). For companies with zero RevenueTTM in AV, margin defaults to 0.
- **DebtToEquityRatio** is often None/NONE for companies without reported debt. Defaults to 0.
- **No quoteType.** yfinance used this to filter out ETFs/warrants. Mechanical filter (Layer 1) already handles this, so it's not needed at the quant layer.

## What was NOT changed

- Scoring math (tiers, thresholds, combo bonus) — untouched
- Mechanical filter — still uses yfinance fast_info (only fails on Hetzner)
- Wikipedia universe builder — untouched
- LLM qualitative rank — untouched
- Watchlist injection — untouched

## Cost estimate

- 516 tickers / 7 days ≈ 74 calls per day
- Alpha Vantage free tier: 25 calls/day (too low)
- Alpha Vantage premium ($49.99/mo): 75 calls/min, unlimited daily
- At 74 calls/day, comfortably within free-tier daily limits. The 75/min rate limit is the binding constraint, which the 0.3s delay handles.

Actually: 74 calls/day exceeds the free tier's 25/day. Need premium or a different approach. Alternative: cache all tickers on first Saturday run, then only refresh changed ones. With 7-day TTL, each Saturday runs 516 calls once. That's 4 Saturdays/month × 516 = 2064 calls/month. At $49.99/mo premium that's fine. Or split across multiple free API keys (not recommended).

## Server verification — BLOCKED

Server 116.203.153.58 is unreachable (no ping, no SSH). Same symptoms as the 7 June brute force outage. Cold run and warm run not yet executed.

Steps to complete when server recovers:

```bash
# Cold run (24 ticker subset)
ssh trading-server << 'EOF'
cd /opt/tradingagents
git pull origin discovery-pipeline-fixes
timeout 180 .venv/bin/python -c "
import tradingagents.portfolio_advisor.messaging as m
m.send_advisor_message = lambda *a, **kw: print(f'(dry run) would send: {kw.get(\"rec_action\",\"?\")}')
import tradingagents.portfolio_advisor.core_discovery as cd
cd._build_universe = lambda: ['NVDA','MSFT','GOOGL','AMZN','META','AVGO','TSM','AAPL','CRM','ADBE','INTU','SNOW','DDOG','NET','CRWD','PANW','SHOP','UBER','ABNB','ALGN','ABBV','NOW','ZS','FTNT']
from tradingagents.default_config import DEFAULT_CONFIG
print(cd.run_core_discovery(DEFAULT_CONFIG.copy()))
"
EOF

# Warm run (cache hits)
ssh trading-server << 'EOF'
timeout 30 .venv/bin/python -c "
import tradingagents.portfolio_advisor.core_discovery as cd
cd._build_universe = lambda: ['NVDA','MSFT','GOOGL','AMZN','META','AVGO','TSM','AAPL']
from tradingagents.default_config import DEFAULT_CONFIG
import time
t0 = time.time()
cd.run_core_discovery(DEFAULT_CONFIG.copy())
print(f'warm run: {time.time()-t0:.1f}s (target: <5s)')
"
EOF
```

## Saturday cron status

Crontab was cleaned in the previous deployment (server-side entry removed, replaced with Hermes cron on Mac). The Hermes cron `core-discovery-weekly` at Saturday 12:00 BST still points at the Mac's venv. After server recovery, this should be switched to run via the server's crontab:

```
0 11 * * 6 cd /opt/tradingagents && /opt/tradingagents/.venv/bin/python -c "from tradingagents.default_config import DEFAULT_CONFIG; from tradingagents.portfolio_advisor.core_discovery import run_core_discovery; run_core_discovery(DEFAULT_CONFIG.copy())"
```

Or keep the Hermes cron but point it at the server via SSH.

## To do after server recovery

1. Run cold + warm tests as above
2. Verify cache files created: `ls ~/.tradingagents/cache/fundamentals/ | wc -l`
3. Reinstall Saturday crontab on server
4. Full 516-ticker cold run to measure real wall clock time
5. Confirm Telegram delivery
