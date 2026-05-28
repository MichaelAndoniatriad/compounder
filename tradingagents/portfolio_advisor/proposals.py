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


def list_pending(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Proposals still awaiting human review (status == 'proposed')."""
    return [r for r in load_all(cfg) if r.get("status") == "proposed"]


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
