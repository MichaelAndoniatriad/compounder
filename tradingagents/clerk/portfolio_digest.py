# tradingagents/clerk/portfolio_digest.py
"""Daily eToro portfolio snapshot for the morning clerk (read-only)."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _snap_dir(cache_dir: Path) -> Path:
    d = Path(cache_dir) / "clerk" / "portfolio_snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _etoro_configured() -> bool:
    return bool(
        (os.environ.get("ETORO_API_KEY") or "").strip()
        and (os.environ.get("ETORO_USER_KEY") or "").strip()
    )


def _load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def fetch_current_portfolio_headlines() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (pnl_payload, headlines dict from portfolio_headlines)."""
    from tradingagents.portfolio_advisor.etoro_scan import (
        _etoro_enabled,
        _log_etoro_disabled_once,
    )
    if not _etoro_enabled():
        _log_etoro_disabled_once("fetch_current_portfolio_headlines")
        raise RuntimeError(
            "eToro reads are disabled (portfolio_advisor_etoro_enabled=False)."
        )
    from tradingagents.integrations.etoro.client import EtoroClient
    from tradingagents.integrations.etoro.portfolio import portfolio_headlines

    client = EtoroClient()
    payload = client.get_portfolio_pnl()
    return payload, portfolio_headlines(payload)


def build_daily_portfolio_markdown(
    *,
    cache_dir: Path,
    trade_date: str,
) -> Tuple[str, bool]:
    """Markdown section for morning digest + whether eToro was used.

    Compares to yesterday's saved snapshot when present (overnight-style delta).
    Persists today's snapshot at end for the next run.
    """
    if not _etoro_configured():
        return (
            "## Portfolio snapshot (eToro)\n\n"
            "*Skipped — set `ETORO_API_KEY` and `ETORO_USER_KEY` in `.env` for balances and overnight deltas.*\n",
            False,
        )

    snap_dir = _snap_dir(cache_dir)
    today = trade_date
    try:
        y = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    except ValueError:
        y = ""

    yesterday_snap = _load_snapshot(snap_dir / f"{y}.json") if y else None

    try:
        _payload, today_hl = fetch_current_portfolio_headlines()
    except Exception as e:
        return (
            f"## Portfolio snapshot (eToro)\n\n"
            f"*Could not load portfolio: {e}*\n",
            True,
        )

    lines: List[str] = [
        "## Portfolio snapshot (eToro — read-only)",
        "",
        f"- **As of:** {today}",
        f"- **Available balance (credit):** {today_hl.get('credit')!r}",
        f"- **Unrealized P&L (aggregate):** {today_hl.get('unrealized_pnl')!r}",
        f"- **Open positions (deduped):** {today_hl.get('open_positions')!r}",
        "",
    ]

    if yesterday_snap:
        lines.append("### vs prior snapshot (previous calendar day)")
        for key, label in (
            ("credit", "Available balance"),
            ("unrealized_pnl", "Unrealized P&L"),
            ("open_positions", "Open positions"),
        ):
            prev = yesterday_snap.get(key)
            cur = today_hl.get(key)
            if prev is not None and cur is not None:
                try:
                    delta = float(cur) - float(prev)  # type: ignore[arg-type]
                    lines.append(f"- **Δ {label}:** {prev!r} → {cur!r} ({delta:+.4g})")
                except (TypeError, ValueError):
                    lines.append(f"- **{label}:** {prev!r} → {cur!r}")
            elif cur is not None:
                lines.append(f"- **{label}:** {cur!r}")
        lines.append("")
    else:
        lines.append(
            "*No prior-day snapshot yet — tomorrow’s run will show day-over-day deltas "
            f"(snapshot file: `{snap_dir / f'{y}.json'}`).*\n"
        )

    lines.append(
        "_Daily clerk does not run deep multi-agent research; it only summarizes your book "
        "and headlines. Deep runs are queued on the **weekly** pass when triggers warrant it._"
    )
    lines.append("")

    # Persist today for tomorrow's delta
    try:
        snap_path = snap_dir / f"{today}.json"
        snap_path.write_text(
            json.dumps(
                {
                    "trade_date": today,
                    "credit": today_hl.get("credit"),
                    "unrealized_pnl": today_hl.get("unrealized_pnl"),
                    "open_positions": today_hl.get("open_positions"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    return "\n".join(lines), True
