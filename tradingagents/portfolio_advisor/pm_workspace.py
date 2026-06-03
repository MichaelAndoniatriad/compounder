"""WAT-style workspace for the Portfolio Manager: layered rules + memory + decisions.

Mirrors the user's CLAUDE.md / memory model so the PM's config reads the same way:

    PM_CLAUDE.md                 global standing rules (always loaded)
    rules/_portfolio.md          portfolio-wide rules (always loaded)
    rules/<TICKER>.md            per-ticker scoped rules (loaded for live tickers)
    memory/MEMORY.md             curated memory index (always loaded)
    memory/positions/<T>.md      per-position memory (loaded for live tickers)
    memory/decisions.jsonl       structured user decisions (machine-read for suppression)
    memory/decisions.md          human-readable mirror of decisions

Rules are *instructions*; memory is *history*; decisions are *what the human
chose* when the PM recommended an action (so it never re-nags a settled call).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.portfolio_advisor import state as pa_state

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Z0-9._-]")


def _safe_ticker(t: Any) -> str:
    return _SAFE.sub("", str(t or "").strip().upper())[:20]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    except OSError:
        return ""


# --- paths ------------------------------------------------------------------
def rules_dir(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "rules"


def memory_dir(cfg: Dict[str, Any]) -> Path:
    return pa_state.advisor_dir(cfg) / "memory"


def positions_dir(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "positions"


def strategies_dir(cfg: Dict[str, Any]) -> Path:
    return rules_dir(cfg) / "strategies"


def ep_trades_jsonl_path(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "strategies" / "ep_trades.jsonl"


def ep_trades_md_path(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "strategies" / "ep_trades.md"


def portfolio_rules_path(cfg: Dict[str, Any]) -> Path:
    return rules_dir(cfg) / "_portfolio.md"


def memory_index_path(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "MEMORY.md"


def decisions_jsonl_path(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "decisions.jsonl"


def decisions_md_path(cfg: Dict[str, Any]) -> Path:
    return memory_dir(cfg) / "decisions.md"


def ensure_workspace(cfg: Dict[str, Any]) -> None:
    rules_dir(cfg).mkdir(parents=True, exist_ok=True)
    positions_dir(cfg).mkdir(parents=True, exist_ok=True)
    strategies_dir(cfg).mkdir(parents=True, exist_ok=True)
    (memory_dir(cfg) / "strategies").mkdir(parents=True, exist_ok=True)


# --- rules ------------------------------------------------------------------
def load_portfolio_rules(cfg: Dict[str, Any], *, cap: int = 3000) -> str:
    body = _read(portfolio_rules_path(cfg))
    return body[:cap]


def load_scoped_rules(cfg: Dict[str, Any], tickers: List[str], *, cap: int = 4000) -> str:
    out: List[str] = []
    for t in sorted({_safe_ticker(x) for x in (tickers or []) if str(x).strip()}):
        body = _read(rules_dir(cfg) / f"{t}.md")
        if body:
            out.append(f"### {t}\n{body}")
    return ("\n\n".join(out))[:cap]


def update_scoped_rule(cfg: Dict[str, Any], ticker: str, rule_text: str) -> str:
    ensure_workspace(cfg)
    t = _safe_ticker(ticker)
    if not t:
        return ""
    p = rules_dir(cfg) / f"{t}.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- {rule_text.strip()}  _(set {ts})_"
    existing = _read(p)
    content = (existing + "\n" + line + "\n") if existing else f"# {t} — scoped rules\n{line}\n"
    p.write_text(content, encoding="utf-8")
    return str(p)


# --- memory -----------------------------------------------------------------
def load_memory_index(cfg: Dict[str, Any], *, cap: int = 4000) -> str:
    return _read(memory_index_path(cfg))[:cap]


def load_position_memory(
    cfg: Dict[str, Any], tickers: List[str], *, per_cap: int = 1200, total_cap: int = 6000
) -> str:
    out: List[str] = []
    for t in sorted({_safe_ticker(x) for x in (tickers or []) if str(x).strip()}):
        body = _read(positions_dir(cfg) / f"{t}.md")
        if body:
            out.append(f"### {t}\n{body[-per_cap:]}")
    return ("\n\n".join(out))[:total_cap]


def update_position_memory(cfg: Dict[str, Any], ticker: str, note: str) -> str:
    ensure_workspace(cfg)
    t = _safe_ticker(ticker)
    note = (note or "").strip()
    if not t or not note:
        return ""
    p = positions_dir(cfg) / f"{t}.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- {ts}: {note}"
    existing = _read(p)
    content = (existing + "\n" + entry + "\n") if existing else f"# {t} — position memory\n{entry}\n"
    # Keep per-file bounded; drop oldest bullet lines past a cap.
    if len(content) > 8000:
        head, *rest = content.splitlines()
        content = head + "\n" + "\n".join(rest[-60:]) + "\n"
    p.write_text(content, encoding="utf-8")
    return str(p)


# --- decisions --------------------------------------------------------------
def load_decisions(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = decisions_jsonl_path(cfg)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _save_decisions(cfg: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    ensure_workspace(cfg)
    p = decisions_jsonl_path(cfg)
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    _render_decisions_md(cfg, rows)


def _expired(row: Dict[str, Any]) -> bool:
    until = row.get("until")
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


def record_decision(
    cfg: Dict[str, Any],
    *,
    ticker: str,
    recommended: str,
    choice: str,
    reason: str = "",
    until: Optional[str] = None,
    source: str = "pm",
) -> Dict[str, Any]:
    """Record what the human chose when the PM recommended an action.

    Supersedes any prior active decision for the same ticker so the latest
    call always wins.
    """
    t = _safe_ticker(ticker)
    rows = load_decisions(cfg)
    for r in rows:
        if r.get("ticker") == t and r.get("status") == "active":
            r["status"] = "superseded"
            r["superseded_at"] = _now_iso()
    row = {
        "id": uuid.uuid4().hex[:16],
        "ticker": t,
        "recommended": str(recommended or "").strip()[:80],
        "choice": str(choice or "").strip()[:80],
        "reason": str(reason or "").strip()[:400],
        "until": until,
        "status": "active",
        "created_at": _now_iso(),
        "source": source,
    }
    rows.append(row)
    _save_decisions(cfg, rows)
    return row


def clear_decision(cfg: Dict[str, Any], ticker: str) -> bool:
    t = _safe_ticker(ticker)
    rows = load_decisions(cfg)
    changed = False
    for r in rows:
        if r.get("ticker") == t and r.get("status") == "active":
            r["status"] = "cleared"
            r["cleared_at"] = _now_iso()
            changed = True
    if changed:
        _save_decisions(cfg, rows)
    return changed


def active_decision(cfg: Dict[str, Any], ticker: str) -> Optional[Dict[str, Any]]:
    t = _safe_ticker(ticker)
    found: Optional[Dict[str, Any]] = None
    for r in load_decisions(cfg):
        if r.get("ticker") == t and r.get("status") == "active" and not _expired(r):
            found = r  # last one wins
    return found


def load_decisions_block(
    cfg: Dict[str, Any], tickers: Optional[List[str]] = None, *, cap: int = 2500
) -> str:
    rows = [r for r in load_decisions(cfg) if r.get("status") == "active" and not _expired(r)]
    if tickers is not None:
        ts = {_safe_ticker(x) for x in tickers}
        rows = [r for r in rows if r.get("ticker") in ts]
    if not rows:
        return ""
    lines = [
        "Standing human decisions (RESPECT these — do not re-alert or re-nag a settled call "
        "unless the situation materially escalates, e.g. a deeper drawdown tier or a broken level):"
    ]
    for r in sorted(rows, key=lambda x: str(x.get("created_at") or "")):
        until = f", until {str(r.get('until'))[:10]}" if r.get("until") else ""
        lines.append(
            f"  {r['ticker']}: PM recommended {r.get('recommended')}, human chose "
            f"{r.get('choice')}{until} — reason: {r.get('reason') or '(none given)'} "
            f"[{str(r.get('created_at') or '')[:10]}]"
        )
    return "\n".join(lines)[:cap] + "\n\n"


def _render_decisions_md(cfg: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    p = decisions_md_path(cfg)
    lines = [
        "# User decisions",
        "",
        "Durable record of what the human chose when the PM recommended an action.",
        "Active decisions suppress re-alerts until the situation materially escalates.",
        "",
    ]
    for r in sorted(rows, key=lambda x: str(x.get("created_at") or ""), reverse=True):
        lines.append(f"## {str(r.get('created_at') or '')[:10]} — {r.get('ticker')} — {r.get('status')}")
        lines.append(f"- PM recommended: {r.get('recommended')}")
        lines.append(f"- Human chose: {r.get('choice')}")
        if r.get("until"):
            lines.append(f"- Until: {str(r.get('until'))[:10]}")
        lines.append(f"- Reason: {r.get('reason') or '(none given)'}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")

# --- strategies (rules/strategies/*.md, always-loaded into the PM prompt) ----
def load_strategies(cfg: Dict[str, Any], *, per_cap: int = 12000, total_cap: int = 16000) -> str:
    """Return concatenated text of all strategy docs under rules/strategies/.

    Strategy docs are big and authoritative. They are loaded in full (up to
    per-doc cap) on every PM cycle so the PM can answer about them without
    paraphrasing. Kept under total_cap to bound prompt growth.
    """
    d = strategies_dir(cfg)
    if not d.is_dir():
        return ""
    parts: List[str] = []
    for p in sorted(d.glob("*.md")):
        body = _read(p)
        if not body:
            continue
        parts.append(f"### Strategy: {p.stem}\n{body[:per_cap]}")
    return ("\n\n".join(parts))[:total_cap]


# --- EP trade journal (Section 10 of episodic_pivot.md) --------------------
def _load_ep_trades(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = ep_trades_jsonl_path(cfg)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _save_ep_trades(cfg: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    ensure_workspace(cfg)
    p = ep_trades_jsonl_path(cfg)
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    _render_ep_trades_md(cfg, rows)


def log_ep_trade(
    cfg: Dict[str, Any],
    *,
    ticker: str,
    tier: str,
    catalyst: str,
    entry_date: str,
    entry_price: float,
    stop_price: float,
    shares: float,
    usd_position: float,
    usd_risk: float,
    sector: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """Append one OPEN EP trade to the journal."""
    row = {
        "id": uuid.uuid4().hex[:16],
        "ticker": _safe_ticker(ticker),
        "tier": str(tier or "").strip()[:8],
        "catalyst": str(catalyst or "").strip()[:300],
        "sector": str(sector or "").strip()[:60],
        "entry_date": str(entry_date or "").strip(),
        "entry_price": round(float(entry_price), 4),
        "stop_price": round(float(stop_price), 4),
        "shares": round(float(shares), 4),
        "usd_position": round(float(usd_position), 2),
        "usd_risk": round(float(usd_risk), 2),
        "status": "open",
        "notes": str(notes or "").strip()[:600],
        "created_at": _now_iso(),
    }
    rows = _load_ep_trades(cfg)
    rows.append(row)
    _save_ep_trades(cfg, rows)
    return row


def update_ep_trade(
    cfg: Dict[str, Any],
    *,
    trade_id: str = "",
    ticker: str = "",
    exit_date: str = "",
    exit_price: Optional[float] = None,
    exit_reason: str = "",
    pnl_usd: Optional[float] = None,
    r_multiple: Optional[float] = None,
    note: str = "",
    new_stop: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Update an existing EP trade. Match by id, else by latest open trade for ticker."""
    rows = _load_ep_trades(cfg)
    target = None
    tid = (trade_id or "").strip()
    if tid:
        for r in rows:
            if r.get("id") == tid:
                target = r
                break
    if target is None and ticker:
        t = _safe_ticker(ticker)
        opens = [r for r in rows if r.get("ticker") == t and r.get("status") == "open"]
        target = opens[-1] if opens else None
    if target is None:
        return None
    if new_stop is not None:
        target["stop_price"] = round(float(new_stop), 4)
        target["stop_updated_at"] = _now_iso()
    if exit_price is not None:
        target["exit_price"] = round(float(exit_price), 4)
        target["exit_date"] = exit_date or _now_iso()[:10]
        target["exit_reason"] = exit_reason or ""
        target["status"] = "closed"
        target["closed_at"] = _now_iso()
        if pnl_usd is not None:
            target["pnl_usd"] = round(float(pnl_usd), 2)
        if r_multiple is not None:
            target["r_multiple"] = round(float(r_multiple), 3)
        # If the human did not supply pnl/r, compute from prices+risk where possible.
        if "pnl_usd" not in target and target.get("shares") and target.get("entry_price"):
            target["pnl_usd"] = round(
                (float(target["exit_price"]) - float(target["entry_price"])) * float(target["shares"]),
                2,
            )
        if "r_multiple" not in target and target.get("usd_risk"):
            try:
                target["r_multiple"] = round(float(target.get("pnl_usd", 0.0)) / float(target["usd_risk"]), 3)
            except (TypeError, ZeroDivisionError):
                pass
    if note:
        existing = target.get("notes") or ""
        target["notes"] = (existing + " | " if existing else "") + str(note)[:600]
    _save_ep_trades(cfg, rows)
    return target


