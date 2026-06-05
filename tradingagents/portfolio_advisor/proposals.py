"""Proposed-trade ledger: read/list/approve/reject the dry-run trade
proposals the PM emits via the ``propose_trade`` tool.

Each proposal is one JSONL row at
``~/.tradingagents/portfolio_advisor/proposed_trades.jsonl``:

  {ts, ticker, action, shares, approx_usd, target_price, sleeve, reason, status}

Status lifecycle: ``proposed`` → ``approved`` (human marked OK to execute)
or ``rejected`` (human said no) or ``executed`` (set later by a real
executor; nothing writes this status yet — placeholder for the future
browser-automation layer). All transitions are append-only via rewriting
the file under a short lock; we never lose history beyond explicit clears.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tradingagents.portfolio_advisor import state as pa_state

_STATUSES = ("proposed", "approved", "rejected", "executed", "cancelled")

# A fired rule (e.g. DOUBLE_FROM_ENTRY) often gets re-proposed as either "sell"
# or "trim" cycle after cycle. Both express the same intent — reduce the
# position — so we dedup on the SIDE, not the exact verb, to stop the same
# decision piling up as many near-identical pending rows.
_REDUCE = ("sell", "trim")
_INCREASE = ("buy", "add")


def _side(action: Any) -> str:
    a = str(action or "").strip().lower()
    if a in _REDUCE:
        return "reduce"
    if a in _INCREASE:
        return "increase"
    return a or "?"


def _path(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "proposed_trades.jsonl"


def _short_id(entry: Dict[str, Any]) -> str:
    """Stable short id (12 hex) derived from the original timestamp.

    Proposals don't get explicit ids today (the tool just writes one row),
    so we synthesize an id from the timestamp for CLI commands like
    ``approve <id>``. If a row already carries an explicit ``id``, use it.
    """
    if entry.get("id"):
        return str(entry["id"])
    ts = str(entry.get("ts") or "")
    return uuid.uuid5(uuid.NAMESPACE_URL, ts or json.dumps(entry, sort_keys=True)).hex[:12]


def load_all(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every proposal (oldest first), each enriched with a stable id."""
    p = _path(cfg)
    if not p.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        row.setdefault("status", "proposed")
        row["id"] = _short_id(row)
        rows.append(row)
    return rows


