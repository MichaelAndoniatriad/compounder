# Local web UI for TradingAgents.
#
# Run:  source .venv/bin/activate && python -m cli.main ui
#   or: sh scripts/run-ui.sh

from __future__ import annotations

import html as html_module
import json
import os
from collections import OrderedDict
import re
import calendar as cal_module
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import tradingagents  # noqa: F401 — loads .env

from tradingagents.agents.utils.learned_rules_log import learned_rules_path_for_config
from tradingagents.clerk.automation_state import (
    is_clerk_scheduled_automation_paused,
    set_clerk_scheduled_automation_paused,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.integrations.etoro.clerk_bridge import _normalize_ticker
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.model_catalog import DEFAULT_CORPORATE_AGENT_ROUTING
from ui.user_config import (
    build_overlay_from_scalars_and_routing,
    effective_corporate_routing,
    merged_app_config,
    runtime_config_path,
    save_runtime_overlay,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ANALYST_ORDER: List[str] = ["market", "social", "news", "fundamentals"]
ANALYST_LABELS = {
    "market": "Market (technicals)",
    "social": "Sentiment",
    "news": "News",
    "fundamentals": "Fundamentals",
}
PROVIDERS = ["openrouter", "openai", "google", "anthropic", "ollama", "deepseek", "xai"]

NAV_PAGES = ["Dashboard", "Cost", "Settings"]

AGENT_SPEC_LABELS = {
    "market_analyst": "Market analyst",
    "sentiment_analyst": "Sentiment analyst",
    "fundamentals_analyst": "Fundamentals analyst",
    "news_analyst": "News analyst",
    "bull_researcher": "Bull researcher",
    "bear_researcher": "Bear researcher",
    "trader": "Trader",
    "risk_aggressive": "Risk (aggressive)",
    "risk_neutral": "Risk (neutral)",
    "risk_conservative": "Risk (conservative)",
    "research_manager": "Research manager",
    "portfolio_manager": "Portfolio manager",
    "reflection": "Reflection",
}

_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_EXECUTION_EVENT_TYPES = [
    "full_graph_decision",
    "single_model_analysis",
    "post_earnings_verdict",
    "advisor_plan",
    "plan_validation_override",
    "outcome_recorded",
    "bootstrap_position_failed",
    "portfolio_bootstrap_complete",
    "advisor_pm_cycle",
    "portfolio_book_changed",
    "advisor_replan_skipped",
]

_WATCHDOG_EVENT_TYPES = [
    "watchdog_critical_alert",
    "watchdog_trim_alert",
    "watchdog_high_alert",
    "watchdog_trigger_change",
]

# Snake_case event_type → short title for lists and tables (fallback: title-cased snake).
EVENT_TYPE_LABELS: Dict[str, str] = {
    "full_graph_decision": "Full multi-agent decision",
    "single_model_analysis": "Lightweight model pass",
    "post_earnings_verdict": "Post-earnings verdict",
    "advisor_plan": "Advisor schedule updated",
    "plan_validation_override": "Plan validation override",
    "outcome_recorded": "Outcome recorded",
    "bootstrap_position_failed": "Bootstrap run failed (one position)",
    "portfolio_bootstrap_complete": "Portfolio bootstrap finished",
    "advisor_pm_cycle": "Portfolio manager (PM) cycle",
    "portfolio_book_changed": "Portfolio holdings changed",
    "advisor_replan_skipped": "Replan skipped (unchanged)",
    "watchdog_critical_alert": "Watchdog: critical",
    "watchdog_trim_alert": "Watchdog: trim",
    "watchdog_high_alert": "Watchdog: elevated",
    "watchdog_trigger_change": "Watchdog: change",
    "pending_outcome_30d": "30-day outcome pending",
    "partial_close_outcome": "Partial close outcome",
}


def _event_type_title(event_type: str) -> str:
    et = str(event_type or "").strip()
    if not et:
        return "Unknown event"
    return EVENT_TYPE_LABELS.get(et, et.replace("_", " ").title())


def _ticker_display(ticker: Any) -> str:
    t = str(ticker or "").strip()
    if not t or t == "*":
        return "Portfolio-wide"
    return t.upper()


def _reason_to_sentence(event_type: str, reason: str) -> str:
    r = (reason or "").strip()
    hints = {
        "portfolio_and_catalyst_unchanged": "Portfolio and catalyst digest match the last plan, so the planner was skipped.",
    }
    if r in hints:
        return hints[r]
    if r:
        return r.replace("_", " ").strip().capitalize() + "."
    return ""


def _format_key_data_compact(kd: Dict[str, Any], *, max_len: int = 280) -> str:
    """Readable fallback for arbitrary key_data dicts (short values only)."""
    parts: List[str] = []
    skip = {"raw", "payload", "body", "html"}
    for k in sorted(kd.keys(), key=str):
        if k in skip:
            continue
        v = kd[k]
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, (dict, list)):
            try:
                s = json.dumps(v, ensure_ascii=False)
            except (TypeError, ValueError):
                s = str(v)
            if len(s) > 120:
                s = s[:117] + "…"
        else:
            s = str(v).strip().replace("\n", " ")
            if len(s) > 100:
                s = s[:97] + "…"
        label = str(k).replace("_", " ").capitalize()
        parts.append(f"{label}: {s}")
        if sum(len(p) + 2 for p in parts) >= max_len:
            break
    out = " · ".join(parts)
    return out[:max_len] if out else "(no details)"


def _channel_status_plain(row: Dict[str, Any]) -> str:
    bits: List[str] = []
    for key, channel, label in (
        ("webhook_ok", "webhook", "Webhook"),
        ("telegram_ok", "telegram", "Telegram"),
        ("smtp_ok", "smtp", "Email"),
    ):
        if channel not in (row.get("channels_attempted") or []):
            continue
        ok = bool(row.get(key))
        bits.append(f"{label}: {'sent' if ok else 'failed'}")
    if not bits:
        return "Saved in this app only (no webhook, Telegram, or email configured for outbound delivery)"
    return " · ".join(bits)


def _message_subject_display(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return "(No subject)"
    for prefix in ("[TradingAgents] ", "[TradingAgents]"):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
            break
    if len(s) > 200:
        return s[:197] + "…"
    return s


def _first_line_preview(text: str, *, max_len: int = 140) -> str:
    if not text or not str(text).strip():
        return ""
    line = str(text).strip().splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line)
    if len(line) > max_len:
        return line[: max_len - 1] + "…"
    return line

# Session keys for full-width outputs below control columns
_SS_ANALYSIS = "_ui_last_analysis"
_SS_PORT_STATUS = "_ui_portfolio_advisor_status"
_SS_PORT_NOTE = "_ui_portfolio_advisor_note"


def _default_openrouter_url() -> str:
    return "https://openrouter.ai/api/v1"


def _inject_app_styles() -> None:
    """Light-touch CSS: works with Streamlit light/dark theme; do not override sidebar colors."""
    st.markdown(
        """
<style>
  .block-container { padding-top: 1rem; padding-bottom: 2.5rem; max-width: 1100px; }
  .ta-section { margin-top: 0.25rem; margin-bottom: 1rem; }
  hr.ta-soft { margin: 1.25rem 0; opacity: 0.35; }

  /* eToro portfolio strip */
  .ta-etoro-wrap {
    border-radius: 0;
    padding: 0.35rem 0 0.85rem;
    margin: 0.35rem 0 1.1rem;
    background: transparent;
    border: none;
    box-shadow: none;
  }
  .ta-etoro-stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.75rem 1rem;
    margin-bottom: 0.85rem;
  }
  @media (max-width: 700px) {
    .ta-etoro-stats { grid-template-columns: 1fr; }
  }
  .ta-etoro-stat {
    background: transparent;
    border-radius: 0;
    padding: 0.35rem 0 0.5rem;
    border: none;
  }
  .ta-etoro-stat-label {
    display: block;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.72;
    margin-bottom: 0.2rem;
  }
  .ta-etoro-stat-value {
    font-size: 1.35rem;
    font-weight: 600;
    line-height: 1.25;
    font-variant-numeric: tabular-nums;
  }
  .ta-val-pnl-up { color: #16a34a; }
  .ta-val-pnl-down { color: #dc2626; }
  .ta-val-pnl-flat { color: var(--text-color); opacity: 0.85; }

  .ta-pos-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 0.65rem;
  }
  .ta-pos-grid.ta-pos-grid--compact {
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 0.5rem;
  }
  .ta-pos-card {
    border-radius: 12px;
    padding: 0.75rem 0.85rem;
    background: var(--secondary-background-color);
    border: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .ta-pos-card:hover {
    border-color: color-mix(in srgb, var(--text-color) 18%, transparent);
    box-shadow: 0 2px 8px color-mix(in srgb, var(--text-color) 8%, transparent);
  }
  .ta-pos-card--compact { padding: 0.55rem 0.65rem; }
  .ta-pos-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.45rem;
  }
  .ta-pos-symbol {
    font-weight: 700;
    font-size: 1.05rem;
    letter-spacing: -0.02em;
  }
  .ta-pos-name {
    font-size: 0.78rem;
    opacity: 0.72;
    line-height: 1.25;
    margin-top: 0.1rem;
  }
  .ta-badge {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.28rem;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.28rem 0.45rem;
    border-radius: 999px;
    border: 1px solid transparent;
  }
  .ta-badge-long {
    background: color-mix(in srgb, #16a34a 18%, transparent);
    color: #15803d;
    border-color: color-mix(in srgb, #16a34a 35%, transparent);
  }
  .ta-badge-short {
    background: color-mix(in srgb, #dc2626 16%, transparent);
    color: #b91c1c;
    border-color: color-mix(in srgb, #dc2626 32%, transparent);
  }
  .ta-badge-unknown {
    background: color-mix(in srgb, var(--text-color) 10%, transparent);
    color: var(--text-color);
    opacity: 0.75;
  }
  .ta-pos-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.75rem;
    font-size: 0.78rem;
    opacity: 0.88;
    font-variant-numeric: tabular-nums;
  }
  .ta-pos-pnl {
    margin-top: 0.5rem;
    font-size: 0.92rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .ta-pos-pnl-up { color: #16a34a; }
  .ta-pos-pnl-down { color: #dc2626; }
  .ta-pos-pnl-flat { opacity: 0.8; }
  .ta-pos-pnl-sticker {
    display: inline-block;
    font-size: 0.85rem;
    line-height: 1;
    margin-right: 0.25rem;
    vertical-align: middle;
  }
  .ta-pos-pnl-sticker-up { color: #16a34a; }
  .ta-pos-pnl-sticker-down { color: #dc2626; }
  .ta-pos-pnl-sticker-flat { opacity: 0.55; }
  .ta-pos-empty {
    padding: 1rem;
    text-align: center;
    opacity: 0.7;
    font-size: 0.9rem;
  }
  .ta-etoro-page-hero {
    margin-bottom: 0.5rem;
  }

  /* eToro open positions — grouped table + P&L tint */
  .ta-etoro-pos-block {
    margin: 0.35rem 0 0.85rem;
  }
  .ta-etoro-pos-table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 0.88rem;
    font-variant-numeric: tabular-nums;
  }
  .ta-etoro-pos-table col.ta-etoro-w-pos {
    width: 32%;
  }
  .ta-etoro-pos-table col.ta-etoro-w-inv,
  .ta-etoro-pos-table col.ta-etoro-w-tot,
  .ta-etoro-pos-table col.ta-etoro-w-ret,
  .ta-etoro-pos-table col.ta-etoro-w-pnl {
    width: 17%;
  }
  .ta-etoro-pos-table th:nth-child(n + 2),
  .ta-etoro-pos-table td:nth-child(n + 2) {
    text-align: right;
  }
  .ta-etoro-pos-table th,
  .ta-etoro-pos-table td {
    padding: 0.42rem 0.48rem;
    border-bottom: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent);
    vertical-align: middle;
  }
  .ta-etoro-pos-table th:first-child,
  .ta-etoro-pos-table td:first-child {
    text-align: left;
  }
  .ta-etoro-pos-table th {
    font-weight: 600;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.72;
  }
  .ta-etoro-inv-cell {
    color: var(--text-color);
    opacity: 0.9;
    font-variant-numeric: tabular-nums;
  }
  .ta-etoro-pnl-up {
    color: #16a34a;
  }
  .ta-etoro-pnl-down {
    color: #dc2626;
  }
  .ta-etoro-pnl-flat {
    color: var(--text-color);
    opacity: 0.88;
  }

  /* Multi-lot: <details> replaces one data row — summary row is the merged totals (click to expand) */
  tr.ta-etoro-detail-slot td {
    padding: 0;
    vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, var(--text-color) 10%, transparent);
  }
  .ta-etoro-nest-cell {
    background: transparent;
  }
  details.ta-etoro-lot-details {
    width: 100%;
  }
  summary.ta-etoro-lot-summary-trigger {
    display: block;
    list-style: none;
    cursor: pointer;
    user-select: none;
    padding: 0;
    margin: 0;
  }
  summary.ta-etoro-lot-summary-trigger::-webkit-details-marker {
    display: none;
  }
  summary.ta-etoro-lot-summary-trigger .ta-etoro-pos-table.ta-etoro-pos-summary-inline {
    margin: 0;
    width: 100%;
  }
  summary.ta-etoro-lot-summary-trigger .ta-etoro-pos-summary-inline td {
    border-bottom: none;
  }
  td.ta-etoro-sum-name-td {
    font-weight: 600;
  }
  td.ta-etoro-sum-name-td::before {
    content: "▸";
    display: inline-block;
    opacity: 0.45;
    margin-right: 0.35rem;
    font-size: 0.72rem;
    vertical-align: middle;
  }
  details.ta-etoro-lot-details[open] td.ta-etoro-sum-name-td::before {
    content: "▾";
  }
  .ta-etoro-lot-hint {
    font-size: 0.78rem;
    font-weight: 500;
    opacity: 0.62;
    white-space: nowrap;
  }
  .ta-etoro-pos-detail-inner {
    padding: 0.2rem 0.35rem 0.5rem 0.5rem;
    background: color-mix(in srgb, var(--secondary-background-color) 65%, transparent);
    border-top: 1px solid color-mix(in srgb, var(--text-color) 8%, transparent);
  }
  .ta-etoro-pos-table.ta-etoro-pos-table--nested {
    width: 100%;
    font-size: 0.82rem;
    margin: 0;
  }
  .ta-etoro-pos-table.ta-etoro-pos-table--nested th,
  .ta-etoro-pos-table.ta-etoro-pos-table--nested td {
    padding: 0.3rem 0.42rem;
  }
</style>
        """,
        unsafe_allow_html=True,
    )


def _page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.caption(subtitle)
    st.markdown('<hr class="ta-soft"/>', unsafe_allow_html=True)


def _build_config(side: Dict[str, Any]) -> Dict[str, Any]:
    cfg: Dict[str, Any] = merged_app_config()
    cfg["llm_provider"] = side["provider"].lower().strip()
    cfg["deep_think_llm"] = side["deep_model"].strip()
    cfg["quick_think_llm"] = side["quick_model"].strip()
    cfg["output_language"] = side["output_language"].strip() or "English"
    cfg["max_debate_rounds"] = int(side["max_debate"])
    cfg["max_risk_discuss_rounds"] = int(side["max_risk"])
    cfg["checkpoint_enabled"] = bool(side["checkpoint"])
    bu = (side.get("backend_url") or "").strip()
    if bu:
        cfg["backend_url"] = bu
    elif cfg["llm_provider"] == "openrouter":
        cfg["backend_url"] = _default_openrouter_url()

    # Corporate hierarchy (OpenRouter per-agent): only adjustable here + DEFAULT_CONFIG.
    cfg["corporate_hierarchy_enabled"] = bool(side.get("corporate_hierarchy", True))
    or_base = (side.get("corporate_openrouter_base_url") or "").strip()
    cfg["corporate_openrouter_base_url"] = or_base or None
    cfg["llm_fallback_openrouter_model"] = (
        side.get("llm_fallback_openrouter_model") or "openai/gpt-4o-mini"
    ).strip()
    raw = (side.get("agent_llm_routing_json") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            cfg["agent_llm_routing"] = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            cfg["agent_llm_routing"] = {}

    dv = dict(cfg.get("data_vendors") or {})
    dv["news_data"] = side["news_vendor"]
    cfg["data_vendors"] = dv

    cfg.pop("progress_callback", None)
    return cfg


def _selected_analysts(side: Dict[str, Any]) -> List[str]:
    return [k for k in ANALYST_ORDER if side.get(f"analyst_{k}", True)]


def _parse_iso_safe(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relative_age(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    delta = now - dt
    secs = int(delta.total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 60:
        unit = f"{secs}s"
    elif secs < 3600:
        unit = f"{secs // 60}m"
    elif secs < 86400:
        unit = f"{secs // 3600}h"
    else:
        unit = f"{secs // 86400}d"
    return f"in {unit}" if future else f"{unit} ago"


def _level_color(level: str) -> str:
    return {
        "critical": "#d93025",
        "high": "#e8710a",
        "review": "#1a73e8",
        "info": "#5f6368",
    }.get((level or "info").lower(), "#5f6368")


def _level_badge_html(level: str) -> str:
    color = _level_color(level)
    label = html_module.escape((level or "info").upper())
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{color};color:white;font-size:11px;font-weight:600;letter-spacing:0.4px">'
        f"{label}</span>"
    )


def _channel_status_html(row: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for key, channel, label in (
        ("webhook_ok", "webhook", "webhook"),
        ("telegram_ok", "telegram", "telegram"),
        ("smtp_ok", "smtp", "email"),
    ):
        attempted = channel in (row.get("channels_attempted") or [])
        if not attempted:
            continue
        ok = bool(row.get(key))
        color = "#1e8e3e" if ok else "#d93025"
        pieces.append(
            f'<span style="color:{color};font-size:12px;margin-right:8px">'
            f'{"✓" if ok else "✗"} {label}</span>'
        )
    if not pieces:
        pieces.append(
            '<span style="color:#9aa0a6;font-size:12px">saved locally (no webhook/email)</span>'
        )
    return "".join(pieces)


def _read_jsonl_tail(path: Path, *, max_lines: int = 12000) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in text.splitlines()[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_events_for_ui(cfg: Dict[str, Any], *, max_lines: int = 12000) -> List[Dict[str, Any]]:
    from tradingagents.agents.utils.event_log import _default_event_path

    path = _default_event_path(cfg)
    rows = _read_jsonl_tail(path, max_lines=max_lines)
    rows.reverse()
    return rows


def _load_messages_for_ui(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    from tradingagents.portfolio_advisor import load_recent_messages

    return load_recent_messages(cfg, limit=500)


def _load_advisor_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    from tradingagents.portfolio_advisor import state as pa_state

    try:
        return pa_state.load_state(cfg)
    except Exception as e:
        return {"error": str(e)}


def _automation_paused() -> bool:
    return is_clerk_scheduled_automation_paused()


def _summarise_event_key_data(row: Dict[str, Any]) -> str:
    """Turn event key_data into a short, readable sentence for tables and the activity feed."""
    kd = row.get("key_data") or {}
    et = str(row.get("event_type") or "")
    if not isinstance(kd, dict):
        s = str(kd).strip()
        return s[:280] if s else "(no details)"

    if et == "portfolio_bootstrap_complete":
        tickers = kd.get("tickers") or []
        n_ok = kd.get("ok")
        n_err = int(kd.get("errors") or 0)
        td = kd.get("trade_date")
        bits: List[str] = []
        if td:
            bits.append(f"as-of {td}")
        if n_ok is not None:
            bits.append(f"{int(n_ok)} deep run(s) succeeded")
        if n_err:
            bits.append(f"{n_err} failed")
        if isinstance(tickers, list) and tickers:
            tix = ", ".join(str(t).strip().upper() for t in tickers[:10])
            if len(tickers) > 10:
                tix += f" (+{len(tickers) - 10} more)"
            bits.append(tix)
        return " · ".join(bits) if bits else _format_key_data_compact(kd)

    if et == "advisor_pm_cycle":
        tr = str(kd.get("trigger") or "?")
        n = kd.get("forward_tasks_n")
        stx = kd.get("stance_tickers") or []
        prev = ""
        if isinstance(stx, list) and stx:
            prev = ", ".join(str(x) for x in stx[:8])
            if len(stx) > 8:
                prev += "…"
        ex = str(kd.get("excerpt") or "").strip()[:140]
        bits2: List[str] = [f"trigger {tr}"]
        if n is not None:
            bits2.append(f"{int(n)} forward task(s)")
        if prev:
            bits2.append(prev)
        if ex:
            bits2.append(ex)
        if kd.get("request_replan"):
            ro = kd.get("replan_outcome")
            bits2.append(f"replan requested → {ro}" if ro is not None else "replan requested")
        if kd.get("replan_error"):
            bits2.append(f"replan error: {str(kd.get('replan_error'))[:80]}")
        ja = kd.get("jobs_appended")
        if ja is not None and int(ja) > 0:
            bits2.append(f"+{int(ja)} job(s)")
        return " · ".join(bits2) if bits2 else _format_key_data_compact(kd)

    if "rating" in kd and "trade_date" in kd:
        return f"Rating {kd.get('rating')} for as-of date {kd.get('trade_date')}"
    if "rating" in kd:
        return f"Rating: {kd.get('rating')}"

    if "trade_date" in kd and len(kd) <= 3:
        extra = [f"{k}: {v}" for k, v in kd.items() if k != "trade_date" and v not in (None, "")]
        base = f"Analysis date {kd.get('trade_date')}"
        if extra:
            return base + " — " + "; ".join(extra)[:200]
        return base

    if "jobs_queued" in kd:
        n = kd.get("jobs_queued")
        cancelled = kd.get("cancelled_pending")
        mode = kd.get("mode")
        parts = [f"Queued {n} deep-research job(s)"]
        if cancelled:
            parts.append(f"removed {cancelled} older pending job(s)")
        if mode:
            parts.append(f"after {mode} plan refresh")
        return " · ".join(parts) + "."

    if "reason" in kd and len(kd) <= 4:
        msg = _reason_to_sentence(et, str(kd.get("reason") or ""))
        if msg:
            tail = [f"{k}: {v}" for k, v in kd.items() if k != "reason" and v not in (None, "", [], {})]
            if tail:
                return msg + " " + "; ".join(tail)[:160]
            return msg
        return _format_key_data_compact(kd)

    if "overrides" in kd:
        ov = kd.get("overrides")
        if isinstance(ov, list):
            n = len(ov)
            preview = ""
            if ov and isinstance(ov[0], dict):
                preview = str(ov[0].get("ticker") or ov[0].get("reason") or "")[:80]
            return f"Validator applied {n} override(s)" + (f" (e.g. {preview})" if preview else "") + "."
        return f"Validator overrides: {str(ov)[:200]}"

    if "excerpt" in kd:
        ex = str(kd.get("excerpt") or "").strip().replace("\n", " ")
        if len(ex) > 200:
            ex = ex[:197] + "…"
        return ex or "(empty excerpt)"

    if "triggers" in kd:
        trig = kd.get("triggers") or []
        if isinstance(trig, list) and trig:
            tickers = [str(t.get("ticker", "?")) for t in trig[:8]]
            more = f" (+{len(trig) - len(tickers)} more)" if len(trig) > len(tickers) else ""
            return f"{len(trig)} watchlist trigger(s): {', '.join(tickers)}{more}"
        return "Watchlist triggers updated (no tickers in payload)."

    if "error" in kd:
        err = str(kd.get("error") or "").strip().replace("\n", " ")
        return "Error — " + (err[:220] + "…" if len(err) > 220 else err)

    if len(kd) == 1:
        k, v = next(iter(kd.items()))
        if isinstance(v, (str, int, float, bool)):
            return f"{str(k).replace('_', ' ').capitalize()}: {v}"

    return _format_key_data_compact(kd)


def _etoro_env_configured() -> bool:
    return bool(
        (os.environ.get("ETORO_API_KEY") or "").strip()
        and (os.environ.get("ETORO_USER_KEY") or "").strip()
    )


def _ensure_etoro_snapshot() -> Dict[str, Any]:
    if not _etoro_env_configured():
        return {"ok": False, "err": "Set ETORO_API_KEY and ETORO_USER_KEY in `.env`."}
    if "etoro_snap" in st.session_state:
        return st.session_state["etoro_snap"]
    try:
        from tradingagents.integrations.etoro.client import EtoroClient
        from tradingagents.integrations.etoro.portfolio import (
            dedupe_positions,
            instrument_id_from_position,
            iter_positions,
            portfolio_headlines,
            summarize_portfolio,
        )

        client = EtoroClient()
        payload = client.get_portfolio_pnl()
        cp = payload.get("clientPortfolio") or {}
        positions = dedupe_positions(iter_positions(cp))
        ids: List[int] = []
        for p in positions:
            iid = instrument_id_from_position(p)
            if iid is not None:
                ids.append(iid)
        meta = client.get_instruments_metadata(ids) if ids else {}
        _, rows = summarize_portfolio(payload, meta)
        snap = {
            "ok": True,
            "rows": rows,
            "headlines": portfolio_headlines(payload),
        }
        st.session_state["etoro_snap"] = snap
        return snap
    except Exception as e:
        err = {"ok": False, "err": str(e)}
        st.session_state["etoro_snap"] = err
        return err


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s == "—":
        return None
    s = re.sub(r"[^\d.\-+eE]", "", s.replace(",", ""))
    if not s or s in {".", "-", "+", "-.", "+."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pnl_class(n: Optional[float], *, prefix: str = "ta-val") -> str:
    if n is None:
        return f"{prefix}-pnl-flat"
    if n > 0:
        return f"{prefix}-pnl-up"
    if n < 0:
        return f"{prefix}-pnl-down"
    return f"{prefix}-pnl-flat"


def _render_etoro_stats_html(hl: Dict[str, Any]) -> str:
    credit = hl.get("credit")
    unreal = hl.get("unrealized_pnl")
    npos = hl.get("open_positions", "—")
    tinv = hl.get("total_invested_open_usd")
    u = _safe_float(unreal)
    pnl_cls = _pnl_class(u)
    cr = html_module.escape(str(credit if credit is not None else "—"))
    if u is not None:
        pnl_display = html_module.escape(f"{u:,.4f}")
    else:
        pnl_display = html_module.escape(str(unreal if unreal is not None else "—"))
    np = html_module.escape(str(npos if npos is not None else "—"))
    ti = _safe_float(tinv) if tinv is not None else None
    if ti is not None:
        inv_display = html_module.escape(f"{ti:,.2f}")
    else:
        inv_display = html_module.escape(str(tinv if tinv is not None else "—"))
    return f"""
<div class="ta-etoro-stats">
  <div class="ta-etoro-stat">
    <span class="ta-etoro-stat-label">Available balance</span>
    <span class="ta-etoro-stat-value">{cr}</span>
  </div>
  <div class="ta-etoro-stat">
    <span class="ta-etoro-stat-label">Total invested (sum of positions)</span>
    <span class="ta-etoro-stat-value">{inv_display}</span>
  </div>
  <div class="ta-etoro-stat">
    <span class="ta-etoro-stat-label">Unrealized P&amp;L</span>
    <span class="ta-etoro-stat-value {pnl_cls}">{pnl_display}</span>
  </div>
  <div class="ta-etoro-stat">
    <span class="ta-etoro-stat-label">Open positions</span>
    <span class="ta-etoro-stat-value">{np}</span>
  </div>
</div>
"""


def _row_unrealized_pnl_usd(r: Dict[str, Any]) -> Optional[float]:
    """Prefer flattened row field; else same fallback as ``position_unrealized_pnl`` (stale session rows)."""
    p = _safe_float(r.get("unrealizedPnL"))
    if p is not None:
        return p
    ubv = _safe_float(r.get("unitsBaseValueDollars"))
    init = _safe_float(r.get("initialAmountInDollars"))
    if ubv is not None and init is not None:
        return ubv - init
    return None


def _etoro_invested_notional(r: Dict[str, Any]) -> Optional[float]:
    """USD cash in the position: prefer API ``amount`` (margin/notional in USD), else |units|×openRate."""
    amt = _safe_float(r.get("amount"))
    if amt is not None and amt > 0:
        return amt
    u = _safe_float(r.get("units"))
    op = _safe_float(r.get("openRate"))
    if u is None or op is None:
        return None
    inv = abs(u) * abs(op)
    return inv if inv > 0 else None


def _total_invested_usd_from_position_rows(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Sum invested USD across flattened open lots — same basis as the **Invested ($)** table column."""
    if not rows:
        return 0.0
    total = 0.0
    n = 0
    for r in rows:
        v = _etoro_invested_notional(r)
        if v is not None:
            total += v
            n += 1
    if n == 0:
        return None
    return total


def _etoro_ticker_group_key(r: Dict[str, Any]) -> str:
    raw = str(r.get("symbolFull") or "").strip()
    k = _normalize_ticker(raw)
    return k if k else (raw.upper() if raw else "?")


def _etoro_groups_in_order(rows: List[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    od: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()
    for r in rows:
        k = _etoro_ticker_group_key(r)
        if k not in od:
            od[k] = []
        od[k].append(r)
    return list(od.items())


def _etoro_pnl_cell_class(pnl: Optional[float]) -> str:
    if pnl is None:
        return "ta-etoro-pnl-flat"
    if pnl > 0:
        return "ta-etoro-pnl-up"
    if pnl < 0:
        return "ta-etoro-pnl-down"
    return "ta-etoro-pnl-flat"


def _etoro_lot_strings(r: Dict[str, Any]) -> Tuple[str, str, str, str, str, Optional[float]]:
    sym = str(r.get("symbolFull") or "?").strip()
    nm = str(r.get("instrumentDisplayName") or "").strip()
    pid = r.get("positionId")
    bits = [sym]
    if nm:
        bits.append(nm)
    if pid is not None and str(pid).strip():
        bits.append(f"#{pid}")
    pos_label = " — ".join(bits) if len(bits) > 1 else sym
    if len(pos_label) > 96:
        pos_label = pos_label[:93] + "…"
    inv = _etoro_invested_notional(r)
    pnl = _row_unrealized_pnl_usd(r)
    inv_s = f"${inv:,.2f}" if inv is not None and inv > 0 else "—"
    if inv is not None and inv > 0 and pnl is not None:
        total_s = f"${inv + pnl:,.2f}"
        pct_s = f"{(pnl / inv) * 100.0:+.2f}%"
        pnl_s = f"${pnl:+,.2f}"
    elif inv is not None and inv > 0:
        total_s = f"${inv:,.2f}"
        pct_s = "—"
        pnl_s = "—" if pnl is None else f"${pnl:+,.2f}"
    else:
        total_s = "—"
        pct_s = "—"
        pnl_s = "—" if pnl is None else f"${pnl:+,.2f}"
    return pos_label, inv_s, total_s, pct_s, pnl_s, pnl


def _etoro_aggregate_strings(lots: List[Dict[str, Any]]) -> Tuple[str, str, str, str, str, Optional[float]]:
    """One summary row per ticker: position, invested ($), est total, return %, P&amp;L string, pnl for tint."""
    ticker = _etoro_ticker_group_key(lots[0])
    names = [str(x.get("instrumentDisplayName") or "").strip() for x in lots]
    nm0 = next((n for n in names if n), "")
    n = len(lots)
    if n > 1:
        # Lot count appears only on the expander label — avoid repeating "(N lots)" here.
        pos = f"{ticker} — {nm0}" if nm0 else ticker
    else:
        sym = str(lots[0].get("symbolFull") or ticker).strip()
        pos = f"{sym} — {nm0}" if nm0 else sym
    if len(pos) > 88:
        pos = pos[:85] + "…"

    inv_sum = 0.0
    pnl_sum = 0.0
    n_pnl = 0
    total_est_sum = 0.0
    any_inv = False
    for r in lots:
        inv = _etoro_invested_notional(r)
        pnl = _row_unrealized_pnl_usd(r)
        if inv is not None and inv > 0:
            inv_sum += inv
            any_inv = True
            if pnl is not None:
                total_est_sum += inv + pnl
                pnl_sum += pnl
                n_pnl += 1
            else:
                total_est_sum += inv
        elif pnl is not None:
            pnl_sum += pnl
            n_pnl += 1

    if not any_inv and n_pnl == 0:
        return pos, "—", "—", "—", "—", None

    invested_s = f"${inv_sum:,.2f}" if any_inv else "—"
    total_s = f"${total_est_sum:,.2f}" if any_inv else "—"
    agg_pnl_val = pnl_sum if n_pnl > 0 else None
    if any_inv and inv_sum > 0 and agg_pnl_val is not None:
        pct_s = f"{(agg_pnl_val / inv_sum) * 100.0:+.2f}%"
    else:
        pct_s = "—"
    if agg_pnl_val is not None:
        pnl_s = f"${agg_pnl_val:+,.2f}"
    else:
        pnl_s = "—"
    return pos, invested_s, total_s, pct_s, pnl_s, agg_pnl_val


def _html_etoro_pos_row(
    pos_plain: str,
    invested_s: str,
    total_s: str,
    pct_s: str,
    pnl_s: str,
    pnl_for_class: Optional[float],
    *,
    row_class: str = "",
) -> str:
    cls = _etoro_pnl_cell_class(pnl_for_class)
    rc = f' class="{html_module.escape(row_class)}"' if row_class else ""
    return (
        f"<tr{rc}>"
        f"<td>{html_module.escape(pos_plain)}</td>"
        f'<td class="ta-etoro-inv-cell">{html_module.escape(invested_s)}</td>'
        f'<td class="{cls}">{html_module.escape(total_s)}</td>'
        f'<td class="{cls}">{html_module.escape(pct_s)}</td>'
        f'<td class="{cls}">{html_module.escape(pnl_s)}</td>'
        "</tr>"
    )


def _html_etoro_colgroup() -> str:
    """Shared column widths so summary <details> rows align with main body rows."""
    return (
        "<colgroup>"
        '<col class="ta-etoro-w-pos" />'
        '<col class="ta-etoro-w-inv" />'
        '<col class="ta-etoro-w-tot" />'
        '<col class="ta-etoro-w-ret" />'
        '<col class="ta-etoro-w-pnl" />'
        "</colgroup>"
    )


def _html_etoro_pos_table(body_rows: str, *, include_thead: bool) -> str:
    head = ""
    if include_thead:
        head = (
            "<thead><tr>"
            "<th>Position</th>"
            "<th>Invested ($)</th>"
            "<th>Est. total ($)</th>"
            "<th>Return since open (%)</th>"
            "<th>P&amp;L ($)</th>"
            "</tr></thead>"
        )
    cg = _html_etoro_colgroup()
    return f'<table class="ta-etoro-pos-table">{cg}{head}<tbody>{body_rows}</tbody></table>'


def _html_etoro_nested_lot_table(lot_body_rows: str) -> str:
    """Per-lot sub-table (thead repeated for readability when <details> is open)."""
    head = (
        "<thead><tr>"
        "<th>Position</th>"
        "<th>Invested ($)</th>"
        "<th>Est. total ($)</th>"
        "<th>Return since open (%)</th>"
        "<th>P&amp;L ($)</th>"
        "</tr></thead>"
    )
    cg = _html_etoro_colgroup()
    return (
        f'<table class="ta-etoro-pos-table ta-etoro-pos-table--nested">{cg}{head}'
        f"<tbody>{lot_body_rows}</tbody></table>"
    )


def _html_etoro_multilot_details_row(lots: List[Dict[str, Any]]) -> str:
    """One table row: merged totals are the <summary> (click to open per-lot nested table)."""
    pos, invested_s, total_s, pct_s, pnl_s, pnl_cls_val = _etoro_aggregate_strings(lots)
    n = len(lots)
    cls = _etoro_pnl_cell_class(pnl_cls_val)
    name_inner = (
        f'<span class="ta-etoro-sum-name-text">{html_module.escape(pos)}</span>'
        f'<span class="ta-etoro-lot-hint"> · {html_module.escape(str(n))} lots</span>'
    )
    sum_row = (
        f'<tr class="ta-etoro-pos-sum">'
        f'<td class="ta-etoro-sum-name-td">{name_inner}</td>'
        f'<td class="ta-etoro-inv-cell">{html_module.escape(invested_s)}</td>'
        f'<td class="{cls}">{html_module.escape(total_s)}</td>'
        f'<td class="{cls}">{html_module.escape(pct_s)}</td>'
        f'<td class="{cls}">{html_module.escape(pnl_s)}</td>'
        "</tr>"
    )
    sum_inner = (
        f'<table class="ta-etoro-pos-table ta-etoro-pos-summary-inline">'
        f"{_html_etoro_colgroup()}<tbody>{sum_row}</tbody></table>"
    )
    lot_rows = "".join(_html_etoro_pos_row(*_etoro_lot_strings(r)) for r in lots)
    inner = _html_etoro_nested_lot_table(lot_rows)
    return (
        '<tr class="ta-etoro-detail-slot"><td colspan="5" class="ta-etoro-nest-cell">'
        '<details class="ta-etoro-lot-details">'
        f'<summary class="ta-etoro-lot-summary-trigger">{sum_inner}</summary>'
        f'<div class="ta-etoro-pos-detail-inner">{inner}</div>'
        "</details></td></tr>"
    )


def _render_etoro_positions_table(rows: List[Dict[str, Any]]) -> None:
    """Single table: multi-lot tickers use one <details> row whose summary is the merged totals."""
    if not rows:
        st.markdown('<p class="ta-pos-empty">No open positions.</p>', unsafe_allow_html=True)
        return
    groups = _etoro_groups_in_order(rows)
    body_parts: List[str] = []
    for _ticker, lots in groups:
        if len(lots) > 1:
            body_parts.append(_html_etoro_multilot_details_row(lots))
        else:
            pos, invested_s, total_s, pct_s, pnl_s, pnl_cls_val = _etoro_aggregate_strings(lots)
            body_parts.append(
                _html_etoro_pos_row(
                    pos,
                    invested_s,
                    total_s,
                    pct_s,
                    pnl_s,
                    pnl_cls_val,
                    row_class="ta-etoro-pos-sum",
                )
            )
    full = _html_etoro_pos_table("".join(body_parts), include_thead=True)
    st.markdown(f'<div class="ta-etoro-pos-block">{full}</div>', unsafe_allow_html=True)
    st.caption(
        "Grouped by ticker (merged totals). **Invested** is USD at open (API `amount` or |units|×open rate), "
        "without unrealized P&L. **Est. total** adds P&L to that. Return and P&L formulas unchanged; "
        "green / red tint follows net P&L on Est. total / Return / P&L columns. **Click the summary row** (▸) "
        "for multi-lot tickers to open per-lot lines. Read-only."
    )


def _render_etoro_export_section(*, key_prefix: str = "etoro") -> None:
    st.divider()
    st.markdown("**Export watchlist JSON** from your open positions (for scripts or templates).")
    out_path = st.text_input(
        "Output file",
        value=str(PROJECT_ROOT / "etoro_watchlist.generated.json"),
        key=f"{key_prefix}_out_path",
    )
    trig_path = st.text_input(
        "Optional triggers template (JSON)",
        value="",
        key=f"{key_prefix}_trig_path",
        help="Copy triggers and analyst settings from this file; tickers still come from eToro.",
    )
    if st.button("Save watchlist JSON", type="primary", key=f"{key_prefix}_export_btn"):
        try:
            from tradingagents.integrations.etoro.clerk_bridge import (
                fetch_clerk_watchlist_from_etoro,
            )

            tpl = Path(trig_path.strip()) if trig_path.strip() else None
            wl = fetch_clerk_watchlist_from_etoro(tpl)
            outp = Path(out_path.strip())
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(wl.to_json_dict(), indent=2), encoding="utf-8")
            st.success(f"Saved `{outp.resolve()}` — tickers: {', '.join(wl.tickers)}")
        except Exception as e:
            st.error(str(e))


def _render_etoro_portfolio_block(
    *,
    show_export: bool,
    compact_title: bool = False,
    show_section_title: bool = True,
    key_prefix: str = "etoro",
) -> None:
    if not _etoro_env_configured():
        return

    if show_section_title:
        st.subheader("Portfolio snapshot" if compact_title else "Live portfolio (read-only)")

    c0, c1 = st.columns([4, 1])
    with c0:
        st.caption("Read-only data from your eToro API keys. This app does not send orders.")
    with c1:
        if st.button("Refresh", key=f"{key_prefix}_snap_refresh", use_container_width=True):
            st.session_state.pop("etoro_snap", None)
            st.rerun()

    snap = _ensure_etoro_snapshot()
    if not snap.get("ok"):
        st.warning(snap.get("err") or "Could not load eToro portfolio.")
        return

    hl = dict(snap.get("headlines") or {})
    rows: List[Dict[str, Any]] = list(snap.get("rows") or [])
    hl["total_invested_open_usd"] = _total_invested_usd_from_position_rows(rows)

    st.markdown(
        '<div class="ta-etoro-wrap">'
        + _render_etoro_stats_html(hl)
        + "</div>",
        unsafe_allow_html=True,
    )

    # Always show the table here (do not hide inside a collapsed expander on Dashboard).
    st.subheader("Open positions")
    _render_etoro_positions_table(rows)

    if show_export:
        _render_etoro_export_section(key_prefix="etoro")


def _render_results(final_state: Dict[str, Any], decision: Any) -> None:
    st.subheader("Final recommendation")
    st.markdown(str(decision) if decision else "_No decision._")

    tabs = st.tabs(
        ["Market", "Sentiment", "News", "Fundamentals", "Research", "Trader", "Risk / PM"]
    )
    with tabs[0]:
        st.markdown(final_state.get("market_report") or "_No report._")
    with tabs[1]:
        st.markdown(final_state.get("sentiment_report") or "_No report._")
    with tabs[2]:
        st.markdown(final_state.get("news_report") or "_No report._")
    with tabs[3]:
        st.markdown(final_state.get("fundamentals_report") or "_No report._")
    with tabs[4]:
        inv = final_state.get("investment_debate_state") or {}
        st.markdown("##### Bull\n" + (inv.get("bull_history") or "_Empty_"))
        st.markdown("##### Bear\n" + (inv.get("bear_history") or "_Empty_"))
        st.markdown("##### Research manager\n" + (inv.get("judge_decision") or "_Empty_"))
        st.markdown("##### Plan\n" + (final_state.get("investment_plan") or "_Empty_"))
    with tabs[5]:
        st.markdown(final_state.get("trader_investment_plan") or "_Empty_")
    with tabs[6]:
        r = final_state.get("risk_debate_state") or {}
        st.markdown("##### Aggressive\n" + (r.get("aggressive_history") or "_Empty_"))
        st.markdown("##### Conservative\n" + (r.get("conservative_history") or "_Empty_"))
        st.markdown("##### Neutral\n" + (r.get("neutral_history") or "_Empty_"))
        st.markdown("##### Portfolio manager\n" + (r.get("judge_decision") or "_Empty_"))
        st.markdown("##### Final decision (raw)\n" + (final_state.get("final_trade_decision") or "_Empty_"))


def _sidebar_shell() -> str:
    st.sidebar.markdown("### TradingAgents")
    st.sidebar.caption("Multi-agent research on your machine.")
    st.sidebar.divider()
    page = st.sidebar.radio(
        "Go to",
        NAV_PAGES,
        label_visibility="visible",
    )
    st.sidebar.divider()
    st.sidebar.caption("Python 3.10+ · use a venv if `pip install` is blocked (`scripts/setup-venv.sh`).")
    if not _etoro_env_configured():
        st.sidebar.caption("eToro: add `ETORO_API_KEY` + `ETORO_USER_KEY` to `.env` for the portfolio strip.")
    return page


def _ensure_fa_session_seeds() -> None:
    if st.session_state.get("_ui_fa_seeded_v1"):
        return
    bc = merged_app_config()
    st.session_state["_ui_fa_seeded_v1"] = True
    st.session_state.setdefault("cb_corporate_hierarchy", bool(bc.get("corporate_hierarchy_enabled", True)))
    st.session_state.setdefault("txt_corporate_or_url", str(bc.get("corporate_openrouter_base_url") or ""))
    st.session_state.setdefault(
        "txt_or_fallback_model",
        str(bc.get("llm_fallback_openrouter_model") or "openai/gpt-4o-mini"),
    )
    routing = bc.get("agent_llm_routing") or {}
    st.session_state.setdefault(
        "ta_agent_llm_routing_json",
        json.dumps(routing, indent=2) if routing else "",
    )


def message_log_path_for_display(cfg: Dict[str, Any]) -> str:
    from tradingagents.portfolio_advisor import message_log_path

    return str(message_log_path(cfg))


def _render_messages_panel(cfg: Dict[str, Any], *, key_prefix: str = "msg") -> None:
    rows = _load_messages_for_ui(cfg)
    top = st.columns([2, 2, 3, 1])
    levels_avail = sorted({(r.get("level") or "info") for r in rows})
    level_choice = top[0].selectbox(
        "Level",
        options=["(all)"] + levels_avail,
        index=0,
        key=f"{key_prefix}_level_filter",
    )
    delivery_choice = top[1].selectbox(
        "Delivery",
        options=["(all)", "delivered", "not delivered"],
        index=0,
        key=f"{key_prefix}_delivery_filter",
    )
    query = top[2].text_input(
        "Search subject/body",
        value="",
        placeholder="ticker, keyword, …",
        key=f"{key_prefix}_search",
    ).strip().lower()
    if top[3].button("Refresh", use_container_width=True, key=f"{key_prefix}_refresh"):
        st.rerun()

    if not rows:
        st.info(
            "No messages yet. The advisor writes here whenever it sends a webhook/email. "
            f"Log path: `{message_log_path_for_display(cfg)}`."
        )
        return

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        if level_choice != "(all)" and r.get("level") != level_choice:
            continue
        if delivery_choice == "delivered" and not r.get("delivered"):
            continue
        if delivery_choice == "not delivered" and r.get("delivered"):
            continue
        if query:
            hay = (str(r.get("subject", "")) + " " + str(r.get("body", ""))).lower()
            if query not in hay:
                continue
        filtered.append(r)

    st.caption(f"Showing {len(filtered)} of {len(rows)} messages.")
    for i, r in enumerate(filtered):
        ts = _parse_iso_safe(r.get("timestamp"))
        ts_disp = (
            ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if ts is not None else str(r.get("timestamp", ""))
        )
        age = _relative_age(ts)
        subject = _message_subject_display(str(r.get("subject", "(no subject)")))
        header = (
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
            f'{_level_badge_html(str(r.get("level", "info")))}'
            f'<span style="font-size:12px;color:#5f6368">{ts_disp} · {age}</span>'
            f'</div>'
            f'<div style="font-weight:600">{html_module.escape(subject)}</div>'
            f'<div style="margin-top:2px">{_channel_status_html(r)}</div>'
        )
        st.markdown(header, unsafe_allow_html=True)
        with st.expander("Body", expanded=False):
            body_raw = str(r.get("body", "") or "")
            pv = _first_line_preview(body_raw, max_len=200)
            if pv:
                st.caption("Preview: " + pv)
            st.text(body_raw[:50000] or "(empty)")
        if i < len(filtered) - 1:
            st.divider()


@st.dialog("All messages", width="large")
def _messages_dialog() -> None:
    _render_messages_panel(merged_app_config(), key_prefix="dlg_msg")


def _activity_collate(cfg: Dict[str, Any], *, limit: int = 25) -> List[Dict[str, Any]]:
    exec_types = set(_EXECUTION_EVENT_TYPES + _WATCHDOG_EVENT_TYPES)
    events = _load_events_for_ui(cfg, max_lines=4000)
    ev_rows: List[Dict[str, Any]] = []
    for e in events:
        et = str(e.get("event_type") or "")
        if et not in exec_types:
            continue
        ts = _parse_iso_safe(e.get("timestamp"))
        if ts is None:
            continue
        summ = _summarise_event_key_data(e)
        title = f"{_event_type_title(et)} — {_ticker_display(e.get('ticker'))}"
        ev_rows.append({
            "ts": ts,
            "kind": "event",
            "title": title,
            "detail": summ[:320],
        })
        if len(ev_rows) >= 16:
            break
    msg_rows: List[Dict[str, Any]] = []
    for r in _load_messages_for_ui(cfg)[:24]:
        ts = _parse_iso_safe(r.get("timestamp"))
        if ts is None:
            continue
        subj = _message_subject_display(str(r.get("subject", "")))
        ch = _channel_status_plain(r)
        prev = _first_line_preview(str(r.get("body") or ""), max_len=160)
        detail = ch + (" — " + prev if prev else "")
        msg_rows.append({
            "ts": ts,
            "kind": "msg",
            "title": subj,
            "detail": detail[:360],
        })
    merged = ev_rows + msg_rows
    merged.sort(key=lambda x: x["ts"], reverse=True)
    return merged[:limit]


def _render_activity_feed(cfg: Dict[str, Any]) -> None:
    st.subheader("Agent activity")
    rows = _activity_collate(cfg)
    if not rows:
        st.caption("No recent execution events or outbound messages yet.")
        return
    for row in rows:
        ts = row["ts"]
        if ts.tzinfo:
            wall = ts.astimezone().strftime("%b %d, %I:%M %p %Z")
        else:
            wall = ts.strftime("%Y-%m-%d %H:%M")
        age = _relative_age(ts)
        tag = "Outbox" if row["kind"] == "msg" else "Advisor run"
        st.markdown(
            f"<div><span style='font-size:0.72rem;text-transform:uppercase;letter-spacing:0.06em;opacity:0.7'>"
            f"{html_module.escape(tag)}</span> · "
            f"<span style='font-size:0.72rem;opacity:0.75'>{html_module.escape(wall)}</span> · "
            f"<span style='font-size:0.72rem;opacity:0.75'>{html_module.escape(age)}</span></div>"
            f"<div style='font-weight:600;margin-top:0.35rem'>{html_module.escape(row['title'])}</div>"
            f"<div style='opacity:0.88;font-size:0.92rem;margin-top:0.25rem;line-height:1.45'>"
            f"{html_module.escape(row['detail'])}</div>",
            unsafe_allow_html=True,
        )
        st.markdown('<hr class="ta-soft"/>', unsafe_allow_html=True)


def _render_event_log_expander(cfg: Dict[str, Any]) -> None:
    with st.expander("Raw event log (filters)", expanded=False):
        events = _load_events_for_ui(cfg, max_lines=12000)
        if not events:
            from tradingagents.agents.utils.event_log import _default_event_path

            st.info(
                "No events recorded yet. Log path: "
                f"`{_default_event_path(cfg)}`."
            )
            return

        all_types = sorted({str(e.get("event_type") or "") for e in events if e.get("event_type")})
        all_tickers = sorted({str(e.get("ticker") or "*") for e in events if e.get("ticker")})

        f1, f2, f3, f4 = st.columns([2, 2, 1.5, 1])
        selected_types = f1.multiselect(
            "Event types",
            options=all_types,
            default=[],
            placeholder="(all)",
            key="dash_trig_types",
            format_func=_event_type_title,
        )
        selected_tickers = f2.multiselect(
            "Tickers",
            options=all_tickers,
            default=[],
            placeholder="(all)",
            key="dash_trig_tickers",
            format_func=_ticker_display,
        )
        days = f3.slider("Lookback (days)", 1, 365, 30, key="dash_trig_days")
        min_weight = f4.slider("Min weight", 1, 10, 1, key="dash_trig_weight")

        if f4.button("Refresh", use_container_width=True, key="dash_trig_refresh"):
            st.rerun()

        from tradingagents.agents.utils.event_log import EVENT_WEIGHTS

        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        filtered: List[Dict[str, Any]] = []
        for r in events:
            ts = _parse_iso_safe(r.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue
            et = str(r.get("event_type") or "")
            if selected_types and et not in selected_types:
                continue
            tk = str(r.get("ticker") or "*")
            if selected_tickers and tk not in selected_tickers:
                continue
            w = int(EVENT_WEIGHTS.get(et, 1))
            if w < int(min_weight):
                continue
            filtered.append(r)

        st.caption(f"Showing {len(filtered)} of {len(events)} events.")

        rows_render: List[Dict[str, Any]] = []
        for r in filtered:
            ts = _parse_iso_safe(r.get("timestamp"))
            et = str(r.get("event_type") or "")
            rows_render.append({
                "When": ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else str(r.get("timestamp", "")),
                "Age": _relative_age(ts),
                "Scope": _ticker_display(r.get("ticker")),
                "Activity": _event_type_title(et),
                "Weight": EVENT_WEIGHTS.get(et, 1),
                "Details": _summarise_event_key_data(r),
            })
        st.dataframe(rows_render, hide_index=True, use_container_width=True, height=360)

        if not filtered:
            st.caption("No events match the filter.")
            return
        options = []
        for i, r in enumerate(filtered[:200]):
            ts = _parse_iso_safe(r.get("timestamp"))
            ts_s = ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else str(r.get("timestamp", ""))[:16]
            title = _event_type_title(str(r.get("event_type") or ""))
            d = _ticker_display(r.get("ticker"))
            options.append(f"{i+1}. {ts_s} · {title} · {d}")
        pick = st.selectbox("Inspect row", options=options, index=0, key="dash_trig_pick")
        if pick:
            idx = int(pick.split(".", 1)[0]) - 1
            if 0 <= idx < len(filtered):
                st.code(json.dumps(filtered[idx], indent=2, ensure_ascii=False), language="json")


def _shift_calendar_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    month += delta
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return year, month


def _pending_jobs_raw(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pending advisor job rows from state (dicts as stored), sorted by scheduled time."""
    st0 = _load_advisor_state(cfg)
    if "error" in st0 and not st0.get("jobs"):
        return []
    out: List[Dict[str, Any]] = []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for j in st0.get("jobs") or []:
        if j.get("status") != "pending":
            continue
        when = _parse_iso_safe(j.get("scheduled_at"))
        if when is None:
            continue
        out.append(j)
    out.sort(key=lambda x: _parse_iso_safe(x.get("scheduled_at")) or epoch)
    return out


def _pending_job_entries(cfg: Dict[str, Any]) -> List[Tuple[datetime, str, str]]:
    """UTC scheduled time, ticker, execution tier for each pending job."""
    out: List[Tuple[datetime, str, str]] = []
    for j in _pending_jobs_raw(cfg):
        when = _parse_iso_safe(j.get("scheduled_at"))
        if when is None:
            continue
        tick = str(j.get("ticker") or "?").strip().upper() or "?"
        tier = str(j.get("execution_tier") or "job")
        out.append((when, tick, tier))
    return out


def _ts_to_local_date(ts: datetime) -> date:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().date()


def _group_pending_jobs_by_local_day(jobs: List[Dict[str, Any]]) -> Dict[date, List[Dict[str, Any]]]:
    by_day: Dict[date, List[Dict[str, Any]]] = {}
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for j in jobs:
        when = _parse_iso_safe(j.get("scheduled_at"))
        if when is None:
            continue
        d = _ts_to_local_date(when)
        by_day.setdefault(d, []).append(j)
    for d in by_day:
        by_day[d].sort(
            key=lambda x: _parse_iso_safe(x.get("scheduled_at")) or epoch,
        )
    return by_day


def _jobs_grouped_by_local_day(entries: List[Tuple[datetime, str, str]]) -> Dict[date, List[str]]:
    by_day: Dict[date, List[str]] = {}
    for when, tick, tier in entries:
        d = _ts_to_local_date(when)
        by_day.setdefault(d, []).append(f"{tick} ({tier})")
    return by_day


@st.dialog("Scheduled jobs", width="large")
def _planner_jobs_modal(d: date, jobs: List[Dict[str, Any]]) -> None:
    st.markdown(f"**{d.strftime('%A, %B %d, %Y')}**")
    if not jobs:
        st.info("No pending advisor jobs on this day.")
    else:
        for j in jobs:
            when = _parse_iso_safe(j.get("scheduled_at"))
            wall = when.astimezone().strftime("%I:%M %p %Z") if when else "—"
            tick = str(j.get("ticker") or "?").strip().upper() or "?"
            tier = str(j.get("execution_tier") or "single_model")
            jtype = str(j.get("job_type") or "routine_monitoring")
            jid = str(j.get("id") or "")
            reason = str(j.get("reason") or "").strip()
            parts = [
                "- **",
                html_module.escape(tick),
                "** · ",
                html_module.escape(wall),
                " · `",
                html_module.escape(tier),
                "` · `",
                html_module.escape(jtype),
                "`",
            ]
            if jid:
                parts.extend([" · `", html_module.escape(jid), "`"])
            st.markdown("".join(parts), unsafe_allow_html=False)
            if reason:
                st.caption(reason[:500] + ("…" if len(reason) > 500 else ""))
    if st.button("Close", use_container_width=True, type="primary", key="dash_cal_dialog_close"):
        st.rerun()


def _render_notifications_box(cfg: Dict[str, Any]) -> None:
    rows = _activity_collate(cfg, limit=14)
    with st.container(border=True):
        if not rows:
            st.caption("No recent outbound messages or advisor runs yet.")
            return
        for i, row in enumerate(rows):
            ts = row["ts"]
            wall = ts.astimezone().strftime("%b %d, %I:%M %p") if ts.tzinfo else ts.strftime("%m/%d %H:%M")
            age = _relative_age(ts)
            tag = "Outbox" if row["kind"] == "msg" else "Run"
            st.markdown(
                f"<div style='font-size:0.72rem;opacity:0.75'>{html_module.escape(tag)} · "
                f"{html_module.escape(wall)} · {html_module.escape(age)}</div>"
                f"<div style='font-weight:600;font-size:0.95rem;margin:0.2rem 0 0.15rem'>"
                f"{html_module.escape(row['title'])}</div>"
                f"<div style='font-size:0.85rem;opacity:0.88;line-height:1.35'>"
                f"{html_module.escape(row['detail'])}</div>",
                unsafe_allow_html=True,
            )
            if i < len(rows) - 1:
                st.markdown(
                    '<div style="height:1px;background:rgba(128,128,128,0.2);margin:0.55rem 0"></div>',
                    unsafe_allow_html=True,
                )


def _render_planner_calendar(cfg: Dict[str, Any]) -> None:
    """Full-width month grid: Streamlit buttons open a modal (no URL navigation)."""
    pending_raw = _pending_jobs_raw(cfg)
    jobs_by_day = _group_pending_jobs_by_local_day(pending_raw)
    entries: List[Tuple[datetime, str, str]] = []
    for j in pending_raw:
        when = _parse_iso_safe(j.get("scheduled_at"))
        if when is None:
            continue
        tick = str(j.get("ticker") or "?").strip().upper() or "?"
        tier = str(j.get("execution_tier") or "job")
        entries.append((when, tick, tier))
    job_str_by_day = _jobs_grouped_by_local_day(entries)
    today = date.today()
    y = int(st.session_state.get("dash_cal_year", today.year))
    m = int(st.session_state.get("dash_cal_month", today.month))
    if m < 1 or m > 12:
        y, m = today.year, today.month

    cal = cal_module.Calendar(firstweekday=cal_module.MONDAY)
    weeks = cal.monthdatescalendar(y, m)

    nav_l, nav_c, nav_r = st.columns([1, 6, 1])
    with nav_l:
        if st.button("Prev", key="dash_cal_prev", use_container_width=True):
            y, m = _shift_calendar_month(y, m, -1)
            st.session_state["dash_cal_year"] = y
            st.session_state["dash_cal_month"] = m
            st.rerun()
    with nav_c:
        st.markdown(
            "<div style='text-align:center;font-size:1.35rem;font-weight:650;letter-spacing:-0.02em;margin:0.15rem 0'>"
            f"{html_module.escape(cal_module.month_name[m])} {y}</div>",
            unsafe_allow_html=True,
        )
        st.caption("Local timezone · hover a day for tickers · click for job popup")
    with nav_r:
        if st.button("Next", key="dash_cal_next", use_container_width=True):
            y, m = _shift_calendar_month(y, m, 1)
            st.session_state["dash_cal_year"] = y
            st.session_state["dash_cal_month"] = m
            st.rerun()

    with st.container(border=True):
        hcols = st.columns(7, gap="small")
        hdrs = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for col, h in zip(hcols, hdrs):
            with col:
                st.caption(h)
        for wi, week in enumerate(weeks):
            cols = st.columns(7, gap="small")
            for di, (col, d) in enumerate(zip(cols, week)):
                is_pad = d.month != m
                n = len(jobs_by_day.get(d, []))
                jobs = job_str_by_day.get(d, [])
                tip = "; ".join(jobs[:8])
                if len(jobs) > 8:
                    tip += f" (+{len(jobs) - 8} more)"
                help_txt = tip if tip else "No pending jobs"
                if len(help_txt) > 280:
                    help_txt = help_txt[:277] + "…"
                with col:
                    if is_pad:
                        st.button(
                            str(d.day),
                            key=f"dash_cal_pad_{y}_{m}_{wi}_{di}_{d.isoformat()}",
                            disabled=True,
                            use_container_width=True,
                        )
                        continue
                    label = f"{d.day} ({n})" if n else str(d.day)
                    btn_type = "primary" if d == today else "secondary"
                    if st.button(
                        label,
                        key=f"dash_cal_day_{d.isoformat()}",
                        use_container_width=True,
                        type=btn_type,
                        help=help_txt,
                    ):
                        _planner_jobs_modal(d, jobs_by_day.get(d, []))
        if not pending_raw:
            st.caption(
                "No pending jobs in advisor state. After **Deploy** / init, scheduled work appears here."
            )


def _render_schedule_timeline_chart(cfg: Dict[str, Any]) -> None:
    entries = _pending_job_entries(cfg)
    start = date.today()
    days_n = 21
    day_list = [start + timedelta(days=i) for i in range(days_n)]
    counts = {d: 0 for d in day_list}
    for when, _tick, _tier in entries:
        d = _ts_to_local_date(when)
        if d in counts:
            counts[d] += 1
    df = pd.DataFrame(
        {
            "Day": [(start + timedelta(days=i)).strftime("%a %m/%d") for i in range(days_n)],
            "Pending jobs due": [counts[start + timedelta(days=i)] for i in range(days_n)],
        }
    )
    st.bar_chart(df.set_index("Day"), height=240, use_container_width=True)


def _render_last_bootstrap_panel(cfg: Dict[str, Any]) -> None:
    """Prominent summary from advisor state (written when bootstrap completes)."""
    st0 = _load_advisor_state(cfg)
    if isinstance(st0, dict) and st0.get("error") and not st0.get("last_bootstrap_iso"):
        return
    summ = st0.get("last_bootstrap_summary") if isinstance(st0, dict) else None
    iso = st0.get("last_bootstrap_iso") if isinstance(st0, dict) else None
    if not iso and not summ:
        st.markdown("##### Last portfolio bootstrap")
        st.caption(
            "No full-portfolio bootstrap recorded in advisor state yet. "
            "CLI: `python -m cli.main advisor portfolio bootstrap` "
            "(add `--resume --date YYYY-MM-DD` to skip tickers that already finished that day), "
            "or enable **Full portfolio scan on deploy** on first-time **Deploy**."
        )
        return

    with st.container(border=True):
        st.markdown("##### Last portfolio bootstrap")
        if iso:
            ts = _parse_iso_safe(str(iso))
            if ts is not None:
                wall = ts.astimezone().strftime("%Y-%m-%d %I:%M %p %Z")
                age = _relative_age(ts)
                st.markdown(f"**When:** {html_module.escape(wall)} · _{html_module.escape(age)}_", unsafe_allow_html=True)
            else:
                st.markdown(f"**When:** `{html_module.escape(str(iso))}`")

        if isinstance(summ, dict) and summ:
            td = str(summ.get("trade_date") or "").strip()
            if td:
                st.markdown(f"**Analysis as-of date:** `{html_module.escape(td)}`", unsafe_allow_html=True)
            tickers = summ.get("tickers") or []
            ok = summ.get("ok")
            err = int(summ.get("errors") or 0)
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Tickers run", len(tickers) if isinstance(tickers, list) else "—")
            with c2:
                st.metric("Succeeded", int(ok) if ok is not None else "—")
            with c3:
                st.metric("Failed", err)
            ratings = summ.get("ratings") or {}
            if isinstance(ratings, dict) and ratings:
                st.caption("Rating mix: " + ", ".join(f"{k} ×{v}" for k, v in sorted(ratings.items(), key=lambda kv: str(kv[0]))))
            if isinstance(tickers, list) and tickers:
                with st.expander("Ticker list", expanded=False):
                    st.text(", ".join(str(t).strip().upper() for t in tickers))
        else:
            st.caption(
                "This install has a bootstrap timestamp but no stored summary. "
                "Run **advisor portfolio bootstrap** again to populate details."
            )

        rd = Path(str(cfg.get("results_dir", "."))).expanduser()
        deep_rel = rd / "clerk_deep"
        st.caption(f"Per-ticker markdown: `{deep_rel}` (under each ticker folder, `*_clerk_triggered.md`).")
        st.caption(
            "Each new full-graph run prepends the **latest** saved clerk snapshot into agent context "
            "(toggle `portfolio_advisor_inject_prior_clerk_report`). "
            "Interrupted bootstrap: `advisor portfolio bootstrap --resume --date YYYY-MM-DD`. "
            "Planner queue: cron `advisor portfolio run-due`."
        )


def _render_pm_council_panel(cfg: Dict[str, Any]) -> None:
    """Latest advisor-level PM cycle (decisions, tasks, memory) from JSONL + markdown trail."""
    from tradingagents.portfolio_advisor.advisor_pm import (
        _pm_unified_memory,
        load_recent_pm_cycles,
        pm_log_path,
        pm_memory_path,
    )

    st.markdown("##### Portfolio manager (PM)")
    cap = f"Structured log: `{pm_log_path(cfg)}` · rolling markdown: `{pm_memory_path(cfg)}`"
    if _pm_unified_memory(cfg):
        cap += " · unified with LangGraph `memory_log_path` when set."
    st.caption(cap)
    rows = load_recent_pm_cycles(cfg, limit=8)
    if not rows:
        st.info(
            "No PM cycles yet. CLI: `python -m cli.main advisor portfolio pm-cycle` "
            "(optional `--trigger after_bootstrap`)."
        )
        if _etoro_env_configured() and st.button(
            "Run PM cycle now", help="Calls the reasoning model; may take a minute.", key="dash_pm_cycle_first"
        ):
            _run_pm_cycle_from_ui(cfg, "ui_manual")
        return

    latest = rows[0]
    ts = str(latest.get("timestamp") or "")
    trig = str(latest.get("trigger") or "")
    model = str(latest.get("model") or "")
    res = latest.get("result") or {}
    if not isinstance(res, dict):
        res = {}
    ex = str(res.get("executive_summary") or "").strip()
    with st.container(border=True):
        st.caption(f"Latest · {html_module.escape(ts)} · trigger `{html_module.escape(trig)}` · model `{html_module.escape(model)}`")
        if ex:
            st.text(ex[:8000] + ("…" if len(ex) > 8000 else ""))
        stances = res.get("stances") or []
        if isinstance(stances, list) and stances:
            st.markdown("**Stances**")
            for s in stances[:40]:
                if not isinstance(s, dict):
                    continue
                tk = str(s.get("ticker") or "?")
                stc = str(s.get("stance") or "?")
                ra = str(s.get("rationale") or "").strip()
                st.text(f"{tk} — {stc} — {ra[:500]}")
        tasks = res.get("forward_tasks") or []
        if isinstance(tasks, list) and tasks:
            st.markdown("**Forward tasks**")
            for t in tasks[:30]:
                st.text(f"• {str(t)[:500]}")
        mem = str(res.get("memory_note") or "").strip()
        if mem:
            st.markdown("**Memory note (next cycle)**")
            st.text(mem[:6000])
        act = latest.get("actions_taken") or {}
        if isinstance(act, dict) and act.get("apply_enabled", True):
            rp = act.get("replan_outcome")
            err = act.get("replan_error")
            ja = int(act.get("jobs_appended") or 0)
            if rp is not None or err or ja:
                st.markdown("**Follow-up actions (replan / extra jobs)**")
                if rp is not None:
                    st.text(f"Replan outcome: {rp}")
                if err:
                    st.text(f"Replan error: {err}")
                if ja:
                    st.text(f"Extra jobs appended: {ja}")

    with st.expander("Older PM cycles", expanded=False):
        for row in rows[1:8]:
            tss = str(row.get("timestamp") or "")
            trg = str(row.get("trigger") or "")
            rr = row.get("result") or {}
            prev = str(rr.get("executive_summary") if isinstance(rr, dict) else "")[:240]
            st.markdown(f"**{html_module.escape(tss)}** `{html_module.escape(trg)}` — {html_module.escape(prev)}…")

    if _etoro_env_configured() and st.button(
        "Run PM cycle now",
        help="Re-runs the PM with a fresh eToro snapshot (reasoning model).",
        key="dash_pm_cycle_refresh",
    ):
        _run_pm_cycle_from_ui(cfg, "ui_manual")


def _run_pm_cycle_from_ui(cfg: Dict[str, Any], trigger: str) -> None:
    from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

    with st.spinner("Running PM cycle (LLM)…"):
        try:
            run_pm_cycle(cfg, trigger=trigger)
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_advisor_how_it_works() -> None:
    """Reduce confusion: bootstrap vs jobs vs where output appears."""
    with st.expander("How the portfolio advisor works (bootstrap vs jobs vs cloud)", expanded=False):
        st.markdown(
            """
**Three different things**

1. **`advisor portfolio init`** (first time only, or `init --force` to reset)  
   Builds the **LLM schedule** → pending rows in **Planner calendar** / timeline.  
   Does **not** run deep research unless you enabled bootstrap-on-init in config.

2. **`advisor portfolio bootstrap`**  
   Runs the **expensive full multi-agent graph** on each holding **now**, writes markdown under  
   `results_dir/clerk_deep/<TICKER>/`, updates **Last portfolio bootstrap** on this page, appends **Messages**.  
   It does **not** replace or create the planner job queue by itself.  
   By default (`portfolio_advisor_pm_enabled` + `portfolio_advisor_pm_after_each_langgraph`), an **advisor PM** pass runs **right after each ticker’s LangGraph run** (portfolio-level memo, stances, scheduling hints, logged here and in PM JSONL).  
   When `portfolio_advisor_pm_cycle_after_bootstrap` is on, a **final** PM pass also runs after the whole bootstrap batch (uses the fresh per-portfolio summary).  
   When `portfolio_advisor_pm_cycle_on_portfolio_change` is on, a PM pass can also run on **bootstrap** book-hash changes and on **`advisor portfolio weekly`** when tickers or the snapshot fingerprint move vs state.

3. **`advisor portfolio replan`**  
   Refreshes the **job queue** from a new planner pass (cancels old pending jobs).  
   Use this when you expected “jobs to appear” after holdings changed or you never got a schedule you like.

**To actually run scheduled work** (while your laptop is off), a **server cron** must call  
`python -m cli.main advisor portfolio run-due` (and optionally `weekly`, `replan`) on a timer.  
The Streamlit UI only **displays** state; it does not run those commands in the background.

**Edit code from Cursor on your laptop**  
Use **git**: commit here → `git push` → on the cloud machine `git pull` → restart Streamlit / cron.  
(Or use **Remote SSH** in Cursor against the same repo on the server.)

**Quick CLI audit** (same folder as `.env`):  
`python -m cli.main advisor portfolio status` — see jobs and timestamps.  
`python -m cli.main advisor portfolio catalogue --json` — export a catalogue file.  
`python -m cli.main advisor portfolio pm-cycle` — one **PM council** pass (big-picture memo, stances, forward tasks, memory); also in the Dashboard.  
When **`portfolio_advisor_pm_unified_memory`** is on (default), PM markdown appends to the same **`memory_log_path`** file as LangGraph decisions (delimiter-separated blocks), and each PM prompt includes a recent tail of that log.  
When **`portfolio_advisor_pm_apply_actions`** is on, the PM can set **`request_replan`** and/or **`append_jobs`** in structured output to refresh the planner queue or queue extra research jobs.  
Set **`TRADINGAGENTS_PORTFOLIO_ADVISOR_PM_ENABLED=false`** only if you need to pause all advisor PM automation (cost or debugging); the LangGraph **node** named Portfolio Manager inside each ticker run is separate.
            """.strip()
        )


def _render_dashboard_planning_row(cfg: Dict[str, Any]) -> None:
    st.subheader("Schedule")
    _render_planner_calendar(cfg)
    st.caption("Bar height = number of **pending** advisor jobs scheduled on that local calendar day (next 21 days).")
    _render_schedule_timeline_chart(cfg)


def _page_dashboard() -> None:
    cfg = merged_app_config()
    paused = _automation_paused()

    # ── Header + controls ──────────────────────────────────────────────────
    hdr_col, ctrl_col = st.columns([2, 3])
    with hdr_col:
        st.title("Dashboard")
        if paused:
            st.markdown(
                "<span style='color:#dc2626;font-weight:600;font-size:0.9rem'>&#9679; Automation paused</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<span style='color:#16a34a;font-weight:600;font-size:0.9rem'>&#9679; Automation running</span>",
                unsafe_allow_html=True,
            )
    with ctrl_col:
        st.write("")
        c_msgs, c_deploy, c_pause = st.columns(3)
        with c_msgs:
            open_msgs = st.button("Messages", use_container_width=True, key="dash_open_msgs")
        with c_deploy:
            deploy = st.button(
                "Deploy",
                type="primary",
                use_container_width=True,
                disabled=not _etoro_env_configured(),
                key="dash_deploy",
            )
        with c_pause:
            if paused:
                resume = st.button("Resume", use_container_width=True, key="dash_resume")
                pause_clicked = False
            else:
                resume = False
                pause_clicked = st.button("Pause", use_container_width=True, key="dash_pause")

    if open_msgs:
        _messages_dialog()

    if resume and paused:
        set_clerk_scheduled_automation_paused(False)
        st.success("Automation resumed.")
        st.rerun()

    if not paused and pause_clicked:
        set_clerk_scheduled_automation_paused(True)
        st.info("Automation paused. Use **Resume** to continue.")
        st.rerun()

    if deploy and _etoro_env_configured():
        full_scan = bool(st.session_state.get("dash_full_scan", False))
        try:
            from tradingagents.portfolio_advisor import service as pa_service

            st_local = _load_advisor_state(cfg)
            if st_local.get("first_scan_complete"):
                st.session_state[_SS_PORT_NOTE] = (
                    "Portfolio advisor is already initialized. **Deploy** only runs first-time setup. "
                    "Use the CLI for replan, run-due, weekly, etc."
                )
            else:
                pa_service.run_init(cfg, force=False)
                note = "Init complete: eToro scan and advisor schedule created."
                if full_scan:
                    pa_service.run_bootstrap(cfg, delay_seconds=45.0, max_positions=None)
                    note += " Full portfolio scan (bootstrap) finished."
                st.session_state[_SS_PORT_NOTE] = note
        except Exception as e:
            st.session_state[_SS_PORT_NOTE] = f"Deploy failed: {e}"
        try:
            from tradingagents.portfolio_advisor import service as pa_service

            st.session_state[_SS_PORT_STATUS] = pa_service.status_text(cfg)
        except Exception as ex:
            st.session_state[_SS_PORT_STATUS] = f"Status unavailable: {ex}"
        st.rerun()

    if _SS_PORT_NOTE in st.session_state:
        note = str(st.session_state[_SS_PORT_NOTE])
        if "failed" in note.lower():
            st.error(note)
        elif "skipped" in note.lower() or "already initialized" in note.lower():
            st.warning(note)
        else:
            st.success(note)

    # ── Portfolio ──────────────────────────────────────────────────────────
    st.divider()
    _render_etoro_portfolio_block(show_export=False, compact_title=True, key_prefix="dash_etoro")

    # ── Agent activity ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Agent activity")
    _render_notifications_box(cfg)

    # ── Schedule ───────────────────────────────────────────────────────────
    st.divider()
    _render_dashboard_planning_row(cfg)

    # ── System status (collapsed) ──────────────────────────────────────────
    st.divider()
    with st.expander("System status & advanced", expanded=False):
        _render_last_bootstrap_panel(cfg)
        st.divider()
        _render_pm_council_panel(cfg)

        if _etoro_env_configured():
            st.divider()
            if st.button("Refresh advisor status", key="dash_ref_status"):
                try:
                    from tradingagents.portfolio_advisor import service as pa_service

                    st.session_state[_SS_PORT_STATUS] = pa_service.status_text(cfg)
                except Exception as e:
                    st.session_state[_SS_PORT_STATUS] = f"Status unavailable: {e}"
                st.rerun()
            if _SS_PORT_STATUS not in st.session_state:
                try:
                    from tradingagents.portfolio_advisor import service as pa_service

                    st.session_state[_SS_PORT_STATUS] = pa_service.status_text(cfg)
                except Exception:
                    st.session_state[_SS_PORT_STATUS] = "(not loaded)"
            st.text_area(
                "Portfolio advisor state",
                value=str(st.session_state.get(_SS_PORT_STATUS, "(not loaded)")),
                height=180,
                key="dash_pa_status_area",
                label_visibility="collapsed",
                disabled=True,
            )
            st.divider()
            st.checkbox(
                "Full portfolio scan on deploy (high cost — runs multi-agent graph on each holding)",
                value=False,
                key="dash_full_scan",
            )
            if st.button("Export jobs & timestamps catalogue (MD + JSON)", key="dash_export_catalogue"):
                try:
                    from tradingagents.portfolio_advisor.catalogue import write_advisor_catalogue

                    paths = write_advisor_catalogue(cfg, write_json=True)
                    st.success("Wrote:\n" + "\n".join(f"- **{k}:** `{v}`" for k, v in paths.items()))
                except Exception as e:
                    st.error(str(e))
            with st.expander("Export watchlist JSON", expanded=False):
                _render_etoro_export_section(key_prefix="dash_etoro")

        st.divider()
        with st.expander("Full activity feed (verbose)", expanded=False):
            _render_activity_feed(cfg)
        _render_event_log_expander(cfg)
        st.divider()
        _render_advisor_how_it_works()


def _settings_memory_tab() -> None:
    cfg = merged_app_config()
    mem_path = Path(str(cfg.get("memory_log_path") or "")).expanduser()
    lr_path = learned_rules_path_for_config(cfg)

    st.markdown("**Paths** (from config / `.env`)")
    st.caption(f"Memory log: `{mem_path}`")
    st.caption(f"Learned rules: `{lr_path or '(disabled / unknown)'}`")

    mem_text = mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
    lr_text = ""
    if lr_path and lr_path.is_file():
        lr_text = lr_path.read_text(encoding="utf-8")

    m1 = st.text_area("Trading memory (markdown)", value=mem_text, height=220, key="mem_edit_md")
    if lr_path is not None:
        m2 = st.text_area("Learned rules (markdown)", value=lr_text, height=160, key="mem_edit_lr")
    else:
        m2 = ""
        st.info("Learned rules path is not configured (check `learned_rules_enabled` / `learned_rules_path`).")

    if st.button("Save memory files to disk", key="mem_save_files"):
        try:
            mem_path.parent.mkdir(parents=True, exist_ok=True)
            mem_path.write_text(m1, encoding="utf-8")
            if lr_path is not None:
                lr_path.parent.mkdir(parents=True, exist_ok=True)
                lr_path.write_text(m2, encoding="utf-8")
            st.success("Files written.")
        except Exception as e:
            st.error(str(e))

    st.markdown("**Context injected into agents**")
    bc = merged_app_config()
    lb = st.number_input("Lookback days (memory context)", min_value=1, max_value=3650, value=int(bc.get("memory_context_lookback_days", 90)), key="mem_ctx_lb")
    ms = st.number_input("Max same-ticker snippets", min_value=1, max_value=100, value=int(bc.get("memory_context_max_same_ticker", 8)), key="mem_ctx_ms")
    mc = st.number_input("Max cross-ticker snippets", min_value=0, max_value=50, value=int(bc.get("memory_context_max_cross_ticker", 3)), key="mem_ctx_mc")
    ed = st.number_input("Event log prompt days", min_value=1, max_value=365, value=int(bc.get("memory_event_log_prompt_days", 30)), key="mem_ctx_ed")

    if st.button("Save memory context settings", key="mem_save_ctx"):
        routing = merged_app_config().get("agent_llm_routing") or {}
        scalars = {
            "memory_context_lookback_days": int(lb),
            "memory_context_max_same_ticker": int(ms),
            "memory_context_max_cross_ticker": int(mc),
            "memory_event_log_prompt_days": int(ed),
        }
        save_runtime_overlay(build_overlay_from_scalars_and_routing(scalars, routing))
        st.success("Saved to ui_runtime_config.json")
        st.rerun()


def _settings_llm_tab() -> None:
    cfg = merged_app_config()
    st.caption(
        "Per-agent models use OpenRouter slugs (e.g. `openai/gpt-4o-mini`). "
        "Changes are written to `ui_runtime_config.json`."
    )
    st.checkbox(
        "Enable corporate hierarchy (per-agent OpenRouter)",
        value=bool(cfg.get("corporate_hierarchy_enabled", True)),
        key="set_llm_corp",
    )
    fb = st.text_input(
        "Rate-limit fallback model (OpenRouter slug)",
        value=str(cfg.get("llm_fallback_openrouter_model") or "openai/gpt-4o-mini"),
        key="set_llm_fb",
    )
    or_url = st.text_input(
        "OpenRouter base URL (optional)",
        value=str(cfg.get("corporate_openrouter_base_url") or ""),
        key="set_llm_orurl",
        help="Leave blank for default https://openrouter.ai/api/v1",
    )
    eff = effective_corporate_routing(cfg)
    for agent_key in DEFAULT_CORPORATE_AGENT_ROUTING:
        label = AGENT_SPEC_LABELS.get(agent_key, agent_key)
        st.text_input(
            label,
            value=str(eff.get(agent_key, {}).get("model", "")),
            key=f"llm_route_{agent_key}",
        )

    st.markdown("**Portfolio advisor models**")
    pm = st.text_input(
        "Planner model (blank = use quick_think_llm from env/defaults)",
        value=str(cfg.get("portfolio_advisor_planner_model") or ""),
        key="set_llm_adv_planner",
    )
    rm = st.text_input(
        "Reasoning model (post-earnings, critical digests)",
        value=str(cfg.get("portfolio_advisor_reasoning_model") or ""),
        key="set_llm_adv_reason",
    )

    if st.button("Save LLM settings", type="primary", key="set_llm_save"):
        routing_out: Dict[str, Any] = {}
        for k in DEFAULT_CORPORATE_AGENT_ROUTING:
            user_m = (st.session_state.get(f"llm_route_{k}") or "").strip()
            default_m = str(DEFAULT_CORPORATE_AGENT_ROUTING[k].get("model") or "")
            if user_m and user_m != default_m:
                routing_out[k] = {"model": user_m}
        scalars: Dict[str, Any] = {
            "corporate_hierarchy_enabled": bool(st.session_state.get("set_llm_corp")),
            "llm_fallback_openrouter_model": (st.session_state.get("set_llm_fb") or "openai/gpt-4o-mini").strip(),
            "corporate_openrouter_base_url": (st.session_state.get("set_llm_orurl") or "").strip() or None,
            "portfolio_advisor_planner_model": (st.session_state.get("set_llm_adv_planner") or "").strip() or None,
            "portfolio_advisor_reasoning_model": (st.session_state.get("set_llm_adv_reason") or "").strip() or None,
        }
        save_runtime_overlay(build_overlay_from_scalars_and_routing(scalars, routing_out))
        for k in DEFAULT_CORPORATE_AGENT_ROUTING:
            st.session_state.pop(f"llm_route_{k}", None)
        for k in ("set_llm_corp", "set_llm_fb", "set_llm_orurl", "set_llm_adv_planner", "set_llm_adv_reason"):
            st.session_state.pop(k, None)
        st.success("Saved to ui_runtime_config.json")
        st.rerun()


def _page_cost() -> None:
    from tradingagents.llm_clients.cost_report import (
        load_records,
        daily_spend,
        model_spend,
        null_cost_count,
        total_spend,
    )

    _page_header("Cost", "LLM spend from ~/.tradingagents/logs/cost.jsonl")

    records_30 = load_records(days=30)
    null_n = null_cost_count(records_30)

    if null_n:
        st.warning(
            f"{null_n} call(s) in the last 30 days have **null** cost_usd — "
            "model slug not in the pricing table in `cost_logger.py`."
        )
    else:
        st.caption("All calls in the last 30 days have known pricing.")

    if not records_30:
        st.info("No cost records found for the last 30 days. Run some analyses to populate the log.")
        return

    grand_total = total_spend(records_30)
    st.metric("Total spend (30 days)", f"${grand_total:.4f}")

    st.subheader("Daily spend (last 30 days)")
    day_totals = daily_spend(records_30)
    if day_totals:
        df_daily = pd.DataFrame(
            {"Date": list(day_totals.keys()), "Spend (USD)": list(day_totals.values())}
        ).set_index("Date")
        st.line_chart(df_daily, use_container_width=True)
    else:
        st.caption("No priced calls in this window.")

    st.subheader("Spend by model (last 30 days)")
    ms = model_spend(records_30)
    if ms:
        df_model = pd.DataFrame(ms, columns=["Model", "Calls", "Total (USD)"])
        df_bar = df_model.set_index("Model")[["Total (USD)"]]
        st.bar_chart(df_bar, use_container_width=True)
        st.dataframe(df_model, use_container_width=True, hide_index=True)

    st.subheader("Last 100 calls")
    all_records = load_records(days=None)
    recent = sorted(all_records, key=lambda r: r.get("ts") or "", reverse=True)[:100]
    if recent:
        df_calls = pd.DataFrame(
            [
                {
                    "Timestamp": str(r.get("ts", ""))[:19],
                    "Model": r.get("model", ""),
                    "Prompt tokens": r.get("prompt_tokens", 0),
                    "Completion tokens": r.get("completion_tokens", 0),
                    "Total tokens": r.get("total_tokens", 0),
                    "Cost (USD)": r.get("cost_usd"),
                }
                for r in recent
            ]
        )
        st.dataframe(df_calls, use_container_width=True, hide_index=True)
    else:
        st.caption("No records found.")


def _page_settings() -> None:
    _page_header(
        "Settings",
        "Memory files, context tuning, and LLM routing. Persisted under ~/.tradingagents/ui_runtime_config.json for this UI only.",
    )
    st.caption(
        f"Overlay file: `{runtime_config_path()}`. CLI and cron jobs still use `.env` unless you load this file yourself."
    )
    t1, t2, t3 = st.tabs(["Memory and learning", "LLMs", "Manual full analysis"])
    with t1:
        _settings_memory_tab()
    with t2:
        _settings_llm_tab()
    with t3:
        _section_full_analysis()


def _section_full_analysis() -> None:
    st.subheader("Manual full analysis")
    st.caption(
        "Run the multi-agent graph for one symbol. Uses **LLMs** tab routing unless you override JSON below. "
        "Reports go to your configured results directory."
    )
    _ensure_fa_session_seeds()
    bc = merged_app_config()
    def_env_provider = str(bc.get("llm_provider") or "openrouter")
    def_deep = str(bc.get("deep_think_llm") or "openai/gpt-4o")
    def_quick = str(bc.get("quick_think_llm") or "openai/gpt-4o-mini")
    def_backend = str(bc.get("backend_url") or "")

    left, right = st.columns([1.1, 1], gap="large")

    with left:
        st.markdown('<p class="ta-section"><strong>Symbol and date</strong></p>', unsafe_allow_html=True)
        today = date.today().isoformat()
        ticker = st.text_input(
            "Ticker",
            value="NVDA",
            help="Include exchange suffix when needed, e.g. 7203.T, VOD.L",
            key="settings_fa_ticker",
        ).strip().upper()
        trade_date = st.text_input(
            "Analysis date",
            value=today,
            help="YYYY-MM-DD. Cannot be in the future.",
            key="settings_fa_date",
        )

        st.markdown('<p class="ta-section"><strong>Analysts</strong></p>', unsafe_allow_html=True)
        ac1, ac2 = st.columns(2)
        for i, k in enumerate(ANALYST_ORDER):
            target = ac1 if i < 2 else ac2
            with target:
                st.checkbox(ANALYST_LABELS[k], value=True, key=f"cb_{k}")

        st.markdown('<p class="ta-section"><strong>Models and data</strong></p>', unsafe_allow_html=True)
        provider = st.selectbox(
            "LLM provider",
            options=PROVIDERS,
            index=PROVIDERS.index(def_env_provider) if def_env_provider in PROVIDERS else 0,
            key="settings_fa_provider",
        )
        backend_url = st.text_input(
            "API base URL (optional)",
            value=def_backend,
            help="Leave blank for the provider default.",
            key="settings_fa_backend",
        )
        deep_model = st.text_input("Deep / slow model", value=def_deep, key="settings_fa_deep")
        quick_model = st.text_input("Quick model", value=def_quick, key="settings_fa_quick")
        output_language = st.text_input(
            "Output language",
            value=str(bc.get("output_language") or "English"),
            key="settings_fa_lang",
        )
        news_vendor = st.selectbox("News data source", ["yfinance", "alpha_vantage"], index=0, key="settings_fa_news")

        with st.expander("Advanced graph options", expanded=False):
            max_debate = st.slider(
                "Research debate rounds",
                1,
                3,
                int(bc.get("max_debate_rounds", 1)),
                key="settings_fa_md",
            )
            max_risk = st.slider(
                "Risk debate rounds",
                1,
                3,
                int(bc.get("max_risk_discuss_rounds", 1)),
                key="settings_fa_mr",
            )
            checkpoint = st.checkbox("Checkpoint resume (SQLite)", value=False, key="settings_fa_ck")

        st.markdown('<p class="ta-section"><strong>Corporate hierarchy (OpenRouter)</strong></p>', unsafe_allow_html=True)
        st.caption(
            "Defaults come from the **LLMs** tab. Requires `OPENROUTER_API_KEY`. "
            "Turn off to use the legacy single-provider pair above."
        )
        corporate_hierarchy = st.checkbox(
            "Enable corporate hierarchy (per-agent OpenRouter)",
            key="cb_corporate_hierarchy",
        )
        corporate_openrouter_base_url = st.text_input(
            "OpenRouter base URL (optional)",
            help="Leave blank for https://openrouter.ai/api/v1",
            key="txt_corporate_or_url",
        )
        llm_fallback_openrouter_model = st.text_input(
            "Rate-limit fallback model (OpenRouter slug)",
            key="txt_or_fallback_model",
        )
        agent_llm_routing_json = st.text_area(
            "Optional `agent_llm_routing` JSON (overrides **LLMs** tab for this run only if valid JSON)",
            height=120,
            placeholder='{"news_analyst": {"model": "google/gemini-2.5-flash"}}',
            key="ta_agent_llm_routing_json",
        )

    with right:
        st.markdown('<p class="ta-section"><strong>Run</strong></p>', unsafe_allow_html=True)
        if news_vendor == "alpha_vantage" and not (os.environ.get("ALPHA_VANTAGE_API_KEY") or "").strip():
            st.warning("Set `ALPHA_VANTAGE_API_KEY` in `.env` for Alpha Vantage news.")

        run = st.button("Run full analysis", type="primary", use_container_width=True, key="btn_run_analysis")
        st.caption("Large models and many analysts can take several minutes.")

    if run:
        side: Dict[str, Any] = {
            "provider": provider,
            "backend_url": backend_url,
            "deep_model": deep_model,
            "quick_model": quick_model,
            "output_language": output_language,
            "max_debate": max_debate,
            "max_risk": max_risk,
            "checkpoint": checkpoint,
            "news_vendor": news_vendor,
            "corporate_hierarchy": corporate_hierarchy,
            "corporate_openrouter_base_url": corporate_openrouter_base_url,
            "llm_fallback_openrouter_model": llm_fallback_openrouter_model,
            "agent_llm_routing_json": agent_llm_routing_json,
        }
        for k in ANALYST_ORDER:
            side[f"analyst_{k}"] = st.session_state.get(f"cb_{k}", True)

        try:
            datetime.strptime(trade_date, "%Y-%m-%d")
        except ValueError:
            st.error("Use a valid date: YYYY-MM-DD.")
            return
        if datetime.strptime(trade_date, "%Y-%m-%d").date() > date.today():
            st.error("Analysis date cannot be in the future.")
            return

        selected = _selected_analysts(side)
        if not selected:
            st.error("Select at least one analyst.")
            return

        run_cfg = _build_config(side)
        progress = st.empty()
        status = st.status("Running agents…", expanded=True)

        def on_progress(merged: Dict[str, Any], _delta: Dict[str, Any]) -> None:
            parts = []
            for key, label in [
                ("market_report", "Market"),
                ("sentiment_report", "Sentiment"),
                ("news_report", "News"),
                ("fundamentals_report", "Fundamentals"),
                ("investment_plan", "Research"),
                ("trader_investment_plan", "Trader"),
                ("final_trade_decision", "Done"),
            ]:
                if merged.get(key):
                    parts.append(label)
            progress.markdown("**Progress:** " + (" → ".join(parts) if parts else "Starting…"))

        run_cfg["progress_callback"] = on_progress

        run_ok = False
        final_state: Dict[str, Any] = {}
        decision: Any = None
        with status:
            try:
                graph = TradingAgentsGraph(
                    selected_analysts=selected,
                    debug=False,
                    config=run_cfg,
                )
                final_state, decision = graph.propagate(ticker, trade_date)
                run_ok = True
            except Exception as e:
                st.error(f"Run failed: {e}")
            finally:
                run_cfg.pop("progress_callback", None)

        if run_ok:
            status.update(label="Complete", state="complete", expanded=False)
            progress.empty()
            st.session_state[_SS_ANALYSIS] = (final_state, decision)
            st.success("Finished. Reports are on disk in your results folder.")
        else:
            st.session_state.pop(_SS_ANALYSIS, None)

    if _SS_ANALYSIS in st.session_state:
        st.divider()
        st.subheader("Results")
        fs, dec = st.session_state[_SS_ANALYSIS]
        _render_results(fs, dec)


def main() -> None:
    st.set_page_config(
        page_title="TradingAgents",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_app_styles()

    page = _sidebar_shell()

    if _etoro_env_configured() and page == "Dashboard":
        _render_etoro_portfolio_block(show_export=False, compact_title=True, show_section_title=True, key_prefix="fa_etoro")
    elif not _etoro_env_configured() and page == "Dashboard":
        st.caption(
            "eToro: set **ETORO_API_KEY** and **ETORO_USER_KEY** in `.env` to show a portfolio summary above."
        )

    if page == "Dashboard":
        _page_dashboard()
    elif page == "Cost":
        _page_cost()
    else:
        _page_settings()


if __name__ == "__main__":
    main()
