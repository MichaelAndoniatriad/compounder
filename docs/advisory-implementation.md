# Portfolio Advisor — Implementation Guide

Build order, technical decisions, and error-proofing for each phase of the
Advisory Vision. This document assumes the reader has access to the full
codebase and understands the WAT framework (Workflows, Agents, Tools).

---

## Phase 1 — Solidify (June 2026)

### 1.1 Deduplicate macro rules

**Problem:** `run_macro_learning_review()` writes the same rule multiple times
because the LLM rephrases an existing rule when no new pattern exists.

**Fix:** Before appending, compare the LLM output against existing rules using
a cheap similarity check.

```python
# In macro_learning.py, add before _append_rules call:

def _is_duplicate(new_text: str, existing_rules: str) -> bool:
    """Check if new_text substantially overlaps any existing rule."""
    if not existing_rules:
        return False
    # Extract key phrases from new_text (3+ word sequences)
    new_words = set(new_text.lower().split())
    # Split existing rules into individual rules
    for rule in existing_rules.split("\n### "):
        rule_words = set(rule.lower().split())
        if not rule_words:
            continue
        overlap = len(new_words & rule_words) / max(len(new_words), 1)
        if overlap > 0.6:  # 60% word overlap = duplicate
            return True
    return False
```

**Error-proofing:**
- The overlap threshold (0.6) should be configurable. Start at 0.6 and tune down
  if legitimate new rules get blocked.
- Log every dedup decision so we can audit false positives.
- If the LLM returns NO_RULES but the similarity check shows no overlap, it
  means the LLM found nothing new — that's correct, log and skip.

### 1.2 Clean stale proposals

**Problem:** 37 pending proposals in `proposed_trades.jsonl`. Many are for
tickers no longer held, or are weeks old with no action.

**Fix:** Two changes:

1. **Auto-close on reconcile:** `reconcile_with_portfolio` already exists but
   only handles "reduce" side proposals. Extend it to close ANY proposal for
   a ticker not held, regardless of side.

```python
# In proposals.py, reconcile_with_portfolio:
# Change the status check from:
#   and _side(r.get("action")) == "reduce"
# To:
#   (no side filter — close ALL proposals for tickers not held)
```

2. **Staleness timeout:** Add an `auto_close_stale()` function that cancels
   any "proposed" entry older than 14 days with no status change.

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
- Never auto-close proposals less than 7 days old. The human might be on
  holiday or waiting for a price level.
- The PM must NOT be able to auto-close proposals. This is a system-level
  cron job, not a PM tool.
- Log every auto-close with proposal ID and ticker so it's reversible.

### 1.3 Fix the learned rules pipeline

**Problem:** `learned_rules.md` has 14 entries from May 13-16, then nothing.
The reflection pipeline that generates these only runs on `full_graph` runs,
which are capped at 2/ticker/14d and gated by a $5/day budget.

**Fix:** Move learned-rule extraction from the `full_graph` path to the
`action-check` path. The action-check already runs the PM cycle and logs
market events. Add outcome resolution for any pending position decisions.

```python
# In advisor_pm.py, in the action-check flow (run_pm_cycle with trigger="action_check"):
# After the PM cycle completes, resolve any pending memory-log entries
# that now have price data available, and extract learned rules.

# Pseudocode:
# 1. Load pending entries from memory log
# 2. For each, fetch current price vs decision date
# 3. If outcome is resolvable (5+ sessions since decision):
#    a. Compute raw return and alpha
#    b. If learned_rules_enabled, call maybe_extend_learned_rules_from_outcome
#    c. Mark entry as resolved
```

**Error-proofing:**
- Only resolve entries where at least 5 trading sessions have passed
  (enough time for a drift to materialise).
- Skip entries where price data is unavailable (delisted, too recent).
- Never resolve the same entry twice — use a `resolved_at` timestamp check.
- Rate-limit: max 3 resolutions per action-check cycle to control cost.

### 1.4 Add confidence tags to all rules

