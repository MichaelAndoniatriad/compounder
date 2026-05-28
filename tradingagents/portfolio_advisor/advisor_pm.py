"""Advisor-level portfolio manager (PM): one structured LLM pass per cycle, logged to disk."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from tradingagents.agents.utils.event_log import append_event
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.dataflows.config import set_config
from tradingagents.integrations.etoro.clerk_bridge import _normalize_ticker
from tradingagents.llm_clients import create_llm_client
from tradingagents.portfolio_advisor import etoro_scan, evidence, state
from tradingagents.portfolio_advisor import position_plans as pos_plans
from tradingagents.portfolio_advisor import watchlist as watchlist_mod
from tradingagents.portfolio_advisor.models import AdvisorPMAppendJob, AdvisorPMCycleResult
from tradingagents.portfolio_advisor.prompt_limits import cfg_int as _pm_int

logger = logging.getLogger(__name__)
_pm_memory_lock = threading.Lock()
_ISO_DATE_RE = re.compile(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)")
_URGENCY_RE = re.compile(r"\b(urgent|urgently|deadline|before|by tomorrow|within 48h|within 48 hours|must)\b", re.I)

_FULL_GRAPH_COMPATIBLE_STANCES = {
    "Buy": {"buy", "add"},
    "Overweight": {"buy", "add", "hold", "watch"},
    "Hold": {"hold", "watch"},
    "Underweight": {"trim", "sell", "watch", "hold"},
    "Sell": {"sell", "trim"},
}

# ---------------------------------------------------------------------------
# PM_CLAUDE.md + PM_MEMORY.md — structured memory system for the PM
# ---------------------------------------------------------------------------

_PM_CLAUDE_DEFAULT = """\
# Portfolio Manager Standing Instructions

You are the Portfolio Manager (PM) for a personal investment portfolio. The human reads
your messages on a phone. You text like a friend — not a robot, not a report.

## Role
- Advisory only: you recommend, the human decides and executes.
- You hold the institutional memory: what we own, why, what the thesis is, what we learned.
- You also hold the **initiative**: idle cash, empty sleeves, and conviction sitting
  unused are costs. If you see an opening, propose it — don't wait to be asked.

## Voice — this matters
- Write like a person texting an update. Conversational sentences. No section headers,
  no "VERDICT" labels, no "REQUIRED ACTIONS" labels, no bullet lists in the executive
  summary. Just say what's going on in plain language.
- **End with a question when there's a real decision the human should make** — don't
  narrate when you can converse. ("Want me to start a small NET position?" beats
  "The catalyst sleeve is at 0%.")
- Acknowledge what you did since last cycle when relevant: "cleared the TEAM SELL",
  "queued a fresh PLTR check".
- Vary how you open. Don't start every message with the same word. Don't repeat the
  same sentence shape twice in a row.
- Short. The human glances at this. 2-4 sentences for routine updates. Up to ~8 for
  meaningful changes. Drop anything that isn't load-bearing.
- Do not list every position. Only mention a ticker if there's something to say about it.
- If you already told the human about a stance recently and nothing material has changed,
  just say "no change since last update" or skip the message entirely (set push_note to "").

## Hard rules — never break these
- Never invent deadlines, cut rules, stop-loss triggers, position sizing rules, or trading
  constraints that the human has not explicitly stated. Absent evidence is absent evidence.
- Never invent research findings, decision history, catalysts, earnings dates, or calendar dates.
- Never say a ticker "must" be cut by a date or "should" be trimmed by X% unless the human told you so.
- Pending jobs in the queue are SCHEDULED research jobs. They do not mean the ticker lacks
  analysis. Do not frame a future scheduled job as "urgently awaiting thesis results."
- Only flag urgency (push_note) when: (a) a stance materially changed since your last cycle,
  (b) a catalyst is within 48h, or (c) the human explicitly asked for something and it hasn't run.
- If you recommend closing or trimming, name the exact ticker, share count, and dollar value
  from the portfolio snapshot. Don't say "reduce exposure" without naming the position.

## Evidence discipline
- Base answers on the portfolio snapshot, pending jobs, completed research (including any
  candidate research present), PM memory, and explicit caller notes. If something depends
  on missing/stale info, say what's missing and queue follow-up research with append_jobs
  instead of guessing.
- When the watchlist has fresh research, evaluate it — use candidate_comparisons and, if
  a candidate is clearly stronger or fills an empty sleeve with cash to spare, propose a
  starter-sized buy stance.
- Use full_graph for new multi-agent research; single_model for a quick thesis check,
  weekly recap, post-earnings read, or routine monitor.

## When the human asks to run jobs sooner
- Use append_jobs to queue the tickers immediately. Tell the human what you queued in one line.
  They reply CANCEL to undo.

