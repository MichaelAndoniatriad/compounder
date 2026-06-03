"""Append-only JSONL event log for decisions, advisor events, and portfolio changes.

No LLM writes here: callers append structured records after routines complete.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EVENT_WEIGHTS: Dict[str, int] = {
    "full_graph_decision": 10,
    "post_earnings_verdict": 10,
    "watchdog_critical_alert": 9,
    "watchdog_trim_alert": 8,
    "watchdog_high_alert": 7,
    "watchdog_trigger_change": 8,
    "plan_validation_override": 8,
    "outcome_recorded": 8,
    "single_model_analysis": 7,
    "advisor_plan": 5,
    "portfolio_book_changed": 5,
    "pending_outcome_30d": 4,
    "partial_close_outcome": 4,
    "bootstrap_position_failed": 3,
    "portfolio_bootstrap_complete": 6,
    "advisor_pm_cycle": 8,
    "pm_validation_override": 8,
    "candidate_status_changed": 6,
    "advisor_replan_skipped": 1,
}


def _event_weight(event_type: Any) -> int:
    return int(EVENT_WEIGHTS.get(str(event_type or ""), 1))


def _default_event_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("event_log_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    mem = cfg.get("memory_log_path")
    if isinstance(mem, str) and mem.strip():
        return Path(mem).expanduser().parent / "events.jsonl"
    return Path.home() / ".tradingagents" / "memory" / "events.jsonl"


def append_event(cfg: Dict[str, Any], record: Dict[str, Any]) -> None:
    """Append one JSON object as a single line. Adds ``timestamp`` if missing."""
    path = _default_event_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(record)
    if "timestamp" not in row:
        row["timestamp"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("event log append failed: %s", e)


def _iter_events(cfg: Dict[str, Any], max_lines: int = 5000) -> List[Dict[str, Any]]:
    path = _default_event_path(cfg)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.splitlines()
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _event_day(row: Dict[str, Any]) -> Optional[str]:
    ts = row.get("timestamp")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


_COMPACT_EVENT_TYPES = frozenset(
    {"full_graph_decision", "single_model_analysis", "outcome_recorded", "candidate_status_changed"}
)

_COMPACT_ET_ALIAS = {
    "full_graph_decision": "deep",
    "single_model_analysis": "quick",
    "outcome_recorded": "outcome",
    "candidate_status_changed": "candidate",
}


def format_recent_events_for_ticker(
    cfg: Dict[str, Any],
    ticker: str,
    *,
    days: int = 30,
    max_events: int = 25,
    min_include: Optional[List[str]] = None,
    min_include_n: int = 1,
    compact: bool = False,
) -> str:
    """Human-readable tail for prompt injection (no LLM).

    Rows are sorted by severity weight then recency, then capped at ``max_events``.
    Types listed in ``min_include`` contribute their latest ``min_include_n`` rows
    even when older than the lookback window.
    When ``compact`` is True, only key event types are included and each row is
    formatted as a single line with ticker, verdict, date, and outcome only.
    """
    sym = ticker.strip().upper()
    if not sym:
        return ""
    cutoff_d = (datetime.now(timezone.utc).date() - timedelta(days=int(days)))
    want_min = min_include or [
        "full_graph_decision",
        "post_earnings_verdict",
        "outcome_recorded",
    ]
    want_set = {str(x).strip() for x in want_min if str(x).strip()}

    pool: List[Dict[str, Any]] = []
    for row in reversed(_iter_events(cfg, max_lines=12000)):
        if str(row.get("ticker", "")).strip().upper() != sym:
            continue
        pool.append(row)

    per_type: Dict[str, List[Dict[str, Any]]] = {k: [] for k in want_set}
    for row in pool:
        et = str(row.get("event_type") or "")
        if et in per_type and len(per_type[et]) < int(min_include_n):
            per_type[et].append(row)

    forced: List[Dict[str, Any]] = []
    for _et, lst in per_type.items():
        forced.extend(lst)

    windowed: List[Dict[str, Any]] = []
    for row in pool:
        ed = _event_day(row)
        if ed:
            try:
                if datetime.strptime(ed, "%Y-%m-%d").date() < cutoff_d:
                    continue
            except ValueError:
                pass
        windowed.append(row)

    def _rk(r: Dict[str, Any]) -> str:
        ts = str(r.get("timestamp") or "")
        et = str(r.get("event_type") or "")
        return f"{ts}\t{et}"

    seen: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for row in forced + windowed:
        k = _rk(row)
        if k in seen:
            continue
        seen.add(k)
        merged.append(row)

    def _ts_sort(r: Dict[str, Any]) -> str:
        return str(r.get("timestamp") or "")

    merged.sort(
        key=lambda r: (_event_weight(r.get("event_type")), _ts_sort(r)),
        reverse=True,
    )
    rows = merged[: int(max_events)]
    if not rows:
        return ""

    if compact:
        compact_rows = [r for r in rows if str(r.get("event_type") or "") in _COMPACT_EVENT_TYPES]
        if not compact_rows:
            return ""
        lines = [f"Recent events for {sym} (compact):"]
        for r in compact_rows:
            et = str(r.get("event_type") or "")
            alias = _COMPACT_ET_ALIAS.get(et, et)
            kd = r.get("key_data") or {}
            if et == "outcome_recorded":
                pnl = kd.get("pnl_pct")
                align = str(r.get("outcome") or kd.get("outcome_alignment") or "?")
                verdict = f"align={align}" + (f" pnl={float(pnl):+.1f}%" if pnl is not None else "")
            elif et == "candidate_status_changed":
                verdict = f"{kd.get('status', '?')} via {kd.get('source', '?')}: {str(kd.get('next_action') or '')[:60]}"
            else:
                verdict = str(kd.get("excerpt") or "").replace("\n", " ").strip()[:80]
            lines.append(f"{str(r.get('timestamp') or '')[:10]} | {alias} | {verdict}")
        return "\n".join(lines)

    lines = [f"Recent event log for {sym} (weighted tail, cap {max_events}):"]
    for r in rows:
        et = r.get("event_type", "?")
        kd = r.get("key_data") or {}
        kd_s = json.dumps(kd, ensure_ascii=False)
        if len(kd_s) > 220:
            kd_s = kd_s[:217] + "..."
        oc = r.get("outcome")
        tail = f" outcome={json.dumps(oc, ensure_ascii=False)}" if oc is not None else ""
        lines.append(f"{r.get('timestamp', '')} | {et} | w={_event_weight(et)} | {kd_s}{tail}")
    return "\n".join(lines)


def load_events_for_review(cfg: Dict[str, Any], *, days: int = 120) -> List[Dict[str, Any]]:
    """Events within the last ``days`` for monthly review."""
    cutoff_d = (datetime.now(timezone.utc).date() - timedelta(days=int(days)))
    out: List[Dict[str, Any]] = []
    for row in _iter_events(cfg, max_lines=20000):
        ed = _event_day(row)
        if not ed:
            continue
        try:
            if datetime.strptime(ed, "%Y-%m-%d").date() < cutoff_d:
                continue
        except ValueError:
            continue
        out.append(row)
    return out