def _collapse_open(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one proposal per (ticker, side) — the newest by ts.

    Defends the display against any historical duplicate pile-up written before
    write-side dedup landed (and against concurrent writers racing the rewrite).
    """
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        key = ((r.get("ticker") or "").strip().upper(), _side(r.get("action")))
        cur = best.get(key)
        if cur is None or str(r.get("ts") or "") > str(cur.get("ts") or ""):
            best[key] = r
    return sorted(best.values(), key=lambda r: str(r.get("ts") or ""))


def list_pending(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Proposals still awaiting human review (status == 'proposed').

    Collapsed to one row per (ticker, side) so the same fired rule never shows
    up as a stack of duplicates in digests or `proposals list`.
    """
    pending = [r for r in load_all(cfg) if r.get("status") == "proposed"]
    return _collapse_open(pending)


def add(
    cfg: Dict[str, Any],
    *,
    ticker: str,
    action: str,
    shares: float = 0.0,
    approx_usd: float = 0.0,
    target_price: float = 0.0,
    sleeve: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """Record a proposal, superseding any existing OPEN proposal for the same
    ticker+side.

    This is the single writer for the ledger. Without the supersede step a rule
    that keeps firing (e.g. DOUBLE_FROM_ENTRY on a name that's still held) writes
    a fresh row every cycle and the digest nags about the same decision N times.
    """
    tk = (ticker or "").strip().upper()
    act = (action or "").strip().lower()
    side = _side(act)
    rows = load_all(cfg)
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if (
            r.get("status") == "proposed"
            and (r.get("ticker") or "").strip().upper() == tk
            and _side(r.get("action")) == side
        ):
            r["status"] = "cancelled"
            r["status_set_at"] = now
            r["status_note"] = "superseded by a newer proposal"
    entry: Dict[str, Any] = {
        "ts": now,
        "ticker": tk,
        "action": act,
        "shares": float(shares or 0),
        "approx_usd": float(approx_usd or 0),
        "target_price": float(target_price or 0),
        "sleeve": (str(sleeve).strip().lower() or None) if sleeve else None,
        "reason": (reason or "").strip()[:500],
        "status": "proposed",
    }
    rows.append(entry)
    save_all(cfg, rows)
    return entry


def reconcile_with_portfolio(cfg: Dict[str, Any], held_tickers: Iterable[str]) -> int:
    """Cancel OPEN reduce-side (sell/trim) proposals for names no longer held.

    Once a position is actually closed on eToro it drops out of the live book —
    so its exit proposals are moot and must stop being surfaced. This is what
    makes "I closed it" finally take effect: the next cycle that sees the name
    gone clears the stale proposal instead of nagging forever. Buy/add proposals
    are left untouched (closing one name doesn't invalidate an idea to open
    another). Returns the number cancelled.
    """
    held = {str(t).strip().upper() for t in (held_tickers or []) if str(t).strip()}
    # An empty book almost always means the eToro fetch failed — never treat
    # that as "everything was sold" and wipe every exit proposal.
    if not held:
        return 0
    rows = load_all(cfg)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for r in rows:
        if (
            r.get("status") == "proposed"
            and (r.get("ticker") or "").strip().upper() not in held
        ):
            r["status"] = "cancelled"
            r["status_set_at"] = now
            r["status_note"] = "auto-cancelled: position no longer held"
            n += 1
    if n:
        save_all(cfg, rows)
    return n


def auto_close_stale(cfg: Dict[str, Any], max_age_days: int = 14) -> int:
    """Cancel proposals that have been open too long with no action.

    This is a system-level operation — the PM does NOT have access to this
    function. It should run as a cron job or during action-check cleanup.

    Returns the number of proposals auto-closed.
    """
    from datetime import timedelta as _td

    rows = load_all(cfg)
    cutoff = datetime.now(timezone.utc) - _td(days=max_age_days)
    now_iso = datetime.now(timezone.utc).isoformat()
    n = 0
    for r in rows:
        if r.get("status") != "proposed":
            continue
        ts = r.get("ts", "")
        try:
            rt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if rt < cutoff:
                r["status"] = "cancelled"
                r["status_set_at"] = now_iso
                r["status_note"] = f"auto-cancelled: stale >{max_age_days}d"
                n += 1
        except (ValueError, TypeError):
            pass
    if n:
        save_all(cfg, rows)
    return n


def save_all(cfg: Dict[str, Any], rows: Iterable[Dict[str, Any]]) -> None:
    """Rewrite the ledger atomically (tmp + replace)."""
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            # Strip the derived id before writing — it's recomputed on load.
            out = {k: v for k, v in r.items() if k != "id"}
            fh.write(json.dumps(out) + "\n")
    tmp.replace(p)


def _set_status(
    cfg: Dict[str, Any],
    target_id: str,
    new_status: str,
    note: str = "",
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    if new_status not in _STATUSES:
        return False, None
    rows = load_all(cfg)
    hit: Optional[Dict[str, Any]] = None
    for r in rows:
        if r.get("id") == target_id:
            r["status"] = new_status
            r["status_set_at"] = datetime.now(timezone.utc).isoformat()
            if note:
                r["status_note"] = note[:300]
            hit = r
            break
    if hit is None:
        return False, None
    save_all(cfg, rows)
    return True, hit


def approve(cfg: Dict[str, Any], target_id: str, note: str = "") -> Tuple[bool, Optional[Dict[str, Any]]]:
    return _set_status(cfg, target_id, "approved", note=note)


def reject(cfg: Dict[str, Any], target_id: str, note: str = "") -> Tuple[bool, Optional[Dict[str, Any]]]:
    return _set_status(cfg, target_id, "rejected", note=note)


def cancel(cfg: Dict[str, Any], target_id: str, note: str = "") -> Tuple[bool, Optional[Dict[str, Any]]]:
    return _set_status(cfg, target_id, "cancelled", note=note)


def clear(cfg: Dict[str, Any], statuses: Optional[Iterable[str]] = None) -> int:
    """Remove rows whose status is in ``statuses`` (default: non-pending).
    Returns count removed."""
    keep_statuses = set(statuses or ()) or {"rejected", "cancelled", "executed"}
    rows = load_all(cfg)
    keep = [r for r in rows if r.get("status") not in keep_statuses]
    removed = len(rows) - len(keep)
    if removed:
        save_all(cfg, keep)
    return removed


def format_one_line(r: Dict[str, Any]) -> str:
    """Compact human display: 'PROPOSED  abc12345  CRWD  buy ~$300 — reason'."""
    sid = (r.get("id") or "")[:8]
    status = (r.get("status") or "proposed").upper()
    tk = (r.get("ticker") or "?").upper()
    act = (r.get("action") or "?").lower()
    if r.get("shares"):
        size = f"{float(r['shares']):g} sh"
    elif r.get("approx_usd"):
        size = f"~${float(r['approx_usd']):.0f}"
    else:
        size = "size TBD"
    reason = (r.get("reason") or "").strip()
    if len(reason) > 90:
        reason = reason[:87] + "…"
    return f"{status:8}  {sid}  {tk} {act} {size}" + (f" — {reason}" if reason else "")