## Memory discipline
- memory_note: one tight paragraph briefing your next self. What mattered, what changed, what to watch.
"""

_PM_MEMORY_SEPARATOR = "\n---\n"


def _pm_claude_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "PM_CLAUDE.md"


def _pm_memory_structured_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "PM_MEMORY.md"


def _ensure_pm_claude_md(cfg: Dict[str, Any]) -> None:
    p = _pm_claude_path(cfg)
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_PM_CLAUDE_DEFAULT, encoding="utf-8")


def _read_pm_claude_md(cfg: Dict[str, Any]) -> str:
    p = _pm_claude_path(cfg)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_pm_memory_structured(cfg: Dict[str, Any]) -> str:
    p = _pm_memory_structured_path(cfg)
    if not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    cap = _pm_int(cfg, "portfolio_advisor_pm_memory_md_prompt_chars", 6000, 500, 30000)
    return text[-cap:] if len(text) > cap else text


def _write_pm_memory_update(cfg: Dict[str, Any], memory_note: str, trigger: str) -> None:
    note = (memory_note or "").strip()
    if not note:
        return
    p = _pm_memory_structured_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"## {ts} — {trigger}\n{note}\n"
    existing = ""
    if p.is_file():
        try:
            existing = p.read_text(encoding="utf-8")
        except OSError:
            pass
    new_content = (existing + _PM_MEMORY_SEPARATOR + entry) if existing else entry
    max_chars = _pm_int(cfg, "portfolio_advisor_pm_memory_md_max_chars", 40000, 2000, 200000)
    if len(new_content) > max_chars:
        new_content = new_content[-max_chars:]
        idx = new_content.find("\n## ")
        if idx > 0:
            new_content = new_content[idx + 1:]
    p.write_text(new_content, encoding="utf-8")


def pm_log_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("portfolio_advisor_pm_log_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return state.advisor_dir(cfg) / "pm_council.jsonl"


_PM_ADVISOR_SENTINEL = "<!-- TRADINGAGENTS_PM_ADVISOR_LOG -->"


def _pm_unified_memory(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("portfolio_advisor_pm_unified_memory", True))


def pm_memory_path(cfg: Dict[str, Any]) -> Path:
    """Markdown trail for PM cycles: unified ``memory_log_path`` or advisor-local ``pm_memory.md``."""
    if _pm_unified_memory(cfg):
        raw = cfg.get("memory_log_path")
        if isinstance(raw, str) and raw.strip():
            p = Path(raw).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        logger.warning(
            "portfolio_advisor_pm_unified_memory is on but memory_log_path is unset; "
            "using advisor_dir/pm_memory.md"
        )
    return state.advisor_dir(cfg) / "pm_memory.md"


def _compact_portfolio_snapshot(portfolio_text: str, rows: List[Dict[str, Any]]) -> str:
    """Build compact portfolio summary from raw eToro rows. Target: under 1200 chars."""
    first_line = (portfolio_text or "").split("\n")[0].strip()
    if not rows:
        return first_line or "(portfolio empty)"
    ubv_vals = [float(r["unitsBaseValueDollars"]) for r in rows if r.get("unitsBaseValueDollars") is not None]
    total_ubv = sum(ubv_vals) if ubv_vals else None
    items = []
    for r in rows:
        ticker = str(r.get("symbolFull") or "?").strip()
        side = "L" if r.get("isBuy") is True else ("S" if r.get("isBuy") is False else "?")
        item: Dict[str, Any] = {"t": ticker, "side": side}
        ubv = r.get("unitsBaseValueDollars")
        if ubv is not None:
            item["capital$"] = round(float(ubv), 2)
        if ubv is not None and total_ubv:
            item["pct"] = round(float(ubv) / total_ubv * 100, 1)
        units = r.get("units")
        if units is not None:
            try:
                item["units"] = round(float(units), 4)
            except (TypeError, ValueError):
                pass
        open_rate = r.get("openRate")
        if open_rate is not None:
            try:
                item["open$"] = round(float(open_rate), 4)
            except (TypeError, ValueError):
                pass
        init = r.get("initialAmountInDollars")
        upnl = r.get("unrealizedPnL")
        if upnl is not None:
            item["upnl$"] = round(float(upnl), 2)
        if ubv is not None and upnl is not None:
            try:
                item["current$"] = round(float(ubv) + float(upnl), 2)
            except (TypeError, ValueError):
                pass
        if upnl is not None and init is not None and float(init) != 0:
            item["upnl%"] = round(float(upnl) / float(init) * 100, 1)
        items.append(item)
    total_line = f"total_portfolio_value_usd={round(total_ubv, 2)}" if total_ubv else ""
    body = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    return f"{first_line}\n{total_line}\n{body}"


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v: Any) -> str:
    fv = _float_or_none(v)
    if fv is None:
        return "unknown value"
    return f"${fv:,.0f}" if abs(fv) >= 100 else f"${fv:,.2f}"


def _num(v: Any, digits: int = 4) -> str:
    fv = _float_or_none(v)
    if fv is None:
        return "?"
    s = f"{fv:.{digits}f}".rstrip("0").rstrip(".")
    return s or "0"


def _rows_for_ticker(rows: List[Dict[str, Any]], ticker: str) -> List[Dict[str, Any]]:
    want = _normalize_ticker(ticker)
    return [r for r in rows or [] if _normalize_ticker(str(r.get("symbolFull") or "")) == want]


def _mentioned_open_rates(text: str) -> set[str]:
    rates: set[str] = set()
    for raw in re.findall(r"(?<!\d)(?:\$)?(\d{1,4}(?:\.\d{1,4})?)(?!\d)", text or ""):
        try:
            rates.add(f"{float(raw):.4f}".rstrip("0").rstrip("."))
        except ValueError:
            continue
    return rates


def _format_close_instruction(ticker: str, stance: str, rationale: str, rows: List[Dict[str, Any]]) -> str:
    """Turn a sell/trim stance into exact lot-level instructions from the live book."""
    lots = _rows_for_ticker(rows, ticker)
    if not lots:
        return "No live eToro lots found for this ticker; verify manually before doing anything."

    mentioned = _mentioned_open_rates(rationale)
    selected = []
    if mentioned:
        for lot in lots:
            op = _float_or_none(lot.get("openRate"))
            op_s = f"{op:.4f}".rstrip("0").rstrip(".") if op is not None else ""
            if op_s in mentioned:
                selected.append(lot)
    if not selected and stance == "sell":
        selected = lots

    total_units = sum((_float_or_none(l.get("units")) or 0.0) for l in selected)
    total_capital = sum((_float_or_none(l.get("unitsBaseValueDollars")) or 0.0) for l in selected)
    total_current = sum(
        (_float_or_none(l.get("unitsBaseValueDollars")) or 0.0)
        + (_float_or_none(l.get("unrealizedPnL")) or 0.0)
        for l in selected
    )

    if stance == "sell":
        header = (
            f"Close {len(selected)} {ticker} position(s): {_num(total_units)} units, "
            f"about {_money(total_current)} current value ({_money(total_capital)} capital/base)."
        )
    else:
        if not selected:
            lot_count = len(lots)
            total_all_units = sum((_float_or_none(l.get("units")) or 0.0) for l in lots)
            total_all_capital = sum((_float_or_none(l.get("unitsBaseValueDollars")) or 0.0) for l in lots)
            total_all_current = sum(
                (_float_or_none(l.get("unitsBaseValueDollars")) or 0.0)
                + (_float_or_none(l.get("unrealizedPnL")) or 0.0)
                for l in lots
            )
            return (
                f"Trim requested but no exact lots/amount were specified. Open {ticker} lots: "
                f"{lot_count} position(s), {_num(total_all_units)} units, about {_money(total_all_current)} "
                f"current value ({_money(total_all_capital)} capital/base). "
                "Ask PM for exact trim size before executing."
            )
        header = (
            f"Trim by closing these {len(selected)} {ticker} lot(s): {_num(total_units)} units, "
            f"about {_money(total_current)} current value ({_money(total_capital)} capital/base)."
        )

    details = []
    for lot in selected[:8]:
        pid = lot.get("positionId")
        pid_s = f"id {pid}, " if pid not in (None, "") else ""
        capital = _float_or_none(lot.get("unitsBaseValueDollars"))
        upnl = _float_or_none(lot.get("unrealizedPnL"))
        current = (capital + upnl) if capital is not None and upnl is not None else None
        details.append(
            f"{pid_s}{_num(lot.get('units'))} units opened at {_money(lot.get('openRate'))}, "
            f"current {_money(current)}, capital/base {_money(capital)}, P/L {_money(upnl)}"
        )
    if len(selected) > 8:
        details.append(f"... plus {len(selected) - 8} more lot(s)")
    return header + " " + " | ".join(details)


def _trading_memory_digest_block(cfg: Dict[str, Any]) -> str:
    """3-line activity digest from the event log instead of raw memory tail."""
    if not _pm_unified_memory(cfg):
        return ""
    try:
        from tradingagents.agents.utils.event_log import _iter_events

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        fg_n = sm_n = 0
        last_action: Optional[str] = None
        last_ts = ""
        outcomes: List[str] = []
        for row in _iter_events(cfg, max_lines=5000):
            ts = str(row.get("timestamp") or "")
            if ts < cutoff:
                continue
            et = str(row.get("event_type") or "")
            ticker = str(row.get("ticker") or "").strip().upper()
            kd = row.get("key_data") or {}
            if et == "full_graph_decision":
                fg_n += 1
                if ts > last_ts:
                    last_ts = ts
                    excerpt = str(kd.get("excerpt") or "").replace("\n", " ")[:40].strip()
                    last_action = f"{ticker} deep ({ts[:10]}): {excerpt}"
            elif et == "single_model_analysis":
                sm_n += 1
                if ts > last_ts:
                    last_ts = ts
                    jt = str(kd.get("job_type") or "").strip()
                    last_action = f"{ticker} {jt} ({ts[:10]})"
            elif et == "outcome_recorded":
                pnl = kd.get("pnl_pct")
                if pnl is not None and len(outcomes) < 4:
                    outcomes.append(f"{ticker} {float(pnl):+.1f}%")
        if fg_n == 0 and sm_n == 0 and not last_action and not outcomes:
            return ""
        line1 = f"Last 30 days: {fg_n} full_graph runs, {sm_n} single_model runs."
        line2 = f"Last action: {last_action}." if last_action else "Last action: none recorded."
        line3 = f"Recent outcomes: {', '.join(outcomes)}." if outcomes else "Recent outcomes: none recorded."
        return f"Recent research activity:\n{line1}\n{line2}\n{line3}\n\n"
    except Exception as e:
        logger.debug("_trading_memory_digest_block failed: %s", e)
        return ""


def _recent_analysis_block(cfg: Dict[str, Any], tickers: List[str]) -> str:
    """Build a block of the most recent analysis verdict per ticker from the event log.

    Reads single_model_analysis and full_graph_decision events so the PM
    can see what research actually found, not just that jobs were queued.
    """
    try:
        from tradingagents.agents.utils.event_log import _iter_events
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        ticker_set = {t.strip().upper() for t in tickers if t.strip()}
        # latest result per ticker
        latest: Dict[str, Dict[str, Any]] = {}
        for row in _iter_events(cfg, max_lines=5000):
            ts = str(row.get("timestamp") or "")
            if ts < cutoff:
                continue
            et = str(row.get("event_type") or "")
            if et not in ("single_model_analysis", "full_graph_decision"):
                continue
            tk = str(row.get("ticker") or "").strip().upper()
            if tk not in ticker_set:
                continue
            if tk not in latest or ts > latest[tk].get("timestamp", ""):
                latest[tk] = row
        if not latest:
            return ""
        lines = ["Latest research results (from completed jobs):"]
        for tk in sorted(latest):
            row = latest[tk]
            kd = row.get("key_data") or {}
            excerpt = (kd.get("excerpt") or kd.get("decision") or "")[:300].replace("\n", " ").strip()
            jt = kd.get("job_type") or row.get("event_type", "")
            ts = str(row.get("timestamp") or "")[:10]
            lines.append(f"  {tk} [{jt} {ts}]: {excerpt}")
        return "\n".join(lines) + "\n\n"
    except Exception as e:
        logger.debug("_recent_analysis_block failed: %s", e)
        return ""


def _pm_evidence_context(
    cfg: Dict[str, Any],
    tickers: List[str],
    pending_jobs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return evidence.collect_pm_evidence(cfg, tickers, pending_jobs=pending_jobs)


def _text_dates(*parts: str) -> set[str]:
    found: set[str] = set()
    for part in parts:
        found.update(_ISO_DATE_RE.findall(str(part or "")))
    return found


def _parse_pm_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _has_newer_completed_evidence(
    stance_refs: List[str],
    *,
    graph_decision: Dict[str, Any],
    evidence_by_id: Dict[str, Dict[str, Any]],
) -> bool:
    graph_ts = _parse_pm_ts(graph_decision.get("timestamp"))
    if graph_ts is None:
        return False
    for ref in stance_refs:
        row = evidence_by_id.get(str(ref))
        if not row:
            continue
        if str(row.get("kind") or "") not in {"single_model_analysis", "full_graph_decision", "post_earnings_verdict", "deep_report_file"}:
            continue
        ref_ts = _parse_pm_ts(row.get("timestamp"))
        if ref_ts and ref_ts > graph_ts:
            return True
    return False


def _ensure_conflict_research_job(result: AdvisorPMCycleResult, ticker: str, graph_rating: str) -> bool:
    tk = ticker.strip().upper()
    if any(str(j.ticker or "").strip().upper() == tk for j in (result.append_jobs or [])):
        return False
    result.append_jobs.append(
        AdvisorPMAppendJob(
            ticker=tk,
            execution_tier="single_model",
            job_type="thesis_check",
            rationale=(
                f"Resolve advisor PM disagreement with latest full-graph decision "
                f"({graph_rating or 'unknown'})."
            ),
            source="pm_conflict",
            evidence_question=(
                f"Does newer evidence justify changing {tk} away from the latest "
                f"full-graph decision ({graph_rating or 'unknown'})?"
            ),
        )
    )
    return True


def _recent_full_graph_count(cfg: Dict[str, Any], ticker: str, days: int = 14) -> int:
    """How many full_graph decisions ran for this ticker in the recent window.

    Used to stop re-running deep research on a name whose verdict keeps
    conflicting with the thesis read — escalate to the human instead of looping.
    """
    try:
        from tradingagents.agents.utils.event_log import _iter_events
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        tk = ticker.strip().upper()
        n = 0
        for row in _iter_events(cfg, max_lines=5000):
            if str(row.get("timestamp") or "") < cutoff:
                continue
            if str(row.get("event_type") or "") != "full_graph_decision":
                continue
            if str(row.get("ticker") or "").strip().upper() == tk:
                n += 1
        return n
    except Exception:
        return 0


def _validate_pm_cycle_result(
    cfg: Dict[str, Any],
    result: AdvisorPMCycleResult,
    *,
    live_tickers: set[str],
    evidence_context: Dict[str, Any],
    pending_jobs: List[Dict[str, Any]],
    trigger: str,
) -> List[Dict[str, Any]]:
    """Enforce council governance rules after the LLM returns structured output."""
    overrides: List[Dict[str, Any]] = []
    by_ticker: Dict[str, List[str]] = evidence_context.get("by_ticker") or {}
    known_dates = set(evidence_context.get("known_dates") or [])
    stale_tickers: Dict[str, Dict[str, Any]] = evidence_context.get("stale_tickers") or {}
    latest_full_graph: Dict[str, Dict[str, Any]] = evidence_context.get("latest_full_graph_decisions") or {}
    evidence_by_id = {
        str(e.get("id")): e for e in (evidence_context.get("evidence") or []) if e.get("id")
    }
    allowed_refs = {str(e.get("id")) for e in (evidence_context.get("evidence") or []) if e.get("id")}

    # Keep portfolio-level refs citable even when the model omitted them.
    if not result.evidence_refs:
        refs = [str(e.get("id")) for e in (evidence_context.get("evidence") or [])[:5] if e.get("id")]
        result.evidence_refs = refs

    for stance in result.stances:
        tk = str(stance.ticker or "").strip().upper()
        stance.ticker = tk
        if tk and tk not in live_tickers:
            old = stance.stance
            stance.stance = "unknown"
            stance.rationale = (
                (stance.rationale or "").strip()
                + " Ticker is not in the live portfolio snapshot; use candidate_comparisons for non-held candidates."
            ).strip()
            overrides.append(
                {
                    "field": f"stances.{tk}.stance",
                    "action": "downgraded_non_live_ticker",
                    "from": old,
                }
            )
            continue
        existing = [r for r in (stance.evidence_refs or []) if str(r).strip()]
        valid_existing = [r for r in existing if r in allowed_refs or r.startswith(("caller:", "memory:", "pm_cycle:"))]
        if valid_existing != existing:
            overrides.append({"field": f"stances.{tk}.evidence_refs", "action": "removed_unknown_refs"})
        stance.evidence_refs = valid_existing
        if not stance.evidence_refs and tk in live_tickers:
            refs = list(by_ticker.get(tk) or [])[:3]
            stance.evidence_refs = refs
        stale_info = stale_tickers.get(tk)
        if (
            stale_info
            and stance.stance in {"buy", "sell", "trim", "add"}
            and not any(str(j.ticker or "").strip().upper() == tk for j in (result.append_jobs or []))
        ):
            reason = str(stale_info.get("reason") or "missing_research")
            source = "pm_stale_evidence" if reason == "stale_research" else "pm_missing_evidence"
            result.append_jobs.append(
                AdvisorPMAppendJob(
                    ticker=tk,
                    execution_tier="single_model",
                    job_type="thesis_check",
                    rationale=(
                        "Refresh evidence before the council takes an action stance. "
                        f"Reason: {reason}."
                    ),
                    source=source,
                    evidence_question=f"Is the current {tk} thesis still intact, weakening, or broken?",
                )
            )
            overrides.append(
                {
                    "field": f"append_jobs.{tk}",
                    "action": "queued_evidence_refresh",
                    "reason": reason,
                    "latest_ref": stale_info.get("latest_ref"),
                }
            )
        graph_decision = latest_full_graph.get(tk)
        graph_rating = str((graph_decision or {}).get("decision") or "").split("/", 1)[0].strip()
        compatible = _FULL_GRAPH_COMPATIBLE_STANCES.get(graph_rating)
        if (
            graph_decision
            and compatible
            and stance.stance != "unknown"
            and stance.stance not in compatible
            and not _has_newer_completed_evidence(
                stance.evidence_refs,
                graph_decision=graph_decision,
                evidence_by_id=evidence_by_id,
            )
        ):
            old = stance.stance
            cap = _pm_int(cfg, "portfolio_advisor_conflict_rerun_cap", 2, 0, 20)
            prior_runs = _recent_full_graph_count(cfg, tk)
            if prior_runs >= cap:
                # Deep research has run repeatedly and still conflicts with the
                # thesis read. Stop looping; escalate to the human once.
                queued = False
                stance.stance = "unknown"
                stance.rationale = (
                    (stance.rationale or "").strip()
                    + f" Deep research ({graph_rating}) and the thesis read have disagreed across "
                    f"{prior_runs} full-graph runs in {14} days — this needs your call, not another run."
                ).strip()
                escalation = (
                    f"{tk}: deep research keeps saying {graph_rating} but the thesis read keeps "
                    f"breaking ({prior_runs} runs, unresolved). I can't settle this automatically — "
                    f"your call: hold or cut."
                )
                existing_push = (result.push_note or "").strip()
                result.push_note = f"{existing_push} | {escalation}" if existing_push else escalation
                overrides.append(
                    {
                        "field": f"stances.{tk}.stance",
                        "action": "escalated_unresolved_conflict",
                        "from": old,
                        "latest_full_graph_rating": graph_rating,
                        "full_graph_runs_14d": prior_runs,
                    }
                )
            else:
                queued = _ensure_conflict_research_job(result, tk, graph_rating)
                stance.stance = "unknown"
                stance.rationale = (
                    (stance.rationale or "").strip()
                    + f" This conflicts with the latest full-graph decision ({graph_rating}); "
                    "newer cited evidence is required before changing stance."
                ).strip()
                overrides.append(
                    {
                        "field": f"stances.{tk}.stance",
                        "action": "downgraded_full_graph_conflict",
                        "from": old,
                        "latest_full_graph_ref": graph_decision.get("id"),
                        "latest_full_graph_rating": graph_rating,
                        "queued_conflict_research": queued,
                    }
                )
        if stance.stance in {"buy", "sell", "trim", "add"}:
            has_research_ref = any(str(r).startswith("event:") for r in stance.evidence_refs)
            if stale_info or not has_research_ref:
                old = stance.stance
                stance.stance = "unknown"
                stance.rationale = (
                    (stance.rationale or "").strip()
                    + " Evidence is insufficient for an action stance; queue research before acting."
                ).strip()
                overrides.append(
                    {
                        "field": f"stances.{tk}.stance",
                        "action": "downgraded_to_unknown",
                        "from": old,
                        "reason": (
                            "action stance had stale or missing research evidence"
                            if stale_info
                            else "action stance lacked completed research evidence"
                        ),
                    }
                )

    live_sorted = sorted(live_tickers)
    for cmp in result.candidate_comparisons:
        cmp.candidate_ticker = str(cmp.candidate_ticker or "").strip().upper()
        before = list(cmp.compared_against or [])
        cmp.compared_against = [
            str(t).strip().upper() for t in (cmp.compared_against or []) if str(t).strip().upper() in live_tickers
        ]
        if before != cmp.compared_against:
            overrides.append(
                {
                    "field": f"candidate_comparisons.{cmp.candidate_ticker}.compared_against",
                    "action": "removed_non_live_comparators",
                }
            )
        if not cmp.compared_against and live_sorted:
            cmp.compared_against = live_sorted[:5]
            overrides.append(
                {
                    "field": f"candidate_comparisons.{cmp.candidate_ticker}.compared_against",
                    "action": "auto_attached_live_comparators",
                    "tickers": cmp.compared_against,
                }
            )
        existing_refs = [r for r in (cmp.evidence_refs or []) if str(r).strip()]
        valid_refs = [r for r in existing_refs if r in allowed_refs or r.startswith(("caller:", "memory:", "pm_cycle:", "candidate:"))]
        if valid_refs != existing_refs:
            cmp.evidence_refs = valid_refs
            overrides.append(
                {
                    "field": f"candidate_comparisons.{cmp.candidate_ticker}.evidence_refs",
                    "action": "removed_unknown_refs",
                }
            )

    unknown_dates = sorted(
        _text_dates(
            result.executive_summary,
            result.memory_note,
            result.push_note,
            result.replan_rationale,
            *[s.rationale for s in result.stances],
            *[j.rationale for j in result.append_jobs],
        )
        - known_dates
    )
    if unknown_dates:
        overrides.append({"field": "dates", "action": "flagged_unknown_dates", "dates": unknown_dates})
        if _text_dates(result.push_note) & set(unknown_dates):
            result.push_note = ""
            overrides.append({"field": "push_note", "action": "cleared_unknown_date", "dates": unknown_dates})

    pending_overdue = False
    now = datetime.now(timezone.utc)
    for j in pending_jobs:
        try:
            sched = datetime.fromisoformat(str(j.get("scheduled_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        if sched < now - timedelta(hours=24):
            pending_overdue = True
            break
    if result.push_note and _URGENCY_RE.search(result.push_note) and not pending_overdue and trigger != "ntfy_question":
        result.push_note = ""
        overrides.append(
            {
                "field": "push_note",
                "action": "cleared_unsupported_urgency",
                "reason": "no overdue job or explicit question context",
            }
        )

    if result.request_replan and not result.replan_rationale.strip():
        result.replan_rationale = "PM requested replan without a detailed rationale."
        overrides.append({"field": "replan_rationale", "action": "filled_default"})

    if overrides:
        append_event(
            cfg,
            {
                "ticker": "*",
                "event_type": "pm_validation_override",
                "key_data": {"trigger": trigger, "overrides": overrides[:20]},
                "outcome": None,
            },
        )
    return overrides


def _pm_model(cfg: Dict[str, Any]) -> str:
    raw = cfg.get("portfolio_advisor_pm_model")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "openai/gpt-5.5"


def _pm_provider(cfg: Dict[str, Any], model: str) -> str:
    if "/" in model:
        return "openrouter"
    return (cfg.get("llm_provider") or "openrouter").lower()


def _provider_kwargs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    prov = (cfg.get("llm_provider") or "openai").lower()
    if prov == "google" and cfg.get("google_thinking_level"):
        kwargs["thinking_level"] = cfg["google_thinking_level"]
    if prov == "openai" and cfg.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["openai_reasoning_effort"]
    if prov == "anthropic" and cfg.get("anthropic_effort"):
        kwargs["effort"] = cfg["anthropic_effort"]
    return kwargs


def _pm_json_for_prompt(cfg: Dict[str, Any], obj: Any) -> str:
    if bool(cfg.get("portfolio_advisor_pm_compact_prompt_json", True)):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, indent=2, ensure_ascii=False)


def load_recent_pm_cycles(cfg: Dict[str, Any], *, limit: int = 30) -> List[Dict[str, Any]]:
    """Newest-first parsed rows from the PM JSONL log."""
    path = pm_log_path(cfg)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines()[-8000:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    if limit > 0:
        out = out[:limit]
    return out


def _append_pm_jsonl(cfg: Dict[str, Any], row: Dict[str, Any]) -> None:
    path = pm_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _append_pm_memory_md(
    cfg: Dict[str, Any],
    *,
    trigger: str,
    result: AdvisorPMCycleResult,
    actions_taken: Optional[Dict[str, Any]] = None,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"{_PM_ADVISOR_SENTINEL}\n",
        f"\n## PM cycle — {ts} — trigger `{trigger}`\n",
        "### Executive summary\n",
        (result.executive_summary or "").strip() + "\n",
        "### Stances\n",
    ]
    for s in result.stances:
        lines.append(f"- **{s.ticker}** — _{s.stance}_ — {s.rationale}\n")
    if result.candidate_comparisons:
        lines.append("\n### Candidate comparisons\n")
        for c in result.candidate_comparisons:
            against = ", ".join(c.compared_against) if c.compared_against else "current holdings"
            lines.append(
                f"- **{c.candidate_ticker}** — _{c.replace_or_add}_ — "
                f"better_than_current_holding={c.better_than_current_holding}; "
                f"compared_against={against}; {c.rationale}\n"
            )
    if result.forward_tasks:
        lines.append("\n### Forward tasks\n")
        for t in result.forward_tasks:
            lines.append(f"- {t}\n")
    if (result.memory_note or "").strip():
        lines.extend(["\n### Memory note (for next cycle)\n", result.memory_note.strip() + "\n"])
    if actions_taken and actions_taken.get("apply_enabled", True):
        lines.append("\n### Actions taken (automation)\n")
        if actions_taken.get("replan_outcome") is not None:
            lines.append(f"- Replan: `{actions_taken.get('replan_outcome')}`\n")
        if actions_taken.get("replan_error"):
            lines.append(f"- Replan error: {actions_taken.get('replan_error')}\n")
        ja = int(actions_taken.get("jobs_appended") or 0)
        if ja:
            lines.append(f"- Extra jobs appended: {ja}\n")
        if actions_taken.get("jobs_skipped"):
            lines.append(f"- Skipped tickers (not in book): {', '.join(actions_taken['jobs_skipped'])}\n")
    block = "".join(lines)
    if _pm_unified_memory(cfg) and isinstance(cfg.get("memory_log_path"), str) and str(cfg["memory_log_path"]).strip():
        block = block + TradingMemoryLog._SEPARATOR
    with _pm_memory_lock:
        path = pm_memory_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)


def _prior_pm_context(cfg: Dict[str, Any], *, current_tickers: Optional[set] = None) -> str:
    n = _pm_int(cfg, "portfolio_advisor_pm_prior_cycles", 2, 0, 8)
    if n <= 0:
        return ""
    prev = load_recent_pm_cycles(cfg, limit=n)
    if not prev:
        return ""
    # Skip prior cycles whose stance ticker set exactly matches the current portfolio —
    # their context is redundant since we're about to review the same holdings again.
    if current_tickers is not None:
        cur_set = {t.strip().upper() for t in current_tickers if str(t).strip()}
        filtered = []
        for row in prev:
            r = row.get("result") or {}
            stance_tickers = {
                str(s.get("ticker") or "").strip().upper()
                for s in (r.get("stances") or [])
                if s.get("ticker")
            }
            if stance_tickers != cur_set:
                filtered.append(row)
        prev = filtered
    if not prev:
        return ""
    ex_cap = _pm_int(cfg, "portfolio_advisor_pm_prior_executive_chars", 450, 80, 4000)
    mem_cap = _pm_int(cfg, "portfolio_advisor_pm_prior_memory_note_chars", 700, 0, 8000)
    total_cap = _pm_int(cfg, "portfolio_advisor_pm_prior_context_total_chars", 2600, 200, 20000)
    chunks = []
    for row in reversed(prev):
        r = row.get("result") or {}
        if not isinstance(r, dict):
            continue
        mem = str(r.get("memory_note") or "").strip()
        if mem_cap > 0 and len(mem) > mem_cap:
            mem = mem[:mem_cap] + "…"
        ex = str(r.get("executive_summary") or "").strip()[:ex_cap]
        chunks.append(f"Previous summary:\n{ex}\nPrevious memory note:\n{mem or '(none)'}\n")
    joined = "\n---\n".join(chunks)
    return joined[:total_cap]


def _content_from_llm_message(msg: Any) -> str:
    content = getattr(msg, "content", str(msg))
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = "\n".join(parts) if parts else str(content)
    return str(content).strip()


def _coerce_pm_result_from_text(text: str) -> AdvisorPMCycleResult:
    raw = (text or "").strip()
    # Pull a JSON object out of the response. Models (esp. DeepSeek) wrap it in
    # ```json fences or surround it with prose, so try a fenced block and the
    # widest brace span before giving up.
    candidates: List[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    candidates.append(raw)

    obj = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                obj = parsed
                break
        except Exception:
            continue

    if obj is None:
        # Not JSON at all — treat the whole thing as a prose answer.
        return AdvisorPMCycleResult(
            executive_summary=raw[:8000],
            stances=[], forward_tasks=[], memory_note="",
            request_replan=False, replan_rationale="", append_jobs=[],
        )

    # Repair commonly-omitted required fields so one missing field doesn't make
    # us discard an otherwise useful response (DeepSeek drops these sometimes).
    obj.setdefault("executive_summary", "")
    repaired_stances = []
    for st in obj.get("stances") or []:
        if isinstance(st, dict) and st.get("ticker"):
            st.setdefault("stance", "unknown")
            repaired_stances.append(st)
    obj["stances"] = repaired_stances

    try:
        return AdvisorPMCycleResult.model_validate(obj)
    except Exception:
        # A nested sub-object is still malformed — preserve the human-facing
        # prose (summary / push_note / memory) instead of losing everything.
        return AdvisorPMCycleResult(
            executive_summary=str(obj.get("executive_summary") or "")[:8000],
            push_note=str(obj.get("push_note") or ""),
            memory_note=str(obj.get("memory_note") or ""),
            stances=[], forward_tasks=[], append_jobs=[],
            request_replan=False, replan_rationale="",
        )


# ---------------------------------------------------------------------------
# Last-action log — lets the user CANCEL the most recent PM-queued jobs
# ---------------------------------------------------------------------------

def _last_action_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "last_action.json"


def save_last_action(
    cfg: Dict[str, Any],
    job_ids: List[str],
    had_replan: bool = False,
    description: str = "",
) -> None:
    p = _last_action_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_ids": job_ids,
                "had_replan": had_replan,
                "description": description,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def cancel_last_action(cfg: Dict[str, Any]) -> str:
    """Cancel jobs queued by the last PM action. Returns a status string."""
    p = _last_action_path(cfg)
    if not p.is_file():
        return "No recent action to cancel."
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Could not read last action."
    job_ids = set(data.get("job_ids") or [])
    if not job_ids:
        return "No jobs recorded in last action."
    st = state.load_state(cfg)
    cancelled: List[str] = []
    for j in st.get("jobs") or []:
        if j.get("id") in job_ids and j.get("status") == "pending":
            j["status"] = "cancelled"
            j["cancel_reason"] = "Cancelled by user via ntfy"
            cancelled.append(j.get("ticker", "?"))
    state.save_state(cfg, st)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    if cancelled:
        note = " (replan cannot be undone)" if data.get("had_replan") else ""
        return f"Cancelled: {', '.join(cancelled)}{note}"
    return "Jobs already ran or were not found in queue."


def apply_pm_cycle_followups(cfg: Dict[str, Any], result: AdvisorPMCycleResult) -> Dict[str, Any]:
    """Execute PM-structured replan / extra job requests (Phase 3). Never raises."""
    actions: Dict[str, Any] = {
        "apply_enabled": bool(cfg.get("portfolio_advisor_pm_apply_actions", True)),
    }
    if not actions["apply_enabled"]:
        return actions

    actions["replan_outcome"] = None
    actions["replan_error"] = None
    actions["jobs_appended"] = 0
    actions["jobs_skipped"] = []
    actions["jobs_deduped"] = []

    if result.request_replan:
        try:
            from tradingagents.portfolio_advisor.service import run_replan

            token = run_replan(
                cfg,
                ignore_weekday=bool(cfg.get("portfolio_advisor_pm_replan_ignore_weekday", True)),
            )
            actions["replan_outcome"] = token
        except Exception as e:
            logger.exception("PM-requested replan failed")
            actions["replan_error"] = str(e)

    specs = list(result.append_jobs or [])[:5]
    if not specs:
        return actions

    try:
        _payload, _pt, tickers, _rows = etoro_scan.fetch_portfolio_rows()
        live = etoro_scan.current_ticker_set(tickers)
    except Exception as e:
        logger.warning("PM append_jobs: portfolio fetch failed: %s", e)
        actions["jobs_fetch_error"] = str(e)
        return actions

    st = state.load_state(cfg)
    pending = state.list_pending_jobs(st)
    now = datetime.now(timezone.utc)
    new_rows: List[Dict[str, Any]] = []
    for i, job in enumerate(specs):
        tid = str(job.ticker or "").strip().upper()
        if not tid or tid not in live:
            actions["jobs_skipped"].append(tid or "?")
            continue
        job_type = str(job.job_type or "thesis_check").strip()
        evidence_question = (job.evidence_question or job.rationale or "").strip()[:300]
        source = str(job.source or "pm_followup").strip()
        duplicate = None
        for existing in pending + new_rows:
            if str(existing.get("ticker") or "").strip().upper() != tid:
                continue
            if str(existing.get("job_type") or "").strip() != job_type:
                continue
            existing_q = str(existing.get("evidence_question") or existing.get("reason") or "").strip()[:300]
            if evidence_question and existing_q and evidence_question == existing_q:
                duplicate = existing
                break
            if not evidence_question:
                duplicate = existing
                break
        if duplicate:
            actions["jobs_deduped"].append(
                {
                    "ticker": tid,
                    "job_type": job_type,
                    "existing_job_id": duplicate.get("id"),
                    "source": source,
                }
            )
            continue
        when = now + timedelta(minutes=1)
        new_rows.append(
            {
                "id": uuid.uuid4().hex[:20],
                "ticker": tid,
                "scheduled_at": when.isoformat(),
                "kind": "deep_research",
                "reason": (job.rationale or "PM-requested follow-up").strip()[:500],
                "status": "pending",
                "created_at": now.isoformat(),
                "execution_tier": str(job.execution_tier or "single_model").strip(),
                "job_type": job_type,
                "source": source,
                "evidence_question": evidence_question,
                "supersedes_job_id": str(job.supersedes_job_id or "").strip(),
                "flags": ["PM_APPEND"],
            }
        )
    if new_rows:
        state.append_jobs(st, new_rows)
        state.save_state(cfg, st)
        actions["jobs_appended"] = len(new_rows)
        save_last_action(
            cfg,
            job_ids=[r["id"] for r in new_rows],
            had_replan=bool(actions.get("replan_outcome")),
            description="Queued: " + ", ".join(r["ticker"] for r in new_rows),
        )
        _trigger_run_due_async()
    return actions


def _trigger_run_due_async() -> None:
    """Launch run-due in a background subprocess so queued jobs start immediately."""
    try:
        root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env_file = root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env.setdefault(k.strip(), v.strip())
        env["PYTHONPATH"] = str(root) + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        subprocess.Popen(
            [sys.executable, "-m", "cli.main", "advisor", "portfolio", "run-due"],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        logger.warning("Failed to trigger async run-due: %s", e)


def _notify_action_stances(
    cfg: Dict[str, Any],
    result: AdvisorPMCycleResult,
    portfolio_rows: List[Dict[str, Any]],
    notify_alert: bool = True,
) -> bool:
    """Update action log from PM stances; push one conversational alert when something is new.

    Prefers the PM's own push_note (written conversationally per PM_CLAUDE.md). Falls back
    to a one-line-per-ticker compact form. No "Action required:" header, no "Reason:" sublines.

    When ``notify_alert`` is False (chat-mode), still syncs the action log so a "hold"
    clears the matching SELL action — but skips the separate alert send because the
    chat reply is already the user-facing message.
    """
    try:
        from tradingagents.portfolio_advisor import messaging
        from tradingagents.portfolio_advisor.action_log import upsert_action, mark_done
        action_stances = [s for s in (result.stances or []) if s.stance in ("sell", "trim")]
        # Auto-close items where stance has improved away from sell/trim
        for s in (result.stances or []):
            if s.stance not in ("sell", "trim"):
                mark_done(cfg, s.ticker)
        if not action_stances:
            return False

        new_or_changed: List[Any] = []
        for s in action_stances:
            rationale = (s.rationale or "").strip()
            action_status = upsert_action(cfg, s.ticker, s.stance, rationale, source="pm_cycle")
            if action_status == "unchanged":
                continue
            new_or_changed.append(s)
        if not new_or_changed:
            return False
        if not notify_alert:
            # Action log synced; chat reply itself carries the message — don't double-alert.
            return False

        push = (result.push_note or "").strip()
        if push:
            body = push
        else:
            # No PM push_note — synthesize a compact one-liner per changed ticker.
            parts: List[str] = []
            for s in new_or_changed:
                instr = _format_close_instruction(s.ticker, s.stance, s.rationale or "", portfolio_rows)
                parts.append(f"{s.ticker}: {instr}")
            body = " ".join(parts)
        # Quiet-hours rule: only bypass the window when the PM itself flagged push_note.
        # No push_note → routine; will hold until the next morning/evening window.
        messaging.send_advisor_message(cfg, "PM", body, urgent=bool(push))
        return True
    except Exception as e:
        logger.debug("_notify_action_stances failed silently: %s", e)
        return False


def _parse_available_balance(portfolio_text: str) -> Optional[float]:
    """Extract available_balance from the first line of fetch_portfolio_rows text."""
    import re
    first = (portfolio_text or "").split("\n")[0]
    m = re.search(r"available_balance=(['\"]?)([0-9]+(?:\.[0-9]+)?)\1", first)
    if m:
        try:
            return float(m.group(2))
        except ValueError:
            pass
    return None


def _deployable_capital_block(
    portfolio_text: str,
    portfolio_rows: List[Dict[str, Any]],
) -> str:
    """Build a short cash + position-sizing block for the PM prompt."""
    cash = _parse_available_balance(portfolio_text)
    ubv_vals = [
        float(r["unitsBaseValueDollars"])
        for r in portfolio_rows
        if r.get("unitsBaseValueDollars") is not None
    ]
    total_ubv = sum(ubv_vals) if ubv_vals else None

    lines = ["Deployable capital:"]
    if cash is not None:
        lines.append(f"  available_balance (eToro): ${cash:,.2f}")
    else:
        lines.append("  available_balance: not parsed from snapshot")

    if total_ubv:
        max_new = total_ubv * 0.05
        first_tranche = max_new * 0.5
        lines.append(f"  total_invested (positions only): ${total_ubv:,.2f}")
        lines.append(
            f"  position sizing rule: max 5% of invested = ${max_new:,.0f} per new name; "
            f"first tranche (half now) = ${first_tranche:,.0f}"
        )
        if cash is not None:
            can_afford = "yes" if cash >= first_tranche else f"NO — only ${cash:,.0f} available, first tranche requires ${first_tranche:,.0f}"
            lines.append(f"  can fund first tranche from cash: {can_afford}")
    return "\n".join(lines) + "\n"


def _sleeve_allocation_block(
    cfg: Dict[str, Any],
    portfolio_text: str,
    portfolio_rows: List[Dict[str, Any]],
) -> str:
    """Build a sleeve mix vs target block (core / catalyst / cash) for the PM prompt.

    Each live position's sleeve comes from its position plan (default 'core' when
    no plan exists). Compares actual weights against the configured targets and
    flags any sleeve that has drifted beyond tolerance.
    """
    targets = cfg.get("portfolio_advisor_sleeve_targets") or {"core": 0.50, "catalyst": 0.40, "cash": 0.10}
    try:
        tolerance = float(cfg.get("portfolio_advisor_sleeve_drift_tolerance", 0.07))
    except (TypeError, ValueError):
        tolerance = 0.07

    plans = pos_plans.load_position_plans(cfg)
    cash = _parse_available_balance(portfolio_text) or 0.0

    core_val = 0.0
    catalyst_val = 0.0
    for r in portfolio_rows:
        ubv = _float_or_none(r.get("unitsBaseValueDollars"))
        if ubv is None:
            continue
        tk = _normalize_ticker(str(r.get("symbolFull") or ""))
        plan = plans.get(tk)
        strategy = plan.strategy if plan else "core"
        if strategy == "catalyst":
            catalyst_val += ubv
        else:
            core_val += ubv

    total = core_val + catalyst_val + cash
    if total <= 0:
        return ""

    actual = {
        "core": core_val / total,
        "catalyst": catalyst_val / total,
        "cash": cash / total,
    }
    dollars = {"core": core_val, "catalyst": catalyst_val, "cash": cash}

    lines = ["Sleeve allocation (actual vs target):"]
    flags: List[str] = []
    for sleeve in ("core", "catalyst", "cash"):
        tgt = float(targets.get(sleeve, 0.0))
        act = actual[sleeve]
        drift = act - tgt
        marker = ""
        if abs(drift) > tolerance:
            marker = "  << DRIFT"
            direction = "over" if drift > 0 else "under"
            flags.append(f"{sleeve} is {abs(drift)*100:.0f}pts {direction} target")
        lines.append(
            f"  {sleeve}: {act*100:.0f}% (${dollars[sleeve]:,.0f}) vs target {tgt*100:.0f}%{marker}"
        )
    if flags:
        lines.append(
            "  Rebalance guidance: steer new capital toward under-target sleeves. "
            "Do NOT sell an intact core position to fund catalyst trades; fund from cash or from "
            "positions with a TRIGGERED exit rule. " + "; ".join(flags) + "."
        )
    return "\n".join(lines) + "\n"


def run_pm_cycle(
    cfg: Dict[str, Any],
    *,
    trigger: str = "manual",
    extra_context: Optional[str] = None,
) -> AdvisorPMCycleResult:
    """Run one PM council pass: portfolio snapshot + state → structured plan → logs."""
    set_config(cfg)
    trigger_s = (trigger or "manual").strip()[:80] or "manual"

    _ensure_pm_claude_md(cfg)
    pm_claude = _read_pm_claude_md(cfg)
    # Always feed PM memory/context — chat-mode was getting too thin a prompt
    # under the agentic persona, causing the model to defer with empty replies.
    pm_memory = _read_pm_memory_structured(cfg)

    _payload, portfolio_text, tickers, portfolio_rows = etoro_scan.fetch_portfolio_rows()
    if not tickers:
        raise RuntimeError("No tickers in eToro portfolio export.")

    live_tickers = etoro_scan.current_ticker_set(tickers)

    st = state.load_state(cfg)
    summ = st.get("last_bootstrap_summary")
    summ_txt = ""
    summ_max = _pm_int(cfg, "portfolio_advisor_pm_bootstrap_summary_chars", 4000, 0, 20000)
    if isinstance(summ, dict) and summ and summ_max > 0:
        summ_txt = _pm_json_for_prompt(cfg, summ)[:summ_max]

    pend = [j for j in (st.get("jobs") or []) if isinstance(j, dict) and j.get("status") == "pending"]
    job_cap = _pm_int(cfg, "portfolio_advisor_pm_pending_jobs_cap", 12, 0, 100)
    pend_preview = _pm_json_for_prompt(
        cfg,
        [
            {
                "ticker": j.get("ticker"),
                "scheduled_at": j.get("scheduled_at"),
                "tier": j.get("execution_tier"),
                "type": j.get("job_type"),
            }
            for j in pend[:job_cap]
        ],
    )

    model = _pm_model(cfg)
    provider = _pm_provider(cfg, model)
    base = cfg.get("corporate_openrouter_base_url") or cfg.get("backend_url")

    portfolio_snapshot = _compact_portfolio_snapshot(portfolio_text, portfolio_rows)
    extra_cap = _pm_int(cfg, "portfolio_advisor_pm_extra_context_chars", 3200, 0, 20000)
    extra_excerpt = (extra_context or "").strip()[:extra_cap] if extra_cap > 0 else ""

    prior_txt = _prior_pm_context(cfg, current_tickers=live_tickers)
    prior_block = f"Prior PM context (most recent cycles):\n{prior_txt}\n\n" if prior_txt else ""
    tm_block = _trading_memory_digest_block(cfg)
    recent_analysis_block = _recent_analysis_block(cfg, sorted(live_tickers))
    evidence_context = _pm_evidence_context(cfg, sorted(live_tickers), pend)
    evidence_block = _pm_json_for_prompt(
        cfg,
        {
            "known_dates": evidence_context.get("known_dates") or [],
            "stale_after_days": evidence_context.get("stale_after_days"),
            "stale_tickers": evidence_context.get("stale_tickers") or {},
            "latest_full_graph_decisions": evidence_context.get("latest_full_graph_decisions") or {},
            "evidence": evidence_context.get("evidence") or [],
        },
    )
    claude_block = f"{pm_claude}\n\n" if pm_claude else ""
    memory_block = f"Your working memory (PM_MEMORY.md — recent notes to self):\n{pm_memory}\n\n" if pm_memory else ""

    # Deterministic rule-trigger check — computed from position_plans.json + current prices.
    try:
        trigger_block = pos_plans.build_trigger_block(cfg, live_tickers)
    except Exception as e:
        logger.debug("position rule trigger block failed: %s", e)
        trigger_block = ""

    # Cash and position-sizing block.
    try:
        capital_block = _deployable_capital_block(portfolio_text, portfolio_rows)
    except Exception as e:
        logger.debug("deployable capital block failed: %s", e)
        capital_block = ""

    # Sleeve allocation (core / catalyst / cash) vs target mix.
    try:
        sleeve_block = _sleeve_allocation_block(cfg, portfolio_text, portfolio_rows)
    except Exception as e:
        logger.debug("sleeve allocation block failed: %s", e)
        sleeve_block = ""

    # Candidate watchlist summary (short — just shows what's on deck, not gated yet).
    try:
        watchlist_block = watchlist_mod.watchlist_summary_for_pm(cfg)
    except Exception as e:
        logger.debug("watchlist summary block failed: %s", e)
        watchlist_block = ""

    # Candidate RESEARCH block — what the system has actually researched on
    # watchlist names. Without this the PM only sees a static summary and
    # never knows PLTR/COST/FOUR (etc.) have completed deep research, so it
    # never produces candidate_comparisons or buy stances on new names.
    try:
        cand_entries = watchlist_mod.load_watchlist(cfg)
        cand_tickers: List[str] = []
        for e in cand_entries:
            if isinstance(e, str):
                cand_tickers.append(e.strip().upper())
            elif isinstance(e, dict):
                tk = str(e.get("ticker") or "").strip().upper()
                if tk:
                    cand_tickers.append(tk)
        cand_tickers = sorted({t for t in cand_tickers if t and t not in live_tickers})
        raw_cand_block = _recent_analysis_block(cfg, cand_tickers) if cand_tickers else ""
        if raw_cand_block:
            # Strip the inner default header so the candidate-specific one reads cleanly.
            if raw_cand_block.startswith("Latest research results"):
                raw_cand_block = raw_cand_block.split("\n", 1)[1] if "\n" in raw_cand_block else ""
            candidate_research_block = (
                "Candidate research (watchlist names with completed research — evaluate "
                "these and, when one is clearly stronger than a current holding or fills "
                "the catalyst sleeve, emit a candidate_comparison AND a starter-sized "
                "'buy' stance with explicit sizing. Cite the full_graph or thesis_check "
                "event id in evidence_refs):\n" + raw_cand_block + "\n"
            )
        else:
            candidate_research_block = ""
    except Exception as e:
        logger.debug("candidate research block failed: %s", e)
        candidate_research_block = ""

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Both "ntfy_question" and "live_chat" made v4-pro defer with empty replies;
    # only "manual" (the same label scheduled cycles use) reliably produced output.
    # Show the model "manual" for chat too; keep trigger_s as "ntfy_question" so
    # post-processing gates (alert send, action-log sync mode) remain correct.
    trigger_label = "manual" if trigger_s == "ntfy_question" else trigger_s
    prompt = f"""{claude_block}You are the portfolio manager for a research stack. Advisory only: no trade orders, no claims that trades executed.

