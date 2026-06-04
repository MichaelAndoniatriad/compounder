"""JSON persistence for portfolio advisor jobs and scan metadata."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List


def advisor_dir(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("portfolio_advisor_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    home = os.path.join(os.path.expanduser("~"), ".tradingagents", "portfolio_advisor")
    return Path(home)


def state_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("portfolio_advisor_state_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return advisor_dir(cfg) / "state.json"


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "first_scan_complete": False,
        "last_init_iso": None,
        "last_weekly_scan_iso": None,
        "last_weekly_check_iso": None,
        "last_replan_iso": None,
        "last_replan_skip_iso": None,
        "last_catalyst_digest": None,
        "last_portfolio_text_hash": None,
        "last_bootstrap_iso": None,
        # Written when ``run_full_portfolio_bootstrap`` finishes (for UI / status).
        "last_bootstrap_summary": None,
        "last_portfolio_tickers": [],
        # Total eToro units per normalized ticker from last successful portfolio row fetch.
        "last_book_units_by_ticker": {},
        # Cash + total-value snapshot, updated at end of every PM cycle so the
        # next cycle can detect deposits/withdrawals (cash diff w/o matching
        # position-units change).
        "last_cash_balance": None,
        "last_total_value": None,
        "last_cash_snapshot_iso": None,
        "last_pm_cycle_iso": None,
        "last_pm_executive_prefix": None,
        # Per-ticker price-watchdog latches keyed by TICKER:
        #   {bucket, codes, gain_pct, drawdown_pct, first_seen, last_seen,
        #    status, pm_note, last_pm_handoff_iso}
        # Lets the watchdog hand off to the PM only on *change* instead of
        # DMing the user every 5 minutes.
        "watchdog_triggers": {},
        "jobs": [],
    }


def load_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = state_path(cfg)
    base = default_state()
    if not path.is_file():
        advisor_dir(cfg).mkdir(parents=True, exist_ok=True)
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    out = deepcopy(base)
    for k, v in data.items():
        if k == "jobs" and not isinstance(v, list):
            continue
        out[k] = v
    return out


def save_state(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def list_pending_jobs(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    jobs = state.get("jobs") or []
    return [j for j in jobs if isinstance(j, dict) and j.get("status") == "pending"]


def claim_jobs_for_run(state: Dict[str, Any], job_ids: List[str]) -> None:
    """Mark jobs as in_progress so concurrent cron ticks don't double-run them."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    for j in state.get("jobs") or []:
        if j.get("id") in job_ids and j.get("status") == "pending":
            j["status"] = "in_progress"
            j["claimed_at"] = now_iso


