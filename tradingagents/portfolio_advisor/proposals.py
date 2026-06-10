"""Proposed-trade ledger: record, auto-execute, and track PM proposals.

Each proposal is one JSONL row at
``~/.tradingagents/portfolio_advisor/proposed_trades.jsonl``:

  {ts, ticker, action, shares, approx_usd, target_price, sleeve, reason,
   catalyst_date, confidence, status, status_set_at, status_note}

Status lifecycle:
  ``proposed``  → new row, waiting for execution
  ``executed``  → mirrored onto Alpaca paper book (autonomous mode)
  ``cancelled`` → superseded by newer proposal, skipped by executor,
                  or auto-closed as stale (auto_close_stale)
  ``approved``  → human explicitly approved (advisory mode)
  ``rejected``  → human explicitly rejected

The dedup gate only considers ``proposed`` rows — executed/cancelled rows
release the gate so new tranches and re-entries work correctly.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
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
    catalyst_date: str = "",
    confidence: float = 0.0,
) -> Dict[str, Any]:
    """Record a proposal, superseding any existing OPEN proposal for the same
    ticker+side.

    catalyst_date (ISO, catalyst trades only) drives the concrete exit date on
    the ticket: exit time_stop_days after it, or the 30-day cap if absent.

    This is the single writer for the ledger. Without the supersede step a rule
    that keeps firing (e.g. DOUBLE_FROM_ENTRY on a name that's still held) writes
    a fresh row every cycle and the digest nags about the same decision N times.
    """
    tk = (ticker or "").strip().upper()
    act = (action or "").strip().lower()
    side = _side(act)
    rows = load_all(cfg)
    now = datetime.now(timezone.utc).isoformat()
    prior = None  # the open proposal this one replaces, if any
    for r in rows:
        if (
            r.get("status") == "proposed"
            and (r.get("ticker") or "").strip().upper() == tk
            and _side(r.get("action")) == side
        ):
            prior = dict(r)
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
        "catalyst_date": (catalyst_date or "").strip() or None,
        "reason": (reason or "").strip()[:500],
        # 0.0 means "not stated" — the paper executor sizes mid-range for it.
        "confidence": float(confidence) if confidence else None,
        "status": "proposed",
    }
    rows.append(entry)
    save_all(cfg, rows)
    # Push a clean, executable ticket to the human ONLY when this is genuinely
    # new or materially changed — so a rule that re-fires every cycle (same side,
    # same size) doesn't re-spam. Action-only: no proposal, no message.
    if _proposal_is_new_or_changed(prior, entry) and bool(
        cfg.get("portfolio_advisor_action_tickets", True)
    ):
        _send_action_ticket(cfg, entry)
    # Mirror onto the Alpaca PAPER book — only a genuinely NEW proposal trades;
    # a superseding restatement of an open proposal must not double the position.
    # After execution, mark the row with the machine-readable result so the dedup
    # gate sees executed/cancelled rows as "resolved" and releases for re-entry.
    if prior is None:
        try:
            from tradingagents.integrations.alpaca import executor as _alpaca

            result = _alpaca.execute_proposal(cfg, entry)
            # result is {"status": "executed"|"skipped"|"error"|"disabled", "detail": str}
            ex_status = result.get("status", "disabled") if isinstance(result, dict) else "disabled"
            ex_detail = result.get("detail", "") if isinstance(result, dict) else str(result or "")
            now = datetime.now(timezone.utc).isoformat()
            # Reload rows from disk in case they were modified by the executor
            # (e.g. position plan save), then apply status update.
            rows_fresh = load_all(cfg)
            for r in rows_fresh:
                if r.get("ts") == entry["ts"] and r.get("ticker") == entry["ticker"]:
                    if ex_status == "executed":
                        r["status"] = "executed"
                        r["status_set_at"] = now
                        r["status_note"] = ex_detail[:300]
                    elif ex_status in ("skipped", "error"):
                        # A skipped/errored intent must not block future proposals
                        # for the same ticker+side (e.g. re-entry after exit).
                        r["status"] = "cancelled"
                        r["status_set_at"] = now
                        r["status_note"] = ex_detail[:300]
                    # "disabled" leaves status as "proposed" (human-advisory mode)
                    break
            save_all(cfg, rows_fresh)
        except Exception:
            pass
    return entry


def _proposal_is_new_or_changed(prior: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    """True if this proposal is new, flips action, or changes size by >15%.

    Small price/size drift on a standing proposal is not worth re-pinging.
    """
    if not prior:
        return True
    if str(prior.get("action", "")).lower() != str(entry.get("action", "")).lower():
        return True
    pu = float(prior.get("approx_usd") or 0)
    eu = float(entry.get("approx_usd") or 0)
    if pu <= 0:
        return eu > 0
    return abs(eu - pu) / pu > 0.15


def format_action_ticket(cfg: Dict[str, Any], p: Dict[str, Any]) -> str:
    """Render a proposal as a clean, executable order ticket for Telegram.

    Answers the three questions every alert must: what + how much, at what price,
    and when to get out. Catalyst names carry the -8% stop; core names exit on
    thesis-break, a real sell-signal, or reallocation (no mechanical price target).
    """
    act = str(p.get("action", "")).lower()
    tk = p.get("ticker", "")
    sleeve = (p.get("sleeve") or "").lower()
    shares = float(p.get("shares") or 0)
    usd = float(p.get("approx_usd") or 0)
    px = float(p.get("target_price") or 0)
    reason = (p.get("reason") or "").strip()

    emoji = {"buy": "🟢", "add": "🟢", "sell": "🔴", "trim": "🟠"}.get(act, "•")
    size = f"{shares:g} sh" if shares else ""
    if usd:
        size = f"{size} (~${usd:,.0f})" if size else f"~${usd:,.0f}"
    at = f" @ ${px:.2f}" if px else ""
    sl = f" · {sleeve}" if sleeve else ""
    lines = [f"{emoji} {act.upper()} {tk} — {size}{at}{sl}".rstrip()]
    # Explicit timing, straight from the rulebook (entry + concrete exit).
    if act in ("sell", "trim"):
        lines.append("When: today — act on this now")
    elif act in ("buy", "add"):
        if sleeve == "catalyst":
            lines.append("Buy: full size today — time-sensitive catalyst")
            stop = f" (${px * 0.92:.2f})" if px else ""
            exit_by = _catalyst_exit_by(cfg, p.get("catalyst_date"))
            cap = int(cfg.get("portfolio_advisor_catalyst_max_hold_days", 30) or 30)
            if exit_by:
                lines.append(f"Sell by {exit_by} — or the −8% stop{stop}; {cap}-day hard cap")
            else:
                lines.append(f"Sell: −8% stop{stop} or {cap}-day cap (set a catalyst date for an exact exit day)")
        else:
            if usd:
                lines.append(f"Buy now: ~${usd / 3:,.0f} — that's ~1/3 of a ~${usd:,.0f} target position")
            else:
                lines.append("Buy now: ~1/3 of the target position")
            lines.append("Then 2 more thirds over the next 2–4 wks — add on dips or confirmation, not all at once")
            if px:
                lines.append(
                    f"Sell: +100% (${px * 2:.2f}, trim half) · −40% (${px * 0.60:.2f}) exit · "
                    "thesis-break · else 3–5 yr hold"
                )
            else:
                lines.append("Sell: +100% trim half · −40% exit · thesis-break · else 3–5 yr hold")
    if reason:
        lines.append(f"Why: {reason[:220]}")
    try:
        from tradingagents.portfolio_advisor.etoro_scan import account_mode

        if account_mode() == "alpaca":
            lines.append("autonomous — executing on Alpaca paper book; FYI only")
        else:
            lines.append("advisory — you execute on eToro")
    except Exception:
        lines.append("advisory — you execute on eToro")
    return "\n".join(lines)


def _catalyst_exit_by(cfg: Dict[str, Any], catalyst_date: Any) -> str:
    """Concrete exit day = catalyst_date + time_stop_days. '' if no usable date."""
    raw = str(catalyst_date or "").strip()
    if not raw:
        return ""
    try:
        d = datetime.fromisoformat(raw[:10])
    except ValueError:
        return ""
    days = int(cfg.get("portfolio_advisor_catalyst_time_stop_days", 3) or 3)
    out = d + timedelta(days=days)
    return f"{out:%b} {out.day}"


def _send_action_ticket(cfg: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Deliver one order ticket. Never raises."""
    try:
        from tradingagents.portfolio_advisor import messaging

        subject = f"{str(entry.get('action','')).upper()} {entry.get('ticker','')}".strip()
        messaging.send_advisor_message(cfg, subject, format_action_ticket(cfg, entry), urgent=True)
    except Exception:
        pass


def _send_stand_down(cfg: Dict[str, Any], entry: Dict[str, Any], reason: str) -> None:
    """Notify the user that a previously sent ticket is now void. Never raises."""
    try:
        from tradingagents.portfolio_advisor import messaging

        tk = (entry.get("ticker") or "").upper()
        act = (entry.get("action") or "").upper()
        subject = f"Stand down on {tk} {act}"
        body = f"⏹ Stand down on {tk} {act}\n{reason} — ignore the earlier ticket."
        messaging.send_advisor_message(cfg, subject, body, urgent=False)
    except Exception:
        pass


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
            # ONLY reduce-side (sell/trim) proposals are mooted by a name leaving
            # the book. A BUY/ADD is precisely FOR a name not held yet — never
            # cancel it just because the position doesn't exist (that silently
            # killed every catalyst entry 16s after it was proposed).
            and _side(r.get("action")) == "reduce"
            and (r.get("ticker") or "").strip().upper() not in held
        ):
            r["status"] = "cancelled"
            r["status_set_at"] = now
            r["status_note"] = "auto-cancelled: position no longer held"
            _send_stand_down(cfg, r, "Position no longer held")
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
