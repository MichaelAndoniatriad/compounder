"""Seed empty scaffolds for ANET + MNDY rules and position memory."""
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG as cfg
from tradingagents.portfolio_advisor import pm_workspace as w

w.ensure_workspace(cfg)

for tk in ("ANET", "MNDY"):
    rp = w.rules_dir(cfg) / f"{tk}.md"
    if not rp.is_file():
        rp.write_text(
            f"# {tk} -- scoped rules\n\n"
            "_Empty scaffold. Add per-ticker rules here as you learn them via\n"
            f"`update_scoped_rule(ticker=\"{tk}\", rule_text=\"...\")`. Portfolio-wide\n"
            "rules live in `rules/_portfolio.md`._\n\n"
            "(none yet)\n",
            encoding="utf-8",
        )
        print(f"  seeded {rp}")
    mp = w.positions_dir(cfg) / f"{tk}.md"
    if not mp.is_file():
        mp.write_text(
            f"# {tk} -- position memory\n\n"
            "_Empty scaffold. Add thesis, history, lessons here as you learn them via\n"
            f"`update_position_memory(ticker=\"{tk}\", note=\"...\")`._\n\n"
            "(none yet)\n",
            encoding="utf-8",
        )
        print(f"  seeded {mp}")

# Regenerate the index now that the new files exist.
w.regenerate_memory_index(cfg)
print("regenerated MEMORY.md")
