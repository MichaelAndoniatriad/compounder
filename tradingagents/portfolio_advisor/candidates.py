"""Candidate discovery gates for new portfolio ideas.

The PM council should not improvise new ideas into the portfolio workflow.
Candidates first become structured records, pass explicit gates, and only then
graduate to light/deep research.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.event_log import append_event
from tradingagents.portfolio_advisor import state

CandidateStatus = Literal["candidate", "watch", "research_queued", "rejected", "promoted"]


@dataclass
class CandidateRecord:
    ticker: str
    source: str = "monthly_lookout"
    reason: str = ""
    strategy: str = "core"
    evidence_refs: List[str] = field(default_factory=list)
    status: CandidateStatus = "candidate"
    priority: int = 3
    gates: Dict[str, str] = field(default_factory=dict)
    gate_failures: List[str] = field(default_factory=list)
    next_action: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _bool_gate(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "pass", "passed", "ok"}:
        return True
    if text in {"false", "no", "n", "0", "fail", "failed"}:
        return False
    return None


def _priority(raw: Any) -> int:
    try:
        return max(1, min(int(raw), 5))
    except (TypeError, ValueError):
        return 3


def normalize_candidate(raw: Any, *, default_source: str = "monthly_lookout", theme: str = "") -> Dict[str, Any]:
    if isinstance(raw, str):
        return {
            "ticker": raw.strip().upper(),
            "source": default_source,
            "reason": theme.strip(),
            "priority": 3,
        }
    if not isinstance(raw, dict):
        raise ValueError("candidate must be a ticker string or object")
    out = dict(raw)
    out["ticker"] = str(out.get("ticker") or out.get("symbol") or "").strip().upper()
    out["source"] = str(out.get("source") or default_source).strip()
    if not out.get("reason") and theme:
        out["reason"] = theme
    strategy = str(out.get("strategy") or "core").strip().lower()
    out["strategy"] = strategy if strategy in ("core", "catalyst") else "core"
    out["priority"] = _priority(out.get("priority"))
    refs = out.get("evidence_refs") or []
    out["evidence_refs"] = [str(r).strip() for r in refs if str(r).strip()] if isinstance(refs, list) else []
    return out


def evaluate_candidate(
    raw: Any,
    *,
    live_tickers: Optional[Iterable[str]] = None,
    default_source: str = "monthly_lookout",
    theme: str = "",
    min_avg_daily_volume: int = 250_000,
) -> CandidateRecord:
    data = normalize_candidate(raw, default_source=default_source, theme=theme)
    ticker = data["ticker"]
    if not ticker:
        raise ValueError("candidate ticker is required")
    live = {str(t).strip().upper() for t in (live_tickers or []) if str(t).strip()}

    gates: Dict[str, str] = {}
    failures: List[str] = []

    if ticker in live:
        gates["portfolio_fit"] = "fail"
        failures.append("already_in_portfolio")
    else:
        portfolio_fit = _bool_gate(data.get("portfolio_fit_ok"))
        gates["portfolio_fit"] = "pass" if portfolio_fit is not False else "fail"
        if portfolio_fit is False:
            failures.append("portfolio_fit")

    policy_ok = _bool_gate(data.get("policy_ok"))
    gates["policy"] = "unknown" if policy_ok is None else ("pass" if policy_ok else "fail")
    if policy_ok is False:
        failures.append("policy")

    liquidity_ok = _bool_gate(data.get("liquidity_ok"))
    avg_volume = data.get("avg_daily_volume")
    if liquidity_ok is None and avg_volume is not None:
        try:
            liquidity_ok = float(avg_volume) >= float(min_avg_daily_volume)
        except (TypeError, ValueError):
            liquidity_ok = None
    gates["liquidity"] = "unknown" if liquidity_ok is None else ("pass" if liquidity_ok else "fail")
    if liquidity_ok is False:
        failures.append("liquidity")

    thesis_text = str(data.get("thesis") or data.get("reason") or "").strip()
    thesis_ok = _bool_gate(data.get("thesis_ok"))
    if thesis_ok is None:
        thesis_ok = len(thesis_text) >= 12
    gates["thesis"] = "pass" if thesis_ok else "unknown"
    if not thesis_ok:
        failures.append("missing_thesis")

    strategy = str(data.get("strategy") or "core").strip().lower()
    if strategy not in ("core", "catalyst"):
        strategy = "core"

    catalyst_ok = _bool_gate(data.get("catalyst_ok"))
    catalyst_text = str(data.get("catalyst") or data.get("catalyst_date") or "").strip()
    if catalyst_ok is None:
        catalyst_ok = bool(catalyst_text)
    # A catalyst-sleeve candidate with no catalyst makes no sense — it is a hard failure.
    if strategy == "catalyst":
        gates["catalyst"] = "pass" if catalyst_ok else "fail"
        if not catalyst_ok:
            failures.append("missing_catalyst")
    else:
        gates["catalyst"] = "pass" if catalyst_ok else "unknown"

    full_graph_rating = str(data.get("full_graph_rating") or "").strip()
    priority = _priority(data.get("priority"))
    status: CandidateStatus
    if any(f in failures for f in ("already_in_portfolio", "policy", "liquidity", "missing_catalyst")):
        status = "rejected"
        next_action = "Do not research until failed gates are resolved."
    elif full_graph_rating in {"Buy", "Overweight"} and priority <= 2:
        status = "promoted"
        next_action = "Ready for PM comparison against current holdings."
    elif gates["thesis"] == "pass" and gates["portfolio_fit"] == "pass" and priority <= 3:
        status = "research_queued"
        next_action = "Queue light thesis_check before any full deep run."
    else:
        status = "watch"
        next_action = "Keep on watchlist until thesis, catalyst, or priority improves."

    return CandidateRecord(
        ticker=ticker,
        source=str(data.get("source") or default_source),
        reason=thesis_text,
        strategy=strategy,
        evidence_refs=list(data.get("evidence_refs") or []),
        status=status,
        priority=priority,
        gates=gates,
        gate_failures=failures,
        next_action=next_action,
    )


def evaluate_candidates(
    raw_candidates: Iterable[Any],
    *,
    live_tickers: Optional[Iterable[str]] = None,
    default_source: str = "monthly_lookout",
    theme: str = "",
) -> List[CandidateRecord]:
    return [
        evaluate_candidate(c, live_tickers=live_tickers, default_source=default_source, theme=theme)
        for c in raw_candidates
    ]


def _candidate_with_latest_full_graph_evidence(cfg: Dict[str, Any], raw: Any, *, theme: str = "") -> Any:
    data = normalize_candidate(raw, theme=theme)
    ticker = str(data.get("ticker") or "").strip().upper()
    if not ticker:
        return data
    try:
        from tradingagents.portfolio_advisor.evidence import collect_pm_evidence

        ctx = collect_pm_evidence(cfg, [ticker], pending_jobs=[])
        latest = (ctx.get("latest_full_graph_decisions") or {}).get(ticker) or {}
    except Exception:
        latest = {}
    decision = str(latest.get("decision") or "").split("/", 1)[0].strip()
    if decision:
        data["full_graph_rating"] = decision
    ref = str(latest.get("id") or "").strip()
    if ref:
        refs = list(data.get("evidence_refs") or [])
        if ref not in refs:
            refs.append(ref)
        data["evidence_refs"] = refs
    summary = str(latest.get("summary") or "").strip()
    if summary and not data.get("reason"):
        data["reason"] = summary
    return data


def _candidate_with_market_data(cfg: Dict[str, Any], raw: Any) -> Any:
    data = dict(raw) if isinstance(raw, dict) else normalize_candidate(raw)
    if not bool(cfg.get("portfolio_advisor_candidate_market_data_enabled", True)):
        return data
    if data.get("liquidity_ok") is not None or data.get("avg_daily_volume") is not None:
        return data
    ticker = str(data.get("ticker") or "").strip().upper()
    if not ticker:
        return data
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="30d")
        if hist is None or hist.empty or "Volume" not in hist:
            return data
        avg_volume = float(hist["Volume"].tail(20).mean())
        data["avg_daily_volume"] = avg_volume
        if "Close" in hist and not hist["Close"].dropna().empty:
            data["last_price"] = float(hist["Close"].dropna().iloc[-1])
    except Exception:
        return data
    return data


def evaluate_candidates_with_evidence(
    cfg: Dict[str, Any],
    raw_candidates: Iterable[Any],
    *,
    live_tickers: Optional[Iterable[str]] = None,
    default_source: str = "monthly_lookout",
    theme: str = "",
) -> List[CandidateRecord]:
    """Evaluate candidates after enriching them from existing full-graph evidence."""
    enriched = []
    for c in raw_candidates:
        with_evidence = _candidate_with_latest_full_graph_evidence(cfg, c, theme=theme)
        enriched.append(_candidate_with_market_data(cfg, with_evidence))
    return evaluate_candidates(
        enriched,
        live_tickers=live_tickers,
        default_source=default_source,
        theme=theme,
    )


def candidate_log_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "candidates.jsonl"


def candidate_state_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "candidates_state.json"


def load_candidate_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    path = candidate_state_path(cfg)
    if not path.is_file():
        return {"version": 1, "candidates": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "candidates": {}}
    if not isinstance(data, dict):
        return {"version": 1, "candidates": {}}
    candidates = data.get("candidates")
    if not isinstance(candidates, dict):
        data["candidates"] = {}
    data.setdefault("version", 1)
    return data


def save_candidate_state(cfg: Dict[str, Any], data: Dict[str, Any]) -> None:
    path = candidate_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def update_candidate_state(cfg: Dict[str, Any], records: Iterable[CandidateRecord]) -> None:
    data = load_candidate_state(cfg)
    candidates = data.setdefault("candidates", {})
    for r in records:
        candidates[r.ticker] = r.to_dict()
    save_candidate_state(cfg, data)


def _candidate_events_enabled(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("event_log_path") or cfg.get("memory_log_path"))


def append_candidate_events(cfg: Dict[str, Any], records: Iterable[CandidateRecord]) -> None:
    if not _candidate_events_enabled(cfg):
        return
    for r in records:
        try:
            append_event(
                cfg,
                {
                    "ticker": r.ticker,
                    "event_type": "candidate_status_changed",
                    "key_data": {
                        "status": r.status,
                        "source": r.source,
                        "priority": r.priority,
                        "gates": dict(r.gates),
                        "gate_failures": list(r.gate_failures),
                        "next_action": r.next_action,
                        "reason": r.reason[:500],
                        "evidence_refs": list(r.evidence_refs),
                    },
                    "outcome": None,
                },
            )
        except Exception:
            continue


def shadow_book_path(cfg: Dict[str, Any]) -> Path:
    return state.advisor_dir(cfg) / "shadow_book.jsonl"


def _shadow_state(path: Path) -> tuple:
    """Replay the append-only ledger into (open_positions, closes).

    A ticker is open iff its latest event is an 'open' row — a later 'close'
    row closes it (rows are appended in time order). Returns
    ({ticker: open_row}, [close_rows]).
    """
    opens: Dict[str, Dict[str, Any]] = {}
    closes: List[Dict[str, Any]] = []
    if not path.is_file():
        return opens, closes
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        tk = str(row.get("ticker") or "").upper()
        if not tk:
            continue
        if row.get("side") == "open":
            opens[tk] = row
        elif row.get("side") == "close":
            opens.pop(tk, None)
            closes.append(row)
    return opens, closes


def _mirror_gate_passers_to_shadow_book(cfg: Dict[str, Any], rows: List[CandidateRecord]) -> None:
    """§6.2 Compounder 2.0 shadow book: open a small paper position for every candidate
    that cleared the hard gates (status != 'rejected'). Equal-notional sizing so no
    single name dominates. Records entry price via yfinance and logs to shadow_book.jsonl.

    The shadow book answers "does the pipeline find alpha?" independently of the PM's
    final picks. Positions are closed when the candidate is rejected or promoted (see
    close_shadow_position). Outcomes feed the alpha-relative outcome tracker.

    Config: portfolio_advisor_shadow_notional (default $500 per position).
    Disabled via portfolio_advisor_shadow_book=False.
    """
    if not cfg.get("portfolio_advisor_shadow_book", True):
        return

    notional = float(cfg.get("portfolio_advisor_shadow_notional", 500) or 500)
    pass_rows = [r for r in rows if r.status != "rejected"]
    if not pass_rows:
        return

    # Load existing shadow positions to avoid duplicates
    path = shadow_book_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    open_positions, _ = _shadow_state(path)
    open_tickers = set(open_positions)

    now_iso = datetime.now(timezone.utc).isoformat()
    new_entries = []
    for r in pass_rows:
        if r.ticker in open_tickers:
            continue
        try:
            import yfinance as yf  # type: ignore
            hist = yf.Ticker(r.ticker).history(period="5d")
            if hist is None or len(hist) == 0:
                continue
            entry_price = float(hist["Close"].iloc[-1])
        except Exception:
            continue
        shares = round(notional / entry_price, 6) if entry_price > 0 else 0
        if shares <= 0:
            continue
        entry = {
            "ts": now_iso,
            "ticker": r.ticker,
            "side": "open",
            "entry_price": round(entry_price, 4),
            "shares": shares,
            "notional": notional,
            "status": r.status,
            "strategy": r.strategy or "unknown",
            "source": r.source,
            "reason": (r.reason or "")[:200],
        }
        new_entries.append(entry)

    if new_entries:
        with open(path, "a", encoding="utf-8") as f:
            for e in new_entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        try:
            from tradingagents.portfolio_advisor import messaging
            names = ", ".join(e["ticker"] for e in new_entries)
            messaging.send_advisor_message(
                cfg,
                f"[SHADOW] Opened {len(new_entries)} positions",
                f"Shadow book: opened {len(new_entries)} gate-passer positions at ~${notional:.0f} each: {names}",
                urgent=False,
            )
        except Exception:
            pass


def close_shadow_position(cfg: Dict[str, Any], ticker: str, reason: str = "") -> Optional[Dict[str, Any]]:
    """Close an open shadow position, record the alpha-relative return.

    Called when a candidate is rejected (close short) or after the hold horizon.
    Returns the outcome dict or None if no open position found.
    """
    path = shadow_book_path(cfg)
    tk = ticker.strip().upper()
    open_positions, _ = _shadow_state(path)
    open_pos = open_positions.get(tk)
    if open_pos is None:
        return None

    try:
        import yfinance as yf  # type: ignore
        hist = yf.Ticker(tk).history(period="5d")
        if hist is None or len(hist) == 0:
            return None
        exit_price = float(hist["Close"].iloc[-1])
    except Exception:
        return None

    entry_price = float(open_pos.get("entry_price") or 0)
    if entry_price <= 0:
        return None

    raw_return = (exit_price - entry_price) / entry_price

    # QQQ alpha
    qqq_return = None
    try:
        from tradingagents.portfolio_advisor.outcome_tracker import _fetch_qqq_return
        entry_ts = str(open_pos.get("ts") or "")[:10]
        now_date = datetime.now(timezone.utc).date().isoformat()
        qqq_return = _fetch_qqq_return(entry_ts, now_date)
    except Exception:
        pass

    alpha = (raw_return - qqq_return) if qqq_return is not None else None
    now_iso = datetime.now(timezone.utc).isoformat()
    outcome: Dict[str, Any] = {
        "ts": now_iso,
        "ticker": tk,
        "side": "close",
        "closed_at": now_iso,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "raw_return": round(raw_return, 4),
        "qqq_return": round(qqq_return, 4) if qqq_return is not None else None,
        "alpha_vs_qqq": round(alpha, 4) if alpha is not None else None,
        "reason": (reason or "")[:200],
        "source": open_pos.get("source"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
    return outcome


def close_due_shadow_positions(cfg: Dict[str, Any], max_hold_days: Optional[int] = None) -> int:
    """Time-stop: close every open shadow position past its hold horizon.

    The shadow book measures the PIPELINE at a fixed horizon (default 30d,
    config portfolio_advisor_shadow_hold_days) — without this, positions never
    resolve and the book produces zero outcomes. Run from the weekly check.
    Returns the number of positions closed.
    """
    days = int(max_hold_days or cfg.get("portfolio_advisor_shadow_hold_days", 30) or 30)
    open_positions, _ = _shadow_state(shadow_book_path(cfg))
    if not open_positions:
        return 0
    now = datetime.now(timezone.utc)
    n = 0
    for tk, row in open_positions.items():
        try:
            opened = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if (now - opened).days >= days:
            if close_shadow_position(cfg, tk, reason=f"time-stop {days}d") is not None:
                n += 1
    return n


def shadow_book_summary(cfg: Dict[str, Any]) -> str:
    """Compact text block for the weekly digest: shadow book P&L vs QQQ."""
    path = shadow_book_path(cfg)
    if not path.is_file():
        return ""
    opens, closes = _shadow_state(path)

    lines = ["--- Shadow book (pipeline gate-passers, not PM picks) ---"]
    if closes:
        alphas = [float(c["alpha_vs_qqq"]) for c in closes if c.get("alpha_vs_qqq") is not None]
        hit_rate = sum(1 for a in alphas if a > 0) / len(alphas) if alphas else 0
        mean_alpha = sum(alphas) / len(alphas) if alphas else 0
        lines.append(f"Closed: {len(closes)} | alpha-positive: {hit_rate:.0%} | mean alpha: {mean_alpha:+.2%}")
    else:
        lines.append("No closed shadow positions yet.")
    if opens:
        lines.append(f"Open: {len(opens)} ({', '.join(sorted(opens))})")
    return "\n".join(lines)


def append_candidate_records(cfg: Dict[str, Any], records: Iterable[CandidateRecord]) -> None:
    rows = list(records)
    path = candidate_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    update_candidate_state(cfg, rows)
    append_candidate_events(cfg, rows)
    # §6.2 Compounder 2.0: shadow book — every candidate that clears the hard
    # gates gets a small equal-notional paper position. This generates 5-10x
    # more resolved outcomes per month than the advisor book alone, letting the
    # learning loop converge faster. The shadow book is internal (paper_portfolio.py)
    # rather than Alpaca, so it never mingles with the advisor track record.
    _mirror_gate_passers_to_shadow_book(cfg, rows)


def queue_candidate_research_jobs(cfg: Dict[str, Any], records: Iterable[CandidateRecord]) -> int:
    """Append pending light research jobs for gated candidates."""
    st = state.load_state(cfg)
    existing = state.list_pending_jobs(st)
    now = datetime.now(timezone.utc)
    new_rows: List[Dict[str, Any]] = []
    for r in records:
        if r.status != "research_queued":
            continue
        duplicate = any(
            str(j.get("ticker") or "").strip().upper() == r.ticker
            and str(j.get("job_type") or "") == "thesis_check"
            and str(j.get("source") or "") == "candidate_gate"
            for j in existing + new_rows
        )
        if duplicate:
            continue
        new_rows.append(
            {
                "id": f"cand_{r.ticker}_{now.strftime('%Y%m%d%H%M%S')}",
                "ticker": r.ticker,
                "scheduled_at": now.isoformat(),
                "kind": "deep_research",
                "reason": r.reason or "Candidate gate passed; run light thesis_check.",
                "status": "pending",
                "created_at": now.isoformat(),
                "execution_tier": "single_model",
                "job_type": "thesis_check",
                "source": "candidate_gate",
                "evidence_question": f"Does {r.ticker} deserve promotion to deep research or PM comparison?",
                "supersedes_job_id": "",
                "flags": ["CANDIDATE_GATE"],
            }
        )
    if not new_rows:
        return 0
    state.append_jobs(st, new_rows)
    state.save_state(cfg, st)
    return len(new_rows)


def queue_deep_candidate_job(
    cfg: Dict[str, Any],
    ticker: str,
    reason: str = "",
    *,
    strategy: str = "core",
) -> bool:
    """Queue ONE full_graph candidate_promotion job (deduped against pending). Returns True if queued.

    On completion, run-due calls handle_candidate_full_graph_result, which runs the PM
    comparison when the deep decision is Buy/Overweight — i.e. the system 'decides' on the name.
    """
    now = datetime.now(timezone.utc)
    tid = str(ticker or "").strip().upper()
    if not tid:
        return False
    if weekly_full_graph_budget_left(cfg) <= 0:
        return False
    strategy = str(strategy or "core").strip().lower()
    if strategy not in ("core", "catalyst"):
        strategy = "core"
    job = {
        "id": f"canddeep_{tid}_{now.strftime('%Y%m%d%H%M%S')}",
        "ticker": tid,
        "scheduled_at": now.isoformat(),
        "kind": "deep_research",
        "reason": (reason or f"Watchlist deep dive on {tid}.")[:500],
        "status": "pending",
        "created_at": now.isoformat(),
        "execution_tier": "full_graph",
        "job_type": "thesis_check",
        "source": "candidate_promotion",
        "evidence_question": f"Does full_graph research support adding {tid} to the portfolio?",
        "supersedes_job_id": "",
        "strategy": strategy,
        "flags": ["CANDIDATE_PROMOTION"],
    }
    return _append_candidate_job(cfg, job)


def is_candidate_job(job: Dict[str, Any]) -> bool:
    source = str(job.get("source") or "").strip()
    flags = {str(f) for f in (job.get("flags") or [])}
    # pm_tool_call = PM actively queued research on a watchlist name via tool;
    # treat as a candidate job so run_due doesn't cancel it for not being a holding.
    return source in {"candidate_gate", "candidate_promotion", "pm_tool_call"} or bool(
        flags & {"CANDIDATE_GATE", "CANDIDATE_PROMOTION"}
    )


def parse_candidate_thesis_verdict(text: str) -> str:
    """Return INTACT / WEAKENING / BROKEN / UNKNOWN from a single-model memo."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    for i, line in enumerate(lines):
        if line.upper() == "VERDICT":
            for nxt in lines[i + 1 : i + 4]:
                upper = nxt.upper()
                for verdict in ("INTACT", "WEAKENING", "BROKEN"):
                    if re.search(rf"\b{verdict}\b", upper):
                        return verdict
    head = "\n".join(lines[:12]).upper()
    for verdict in ("INTACT", "WEAKENING", "BROKEN"):
        if re.search(rf"\b{verdict}\b", head):
            return verdict
    return "UNKNOWN"


