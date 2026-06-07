#!/usr/bin/env python3
"""One-shot backfill: map proposed_trades.jsonl entries into recommendation_log.jsonl.

Idempotent — checks whether each proposal's (ts, ticker, action) tuple already
exists in the recommendation log before appending. Run once on the server.

Usage:
  python3 scripts/backfill_proposed_trades_to_rec_log.py [--dry-run]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

# ── paths ────────────────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
PROPOSED = HOME / ".tradingagents" / "portfolio_advisor" / "proposed_trades.jsonl"
REC_LOG = HOME / ".tradingagents" / "portfolio_advisor" / "recommendation_log.jsonl"

# ── helpers ──────────────────────────────────────────────────────────────────

def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def existing_keys(rec_path: Path) -> Set[Tuple[str, str, str]]:
    """Return set of (ts, ticker, action) already in the recommendation log."""
    keys: Set[Tuple[str, str, str]] = set()
    for row in load_jsonl(rec_path):
        ts = str(row.get("ts", ""))
        tk = str(row.get("ticker", "")).upper()
        act = str(row.get("action", "")).lower()
        if ts and tk and act:
            keys.add((ts, tk, act))
    return keys


def map_proposal_to_recommendation(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Map a proposed_trades.jsonl row to a recommendation_log.jsonl row."""
    import uuid

    tk = str(entry.get("ticker", "")).strip().upper()
    act = str(entry.get("action", "")).strip().lower()
    reason = str(entry.get("reason", "")).strip()
    target_price = float(entry.get("target_price", 0) or 0)
    shares = float(entry.get("shares", 0) or 0)
    sleeve = str(entry.get("sleeve", "")).strip().lower()
    ts = str(entry.get("ts", ""))

    sleeve_map = {"core": "core", "catalyst": "catalyst"}
    rule_ref = sleeve_map.get(sleeve)

    return {
        "id": uuid.uuid4().hex[:16],
        "ts": ts,
        "trigger": "action_check",
        "type": "trade_proposal",
        "ticker": tk if tk else None,
        "action": act,
        "rationale": reason[:600],
        "rule_ref": rule_ref,
        "entry_price": round(target_price, 2) if target_price > 0 else None,
        "stop_price": None,
        "shares": round(shares, 4) if shares > 0 else None,
        "status": "pending",
        "human_response": None,
        "outcome_measured_at": None,
        "was_correct": None,
        "pnl_impact_est": None,
        "outcome_note": None,
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    dry_run = "--dry-run" in sys.argv

    if not PROPOSED.is_file():
        print(f"Source file not found: {PROPOSED}")
        return 1

    proposals = load_jsonl(PROPOSED)
    print(f"Read {len(proposals)} entries from {PROPOSED}")

    keys = existing_keys(REC_LOG)
    print(f"Found {len(keys)} existing entries in {REC_LOG}")

    new_entries = []
    skipped = 0
    for entry in proposals:
        ts = str(entry.get("ts", ""))
        tk = str(entry.get("ticker", "")).upper()
        act = str(entry.get("action", "")).lower()
        if not ts or not tk or not act:
            skipped += 1
            continue
        if (ts, tk, act) in keys:
            skipped += 1
            continue
        new_entries.append(map_proposal_to_recommendation(entry))

    print(f"New entries to backfill: {len(new_entries)}  (skipped: {skipped})")

    if not new_entries:
        print("Nothing to backfill.")
        return 0

    if dry_run:
        print("\nDRY RUN — first 3 entries that would be written:")
        for e in new_entries[:3]:
            print(json.dumps(e, indent=2))
        return 0

    REC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REC_LOG, "a", encoding="utf-8") as f:
        for entry in new_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Backfilled {len(new_entries)} entries to {REC_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
