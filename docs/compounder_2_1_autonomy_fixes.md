# Compounder 2.1 — Autonomy Gap Fixes (implementation work order)

**Date:** 2026-06-10. **Supersedes:** the in-chat fix list from the 2026-06-10 audit session.
**Audience:** an AI or engineer with NO prior context. Everything needed is in this file + the repo.

## Context (read first)

Repo: `~/workspace/trading-agents`. Python 3.12 venv at `.venv` (use `.venv/bin/python`; it is a uv venv — no `python -m pip`).
Run tests: `cd ~/workspace/trading-agents && set -a && source .env && set +a && .venv/bin/python -m pytest tests/ -q` → **all 433 tests + 93 subtests must pass when you finish.**

The system is an autonomous AI portfolio manager ("Compounder"). As of 2026-06-10 it runs in **autonomous mode** (`TRADINGAGENTS_ACCOUNT_MODE=alpaca` in `.env`): the PM LLM manages an Alpaca PAPER account (~$100k, account PA3D14DBA83W) end-to-end. PM proposals (`pm_tools.propose_trade` → `proposals.add()`) auto-execute via `tradingagents/integrations/alpaca/executor.py`. The portfolio snapshot everywhere comes from `etoro_scan.fetch_portfolio_rows()`, which routes to `tradingagents/integrations/alpaca/account_adapter.py` in this mode. A watchdog LaunchAgent runs every 5 min in US market hours; `executor.enforce_paper_exits()` runs at the top of every tick.

Two-sleeve strategy: **core** = 3–5yr holds (staged 1/3 entries, +15% pre-earnings trim, +100% trim-half, −30% soft / −40% hard stop, thesis-break exit); **catalyst** = short-term event trades (full-size entry before a dated catalyst, −8% hard stop, trailing stop arms at +10% then −8%-from-peak, time-stop 3d after `catalyst_date`, 30d max hold).

**Safety invariants — never violate:**
- PAPER ONLY. The executor refuses non-`PK` Alpaca keys and hard-codes `paper=True` (`executor.py::_client`). Never add a real-money path. Never touch eToro execution.
- `PYTEST_CURRENT_TEST` guards: `executor.enabled()` and `etoro_scan.account_mode()` both return safe defaults under pytest. Preserve this; tests must never hit live APIs.
- All executor entry points swallow their own errors (log + Telegram, never raise into the PM cycle/watchdog). Keep that contract.
- State lives in `~/.tradingagents/portfolio_advisor/` (ledgers: `proposed_trades.jsonl`, `alpaca_trades.jsonl`, `position_plans.json`). Tests use a sandboxed dir via cfg `portfolio_advisor_dir` — never write to the real one from tests.

A 26-agent audit (2026-06-10) found the gaps below, each with file:line evidence (line numbers approximate ±15; re-locate by symbol). Fix in the given order: F1/F2 unblock F6.

---

## F1 — Auto-create a PositionPlan on every autonomous buy  (BLOCKER)

**Problem:** No code path creates a `PositionPlan` for a position the PM opens itself — every creator is a manual CLI (`cli/advisor_cmd.py position-plan set` / backfill). `pm_tools.adjust_position_plan` edits only ("no existing plan" error, `pm_tools.py:239`), the classifier requires an existing plan. Result: the trigger machinery in `position_plans.py` (`_catalyst_triggers` trailing/time stops at :481–548, `DOUBLE_FROM_ENTRY` +100% at :445, thesis-break metrics) never runs for autonomous positions — the PM prompt only shows "Positions with no plan on file".
Also: `catalyst_date` is persisted in `proposed_trades.jsonl` (`proposals.py:156`) but **nowhere the exit logic reads** — `_paper_buy`'s ledger row omits it (`executor.py:247–262`).

**Fix:**
1. In `executor.py::_paper_buy`, after a successful `submit_order`: create/upsert a `PositionPlan` (use the existing constructor/upsert in `position_plans.py` — match its schema exactly) with: ticker, `strategy=sleeve` (from the proposal; default `core`), `entry_price` = proposal `target_price` if >0 else last close via yfinance, `catalyst_date` from the proposal (may be None), a note `"auto-created on autonomous buy <ts>"`. Wrap in try/except (never break the buy path).
2. Add `catalyst_date` to the `alpaca_trades.jsonl` buy row.
3. In `enforce_paper_exits` (`executor.py:373–422`): prefer the plan's `strategy`/`catalyst_date` (load via `position_plans` helpers) over the ledger row; if NEITHER source knows the position, send a one-time Telegram warning (currently it silently defaults to core floors — audit gap "silent demotion").

