"""One-time migration: seed the PM WAT workspace from existing flat files.

Idempotent-ish: rule/memory files are (re)written from source; decisions are
left untouched if they already exist. Safe to re-run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG as cfg
from tradingagents.portfolio_advisor import pm_workspace as w
from tradingagents.portfolio_advisor import state as pa_state

MEM = pa_state.advisor_dir(cfg).parent / "memory"  # ~/.tradingagents/memory
LEARNED = MEM / "learned_rules.md"
LESSONS = MEM / "decision_lessons.jsonl"
PM_MEMORY = pa_state.advisor_dir(cfg) / "PM_MEMORY.md"

w.ensure_workspace(cfg)

# --- 1) rules/_portfolio.md : curated portfolio-wide ruleset ----------------
portfolio_rules = """# Portfolio-wide rules

These are the standing, portfolio-level rules. Per-ticker overrides live in
`rules/<TICKER>.md`. Global voice/behaviour lives in `PM_CLAUDE.md`.

## Sleeve targets
- Target mix: 50% core (long-term growth) / 40% catalyst (event-driven) / 10% cash.
- When catalyst is at 0% and cash > $500, deploy a starter — do not wait for a perfect standalone Buy.

## Stops & trims (only when the human has set them per position)
- Core: staged entry; +15% pre-earnings trim; -30% review / -40% hard exit; thesis-break exit.
- Catalyst: single entry before a dated catalyst; -8% hard stop; trailing stop after +10%;
  time-stop if the catalyst passes without the move.
- Double-from-entry: when a position doubles, sell half to lock recovered capital, let the rest run.

## Discipline
- Never invent a stop, deadline, or trim the human has not stated.
- Express every trim/close as exact shares AND dollars from the live snapshot.
"""
w.portfolio_rules_path(cfg).write_text(portfolio_rules, encoding="utf-8")
print("wrote", w.portfolio_rules_path(cfg))

# --- 2) rules/<TICKER>.md : from learned_rules.md ---------------------------
by_ticker: dict[str, list[str]] = {}
if LEARNED.is_file():
    cur = None
    hdr = re.compile(r"^###\s+\S+\s+[—-]\s+([A-Z]{1,6})\b")
    for line in LEARNED.read_text(encoding="utf-8").splitlines():
        m = hdr.match(line.strip())
        if m:
            cur = m.group(1)
            by_ticker.setdefault(cur, [])
        elif cur and line.strip().startswith("- "):
            rule = line.strip()[2:].strip()
            if rule and rule not in by_ticker[cur]:
                by_ticker[cur].append(rule)

for t, rules in sorted(by_ticker.items()):
    p = w.rules_dir(cfg) / f"{t}.md"
    body = [f"# {t} — scoped rules", "", "_Migrated from learned_rules.md._", ""]
    for r in rules:
        body.append(f"- {r}")
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
print(f"wrote {len(by_ticker)} per-ticker rule files:", ", ".join(sorted(by_ticker)))

# --- 3) memory/positions/<TICKER>.md : from decision_lessons.jsonl ----------
lessons_by_ticker: dict[str, list[dict]] = {}
if LESSONS.is_file():
    for line in LESSONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        tk = w._safe_ticker(row.get("ticker"))
        if tk:
            lessons_by_ticker.setdefault(tk, []).append(row)

for t, rows in sorted(lessons_by_ticker.items()):
    p = w.positions_dir(cfg) / f"{t}.md"
    body = [f"# {t} — position memory", "", "_Seeded from decision lessons._", ""]
    for r in sorted(rows, key=lambda x: str(x.get("date") or "")):
        q = r.get("outcome_quality", "")
        body.append(f"- {r.get('date','')}: [{q}] {r.get('pnl_description','')} — {r.get('lesson','')}")
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
print(f"wrote {len(lessons_by_ticker)} per-position memory files:", ", ".join(sorted(lessons_by_ticker)))

# --- 4) memory/MEMORY.md : the index ----------------------------------------
# Carry forward the single most recent PM_MEMORY.md note as a state snapshot.
last_snapshot = ""
if PM_MEMORY.is_file():
    chunks = [c.strip() for c in PM_MEMORY.read_text(encoding="utf-8").split("\n---\n") if c.strip()]
    if chunks:
        last_snapshot = chunks[-1]

rule_files = sorted(p.stem for p in w.rules_dir(cfg).glob("*.md") if not p.stem.startswith("_"))
pos_files = sorted(p.stem for p in w.positions_dir(cfg).glob("*.md"))
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
index = [
    "# PM memory index",
    "",
    f"_Last consolidated: {today}_",
    "",
    "## How this is organised",
    "- `../PM_CLAUDE.md` — global standing rules (always loaded).",
    "- `../rules/_portfolio.md` — portfolio-wide rules. `../rules/<TICKER>.md` — per-ticker rules.",
    "- `positions/<TICKER>.md` — per-position memory (thesis, history, lessons).",
    "- `decisions.md` / `decisions.jsonl` — what the human chose on PM recommendations.",
    "",
    f"## Per-ticker rules on file\n{', '.join(rule_files) or '(none)'}",
    "",
    f"## Per-position memory on file\n{', '.join(pos_files) or '(none)'}",
    "",
    "## Last working-memory snapshot (migrated)",
    last_snapshot or "(none)",
    "",
]
w.memory_index_path(cfg).write_text("\n".join(index), encoding="utf-8")
print("wrote", w.memory_index_path(cfg))

# --- 5) decisions : initialise empty if absent ------------------------------
if not w.decisions_jsonl_path(cfg).is_file():
    w._save_decisions(cfg, [])
    print("initialised empty decisions store")
else:
    print("decisions store already exists — left untouched")

print("MIGRATION DONE")
