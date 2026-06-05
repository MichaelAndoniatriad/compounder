# Portfolio Advisor — Vision & Roadmap

## Where we are

The PM is a Telegram-based advisory system that watches a live eToro portfolio
and makes recommendations. It detects macro events, remembers patterns,
extracts rules, and scores risk. It does not execute trades.

| Component | Status |
|---|---|
| Market event detection | ✅ Working — 5 events in 4 days |
| Pattern memory (90-day window) | ✅ Injected into every PM cycle |
| Macro rule extraction | ✅ Weekly, with invalidation checks |
| Risk scoring (0-10) | ✅ Computed every cycle, fed into prompt |
| Pre-event alerts | ✅ PM warns when new events match learned rules |
| EP catalyst scanning | ✅ Pre-market + post-close pipeline |
| Position monitoring (watchdog) | ✅ Detects changes, triggers PM |
| Decision recording | ✅ No re-nagging settled calls |
| Rule invalidation | ✅ Weekly audit of existing rules |
| Sizing guidance | ✅ Risk-score-driven, adjusts by % |
| Portfolio optimization | ❌ None — no correlation, volatility, or risk budget |
| Probabilistic reasoning | ❌ LLM judgment only, no EV calculations |
| Performance attribution | ❌ No measurement of whether advice improved returns |
| Backtesting | ❌ Rules adopted from single observations |
| Automated execution | ❌ Advisory only by design |
| EP trade journal | 🟡 Built but empty — 0 open trades |

## Where we're going

The PM should be a **probabilistic portfolio advisor** that balances rules
with evidence, gets measurably better over time, and can defend every
recommendation with data.

The final product has four pillars:

### Pillar 1 — Pattern recognition (built)
The system watches the portfolio and the market. It detects when things move,
identifies the cause, and remembers what happened. This pillar is 80% done.

### Pillar 2 — Probabilistic reasoning (not started)
Every recommendation should carry a confidence level and an expected value.
"Reduce entries by 50%" should be backed by: "at the current VIX level and
tariff event frequency, historical patterns suggest a 65% probability of a
further 3% drawdown within 5 sessions. Expected cost of doing nothing: $X.
Expected benefit of reducing: $Y."

This requires:
- A statistical layer alongside the LLM
- Historical probability distributions for macro patterns
- Expected value calculations for each recommendation
- Confidence intervals, not just assertions

### Pillar 3 — Performance measurement (not started)
The system must know if its advice is good. Every macro rule, every EP
recommendation, every sizing adjustment must be tracked to outcome.

This requires:
- Forward-testing: log every recommendation, then measure what actually happened
- Performance attribution: which rules added alpha, which cost money
- Automatic rule retirement: rules that fail twice are flagged, three times are deleted
- A quarterly "system audit" showing advice quality vs benchmark

### Pillar 4 — Portfolio optimization (not started)
Position sizing should be mathematical, not intuitive. The system should
compute correlation, concentration, volatility, and risk contribution.

This requires:
- A correlation matrix of holdings
- Volatility-weighted position sizing
- A risk budget with explicit limits
- Rebalancing recommendations based on drift from targets
- Kelly-based sizing for catalyst trades instead of fixed 1%

## The roadmap

### Phase 1 — Solidify what exists (June 2026)

Fix the basics before building anything new.

- [ ] Deduplicate macro rule extraction (same rule written twice)
- [ ] Clean up 37 stale proposals — auto-close any for tickers not held
- [ ] Fix the learned rules pipeline — it stopped May 16, needs to run on
      action-check cycles, not just rare full_graph runs
- [ ] Add rule quality scoring — rules from 1 observation get a "low confidence"
      tag, rules confirmed by 3+ events get "high confidence"
- [ ] Get the EP sleeve active — the pipeline runs but hasn't found a setup.
      Audit: are the gates too tight?

### Phase 2 — Measure what we advise (July 2026)

Close the feedback loop.

- [ ] **Recommendation log**: every PM recommendation is logged with timestamp,
      ticker, action, rationale, and confidence. Append-only JSONL, same pattern
      as market events.
- [ ] **Outcome tracking**: a weekly job that scans the recommendation log,
      fetches actual prices, and computes: was the advice correct? Did following
      it save money or cost money?
- [ ] **Rule performance dashboard**: which macro rules have a positive track
      record? Which are unproven? Which have been contradicted?