**Accept:** a new autonomous catalyst buy yields a `position_plans.json` entry with strategy=catalyst and the date; the PM prompt's POSITION RULE STATUS block lists it; unit test with sandboxed cfg proves plan creation (mock the Alpaca client; see test style in `tests/test_outcome_tracker.py`).

## F2 — Proposal lifecycle: mark `executed`, release the dedup gate  (BLOCKER)

**Problem:** `proposals.add()` auto-executes only when `prior is None` (`proposals.py:173–179`), and **nothing ever transitions autonomous proposals out of `proposed`** (no writer of `executed`; `auto_close_stale` has zero callers; `reconcile_with_portfolio` cancels reduce-side only). Consequences (all audit-confirmed): staged core tranches 2–3 are silently never executed ("add" supersedes the still-open "buy" row — same side); a stopped-out catalyst name can never be re-entered; a PM exit on a trailing/time stop can be swallowed by a stale open trim row.

**Fix (in `proposals.py::add` + small executor change):**
1. Make `executor.execute_proposal` return a machine-readable result — change it to return `{"status": "executed"|"skipped"|"error"|"disabled", "detail": str}` (update its 3 call sites: `proposals.py`, tests, none else; keep a string fallback if simpler — but the caller must distinguish outcomes).
2. In `add()`: call the executor for genuinely new proposals as now; on `executed` → set the row's `status="executed"`, `status_set_at`, `status_note=detail`; on `skipped` → `status="cancelled"` with the skip reason (a skipped intent must not block the future). Persist via `save_all`.
3. The supersede/dedup logic already only considers `status=="proposed"` rows — verify, so executed/cancelled rows release the gate.
4. **Restatement double-buy protection** (new risk once the gate opens): in `execute_proposal`, before a `buy`/`add`, check `alpaca_trades.jsonl` for an executed same-ticker increase within `portfolio_advisor_add_cooldown_days` (new cfg key, default 5): if found, return `skipped` ("cooldown"). Plain `buy` of a held name stays skipped (existing behavior); `add` respects only the cooldown — that's what allows weekly tranches 2–3.
5. Wire `proposals.auto_close_stale(cfg)` into the action-check cron path (`scripts/cron-portfolio-advisor-action-check.sh` → find the CLI it calls and add the call there) as defense-in-depth.

**Accept:** unit tests: (a) buy → executed status; (b) re-proposal after exit executes again; (c) add 5+ days after buy executes (tranche), add 1 day after is skipped; (d) sell of unheld name → cancelled.

## F3 — Empty paper book must not deadlock the PM  (BLOCKER)

**Problem:** `advisor_pm.py:~1875` — `run_pm_cycle` raises `RuntimeError("No tickers in eToro portfolio export.")` on an empty book. In autonomous mode a fully stopped-out book (all cash) means every PM cycle dies before the LLM is invoked → the system can never re-enter. Full de-risk is a terminal state.

**Fix:** when `account_mode()=="alpaca"` and rows are empty, proceed with an explicit snapshot text like "(no open positions — book is 100% cash: $X; your job this cycle is re-entry from candidates/watchlist)" and empty `live_tickers`. Audit downstream uses of `live_tickers` in the prompt assembly for crashes on empty (sleeve block, trigger block, "Live tickers" line) — they must degrade gracefully. Keep the raise for eToro mode (there, empty = fetch failure).