def _append_candidate_job(cfg: Dict[str, Any], job: Dict[str, Any]) -> bool:
    st = state.load_state(cfg)
    pending = state.list_pending_jobs(st)
    tid = str(job.get("ticker") or "").strip().upper()
    source = str(job.get("source") or "")
    tier = str(job.get("execution_tier") or "")
    duplicate = any(
        str(j.get("ticker") or "").strip().upper() == tid
        and str(j.get("source") or "") == source
        and str(j.get("execution_tier") or "") == tier
        and str(j.get("status") or "") == "pending"
        for j in pending
    )
    if duplicate:
        return False
    state.append_jobs(st, [job])
    state.save_state(cfg, st)
    return True


def _weekly_full_graph_cap(cfg: Dict[str, Any]) -> int:
    try:
        return int(cfg.get("portfolio_advisor_weekly_full_graph_cap", 4))
    except (TypeError, ValueError):
        return 4


def weekly_full_graph_budget_left(cfg: Dict[str, Any]) -> int:
    """How many candidate full_graph deep runs remain in this rolling 7-day window.

    Counts candidate-driven full_graph jobs (source candidate_promotion) created in the last
    7 days regardless of status — pending, completed, or failed all consume the budget.
    """
    from datetime import timedelta

    cap = _weekly_full_graph_cap(cfg)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    st = state.load_state(cfg)
    used = 0
    for j in st.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        if str(j.get("execution_tier") or "") != "full_graph":
            continue
        if str(j.get("source") or "") != "candidate_promotion":
            continue
        if str(j.get("created_at") or "") >= cutoff:
            used += 1
    return max(0, cap - used)


