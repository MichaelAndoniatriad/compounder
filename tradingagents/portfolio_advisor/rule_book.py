"""Evolving PM rule book — heuristics the PM learns from its own decisions.

PM_RULES.md is NOT static instructions (that's PM_CLAUDE.md). It is a living
document the PM writes to as it accumulates trading experience:
  - patterns it has observed across multiple cycles
  - rules it derived from outcomes (right/wrong calls)
  - confidence updates as evidence accumulates

The PM calls update_pm_rule (via pm_tools) to add/confirm/violate/revise rules.
Every PM cycle reads the rule book and injects it into the prompt so prior
experience shapes current reasoning.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SEPARATOR = "\n---\n"


def _rules_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("pm_rules_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "PM_RULES.md"


def _lessons_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("pm_lessons_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    mem = cfg.get("memory_log_path")
    if isinstance(mem, str) and mem.strip():
        return Path(mem).expanduser().parent / "decision_lessons.jsonl"
    return Path("~/.tradingagents/memory/decision_lessons.jsonl").expanduser()


# ---------------------------------------------------------------------------
# Rule book: read / write
# ---------------------------------------------------------------------------

def load_rule_book_text(cfg: Dict[str, Any]) -> str:
    p = _rules_path(cfg)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_rules(text: str) -> Dict[str, Dict[str, Any]]:
    """Parse PM_RULES.md into {rule_name: {fields...}}."""
    rules: Dict[str, Dict[str, Any]] = {}
    if not text:
        return rules
    for block in re.split(r"\n---\n", text):
        block = block.strip()
        if not block:
            continue
        name_m = re.search(r"##\s+Rule:\s*(.+)", block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        rule: Dict[str, Any] = {"name": name, "_raw": block}
        for field in ("Source", "Confidence", "Added", "Updated", "Confirmed", "Violated"):
            m = re.search(rf"^{field}:\s*(.+)$", block, re.MULTILINE)
            if m:
                rule[field.lower()] = m.group(1).strip()
        rules[name] = rule
    return rules


def _build_rule_block(
    name: str,
    pattern: str,
    rule_text: str,
    confidence: str,
    source: str,
    confirmed: int,
    violated: int,
    added: str,
    updated: str,
    evidence_notes: List[str],
) -> str:
    header = f"## Rule: {name}"
    meta = (
        f"Source: {source}\n"
        f"Confidence: {confidence}\n"
        f"Added: {added}\n"
        f"Updated: {updated}\n"
        f"Confirmed: {confirmed}\n"
        f"Violated: {violated}"
    )
    body = f"Pattern: {pattern}\n\nRule: {rule_text}"
    if evidence_notes:
        body += "\n\nEvidence:\n" + "\n".join(f"- {n}" for n in evidence_notes[-6:])
    return f"{header}\n{meta}\n\n{body}"


def add_rule(
    cfg: Dict[str, Any],
    *,
    name: str,
    pattern: str,
    rule_text: str,
    confidence: str = "emerging",
    source: str = "pm-learned",
    evidence_note: str = "",
) -> str:
    """Add a new rule to PM_RULES.md. Returns status string."""
    name = name.strip()
    if not name or not pattern.strip() or not rule_text.strip():
        return "error: name, pattern, and rule_text are all required"
    p = _rules_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_rule_book_text(cfg)
    rules = _parse_rules(existing)
    if name in rules:
        return f"rule '{name}' already exists — use action=update to modify it"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block = _build_rule_block(
        name=name, pattern=pattern, rule_text=rule_text,
        confidence=confidence, source=source,
        confirmed=0, violated=0,
        added=today, updated=today,
        evidence_notes=[evidence_note] if evidence_note else [],
    )
    new_text = (existing + _SEPARATOR + block) if existing else block
    p.write_text(new_text, encoding="utf-8")
    return f"rule '{name}' added (confidence={confidence})"


def update_rule(
    cfg: Dict[str, Any],
    *,
    name: str,
    action: str,
    pattern: str = "",
    rule_text: str = "",
    confidence: str = "",
    evidence_note: str = "",
) -> str:
    """Update an existing rule.

    action: confirm | violate | revise | retire
    confirm — increments confirmed count, optionally updates confidence
    violate — increments violated count, optionally downgrades confidence
    revise — rewrites pattern / rule_text / confidence
    retire — marks confidence=retired
    """
    name = name.strip()
    action = (action or "").strip().lower()
    if not name:
        return "error: name is required"
    p = _rules_path(cfg)
    existing = load_rule_book_text(cfg)
    rules = _parse_rules(existing)
    if name not in rules:
        return f"rule '{name}' not found — use action=add to create it"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rule = rules[name]
    confirmed = int(rule.get("confirmed", 0) if str(rule.get("confirmed", 0)).isdigit() else 0)
    violated = int(rule.get("violated", 0) if str(rule.get("violated", 0)).isdigit() else 0)
    cur_confidence = rule.get("confidence", "emerging")
    cur_pattern = ""
    cur_rule_text = ""
    raw = rule.get("_raw", "")
    pm = re.search(r"^Pattern:\s*(.+)$", raw, re.MULTILINE)
    if pm:
        cur_pattern = pm.group(1).strip()
    rm = re.search(r"^Rule:\s*(.+?)(?=\n\n|\Z)", raw, re.DOTALL | re.MULTILINE)
    if rm:
        cur_rule_text = rm.group(1).strip()

    ev_notes: List[str] = []
    em = re.search(r"^Evidence:\n(.*?)(?=\Z)", raw, re.DOTALL | re.MULTILINE)
    if em:
        ev_notes = [ln.lstrip("- ").strip() for ln in em.group(1).splitlines() if ln.strip()]

    if action == "confirm":
        confirmed += 1
        if evidence_note:
            ev_notes.append(f"[confirmed {today}] {evidence_note}")
        if confidence:
            cur_confidence = confidence
        elif confirmed >= 3 and cur_confidence == "emerging":
            cur_confidence = "medium"
        elif confirmed >= 6 and cur_confidence == "medium":
            cur_confidence = "high"
    elif action == "violate":
        violated += 1
        if evidence_note:
            ev_notes.append(f"[violated {today}] {evidence_note}")
        if confidence:
            cur_confidence = confidence
        elif violated > confirmed and cur_confidence == "high":
            cur_confidence = "medium"
        elif violated >= 3 and cur_confidence == "emerging":
            cur_confidence = "weak"
    elif action == "revise":
        if pattern.strip():
            cur_pattern = pattern.strip()
        if rule_text.strip():
            cur_rule_text = rule_text.strip()
        if confidence.strip():
            cur_confidence = confidence.strip()
        if evidence_note:
            ev_notes.append(f"[revised {today}] {evidence_note}")
    elif action == "retire":
        cur_confidence = "retired"
        if evidence_note:
            ev_notes.append(f"[retired {today}] {evidence_note}")
    else:
        return f"unknown action '{action}' — use confirm | violate | revise | retire"

    new_block = _build_rule_block(
        name=name,
        pattern=cur_pattern, rule_text=cur_rule_text,
        confidence=cur_confidence, source=rule.get("source", "pm-learned"),
        confirmed=confirmed, violated=violated,
        added=rule.get("added", today), updated=today,
        evidence_notes=ev_notes,
    )
    # Replace old block in the full text
    blocks = re.split(r"\n---\n", existing)
    new_blocks = []
    replaced = False
    for blk in blocks:
        nm = re.search(r"##\s+Rule:\s*(.+)", blk)
        if nm and nm.group(1).strip() == name:
            new_blocks.append(new_block)
            replaced = True
        else:
            new_blocks.append(blk)
    if not replaced:
        new_blocks.append(new_block)
    p.write_text(_SEPARATOR.join(new_blocks), encoding="utf-8")
    return f"rule '{name}' {action}d (confidence={cur_confidence}, confirmed={confirmed}, violated={violated})"


def auto_retire_failed_rules(
    cfg: Dict[str, Any],
    *,
    min_violations: int = 3,
    violation_to_confirmation_ratio: float = 2.0,
) -> List[str]:
    """Auto retire rules where evidence clearly contradicts them.

    A rule is retired when:
    - violated >= min_violations (default 3), AND
    - violated >= confirmed * violation_to_confirmation_ratio (default 2x),
      meaning the rule has been contradicted at least twice as often as it
      has been confirmed.

    Already retired rules are skipped. Returns the list of rule names retired
    in this pass.

    Intended for weekly cron invocation; idempotent.
    """
    text = load_rule_book_text(cfg)
    if not text:
        return []
    rules = _parse_rules(text)
    if not rules:
        return []

    retired_names: List[str] = []
    for name, rule in rules.items():
        current_confidence = (rule.get("confidence") or "").strip().lower()
        if current_confidence == "retired":
            continue
        try:
            confirmed = int(rule.get("confirmed", 0))
        except (TypeError, ValueError):
            confirmed = 0
        try:
            violated = int(rule.get("violated", 0))
        except (TypeError, ValueError):
            violated = 0

        if violated < min_violations:
            continue
        threshold = max(confirmed * violation_to_confirmation_ratio, 1)
        if violated < threshold:
            continue

        update_rule(
            cfg,
            name=name,
            action="retire",
            evidence_note=(
                f"auto retired: {violated} violations vs {confirmed} confirmations "
                f"(threshold {violation_to_confirmation_ratio}x)"
            ),
        )
        retired_names.append(name)
        logger.info("rule_book: auto retired rule '%s'", name)

    return retired_names


def recently_retired_block(cfg: Dict[str, Any], lookback_days: int = 14) -> str:
    """PM prompt context block listing rules retired in the last N days.

    Empty string if none. Helps the PM understand why a rule it might recall
    is no longer active.
    """
    from datetime import timedelta
    text = load_rule_book_text(cfg)
    if not text:
        return ""
    rules = _parse_rules(text)
    if not rules:
        return ""

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    recent_retired: List[Dict[str, Any]] = []
    for rule in rules.values():
        if (rule.get("confidence") or "").strip().lower() != "retired":
            continue
        updated = rule.get("updated") or ""
        if updated < cutoff:
            continue
        recent_retired.append(rule)

    if not recent_retired:
        return ""

    lines = [f"Recently retired rules (last {lookback_days}d — do not rely on these):"]
    for r in recent_retired[:6]:
        confirmed = r.get("confirmed", "0")
        violated = r.get("violated", "0")
        lines.append(f"  {r['name']}: confirmed={confirmed} violated={violated} updated={r.get('updated', '?')}")
    lines.append("")
    return "\n".join(lines)


def build_rule_book_prompt_block(cfg: Dict[str, Any]) -> str:
    """Compact rule book for PM prompt — active rules only, newest evidence last."""
    text = load_rule_book_text(cfg)
    if not text:
        return ""
    rules = _parse_rules(text)
    if not rules:
        return ""
    active = [r for r in rules.values() if r.get("confidence", "") != "retired"]
    if not active:
        return ""
    lines = [f"PM rule book ({len(active)} active rules — derived from your own decisions):"]
    for r in active:
        name = r["name"]
        conf = r.get("confidence", "emerging")
        confirmed = r.get("confirmed", "0")
        violated = r.get("violated", "0")
        raw = r.get("_raw", "")
        pm = re.search(r"^Pattern:\s*(.+)$", raw, re.MULTILINE)
        rl = re.search(r"^Rule:\s*(.+?)(?=\n\n|\Z)", raw, re.DOTALL | re.MULTILINE)
        pat = pm.group(1).strip()[:120] if pm else "?"
        rul = rl.group(1).strip().replace("\n", " ")[:160] if rl else "?"
        lines.append(f"  [{conf}|{confirmed}✓{violated}✗] {name}: when {pat} → {rul}")
    lines.append("")
    return "\n".join(lines)


def auto_retire_failed_rules(
    cfg: Dict[str, Any],
    *,
    min_violations: int = 3,
    violation_to_confirmation_ratio: float = 2.0,
) -> List[str]:
    """Retire rules where violations dominate confirmations.

    A rule is retired when ALL of these hold:
    - violations >= min_violations
    - violations / max(confirmed, 1) >= violation_to_confirmation_ratio
    - confidence is NOT already "retired"

    Returns the list of rule names that were retired. Idempotent.
    """
    existing = load_rule_book_text(cfg)
    rules = _parse_rules(existing)
    retired: List[str] = []
    for name, rule in rules.items():
        if rule.get("confidence", "") == "retired":
            continue
        confirmed = int(rule.get("confirmed", 0)) if str(rule.get("confirmed", "0")).isdigit() else 0
        violated = int(rule.get("violated", 0)) if str(rule.get("violated", "0")).isdigit() else 0
        if violated < min_violations:
            continue
        ratio = violated / max(confirmed, 1)
        if ratio >= violation_to_confirmation_ratio:
            update_rule(
                cfg, name=name, action="retire",
                evidence_note=f"auto-retired: {violated}v vs {confirmed}c (ratio={ratio:.1f})",
            )
            retired.append(name)
    return retired


def recently_retired_block(cfg: Dict[str, Any], *, lookback_days: int = 14) -> str:
    """Return a compact text block listing rules retired recently.

    Empty string if no rules were retired within lookback_days.
    """
    existing = load_rule_book_text(cfg)
    if not existing:
        return ""
    rules = _parse_rules(existing)
    if not rules:
        return ""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    recent: List[Dict[str, Any]] = []
    for name, rule in rules.items():
        if rule.get("confidence", "") != "retired":
            continue
        raw = rule.get("_raw", "")
        # Find the most recent retirement date from evidence notes
        m = re.findall(r"\[retired (\d{4}-\d{2}-\d{2})\]", raw)
        if not m:
            continue
        last_retired = max(m)
        if last_retired >= cutoff:
            recent.append({"name": name, "date": last_retired})
    if not recent:
        return ""
    recent.sort(key=lambda r: r["date"], reverse=True)
    lines = [f"Recently retired rules (last {lookback_days}d):"]
    for r in recent:
        lines.append(f"  [{r['date']}] {r['name']} — auto-retired due to excess violations")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decision lessons log
# ---------------------------------------------------------------------------

def append_lesson(cfg: Dict[str, Any], lesson: Dict[str, Any]) -> str:
    """Write a decision lesson to decision_lessons.jsonl."""
    import uuid
    p = _lessons_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": lesson.get("id") or uuid.uuid4().hex[:16],
        "date": lesson.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "ticker": (lesson.get("ticker") or "").strip().upper(),
        "stance_was": (lesson.get("stance_was") or "").strip().lower(),
        "outcome_quality": (lesson.get("outcome_quality") or "").strip().lower(),
        "pnl_description": (lesson.get("pnl_description") or "").strip()[:200],
        "lesson": (lesson.get("lesson") or "").strip()[:500],
        "pattern_tags": lesson.get("pattern_tags") or [],
        "rule_updated": lesson.get("rule_updated") or None,
    }
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def load_recent_lessons(cfg: Dict[str, Any], days: int = 180) -> List[Dict[str, Any]]:
    from datetime import timedelta
    p = _lessons_path(cfg)
    if not p.is_file():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("date") or "") >= cutoff:
                out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return out


def build_lessons_prompt_block(cfg: Dict[str, Any], days: int = 90) -> str:
    """Recent decision lessons for PM prompt — what the PM learned from past calls."""
    lessons = load_recent_lessons(cfg, days=days)
    if not lessons:
        return ""
    lines = [f"Recent decision lessons (last {days}d — your own retrospectives):"]
    for ls in lessons[:8]:
        date = ls.get("date", "?")
        ticker = ls.get("ticker", "?")
        stance = ls.get("stance_was", "?")
        quality = ls.get("outcome_quality", "?")
        lesson = (ls.get("lesson") or "").strip()
        tags = ", ".join(ls.get("pattern_tags") or [])
        line = f"{date} {ticker} [{stance}→{quality}]: {lesson}"
        if tags:
            line += f" | tags: {tags}"
        lines.append(f"  {line}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outcome digest — surfaces recent outcome_recorded events for PM review
# ---------------------------------------------------------------------------

def build_outcomes_to_review_block(cfg: Dict[str, Any]) -> str:
    """Surface recent outcome_recorded events so the PM can extract lessons.

    Shows the original decision rating and the outcome alignment so the PM
    can call log_decision_lesson and update_pm_rule when patterns are clear.
    """
    try:
        from datetime import timedelta
        from tradingagents.agents.utils.event_log import _iter_events
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        outcomes: List[Dict[str, Any]] = []
        for row in _iter_events(cfg, max_lines=8000):
            ts = str(row.get("timestamp") or "")
            if ts < cutoff:
                continue
            if str(row.get("event_type") or "") != "outcome_recorded":
                continue
            kd = row.get("key_data") or {}
            outcomes.append({
                "ticker": str(row.get("ticker") or kd.get("ticker") or "?").strip().upper(),
                "date": ts[:10],
                "decision": str(kd.get("decision_was") or "?"),
                "alignment": str(row.get("outcome") or kd.get("outcome_alignment") or "?"),
                "pnl_pct": kd.get("pnl_pct"),
                "holding_days": kd.get("holding_days"),
            })
        if not outcomes:
            return ""
        lines = ["Recent closed-position outcomes (extract lessons if you haven't yet):"]
        for o in outcomes[-8:]:
            pnl = f"{float(o['pnl_pct']):+.1f}%" if o.get("pnl_pct") is not None else "n/a"
            days_held = f"{o['holding_days']}d" if o.get("holding_days") else "?"
            align = o["alignment"]
            lines.append(
                f"  {o['date']} {o['ticker']}: was {o['decision']}, "
                f"outcome {align} ({pnl} over {days_held}) — "
                f"call log_decision_lesson if you haven't logged a lesson for this yet"
            )
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("build_outcomes_to_review_block failed: %s", e)
        return ""
