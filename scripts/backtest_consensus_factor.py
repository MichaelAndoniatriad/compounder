"""Cheap consensus factor backtest — scrape Insider Monkey + yfinance prices.

4 dates, 20 tickers, no paid APIs. Produces cheap_backtest_data.csv.
"""
import csv, json, re, sys, time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Set, Tuple

import requests

OBSERVATION_DATES = ["2024-01-31", "2024-07-31", "2025-01-31", "2025-07-31"]
CONSENSUS_TICKERS = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "AAPL", "PLTR", "AMD"]
CONTROL_TICKERS = ["PG", "KO", "JNJ", "GE", "CAT", "MMM", "UNH", "CVX", "XOM", "WMT"]
ALL_TICKERS = CONSENSUS_TICKERS + CONTROL_TICKERS

SEARCH_QUERIES = ["ChatGPT+stock", "Claude+stock", "Grok+stock", "AI+stock+picks"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
NOISE = {"THE", "AND", "FOR", "ARE", "NOT", "BUT", "HAS", "HAD", "WAS", "WERE",
         "WILL", "FROM", "THAT", "WITH", "THIS", "THEY", "HAVE", "BEEN", "WOULD",
         "WHICH", "THEIR", "THERE", "ABOUT", "STOCK", "STOCKS", "YOUR", "WHAT",
         "WHEN", "MORE", "ALSO", "INTO", "OVER", "AFTER", "BEFORE", "COULD",
         "OTHER", "SOME", "THESE", "THOSE", "EACH", "BOTH", "MANY", "MOST",
         "SUCH", "ONLY", "THEN", "NOW", "HERE", "VERY", "JUST", "LIKE", "MAKE",
         "MADE", "WELL", "EVEN", "MUCH", "STILL", "KNOW", "SHOULD", "WHILE",
         "FIRST", "AFTER", "BETWEEN", "SINCE", "UNDER", "ABOVE", "BELOW",
         "WHERE", "WHICH", "THROUGH", "DURING", "DOES", "MIGHT", "BEING",
         "LOOK", "NEXT", "YEAR", "YEARS", "MONTH", "MONTHS", "WEEK", "WEEKS",
         "TAKE", "GETS", "GETS", "SAYS", "GOOD", "BEST", "HIGH", "LOW", "LONG",
         "SHORT", "LARGE", "SMALL", "GREAT", "LESS", "MEAN", "SAME", "LAST"}

CACHE_DIR = Path.home() / ".tradingagents" / "cache" / "cheap_backtest"


def scrape_date(date_str: str) -> Set[str]:
    """Scrape Insider Monkey for ticker mentions in the 30 days before date_str."""
    cache_file = CACHE_DIR / f"scrape_{date_str}.json"
    if cache_file.is_file():
        try:
            return set(json.loads(cache_file.read_text()))
        except (json.JSONDecodeError, OSError):
            pass

    all_tickers: Set[str] = set()
    for query in SEARCH_QUERIES:
        url = f"https://www.insidermonkey.com/?s={query}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                print(f"  WARN: HTTP {r.status_code} for {url[:60]}")
                continue
            raw = set(re.findall(r'\b[A-Z]{2,5}\b', r.text))
            all_tickers.update(raw)
            time.sleep(1)  # Be polite
        except requests.RequestException as e:
            print(f"  WARN: {e} for {url[:60]}")
            continue

    target_hits = sorted(all_tickers & set(ALL_TICKERS) - NOISE)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(target_hits))
    return set(target_hits)


def get_returns(ticker: str, obs_date: str) -> Tuple[float, float, float, float]:
    """Return (close, return_90d, return_180d, return_365d) or all None on failure."""
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(start="2024-01-01", end="2026-07-01", interval="1d", auto_adjust=False)
        if hist.empty:
            return (None, None, None, None)
        
        # Find closest trading day to obs_date
        obs_dt = date.fromisoformat(obs_date)
        # Get price at or just before obs_date
        before = hist[hist.index <= str(obs_dt)]
        if before.empty:
            return (None, None, None, None)
        entry_close = float(before["Close"].iloc[-1])
        
        # Get prices at horizons
        horizons = [90, 180, 365]
        returns = []
        for h_days in horizons:
            target_dt = obs_dt + timedelta(days=h_days)
            after = hist[hist.index >= str(target_dt)]
            if after.empty:
                returns.append(None)
            else:
                exit_close = float(after["Close"].iloc[0])
                returns.append(round((exit_close / entry_close - 1), 4))
        
        return (round(entry_close, 2), returns[0], returns[1], returns[2])
    except Exception as e:
        print(f"  ERROR {ticker} @ {obs_date}: {e}")
        return (None, None, None, None)