**Problem:** Rules are adopted with equal weight regardless of evidence
quality. A rule from one observation carries the same authority as a rule
confirmed by five.

**Fix:** Tag every rule with a confidence level based on supporting evidence.

```python
def _rule_confidence(rule_text: str, events: List[Dict]) -> str:
    """Return 'low' | 'medium' | 'high' based on evidence count."""
    # Count how many events match this rule's pattern
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

Append the confidence tag to the rule:

```
- **2026-06-04** [confidence: low, 2 supporting events]: When 5+ holdings...
```

**Error-proofing:**
- Confidence is displayed in PM prompts so the PM weights rules appropriately.
- Low-confidence rules should be phrased as observations, not commands.
  The PM prompt should say: "Low-confidence rules are patterns seen 1-2 times.
  Apply them cautiously. High-confidence rules have 5+ confirmations."
- Recompute confidence on every macro review. A rule can graduate from low
  to high as evidence accumulates.

### 1.5 Audit EP pipeline gates

**Problem:** 0 EP trades in 4 days. Either no good catalysts, or the gates
are too tight.

**Fix:** Add logging to every gate in `ep_scanner.py` that records WHY each
candidate was rejected.

```python
# In scan_for_ep_candidates(), for each skipped entry:
# Instead of just appending to the skipped list, log at INFO level:
logger.info("EP skip: %s — %s", tk, reason)
```

Then after a week, review the EP scan logs:
- How many news items were pulled?
- How many ticker hits?
- How many passed the price/gap gates?
- How many passed the extended-run check?
- How many survived to PM classification?
- Of those, how many did the PM classify as Tier 1/2?

If 200 news items → 30 ticker hits → 3 passed gates → 0 classified as Tier 1/2,
the gates are fine but the market isn't producing qualifying catalysts.

If 200 → 30 → 15 passed gates → 0 classified, the PM is being too strict or
the EP strategy doc's Tier 1/2 criteria are too narrow for current market
conditions.

---

## Phase 2 — Measure (July 2026)

### 2.1 Recommendation log

**Schema:**

```json
{
  "id": "uuid",
  "ts": "ISO8601",
  "trigger": "action_check | ep_scan | watchdog | human_query",
  "type": "sizing | ep_entry | ep_exit | stop_adjust | macro_alert | rebalance",
  "ticker": "AAPL or null for portfolio-level",
  "action": "reduce_entries_50pct | buy_100_shares | raise_stop_to_50 | hold | exit",
  "rationale": "Macro risk 6/10, tariff escalation pattern...",
  "confidence": "high | medium | low",
  "rule_ref": "macro_rule_2026_06_04 or null",
  "expected_outcome": "Avoid ~$200 drawdown if selloff continues",
  "status": "pending | accepted | rejected | expired",
  "human_response": "accepted: reduced EP size | rejected: staying full size | null",
  "outcome_measured_at": null,
  "actual_outcome": null
}
```

**Implementation:** New module `recommendation_log.py` with append-only JSONL.
Same pattern as `market_memory.py` and `proposals.py`.

**Error-proofing:**
- Every `messaging.send_advisor_message()` call must log to the recommendation
  log BEFORE sending. If the log write fails, don't send (the message would
  be untracked).
- The PM tools (`emit_ep_candidate`, `propose_trade`, `log_market_event`)
  must include a recommendation log entry.
- The log is append-only. Never update an entry in place — write a new entry
  with a `supersedes` field referencing the old ID.

### 2.2 Outcome tracking

**Weekly cron job (Sunday):**

1. Load all pending recommendations from the log
2. For each, compute what actually happened:
   - Sizing recommendation: did the market move in the predicted direction?
   - EP entry recommendation: if entered at next open, what's the P&L now?
   - Stop adjustment: was the new stop breached?
   - Macro alert: did the pattern play out as predicted?
3. Write outcome to the recommendation log entry
4. Update rule performance counters

```python
def measure_outcome(rec: Dict, current_prices: Dict[str, float]) -> Dict:
    """Return {was_correct: bool, pnl_impact: float, note: str}"""
    # If human rejected the recommendation, outcome is "not applicable"
    if rec["status"] == "rejected":
        return {"was_correct": None, "pnl_impact": 0.0, "note": "human rejected"}

    # For sizing recommendations: compare market move vs prediction
    if rec["type"] == "sizing":
        spy_change = current_prices.get("SPY", 0)
        if "selloff continues" in rec.get("expected_outcome", ""):
            was_correct = spy_change < -0.02  # SPY dropped >2%
        # ... etc

    # For EP entries: compute P&L from recommended entry to current
    if rec["type"] == "ep_entry":
        entry = rec.get("entry_price")
        current = current_prices.get(rec["ticker"])
        if entry and current:
            pnl_pct = (current - entry) / entry
            rec["actual_outcome"] = f"pnl={pnl_pct:+.1%}"