**Accept:** test: `run_pm_cycle` prompt-assembly path works with zero positions in alpaca mode (mock the adapter; you don't need to invoke the LLM — refactor-extract or monkeypatch at the LLM call boundary if needed).

## F4 — Feed the catalyst sleeve  (BLOCKER for short-term trading)

**Problem A:** the weekly news/catalyst funnel (`news_researcher.run_weekly_discovery` — the ONLY producer of catalyst-dated, gate-checked candidates) is **unscheduled**: its CLI is `advisor watchlist research` (`cli/advisor_cmd.py:1203`), but `scripts/run-advisor.sh:60` hardcodes `advisor portfolio "$CMD"`, so no LaunchAgent can reach it. **Fix:** extend `run-advisor.sh` to support a second arg form (e.g. `run-advisor.sh watchlist research`) routing to `advisor watchlist <cmd>`; add LaunchAgent `com.compounder.watchlist-research.plist` (Mon 04:30 local, `StartCalendarInterval` `Weekday=1`), modeled on the existing 14 `com.compounder.*` plists in `~/Library/LaunchAgents/` (read one for the pattern: ProgramArguments → run-advisor.sh, log paths, KeepAlive=false). Load it with `launchctl load`.

**Problem B:** the scheduled EP scanner (daily 13:30 + 21:15) terminates in `pm_tools.emit_ep_candidate` (`pm_tools.py:610–685`) which only sends an **advisory Telegram** ("the human decides") — bypassing the autonomous executor entirely. **Fix:** in `emit_ep_candidate`, when `etoro_scan.account_mode()=="alpaca"`: after composing, also call `proposals.add(...)` with `action="buy"`, `sleeve="catalyst"`, the computed sizing, the scan's catalyst date, confidence if available. **Hard guard: no dated catalyst → stay advisory-only (do not trade).** Keep the Telegram message in both modes (in autonomous mode it becomes the decision notice).

**Problem C (small):** `advisor_pm.py::_apply_candidate_comparisons` (`:1328–1331`) calls `proposals.add` directly with `sleeve` from the LLM and **no catalyst_date** — bypassing the dated-event guard in `propose_trade`. **Fix:** in that path, if `sleeve=="catalyst"` and no date is available, force `sleeve="core"` and append "(forced core: no dated catalyst)" to the reason — mirroring the guard rationale in `pm_tools.py:298–312`.

**Accept:** plist loads and `launchctl list | grep watchlist` shows it; `run-advisor.sh watchlist research` invokes the right CLI (dry-check by echo or `--help`); unit test for the comparisons sleeve-forcing; emit_ep_candidate test that a dated catalyst in alpaca mode produces a proposals row and an undated one does not.

## F5 — Watchdog/executor correctness trio  (DEGRADED)

1. **Pre-earnings trim re-fires** and can repeatedly halve a position: the mirror loop (`watchdog.py:481–489`) iterates the FULL current trim list on any changed tick, and per-share gain doesn't drop after a sell. **Fix:** persist a consumed marker in watchdog state keyed `(ticker, earnings_date)` (state dict already flows through `pa_state`); mirror a pre-earnings trim only once per key. (Precedent: the removed `double_from_entry` consumption logic, `watchdog.py:424–431`.)
2. **`sell` ignores size** — `_paper_reduce` (`executor.py:286–292`) closes 100% for `sell` regardless of `approx_usd`. **Fix:** if `act=="sell"` and `0 < approx_usd < 0.95 × |market_value|`, treat as fractional close (same math as trim); else full close.
3. **+100% trim-half has no enforcer** (removed from watchdog; plan-based prompt line only — and until F1 no plans existed). **Fix:** with plans from F1, add to `enforce_paper_exits`: core position with a plan, `current_price >= entry*2`, and no prior consumption → close 50% once, rule `paper_core_double_trim`; persist consumption on the plan (e.g. `double_trim_done_at`).

**Accept:** unit tests for each (mock client; synthetic ledger/plan rows).

## F6 — Deterministic trailing stop + time stop every tick  (BLOCKER, depends on F1)

**Problem:** catalyst trailing stop (+10% arms, exit at peak×0.92) and the 3d-post-catalyst time stop are implemented correctly in `position_plans._catalyst_triggers` (`:481–548`) but evaluated ONLY inside PM prompts (`build_trigger_block`, called once per PM cycle) — peaks ratchet only on PM cycles, nothing deterministic ever executes the exits.

**Fix:** extend `enforce_paper_exits` (`executor.py`): for each open catalyst position with a plan: update the plan's persisted peak with the current Alpaca price (reuse/refactor the peak-persistence in `position_plans.py:677–688` into a helper callable outside the prompt path); then evaluate trailing (`peak >= entry*1.10 and price <= peak*0.92` → close 100%, rule `paper_catalyst_trailing_stop`) and time stop (`catalyst_date` set and today ≥ catalyst_date + `portfolio_advisor_catalyst_time_stop_days` (default 3) and the position hasn't run — reuse the exact semantics of `_catalyst_triggers` rather than re-deriving; refactor that function so the watchdog tick and the prompt block share one implementation). Price source: Alpaca position (`market_value/qty` or `current_price`) — no yfinance call needed per tick.

**Accept:** unit tests: peak ratchets across two calls; trailing fires at the right prices and not before arming; time-stop fires 3d after the date; consumed/closed positions don't re-fire. Full suite green.

---

## Done criteria for the whole work order
1. Full test suite green (433+ tests).
2. Every fix has at least one new unit test.
3. `git add` only files you changed; commit per fix-group (F1+F2 may share one commit) with messages explaining WHY, ending with the project's `Co-Authored-By` convention if instructed by the operator.
4. Update the stale docstrings you touch in passing (e.g. `proposals.py:9–13` "nothing writes this status yet", `pm_tools.propose_trade` "human executes on eToro", `executor.py` module docstring eToro-scaling framing) — only where you're already editing.
5. Do NOT: place real orders in tests, weaken the PK-key/paper guards, schedule anything beyond the one new LaunchAgent, or touch `docs/compounder_2_0_vision.md`.