- [ ] **Automatic rule retirement**: any rule that costs money twice in a row
      is flagged for human review. Any rule contradicted three times is deleted.

### Phase 3 — Add probability (August 2026)

Move from "the PM thinks" to "the evidence suggests."

- [ ] **Statistical layer**: for each macro pattern (tariff escalation, Fed
      relief rally, sector rotation), compute historical probability
      distributions. "After a tariff-driven -3%+ SPY day, the market is higher
      5 sessions later X% of the time over the last Y occurrences."
- [ ] **Expected value on every recommendation**: "Recommended action has an
      expected value of +$X based on historical pattern frequency and magnitude."
- [ ] **Confidence calibration**: track whether the PM's confidence levels
      match actual outcomes. If the PM says "80% confident" and is right 55%
      of the time, recalibrate.
- [ ] **Bayesian updating**: as new events occur, update the probability
      distributions. The PM's confidence in "bounces come in 1-2 sessions"
      should strengthen or weaken with each new observation.

### Phase 4 — Optimize the portfolio (September 2026)

Make sizing mathematical.

- [ ] **Correlation matrix**: compute pairwise correlations between all holdings.
      Flag when two "diversified" positions have 0.85 correlation.
- [ ] **Volatility targeting**: adjust position sizes so each holding contributes
      roughly equal risk, accounting for correlation.
- [ ] **Risk budget**: define explicit limits. Max portfolio volatility: 25%
      annualized. Max single-position risk contribution: 15%. Max sector
      concentration: 40%.
- [ ] **Kelly-based EP sizing**: replace fixed 1% risk with Kelly criterion
      scaled to the EP strategy's historical win rate and average R-multiple.
      If EP has a 40% win rate and 3.5R average winner, Kelly says bet ~17%
      of bankroll — scale down to quarter-Kelly (4.25%) for safety.
- [ ] **Rebalancing alerts**: when a sleeve drifts beyond tolerance, PM
      recommends specific share quantities to rebalance.

### Phase 5 — Forward-test and validate (October 2026+)

Prove the system works before giving it execution authority.

- [ ] **Paper portfolio**: run a parallel simulated portfolio that follows
      every PM recommendation. Compare returns against the actual portfolio
      (where the human decides).
- [ ] **90-day audit**: after 90 days of paper trading, publish a report:
      advisory alpha vs benchmark, win rate by recommendation type, rule
      performance, calibration score.
- [ ] **Execution gate**: if the paper portfolio outperforms by a statistically
      significant margin over 90 days, the human may grant limited execution
      authority (stop-loss exits only, then full EP trades, then full
      rebalancing).

## Success metrics

The system is working when:

| Metric | Target |
|---|---|
| Market events detected | >80% of days with a macro move |
| Rules with performance data | 100% after 30 days of Phase 2 |
| Rule accuracy (direction correct) | >60% after 50 observations |
| PM confidence calibration | Within 10% of actual outcome rate |
| EP recommendations per week | >0 (the sleeve should not stay empty) |
| Recommendation log completeness | 100% of PM advice is tracked |
| Advisory alpha (Phase 5) | Positive Sharpe above benchmark |

## Design principles

1. **The LLM is the strategist, not the calculator.** The LLM identifies
   patterns and crafts rules. Statistical models compute probabilities and
   sizes. Neither replaces the other.

2. **Every claim must be measurable.** "Reduce entries by 50%" must be
   traceable to a specific rule with a track record. "This looks like a
   tariff rout" must be verifiable against historical tariff events.

3. **Rules earn confidence, they don't inherit it.** A rule from one
   observation starts at low confidence. Confidence increases as events
   confirm it. Rules that fail retract or die.

4. **The human is the execution layer, but the system should be auditable.**
   When you override a PM recommendation, that's data. The system should
   track: what the PM said, what you did, and what happened. Over time,
   this answers: does the human add or subtract value vs following the PM
   blindly?

5. **Cost should be proportional to value.** The system spends ~$2-3/day
   on API calls. As it gets better, the spend should correlate with
   portfolio size and recommendation quality. A $10,000 portfolio justifies
   $3/day. A $100,000 portfolio justifies more compute.

## Immediate next actions

1. Deduplicate macro rules
2. Clean stale proposals
3. Fix learned rules pipeline (run on action-check)
4. Add confidence tags to all rules
5. Audit EP pipeline gates
6. Create recommendation log schema

---

*Version 1.0 — June 5, 2026*
*Author: Michael Andonia + Hermes*