```

**Error-proofing:**
- Only measure outcomes after enough time has passed (5 sessions for sizing,
  10 sessions for EP entries).
- Skip outcomes where price data is unavailable.
- Never claim a recommendation was "correct" if the human didn't follow it.
- Log measurement failures separately so we can debug data issues.

### 2.3 Rule performance dashboard

Not a visual dashboard yet — a text block injected into the PM prompt that shows:

```
Rule performance (last 90 days):
  macro/2026-06-04 "don't sell into tariff panics"
    Applied: 2 times | Correct: 2 | Accuracy: 100% | P&L impact: ~$300 avoided loss
  position/NVDA/2026-05-13 "enforce thesis-break metrics"
    Applied: 0 times | No data
  position/DDOG/2026-05-15 "allow partial entries on strength"
    Applied: 1 time | Correct: 1 | P&L impact: +$85
```

Implementation: `rule_performance.py` that cross-references the recommendation
log with the rules files.

**Error-proofing:**
- "Applied" means the PM used this rule to make a recommendation AND the
  human accepted it.
- "Correct" means the outcome matched the predicted direction.
- "P&L impact" is best-effort — flag when it's an estimate vs a precise
  calculation.

### 2.4 Automatic rule retirement

```python
def retire_bad_rules(cfg: Dict[str, Any]) -> List[str]:
    """Scan rule performance. Flag rules that should be retired."""
    retired = []
    for rule_id, perf in load_rule_performance(cfg).items():
        if perf["applied"] >= 3 and perf["accuracy"] < 0.4:
            # Rule has been wrong more than right over 3+ applications
            _flag_for_retirement(cfg, rule_id, perf)
            retired.append(rule_id)
        elif perf["applied"] >= 5 and perf["accuracy"] < 0.5:
            # Rule is no better than a coin flip after 5 applications
            _flag_for_retirement(cfg, rule_id, perf)
            retired.append(rule_id)
    return retired
```

**Error-proofing:**
- Never auto-delete. Flag with ⚠ RETIRE marker in the rules file. Human
  reviews quarterly.
- A rule that hasn't been applied enough times (min 3) is marked "unproven,"
  not retired.
- Log every retirement decision with the rule text and performance stats.

---

## Phase 3 — Probability (August 2026)

### 3.1 Statistical layer

**Architecture:** A new `statistics.py` module that sits alongside the LLM.
The LLM identifies patterns; statistics computes probabilities.

**Data source:** The market events log is already structured. Each event has:
date, category, cause, market_move, magnitude, portfolio_impact.

**Implementation:**

```python
def pattern_probability(
    events: List[Dict],
    pattern: str,  # e.g. "tariff_escalation"
    outcome: str,  # e.g. "bull" (bounce within 5 sessions)
    lookback_sessions: int = 5,
) -> Dict[str, Any]:
    """Given a pattern, compute P(outcome) and confidence interval."""
    matches = [
        e for e in events
        if pattern in str(e.get("cause", "")).lower()
        or pattern in ",".join(e.get("pattern_tags", []))
    ]
    if len(matches) < 2:
        return {"probability": None, "confidence": "insufficient data", "n": len(matches)}

    # For each match, check if the outcome occurred within lookback_sessions
    outcomes = []
    for match in matches:
        match_date = match["date"]
        # Find the next N sessions of events
        future_events = [
            e for e in events
            if e["date"] > match_date
            and _sessions_between(match_date, e["date"]) <= lookback_sessions
        ]
        had_outcome = any(
            e.get("market_move") == outcome for e in future_events
        )
        outcomes.append(had_outcome)

    p = sum(outcomes) / len(outcomes)
    # Wilson score interval for small samples
    from statistics import _wilson_score
    ci_low, ci_high = _wilson_score(p, len(outcomes))

    return {
        "probability": round(p, 3),
        "ci_low": round(ci_low, 3),
        "ci_high": round(ci_high, 3),
        "n": len(outcomes),
        "confidence": "high" if len(outcomes) >= 5 else "medium" if len(outcomes) >= 3 else "low",
    }
