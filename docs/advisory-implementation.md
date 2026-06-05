# Portfolio Advisor — Implementation Guide

Build order, technical decisions, and error-proofing for each phase of the
Advisory Vision. This document assumes the reader has access to the full
codebase and understands the WAT framework (Workflows, Agents, Tools).

**Effort key:** ⚡ = weekend build (well-scoped, deterministic). 🔬 = research
project (LLM-dependent, requires iteration). 🚫 = deferred — not in 2026 scope.

---

## Phase 1 — Solidify (June 2026) ⚡

### 1.1 Deduplicate macro rules

**Problem:** `run_macro_learning_review()` writes the same rule multiple times
because the LLM rephrases an existing rule when no new pattern exists.

**Fix:** Have the LLM assign a stable rule-ID to each extracted rule. Dedup on
the ID, not on bag-of-words similarity (which triggers false positives when
two unrelated finance rules share vocabulary).

```python
# In the macro learning prompt, add:
# "For each rule you extract, assign a stable slug ID using this format:
#  RULE_ID: <category>_<YYYY-MM-DD>_<short-hyphenated-description>
#  Example: RULE_ID: macro_2026-06-05_tariff-panic-no-selling
#
#  If a rule ALREADY EXISTS (you see it in the existing rules below), do NOT
#  re-extract it. Instead say 'DUPLICATE: <existing_rule_id>'."

# In macro_learning.py, parse the response for RULE_ID lines.
# Before appending, check if the ID already exists in _portfolio.md.
# If DUPLICATE, skip.
```

**Error-proofing:**
- The LLM is fallible at generating stable IDs — if the ID format doesn't
  match, extract the rule text and log a warning but still write it.
- If two rules have different IDs but the LLM marked one as DUPLICATE,
  trust the LLM's judgment (it read both).
- Log every dedup decision: which ID was skipped because it matched which
  existing ID.

### 1.2 Clean stale proposals

**Problem:** 37 pending proposals in `proposed_trades.jsonl`. Many are for
tickers no longer held, or are weeks old with no action.

**Fix:** Two changes:

1. **Extend reconcile_with_portfolio** to close ALL proposals for tickers
   not held, not just "reduce" side:

```python
# In proposals.py, reconcile_with_portfolio:
# Remove the side filter — close ANY proposal for a ticker not held.
# Before:  and _side(r.get("action")) == "reduce"
# After:   (no side filter)
```

2. **Add auto_close_stale()** for proposals older than 14 days:

```python
def auto_close_stale(cfg: Dict[str, Any], max_age_days: int = 14) -> int:
    """Cancel proposals that have been open too long with no action."""
    rows = load_all(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for r in rows:
        if r.get("status") != "proposed":
            continue
        ts = r.get("ts", "")
        try:
            rt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if rt < cutoff:
                r["status"] = "cancelled"
                r["status_set_at"] = now
                r["status_note"] = f"auto-cancelled: stale >{max_age_days}d"
                n += 1
        except (ValueError, TypeError):
            pass
    if n:
        save_all(cfg, rows)
    return n
```

**Error-proofing:**
- Never auto-close proposals less than 7 days old.
- The PM must NOT have the `auto_close_stale` tool. This is a system-level
  cron job, not a PM capability.
- Log every auto-close with proposal ID and ticker.

### 1.3 Fix the learned rules pipeline

**Problem:** `learned_rules.md` has 14 entries from May 13-16, then nothing.
The reflection pipeline only runs on `full_graph` runs, capped at
2/ticker/14d with a $5/day budget. It's effectively dead.

**Fix:** Move learned-rule extraction to the `action-check` path:

```python
# In advisor_pm.py, in the action-check flow (run_pm_cycle with trigger="action_check"):
# After the PM cycle, resolve pending memory-log entries and extract rules.

# Pseudocode:
# 1. Load pending entries from memory log (ticker-level decisions)
# 2. For each entry where 5+ trading sessions have passed:
#    a. Fetch current price via yfinance
#    b. Compute raw return and alpha vs benchmark
#    c. If learned_rules_enabled, call maybe_extend_learned_rules_from_outcome
#    d. Mark entry as resolved with a timestamp
# 3. Max 3 resolutions per action-check cycle (cost control)
```

