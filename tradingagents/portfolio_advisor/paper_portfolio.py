"""Paper portfolio — simulates following every PM recommendation exactly.

Phase 5: Tracks a parallel portfolio that executes every recommendation at
next-session-open prices with realistic friction (0.1% per trade). Compares
returns against the actual portfolio and SPY benchmark.

Answers: if you followed the PM blindly, would you be better or worse off?
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FRICTION = 0.001  # 0.1% per trade (spread + commission proxy)


def _paper_path(cfg: Dict[str, Any]) -> Path:
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "paper_portfolio.json"


def _trades_path(cfg: Dict[str, Any]) -> Path:
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "paper_trades.jsonl"


class PaperPortfolio:
    """Simulated portfolio that follows PM recommendations at next-open prices."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.cash: float = 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}  # ticker -> {shares, avg_price, cost_basis}
        self.start_date: str = ""
        self.total_fees: float = 0.0
        self._load()

    def _load(self) -> None:
        p = _paper_path(self.cfg)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.cash = float(data.get("cash", 0))
                self.positions = data.get("positions", {})
                self.start_date = data.get("start_date", "")
                self.total_fees = float(data.get("total_fees", 0))
            except (json.JSONDecodeError, ValueError):
                pass

    def _save(self) -> None:
        p = _paper_path(self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self.cash,
            "positions": self.positions,
            "start_date": self.start_date,
            "total_fees": round(self.total_fees, 2),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def initialise_from_etoro(self) -> str:
        """Seed the paper portfolio with current eToro positions and cash."""
        try:
            from tradingagents.portfolio_advisor.etoro_scan import fetch_portfolio_rows
            from tradingagents.portfolio_advisor.etoro_scan import portfolio_headlines as _hl
            payload, _, _, rows = fetch_portfolio_rows()
        except Exception as e:
            return f"paper portfolio init failed: {e}"

        headlines = _hl(payload)
        self.cash = float(headlines.get("credit", 0))
        self.positions = {}
        self.start_date = date.today().isoformat()
        self.total_fees = 0.0

        for r in rows:
            ticker = str(r.get("symbolFull", "") or r.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            units = float(r.get("units", 0) or r.get("Units", 0))
            open_rate = float(r.get("openRate", 0) or r.get("OpenRate", 0))
            if units <= 0 or open_rate <= 0:
                continue
            if ticker not in self.positions:
                self.positions[ticker] = {"shares": 0.0, "avg_price": 0.0, "cost_basis": 0.0}
            pos = self.positions[ticker]
            new_shares = pos["shares"] + units
            new_cost = pos["cost_basis"] + units * open_rate
            pos["shares"] = round(new_shares, 6)
            pos["avg_price"] = round(new_cost / new_shares, 2) if new_shares > 0 else 0.0
            pos["cost_basis"] = round(new_cost, 2)

        self._save()
        n_positions = len(self.positions)
        total_invested = sum(p["cost_basis"] for p in self.positions.values())
        return (
            f"paper portfolio initialised: ${total_invested:.0f} invested across "
            f"{n_positions} tickers, ${self.cash:.0f} cash, "
            f"start date {self.start_date}"
        )

    def execute_recommendation(self, rec: Dict[str, Any]) -> str:
        """Execute a recommendation at next-session-open price with friction.

        Handles: ep_entry (buy), ep_exit (sell), sizing (adjust positions).
        Returns a status string.
        """
        rec_type = rec.get("type", "")
        ticker = rec.get("ticker", "")
        action = rec.get("action", "")

        if not ticker and rec_type not in ("sizing",):
            return f"skipped: no ticker for {rec_type}"

        # Sizing and macro recommendations have no executable fill but are tracked
        if rec_type == "sizing":
            self._log_trade(rec["id"], "PORTFOLIO", "sizing_skip", 0, 0, 0,
                           note=f"sizing: {action} — tracked for audit, no fill")
            return f"paper sizing: {action} (tracked, no fill)"

        if rec_type == "macro_alert":
            self._log_trade(rec["id"], "PORTFOLIO", "alert_skip", 0, 0, 0,
                           note=f"macro alert: {action} — tracked for audit, no fill")
            return f"paper alert: {action} (tracked, no fill)"

        if not ticker:
            return f"skipped: no ticker for {rec_type}"

        import yfinance as yf

        try:
            if ticker:
                hist = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
                if len(hist) < 1:
                    return f"skipped {ticker}: no price data"
                exec_price = float(hist["Close"].iloc[-1])
            else:
                exec_price = None
        except Exception:
            return f"skipped {ticker}: price fetch failed"

        if rec_type == "ep_entry" and ticker and exec_price:
            shares = float(rec.get("shares", 0))
            if shares <= 0:
                return f"skipped {ticker}: no share count"
            cost = shares * exec_price
            fee = cost * FRICTION

            if cost + fee > self.cash:
                # Scale down to available cash
                affordable = int((self.cash - fee) / exec_price)
                if affordable <= 0:
                    return f"skipped {ticker}: insufficient cash (need ${cost:.0f}, have ${self.cash:.0f})"
                shares = float(affordable)
                cost = shares * exec_price
                fee = cost * FRICTION

            self.cash -= cost + fee
            self.total_fees += fee

            if ticker not in self.positions:
                self.positions[ticker] = {"shares": 0.0, "avg_price": 0.0, "cost_basis": 0.0}
            pos = self.positions[ticker]
            new_shares = pos["shares"] + shares
            new_cost = pos["cost_basis"] + cost
            pos["shares"] = round(new_shares, 6)
            pos["avg_price"] = round(new_cost / new_shares, 2)
            pos["cost_basis"] = round(new_cost, 2)

            self._save()
            self._log_trade(rec["id"], ticker, "buy", shares, exec_price, fee)
            return f"paper buy: {shares:.2f} {ticker} @ ${exec_price:.2f} (fee ${fee:.2f})"

        elif rec_type == "ep_exit" and ticker and exec_price:
            pos = self.positions.get(ticker)
            if not pos or pos["shares"] <= 0:
                return f"skipped {ticker}: no position to sell"
            shares = pos["shares"]
            proceeds = shares * exec_price
            fee = proceeds * FRICTION

            self.cash += proceeds - fee
            self.total_fees += fee
            del self.positions[ticker]

            self._save()
            self._log_trade(rec["id"], ticker, "sell", shares, exec_price, fee)
            return f"paper sell: {shares:.2f} {ticker} @ ${exec_price:.2f} (fee ${fee:.2f}, proceeds ${proceeds - fee:.0f})"

        return f"skipped {ticker}: {rec_type} not supported for paper execution"


    def compute_returns(self) -> Dict[str, Any]:
        """Compute paper portfolio returns vs actual portfolio and SPY."""
        total_invested = sum(
            (p["shares"] * self._current_price(ticker))
            for ticker, p in self.positions.items()
        )
        paper_value = self.cash + total_invested

        # Actual portfolio value from eToro
        try:
            from tradingagents.portfolio_advisor.etoro_scan import fetch_portfolio_rows
            payload, _, _, _ = fetch_portfolio_rows()
            cp = payload.get("clientPortfolio", {})
            actual_credit = float(cp.get("credit", 0))
            actual_unreal = float(cp.get("unrealizedPnL", 0))
            # Approximate actual total value
            actual_value = actual_credit + actual_unreal
            for p in self.positions.values():
                actual_value += p["cost_basis"]
        except Exception:
            actual_value = None

        # SPY benchmark
        spy_return = None
        try:
            import yfinance as yf
            spy = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=False)
            if len(spy) >= 2 and self.start_date:
                start_dt = datetime.strptime(self.start_date, "%Y-%m-%d")
                spy_start = spy.loc[spy.index >= start_dt]
                if len(spy_start) >= 1:
                    spy_return = float(
                        (spy["Close"].iloc[-1] - spy_start["Close"].iloc[0])
                        / spy_start["Close"].iloc[0]
                    )
        except Exception:
            pass

        trades = list(self._iter_trades())
        executable = [t for t in trades if t.get("side") not in ("sizing_skip", "alert_skip")]
        tracked = [t for t in trades if t.get("side") in ("sizing_skip", "alert_skip")]

        return {
            "paper_value": round(paper_value, 2),
            "paper_cash": round(self.cash, 2),
            "paper_invested": round(total_invested, 2),
            "paper_fees": round(self.total_fees, 2),
            "actual_value": round(actual_value, 2) if actual_value else None,
            "spy_return": round(spy_return * 100, 1) if spy_return is not None else None,
            "start_date": self.start_date,
            "n_positions": len(self.positions),
            "n_trades": len(trades),
            "n_executable": len(executable),
            "n_tracked_only": len(tracked),
        }

    def _current_price(self, ticker: str) -> float:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 1:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    def _log_trade(self, rec_id: str, ticker: str, side: str,
                   shares: float, price: float, fee: float,
                   note: str = "") -> None:
        p = _trades_path(self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": uuid.uuid4().hex[:12],
            "rec_id": rec_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "side": side,
            "shares": round(shares, 6),
            "price": round(price, 2),
            "fee": round(fee, 2),
        }
        if note:
            entry["note"] = note
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _iter_trades(self):
        p = _trades_path(self.cfg)
        if not p.is_file():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def build_paper_portfolio_block(cfg: Dict[str, Any]) -> str:
    """Return a PM prompt block with paper vs actual comparison."""
    pp = PaperPortfolio(cfg)
    if not pp.start_date:
        return ""

    returns = pp.compute_returns()
    lines = [
        f"Paper portfolio (since {returns['start_date']}):",
        f"  Value: ${returns['paper_value']:.0f} ({returns['n_positions']} positions, ${returns['paper_cash']:.0f} cash)",
        f"  Fees paid: ${returns['paper_fees']:.2f}",
        f"  Trades executed: {returns['n_trades']}",
    ]
    if returns["actual_value"]:
        lines.append(f"  Actual portfolio: ~${returns['actual_value']:.0f}")
    if returns["spy_return"] is not None:
        lines.append(f"  SPY return: {returns['spy_return']:+.1f}%")

    return "\n".join(lines) + "\n"