```

**Error-proofing:**
- Never return a probability with n < 3. Say "insufficient data."
- Use Wilson score interval, not the normal approximation, for small samples.
- The probability block in the PM prompt must show n. "P(bounce) = 0.67 (n=3)"
  is very different from "P(bounce) = 0.67 (n=30)."
- Store computed probabilities in a cache file. Recompute only when new events
  are added. Don't burn API credits on math.

### 3.2 Expected value on recommendations

Every PM tool that makes a recommendation must compute EV before sending.

```python
def expected_value(
    action: str,       # "reduce_entries_50pct"
    current_state: Dict,  # VIX, SPY trend, event frequency
    prob_dist: Dict,   # from statistics.py
    portfolio_value: float,
) -> Dict[str, Any]:
    """Compute expected $ impact of following this recommendation."""
    # Example: "reduce entries by 50%"
    # If we reduce and the selloff continues: save X
    # If we reduce and the market rallies: miss out on Y
    # EV = P(selloff) * X - P(rally) * Y

    p_selloff = prob_dist.get("bear_continuation", 0.5)
    p_rally = 1.0 - p_selloff

    # Assume reducing halves exposure on ~20% of portfolio that would be new entries
    exposed_capital = portfolio_value * 0.20
    avg_drawdown_if_wrong = exposed_capital * 0.05  # 5% typical macro drawdown
    avg_missed_gain_if_wrong = exposed_capital * 0.03  # 3% typical relief rally

    ev = (p_selloff * avg_drawdown_if_wrong) - (p_rally * avg_missed_gain_if_wrong)

    return {
        "expected_value_usd": round(ev, 2),
        "scenario_win": f"save ~${avg_drawdown_if_wrong:.0f} if selloff continues",
        "scenario_loss": f"miss ~${avg_missed_gain_if_wrong:.0f} if market rallies",
        "probability_win": round(p_selloff, 2),
    }
```

**Error-proofing:**
- Always show both scenarios and their probabilities. Never just "EV = +$X."
- When probabilities are from small samples (n < 5), state that explicitly.
- The EV calculation must use the PORTFOLIO VALUE, not a fixed number. It must
  pull live eToro data.

### 3.3 Confidence calibration

Track whether the PM's stated confidence matches reality.

```python
def calibration_score(recommendations: List[Dict]) -> Dict:
    """Compute: when PM said X% confident, was it right X% of the time?"""
    buckets = {"low": [], "medium": [], "high": []}
    for rec in recommendations:
        if rec.get("outcome_measured_at"):
            conf = rec.get("confidence", "medium")
            correct = rec.get("was_correct")
            if correct is not None:
                buckets[conf].append(correct)

    return {
        conf: {
            "accuracy": sum(bucket) / len(bucket) if bucket else None,
            "n": len(bucket),
        }
        for conf, bucket in buckets.items()
    }