**Error-proofing:**
- Only resolve entries with 5+ trading sessions elapsed.
- Skip entries where price data is unavailable.
- Use `resolved_at` timestamp to prevent double-resolution.
- Rate-limit: max 3 per cycle, max 9 per day (3 action-checks × 3).

### 1.4 Add confidence tags — single source of truth

**Problem:** The docs proposed three competing rule-quality systems. Pick one:
**supporting-event count**, because it's deterministic and the PM can weight
it directly without needing performance data that doesn't exist yet.

```python
def _rule_confidence(rule_text: str, events: List[Dict]) -> str:
    """Return 'low' | 'medium' | 'high' based on supporting event count.
    This is the SINGLE source of truth for rule quality until Phase 2
    performance data exists."""
    pattern_keywords = _extract_keywords(rule_text)
    matches = sum(
        1 for e in events
        if any(kw in str(e.get("cause", "")).lower() for kw in pattern_keywords)
    )
    if matches >= 5:
        return "high"
    elif matches >= 3:
        return "medium"
    return "low"
```

Rules display as:
```
- **2026-06-04** [n=2, confidence: low] When 5+ holdings...
```

**Error-proofing:**
- Low-confidence rules (n<3) are displayed with an explicit caveat in the PM
  prompt: "Apply cautiously — limited supporting events."
- After Phase 2, when performance data exists, confidence tags can be upgraded
  to P&L-weighted scores. But n-count remains the fallback.
- The PM sees n directly — it can judge sample size itself.

### 1.5 Audit EP pipeline gates

**Problem:** 0 EP trades in 4 days. Either no good catalysts, or the gates
are too tight.

**Fix:** Add INFO-level logging to every rejection gate in `ep_scanner.py`:

```python
# In scan_for_ep_candidates(), for each skipped entry:
logger.info("EP skip: %s — %s", tk, reason)
```

Review after one week: how many news items → ticker hits → gate passes →
PM classifications → Tier 1/2 calls. The ratios tell us whether gates are
too tight or the market isn't producing qualifying setups.

---

## Phase 2 — Measure (July 2026) ⚡

This is the centerpiece. It works at n=10 and answers the only question
that matters: **is the system helping or hurting?**

### 2.1 Recommendation log

**Schema:**

```json
{
  "id": "uuid",
  "ts": "ISO8601",
  "trigger": "action_check | ep_scan | watchdog | human_query",
  "type": "sizing | ep_entry | ep_exit | stop_adjust | macro_alert",
  "ticker": "AAPL or null for portfolio-level",
  "action": "reduce_entries_50pct | buy_100_shares | raise_stop_to_50 | hold | exit",
  "rationale": "Macro risk 6/10, tariff escalation pattern...",
  "rule_ref": "macro_2026-06-04_tariff-panic-no-selling or null",
  "status": "pending | accepted | rejected | expired",
  "human_response": null,
  "outcome_measured_at": null,
  "was_correct": null,
  "pnl_impact_est": null,
  "outcome_note": null
}
```

**Implementation:** New module `recommendation_log.py`, append-only JSONL.

**Error-proofing:**
- Log BEFORE sending the Telegram message. If the log write fails, don't send.
- Append-only: never update in place. Corrections are new entries with a
  `supersedes` field.
- The human_response field is populated when you reply "accepted" or
  "skipped" via Telegram. The PM updates the log entry.

### 2.2 Outcome tracking

**Weekly cron job (Sunday):**

```python
def measure_outcome(rec: Dict, prices: Dict[str, Optional[float]]) -> Dict:
    """Return {was_correct, pnl_impact_est, note}. Returns None for was_correct
    when data is missing — NEVER defaults to a value that flips the verdict."""
    if rec["status"] == "rejected":
        return {"was_correct": None, "pnl_impact_est": 0.0, "note": "human rejected"}

    if rec["type"] == "sizing":
        spy_change = prices.get("SPY")
        if spy_change is None:
            return {"was_correct": None, "pnl_impact_est": None,
                    "note": "SPY price unavailable — cannot measure"}
        if "selloff" in rec.get("rationale", "").lower():
            was_correct = spy_change < -0.02  # SPY dropped >2%
        # ... etc
```

