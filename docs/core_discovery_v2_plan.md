# Core Discovery v2 — De-Herding & Risk-Balanced Screen (Execution Plan)

**Owner:** autonomous agent (hermes) executing on a goal command
**Repo:** `~/workspace/trading-agents` (compounder)
**Status:** ready to implement. **Local-first**: the entire advisor, including the PM cycle, runs on the Mac via `launchd` — the Hetzner server is abandoned. There is no deploy step; code on the working branch (merged to `main`) is what runs. See §8.

---

## 1. Goal & final vision

Make **core discovery** reliably surface *differentiated* long-term compounders — names that clear a hard risk floor but are **not** the same mega-caps every AI screen converges on — while keeping the system honest about risk and profit-first.

This plugs into the project's north star: a **fully autonomous advisor** that
1. continuously discovers both **long-term compounders** (core, this plan) and **event catalysts** (EP scanner, separate),
2. operates strictly **within the user's hard risk gates**,
3. is **tilted away from the AI herd** toward under-followed names,
4. is **profit-first and balanced** (anti-herd is a tilt, never an override),
5. **measures its own picks** and retires what fails (learning loop),
6. surfaces decisions to the human via Telegram while **trade execution stays manual**.

The single guiding principle for this plan:

> **The mechanical gates are the risk boundary and stay hard. Everything else is re-ranking *within* that safe boundary. We do not widen the universe down-cap in this plan.**

---

## 2. Where core discovery sits in the system (ties to the rest)

```
                 ┌─────────────────────────────────────────────┐
   monthly  ───► │  core_discovery.run_core_discovery()         │ ──► CORE sleeve
                 │  universe → mechanical_filter → quant score   │
                 │  → consensus tilt → LLM rank → tiered output  │
                 └───────────────┬─────────────────────────────┘
                                 │ pushes picks
                                 ▼
                 ┌─────────────────────────────────────────────┐
   daily    ───► │  ep_scanner.scan_for_ep_candidates()         │ ──► CATALYST sleeve
                 │  (separate pipeline, momentum/catalyst)       │
                 └───────────────┬─────────────────────────────┘
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │  watchlist.py  (core + catalyst candidates)   │
                 └───────────────┬─────────────────────────────┘
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │  PM cycle (advisor_pm.py)                     │
                 │  reads watchlist + portfolio + evidence       │
                 │  → full_graph deep research on promoted names │
                 │  → sleeve allocation (50 core / 40 cat / 10$) │
                 │  → sizing & entry timing                      │
                 └───────────────┬─────────────────────────────┘
                                 ▼
                 messaging.py (Telegram, quiet hours, dedupe) ──► human (executes manually)
                                 │
                                 ▼
                 Learning loop: recommendation_log → outcome_tracker → rule_book.auto_retire
```

Key integration facts the implementer must preserve:
- Core discovery **feeds the CORE sleeve**; the EP scanner feeds the CATALYST sleeve. They are independent and must stay so.
- Discovery's job is **"what's worth tracking,"** not **"when to buy."** Entry timing lives in the PM layer. Do **not** put price/entry-timing logic in the discovery screen (see §6).
- The **consensus factor module is shared**: discovery consumes the *crowding* components for ranking; the PM consumes the *full* composite (incl. retail flow) for sizing & entry timing. One module, two consumers (see §5 Phase 2 and §7).
- Every pick must be logged via `recommendation_log` so `outcome_tracker` can measure it and `rule_book` can retire heuristics that don't pay — that is how the fragility gate and anti-herd tilt become *measurable* rather than faith-based.

---

## 3. Current state (confirmed) and root problems

Confirmed on `main`:
- AV migration is **already merged**: `core_discovery._quantitative_screen` uses Alpha Vantage OVERVIEW via `alpha_vantage_fundamentals_cached.get_ticker_fundamentals` (7-day disk cache). `mechanical_filter` still uses yfinance.
- Universe = S&P 500 + NASDAQ-100 scraped from Wikipedia (`_build_universe`). All large/mega-cap, all liquid.
- Cadence = **weekly**, Saturday 10:00 UTC, with a within-week pick cache.