```

**Error-proofing:**
- Only include recommendations the human ACCEPTED and that have measured outcomes.
- If the PM is miscalibrated (says "high confidence" but is right 55% of the
  time), inject a calibration warning into the PM prompt: "Your high-confidence
  recommendations have been correct 55% of the time. Calibrate accordingly."

### 3.4 Bayesian updating

```python
def update_rule_confidence(rule: Dict, new_event: Dict, was_correct: bool):
    """Update a rule's confidence using Bayes."""
    # Prior: current P(rule is valid) based on historical accuracy
    prior = rule.get("accuracy", 0.5)
    # Likelihood: P(observe this outcome | rule is valid)
    # If rule predicted bounce and bounce happened, likelihood is high
    likelihood = 0.8 if was_correct else 0.2
    # Marginal: P(observe this outcome)
    marginal = likelihood * prior + (1 - likelihood) * (1 - prior)
    # Posterior
    posterior = (likelihood * prior) / marginal if marginal > 0 else prior

    rule["accuracy"] = round(posterior, 3)
    rule["n_events"] = rule.get("n_events", 0) + 1
    rule["last_updated"] = datetime.now(timezone.utc).isoformat()
    return rule
```

---

## Phase 4 — Optimize (September 2026)

### 4.1 Correlation matrix

```python
def compute_correlation_matrix(
    tickers: List[str],
    lookback_days: int = 60,
) -> Dict[str, Dict[str, float]]:
    """Compute pairwise correlations between all holdings."""
    import yfinance as yf
    import numpy as np

    # Fetch daily returns for all tickers
    returns = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period=f"{lookback_days}d")
            returns[t] = hist["Close"].pct_change().dropna()
        except Exception:
            continue

    # Build matrix
    tickers = [t for t in tickers if t in returns]
    matrix = {}
    for t1 in tickers:
        matrix[t1] = {}
        for t2 in tickers:
            if t1 == t2:
                matrix[t1][t2] = 1.0
            else:
                corr = returns[t1].corr(returns[t2])
                matrix[t1][t2] = round(float(corr), 3)

    return matrix
```

**Error-proofing:**
- Require at least 20 data points for a correlation estimate.
- Flag pairs with correlation > 0.80 as "effectively the same position."
- Recompute weekly. Use yfinance's auto_adjust=False for accurate prices.

### 4.2 Risk budget

```python
def compute_risk_contributions(
    positions: List[Dict],  # from etoro_scan
    correlation_matrix: Dict,
) -> Dict:
    """Compute each position's contribution to portfolio risk."""
    weights = {}
    total_value = sum(p["market_value"] for p in positions)
    for p in positions:
        weights[p["ticker"]] = p["market_value"] / total_value

    # Portfolio variance = w' * Σ * w
    # Individual risk contribution = w_i * (Σw)_i / portfolio_volatility
    # (simplified — see Meucci, "Risk and Asset Allocation")
    ...
```

**Error-proofing:**
- Always use market value, not cost basis, for weight calculations.
- Recompute after any position change (watchdog fires on new/closed positions).
- Inject risk contribution into the PM prompt: "NVDA contributes 18% of
  portfolio risk despite being 9% of capital. Consider reducing."

### 4.3 Kelly-based EP sizing

```python
def kelly_fraction(win_rate: float, avg_win_r: float, avg_loss_r: float) -> float:
    """Kelly criterion for a binary outcome strategy."""
    if win_rate <= 0 or avg_win_r <= 0 or avg_loss_r <= 0:
        return 0.0
    b = avg_win_r / avg_loss_r  # odds ratio
    f = (win_rate * b - (1 - win_rate)) / b
    return max(0.0, min(f, 0.25))  # Cap at quarter-Kelly for safety

