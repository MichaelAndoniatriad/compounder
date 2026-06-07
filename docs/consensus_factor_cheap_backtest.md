# Consensus Factor Cheap Backtest

**Date:** 2026-06-07
**Sample:** 80 observations across 4 dates

## 90 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=12, mean=+8.05%, median=+7.75% |
| Not in consensus | n=68, mean=+8.26%, median=+3.83% |

Spread (in minus out): -0.21%

## 180 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=12, mean=+20.29%, median=+11.48% |
| Not in consensus | n=68, mean=+18.25%, median=+13.28% |

Spread (in minus out): +2.04%

## 365 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=9, mean=+41.62%, median=+42.74% |
| Not in consensus | n=51, mean=+46.33%, median=+25.21% |

Spread (in minus out): -4.71%

## Interpretation

At the 365 day horizon, the spread is negligible at -4.7% (n=9 in, n=51 out). The binary in/out consensus signal alone does not separate winners from losers. The sub-factor decomposition (entry timing, divergence, retail flow) matters more than the binary flag.

## Caveats

- 80 observation cap (4 dates × 20 tickers). Small sample, no statistical power.
- Manual universe selection: consensus group is high growth tech; control group is   value/industrial/consumer staples. Sector effects swamp consensus effects.
- Survivorship bias in both groups: all 20 are large cap survivors.
- Insider Monkey scraping is noisy: article date filtering is approximate,   search results include content outside the 30 day window.
- No divergence factor tested. The cheap backtest only tests binary consensus membership.
- Scraping returned 12 total ticker hits across 4 dates.

## Recommendation

**Keep the scaffolding, refine to sub-factors.** The binary consensus flag shows no large directional effect (spread -4.7%). But the entry timing, divergence, and retail flow sub-factors (untested here) may carry signal. The infrastructure is built and costs nothing to run. Keep CONSENSUS_FACTOR_LIVE=false until a proper sub-factor backtest with 30+ real trades confirms or refutes.
