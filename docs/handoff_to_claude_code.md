# Handoff to Claude Code — Compounder Project

**Date:** 8 June 2026
**Author:** Cowork OS session
**Reader:** Claude Code (CLI), next agent picking this up

## Context in one paragraph

This is the trading-agents portfolio advisor ("compounder") on a Hetzner server. Built originally on the open source TauricResearch/TradingAgents multi agent LLM framework. The system runs a PM cycle on cron, generates trade proposals, and is meant to surface them to the human via Telegram. The session that produced this handoff spent roughly 24 hours building out the missing pieces of the learning loop (recommendation log, outcome tracker, rule retirement, dashboard) and fixing two pipelines that were silently returning zero candidates (EP scanner and core discovery). The compounder logic is now sound. The current blocker is operational: the Hetzner VM has been down for 24+ hours after a second SSH brute force takedown in 48 hours.

## Two parallel projects

### A. Trading Agents repo (~/workspace/trading-agents)

The actual compounder. This is what needs to come back online.

### B. Research project (~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/)

A separate academic-style research thread on AI usage in retail trading and its market microstructure impact. Phase 0 done (scoping, Track B LLM concentration audit, broker disclosure deep dive, working thesis at ~5,000 words, survey design at n=2,000 on Prolific). Phase 1 (empirical) is queued. Not on the critical path for operations.

## What was built in this session on the trading-agents side

### Code (all on branches, none merged to main)

| Branch | What it contains |
|--------|------------------|
| `rec-log-bridge` | Bridges propose_trade calls into recommendation_log so backfilled trades exist to measure. Plus the recommendation log schema extension (8 new optional fields including consensus tagging). |
| `ep-gate-audit` | Per-scan EP scanner instrumentation, Yahoo Finance RSS adapter as a second news source, widened hint regex without false positives, EP gate audit report. |
| `discovery-pipeline-fixes` | Core discovery scoring screen (replaces 4-hard-AND filters), ROE fallback when yfinance lacks returnOnCapital. **Latest commit:** AV migration code (alpha_vantage_fundamentals_cached with 7-day disk cache, core_discovery swapped off yfinance). |
| `consensus-guardrails-v1` | The v4 consensus factor scoring infrastructure (feature flag CONSENSUS_FACTOR_LIVE defaults false). |

### Modules added or extended

- `tradingagents/portfolio_advisor/recommendation_log.py` — extended schema, `load_due_for_measurement()`
- `tradingagents/portfolio_advisor/outcome_tracker.py` — new, weekly outcome measurement
- `tradingagents/portfolio_advisor/rule_book.py` — extended with `auto_retire_failed_rules()` + `recently_retired_block()`
- `tradingagents/portfolio_advisor/core_discovery.py` — scoring screen (was 4-hard-AND), AV-backed quant data
- `tradingagents/portfolio_advisor/ep_scanner.py` — per-candidate gate logging, multi-source news, widened regex
- `tradingagents/dataflows/yahoo_news_rss.py` — new, Alpha Vantage shape compatible
- `tradingagents/dataflows/alpha_vantage_fundamentals_cached.py` — new, 7-day disk cache for AV OVERVIEW endpoint
- `tradingagents/portfolio_advisor/consensus_score.py` — composite consensus factor scoring

### Scripts added on the Mac side

- `scripts/mac-watchdog.sh` — external reachability monitor (5 minute cron, alerts via macOS notification and Telegram)
- `scripts/sync_compounder_state.sh` — daily rsync of `~/.tradingagents/` from server to `~/local/compounder_state/`
- `scripts/generate_dashboard.py` — static HTML dashboard generator (Chart.js, single file, runs after sync)

### Crons installed

- **Mac:** mac-watchdog every 5 minutes, sync at 04:00, dashboard at 04:15
- **Server (before it went down):** outcome tracker Sunday 06:00 UTC, rule retirement Sunday 07:00 UTC, core discovery Saturday 10:00 UTC

## Current state at handoff