Today's date (UTC): {today_utc}

Authority: the human controls the real portfolio and every execution decision. LangGraph and lighter single-model passes are research tools — treat their outputs as inputs, not as orders or fills.

For direct human questions, answer from the live portfolio snapshot, latest completed evidence, pending jobs,
and the explicit question only. Do not echo prior PM prose or repeated old notifications. If old memory conflicts
with the live snapshot or latest evidence, say what changed and prefer the live/latest evidence.

The portfolio snapshot below is fetched live from eToro on every PM cycle — it is always current as of this run.
If the human asks you to "refresh" or "check eToro", you already have done so: the snapshot above is the result.
If a position the human says they closed still appears in the snapshot, tell them it still shows open in eToro's
live data (likely an eToro API lag) — do not say you cannot refresh or that you need a sync.

Execution tiers (for append_jobs only): "full_graph" runs the full multi-agent pipeline on one ticker; "single_model" is a faster desk-style pass (thesis_check, weekly_summary, post_earnings, routine_monitoring).

Trigger for this cycle: {trigger_label}

{memory_block}Portfolio snapshot:
{portfolio_snapshot}

{sleeve_block}
{capital_block}
{trigger_block}
Live tickers (normalized): {", ".join(sorted(live_tickers))}

{watchlist_block}
Pending advisor jobs preview (JSON):
{pend_preview}

