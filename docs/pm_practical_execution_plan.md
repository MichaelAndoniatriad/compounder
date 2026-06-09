# PM Practical Execution Plan

**Date:** 6 June 2026
**Owner:** Michael Andonia
**Status:** Live
**Mode:** One section = one Hermes goal command. Sequential. Practical, not research.

---

## Purpose

The portfolio advisor exists. The scaffolding works. The catalyst sleeve has not produced a trade. Rules are not being extracted. Outcomes are not being measured. The system runs but does not improve.

This plan fixes that. Each section is a self contained Hermes goal command. Total estimated cost: under $20 across all 8 sections. Order matters: earlier sections unblock later ones.

No new research. No new theory. No new scoping. Execute the existing advisory roadmap.

---

## Order of operations

| # | Section | Why this order | Est. Hermes cost |
|---|---------|----------------|-------------------|
| 1 | Dead man's switch | Server already died once. Detect the next time before days pass. | < $1 |
| 2 | Macro rule dedupe + stale proposal cleanup | Cheap noise reduction. PM prompts get cleaner immediately. | < $1 |
| 3 | Learned rules pipeline fix | Broken since 16 May. No new rules entered the system in 3 weeks. | < $2 |
| 4 | Rule confidence tags + retirement | Rules earn weight from observation, lose weight from failure. | < $2 |
| 5 | EP gate audit + loosen | Catalyst sleeve is empty. No new trades = no new outcomes = no learning. | < $3 |
| 6 | Recommendation log verification + tagging | Confirm log writes on every PM cycle. Add outcome attribution fields. | < $2 |
| 7 | Outcome tracking weekly job | Close the loop. Match recommendations to realised returns. | < $3 |
| 8 | Performance dashboard | Visibility so decisions are data driven, not gut driven. | < $2 |

Total est. budget: under $16.

---

## Section 1 — Dead man's switch

**Goal:** External monitor on the Mac that alerts via Telegram if the Hetzner server is unreachable for more than 30 minutes.

**Why:** The server died, we did not know for hours. The internal alerting cannot warn us about itself going dark.

**Scope:**
- New script `~/bin/ping_compounder.sh` on the Mac
- Cron entry every 5 minutes
- Sends Telegram alert if 6 consecutive failures (30 minutes)
- Sends recovery alert when server comes back

**Goal command for Hermes:**

```
Build a Mac side dead man's switch for the Hetzner trading server.

Server IP: 116.203.153.58 (verify current IP via Hetzner API if changed)
Telegram bot token: read from existing TradingAgents config or .env on the Mac (do NOT hardcode)
Telegram chat ID: same source

Deliverables:
1. ~/bin/ping_compounder.sh — bash script that pings the server with timeout 5 seconds, increments a failure counter in /tmp/compounder_health, sends Telegram alert at exactly 6 consecutive failures, sends recovery alert when reachable again after being down
2. Cron entry: */5 * * * * /Users/michaelandonia/bin/ping_compounder.sh
3. State file: /tmp/compounder_health.json with {last_check, consecutive_failures, last_alert_sent, status}
4. Brief one page doc at ~/Documents/Cowork OS/Research/AI Retail Trading Microstructure/server_deadmans_switch.md describing what was set up

Constraints:
- No paid services
- Use existing Telegram bot credentials from the trading-agents .env
- Single bash script under 100 lines
- Tested by simulating a failure (set IP to localhost:1, confirm alert fires)
- British English, no em / en / hyphen dashes in new prose

Stop conditions: cannot find Telegram credentials (report and stop); cron not installable (report; user installs manually)
```

**Success criteria:** Cron is running. Simulated outage triggers a Telegram alert within 30 minutes. Recovery alert fires on restore.

**Dependencies:** Server must be reachable to test, but the script works offline.

---

## Section 2 — Macro rule dedupe + stale proposal cleanup

**Goal:** Cheap noise reduction. Remove duplicates from learned rules. Auto close proposals for tickers not held.

**Why:** Per `advisory-vision.md` Phase 1 immediate actions 1 and 2. 37 stale proposals exist. Duplicate rules clutter the PM prompt.

**Scope:**
- Add a dedupe pass over the macro rules file
- Auto close proposals for tickers no longer in the eToro portfolio
- Commit and ship

**Goal command for Hermes:**