def handle_candidate_light_research_result(
    cfg: Dict[str, Any],
    job: Dict[str, Any],
    analysis_text: str,
) -> Dict[str, Any]:
    """Transition a candidate after the cheap thesis_check pass."""
    if str(job.get("source") or "") != "candidate_gate":
        return {"handled": False}
    if str(job.get("job_type") or "") != "thesis_check":
        return {"handled": False}
    tid = str(job.get("ticker") or "").strip().upper()
    verdict = parse_candidate_thesis_verdict(analysis_text)
    now = datetime.now(timezone.utc)

    if verdict == "INTACT":
        # Screen passed. Promote to full_graph only if the weekly deep-run budget allows;
        # otherwise hold the name as research_queued for a later week.
        budget_left = weekly_full_graph_budget_left(cfg)
        if budget_left <= 0:
            rec = CandidateRecord(
                ticker=tid,
                source="candidate_light_research",
                reason=str(job.get("reason") or ""),
                status="research_queued",
                priority=2,
                gates={"light_thesis": "pass"},
                next_action="Screen passed but weekly full_graph budget exhausted; holding for next week.",
            )
            append_candidate_records(cfg, [rec])
            return {"handled": True, "verdict": verdict, "status": rec.status, "full_graph_queued": False, "deferred_cap": True}
        rec = CandidateRecord(
            ticker=tid,
            source="candidate_light_research",
            reason=str(job.get("reason") or ""),
            status="research_queued",
            priority=2,
            gates={"light_thesis": "pass"},
            next_action="Light thesis check was INTACT; full_graph candidate research queued.",
        )
        append_candidate_records(cfg, [rec])
        queued = _append_candidate_job(
            cfg,
            {
                "id": f"canddeep_{tid}_{now.strftime('%Y%m%d%H%M%S')}",
                "ticker": tid,
                "scheduled_at": now.isoformat(),
                "kind": "deep_research",
                "reason": f"Candidate light thesis check was INTACT: {str(job.get('reason') or '')[:300]}",
                "status": "pending",
                "created_at": now.isoformat(),
                "execution_tier": "full_graph",
                "job_type": "thesis_check",
                "source": "candidate_promotion",
                "evidence_question": f"Does full_graph research support promoting {tid} for PM comparison?",
                "supersedes_job_id": str(job.get("id") or ""),
                "flags": ["CANDIDATE_PROMOTION"],
            },
        )
        return {"handled": True, "verdict": verdict, "status": rec.status, "full_graph_queued": queued}

    status: CandidateStatus = "watch" if verdict in {"WEAKENING", "UNKNOWN"} else "rejected"
    rec = CandidateRecord(
        ticker=tid,
        source="candidate_light_research",
        reason=str(job.get("reason") or ""),
        status=status,
        priority=4,
        gates={"light_thesis": "unknown" if verdict == "UNKNOWN" else "fail"},
        gate_failures=[] if status == "watch" else ["light_thesis_broken"],
        next_action=(
            "Watch only; light thesis check was inconclusive or weakening."
            if status == "watch"
            else "Reject; light thesis check was BROKEN."
        ),
    )
    append_candidate_records(cfg, [rec])
    return {"handled": True, "verdict": verdict, "status": status, "full_graph_queued": False}


