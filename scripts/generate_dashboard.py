#!/usr/bin/env python3
"""Generate a static HTML performance dashboard from synced server state.

Inputs (defaults; override via env):
  COMPOUNDER_STATE_DIR    ~/local/compounder_state/
  COMPOUNDER_DASHBOARD    ~/local/compounder_dashboard.html
  COMPOUNDER_WATCHDOG_STATE  /tmp/mac-watchdog.state.json (from scripts/mac-watchdog.sh)

Outputs:
  Single HTML file at COMPOUNDER_DASHBOARD. Chart.js loaded from CDN. Inline CSS.
  No external resources beyond Chart.js. Renders correctly with missing or empty data.

Sections:
  Top    server status, last PM cycle, portfolio NAV
  A      recommendation log volume by week (bar)
  B      outcomes by classification (pie)
  C      top 5 rules by performance, bottom 5 (table)
  D      recent macro events (table)
  E      open positions with thesis status (table)

Design rules:
  Pure HTML and inline CSS. No JS frameworks. Chart.js from CDN only.
  Renders gracefully with empty or missing input files.
  Single file, no build step.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# --- Configuration -----------------------------------------------------------

HOME = Path.home()
STATE_DIR = Path(os.environ.get("COMPOUNDER_STATE_DIR", str(HOME / "local" / "compounder_state")))
DASHBOARD_PATH = Path(os.environ.get("COMPOUNDER_DASHBOARD", str(HOME / "local" / "compounder_dashboard.html")))
WATCHDOG_STATE = Path(os.environ.get("COMPOUNDER_WATCHDOG_STATE", "/tmp/mac-watchdog.state.json"))


# --- Data loaders (graceful with missing files) -----------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
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


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_watchdog_state() -> Dict[str, Any]:
    data = _read_json(WATCHDOG_STATE) or {}
    return {
        "status": data.get("status", "unknown"),
        "consecutive_failures": data.get("consecutive_failures", 0),
        "down_since": data.get("down_since"),
        "last_alert_sent": data.get("last_alert_sent"),
    }


def load_recommendations() -> List[Dict[str, Any]]:
    return _read_jsonl(STATE_DIR / "recommendation_log.jsonl")


def load_outcomes() -> List[Dict[str, Any]]:
    return _read_jsonl(STATE_DIR / "outcomes.jsonl")


def load_proposed_trades() -> List[Dict[str, Any]]:
    return _read_jsonl(STATE_DIR / "proposed_trades.jsonl")


def load_pm_council() -> List[Dict[str, Any]]:
    # Best effort: this file may have different schemas across versions.
    return _read_jsonl(STATE_DIR / "pm_council.jsonl")


def load_state() -> Dict[str, Any]:
    return _read_json(STATE_DIR / "state.json") or {}


def load_paper_portfolio() -> Dict[str, Any]:
    return _read_json(STATE_DIR / "paper_portfolio.json") or {}


# --- Aggregation -------------------------------------------------------------

def rec_log_volume_by_week(recs: List[Dict[str, Any]], weeks: int = 13) -> Tuple[List[str], List[int]]:
    """Return (labels, counts) for the last N weeks."""
    counts = Counter()
    for r in recs:
        ts_str = r.get("ts") or ""
        try:
            dt = datetime.fromisoformat(ts_str)
        except (TypeError, ValueError):
            continue
        # Week starts Monday; label by ISO week
        year, week, _ = dt.isocalendar()
        counts[(year, week)] += 1
    now = datetime.now(timezone.utc)
    labels: List[str] = []
    values: List[int] = []
    for offset in range(weeks - 1, -1, -1):
        d = now - timedelta(weeks=offset)
        y, w, _ = d.isocalendar()
        labels.append(f"{y}-W{w:02d}")
        values.append(counts.get((y, w), 0))
    return labels, values


def outcome_classification_counts(outcomes: List[Dict[str, Any]], recs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"good": 0, "bad": 0, "neutral": 0, "pending": 0}
    measured_ids = set()
    for o in outcomes:
        cls = (o.get("classification") or "").lower()
        if cls in counts:
            counts[cls] += 1
        rid = o.get("recommendation_id")
        if rid:
            measured_ids.add(rid)
    for r in recs:
        if r.get("id") not in measured_ids and r.get("was_correct") is None:
            counts["pending"] += 1
    return counts


def rule_performance(outcomes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (top_5, bottom_5) rules by mean score."""
    score_map = {"good": 1, "neutral": 0, "bad": -1}
    per_rule: Dict[str, Dict[str, Any]] = {}
    for o in outcomes:
        rule = o.get("rule_ref")
        if not rule:
            continue
        rec = per_rule.setdefault(rule, {"rule_ref": rule, "uses": 0, "good": 0, "bad": 0, "neutral": 0, "sum_score": 0, "sum_return": 0.0})
        rec["uses"] += 1
        cls = (o.get("classification") or "neutral").lower()
        rec[cls] = rec.get(cls, 0) + 1
        rec["sum_score"] += score_map.get(cls, 0)
        try:
            rec["sum_return"] += float(o.get("realised_return") or 0)
        except (TypeError, ValueError):
            pass
    rows = []
    for r in per_rule.values():
        u = r["uses"]
        r["mean_score"] = round(r["sum_score"] / u, 3) if u else 0
        r["mean_return_pct"] = round((r["sum_return"] / u) * 100, 2) if u else 0
        r["pct_good"] = round(r["good"] / u * 100, 0) if u else 0
        r["pct_bad"] = round(r["bad"] / u * 100, 0) if u else 0
        rows.append(r)
    rows.sort(key=lambda r: (r["mean_score"], r["uses"]), reverse=True)
    top = rows[:5]
    bottom = list(reversed(rows[-5:])) if len(rows) > 5 else []
    return top, bottom