Root problems (why it herds toward consensus mega-caps):
1. **`mechanical_filter` hard-gates `negative_eps`** (`mechanical_filter.py` `_check_one`) → kills early high-growth compounders before they're ever scored. Survivors are profitable large-caps = the consensus pool.
2. **PEG scoring zeroes loss-makers** (`_score_quantitative`: `peg=999` → 0 of 0.25 points) → re-sinks the same names.
3. **Generic LLM prompt** ("moat / founder / tailwind") with no anti-herd pressure → converges on the obvious pick.
4. **Funnel starvation**: `_llm_qualitative_rank` returns `[]` unless a pick is `DEEP_DIVE YES`, silently dropping decent names instead of putting them on the radar.
5. **Cadence too high**: these are 5-yr names screened on quarterly-updating data; weekly re-runs identical data (the within-week cache is the tell).
6. **Anti-herd tool exists but is dead**: `consensus_score.py` (branch `consensus-guardrails-v1`) is well-designed as a ±1 tilt, but its `snapshot.json` is **empty** (`top_20: []`) because the scraper (`llm_consensus.py`, same branch) never ran. The "null backtest" that got it parked was scored against no data — **that verdict is void**.

Validation pitfall to avoid repeating: the previous session "validated" the screen because ABBV (0.550) and ALGN (0.590) cleared 0.55. Those are exactly the consensus large-caps the project aims to avoid — clearing the bar is not evidence of de-herding. Acceptance criteria in this plan test for *differentiation*, not just "a plausible name passed."

---

## 4. Design principles / guardrails (do not violate)