def handle_candidate_full_graph_result(
    cfg: Dict[str, Any],
    job: Dict[str, Any],
    final_decision_text: str,
    *,
    live_tickers: Iterable[str],
) -> Dict[str, Any]:
    """Transition a candidate after full_graph research."""
    if str(job.get("source") or "") != "candidate_promotion":
        return {"handled": False}
    tid = str(job.get("ticker") or "").strip().upper()
    rating = parse_rating(final_decision_text)
    promoted = rating in {"Buy", "Overweight"}
    rec = CandidateRecord(
        ticker=tid,
        source="candidate_full_graph",
        reason=str(job.get("reason") or ""),
        status="promoted" if promoted else "watch",
        priority=1 if promoted else 4,
        gates={"full_graph": "pass" if promoted else "watch"},
        next_action=(
            "Full graph was positive; PM comparison against current holdings requested."
            if promoted
            else "Full graph was not strong enough for promotion; keep on watchlist."
        ),
    )
    append_candidate_records(cfg, [rec])
    compared = run_promoted_candidate_pm_comparison(cfg, [rec], live_tickers=live_tickers) if promoted else 0
    return {"handled": True, "rating": rating, "status": rec.status, "pm_compared": compared}


def promoted_candidate_context(records: Iterable[CandidateRecord], *, live_tickers: Iterable[str]) -> str:
    """Build PM extra_context for promoted candidates."""
    promoted = [r for r in records if r.status == "promoted"]
    if not promoted:
        return ""
    live = sorted({str(t).strip().upper() for t in live_tickers if str(t).strip()})
    lines = [
        "Candidate comparison request:",
        "The following candidates passed gates and should be compared against current holdings.",
        "Do not treat candidates as held positions. Do not put candidate tickers in stances unless they are in the live portfolio snapshot.",
        "Use forward_tasks and executive_summary to say whether any candidate deserves deeper portfolio comparison, replacement analysis, or no action.",
        f"Current holdings: {', '.join(live) if live else '(unknown)'}",
        "",
        "Promoted candidates:",
    ]
    for r in promoted:
        refs = f" evidence_refs={','.join(r.evidence_refs)}" if r.evidence_refs else ""
        lines.append(
            f"- {r.ticker}: priority={r.priority}; source={r.source}; reason={r.reason or '(none)'}; "
            f"gates={json.dumps(r.gates, sort_keys=True)}{refs}"
        )
    return "\n".join(lines)


def run_promoted_candidate_pm_comparison(
    cfg: Dict[str, Any],
    records: Iterable[CandidateRecord],
    *,
    live_tickers: Iterable[str],
) -> int:
    """Run one advisor PM comparison cycle for promoted candidates. Returns count sent."""
    promoted = [r for r in records if r.status == "promoted"]
    if not promoted:
        return 0
    if not bool(cfg.get("portfolio_advisor_pm_enabled", True)):
        return 0
    if not bool(cfg.get("portfolio_advisor_pm_candidate_comparison", True)):
        return 0
    context = promoted_candidate_context(promoted, live_tickers=live_tickers)
    if not context:
        return 0
    from tradingagents.portfolio_advisor.advisor_pm import run_pm_cycle

    run_pm_cycle(cfg, trigger="candidate_comparison", extra_context=context)
    return len(promoted)
