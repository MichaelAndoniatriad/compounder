"""Portfolio risk analytics — correlation, concentration, risk contribution.

Phase 4: Deterministic math layer. No LLM, no probability estimates, just
computation from yfinance data and eToro positions. Feeds into PM prompt
as a risk block so the PM can flag issues with data, not intuition.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def compute_correlation_matrix(
    tickers: List[str],
    lookback_days: int = 60,
) -> Dict[str, Dict[str, float]]:
    """Compute pairwise Pearson correlations between all holdings.

    Requires at least 20 data points per ticker. Returns empty dict if
    insufficient data. Pairs with >0.80 correlation are effectively the
    same position in risk terms.
    """
    if len(tickers) < 2:
        return {}

    # Fetch daily returns
    returns: Dict[str, np.ndarray] = {}
    import yfinance as yf

    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period=f"{lookback_days}d", interval="1d",
                                        auto_adjust=False)
            if len(hist) < 20:
                continue
            rets = hist["Close"].pct_change().dropna().values
            if len(rets) < 20:
                continue
            returns[t] = rets
        except Exception:
            logger.debug("correlation: yfinance failed for %s", t, exc_info=True)
            continue

    valid = sorted(returns.keys())
    if len(valid) < 2:
        return {}

    matrix: Dict[str, Dict[str, float]] = {}
    for t1 in valid:
        matrix[t1] = {}
        for t2 in valid:
            if t1 == t2:
                matrix[t1][t2] = 1.0
            else:
                # Align lengths
                min_len = min(len(returns[t1]), len(returns[t2]))
                r1 = returns[t1][-min_len:]
                r2 = returns[t2][-min_len:]
                try:
                    corr = float(np.corrcoef(r1, r2)[0, 1])
                    matrix[t1][t2] = round(corr, 3) if not np.isnan(corr) else 0.0
                except Exception:
                    matrix[t1][t2] = 0.0
    return matrix


def compute_risk_contributions(
    positions: List[Dict[str, Any]],
    correlation_matrix: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    """Compute each position's contribution to portfolio risk.

    Uses a simplified approach: weight × volatility × correlation to portfolio.
    Returns dict keyed by ticker with {weight, vol_annual, risk_contribution_pct}.
    """
    if not positions or not correlation_matrix:
        return {}

    # Compute market values and weights
    total_value = sum(
        abs(float(p.get("invested_usd", 0) or p.get("initialAmountInDollars", 0)))
        for p in positions
    )
    if total_value <= 0:
        return {}

    weights: Dict[str, float] = {}
    vols: Dict[str, float] = {}
    tickers = list(correlation_matrix.keys())

    import yfinance as yf
    for p in positions:
        ticker = str(p.get("ticker", "") or p.get("symbolFull", "")).strip().upper()
        if not ticker or ticker not in tickers:
            continue
        val = abs(float(p.get("invested_usd", 0) or p.get("initialAmountInDollars", 0)))
        weights[ticker] = val / total_value

        # Fetch volatility
        try:
            hist = yf.Ticker(ticker).history(period="60d", interval="1d", auto_adjust=False)
            if len(hist) >= 20:
                daily_ret = hist["Close"].pct_change().dropna()
                vols[ticker] = float(daily_ret.std() * np.sqrt(252))  # annualised
        except Exception:
            vols[ticker] = 0.0

    # Compute portfolio volatility (simplified)
    port_var = 0.0
    for t1 in tickers:
        if t1 not in weights or t1 not in vols:
            continue
        for t2 in tickers:
            if t2 not in weights or t2 not in vols:
                continue
            corr = correlation_matrix.get(t1, {}).get(t2, 0.0)
            port_var += weights[t1] * weights[t2] * vols[t1] * vols[t2] * corr

    port_vol = np.sqrt(max(port_var, 0.0001))

    # Marginal risk contribution per position
    out: Dict[str, Dict[str, float]] = {}
    for t1 in tickers:
        if t1 not in weights or t1 not in vols:
            continue
        # Simplified: marginal contribution ≈ weight × cov with portfolio
        cov_with_port = 0.0
        for t2 in tickers:
            if t2 not in weights or t2 not in vols:
                continue
            corr = correlation_matrix.get(t1, {}).get(t2, 0.0)
            cov_with_port += weights[t2] * vols[t1] * vols[t2] * corr

        risk_contribution = weights[t1] * cov_with_port / port_vol if port_vol > 0 else 0.0
        out[t1] = {
            "weight": round(weights[t1] * 100, 1),
            "vol_annual": round(vols.get(t1, 0) * 100, 1),
            "risk_contribution_pct": round(risk_contribution / port_vol * 100, 1) if port_vol > 0 else 0.0,
        }

    return out


def compute_concentration_flags(
    positions: List[Dict[str, Any]],
    correlation_matrix: Dict[str, Dict[str, float]],
    risk_contributions: Dict[str, Dict[str, float]],
) -> List[str]:
    """Return a list of human-readable concentration warnings."""
    flags: List[str] = []

    # 1. High-correlation pairs
    high_corr_pairs = []
    tickers = sorted(correlation_matrix.keys())
    for i, t1 in enumerate(tickers):
        for t2 in tickers[i + 1:]:
            corr = correlation_matrix[t1].get(t2, 0)
            if abs(corr) > 0.80:
                high_corr_pairs.append((t1, t2, corr))

    if high_corr_pairs:
        pairs_str = ", ".join(f"{t1}/{t2}={c:.2f}" for t1, t2, c in high_corr_pairs[:5])
        flags.append(f"High correlation pairs (>0.80): {pairs_str}. These are effectively the same position.")

    # 2. Overweight positions (risk contribution > 2× weight)
    for ticker, rc in risk_contributions.items():
        if rc["weight"] > 0 and rc["risk_contribution_pct"] > rc["weight"] * 2:
            flags.append(
                f"{ticker}: {rc['risk_contribution_pct']:.0f}% of portfolio risk "
                f"from {rc['weight']:.0f}% allocation — consider reducing."
            )

    # 3. Single-ticker concentration > 25%
    ticker_positions: Dict[str, float] = defaultdict(float)
    for p in positions:
        ticker = str(p.get("ticker", "") or p.get("symbolFull", "")).strip().upper()
        if not ticker:
            continue
        val = abs(float(p.get("invested_usd", 0) or p.get("initialAmountInDollars", 0)))
        ticker_positions[ticker] += val

    total = sum(ticker_positions.values())
    for ticker, val in ticker_positions.items():
        pct = val / total * 100 if total > 0 else 0
        if pct > 25:
            lot_count = sum(1 for p in positions
                           if str(p.get("ticker", "") or p.get("symbolFull", "")).strip().upper() == ticker)
            flags.append(
                f"{ticker}: {pct:.0f}% of portfolio across {lot_count} lots — "
                f"extreme concentration. Consider staged reduction."
            )

    # 4. Too many lots in one ticker
    for ticker, val in ticker_positions.items():
        lot_count = sum(1 for p in positions
                       if str(p.get("ticker", "") or p.get("symbolFull", "")).strip().upper() == ticker)
        if lot_count >= 8:
            flags.append(
                f"{ticker}: {lot_count} separate lots at different entries — "
                f"consider consolidating or trimming smallest lots."
            )

    return flags


def build_portfolio_risk_block(cfg: Dict[str, Any]) -> str:
    """Build a PM prompt block with portfolio risk analytics.

    Fetches live positions from eToro, computes correlations, risk
    contributions, and concentration flags. Returns compact text for
    injection into the PM prompt.
    """
    try:
        from tradingagents.portfolio_advisor.etoro_scan import fetch_portfolio_rows

        _, _, tickers, rows = fetch_portfolio_rows()
        if not tickers or not rows:
            return ""
    except Exception as e:
        logger.debug("portfolio risk: eToro fetch failed: %s", e)
        return ""

    # Get unique tickers
    unique_tickers = sorted(set(
        str(r.get("symbolFull", "") or r.get("ticker", "")).strip().upper()
        for r in rows
        if str(r.get("symbolFull", "") or r.get("ticker", "")).strip()
    ))

    if len(unique_tickers) < 2:
        return ""

    # Compute
    corr_matrix = compute_correlation_matrix(unique_tickers)
    risk_contribs = compute_risk_contributions(rows, corr_matrix)
    flags = compute_concentration_flags(rows, corr_matrix, risk_contribs)

    # Build output
    lines = ["Portfolio risk analytics:"]

    # Risk contributions
    if risk_contribs:
        lines.append("  Risk contribution by holding:")
        for ticker, rc in sorted(risk_contribs.items(),
                                  key=lambda x: -x[1]["risk_contribution_pct"]):
            lines.append(
                f"    {ticker}: {rc['weight']:.0f}% weight, "
                f"{rc['vol_annual']:.0f}% vol, "
                f"{rc['risk_contribution_pct']:.0f}% risk contribution"
            )

    # Concentration flags
    if flags:
        lines.append("  Concentration warnings:")
        for flag in flags:
            lines.append(f"    ⚠ {flag}")
    elif risk_contribs:
        lines.append("  No concentration warnings.")

    return "\n".join(lines) + "\n"
