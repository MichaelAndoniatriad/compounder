# Core Position Discovery — Build Plan

**Date:** 7 June 2026
**Status:** Draft for review

## Goal

Every Saturday morning, the system screens the entire US equity market for long-term growth candidates, applies three filtering layers (two mechanical, one LLM), and sends the top 5 to Telegram with ranked theses.

## Architecture

Three layers, in sequence. Each layer narrows the pool. Only survivors pass to the next.

### Layer 1 — Mechanical disqualification (fast)

Eliminates stocks that definitively cannot qualify. Uses yfinance `fast_info` — one request per ticker, minimal payload. No LLM.

| Gate | Threshold | Reason |
|---|---|---|
| Price | Below $5 | Manipulation risk, poor liquidity |
| Market cap | Below $500M | Too small for a long-term hold |
| Earnings | Negative trailing EPS | PEG requires positive earnings |
| Debt/equity | Above 5x | Balance sheet risk |
| Volume | Below 500K avg daily | Cannot enter or exit cleanly |
| Instrument type | Not common equity | ETFs, warrants, preferred shares excluded |

Output: survivors list + rejection audit CSV showing every cut with reason and ticker count.

### Layer 2 — Quantitative screen (detailed)

Pulls full `yfinance.ticker.info` on survivors. Applies the pre-buy framework from `investor_policy.py`.

| Gate | Threshold |
|---|---|
| Revenue growth (YoY) | Above 20% |
| PEG ratio | Below 1.5 |
| ROIC | Above 15% |
| Market cap | Above $1B (tighter than Layer 1) |
| Forward PE | Collected for context, not a gate |

Output: survivors with enriched data (sector, industry, forward PE, debt/equity, current price).

### Layer 3 — LLM qualitative rank

Top 30 from Layer 2 go to a single LLM call. The LLM ranks by conviction, assessing:

- Competitive moat (network effects, switching costs, scale)
- Operator quality (founder-led, skin in the game)
- Secular tailwind (not cyclical demand)
- Red flags (cash flow vs GAAP gap, dilution, decelerating growth)

Output: up to 5 tickers, ranked, with a one-sentence investment thesis each.

### Delivery

Telegram message every Saturday at 11:00 UTC:

```
CORE POSITION DISCOVERY
Week of 2026-06-07

Screened 603 tickers (S&P 500 + NASDAQ 100). 487 eliminated by mechanical filters,
27 passed quantitative screen. Top picks after qualitative review:

1. DDOG (Datadog Inc) — Technology | rev_growth=26% | ROIC=18% | PEG=1.2
   DDOG — CONVICTION High — Observability platform with durable switching costs and secular cloud tailwind; founder-led, profitable, no red flags.

2. ...

Reply with a ticker to deep-dive. Say 'skip' to pass.
```

### Universe

Pulled live from Wikipedia every run. No hardcoded lists:

- S&P 500: `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`
- NASDAQ 100: `https://en.wikipedia.org/wiki/Nasdaq-100`

If both fail, falls back to a 24-ticker minimal growth list.

### Files

| File | Purpose |
|---|---|
| `tradingagents/portfolio_advisor/mechanical_filter.py` | Layer 1 |
| `tradingagents/portfolio_advisor/core_discovery.py` | Layers 2 + 3 + orchestration |
| `cli/advisor_cmd.py` | CLI command `advisor portfolio core-scan` |
| `scripts/cron-portfolio-advisor-core-scan.sh` | Cron script |
| Crontab entry | `0 11 * * 6 /opt/tradingagents/scripts/cron-portfolio-advisor-core-scan.sh` |

### Cost

Zero paid APIs. yfinance is free. Wikipedia is free. One LLM call per week (DeepSeek v4-flash, roughly $0.01).

### Edge cases

- Wikipedia fetch fails: fallback to minimal growth list
- Zero tickers pass Layer 1: message "No stocks passed mechanical filters this week"
- Zero tickers pass Layer 2: message with Layer 1 rejection stats for audit
- LLM call fails: fallback to top 5 by ROIC
- Telegram send fails: logged, returns error string for cron log

## Build order

1. Write `mechanical_filter.py` — pure function, testable in isolation
2. Write `core_discovery.py` — orchestrates all three layers
3. Add CLI command
4. Add cron script
5. Deploy to server, test with dry run, add crontab
6. Smoke test: one manual run, verify Telegram delivery

## What this does NOT do

- No paid APIs
- No backtesting
- No execution
- No dedup suppression (merit-only)
- No portfolio rebalancing recommendations