**Error-proofing:**
- Missing price data returns `None` for was_correct — NEVER a default that
  could flip the verdict. `current_prices.get("SPY", 0)` was a bug:
  if the fetch fails, SPY=0 makes `spy_change < -0.02` true, scoring
  every recommendation "correct."
- Only measure after 5 sessions for sizing, 10 for EP entries.
- Log measurement failures with the reason.

### 2.3 Human-override analysis

The cheapest, highest-value feature. No statistics needed.

```python
def human_override_analysis(recommendations: List[Dict]) -> Dict:
    """Compare outcomes when human followed PM advice vs overrode it."""
    followed = [r for r in recommendations if r["status"] == "accepted"]
    overrode = [r for r in recommendations if r["status"] == "rejected"]

    def avg_pnl(recs):
        vals = [r["pnl_impact_est"] for r in recs if r.get("pnl_impact_est") is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "followed_count": len(followed),
        "followed_avg_pnl": avg_pnl(followed),
        "overrode_count": len(overrode),
        "overrode_avg_pnl": avg_pnl(overrode),
        "verdict": "human adds value" if (avg_pnl(overrode) or 0) > (avg_pnl(followed) or 0)
                   else "PM adds value" if (avg_pnl(followed) or 0) > (avg_pnl(overrode) or 0)
                   else "insufficient data",
    }
```

**Error-proofing:**
- Only compare recommendations with measured outcomes.
- Display both counts. "PM adds value (n=3 followed, n=2 overrode)" is honest.
  "PM adds value, P<0.05" on n=5 is fraudulent.

### 2.4 Rule quality — P&L-based, single source of truth

Replaces Phase 1's event-count confidence with outcome-backed scores:

```python
def compute_rule_performance(rule_id: str, recommendations: List[Dict]) -> Dict:
    """Return {applied, correct, accuracy, total_pnl_impact} for one rule."""
    apps = [r for r in recommendations
            if r.get("rule_ref") == rule_id
            and r["status"] == "accepted"
            and r.get("was_correct") is not None]

    if not apps:
        return {"applied": 0, "correct": 0, "accuracy": None,
                "total_pnl_impact": None, "verdict": "unproven"}

    correct = sum(1 for r in apps if r["was_correct"])
    pnl = sum(r.get("pnl_impact_est", 0) or 0 for r in apps)

    return {
        "applied": len(apps),
        "correct": correct,
        "accuracy": round(correct / len(apps), 2),
        "total_pnl_impact": round(pnl, 2),
        "verdict": "retire" if (len(apps) >= 5 and correct / len(apps) < 0.5)
                   else "active",
    }
```

**Error-proofing:**
- Retirement threshold: 5+ applications AND accuracy < 50%. Not 3 applications
  (too noisy). Not P&L < 0 (a rule can be directionally right but unlucky on
  timing).
- Flagged for retirement, not auto-deleted. Human reviews quarterly.
- Never retire a rule with <5 applications — it's "unproven," not wrong.

---

## Phase 3 — Honest evidence display (Aug 2026+) 🔬

This phase replaces the original "Probability/EV/Bayes" plan with something
the data can actually support. The goal is to show the PM what we know,
what we don't, and how much data backs each claim.

**What we do NOT build:** No expected value calculations. No Bayesian
updating. No Kelly sizing. No probability distributions on n<5. These
create false precision from insufficient data.

**What we DO build:**

### 3.1 Evidence block in PM prompt

```
Macro evidence (last 90 days):
  tariff_escalation events: 3
    → bounce within 5 sessions: 2/3 (67%)
    → continued selloff: 1/3 (33%)
    → INSUFFICIENT DATA (n=3) — do not derive probabilities

  fed_hawkish events: 1
    → INSUFFICIENT DATA (n=1) — no conclusions possible

  sector_rotation events: 0
    → no data

Portfolio impact in tariff events:
  avg portfolio drawdown: -4.2% (range: -11% to +2%)
  avg recovery sessions: 1-2 when bounce occurs
  worst case: Jun 3 tariff rout, NVDA -11%, MNDY -14.4%
```

### 3.2 The "flag, don't calculate" rule

