# PM Practical Plan — Remaining Hermes Goal Commands

**Context:** Sections 1, 4, 6, 7 completed locally (no server needed). Sections 2, 3, 5, 8 require server access or end-to-end verification. Hand these to Hermes one at a time after the server is back.

---

## Section 2 — Macro rule dedupe + stale proposal cleanup

Server access required.

```
Clean up the portfolio advisor noise per improvement_plan.md and advisory-vision.md Phase 1 immediate actions 1 and 2.

Tasks:
1. Locate the PM rules file via tradingagents/portfolio_advisor/rule_book.py (function _rules_path). On the production server it is at ~/.tradingagents/portfolio_advisor/PM_RULES.md. Read it. Dedupe by exact rule name. When duplicates exist, keep the entry with the most recent Updated date; sum the Confirmed and Violated counters across duplicates. Log how many duplicates were removed.

2. Locate the proposals file (likely ~/.tradingagents/portfolio_advisor/proposals.jsonl). For each proposal with status "pending" or "candidate", check whether the ticker is currently in the eToro portfolio via the existing etoro_scan dataflow. If not, set status to "auto_closed_no_position" and add note "auto closed: ticker not in portfolio". Log count.

3. After both passes, call tradingagents.portfolio_advisor.rule_book.auto_retire_failed_rules(cfg) once (the function was added in section 4). Surface the list of names it retired.

Constraints:
- Branch: pm-cleanup-2026-06-06 off main
- Do NOT merge; push and report
- British English. No em / en / hyphen dashes in new prose.
- Stop conditions: rules file not at expected path after 2 attempts (report and stop); proposals file not present (treat as zero entries, proceed)

Deliverable: report at docs/pm_cleanup_report.md with: rules before/after count, proposals before/after count, list of names auto retired, and any unexpected findings.
```

---

## Section 3 — Learned rules pipeline fix

Server access required to verify the fix end to end.

```
Restore the learned rules extraction pipeline per advisory-vision.md action 3.

Background: rule extraction stopped on 16 May 2026. The doc says it needs to run on action-check cycles, not just rare full_graph runs.

Tasks:
1. Find every call site of any function whose name matches r"(extract|learn).*rule" in tradingagents/. Identify which function is the canonical rule extractor.
2. Find the action_check cycle entry in tradingagents/portfolio_advisor/advisor_pm.py. The function name likely contains "action_check" or "run_action_check".
3. After existing market event processing within action_check, before the PM prompt is built, add a call to the canonical rule extractor with a try/except wrapper that logs failures at WARNING level (per the silent-failure policy from improvement_plan.md item 1.2).
4. Backfill: write a one-shot script scripts/backfill_learned_rules.py that runs the extractor over market events from 2026-05-16 to today. Idempotent (must check existing rule names to avoid duplicates). Run it once.
5. Verify: inspect PM_RULES.md before and after. Count of rules should increase. Log the new rule names.
6. Add a one-line unit test in tests/test_advisor_pm_action_check.py that mocks the extractor and asserts it is called during action_check.

Constraints:
- Branch: rules-pipeline-fix off main
- Do NOT merge; push and report
- British English. No em / en / hyphen dashes in new prose.
- Stop conditions: cannot identify the extractor function after 3 grep variations (report and stop); cannot locate action_check entry in advisor_pm.py (report and stop)

Deliverable: report at docs/rules_pipeline_fix_report.md with: extractor function name and path, where the call was added in advisor_pm.py, backfill count, the most interesting new rule name, and any unexpected findings.
```

---

## Section 5 — EP gate audit

Server access required for the scan log.

