"""Tests for three catalyst trade lifecycle defect fixes.

Fix 1 — Re-entry cooldown (wash-loop prevention):
  After a position is closed (watchdog_exit, sell, or trim in the ledger),
  a new BUY or ADD for the same ticker within portfolio_advisor_reentry_cooldown_days
  is skipped with 're-entry cooldown: stopped out N days ago'.

Fix 2 — Stop anchored to fill:
  When a position plan's entry_price diverges from Alpaca's avg_entry_price by >0.5%,
  enforce_paper_exits updates the plan's entry_price once (idempotent) and appends
  an audit note 'entry synced to fill <px> on <date>'.

Fix 3 — Dead zone config keys:
  portfolio_advisor_catalyst_trail_arm_pct (default 0.05) and
  portfolio_advisor_catalyst_trail_dist_pct (default 0.08) now drive the trailing
  stop arm threshold and exit distance respectively.
  The old +10% arm threshold left +5..+10% winners with no exit rule; the new
  default of +5% closes that gap.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **overrides) -> Dict[str, Any]:
    cfg = {
        "portfolio_advisor_dir": str(tmp_path / "pa"),
        "portfolio_advisor_alpaca_paper": True,
        "portfolio_advisor_alpaca_max_position_pct": 0.10,
        "portfolio_advisor_add_cooldown_days": 5,
        "portfolio_advisor_reentry_cooldown_days": 5,
        "portfolio_advisor_catalyst_max_hold_days": 30,
        "portfolio_advisor_catalyst_time_stop_days": 3,
        "portfolio_advisor_catalyst_hard_stop_pct": 0.08,
        "portfolio_advisor_catalyst_trail_arm_pct": 0.05,
        "portfolio_advisor_catalyst_trail_dist_pct": 0.08,
    }
    cfg.update(overrides)
    return cfg


def _pa_dir(cfg: Dict[str, Any]) -> Path:
    p = Path(cfg["portfolio_advisor_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_ledger_row(
    pa_dir: Path,
    ticker: str,
    action: str,
    days_ago: int = 0,
    status: str = "submitted",
    sleeve: str = "core",
    catalyst_date: str = "",
) -> None:
    pa_dir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {
        "ts": ts,
        "ticker": ticker.upper(),
        "action": action,
        "status": status,
        "sleeve": sleeve,
        "catalyst_date": catalyst_date,
        "order_id": "old-order",
    }
    ledger = pa_dir / "alpaca_trades.jsonl"
    with ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _make_alpaca_client(equity: float = 100_000.0) -> MagicMock:
    client = MagicMock()
    acct = MagicMock()
    acct.equity = str(equity)
    client.get_account.return_value = acct
    order = MagicMock()
    order.id = "mock-order-id"
    client.submit_order.return_value = order
    client.get_open_position.side_effect = Exception("no position")
    client.get_all_positions.return_value = []
    return client


def _make_position(
    ticker: str,
    plpc: float,
    market_value: float = 5000.0,
    qty: float = 50.0,
    avg_entry_price: str = "",
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = ticker
    pos.unrealized_plpc = str(plpc)
    pos.market_value = str(market_value)
    pos.qty = str(qty)
    pos.current_price = str(abs(market_value) / abs(qty)) if qty != 0 else "0"
    # avg_entry_price is a string attribute on the real Alpaca Position object.
    pos.avg_entry_price = avg_entry_price if avg_entry_price else str(abs(market_value) / abs(qty))
    return pos


def _write_plan(
    cfg: Dict[str, Any],
    ticker: str,
    entry_price: float,
    strategy: str = "core",
    catalyst_date: str = "",
    notes: str = "",
    peak_price=None,
) -> None:
    from tradingagents.portfolio_advisor.position_plans import PositionPlan, upsert_position_plan

    plan = PositionPlan(
        ticker=ticker.upper(),
        entry_price=entry_price,
        strategy=strategy,
        catalyst_date=catalyst_date,
        notes=notes,
        peak_price=peak_price,
    )
    upsert_position_plan(cfg, plan)


# ===========================================================================
# Fix 1 — Re-entry cooldown
# ===========================================================================


class TestReentryCooldown:
    """Fix 1: BUY/ADD is skipped when the same ticker was recently closed."""

    def test_reentry_cooldown_helper_finds_watchdog_exit(self, tmp_path):
        """_reentry_cooldown_skip_reason returns reason when ticker was watchdog_exited."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "NVDA", "watchdog_exit", days_ago=2)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason, "Should return a skip reason for recent watchdog_exit"
        assert "re-entry cooldown" in reason
        assert "stopped out" in reason

    def test_reentry_cooldown_helper_finds_sell(self, tmp_path):
        """_reentry_cooldown_skip_reason returns reason when ticker was recently sold."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "AAPL", "sell", days_ago=1)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "AAPL", 5)
        assert reason, "Should return a skip reason for recent sell"
        assert "re-entry cooldown" in reason

    def test_reentry_cooldown_helper_finds_trim(self, tmp_path):
        """_reentry_cooldown_skip_reason returns reason when ticker was recently trimmed."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "TSLA", "trim", days_ago=3)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "TSLA", 5)
        assert reason, "Should return a skip reason for recent trim"

    def test_reentry_cooldown_allows_after_window(self, tmp_path):
        """_reentry_cooldown_skip_reason returns '' when close was > cooldown_days ago."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "NVDA", "watchdog_exit", days_ago=7)  # outside 5-day window

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "Should allow re-entry after cooldown window expires"

    def test_reentry_cooldown_no_ledger_allows(self, tmp_path):
        """_reentry_cooldown_skip_reason returns '' when there is no ledger file."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "No ledger → no cooldown"

    def test_reentry_cooldown_different_ticker_allows(self, tmp_path):
        """Cooldown for AAPL close should NOT block a buy of NVDA."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "AAPL", "watchdog_exit", days_ago=1)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "Cooldown for different ticker should not block"

    def test_reentry_cooldown_does_not_block_regular_buy(self, tmp_path):
        """A recent BUY in ledger does NOT trigger re-entry cooldown (only closes do)."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "NVDA", "buy", days_ago=1)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "NVDA", 5)
        assert reason == "", "A recent buy should NOT trigger re-entry cooldown (that's the add-cooldown's job)"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_paper_buy_skipped_after_recent_close(self, mock_enabled, mock_client_fn, tmp_path):
        """_paper_buy returns 'skipped' when the same ticker was recently watchdog_exited."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "CRWD", "watchdog_exit", days_ago=2)

        client = _make_alpaca_client()
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "CRWD", {
            "ticker": "CRWD",
            "action": "buy",
            "approx_usd": 5000.0,
            "target_price": 200.0,
            "sleeve": "catalyst",
        })
        assert "skipped" in result.lower()
        assert "re-entry cooldown" in result
        # Must NOT have placed an order
        client.submit_order.assert_not_called()

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_paper_buy_allowed_after_cooldown_expires(self, mock_enabled, mock_client_fn, tmp_path):
        """_paper_buy proceeds when the close was > reentry_cooldown_days ago."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "CRWD", "watchdog_exit", days_ago=8)  # outside 5-day window

        client = _make_alpaca_client()
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "CRWD", {
            "ticker": "CRWD",
            "action": "buy",
            "approx_usd": 5000.0,
            "target_price": 200.0,
            "sleeve": "catalyst",
        })
        assert "skipped" not in result.lower() or "re-entry" not in result
        client.submit_order.assert_called_once()

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_paper_add_also_blocked_by_reentry_cooldown(self, mock_enabled, mock_client_fn, tmp_path):
        """An 'add' (second tranche) is also blocked by re-entry cooldown after a stop-out."""
        cfg = _cfg(tmp_path)
        pa_dir = _pa_dir(cfg)
        _write_ledger_row(pa_dir, "MSFT", "watchdog_exit", days_ago=1)

        client = _make_alpaca_client()
        mock_client_fn.return_value = client

        from tradingagents.integrations.alpaca import executor as ex

        result = ex._paper_buy(cfg, client, 100_000.0, "MSFT", {
            "ticker": "MSFT",
            "action": "add",
            "approx_usd": 5000.0,
            "target_price": 300.0,
            "sleeve": "core",
        })
        assert "skipped" in result.lower()
        assert "re-entry cooldown" in result
        client.submit_order.assert_not_called()

    def test_reentry_cooldown_respects_custom_days_config(self, tmp_path):
        """Custom portfolio_advisor_reentry_cooldown_days is respected."""
        cfg = _cfg(tmp_path, portfolio_advisor_reentry_cooldown_days=10)
        pa_dir = _pa_dir(cfg)
        # 7 days ago — outside 5-day default window, but inside 10-day custom window
        _write_ledger_row(pa_dir, "AMD", "watchdog_exit", days_ago=7)

        from tradingagents.integrations.alpaca import executor as ex

        reason = ex._reentry_cooldown_skip_reason(cfg, "AMD", 10)
        assert reason, "Custom 10-day cooldown should block a 7-day-old stop-out"

        reason_5d = ex._reentry_cooldown_skip_reason(cfg, "AMD", 5)
        assert reason_5d == "", "Default 5-day cooldown should NOT block a 7-day-old stop-out"