```python
def build_evidence_block(events: List[Dict]) -> str:
    """Build an honest evidence display for the PM prompt."""
    patterns = _group_by_pattern(events)
    lines = ["Macro evidence (last 90 days):"]
    for pattern, matches in patterns.items():
        n = len(matches)
        if n < 3:
            lines.append(f"  {pattern}: INSUFFICIENT DATA (n={n})")
            continue
        # Show raw hit rate, no confidence interval
        bounce_count = sum(1 for m in matches if _had_bounce(m, events))
        rate = bounce_count / n
        lines.append(f"  {pattern} events: {n}")
        lines.append(f"    → bounce within 5 sessions: {bounce_count}/{n} ({rate:.0%})")
        lines.append(f"    → continued selloff: {n-bounce_count}/{n} ({(1-rate):.0%})")
        if n < 10:
            lines.append(f"    → LOW CONFIDENCE (n={n}<10)")
    return "\n".join(lines)
```

### 3.3 Backfill — the highest-leverage move

Rebuild years of macro events from historical price/news data instead of
waiting to accumulate them live at 1-2 events/day.

**Approach:**
1. Pull SPY daily returns for the last 3 years from yfinance
2. Identify days with moves >2% (macro event days)
3. For each event day, pull news headlines from Alpha Vantage or a news API
   for that date range
4. Classify each event using keyword matching (same Tier 1/2 hints as EP scanner)
5. Build a synthetic market_events.jsonl with 200-500 entries
6. Run macro_learning_review against the backfilled dataset to pre-seed rules

**Effort:** 2-3 days of implementation. **Payoff:** transforms Phase 3 from
n=5 to n=200+, making evidence display actually useful.

**Error-proofing:**
- Tag backfilled events as `source: "backfill"` so they're distinguishable
  from live events.
- Backfilled events use historical news, which may have survivorship bias
  (news sources may remove or edit old articles).
- Backfilled rules are pre-seeded at "medium" confidence until confirmed by
  live events.

---

## Phase 4 — Portfolio optimization 🔬

Deferred until Phase 3 evidence exists. Keep the correlation matrix and
risk budget from the original plan — they're deterministic and useful
without probability data. Drop Kelly sizing entirely until we have 25+
closed EP trades.

**What ships now:**
- Correlation matrix (purely mathematical, no data requirements)
- Risk contribution per position (same)
- Concentration flagging (same)

**What's deferred:**
- Kelly-based EP sizing → use fixed 1% until 25+ closed EP trades exist
- Volatility targeting → requires correlation matrix + position data, ships
  with it
- Rebalancing alerts → ships with risk budget

---

## Phase 5 — Validation 🚫

Deferred beyond 2026. 90 days cannot distinguish skill from luck with 50
recommendations across 6 types (~8 per type). A single market regime.

**What stays as concept:**
- Paper portfolio that follows PM recommendations
- Human-override analysis (already in Phase 2 — it works at n=10)
- Quarterly system audit

**What's out of scope:**
- Execution gate based on 90-day performance
- Automated trade execution of any kind
- Statistical significance claims on small samples

The execution gate is the most consequential decision in this document. It
should not rest on 90 days of data. When the system has 12+ months of
measured recommendations across multiple market regimes, revisit.

---

## Cross-cutting concerns

### Error handling philosophy

1. **Fail open for detection, fail closed for action.** Market event detection
   fails → log and skip. Trade execution fails → halt and alert.
2. **Append-only.** Market events, recommendations, outcomes, rules — all
   append-only JSONL. Every state change is auditable.
3. **LLM failures have deterministic fallbacks.** Pattern from
   `_fallback_risk_score()`. Apply everywhere.
4. **Missing data returns None, never a default.** `dict.get(key, 0)` that
   flips a verdict is a bug. `dict.get(key)` → None → skip measurement.

### Testing checklist

For each new module:
- [ ] Import works in the server's venv
- [ ] Dry-run with empty data (no events, no positions)
- [ ] Dry-run with edge case data (one position, one event)
- [ ] LLM call has a fallback that doesn't crash
- [ ] File writes are atomic (write to temp, rename)
- [ ] Lock file prevents concurrent writes
- [ ] Log at INFO for normal, WARNING for degraded, ERROR for broken

---

*Version 2.0 — June 5, 2026*
*Revised per Claude code review: Phase 3 demoted to honest evidence display,
Phase 5 deferred, bugs fixed, single rule-quality source of truth.*