- **Server 116.203.153.58 is DOWN.** Rescue mode boots but networking does not come up. Likely Hetzner null routing the IP after sustained brute force attacks. Will not recover via API.
- **Hetzner support ticket** open (raised in the previous turn of the conversation).
- **All code is pushed** to GitHub on the branches listed above. Nothing local-only that matters.
- **Mac local cache** at `~/local/compounder_state/` may contain the last good snapshot of `~/.tradingagents/portfolio_advisor/`. Confirm with `ls -la ~/local/compounder_state/portfolio_advisor/`.

## What needs to happen next (in order)

1. **Decide: rebuild or wait on Hetzner support.**
   - Rebuild is faster. New CX31 (8 GB RAM) in a different datacenter (Helsinki) with a cloud-init script that pre-hardens: SSH on port 33893, fail2ban active, key-only auth from first boot.
   - Hetzner support might unblock the existing server in hours or days — useful but not the critical path.

2. **Restore state** from `~/local/compounder_state/portfolio_advisor/` if it exists. Rsync to the new server's `/root/.tradingagents/portfolio_advisor/`.

3. **Update IP and port everywhere on the Mac:**
   - `~/.ssh/config`
   - `scripts/mac-watchdog.sh` (MAC_WATCHDOG_HOST)
   - `scripts/sync_compounder_state.sh` (COMPOUNDER_HOST, port 33893)
   - Any deploy scripts

4. **Verify AV migration on the new server.** The discovery-pipeline-fixes branch has untested code that swaps core_discovery off yfinance and onto Alpha Vantage with disk cache. Cold and warm runs were never executed because the server died.
   ```bash
   ssh -p 33893 root@NEW_IP "cd /opt/tradingagents && .venv/bin/python -c \
     'from tradingagents.portfolio_advisor.core_discovery import run_core_discovery; \
      from tradingagents.default_config import DEFAULT_CONFIG; \
      print(run_core_discovery(DEFAULT_CONFIG.copy()))'"
   ```

5. **Reinstall the server crons** for outcome tracker, rule retirement, core discovery Saturday 10:00 UTC.

6. **Long-term: consider switching off Hetzner.** Two outages in 48 hours suggest the IP class is heavily attacked. DigitalOcean, Vultr, Linode have cleaner cheap-tier IPs. Not urgent but worth filing.

## Open issues at time of handoff

- AV migration code on `discovery-pipeline-fixes` was never verified end-to-end (server died first). Cold run + warm run on 24 ticker subset is the validation. Document: `docs/discovery_av_migration.md` has the recipe.
- `docs/server_outage_2026-06-08.md` should be updated with whatever Hetzner support says + root cause once accessible.
- Telegram pipeline has never fired in production. Two paths can produce messages: pre-event macro alert (gated on accumulated learned rules, currently zero) and EP recommendation (now possible after the regex widening + Yahoo RSS, but server has to be up to run scans).

## Key files to read on entry

In order:
1. `docs/pm_practical_execution_plan.md` — the 8 section plan we executed against
2. `docs/handoff_to_claude_code.md` — this file
3. `docs/discovery_av_migration.md` — the AV switch, what was done, what remains to verify
4. `docs/server_outage_2026-06-08.md` — current crisis
5. `docs/advisory-vision.md` — long-term roadmap for the advisor (4 pillar plan, written earlier than this session)
6. `docs/improvement_plan.md` — pre-existing tech debt list (some items now done)

## What NOT to spend time on

- The consensus guardrails plan (v3 to v4) — that's a parked research thread, scaffolding exists behind feature flag, not on critical path.
- The cheap backtest of the consensus factor — done, returned null, scrap.
- Any further survey design work — the research project is on a separate budget and timeline.
- Rebuilding core_discovery from scratch — the current scoring math is correct, verified on real data (ALGN 0.590 and ABBV 0.550 cleared the 0.55 threshold on a 30 ticker live sample).

## Estimated time to resume operations

About 1 hour of focused Claude Code work once the user is ready to drive: 20 minutes to provision and harden the new server, 5 minutes to restore state, 15 minutes to update IP everywhere and reinstall crons, 20 minutes to verify the AV migration end-to-end. Then the Saturday cron fires automatically and the system is live.

---

**End of handoff.**