def recent_macro_events(state: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    # The PM event log lives in different places depending on version. Best
    # effort: read pm_market_events.jsonl if it exists.
    events_file = STATE_DIR / "pm_market_events.jsonl"
    events = _read_jsonl(events_file)
    if not events:
        # Fall back to state.json macro_events if present
        events = state.get("macro_events") or []
    events.sort(key=lambda e: e.get("ts") or e.get("date") or "", reverse=True)
    return events[:limit]


def open_positions(paper_portfolio: Dict[str, Any]) -> List[Dict[str, Any]]:
    positions = paper_portfolio.get("positions") or []
    return positions if isinstance(positions, list) else []


def latest_pm_cycle_ts(recs: List[Dict[str, Any]], council: List[Dict[str, Any]]) -> Optional[str]:
    candidates = []
    if recs:
        candidates.append(max((r.get("ts") or "") for r in recs))
    if council:
        candidates.append(max((c.get("ts") or "") for c in council))
    if not candidates:
        return None
    return max(candidates) or None


def portfolio_nav(paper_portfolio: Dict[str, Any]) -> Optional[float]:
    nav = paper_portfolio.get("nav")
    if isinstance(nav, (int, float)):
        return float(nav)
    cash = paper_portfolio.get("cash") or 0
    positions = open_positions(paper_portfolio)
    pos_value = 0.0
    for p in positions:
        try:
            pos_value += float(p.get("market_value") or p.get("value") or 0)
        except (TypeError, ValueError):
            continue
    total = float(cash) + pos_value
    return total if total > 0 else None


# --- HTML rendering ----------------------------------------------------------

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Compounder Dashboard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  h2 {{ font-size: 16px; margin: 24px 0 12px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 6px; padding: 16px; }}
  .stat {{ display: flex; justify-content: space-between; align-items: center; font-size: 14px; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }}
  .stat:last-child {{ border-bottom: none; }}
  .stat .label {{ color: #666; }}
  .stat .value {{ font-weight: 600; }}
  .status-up {{ color: #2a9d2a; }}
  .status-down {{ color: #d12d2d; }}
  .status-unknown {{ color: #999; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #f0f0f0; }}
  th {{ color: #666; font-weight: 500; }}
  .empty {{ color: #aaa; font-style: italic; padding: 12px 0; }}
  canvas {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<h1>Compounder Dashboard</h1>
<div class="meta">Generated {generated_at}</div>

<div class="grid">
  <div class="card">
    <h2>Server status</h2>
    <div class="stat"><span class="label">Reachability</span><span class="value status-{status_class}">{status_label}</span></div>
    <div class="stat"><span class="label">Down since</span><span class="value">{down_since}</span></div>
    <div class="stat"><span class="label">Last PM cycle</span><span class="value">{last_pm_cycle}</span></div>
    <div class="stat"><span class="label">Paper portfolio NAV</span><span class="value">{nav}</span></div>
  </div>

  <div class="card">
    <h2>Recommendation log volume (13w)</h2>
    {rec_log_chart}
  </div>

  <div class="card">
    <h2>Outcomes</h2>
    {outcomes_chart}
  </div>

  <div class="card">
    <h2>Top 5 rules</h2>
    {top_rules_table}
  </div>

  <div class="card">
    <h2>Bottom 5 rules</h2>
    {bottom_rules_table}
  </div>

  <div class="card">
    <h2>Recent macro events</h2>
    {macro_events_table}
  </div>

  <div class="card">
    <h2>Open positions</h2>
    {positions_table}
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
{js_block}
</script>
</body>
</html>
"""


def _empty_div(message: str) -> str:
    return f'<div class="empty">{message}</div>'


def _format_table(rows: List[List[str]], headers: List[str], empty_msg: str) -> str:
    if not rows:
        return _empty_div(empty_msg)
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table>{head}{body}</table>"


def _bar_chart_block(canvas_id: str, labels: List[str], values: List[int]) -> Tuple[str, str]:
    if not any(values):
        return _empty_div("no data yet"), ""
    html = f'<canvas id="{canvas_id}"></canvas>'
    js = f"""
new Chart(document.getElementById('{canvas_id}'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(labels)},
    datasets: [{{ label: 'count', data: {json.dumps(values)}, backgroundColor: '#4a90e2' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
}});
"""
    return html, js


def _pie_chart_block(canvas_id: str, counts: Dict[str, int]) -> Tuple[str, str]:
    if sum(counts.values()) == 0:
        return _empty_div("no outcomes yet"), ""
    labels = list(counts.keys())
    values = list(counts.values())
    colors = {"good": "#2a9d2a", "bad": "#d12d2d", "neutral": "#888", "pending": "#cccccc"}
    bg = [colors.get(l, "#aaa") for l in labels]
    html = f'<canvas id="{canvas_id}"></canvas>'
    js = f"""
new Chart(document.getElementById('{canvas_id}'), {{
  type: 'pie',
  data: {{
    labels: {json.dumps(labels)},
    datasets: [{{ data: {json.dumps(values)}, backgroundColor: {json.dumps(bg)} }}]
  }},
  options: {{ responsive: true }}
}});
"""
    return html, js


def _format_rule_table(rules: List[Dict[str, Any]]) -> str:
    if not rules:
        return _empty_div("not enough outcomes to rank rules yet")
    rows = []
    for r in rules:
        rows.append([
            r["rule_ref"],
            str(r["uses"]),
            f"{r['pct_good']}%",
            f"{r['pct_bad']}%",
            f"{r['mean_return_pct']:+.2f}%",
        ])
    return _format_table(rows, ["Rule", "Uses", "% good", "% bad", "Avg return"], "no rule data")


def _format_macro_events(events: List[Dict[str, Any]]) -> str:
    rows = []
    for e in events:
        when = e.get("ts") or e.get("date") or "?"
        kind = e.get("kind") or e.get("event_type") or e.get("type") or "?"
        ticker = e.get("ticker") or "-"
        summary = (e.get("summary") or e.get("note") or "")[:80]
        rows.append([when[:19], kind, ticker, summary])
    return _format_table(rows, ["When", "Kind", "Ticker", "Summary"], "no macro events recorded")


def _format_positions(positions: List[Dict[str, Any]]) -> str:
    rows = []
    for p in positions:
        rows.append([
            p.get("ticker", "?"),
            f"{float(p.get('shares', 0)):.2f}" if p.get("shares") is not None else "?",
            f"${float(p.get('entry_price', 0)):.2f}" if p.get("entry_price") is not None else "?",
            f"${float(p.get('current_price', 0)):.2f}" if p.get("current_price") is not None else "?",
            p.get("thesis_status", "-"),
        ])
    return _format_table(rows, ["Ticker", "Shares", "Entry", "Current", "Thesis"], "no open positions")


def render() -> str:
    watchdog = load_watchdog_state()
    recs = load_recommendations()
    outcomes = load_outcomes()
    council = load_pm_council()
    state = load_state()
    paper = load_paper_portfolio()

    # If no recommendation_log.jsonl yet, fall back to proposed_trades.jsonl as a proxy
    if not recs:
        recs = load_proposed_trades()

    status = watchdog.get("status", "unknown")
    status_label = {
        "up": "UP",
        "down": "DOWN",
        "down_alerted": "DOWN (alerted)",
        "unknown": "UNKNOWN",
    }.get(status, status.upper())
    status_class = "up" if status == "up" else ("down" if status in ("down", "down_alerted") else "unknown")

    last_pm = latest_pm_cycle_ts(recs, council) or "n/a"
    nav = portfolio_nav(paper)
    nav_str = f"${nav:,.2f}" if nav is not None else "n/a"

    labels, values = rec_log_volume_by_week(recs)
    outcome_counts = outcome_classification_counts(outcomes, recs)
    top_rules, bottom_rules = rule_performance(outcomes)
    macro = recent_macro_events(state)
    positions = open_positions(paper)

    rec_chart_html, rec_chart_js = _bar_chart_block("recVolumeChart", labels, values)
    outcome_chart_html, outcome_chart_js = _pie_chart_block("outcomesChart", outcome_counts)

    js_block = "\n".join(filter(None, [rec_chart_js, outcome_chart_js]))

    return _TEMPLATE.format(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        status_label=status_label,
        status_class=status_class,
        down_since=watchdog.get("down_since") or "-",
        last_pm_cycle=last_pm[:19] if last_pm else "n/a",
        nav=nav_str,
        rec_log_chart=rec_chart_html,
        outcomes_chart=outcome_chart_html,
        top_rules_table=_format_rule_table(top_rules),
        bottom_rules_table=_format_rule_table(bottom_rules) if bottom_rules else _empty_div("not enough outcomes yet"),
        macro_events_table=_format_macro_events(macro),
        positions_table=_format_positions(positions),
        js_block=js_block,
    )


def main() -> None:
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    html = render()
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    print(f"wrote {DASHBOARD_PATH}")


if __name__ == "__main__":
    main()
