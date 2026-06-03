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
