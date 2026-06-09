# Compounder Dashboard

Static HTML performance view of the portfolio advisor. Reads a local Mac cache of the server state (synced daily via rsync) and writes a single HTML file. No build step, no JS framework, only Chart.js from CDN.

## Install

```bash
# 1. Make scripts executable
chmod +x ~/workspace/trading-agents/scripts/sync_compounder_state.sh
chmod +x ~/workspace/trading-agents/scripts/generate_dashboard.py

# 2. Ensure your SSH key for the server exists at ~/.ssh/trading-server.key
ls -la ~/.ssh/trading-server.key

# 3. First time: do a manual sync to confirm rsync works
~/workspace/trading-agents/scripts/sync_compounder_state.sh

# 4. Add the cron entries (one for sync, one for dashboard regeneration)
( crontab -l 2>/dev/null
  echo "0 4 * * * /Users/michaelandonia/workspace/trading-agents/scripts/sync_compounder_state.sh"
  echo "15 4 * * * /usr/bin/python3 /Users/michaelandonia/workspace/trading-agents/scripts/generate_dashboard.py"
) | crontab -

# 5. Open the dashboard
open ~/local/compounder_dashboard.html
```

## What you see

- **Server status:** reachability from the Mac watchdog state, last PM cycle, paper portfolio NAV
- **Recommendation log volume:** weekly count over the last 13 weeks (bar chart)
- **Outcomes:** good / bad / neutral / pending (pie chart). Empty until the outcome tracker has run.
- **Top 5 rules:** by mean outcome score
- **Bottom 5 rules:** by mean outcome score (only if you have more than 5 rules)
- **Recent macro events:** last 10
- **Open positions:** from paper portfolio

The dashboard renders gracefully with missing data. Empty sections show "no data yet" placeholders.

## Configuration via env vars

| Variable | Default |
|----------|---------|
| `COMPOUNDER_USER` | `root` |
| `COMPOUNDER_HOST` | `116.203.153.58` |
| `COMPOUNDER_KEY` | `~/.ssh/trading-server.key` |
| `COMPOUNDER_REMOTE` | `~/.tradingagents/portfolio_advisor/` |
| `COMPOUNDER_LOCAL` | `~/local/compounder_state/` |
| `COMPOUNDER_DASHBOARD` | `~/local/compounder_dashboard.html` |
| `COMPOUNDER_WATCHDOG_STATE` | `/tmp/mac-watchdog.state.json` |

## Test the generator with synthetic data

```bash
mkdir -p /tmp/test_state
echo '{"id":"a1","ts":"2026-06-01T10:00:00+00:00","ticker":"NVDA","action":"buy","rule_ref":"r1","exit_horizon_days":30}' > /tmp/test_state/recommendation_log.jsonl
echo '{"cash": 1000, "positions": [{"ticker":"NVDA","shares":10,"entry_price":900,"current_price":1000}]}' > /tmp/test_state/paper_portfolio.json
COMPOUNDER_STATE_DIR=/tmp/test_state \
  COMPOUNDER_DASHBOARD=/tmp/test.html \
  python3 ~/workspace/trading-agents/scripts/generate_dashboard.py
open /tmp/test.html
```

## Limits

- Mac only (uses `open`, `~/Library/Logs`, default cron). Linux works with path tweaks.
- Single dashboard per Mac. If you have multiple servers, run multiple sync + generator pairs with different env vars.
- Refreshes daily. For real time, run the generator on demand: `python3 scripts/generate_dashboard.py`.
- Dashboard is local file; no auth. Do not host on a public web server without protection.

## Uninstall

```bash
( crontab -l 2>/dev/null | grep -v compounder | grep -v generate_dashboard ) | crontab -
rm -rf ~/local/compounder_state ~/local/compounder_dashboard.html
```