# Usage in emit_ep_candidate:
# Instead of fixed 1% risk:
# ep_stats = load_ep_trade_stats(cfg)  # from journal
# kelly = kelly_fraction(ep_stats["win_rate"], ep_stats["avg_winner_r"], ep_stats["avg_loser_r"])
# risk_pct = kelly * 0.25  # quarter-Kelly
```

**Error-proofing:**
- Require at least 25 closed EP trades before using Kelly. Below that, use
  fixed 1% as fallback.
- Never allow Kelly to exceed 5% risk per trade (quarter-Kelly on a 40% win
  rate / 3.5R strategy gives ~4.3%, which is acceptable).
- If win rate drops below 25%, Kelly goes to zero — the PM should recommend
  pausing the strategy, not sizing to zero.

---

## Phase 5 — Validate (October 2026+)

### 5.1 Paper portfolio

```python
# New module: paper_portfolio.py
# Tracks a simulated portfolio that follows every PM recommendation exactly.

class PaperPortfolio:
    def __init__(self, initial_cash: float):
        self.cash = initial_cash
        self.positions: Dict[str, Dict] = {}  # ticker -> {shares, avg_price}
        self.trades: List[Dict] = []
        self.start_date = datetime.now(timezone.utc).isoformat()

    def execute_recommendation(self, rec: Dict, current_price: float):
        """Execute a recommendation in the paper portfolio at current_price."""
        # Buy: add position at current price
        # Sell: close position at current price
        # Stop adjustment: update stop level
        # Debit fees (0.1% per trade as proxy for spread + commission)
        ...

    def compute_returns(self) -> Dict:
        """Compare paper portfolio returns vs actual portfolio."""
        ...
```

**Error-proofing:**
- Include realistic friction: 0.1% per trade, no fractional shares below 0.01,
  no short selling.
- Track divergence: where did the paper portfolio execute but the human didn't?
  This measures the "human override P&L."
- The paper portfolio starts with the SAME positions as the actual portfolio
  on day 1. It doesn't start from cash.

### 5.2 90-day audit

A scheduled job that runs at the end of the 90-day paper trading period and
produces a markdown report.

**Sections:**
1. Performance: paper vs actual vs SPY, Sharpe, max drawdown
2. Recommendation quality: accuracy by type, calibration score
3. Rule performance: best and worst rules by P&L impact
4. Human override analysis: did overrides add or subtract value?
5. Cost analysis: API spend vs advisory alpha
6. Recommendation: proceed to execution? Which authorities to grant?

### 5.3 Execution gate

Conditions for granting execution authority:

1. Paper portfolio Sharpe > 0.8 over 90 days
2. Recommendation accuracy > 60% for accepted recommendations
3. No single recommendation lost >2% of portfolio
4. Human override P&L is not significantly negative (you didn't save yourself
   by rejecting good advice)
5. The PM has at least 50 measured recommendations

Start with stop-loss exits only (lowest risk). After 30 days of successful
stop-loss execution, escalate to full EP entries. After 60 days, full
rebalancing authority.

---

## Cross-cutting concerns

### Error handling philosophy

1. **Fail open for detection, fail closed for action.** If the market event
   detector fails, log a warning and skip — the PM can still function. If the
   trade executor fails, halt and alert — never silently miss an exit.

2. **Every write is append-only.** Market events, recommendations, outcomes,
   rules — all append-only JSONL. Never update in place. This means every
   state change is auditable and reversible.

3. **LLM failures are expected.** Every LLM call has a deterministic fallback.
   `_fallback_risk_score()` in macro_learning.py is the pattern. Apply it
   everywhere: if the LLM fails to classify a catalyst, use keyword matching.
   If it fails to compute EV, use historical averages.

4. **Cost must be bounded.** Every new LLM call adds to the daily budget.
   Batch where possible (combine multiple checks into one prompt). Use the
   cheap model (v4-flash) for classification, expensive model (v4-pro) for
   synthesis.

### Testing checklist

For each new module:
- [ ] Import works in the server's venv
- [ ] Dry-run with empty data (no events, no positions)
- [ ] Dry-run with edge case data (one position, one event)
- [ ] LLM call has a fallback that doesn't crash
- [ ] File writes are atomic (write to temp, rename)
- [ ] Lock file prevents concurrent writes
- [ ] Log at INFO for normal operation, WARNING for degraded, ERROR for broken

---

*Version 1.0 — June 5, 2026*
