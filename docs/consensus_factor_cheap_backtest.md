# Consensus Factor Cheap Backtest

**Date:** 2026-06-07
**Sample:** 80 observations across 4 dates

## 90 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=28, mean=+8.35%, median=+7.75% |
| Not in consensus | n=52, mean=+8.16%, median=+2.49% |

Spread (in minus out): +0.19%

## 180 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=28, mean=+17.25%, median=+14.72% |
| Not in consensus | n=52, mean=+19.26%, median=+11.82% |

Spread (in minus out): -2.01%

## 365 day

| Bucket | Sample | Mean | Median |
|--------|--------|------|--------|
| In consensus | n=21, mean=+38.40%, median=+26.52% |
| Not in consensus | n=39, mean=+49.51%, median=+25.21% |

Spread (in minus out): -11.11%

## Interpretation

At the 365 day horizon, consensus names underperformed by -11.1% (n=21 in, n=39 out). Being outside the consensus was beneficial for long term returns in this window. The anti-herd factor has merit.

## Caveats

- 80 observation cap (4 dates × 20 tickers). Small sample, no statistical power.
- Manual universe selection: consensus group is high growth tech; control group is   value/industrial/consumer staples. Sector effects swamp consensus effects.
- Survivorship bias in both groups: all 20 are large cap survivors.
- Insider Monkey scraping is noisy: article date filtering is approximate,   search results include content outside the 30 day window.
- No divergence factor tested. The cheap backtest only tests binary consensus membership.
- Scraping returned 28 total ticker hits across 4 dates.

## Recommendation

**The anti-herd factor shows weak evidence.** Being outside consensus correlated with better returns in this window. Keep the scaffolding and test with sub-factor decomposition when real trade data is available. Do not flip CONSENSUS_FACTOR_LIVE based on this cheap backtest alone.
