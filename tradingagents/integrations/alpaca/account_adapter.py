# tradingagents/integrations/alpaca/account_adapter.py
"""Alpaca paper account presented in the eToro portfolio-row schema.

Autonomous mode (account_mode="alpaca"): the PM manages the Alpaca PAPER book
as the first-class portfolio. Everything downstream of
``etoro_scan.fetch_portfolio_rows()`` — PM snapshot, watchdog trigger math,
sleeve allocation, weekly check, portfolio risk — consumes rows in the eToro
field shape, so this adapter maps Alpaca positions into exactly that shape and
nothing else has to change:

    symbolFull              ← position.symbol
    units                   ← qty
    openRate                ← avg_entry_price
    amount                  ← cost_basis        (invested USD)
    initialAmountInDollars  ← cost_basis
    unitsBaseValueDollars   ← market_value
    unrealizedPnL           ← unrealized_pl     (flat USD value)
    isBuy                   ← side == long
    positionId              ← "alpaca:<symbol>"

The payload mirrors the eToro PnL envelope (``clientPortfolio.credit`` = cash)
so ``portfolio_headlines()`` keeps working. Read-only: this module never
places orders (the executor owns that).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def fetch_portfolio_rows_alpaca() -> Tuple[Dict[str, Any], str, List[str], List[Dict[str, Any]]]:
    """Drop-in twin of ``etoro_scan.fetch_portfolio_rows()`` reading the Alpaca paper book."""
    from tradingagents.integrations.alpaca import executor as _exec

    client = _exec._client()
    acct = client.get_account()
    positions = client.get_all_positions()

    cash = float(acct.cash)
    equity = float(acct.equity)

    rows: List[Dict[str, Any]] = []
    total_upl = 0.0
    for p in positions:
        try:
            qty = float(p.qty)
            entry = float(p.avg_entry_price)
            mv = float(p.market_value or 0)
            cost = float(p.cost_basis or 0)
            upl = float(p.unrealized_pl or 0)
        except (TypeError, ValueError):
            continue
        total_upl += upl
        rows.append(
            {
                "symbolFull": str(p.symbol).upper(),
                "instrumentDisplayName": str(p.symbol).upper(),
                "instrumentId": None,
                "openRate": entry,
                "units": qty,
                "amount": abs(cost),
                "initialAmountInDollars": abs(cost),
                "unitsBaseValueDollars": mv,
                "isBuy": str(getattr(p, "side", "long")).lower().endswith("long"),
                "unrealizedPnL": upl,
                "positionId": f"alpaca:{p.symbol}",
            }
        )

    payload = {
        "clientPortfolio": {
            "credit": cash,
            "unrealizedPnL": round(total_upl, 2),
            "positions": rows,
            "mirrors": [],
        }
    }

    tickers = sorted({r["symbolFull"] for r in rows})
    lines = [
        "Account snapshot (Alpaca PAPER — autonomous mode, PM-managed)",
        f"Available balance (cash): {cash}",
        f"Equity: {equity}",
        f"Unrealized P&L (aggregate): {round(total_upl, 2)}",
        "",
        "Open positions:",
    ]
    for r in rows:
        gain_pct = (r["unrealizedPnL"] / r["amount"] * 100.0) if r["amount"] else 0.0
        lines.append(
            f"  • {r['symbolFull']}  {'long' if r['isBuy'] else 'short'}  "
            f"units={r['units']:g}  open={r['openRate']}  uPnL={r['unrealizedPnL']:+.2f} ({gain_pct:+.1f}%)"
        )
    if not rows:
        lines.append("  (none)")

    head = (
        f"Headlines: available_balance={cash!r}, "
        f"unrealized_pnl={round(total_upl, 2)!r}, "
        f"open_positions={len(rows)!r}\n"
    )
    return payload, head + "\n".join(lines), tickers, rows