def _session_n(entry_date: str) -> Optional[int]:
    """Approximate trading-session count from entry_date to today (skips weekends).

    Holidays not deducted -- the PM should treat this as an approximation and
    re-derive when the exact session matters for a checkpoint decision.
    """
    try:
        ed = datetime.fromisoformat(entry_date.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    if today < ed:
        return None
    days = 0
    d = ed
    while d < today:
        d = d + _td(days=1)
        if d.weekday() < 5:
            days += 1
    return days + 1  # session 1 = entry day


def load_ep_open_trades_block(cfg: Dict[str, Any], *, cap: int = 3000) -> str:
    """Render currently-open EP trades for the PM prompt, with session count."""
    rows = [r for r in _load_ep_trades(cfg) if r.get("status") == "open"]
    if not rows:
        return ""
    lines = ["Open EP trades (Episodic Pivot journal; apply Section 15 checkpoints by session):"]
    for r in sorted(rows, key=lambda x: str(x.get("entry_date") or "")):
        sn = _session_n(str(r.get("entry_date") or ""))
        sn_s = f"session ~{sn}" if sn else "session ?"
        lines.append(
            f"  {r.get('ticker')} [{r.get('tier')}] entry {r.get('entry_date','?')} @ "
            f"${float(r.get('entry_price') or 0):.2f}, stop ${float(r.get('stop_price') or 0):.2f}, "
            f"{float(r.get('shares') or 0):.2f}sh / ${float(r.get('usd_position') or 0):.0f}, "
            f"risk ${float(r.get('usd_risk') or 0):.0f} -- {sn_s} -- {str(r.get('catalyst') or '')[:80]}"
        )
    return ("\n".join(lines))[:cap] + "\n\n"


def load_ep_stats_block(cfg: Dict[str, Any], *, lookback: int = 50) -> str:
    """Render rolling EP stats (per Section 10) for the PM prompt."""
    closed = [r for r in _load_ep_trades(cfg) if r.get("status") == "closed"]
    if not closed:
        return ""
    closed = sorted(closed, key=lambda x: str(x.get("closed_at") or ""))[-lookback:]
    n = len(closed)
    wins = [r for r in closed if float(r.get("r_multiple") or 0) > 0]
    losses = [r for r in closed if float(r.get("r_multiple") or 0) <= 0]
    win_rate = (len(wins) / n) if n else 0.0
    avg_win = (sum(float(r.get("r_multiple") or 0) for r in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(float(r.get("r_multiple") or 0) for r in losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    lines = [
        f"EP stats (last {n} closed trades):",
        f"  win_rate={win_rate*100:.0f}%  avg_win={avg_win:+.2f}R  avg_loss={avg_loss:+.2f}R  "
        f"expectancy={expectancy:+.2f}R/trade",
    ]
    return "\n".join(lines) + "\n\n"


def _render_ep_trades_md(cfg: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    p = ep_trades_md_path(cfg)
    lines = ["# EP trade journal", "", "Mandatory Section 10 log. Auto-generated; edit the .jsonl directly.", ""]
    for r in sorted(rows, key=lambda x: str(x.get("entry_date") or ""), reverse=True):
        lines.append(f"## {r.get('entry_date','?')} {r.get('ticker','?')} [{r.get('tier','?')}] {r.get('status','?')}")
        lines.append(f"- Catalyst: {r.get('catalyst','')}")
        lines.append(f"- Entry ${r.get('entry_price')} stop ${r.get('stop_price')} -- {r.get('shares')}sh / ${r.get('usd_position')} (risk ${r.get('usd_risk')})")
        if r.get("status") == "closed":
            lines.append(
                f"- Exit ${r.get('exit_price')} on {r.get('exit_date','?')} -- {r.get('exit_reason','?')} "
                f"-- pnl ${r.get('pnl_usd','?')} ({r.get('r_multiple','?')}R)"
            )
        if r.get("notes"):
            lines.append(f"- Notes: {r.get('notes')}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