Latest completed research decisions/results:
{recent_analysis_block or "(none found in the recent event log)\n\n"}
{candidate_research_block}
Retrieved evidence refs and known dates (JSON):
{evidence_block}

If stale_tickers is non-empty, do not make action stances for those names from old evidence. Queue or cite a
fresh research job first.
If latest_full_graph_decisions contains a ticker, treat it as the authoritative ticker-level decision. To
disagree, cite newer completed evidence than that full-graph decision or queue conflict research.

Last bootstrap summary (JSON, may be empty):
{summ_txt or "(none)"}

{prior_block}{tm_block}Extra notes from caller (may be empty):
{extra_excerpt or "(none)"}

Structured output fields (use defaults when unsure):
- request_replan: set true only when the pending job queue should be fully rebuilt via the planner LLM
  (cancels current pending jobs). Set replan_rationale when true.
- evidence_refs: cite the evidence IDs above for portfolio-level claims. Each non-unknown stance should cite
  at least one evidence ref. Do not cite refs that are not present in the evidence JSON.
- append_jobs: up to five extra pending jobs to queue without relying on the planner (use live tickers only).
  Each entry: ticker, execution_tier single_model or full_graph, job_type thesis_check|weekly_summary|post_earnings|routine_monitoring, rationale.
  Set source to pm_missing_evidence, pm_human_request, pm_conflict, pm_stale_evidence, or pm_followup.
  Fill evidence_question with the exact question that job should answer.
  If you set request_replan true, you may still append_jobs; they are added after the replan finishes.