def reset_stalled_in_progress_jobs(state: Dict[str, Any], ttl_seconds: int) -> List[str]:
    """Reset ``in_progress`` jobs whose claim is older than ``ttl_seconds`` back to ``pending``.

    Without this, a crashed ``run_due`` leaves jobs stuck forever — they'd be
    skipped on every future tick. Returns the IDs that were reset.
    """
    from datetime import datetime, timezone

    if ttl_seconds <= 0:
        return []
    now = datetime.now(timezone.utc)
    reset_ids: List[str] = []
    for j in state.get("jobs") or []:
        if j.get("status") != "in_progress":
            continue
        claimed_raw = j.get("claimed_at") or j.get("started_at") or j.get("scheduled_at") or ""
        try:
            claimed_at = datetime.fromisoformat(str(claimed_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        if (now - claimed_at).total_seconds() >= ttl_seconds:
            j["status"] = "pending"
            j["reset_count"] = int(j.get("reset_count") or 0) + 1
            j["last_reset_iso"] = now.isoformat()
            j.pop("claimed_at", None)
            reset_ids.append(str(j.get("id") or ""))
    return reset_ids


def cancel_job(state: Dict[str, Any], job_id: str, reason: str = "") -> bool:
    jobs: List[Dict[str, Any]] = state.get("jobs") or []
    for j in jobs:
        if j.get("id") == job_id and j.get("status") == "pending":
            j["status"] = "cancelled"
            j["cancel_reason"] = reason
            return True
    return False


def cancel_all_pending(state: Dict[str, Any], reason: str) -> int:
    """Cancel pending jobs, but preserve jobs queued by PM tool calls.

    The planner only knows about holdings maintenance; wiping pm_tool_call
    jobs (candidate research the PM actively queued) destroys work the PM
    decided to do and prevents the catalyst sleeve from ever being researched.
    """
    n = 0
    for j in state.get("jobs") or []:
        if j.get("status") != "pending":
            continue
        if str(j.get("source") or "") == "pm_tool_call":
            continue  # preserve PM-initiated candidate research
        j["status"] = "cancelled"
        j["cancel_reason"] = reason
        n += 1
    return n


def append_jobs(state: Dict[str, Any], new_jobs: List[Dict[str, Any]]) -> None:
    jobs = state.setdefault("jobs", [])
    jobs.extend(new_jobs)


# --- Price-watchdog latches -------------------------------------------------
# Bucket severity ranking; higher wins when a ticker would match several.
_WATCHDOG_BUCKET_RANK = {"dd30_review": 1, "trim_half": 2, "dd40_mandatory_exit": 3}


def get_watchdog_triggers(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return the mutable watchdog-trigger map, creating it if absent."""
    wt = state.get("watchdog_triggers")
    if not isinstance(wt, dict):
        wt = {}
        state["watchdog_triggers"] = wt
    return wt


def upsert_watchdog_trigger(
    state: Dict[str, Any],
    ticker: str,
    *,
    bucket: str,
    codes: List[str],
    gain_pct: float,
    drawdown_pct: float,
    now_iso: str,
    entry: float = 0.0,
    price: float = 0.0,
    units: float = 0.0,
) -> Dict[str, Any]:
    """Insert or refresh one ticker latch.

    Returns {"changed": bool, "kind": new|escalated|deescalated|refresh,
    "prev_bucket": str|None}. "changed" is True only when the PM should be
    woken: a ticker newly enters a bucket, escalates, or de-escalates. A
    still-true unchanged trigger returns changed=False (no PM, no message).
    """
    tid = str(ticker or "").strip().upper()
    wt = get_watchdog_triggers(state)
    prev = wt.get(tid) if isinstance(wt.get(tid), dict) else None
    prev_bucket = str(prev.get("bucket")) if prev else None
    prev_status = str(prev.get("status")) if prev else None

    if prev is None:
        kind, changed = "new", True
        first_seen, status = now_iso, "open"
    else:
        first_seen = str(prev.get("first_seen") or now_iso)
        new_rank = _WATCHDOG_BUCKET_RANK.get(bucket, 0)
        old_rank = _WATCHDOG_BUCKET_RANK.get(prev_bucket or "", 0)
        if new_rank > old_rank:
            kind, changed, status = "escalated", True, "open"  # re-arm even if muted
        elif new_rank < old_rank:
            kind, changed, status = "deescalated", True, (prev_status or "open")
        else:
            kind, changed, status = "refresh", False, (prev_status or "open")

    wt[tid] = {
        "ticker": tid,
        "bucket": bucket,
        "codes": list(codes or []),
        "gain_pct": round(float(gain_pct or 0.0), 2),
        "drawdown_pct": round(float(drawdown_pct or 0.0), 2),
        "first_seen": first_seen,
        "last_seen": now_iso,
        "status": status,
        "entry": round(float(entry or 0.0), 4),
        "price": round(float(price or 0.0), 4),
        "units": round(float(units or 0.0), 6),
        # Baseline size when the trigger first latched, so we can tell when a
        # one-time trim (e.g. sell-half-on-double) was actually executed.
        "units_at_trigger": (
            prev.get("units_at_trigger") if (prev and prev.get("units_at_trigger")) else round(float(units or 0.0), 6)
        ),
        "double_trim_consumed": bool(prev.get("double_trim_consumed")) if prev else False,
        "pm_note": (prev.get("pm_note") if prev else "") or "",
        "last_pm_handoff_iso": (prev.get("last_pm_handoff_iso") if prev else None),
        # True once a trigger needs the PM but the hand-off has not yet
        # succeeded (e.g. PM was down). Retried each tick until delivered.
        "handoff_pending": bool(prev.get("handoff_pending")) if prev else False,
    }
    return {"changed": changed, "kind": kind, "prev_bucket": prev_bucket}


def clear_watchdog_triggers_not_in(
    state: Dict[str, Any], active_tickers: Any, now_iso: str
) -> List[str]:
    """Drop latches for tickers no longer triggering. Returns cleared tickers."""
    active = {str(t).strip().upper() for t in (active_tickers or [])}
    wt = get_watchdog_triggers(state)
    cleared = [t for t in list(wt.keys()) if t not in active]
    for t in cleared:
        wt.pop(t, None)
    return cleared


def mark_watchdog_handoff(state: Dict[str, Any], tickers: List[str], now_iso: str) -> None:
    """Stamp last_pm_handoff_iso on the tickers we just handed to the PM."""
    wt = get_watchdog_triggers(state)
    for t in tickers or []:
        row = wt.get(str(t).strip().upper())
        if isinstance(row, dict):
            row["last_pm_handoff_iso"] = now_iso


def set_watchdog_trigger_status(
    state: Dict[str, Any], ticker: str, action: str, note: str = ""
) -> bool:
    """PM-facing latch update. action: acknowledge|ack|mute|open|clear|note.

    clear drops the latch; acknowledge/mute/open set status; note just attaches
    a note without changing status. Returns True when something was applied.
    """
    tid = str(ticker or "").strip().upper()
    wt = get_watchdog_triggers(state)
    if action == "clear":
        return wt.pop(tid, None) is not None
    row = wt.get(tid)
    if not isinstance(row, dict):
        return False
    mapping = {"acknowledge": "acknowledged", "ack": "acknowledged", "mute": "muted", "open": "open"}
    if action in mapping:
        row["status"] = mapping[action]
    if note:
        row["pm_note"] = str(note)[:300]
    return True