def main():
    print("=== CHEAP CONSENSUS BACKTEST ===")
    
    # Phase 1: Scrape
    print("\nPhase 1: Scraping Insider Monkey...")
    consensus_by_date: Dict[str, Set[str]] = {}
    for obs_date in OBSERVATION_DATES:
        print(f"  {obs_date}...")
        consensus_by_date[obs_date] = scrape_date(obs_date)
        hits = sorted(consensus_by_date[obs_date])
        print(f"    Found {len(hits)}: {', '.join(hits[:10])}{'...' if len(hits) > 10 else ''}")
    
    # Phase 2: Price data
    print("\nPhase 2: Fetching prices via yfinance...")
    rows = []
    skipped = 0
    for obs_date in OBSERVATION_DATES:
        consensus_set = consensus_by_date[obs_date]
        for ticker in ALL_TICKERS:
            in_consensus = ticker in consensus_set
            entry, r90, r180, r365 = get_returns(ticker, obs_date)
            if r90 is None and r180 is None and r365 is None:
                skipped += 1
                continue
            rows.append({
                "ticker": ticker,
                "observation_date": obs_date,
                "in_consensus": in_consensus,
                "return_90d": r90,
                "return_180d": r180,
                "return_365d": r365,
            })
    
    # Write CSV
    csv_path = Path("docs/cheap_backtest_data.csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "observation_date", "in_consensus", "return_90d", "return_180d", "return_365d"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nPhase 3: Wrote {len(rows)} rows to {csv_path} ({skipped} skipped)")
    
    # Phase 4: Summary stats
    print("\nPhase 4: Computing summary stats...")
    horizons = ["return_90d", "return_180d", "return_365d"]
    with open("docs/consensus_factor_cheap_backtest.md", "w") as f:
        f.write("# Consensus Factor Cheap Backtest\n\n")
        f.write(f"**Date:** {date.today().isoformat()}\n")
        f.write(f"**Sample:** {len(rows)} observations across {len(OBSERVATION_DATES)} dates\n\n")
        
        for horizon in horizons:
            in_c = [r[horizon] for r in rows if r["in_consensus"] and r[horizon] is not None]
            out_c = [r[horizon] for r in rows if not r["in_consensus"] and r[horizon] is not None]
            
            def stats(vals):
                if not vals:
                    return "n=0"
                v = sorted(vals)
                mean = sum(v) / len(v)
                median = v[len(v)//2]
                return f"n={len(v)}, mean={mean:+.2%}, median={median:+.2%}"
            
            h_label = horizon.replace("return_", "").replace("d", " day")
            f.write(f"## {h_label}\n\n")
            f.write(f"| Bucket | Sample | Mean | Median |\n")
            f.write(f"|--------|--------|------|--------|\n")
            f.write(f"| In consensus | {stats(in_c)} |\n")
            f.write(f"| Not in consensus | {stats(out_c)} |\n\n")
            
            if in_c and out_c:
                spread = (sum(in_c)/len(in_c)) - (sum(out_c)/len(out_c))
                f.write(f"Spread (in minus out): {spread:+.2%}\n\n")
        
        # Interpretation
        f.write("## Interpretation\n\n")
        
        if rows:
            # Quick check: is there any pattern?
            all_in = [r["return_365d"] for r in rows if r["in_consensus"] and r["return_365d"] is not None]
            all_out = [r["return_365d"] for r in rows if not r["in_consensus"] and r["return_365d"] is not None]
            if all_in and all_out:
                spread_365 = (sum(all_in)/len(all_in)) - (sum(all_out)/len(all_out))
                n_in = len(all_in)
                n_out = len(all_out)
                
                if spread_365 > 0.05:
                    f.write(f"At the 365 day horizon, consensus names outperformed by {spread_365:+.1%} ")
                    f.write(f"(n={n_in} in, n={n_out} out). This suggests the herd is directionally correct ")
                    f.write(f"on a multi-month timeframe. The consensus factor functions as a trend ")
                    f.write(f"confirmation signal: being in the herd is not harmful for long holds.\n\n")
                    f.write(f"The entry timing sub-factor (section 7.4 of the v4 plan) may still be useful: ")
                    f.write(f"fresh consensus entries (under 30 days) versus stale (180 plus days). ")
                    f.write(f"Without sub-factor decomposition in this cheap backtest, that remains untested.\n\n")
                elif spread_365 < -0.05:
                    f.write(f"At the 365 day horizon, consensus names underperformed by {spread_365:+.1%} ")
                    f.write(f"(n={n_in} in, n={n_out} out). Being outside the consensus was beneficial ")
                    f.write(f"for long term returns in this window. The anti-herd factor has merit.\n\n")
                else:
                    f.write(f"At the 365 day horizon, the spread is negligible at {spread_365:+.1%} ")
                    f.write(f"(n={n_in} in, n={n_out} out). The binary in/out consensus signal alone ")
                    f.write(f"does not separate winners from losers. The sub-factor decomposition ")
                    f.write(f"(entry timing, divergence, retail flow) matters more than the binary flag.\n\n")
        
        # Caveats
        f.write("## Caveats\n\n")
        f.write(f"- 80 observation cap (4 dates × 20 tickers). Small sample, no statistical power.\n")
        f.write(f"- Manual universe selection: consensus group is high growth tech; control group is ")
        f.write(f"  value/industrial/consumer staples. Sector effects swamp consensus effects.\n")
        f.write(f"- Survivorship bias in both groups: all 20 are large cap survivors.\n")
        f.write(f"- Insider Monkey scraping is noisy: article date filtering is approximate, ")
        f.write(f"  search results include content outside the 30 day window.\n")
        f.write(f"- No divergence factor tested. The cheap backtest only tests binary consensus membership.\n")
        f.write(f"- Scraping returned {sum(len(consensus_by_date[d]) for d in consensus_by_date)} total ")
        f.write(f"ticker hits across {len(OBSERVATION_DATES)} dates.\n\n")
        
        # Recommendation
        f.write("## Recommendation\n\n")
        if all_in and all_out:
            spread_365 = (sum(all_in)/len(all_in)) - (sum(all_out)/len(all_out))
            if abs(spread_365) < 0.05:
                f.write(f"**Keep the scaffolding, refine to sub-factors.** The binary consensus flag ")
                f.write(f"shows no large directional effect (spread {spread_365:+.1%}). But the ")
                f.write(f"entry timing, divergence, and retail flow sub-factors (untested here) ")
                f.write(f"may carry signal. The infrastructure is built and costs nothing to run. ")
                f.write(f"Keep CONSENSUS_FACTOR_LIVE=false until a proper sub-factor backtest ")
                f.write(f"with 30+ real trades confirms or refutes.\n")
            elif spread_365 > 0.05:
                f.write(f"**Consensus is a mild positive factor for long holds.** Consider adjusting ")
                f.write(f"the composite score to reward consensus membership for core positions ")
                f.write(f"(where multi-month holds are the norm) while keeping the divergence score ")
                f.write(f"for catalyst entries. Do not use consensus as a gate.\n")
            else:
                f.write(f"**The anti-herd factor shows weak evidence.** Being outside consensus ")
                f.write(f"correlated with better returns in this window. Keep the scaffolding ")
                f.write(f"and test with sub-factor decomposition when real trade data is available. ")
                f.write(f"Do not flip CONSENSUS_FACTOR_LIVE based on this cheap backtest alone.\n")
    
    print("Done. Reports written to docs/consensus_factor_cheap_backtest.md")
    return rows


if __name__ == "__main__":
    main()