- candidate_comparisons: when Extra notes contain a Candidate comparison request, put each candidate assessment here.
  Do not put non-held candidate tickers in stances. Compare candidates against live tickers only.
- push_note: one short observation worth pushing to the human right now — deadline approaching, unexpected finding,
  stance change, catalyst within 48h. Max 280 chars. Leave empty if nothing urgent or new. This goes straight
  to the human's phone, so only fill it when you genuinely have something they need to know unprompted.
  Do not repeat the same action already present in prior PM context unless the exact required close list changed.

IMPORTANT — position rule triggers: the POSITION RULE STATUS block above is computed deterministically from
position_plans.json and current prices. If a ticker shows TRIGGERED, name the exact advisory action in your
response and set push_note if this is a new trigger. TRIGGERED always takes priority over everything else.
Positions without a plan on file cannot have rule triggers checked; recommend the human set entry prices for them.

IMPORTANT — position sizing: the portfolio snapshot above includes capital$ (cash/base committed), current$
(capital plus unrealized P/L when available), units (shares held), open$ (cost basis per share), and
total_portfolio_value_usd. When you recommend trimming or selling, always express
the action as a specific dollar amount AND share count drawn from that data — never just a percentage like "trim to 2%".
Example: "Sell 3 units of TEAM (~$240) — reduces exposure from $720 to $480." The human does not know what 2% of the
portfolio is in dollars; give them the exact number so they can act immediately. If you mean "close", list exactly
which lots/positions using the open$ values shown in the snapshot. If the exact lot list is not supported by the
snapshot, do not create an action stance; queue follow-up research or ask for clarification.