- **Gates = the limit.** `mechanical_filter`'s price ($5), market-cap ($500M), volume (500K/day), and debt/equity (≤5×) checks stay **hard**. Nothing below the floor, ever.
- **Anti-herd is a tilt, never an override.** It re-orders within the gated pool; it never admits a name that fails a gate and never carries more than ~0.10 of score weight.
- **Profit-first / balanced.** Unprofitability is allowed *only* when paired with real growth + margin quality (it's an investment signal, not a free pass). Genuine fragility stays excluded.
- **Universe unchanged.** S&P 500 + NDX only. No down-cap widening in this plan — that's a separate, later decision after de-biasing is proven (§9).
- **No deploy flow — local-first (§8):** all work runs on the Mac. Commit to the branch, merge to `main` locally, run via `launchd`. There is no server `git pull` step.
- **DeepSeek JSON:** keep parsing lenient (no `bind_structured`/`tool_choice=required` in thinking mode; tolerate ```fences and missing fields).
- **Fail-safe:** if consensus data is missing/empty, every consensus tilt must no-op to neutral (0.0), never error and never block a pick.

Work on a branch: `core-discovery-v2`. Keep each phase a separate commit so it can be reviewed/reverted independently.

---

## 5. The plan (ordered phases)

### Phase 0 — Cadence: weekly → monthly
**Why:** core names change on a quarterly fundamentals cadence; weekly re-screens identical data.

- `core_discovery.py`: change the within-week cache to a **within-month** cache — key on `%Y-%m` instead of `%Y-W%W` (`_load_weekly_cache`/`_save_weekly_cache` and the `core_discovery_picks_*.json` filename). Rename to `_load_monthly_cache`/`_save_monthly_cache` for clarity.
- `default_config.py`: add `core_discovery_cadence = "monthly"`.
- Cron (deferred to deploy, §8): replace the Saturday-weekly entry with **first Saturday of each month** — `0 10 1-7 * 6` (runs Sat only if it falls on the 1st–7th). Document in `docs/` and in the crontab comment.
- Keep the AV 7-day cache as-is so manual re-runs within a month stay cheap.
- **Optional (note only, do not build now):** an extra discovery pass during earnings season. Leave a `# TODO earnings-season refresh` marker.

**Acceptance:** running `run_core_discovery` twice in the same month returns the cached picks on the second run; the cache file is named by month.

---

### Phase 1 — De-bias the screen within the safe universe (zero added risk; highest value)

#### 1a. `negative_eps` hard gate → fragility gate
File: `tradingagents/portfolio_advisor/mechanical_filter.py`, `_check_one`.

Replace the blanket `eps <= 0 → "negative_eps"` rejection with: **disqualify an unprofitable name only when it is *not* a credible growth investment.**

Logic (null-safe — missing data must NOT disqualify; let the AV quant screen decide):
```
eps = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
if eps is not None and eps <= 0:
    rev_growth   = info.get("revenueGrowth")      # yfinance fraction, may be None
    gross_margin = info.get("grossMargins")       # yfinance fraction, may be None
    # Only reject if we HAVE the data AND it fails the growth-investment bar.
    if rev_growth is not None and gross_margin is not None:
        if not (rev_growth >= 0.20 and gross_margin >= 0.40):
            return "unprofitable_no_growth"
    # else: data missing → pass through to the AV quant screen (lenient)
```
Keep the hard debt/equity gate (`MAX_DEBT_EQUITY`) and all other gates unchanged. Update the rejection-log reason set and the module docstring.

**Acceptance:** a profitless-but-high-growth name (e.g. a 30%-growth, 70%-gross-margin SaaS) survives the mechanical filter; a profitless, no-growth, low-margin name is still rejected as `unprofitable_no_growth`; names with missing growth/margin data pass through rather than being dropped.

#### 1b. Fix PEG-zeroing → growth-adjusted sales multiple for loss-makers
Files: `alpha_vantage_fundamentals_cached.py` (data) and `core_discovery._score_quantitative` (scoring).

- In `get_ticker_fundamentals`, extract two more AV OVERVIEW fields and add them to the returned dict:
  - `evToRevenue   = _f(data.get("EVToRevenue"))`
  - `priceToSales  = _f(data.get("PriceToSalesRatioTTM"))`
- In `_score_quantitative`, when PEG is usable (`0 < peg < 999`) keep the existing `_PEG_TIERS`. When PEG is garbage, fall back to an **EV/Sales-to-Growth** ratio for the same 0.25-point valuation pool:
  ```
  ev_sales = info.get("evToRevenue") or info.get("priceToSales") or 0
  growth_pct = (rev_growth or 0) * 100      # rev_growth is a fraction
  evs_to_growth = ev_sales / growth_pct if (ev_sales > 0 and growth_pct > 0) else None
  ```
  Tier it like PEG (lower = cheaper for the growth). Starting tiers (calibrate on the §5 sample, document final values):
  `<=0.40 → 0.25`, `<=0.70 → 0.18`, `<=1.20 → 0.10`, `<=2.00 → 0.04`, else `0`.
- A loss-maker with strong growth now earns real valuation points instead of a structural zero. PEG remains preferred when valid.

**Acceptance:** a profitless high-growth name with a reasonable EV/Sales receives non-zero valuation points and can reach `SURVIVOR_SCORE`; a profitable name with a valid PEG scores exactly as before (no regression).

#### 1c. Anti-herd in the LLM ranking prompt
File: `core_discovery._llm_qualitative_rank`.

- If a consensus snapshot exists (§Phase 2 data), inject the current public `top_20` list into the prompt as "the names the AI/financial-media herd is currently pushing."
- Add explicit instruction (paraphrase, keep it punchy):
  > "Do not default to the obvious mega-caps that every AI screen surfaces. If a candidate is a current consensus/herd name, treat that as a mild negative unless its setup is exceptional. Prefer under-followed, differentiated businesses that clear the same quality bar. Mark each pick as CONSENSUS or DIFFERENTIATED in your output."
- Extend the output format/regex to capture the CONSENSUS/DIFFERENTIATED tag and carry it into the Telegram message and the recommendation log.

**Acceptance:** with a populated `top_20`, the LLM's selections skew toward names not in `top_20`, and each pick is tagged CONSENSUS/DIFFERENTIATED in the message and the log.

---

### Phase 2 — Turn the consensus tilt on, properly
**Why:** the anti-herd factor already exists and is well-designed; it's dead only because its data was never collected. Wire it as a small ranking tilt *after* the gates.

#### 2a. Merge the parked data + scoring modules
Cherry-pick / merge from `consensus-guardrails-v1` into `core-discovery-v2`:
- `tradingagents/portfolio_advisor/consensus_score.py`
- `tradingagents/dataflows/llm_consensus.py` + `llm_consensus_sources.json`
- `tradingagents/dataflows/retail_flow_tracker.py`
- their tests (`tests/test_llm_consensus.py`, `tests/test_retail_flow_tracker.py`)

#### 2b. Populate the snapshot
- `llm_consensus.run_daily_scrape()` populates `~/.tradingagents/cache/llm_consensus/`. Add a **daily** cron to run it (deferred to deploy, §8). `top_20` fills after a few days of scraping; until then the tilt no-ops to neutral (acceptable).
- Wire `deepseek_alignment.deepseek_last_recommended` from `recommendation_log` — i.e. the system's *own* recent core/EP picks — so `consensus_divergence_score` can detect "are *we* herding."
- **Known limitation (note, don't over-fix):** ticker extraction in `llm_consensus._extract_tickers` is a crude regex ∩ universe and will have some false positives. It's a low-weight, fail-safe tilt — acceptable for v2. Add a short comment flagging it for later hardening.

#### 2c. Wire the discovery tilt (ranking only, post-gate)
In `core_discovery` ranking (after `_quantitative_screen` passes a name through `SURVIVOR_SCORE`, before/within the LLM step):
- Compute a **discovery-specific** consensus tilt using only the *crowding* components — **`consensus_entry_score` + `consensus_divergence_score`**, averaged. **Do NOT use `consensus_retail_flow_score` here** — reserve retail flow for the PM sizing layer (§7).
  ```
  tilt = consensus_tilt_weight * (entry + divergence) / 2      # entry,divergence ∈ [-1,1]
  final_score = quant_score + tilt
  ```
- `default_config.py`: add `consensus_tilt_weight = 0.10` and gate the whole tilt behind the existing `CONSENSUS_FACTOR_LIVE` flag (default **False** until the snapshot is proven populated).
- The tilt re-orders survivors; it **never** changes who passes the gate. If flag off or snapshot empty → `tilt = 0`.

#### 2d. Void the old verdict
The prior "null backtest" was run against an empty snapshot and is invalid. Once the scrape has produced ≥2 weeks of data, re-evaluate the factor on real data before raising `consensus_tilt_weight` or flipping the flag live. Record the re-evaluation in `docs/`.

**Acceptance:** with `CONSENSUS_FACTOR_LIVE=True` and a populated snapshot, two names with equal quant scores are ordered by crowding (the less-crowded one ranks higher); with the flag off or snapshot empty, ranking is identical to Phase 1 output (tilt = 0, no errors).

---

### Phase 3 — Stop starving the funnel (tiered output)
File: `core_discovery._llm_qualitative_rank` + `run_core_discovery`.

- Return **two tiers** instead of "deep-dive-YES or nothing":
  - **Deep-dive picks** (`DEEP_DIVE YES`): delivered to Telegram (urgent, as today) **and** pushed to the watchlist with `source="core_discovery"`.
  - **On-the-radar** (passed quant + LLM but `DEEP_DIVE NO`): pushed to the watchlist with `source="core_discovery_watch"`, low priority, **no Telegram** (or a single quiet summary line). These feed the PM funnel without forcing a pick.
- Log **both tiers** to `recommendation_log` (deep-dive and watch as distinct `rec_action`s) so `outcome_tracker` measures them and the learning loop can tell whether the watch tier ever pays.
- Preserve the existing "quiet week" behavior for the *deep-dive* Telegram message ("no compelling deep-dive this week") — but the watch tier still populates silently.

**Acceptance:** a run where the LLM marks everything `DEEP_DIVE NO` still adds names to the watchlist under `core_discovery_watch` and logs them, while Telegram correctly says no deep-dive candidate.

---

### Phase 4 — Keep the risk boundary explicit (no universe change)
- Universe stays S&P 500 + NDX. Add a short comment block at the top of `_build_universe` stating that down-cap widening is **deliberately deferred** and is a separate decision (link this doc, §9), so a future agent doesn't "helpfully" widen it and quietly raise risk.
- Confirm all `mechanical_filter` hard gates remain unchanged and hard.

---

## 6. Valuation policy (explicit — so it isn't re-litigated)

- **EP discovery (catalyst/momentum):** **no valuation bias.** EP rides post-catalyst continuation; cheapness is an anti-signal. Keep only the **momentum-exhaustion** check (Section 10 extended-run: skip names already up 50%+ in 10 sessions). Do **not** add any "undervalued" filter to the EP scanner.
- **Core discovery (compounders):** valuation enters **only** as **growth-adjusted** (PEG, or EV/Sales÷growth for loss-makers — Phase 1b), as **one weighted input** among moat/growth/ROIC/margins. **No deep-value / "buy cheap" bias** — that excludes the best compounders and walks into value traps.
- **Entry timing (price/"buy it cheaper"):** a *legitimate* instinct, but it belongs in the **PM layer**, not discovery. Discovery = "worth tracking"; PM = "good price now," where a mild prefer-a-pullback tilt is fine. Do not fold entry-price logic into the discovery screen — it fights the quality signal.

---

## 7. Consensus factor: one module, two consumers

- **Discovery (this plan):** uses **entry + divergence** (crowding/freshness) as a **ranking tilt** (`consensus_tilt_weight`, post-gate). Answers "is this candidate part of the herd?"
- **PM layer (existing/next):** uses the **full composite** including **`consensus_retail_flow_score`** for **sizing & entry timing** ("modulate how much and when, not whether" — the module's v4 design intent). The retail-flow component is deliberately *excluded* from discovery and *reserved* for the PM so the two layers don't double-count it.

This keeps anti-herd present at both the *what-to-track* and *how-much-to-size* stages, from a single source of truth, without conflating discovery and sizing.

---

## 8. Local-first operation (no server)

Decision: the whole advisor — including the **PM cycle** — runs **locally on the Mac**. The Hetzner server is abandoned for now; do **not** plan around it. State already lives at `~/.tradingagents/` on the Mac, the Mac crons already exist (mac-watchdog, sync, dashboard), and `.env` is local. There is **no deploy step** — code on `main` (or the working branch once merged) *is* what runs.

**Scheduling — use macOS `launchd`, not crontab.** launchd is the right tool on a laptop because a job with `StartCalendarInterval` that was missed while the Mac slept **runs once on wake** — crontab silently skips it. Install LaunchAgents under `~/Library/LaunchAgents/` for each job:
- `com.compounder.core-discovery` — monthly, first Saturday 10:00 (or local equivalent). Replaces the old weekly server cron.
- `com.compounder.llm-consensus` — daily scrape (`llm_consensus.run_daily_scrape()`) to populate the snapshot.
- `com.compounder.run-due` — the PM run-due cycle (`service.py`), every 15 min.
- `com.compounder.watchdog` — every 5 min during US market hours.
- `com.compounder.morning` / `com.compounder.evening` / `com.compounder.weekly` / `com.compounder.replan` — the existing digest/replan schedule.
- `com.compounder.telegram-listener` — long-running inbound listener as a `KeepAlive` LaunchAgent (replaces the server systemd service). **It is now the SOLE listener** — with the server down there is no second `getUpdates` poller, so the prior conflict risk is gone. Never start a second one.

**Sleep is the one real gotcha (operational, settle at install time — not a code task):** launchd reruns *missed* calendar jobs on wake, but the **5-minute market-hours watchdog needs the Mac actually awake** 14:30–21:00 UTC. Options: (a) a `pmset repeat wake` schedule to wake the Mac on market days + `caffeinate` during the session, or (b) keep the Mac plugged in and awake on trading days. Pick one when wiring launchd; it does not block any of the code phases.

**Local verification (required before declaring done):**
1. Cold run: clear the AV + monthly caches, run `run_core_discovery(DEFAULT_CONFIG.copy())` on a ~30-ticker subset; confirm no exceptions and a sensible candidate list.
2. Warm run: re-run; confirm the monthly cache returns identical picks.
3. Phase-1 differentiation check: confirm at least one profitless-high-growth name now survives that the old `negative_eps` gate would have killed.
4. Phase-2 (if data available): populate the snapshot manually (`run_daily_scrape()` a few times or hand-seed a test snapshot), set `CONSENSUS_FACTOR_LIVE=True`, confirm the tilt re-orders equal-quant names and no-ops cleanly when the snapshot is empty.
5. Phase-3: force an all-`DEEP_DIVE NO` run and confirm the watch tier populates the watchlist and the log.
6. End-to-end: one local cold run that reaches Telegram delivery, plus confirm the PM run-due cycle picks up the discovery picks from the watchlist.

**Install once code is verified (local, no deploy):**
- Merge `core-discovery-v2` → `main` locally.
- Write the LaunchAgent plists above; `launchctl load` each.
- Confirm the old weekly core-discovery schedule is gone and the monthly one is active.
- Confirm exactly one Telegram listener is running.

---

## 9. Out of scope / do-not-regress

**Out of scope (do not do in this plan):**
- Widening the universe below S&P 500 + NDX (down-cap). Separate decision; raises risk; only consider after de-biasing is proven.
- Reworking the EP scanner (that's the parallel EP plan — `ep-gate-audit` branch). Touch EP only to confirm the §6 valuation policy isn't violated.
- Raising `consensus_tilt_weight` or flipping `CONSENSUS_FACTOR_LIVE` live before the re-evaluation on real data (Phase 2d).
- Any trade execution wiring — execution stays manual.

**Do-not-regress:**
- Mechanical gates stay hard (price/cap/volume/debt).
- The watchlist push from discovery keeps working.
- Every pick keeps getting logged to `recommendation_log`.
- Lenient DeepSeek JSON parsing.
- Never hot-patch the server tree.

---

## 10. Definition of done

- [ ] Branch `core-discovery-v2` with one commit per phase.
- [ ] Phase 0: monthly cadence + monthly cache; config flag added; cron change documented (install deferred).
- [ ] Phase 1a: fragility gate replaces `negative_eps`; profitless-high-growth survives, profitless-no-growth rejected, missing-data passes through.
- [ ] Phase 1b: AV wrapper extracts EV/Sales + P/S; growth-adjusted valuation fallback scores loss-makers; profitable-name scoring unchanged.
- [ ] Phase 1c: anti-herd prompt + CONSENSUS/DIFFERENTIATED tagging in message and log.
- [ ] Phase 2: consensus modules merged; snapshot-populate path + daily scrape (cron deferred); discovery tilt wired post-gate behind `CONSENSUS_FACTOR_LIVE`; retail-flow reserved for PM; old null verdict voided with a note.
- [ ] Phase 3: two-tier output; watch tier populates watchlist + log; deep-dive Telegram unchanged.
- [ ] Phase 4: universe-unchanged comment; gates confirmed hard.
- [ ] §6 valuation policy reflected in code/comments (no undervalued filter in either pipeline; growth-adjusted only in core).
- [ ] Local verification §8 steps 1–5 pass; results recorded in `docs/core_discovery_v2_verification.md`.
- [ ] No regression to watchlist push or recommendation logging.

---

*Plan authored 2026-06-09. Implements the de-herding + risk-balance redesign agreed with the user: gates are the risk limit, anti-herd is a post-gate tilt, valuation is growth-adjusted (core) / ignored (EP), cadence is monthly, and the parked consensus factor is revived on real data.*