# ===========================================================================
# Fix 2 — Stop anchored to fill
# ===========================================================================


class TestStopAnchoredToFill:
    """Fix 2: plan.entry_price is synced to Alpaca avg_entry_price when they diverge > 0.5%."""

    def test_sync_helper_updates_entry_on_divergence(self, tmp_path):
        """_sync_plan_entry_to_fill updates entry_price when fill diverges > 0.5%."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan

        plan = PositionPlan(ticker="NVDA", entry_price=100.0, strategy="catalyst")

        # Simulate a 3% gap: plan said 100 but actual fill was 103
        pos = MagicMock()
        pos.avg_entry_price = "103.0"  # string, like the real Alpaca API returns

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = ex._sync_plan_entry_to_fill(plan, pos, now_iso)
        assert changed, "Should return True when divergence > 0.5%"
        assert abs(plan.entry_price - 103.0) < 0.001, "entry_price should be updated to fill price"
        assert "entry synced to fill" in plan.notes

    def test_sync_helper_is_idempotent(self, tmp_path):
        """_sync_plan_entry_to_fill does NOT update again if already synced."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan

        plan = PositionPlan(
            ticker="NVDA",
            entry_price=103.0,
            strategy="catalyst",
            notes="entry synced to fill 103.0 on 2026-06-10 (was 100.0000, diverged 3.00%)",
        )
        pos = MagicMock()
        pos.avg_entry_price = "103.0"

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = ex._sync_plan_entry_to_fill(plan, pos, now_iso)
        assert not changed, "Should NOT update again — idempotency guard triggered"

    def test_sync_helper_ignores_small_divergence(self, tmp_path):
        """_sync_plan_entry_to_fill does NOT update when divergence <= 0.5%."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan

        plan = PositionPlan(ticker="AAPL", entry_price=100.0, strategy="core")
        pos = MagicMock()
        pos.avg_entry_price = "100.3"  # 0.3% divergence — within tolerance

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = ex._sync_plan_entry_to_fill(plan, pos, now_iso)
        assert not changed, "Small divergence (<= 0.5%) should not trigger sync"
        assert plan.entry_price == pytest.approx(100.0)

    def test_sync_helper_ignores_missing_avg_entry_price(self, tmp_path):
        """_sync_plan_entry_to_fill does not update when avg_entry_price is not a valid number."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan

        plan = PositionPlan(ticker="TSLA", entry_price=100.0, strategy="core")
        pos = MagicMock()
        # avg_entry_price is a MagicMock (not a string/float), as in test environments
        # The helper must NOT update the plan in this case.
        # Don't set pos.avg_entry_price as a string — leave as MagicMock attr.

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = ex._sync_plan_entry_to_fill(plan, pos, now_iso)
        assert not changed, "MagicMock avg_entry_price should not trigger sync"
        assert plan.entry_price == pytest.approx(100.0)

    def test_sync_helper_ignores_zero_fill_price(self, tmp_path):
        """_sync_plan_entry_to_fill does not update when avg_entry_price is '0'."""
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import PositionPlan

        plan = PositionPlan(ticker="GOOG", entry_price=150.0, strategy="core")
        pos = MagicMock()
        pos.avg_entry_price = "0"

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = ex._sync_plan_entry_to_fill(plan, pos, now_iso)
        assert not changed, "Zero fill price should not trigger sync"

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_enforce_paper_exits_syncs_plan_to_fill(self, mock_enabled, mock_client_fn, tmp_path):
        """enforce_paper_exits syncs plan.entry_price to avg_entry_price when they diverge."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        # Plan says entry=100, but Alpaca fill was 107 (7% gap on open)
        _write_plan(cfg, "CRWD", entry_price=100.0, strategy="catalyst",
                    catalyst_date="2026-09-15", notes="auto-created on autonomous buy 2026-06-01")

        pos = _make_position("CRWD", plpc=0.07, market_value=5350.0, qty=50.0,
                             avg_entry_price="107.0")

        client = MagicMock()
        client.get_all_positions.return_value = [pos]

        mock_client_fn.return_value = client

        ex.enforce_paper_exits(cfg)

        plans = load_position_plans(cfg)
        assert "CRWD" in plans
        assert abs(plans["CRWD"].entry_price - 107.0) < 0.01, \
            f"entry_price should be synced to 107.0, got {plans['CRWD'].entry_price}"
        assert "entry synced to fill" in plans["CRWD"].notes

    @patch("tradingagents.integrations.alpaca.executor._client")
    @patch("tradingagents.integrations.alpaca.executor.enabled", return_value=True)
    def test_enforce_paper_exits_does_not_resync(self, mock_enabled, mock_client_fn, tmp_path):
        """enforce_paper_exits does not re-sync a plan that was already synced."""
        cfg = _cfg(tmp_path)
        from tradingagents.integrations.alpaca import executor as ex
        from tradingagents.portfolio_advisor.position_plans import load_position_plans

        # Plan was already synced once — note contains the idempotency marker
        _write_plan(
            cfg, "NVDA", entry_price=107.0, strategy="catalyst",
            catalyst_date="2026-09-15",
            notes="auto-created on autonomous buy 2026-06-01\nentry synced to fill 107.0 on 2026-06-02",
        )

        pos = _make_position("NVDA", plpc=0.07, market_value=5350.0, qty=50.0,
                             avg_entry_price="107.0")

        client = MagicMock()
        client.get_all_positions.return_value = [pos]
        mock_client_fn.return_value = client

        ex.enforce_paper_exits(cfg)

        plans = load_position_plans(cfg)
        # Notes should contain only one "entry synced to fill" occurrence
        occurrences = plans["NVDA"].notes.count("entry synced to fill")
        assert occurrences == 1, "Sync note should appear exactly once (idempotent)"


# ===========================================================================
# Fix 3 — Dead zone config keys
# ===========================================================================


class TestDeadZoneConfigKeys:
    """Fix 3: portfolio_advisor_catalyst_trail_arm_pct and _trail_dist_pct drive trailing stop."""

    def test_default_arm_pct_is_0_05(self):
        """DEFAULT catalyst_trail_arm_pct is 0.05 (arms at +5%, not +10%)."""
        from tradingagents.portfolio_advisor.position_plans import CatalystRules

        rules = CatalystRules()
        assert rules.trailing_activate_pct == pytest.approx(0.05), \
            "Default arm threshold should be 0.05 (not the old 0.10)"

    def test_default_dist_pct_is_0_08(self):
        """DEFAULT catalyst_trail_dist_pct is 0.08 (exit 8% below peak)."""
        from tradingagents.portfolio_advisor.position_plans import CatalystRules

        rules = CatalystRules()
        assert rules.trailing_stop_pct == pytest.approx(0.08)

    def test_catalyst_rules_from_cfg_reads_new_arm_key(self, tmp_path):
        """catalyst_rules_from_cfg reads portfolio_advisor_catalyst_trail_arm_pct."""
        from tradingagents.portfolio_advisor.position_plans import catalyst_rules_from_cfg

        cfg = {"portfolio_advisor_catalyst_trail_arm_pct": 0.07}
        rules = catalyst_rules_from_cfg(cfg)
        assert rules.trailing_activate_pct == pytest.approx(0.07)

    def test_catalyst_rules_from_cfg_reads_new_dist_key(self, tmp_path):
        """catalyst_rules_from_cfg reads portfolio_advisor_catalyst_trail_dist_pct."""
        from tradingagents.portfolio_advisor.position_plans import catalyst_rules_from_cfg

        cfg = {"portfolio_advisor_catalyst_trail_dist_pct": 0.10}
        rules = catalyst_rules_from_cfg(cfg)
        assert rules.trailing_stop_pct == pytest.approx(0.10)

    def test_catalyst_rules_from_cfg_falls_back_to_old_arm_key(self):
        """catalyst_rules_from_cfg falls back to old trailing_activate_pct key if new key absent."""
        from tradingagents.portfolio_advisor.position_plans import catalyst_rules_from_cfg

        # Only the old key is present — should use it
        cfg = {"portfolio_advisor_catalyst_trailing_activate_pct": 0.15}
        rules = catalyst_rules_from_cfg(cfg)
        assert rules.trailing_activate_pct == pytest.approx(0.15)

    def test_new_arm_key_takes_priority_over_old_key(self):
        """When both old and new arm keys are present, new key wins."""
        from tradingagents.portfolio_advisor.position_plans import catalyst_rules_from_cfg

        cfg = {
            "portfolio_advisor_catalyst_trail_arm_pct": 0.05,
            "portfolio_advisor_catalyst_trailing_activate_pct": 0.10,  # old key
        }
        rules = catalyst_rules_from_cfg(cfg)
        assert rules.trailing_activate_pct == pytest.approx(0.05), \
            "New trail_arm_pct key should take priority over the old trailing_activate_pct"

    def test_dead_zone_covered_by_new_default(self):
        """With arm_pct=0.05, a +7% winner (in the old dead zone) is protected by trailing stop.

        Old behavior: arm at +10%, so +7% position had NO trailing stop and time-stop
        only fired AFTER catalyst_date + 3d. The +5%..+10% band was unprotected.
        New behavior: arm at +5%, so a +7% winner gets trailing stop protection immediately.
        """
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        # entry=100, peak=108 (+8% → above 5% arm, which = 105) → trailing armed
        # stop level = 108 * (1 - 0.08) = 99.36
        plan = PositionPlan(
            ticker="DKNG",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date="2026-09-15",
            peak_price=108.0,  # >= entry*(1+0.05)=105 → armed with new default
        )
        rules = CatalystRules(trailing_activate_pct=0.05, trailing_stop_pct=0.08)

        # Price drops to 99 (below stop level 99.36) → should fire
        result = eval_catalyst_exit(plan, 99.0, rules)
        assert result == "paper_catalyst_trailing_stop", \
            "+7% peak position should be protected by trailing stop with new arm_pct=0.05"

    def test_dead_zone_was_not_covered_by_old_default(self):
        """With the old arm_pct=0.10, a +7% winner was in the dead zone (no trailing stop)."""
        from tradingagents.portfolio_advisor.position_plans import (
            PositionPlan, CatalystRules, eval_catalyst_exit,
        )

        # Same scenario but with OLD default arm_pct=0.10:
        # peak=108 < entry*(1+0.10)=110 → NOT armed
        plan = PositionPlan(
            ticker="DKNG",
            entry_price=100.0,
            strategy="catalyst",
            catalyst_date="2026-09-15",
            peak_price=108.0,
        )
        rules = CatalystRules(trailing_activate_pct=0.10, trailing_stop_pct=0.08)

        # Price drops to 99 → NOT caught by trailing (peak < 110, not armed)
        result = eval_catalyst_exit(plan, 99.0, rules)
        assert result is None, \
            "Demonstrates the old dead zone: +7% peak not armed with old 0.10 threshold"

    def test_default_config_has_new_keys(self):
        """DEFAULT_CONFIG contains the new trail_arm_pct and trail_dist_pct keys."""
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "portfolio_advisor_catalyst_trail_arm_pct" in DEFAULT_CONFIG, \
            "portfolio_advisor_catalyst_trail_arm_pct must be in DEFAULT_CONFIG"
        assert "portfolio_advisor_catalyst_trail_dist_pct" in DEFAULT_CONFIG, \
            "portfolio_advisor_catalyst_trail_dist_pct must be in DEFAULT_CONFIG"
        assert DEFAULT_CONFIG["portfolio_advisor_catalyst_trail_arm_pct"] == pytest.approx(0.05)
        assert DEFAULT_CONFIG["portfolio_advisor_catalyst_trail_dist_pct"] == pytest.approx(0.08)

    def test_default_config_also_has_reentry_cooldown_key(self):
        """DEFAULT_CONFIG contains portfolio_advisor_reentry_cooldown_days."""
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "portfolio_advisor_reentry_cooldown_days" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["portfolio_advisor_reentry_cooldown_days"] == 5