```
Audit the EP (catalyst sleeve) pipeline gates per advisory-vision.md Phase 1 action 5. The sleeve has not produced a trade. Find why and propose a loosening.

Tasks:
1. Locate the EP scanner in tradingagents/portfolio_advisor/ep_scanner.py and the candidate gates in candidates.py.
2. Find where EP scan attempts are logged. Likely the event_log under event_type containing "ep_scan" or similar. If no log exists, instrument the scanner to log every attempt with the candidate ticker and the failure gate before doing anything else.
3. Pull the last 90 days of EP scan attempts. For each candidate, record which gate it failed at: portfolio_fit, policy, liquidity, thesis, catalyst, or another. Build a table: gate name vs failure count vs sample candidate tickers.
4. Identify the highest failure gate. Inspect the actual failed candidates: are they failing because the threshold is too tight, or because the candidates themselves are weak?
5. Propose one specific threshold change with empirical justification (eg "lower min_avg_daily_volume from 250000 to 150000 because 65% of failed candidates had ADV in the 100K to 250K range and at least 30% of those names had a dated catalyst").
6. Add the new threshold to tradingagents/default_config.py with the CURRENT value as the default (no behaviour change). Document the proposed value alongside.
7. Do NOT enable the loosened threshold automatically. Report the proposal and let the human decide.

Constraints:
- Branch: ep-gate-audit off main
- Do NOT merge; push and report
- British English. No em / en / hyphen dashes in new prose.
- Stop conditions: no EP scan log data after 14 days back (report; recommend enabling logging and waiting a week)

Deliverable: report at docs/ep_gate_audit.md with the gate failure table, sample failed candidates per gate, the proposed loosening with rationale, expected impact (how many additional candidates would pass), and a final recommendation.
```

---

## Section 8 — Performance dashboard

Server access required for daily rsync of state.

```
Build a static HTML performance dashboard for the portfolio advisor.

Tasks:
1. Create scripts/sync_compounder_state.sh on the Mac that rsyncs the trading server's ~/.tradingagents/portfolio_advisor/ directory to ~/local/compounder_state/ daily. Cron at 04:00 local time. Use rsync over SSH, key based auth.
2. Create scripts/generate_dashboard.py that reads ~/local/compounder_state/ and produces ~/local/compounder_dashboard.html. Single page. Pure HTML and inline CSS. Chart.js from CDN only (no other JS libraries, no build tools).
3. Dashboard sections:
   - Top: server reachability (read /tmp/mac-watchdog.state.json from section 1), last successful PM cycle timestamp, portfolio NAV
   - Section A: recommendation log volume by week (Chart.js bar chart, last 13 weeks)
   - Section B: outcomes by classification (Chart.js pie: good / bad / neutral / pending)
   - Section C: top 5 rules by performance, bottom 5 (use outcome_tracker.rule_performance_summary)
   - Section D: recent macro events (last 10, table)
   - Section E: open positions with thesis status (table)
4. Cron entry on the Mac to regenerate the dashboard daily at 04:15 local time (after the rsync).
5. Brief README at ~/local/compounder_dashboard.README.md explaining how to open and what each section means.

Constraints:
- Pure HTML + inline CSS + Chart.js CDN. No build tools.
- No paid services.
- British English. No em / en / hyphen dashes in new prose.
- Stop conditions: rsync fails 3 attempts (report; defer dashboard generation); local cache empty (generate dashboard with placeholders and a warning banner)

Deliverable: working dashboard at ~/local/compounder_dashboard.html (or a placeholder version if state not yet synced) plus a short note at docs/dashboard_report.md describing what was built and how to extend it.
```

---

## How to use

Run each block as a single goal command, one at a time. Wait for the report before moving to the next. If any section blows past $3 in compute, stop and inspect.

## Status so far

| Section | Status | Where |
|---------|--------|-------|
| 1 | DONE | `scripts/mac-watchdog.sh` + README |
| 2 | Pending Hermes | This file |
| 3 | Pending Hermes | This file |
| 4 | DONE | `rule_book.py` (auto_retire_failed_rules + recently_retired_block) + tests |
| 5 | Pending Hermes | This file |
| 6 | DONE | `recommendation_log.py` extended + tests |
| 7 | DONE | `outcome_tracker.py` new + tests |
| 8 | Pending Hermes | This file |

Half the plan is built and tested. Remaining four sections need server data or end-to-end runs Hermes can do once the server is back.