```
Clean up the portfolio advisor noise per improvement_plan.md and advisory-vision.md Phase 1 immediate actions 1 and 2.

Tasks:
1. Locate the macro rules file (likely tradingagents/portfolio_advisor/rule_book.py output or ~/.tradingagents/portfolio_advisor/macro_rules.json on the server). Dedupe by canonical rule text. Keep the most recent observation timestamp per rule. Log how many duplicates were removed.
2. Locate the proposals file (likely ~/.tradingagents/portfolio_advisor/proposals.jsonl). For each proposal, check if the ticker is currently in the eToro portfolio (via existing etoro_scan dataflow). If not, mark proposal status as "auto_closed_no_position". Log count.
3. Commit changes to the trading-agents repo if any code changes were made (script files only, not data files).

Constraints:
- Read state via existing modules; do not invent new data paths
- Branch: pm-cleanup-2026-06-06 off main
- Do NOT merge; push branch and report
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: rules file not found in expected location after 2 attempts (report and stop)

Deliverable: a single status report at docs/pm_cleanup_report.md with rule count before/after, proposal count before/after, and any unexpected findings.
```

**Success criteria:** Macro rules count drops by N (Hermes reports N). Stale proposals count drops to zero.

**Dependencies:** Server reachable. Section 1 can run in parallel.

---

## Section 3 — Learned rules pipeline fix

**Goal:** Restart automatic rule extraction. Per `advisory-vision.md` action 3: broken since 16 May.

**Why:** Rules stopped being extracted when the pipeline regressed to running only on rare full_graph cycles. Every action_check cycle should extract.

**Scope:**
- Find where the learned rules extraction is invoked
- Add invocation to the action_check cycle in advisor_pm.py
- Backfill rules from market events since 16 May

**Goal command for Hermes:**

```
Restore the learned rules extraction pipeline per advisory-vision.md action 3.

Background: rule extraction stopped on 16 May 2026. The advisory-vision doc says "needs to run on action-check cycles, not just rare full_graph runs."

Tasks:
1. Find where learned rule extraction is currently invoked. Likely in graph runs of full_graph but not in advisor_pm.py action checks.
2. Add an extract_learned_rules() call to the action_check cycle in advisor_pm.py at the appropriate point (after market event processing, before PM prompt construction).
3. Backfill: run the extraction over market events from 16 May 2026 to current date. Write any rules found into the rule book.
4. Verify by inspecting the rule book before and after.

Constraints:
- Branch: rules-pipeline-fix off main
- Do NOT merge; push and report
- Coverage target: small test that confirms extract_learned_rules() is called in action_check
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: extraction function not findable in the codebase after grep (report and stop)

Deliverable: report at docs/rules_pipeline_fix_report.md with: where extraction was missing, what was added, how many rules backfilled, and a one line summary of the most interesting new rule.
```

**Success criteria:** Rule book gains N rules from backfill. Next action_check cycle adds at least one new rule. Test confirms the wiring.

**Dependencies:** Section 2 makes the rule book cleaner first.

---

## Section 4 — Rule confidence tags and auto retirement

**Goal:** Each rule earns confidence from observations and loses it from failures. Rules contradicted three times get deleted automatically.

**Why:** Per `advisory-vision.md` Phase 1 action 4 and Phase 2 outcome tracking. Confidence is the prerequisite for probabilistic reasoning later.

**Scope:**
- Add confidence fields to each rule (observations, failures, status)
- Update extraction to assign initial confidence: low (1 observation), medium (2 to 3), high (4+)
- Add a check function that runs weekly: if a rule has been contradicted twice, flag for review; three times, mark retired and exclude from PM prompt

**Goal command for Hermes:**

```
Add rule confidence tags and automatic retirement per advisory-vision.md Phase 1 action 4 and Phase 2.

Tasks:
1. Extend the rule schema with: observations_count, failures_count, confidence ("low", "medium", "high"), status ("active", "review", "retired"), last_validated_date.
2. On extraction (Section 3 work), set initial fields: observations_count = 1 if new, confidence = "low".
3. Add validate_rule_against_event() to the action_check cycle: when a market event occurs, for each currently active rule, check whether the event confirms (increment observations_count) or contradicts (increment failures_count). Recompute confidence: high if observations >= 4 and failures == 0; medium if observations >= 2 and failures <= 1; low otherwise.
4. Add a weekly job (extend existing cron, no new infra) called retire_failed_rules() that scans active rules; status -> "review" at 2 contradictions; status -> "retired" at 3.
5. PM prompt construction reads only status == "active" rules. Retired rules stay in the rule book for audit but do not enter the prompt.

Constraints:
- Branch: rule-confidence off main
- Do NOT merge; push and report
- Tests: unit tests for the validate_rule_against_event() state machine
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: rule book schema cannot be migrated cleanly (report; do not corrupt existing rule file)

Deliverable: report at docs/rule_confidence_report.md with the new schema, counts of rules at each confidence tier after backfill, and any rules that ended up at "review" or "retired" status on first pass.
```

**Success criteria:** Every rule has a confidence tag. Weekly retirement job exists. PM prompt drops any retired rules.

**Dependencies:** Sections 2 and 3.

---

## Section 5 — EP gate audit and loosen

**Goal:** Make the catalyst sleeve actually produce trades.

