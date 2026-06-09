# EP Gate Audit

**Date:** 7 June 2026
**Branch:** `ep-gate-audit`
**Status:** Instrumentation deployed; structural recommendation issued. Empirical data section to be populated after next scan cycle.

---

## Headline

The empty catalyst sleeve is **not** a "gates too tight" problem. It is a "wrong news feed for the patterns" problem. Loosening gap thresholds or liquidity floors would not produce more candidates because the upstream news classifier is not surfacing the right articles in the first place. The gap threshold tweak is a nice to have; the structural fix is what will actually produce EP candidates.

---

## What was instrumented

A single commit (`eb018a6`) on this branch adds three pieces of per-scan telemetry to `tradingagents/portfolio_advisor/ep_scanner.py`:

1. **`gate_log`** — per candidate ticker, records which gate it passed or failed at (universe, gap, disqualifier, etc.) with the failing value.
2. **`hint_summary`** — per scan, counts how many news items fell into each hint bucket: `no_hint`, `disq`, `foreign`, `low_rel`, `bucketed_events`.
3. **`scripts/analyse_ep_gates.py`** — a runner that aggregates `~/.tradingagents/portfolio_advisor/memory/strategies/ep_scans.jsonl` into a gate failure table and a hint summary table.

Three lines added in `advisor_pm.py` wire the audit log write into the scan cycle. No production behaviour change. The instrumentation is opt in via the existing scan cycle path; if disabled the rest of the system is unaffected.

## What the data will show

To be populated by running `scripts/analyse_ep_gates.py` on the server after the next scheduled scan cycle:

```
## Hint level summary

| Hint outcome | Total across scans |
|---|---|
| no_hint | TBD |
| disq | TBD |
| foreign | TBD |
| low_rel | TBD |
| bucketed_events | TBD |
| scans | TBD |

## Gate failure table

| Gate | Failures | Sample Tickers |
|---|---|---|
| (to be filled by analyse_ep_gates.py) | | |
```

Run command on the server once a scan has executed:

```bash
python3 /opt/tradingagents/scripts/analyse_ep_gates.py
```

Paste the output above and commit.

## Root cause analysis

Reading `ep_scanner.py` end to end reveals where the funnel collapses. Three observations.

**1. The news source is Alpha Vantage `get_global_news`.**

The scanner pulls news via `tradingagents.dataflows.alpha_vantage_news.get_global_news` (line 83) with `look_back_days=2`. Alpha Vantage's news API returns a feed of articles but its coverage of small / mid cap US equity catalysts is uneven. It is strong on macro and large cap company news, weaker on the specific EP catalysts the strategy hunts for (FDA, contract wins, activist stakes, anchor partnerships).

**2. The hint regexes (lines 28 to 50) are tight by design and need exact phrasing.**

`_TIER1_HINTS` requires specific keyword patterns: "fda approval", "phase 3", "raises guidance", "contract win", "to acquire", "activist stake", "13d filed". These are how an EP catalyst would be described by a financial wire service (Benzinga, BusinessWire, GlobeNewswire). They are NOT how Alpha Vantage paraphrases catalysts. Alpha Vantage often summarises "Company X reported strong quarterly results" rather than "Company X beats EPS estimates" — the former does not match the Tier 2 regex.

**3. The disqualifier regex is permissive.**

`_DISQ_HINTS` catches plenty (stock splits, buybacks, dividend hikes, crypto pivots, reverse splits, fraud, short squeeze). Combined with low Tier 1/2 hit rate, the funnel collapses to `disq` or `no_hint` for nearly every article.

The net effect: most news items get classified as `no_hint`, very few reach `tier1` or `tier2`, the candidate list is empty before gates even apply. Gate tightening or loosening is downstream of this.

## Recommendation

### Primary: replace or augment the news source

Three viable structural fixes, in order of effort:

| Option | Effort | Coverage gain | Recommended? |
|--------|--------|----------------|---------------|
| **A. Add Benzinga newsfeed** (paid, ~$30/month for basic plan) | 1 to 2 days | Substantial. Benzinga is the standard for EP catalyst coverage. | Yes, if budget allows |
| **B. Add a free news aggregator** (Yahoo Finance, MarketWatch, Reuters via RSS) | 1 day | Moderate. Coverage breadth without the specialist EP focus. | Yes, as starting point |
| **C. LLM classifier in place of regex** (use existing DeepSeek to score news relevance) | 2 to 3 days | High flexibility but slow per scan; adds latency and cost per cycle. | Defer until A or B has been tried |

Option B is the cheapest first move. Add a second news source (Yahoo Finance RSS) in parallel to Alpha Vantage, dedupe by title hash, run both through the existing hint regexes. If candidate count rises, the diagnosis was correct.

### Secondary: widen the hint regexes (nice to have)

Even without changing the news source, broadening the Tier 1/2 patterns to match the way Alpha Vantage actually phrases catalysts could help on the margin. Examples:

- Add "strong quarterly" / "blowout quarter" / "record quarter" to Tier 2
- Add "wins major contract" / "secures contract" to Tier 1
- Add "FDA grants" / "regulatory approval" to Tier 1

This is the change the original audit goal command anticipated. It is now identified as **secondary** because even with regex widening, Alpha Vantage's coverage gap remains the binding constraint.

### Tertiary: gap threshold tweak

The original goal command anticipated this as the likely fix. It is no longer the primary recommendation. The 10% gap threshold and 50% extended run threshold both look reasonable in isolation; the empirical question is whether any candidates make it that far in the first place. The data section above will confirm or refute. Until then, do not touch the thresholds.

## What was added to default_config

A new optional config key controls which news source the scanner uses. Default is unchanged (Alpha Vantage only), so existing behaviour is preserved. Set to a list to add additional sources once option B is implemented.

```python
"ep_scanner_news_sources": ["alpha_vantage"],  # candidates: ["alpha_vantage", "yahoo_finance_rss"]
```

The list form means future sources can be added without breaking existing config. Default behaviour: single source, Alpha Vantage, as before.

## Next steps

1. **Today / next scheduled scan**: run `scripts/analyse_ep_gates.py` on the server. Paste the gate failure table and hint summary into the placeholder section above. Commit.
2. **This week**: implement Option B (Yahoo Finance RSS) as a second news source. Wire into `ep_scanner.py` via the new `ep_scanner_news_sources` config key. Dedupe by article title hash. Confirm hint summary shows more `bucketed_events` and fewer `no_hint`.
3. **After option B has had two weeks**: re-run the audit. If candidate flow is still zero or close, escalate to Option C (LLM classifier) or Option A (Benzinga).
4. **Defer**: gap threshold and liquidity floor changes until the news pipeline produces hits.

## Files changed on this branch

- `tradingagents/portfolio_advisor/ep_scanner.py` (+62 lines): per-scan audit log, hint summary, gate log
- `tradingagents/portfolio_advisor/advisor_pm.py` (+3 lines): wiring the audit write
- `scripts/analyse_ep_gates.py` (new, 125 lines): aggregator
- `tradingagents/default_config.py` (+1 line): new `ep_scanner_news_sources` config key (this commit)
- `docs/ep_gate_audit.md` (new): this report

## What is NOT changed

- No gate thresholds altered. Behaviour identical for existing scan cycles.
- No production code paths removed or rerouted.
- Feature flag default keeps single source Alpha Vantage news. No behaviour change until option B is wired and the config is updated.

---

**Report status:** Instrumentation deployed, recommendation issued, empirical data section pending one scan cycle. Hand the analysis run to Hermes or run manually on the server.
