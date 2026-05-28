"""Execution-layer safety scaffold for real-money trade execution.

Every real-execution path MUST go through ``attempt_execute``. Bypassing this
wrapper is a bug. Today it's dry-run only — Phase 2 will wire the eToro
browser layer behind an explicit opt-in.

Triple gate for real execution:
  1. ``real=True`` passed to ``attempt_execute``.
  2. ``REAL_EXECUTION_ENABLED=yes`` env var set on the server.
  3. ``execute_trade`` in ``etoro_browser`` not raising NotImplementedError.

Safety limits (config keys, override per environment):
  - ``portfolio_advisor_exec_max_trades_per_day`` (default 4)
  - ``portfolio_advisor_exec_max_position_usd``  (default 500)
  - ``portfolio_advisor_exec_allowed_actions``   (default buy/sell/trim/add)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

from tradingagents.portfolio_advisor import state as pa_state

DEFAULT_MAX_TRADES_PER_DAY = 4
DEFAULT_MAX_POSITION_USD = 500.0
DEFAULT_ALLOWED_ACTIONS = frozenset({"buy", "sell", "trim", "add"})


@dataclass(frozen=True)
class ExecutionLimits:
    max_trades_per_day: int
    max_position_usd: float
    allowed_actions: FrozenSet[str]
    allowed_tickers: Optional[FrozenSet[str]] = None


def limits_from_cfg(cfg: Dict[str, Any]) -> ExecutionLimits:
    actions = cfg.get("portfolio_advisor_exec_allowed_actions")
    if isinstance(actions, (list, tuple)):
        action_set = frozenset(str(a).strip().lower() for a in actions if a)
    else:
        action_set = DEFAULT_ALLOWED_ACTIONS
    allowed_tickers = cfg.get("portfolio_advisor_exec_allowed_tickers")
    ticker_set: Optional[FrozenSet[str]] = None
    if isinstance(allowed_tickers, (list, tuple)) and allowed_tickers:
        ticker_set = frozenset(str(t).strip().upper() for t in allowed_tickers if t)
    return ExecutionLimits(
        max_trades_per_day=int(cfg.get("portfolio_advisor_exec_max_trades_per_day", DEFAULT_MAX_TRADES_PER_DAY)),
        max_position_usd=float(cfg.get("portfolio_advisor_exec_max_position_usd", DEFAULT_MAX_POSITION_USD)),
        allowed_actions=action_set,
        allowed_tickers=ticker_set,
    )


def _audit_path(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "execution_audit.jsonl"


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _count_today(cfg: Dict[str, Any]) -> int:
    p = _audit_path(cfg)
    if not p.is_file():
        return 0
    today = _today_iso()
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("ts", "")).startswith(today) and row.get("outcome") in ("dry_run", "executed"):
            n += 1
    return n


def _append_audit(cfg: Dict[str, Any], row: Dict[str, Any]) -> None:
    p = _audit_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def attempt_execute(
    cfg: Dict[str, Any],
    proposal: Dict[str, Any],
    *,
    real: bool = False,
) -> Tuple[bool, str]:
    """Validate a proposal against safety limits and (if ``real``) attempt
    real-money execution via the eToro browser layer.

    Returns ``(ok, message)``. Always writes an audit row.
    """
    limits = limits_from_cfg(cfg)
    action = (proposal.get("action") or "").strip().lower()
    ticker = (proposal.get("ticker") or "").strip().upper()
    approx_usd = float(proposal.get("approx_usd") or 0.0)
    shares = float(proposal.get("shares") or 0.0)

    # Hard guards
    if action not in limits.allowed_actions:
        return False, f"action {action!r} not in allowed set {sorted(limits.allowed_actions)!r}"
    if not ticker:
        return False, "missing ticker"
    if approx_usd > limits.max_position_usd:
        return False, f"position ~${approx_usd:.0f} exceeds limit ${limits.max_position_usd:.0f}"
    if limits.allowed_tickers and ticker not in limits.allowed_tickers:
        return False, f"ticker {ticker} not in allowlist"
    today_n = _count_today(cfg)
    if today_n >= limits.max_trades_per_day:
        return False, f"daily trade cap reached ({today_n}/{limits.max_trades_per_day})"

    # Real-mode triple gate
    if real and os.environ.get("REAL_EXECUTION_ENABLED", "").strip().lower() != "yes":
        return False, "real execution disabled (REAL_EXECUTION_ENABLED != 'yes')"

    audit: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "action": action,
        "approx_usd": approx_usd,
        "shares": shares,
        "proposal_id": proposal.get("id"),
        "real_attempted": bool(real),
        "outcome": "dry_run",
        "error": "",
    }

    if not real:
        _append_audit(cfg, audit)
        return True, "DRY-RUN ok (no trade placed)"

    # Real-mode path
    try:
        from tradingagents.portfolio_advisor.etoro_browser import execute_trade
        ok, msg = execute_trade(cfg, proposal)
        audit["outcome"] = "executed" if ok else "failed"
        audit["error"] = "" if ok else msg
        _append_audit(cfg, audit)
        return ok, msg
    except NotImplementedError as e:
        audit["outcome"] = "not_wired"
        audit["error"] = str(e)
        _append_audit(cfg, audit)
        return False, f"real execution not yet wired: {e}"
    except Exception as e:
        audit["outcome"] = "error"
        audit["error"] = str(e)[:300]
        _append_audit(cfg, audit)
        return False, f"exec error: {e}"


def read_audit_today(cfg: Dict[str, Any]) -> list:
    """Return today's audit rows (oldest first)."""
    p = _audit_path(cfg)
    if not p.is_file():
        return []
    today = _today_iso()
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("ts", "")).startswith(today):
            out.append(row)
    return out