**Why:** Per `advisory-vision.md` Phase 1 action 5. Without catalyst trades, no new outcomes, no learning, no improvement. This is the unblock.

**Scope:**
- Audit which EP gates are blocking candidates
- Loosen the binding constraint
- Aim for at least 1 to 2 catalyst trades per month

**Goal command for Hermes:**

```
Audit the EP (catalyst sleeve) pipeline gates. The sleeve has not produced a trade. Find why and loosen the binding constraint.

Tasks:
1. Locate the EP scanner in tradingagents/portfolio_advisor/ep_scanner.py and the candidate gates in candidates.py.
2. Pull the last 90 days of EP scan attempts (logs or state). For each candidate that was screened, record which gate it failed at: portfolio_fit, policy, liquidity, thesis, catalyst, or another.
3. Tabulate: what fraction of candidates fail at each gate. Identify the gate with the highest failure rate.
4. For the binding gate, propose a specific threshold loosening based on what the failed candidates actually looked like (eg if liquidity threshold is $250K ADV and 70% of candidates have $100K to $250K, propose lowering to $150K).
5. Apply the loosening as a configurable threshold (do NOT hardcode; add to default_config.py with the current value as the default).
6. Backtest the change against the 90 day log: how many additional candidates would have passed?
7. Do NOT auto enable the loosened threshold in production. Push the branch with the change and a report explaining the analysis.

Constraints:
- Branch: ep-gate-audit off main
- Do NOT merge; push and report
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: EP scan log is empty (report; suggest enabling logging and re running in a week)

Deliverable: report at docs/ep_gate_audit.md with the failure rate table, the proposed loosening, expected impact, and a final recommendation.
```

**Success criteria:** Audit identifies the binding gate. Proposes a defensible threshold change. Estimates the impact on trade count.

**Dependencies:** Server must be online with EP scan logs accessible.

---

## Section 6 — Recommendation log verification and tagging

**Goal:** Confirm the recommendation log writes on every PM cycle, not just on full_graph deep runs. Add outcome attribution fields.

**Why:** Per `advisory-vision.md` Phase 2 the recommendation log is the basis for the learning loop. If it only logs deep runs, the sample is sparse.

**Scope:**
- Verify current logging behaviour
- Extend to log every PM recommendation regardless of cycle type
- Add fields needed for Section 7 outcome attribution

**Goal command for Hermes:**

```
Verify and extend the recommendation log so every PM recommendation lands in the log with the fields needed for outcome attribution.

Tasks:
1. Inspect recommendation_log.py. Identify every code path that writes to it. Document which cycle types currently log (full_graph yes, action_check unknown, weekly_summary unknown, single_model_analysis unknown).
2. If any PM recommendation code path skips the log, add the append call.
3. Extend the log entry schema with:
   - cycle_type (full_graph / action_check / weekly_summary / single_model_analysis)
   - confidence_self_report (PM's self assessed confidence 0 to 1, if present)
   - thesis_break_metrics (the 2 to 3 conditions that would invalidate)
   - exit_horizon_days (expected holding period from the recommendation)
   - peer_holdings_at_decision (snapshot of portfolio at decision time)
4. Backward compatibility: old entries without new fields still parse. Use None defaults.
5. Tests: append a synthetic recommendation, query it back, confirm fields present.

Constraints:
- Branch: recommendation-log-extension off main
- Do NOT merge; push and report
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: recommendation_log.py is missing or non writable (report and stop)

Deliverable: report at docs/recommendation_log_audit.md with the audit findings (which cycles log, which do not), the schema change, and a sample log entry from the new format.
```

**Success criteria:** Every PM cycle that produces a recommendation appends to the log. New fields present on every new entry.

**Dependencies:** Server reachable. Sections 3 and 4 should be done first so logs include rule confidence context.

---

## Section 7 — Outcome tracking weekly job

**Goal:** Match each logged recommendation to its realised outcome. Compute return delta, classify as good / bad / neutral advice.

**Why:** Per `advisory-vision.md` Phase 2. Without this, the system cannot tell whether its advice helped or hurt.

**Scope:**
- New weekly job that scans the recommendation log
- For each recommendation older than the exit_horizon, fetch realised return
- Append outcome record linked to recommendation id
- Surface aggregate stats in PM prompt context

**Goal command for Hermes:**

