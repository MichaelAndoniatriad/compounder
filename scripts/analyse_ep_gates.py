#!/usr/bin/env python3
"""Analyse EP scan audit logs and produce a gate failure table.

Reads ep_scans.jsonl (created by run_ep_scan_cycle), aggregates gate failure
counts across all scans, and emits a markdown table suitable for pasting into
the gate audit report.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Any


def load_scans(path: Path) -> List[Dict[str, Any]]:
    scans: List[Dict[str, Any]] = []
    if not path.is_file():
        return scans
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            scans.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return scans


def gate_failure_table(scans: List[Dict[str, Any]]) -> str:
    """Build a markdown table: gate name | failure count | sample tickers."""
    gate_counts: Counter = Counter()
    gate_samples: Dict[str, List[str]] = {}
    passed: List[str] = []

    for scan in scans:
        gate_log = scan.get("gate_log") or []
        for entry in gate_log:
            if entry.get("passed"):
                passed.append(entry.get("t", "?"))
                continue
            gate = entry.get("failed_at", "unknown")
            gate_counts[gate] += 1
            ticker = entry.get("t", "?")
            if gate not in gate_samples:
                gate_samples[gate] = []
            if ticker not in gate_samples[gate]:
                gate_samples[gate].append(ticker)

    lines: List[str] = []
    lines.append("| Gate | Failures | Sample Tickers |")
    lines.append("|---|---|---|")
    for gate, count in gate_counts.most_common():
        samples = ", ".join(gate_samples[gate][:5])
        lines.append(f"| {gate} | {count} | {samples} |")
    if passed:
        lines.append(f"| **passed** | {len(passed)} | {', '.join(passed[:5])} |")
    if not gate_counts and not passed:
        lines.append("| *(no gate entries)* | 0 | |")

    return "\n".join(lines)


def hint_summary_table(scans: List[Dict[str, Any]]) -> str:
    """Summarise the hint-level filtering across scans."""
    totals: Counter = Counter()
    scan_count = 0
    for scan in scans:
        hs = scan.get("hint_summary") or {}
        if not hs:
            continue
        scan_count += 1
        for k, v in hs.items():
            totals[k] += v

    if not totals:
        return "No hint summary data available."

    lines: List[str] = []
    lines.append("| Hint outcome | Total across scans |")
    lines.append("|---|---|")
    for k in ["no_hint", "disq", "foreign", "low_rel", "bucketed_events"]:
        v = totals.get(k, 0)
        lines.append(f"| {k} | {v} |")
    lines.append(f"| **scans** | {scan_count} |")
    return "\n".join(lines)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".tradingagents" / "portfolio_advisor" / "memory" / "strategies" / "ep_scans.jsonl"
    if not path.is_file():
        print(f"File not found: {path}")
        sys.exit(1)

    scans = load_scans(path)
    print(f"## Hint-level summary\n\n{hint_summary_table(scans)}\n")
    print(f"## Gate failure table\n\n{gate_failure_table(scans)}\n")

    # Print per-scan detail for report context.
    print("## Per-scan detail\n")
    for i, scan in enumerate(scans, 1):
        ts = scan.get("scanned_at", "?")[:19]
        mode = scan.get("scan_mode", "pre_market")
        news = scan.get("news_items", 0)
        hits = scan.get("ticker_hits", 0)
        mkt = scan.get("market") or {}
        blocked = mkt.get("blocked", False)
        cands = scan.get("candidates") or []
        skipped = scan.get("skipped") or []
        print(f"### Scan {i} — {ts} ({mode})")
        print(f"- News items: {news}, ticker hits: {hits}")
        if blocked:
            print(f"- Market blocked: {mkt.get('reason', '?')}")
        if cands:
            print(f"- Candidates passed: {', '.join(cands)}")
        if skipped:
            for s in skipped[:10]:
                print(f"- Skipped: **{s['t']}** — {s['r']}")
        print()


if __name__ == "__main__":
    main()