IMPORTANT — strategy sleeves: the portfolio runs two sleeves. core = long-term hold + growth (3-5yr: staged
entry, +15% pre-earnings trim, -30%/-40% stops, thesis-break exit). catalyst = short-term event-driven (single
entry before a dated catalyst, -8% hard stop, trailing stop after +10%, time-stop if the catalyst passes without
the move). The POSITION RULE STATUS block already applies the correct rules per position based on its sleeve.
Treat a position's sleeve as fixed: never suggest reclassifying a losing catalyst trade as core to dodge its stop,
or vice versa. If the human wants to keep a catalyst name long-term after the event, say that explicitly as a NEW
core entry decision.

IMPORTANT — new positions and reallocation: before recommending any new entry, check the Deployable capital and
Sleeve allocation blocks. First tranche must fit within available_balance, and new capital should move the sleeve
mix toward target. Do NOT recommend selling an intact existing position to fund a new one unless the existing
position has a TRIGGERED exit rule. Cash-funded entry first; reallocation only on thesis break or a fired exit rule.

Deliver structured output only. Stances must use tickers you see above. forward_tasks should be concrete
(research X, schedule replan, verify Y thesis, respond to risk flag, etc.). memory_note is what you want your next self to read first.

Do not create facts to make the memo feel complete. If completed research is missing, stale, or insufficient for
the trigger, say that plainly and use append_jobs to send a new research layer to gather it.
"""
    client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base,
        max_completion_tokens=4096,
        **_provider_kwargs(cfg),
    )
    llm = client.get_llm()
    structured = bind_structured(llm, AdvisorPMCycleResult, "AdvisorPMCycle")
    if structured is not None:
        try:
            out = structured.invoke([HumanMessage(content=prompt)])
            if isinstance(out, AdvisorPMCycleResult):
                result = out
            else:
                result = _coerce_pm_result_from_text(_content_from_llm_message(out))
        except Exception as e:
            logger.warning("PM structured cycle failed: %s", e)
            raw = llm.invoke([HumanMessage(content=prompt)])
            result = _coerce_pm_result_from_text(_content_from_llm_message(raw))
    else:
        raw = llm.invoke([HumanMessage(content=prompt)])
        result = _coerce_pm_result_from_text(_content_from_llm_message(raw))

    validation_overrides = _validate_pm_cycle_result(
        cfg,
        result,
        live_tickers=live_tickers,
        evidence_context=evidence_context,
        pending_jobs=pend,
        trigger=trigger_s,
    )
    actions_taken = apply_pm_cycle_followups(cfg, result)

    # Always sync the action log from stances so a chat-mode "hold" actually
    # clears the matching SELL action (the digest stops contradicting the PM).
    # Only AUTO-ALERT on scheduled cycles — for ntfy_question the chat reply is
    # itself the user-facing message.
    action_alert_sent = _notify_action_stances(
        cfg, result, portfolio_rows,
        notify_alert=(trigger_s != "ntfy_question"),
    )

    # Push note — only when it is not already included in the consolidated action alert.
    # push_note is the PM's own urgency signal and bypasses quiet hours.
    note = (result.push_note or "").strip()
    if note and not action_alert_sent and trigger_s != "ntfy_question":
        try:
            from tradingagents.portfolio_advisor import messaging
            messaging.send_advisor_message(cfg, "PM", note, urgent=True)
        except Exception as e:
            logger.debug("push_note send failed: %s", e)

    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "timestamp": ts,
        "trigger": trigger_s,
        "model": model,
        "result": result.model_dump(),
        "validation_overrides": validation_overrides,
        "actions_taken": actions_taken,
    }
    _append_pm_jsonl(cfg, row)
    _append_pm_memory_md(cfg, trigger=trigger_s, result=result, actions_taken=actions_taken)
    _write_pm_memory_update(cfg, result.memory_note, trigger_s)

    st2 = state.load_state(cfg)
    st2["last_pm_cycle_iso"] = ts
    ex = (result.executive_summary or "").strip()
    st2["last_pm_executive_prefix"] = ex[:500] + ("…" if len(ex) > 500 else "")
    state.save_state(cfg, st2)

    excerpt = ex[:900] if ex else ""
    append_event(
        cfg,
        {
            "ticker": "*",
            "event_type": "advisor_pm_cycle",
            "key_data": {
                "trigger": trigger_s,
                "model": model,
                "stance_tickers": [s.ticker for s in result.stances[:24]],
                "forward_tasks_n": len(result.forward_tasks),
                "excerpt": excerpt,
                "request_replan": bool(result.request_replan),
                "replan_outcome": actions_taken.get("replan_outcome"),
                "replan_error": actions_taken.get("replan_error"),
                "jobs_appended": int(actions_taken.get("jobs_appended") or 0),
            },
            "outcome": None,
        },
    )
    return result


def run_pm_after_full_graph_if_enabled(
    cfg: Dict[str, Any],
    *,
    ticker: str,
    trade_date: str,
    final_state: Dict[str, Any],
) -> None:
    """Run advisor PM immediately after a full LangGraph deep run (best-effort; never raises)."""
    if not bool(cfg.get("portfolio_advisor_pm_enabled", True)):
        return
    if not bool(cfg.get("portfolio_advisor_pm_after_each_langgraph", True)):
        return
    sym = str(ticker or "").strip().upper()
    td = str(trade_date or "").strip()
    dec = str((final_state or {}).get("final_trade_decision") or "").strip()
    extra = (
        f"A full-graph (LangGraph) run just finished for {sym} (trade_date={td}).\n"
        "The human owns the portfolio; use this decision as one input and set portfolio-level next steps.\n\n"
        f"Final decision text (truncated):\n{dec[:2800]}"
    )
    try:
        run_pm_cycle(cfg, trigger="after_langgraph", extra_context=extra)
    except Exception:
        logger.exception("Advisor PM after LangGraph failed for %s", sym)


def _outcome_lines_for_removed(cfg: Dict[str, Any], removed: List[str]) -> List[str]:
    """Return short outcome summary lines for recently closed tickers (best-effort)."""
    from tradingagents.agents.utils import event_log as el

    lines: List[str] = []
    try:
        events = list(el._iter_events(cfg, max_lines=8000))
    except Exception:
        return lines
    seen: set = set()
    for row in reversed(events):
        et = str(row.get("event_type") or "")
        if et not in ("outcome_recorded", "partial_close_outcome"):
            continue
        sym = str(row.get("ticker") or "").strip().upper()
        if sym not in removed or sym in seen:
            continue
        seen.add(sym)
        kd = row.get("key_data") if isinstance(row.get("key_data"), dict) else {}
        align = str(row.get("outcome") or kd.get("outcome_alignment") or "unknown")
        pnl = kd.get("pnl_pct")
        decision = kd.get("decision_was") or ""
        pnl_str = f"{float(pnl):+.1f}%" if pnl is not None else "n/a"
        lines.append(f"- {sym}: decision={decision or 'unknown'}, alignment={align}, pnl_proxy={pnl_str}")
        if len(seen) >= 8:
            break
    return lines


def optional_pm_cycle_on_portfolio_change(
    cfg: Dict[str, Any],
    *,
    trigger: str,
    old_portfolio_text_hash: Optional[str],
    new_portfolio_text_hash: str,
    tickers_added: Optional[List[str]] = None,
    tickers_removed: Optional[List[str]] = None,
) -> None:
    """Run a PM cycle when the live book or snapshot fingerprint changed (Phase 4). Best-effort."""
    if not bool(cfg.get("portfolio_advisor_pm_enabled", True)):
        return
    if not bool(cfg.get("portfolio_advisor_pm_cycle_on_portfolio_change", True)):
        return
    ta = [str(x).strip().upper() for x in (tickers_added or []) if str(x).strip()]
    tr = [str(x).strip().upper() for x in (tickers_removed or []) if str(x).strip()]
    h_changed = bool(
        old_portfolio_text_hash
        and new_portfolio_text_hash
        and str(old_portfolio_text_hash) != str(new_portfolio_text_hash)
    )
    if not h_changed and not ta and not tr:
        return
    lines: List[str] = []
    if ta or tr:
        lines.append(
            f"Tickers added: {', '.join(ta) if ta else '(none)'}; "
            f"removed: {', '.join(tr) if tr else '(none)'}"
        )
    if h_changed:
        lines.append(
            "Portfolio snapshot fingerprint changed: "
            f"{str(old_portfolio_text_hash)[:16]}… → {str(new_portfolio_text_hash)[:16]}…"
        )

    # Outcome feedback: look up alignment for each removed ticker and surface it to the PM.
    if tr:
        outcome_lines = _outcome_lines_for_removed(cfg, tr)
        if outcome_lines:
            lines.append("\nOutcome alignments for closed positions (yfinance proxy, not eToro fills):")
            lines.extend(outcome_lines)

    extra = "\n".join(lines) if lines else "Portfolio change signal (no ticker list diff captured)."
    try:
        run_pm_cycle(cfg, trigger=trigger, extra_context=extra)
    except Exception:
        logger.exception("PM cycle on portfolio change failed (trigger=%s)", trigger)