```
Build the outcome tracking weekly job per advisory-vision.md Phase 2.

Tasks:
1. New module tradingagents/portfolio_advisor/outcome_tracker.py.
2. Function compute_recommendation_outcomes() that:
   - Reads the recommendation log
   - For each recommendation where (today - recommendation_date) >= exit_horizon_days
   - Fetches the realised return for the recommended ticker between recommendation_date and exit_horizon
   - Classifies as good (>5% return), bad (<-5%), neutral (between)
   - Writes to outcomes.jsonl with fields: recommendation_id, ticker, recommendation_date, exit_date, realised_return, classification
3. Function rule_performance_summary() that:
   - Joins outcomes to the rules that contributed to each recommendation (rules cited in the rationale field)
   - Computes per rule: total uses, % good, % bad, total return contribution
4. Cron entry: weekly, Sunday 06:00 UTC
5. New PM prompt context block: top 5 rules by performance, bottom 5. One line each.
6. Tests: synthetic recommendation log + price fetch mock, confirm classification logic.

Constraints:
- Branch: outcome-tracking off main
- Do NOT merge; push and report
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: recommendation log has fewer than 5 entries at job runtime (report; job continues but produces empty summary)

Deliverable: report at docs/outcome_tracking_report.md with the schema, sample outcome record, and current rule performance summary if any data exists.
```

**Success criteria:** Weekly cron exists. Outcomes file is being written. Rule performance summary appears in PM prompt.

**Dependencies:** Section 6 must be done. Sections 3 and 4 should be done so rule attribution works.

---

## Section 8 — Performance dashboard

**Goal:** Single page view of how the system is doing. Open it in a browser, read in 30 seconds.

**Why:** Visibility. Right now the only way to know the system's health is to SSH in and read jsonl files. That is friction. Friction means it does not get checked.

**Scope:**
- Static HTML dashboard
- Reads from local cache (Mac side mirror of server state, fetched daily)
- Shows: portfolio value over time, recommendation log volume, outcomes by classification, top rules, recent macro events
- Lives on the Mac, opens via file://

**Goal command for Hermes:**

```
Build a static HTML performance dashboard for the portfolio advisor.

Tasks:
1. New script ~/bin/sync_compounder_state.sh that rsyncs the trading server's portfolio_advisor state directory to ~/local/compounder_state/ daily. Cron at 04:00 local time.
2. New dashboard generator scripts/generate_dashboard.py that reads ~/local/compounder_state/ and produces ~/local/compounder_dashboard.html. Single page. Pure HTML and inline CSS. No JS frameworks. Chart.js from CDN if charts are needed.
3. Sections of the dashboard:
   - Top: server status, last successful PM cycle, portfolio NAV
   - Section A: recommendation log volume by week (bar chart)
   - Section B: outcomes by classification (pie chart: good / bad / neutral / pending)
   - Section C: top 5 rules by performance, bottom 5
   - Section D: recent macro events (last 10, table)
   - Section E: open positions with thesis status (table)
4. Cron entry to regenerate the dashboard daily after the sync at 04:15 local time.
5. README at ~/local/compounder_dashboard.README.md explaining how to open and what each section means.

Constraints:
- Pure HTML + inline CSS + Chart.js CDN. No build tools.
- No paid services
- British English, no em / en / hyphen dashes in new prose
- Stop conditions: rsync fails 3 times (report; sync deferred)

Deliverable: working dashboard at ~/local/compounder_dashboard.html plus a one paragraph report at docs/dashboard_report.md describing what was built.
```

**Success criteria:** Dashboard opens. Shows real data. Refreshes daily.

**Dependencies:** Sections 6 and 7 must be done so there is data to display.

---

## Reference table

| Section | Branch | Deliverable doc | Hard cost cap |
|---------|--------|-----------------|----------------|
| 1 | n/a (Mac side) | server_deadmans_switch.md | $1 |
| 2 | pm-cleanup-2026-06-06 | pm_cleanup_report.md | $1 |
| 3 | rules-pipeline-fix | rules_pipeline_fix_report.md | $2 |
| 4 | rule-confidence | rule_confidence_report.md | $2 |
| 5 | ep-gate-audit | ep_gate_audit.md | $3 |
| 6 | recommendation-log-extension | recommendation_log_audit.md | $2 |
| 7 | outcome-tracking | outcome_tracking_report.md | $3 |
| 8 | n/a (Mac side) | dashboard_report.md | $2 |

Total cost cap: $16. Hard budget: $20.

---

## How to use this plan

Send one section's goal command block to Hermes. Wait for completion. Review the report. Move to the next section. If a section fails, the next section can usually still proceed (only Section 7 depends on 6 being done first, and only Section 8 depends on 6+7).

If a section blows past its cost cap, stop and investigate before continuing.

---

## What this plan deliberately excludes

- Consensus factor backtest (parked; the scaffolding is built and tagging passively; revisit when 30+ real trades exist)
- LLM consensus daily polls (deferred; Section 8 dashboard will surface when worth revisiting)
- Survey on AI influence rate (academic project, not operations)
- TAQ data access (academic project)
- Robinhood Q2 monitoring infrastructure (manual; calendar reminder is enough)
- Any new defensive guardrails beyond what is already on `consensus-guardrails-v1`

---

**End of plan.**
