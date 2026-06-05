"""Macro learning review — extracts durable portfolio rules, validates existing
rules, computes risk scores, and triggers pre-event alerts from market memory.

Runs weekly (Saturday) or on-demand. Core functions:
  - run_macro_learning_review: extract new rules from 90d events
  - run_rule_invalidation: check if existing rules failed against recent events
  - compute_macro_risk_score: 0-10 risk score from event recency and magnitude
  - check_pre_event_alert: match a new event against existing rules, return alert
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.portfolio_advisor.market_memory import (
    build_market_memory_block,
    load_recent_market_events,
)
from tradingagents.portfolio_advisor.pm_workspace import portfolio_rules_path

logger = logging.getLogger(__name__)

_MACRO_LEARNING_PROMPT = """You are a macro trading strategist reviewing recent market events
that impacted this portfolio. Your job: extract DUrable, reusable rules from
recurring patterns. Rules must be SPECIFIC and ACTIONABLE — not vague advice.

A good rule:
- Names the trigger condition precisely (e.g. "SPY down 2%+ pre-market on tariff news")
- Prescribes a concrete action (e.g. "halve all new EP entry sizes until VIX < 25")
- Has a clear expiry or review condition (e.g. "review after 30 days")
- References what happened last time (e.g. "Jun 3 tariff rout: NVDA -11% intraday, fully recovered in 2 sessions")

A bad rule:
- "Be careful during volatility" (too vague)
- "Consider reducing risk" (no action specified)
- "Tariffs are bad for tech" (descriptive, not prescriptive)

IMPORTANT — Deduplication:
For EACH rule you extract, assign a stable slug ID using this format:
  RULE_ID: <category>_<YYYY-MM-DD>_<short-hyphenated-description>
  Example: RULE_ID: macro_2026-06-05_tariff-panic-no-selling

If a rule ALREADY EXISTS in the existing rules below, do NOT re-extract it.
Instead say: DUPLICATE: <existing_rule_id>

Rules should be written in the style of the existing _portfolio.md document.
Below are the market events. Extract 0-3 rules maximum. If no clear pattern
exists, say "NO_RULES" and explain why."""

_INVALIDATION_PROMPT = """You are a macro strategist auditing existing portfolio rules
against recent market events. For each rule below, determine if recent events
SUPPORT, CHALLENGE, or INVALIDATE the rule.

- SUPPORT: recent events reinforced the rule's premise (e.g., another tariff panic followed by a bounce)
- CHALLENGE: a recent event partially contradicted the rule (e.g., the bounce was weaker or slower than expected)
- INVALIDATE: a recent event directly disproved the rule (e.g., the bounce never came, the pattern broke)

For each rule that is CHALLENGED or INVALIDATED, write a one-line revision or
a one-line flag with the date and what happened. Use this format for each:

  RULE: <first 80 chars of rule>
  VERDICT: SUPPORT | CHALLENGE | INVALIDATE
  NOTE: <one line explaining why, with date and data>

Rules that are SUPPORTED need no note. Do not rewrite supported rules.

If no rules need revision, say "ALL_RULES_INTACT"."""

_RISK_SCORE_PROMPT = """You are assessing macro risk for a tech-heavy long-only portfolio.
Given the recent market events below, output a single JSON line:

{"risk_score": <0-10>, "trend": "improving|stable|deteriorating", "primary_driver": "<short phrase>", "sizing_guidance": "<one sentence recommendation>", "ep_recommendation": "normal|caution|pause", "rationale": "<2 sentences max>"}

Scoring guide:
- 0-2: calm, no macro drivers active, VIX low
- 3-4: moderate risk, isolated events, portfolio impact muted
- 5-6: elevated risk, recurring macro pattern, some position drawdowns
- 7-8: high risk, active macro driver hitting positions hard, VIX elevated
- 9-10: crisis conditions, broad drawdowns, systemic risk

sizing_guidance should recommend: normal sizing, reduce new entries by X%, or pause all new entries.
ep_recommendation: normal (run EP strategy as usual), caution (only Tier 1, half size), or pause (no new EP entries)."""


# ——— public API ————————————————————————————————————————————————————————————

def run_macro_learning_review(cfg: Dict[str, Any]) -> str:
    """Extract new macro rules AND validate existing ones. Returns status."""
    events = load_recent_market_events(cfg, days=90)
    if len(events) < 3:
        return "macro learning: skipped — fewer than 3 market events in 90 days"

    memory_block = build_market_memory_block(cfg, days=90)
    if not memory_block.strip():
        return "macro learning: skipped — no market memory block generated"

    # Phase 1: extract new rules
    llm_result = _call_llm(cfg, _MACRO_LEARNING_PROMPT, memory_block)
    rules_msg = ""
    if llm_result and "NO_RULES" not in llm_result:
        n = _append_rules(cfg, llm_result)
        rules_msg = f"{n} new rule(s) written; " if n else ""

    # Phase 2: validate existing rules
    invalidation_msg = run_rule_invalidation(cfg)

    return f"macro learning: {rules_msg}{invalidation_msg}"


def run_rule_invalidation(cfg: Dict[str, Any]) -> str:
    """Check existing macro-learned rules against recent events. Returns status."""
    existing_rules = _load_existing_rules(cfg)
    if not existing_rules:
        return "no existing rules to validate"

    memory_block = build_market_memory_block(cfg, days=90)
    if not memory_block.strip():
        return "no market memory to validate against"

    prompt = (
        f"{_INVALIDATION_PROMPT}\n\n"
        f"=== EXISTING RULES ===\n{existing_rules}\n\n"
        f"=== RECENT MARKET EVENTS ===\n{memory_block}\n\n"
        f"Evaluate each rule:"
    )
    result = _call_llm(cfg, prompt, memory_block)
    if not result or "ALL_RULES_INTACT" in result:
        return "all rules intact"

    _flag_challenged_rules(cfg, result)
    challenged_count = result.count("VERDICT: CHALLENGE") + result.count("VERDICT: INVALIDATE")
    return f"{challenged_count} rule(s) flagged for review"


def compute_macro_risk_score(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a macro risk score from recent events. Returns dict with keys:
    risk_score (0-10), trend, primary_driver, sizing_guidance, ep_recommendation, rationale.
    """
    events = load_recent_market_events(cfg, days=30)
    if not events:
        return {
            "risk_score": 0, "trend": "stable", "primary_driver": "none",
            "sizing_guidance": "normal sizing", "ep_recommendation": "normal",
            "rationale": "no recent macro events",
        }

    memory_block = build_market_memory_block(cfg, days=30)
    result = _call_llm(cfg, _RISK_SCORE_PROMPT, memory_block)
    if not result:
        return _fallback_risk_score(events)

    # Parse JSON from the response
    try:
        # Find JSON in the response (may have markdown wrapping)
        json_match = re.search(r'\{[^}]+\}', result)
        if json_match:
            return json.loads(json_match.group())
    except (json.JSONDecodeError, ValueError):
        pass

    return _fallback_risk_score(events)


def check_pre_event_alert(
    cfg: Dict[str, Any], event_category: str, event_cause: str,
) -> Optional[str]:
    """Check if a new market event matches any existing macro-learned rule.
    Returns an alert string if a match is found, None otherwise."""
    existing_rules = _load_existing_rules(cfg)
    if not existing_rules or len(existing_rules) < 50:
        return None

    # Quick keyword match before calling LLM
    cause_lower = event_cause.lower()
    rule_keywords = ["tariff", "fed", "vix", "selloff", "rout", "panic", "bounce",
                     "relief", "hawkish", "dovish", "fomc", "geopolitical"]
    if not any(kw in cause_lower for kw in rule_keywords):
        return None

    # Build a quick prompt to match the event against existing rules
    prompt = (
        f"A new macro event just occurred: [{event_category}] {event_cause}\n\n"
        f"Existing macro-learned rules:\n{existing_rules}\n\n"
        f"If any rule directly applies to this event, output a ONE-LINE alert "
        f"the PM should send to the user right now. The alert should name the "
        f"rule and prescribe the action. If no rule applies, say NO_MATCH."
    )
    result = _call_llm(cfg, prompt, existing_rules)
    if result and "NO_MATCH" not in result and len(result.strip()) > 10:
        return result.strip()
    return None


def build_macro_risk_block(cfg: Dict[str, Any]) -> str:
    """Return a compact PM prompt block with current macro risk assessment."""
    score = compute_macro_risk_score(cfg)
    return (
        f"Macro risk: {score['risk_score']}/10 [{score['trend']}] — "
        f"{score['primary_driver']}\n"
        f"Sizing: {score['sizing_guidance']}\n"
        f"EP: {score['ep_recommendation']}\n"
        f"Rationale: {score['rationale']}\n"
    )


# ——— internal helpers ————————————————————————————————————————————————————————

def _call_llm(cfg: Dict[str, Any], system_prompt: str, user_content: str) -> Optional[str]:
    """Send a prompt to the LLM and return the response text."""
    try:
        from tradingagents.llm_clients.corporate_llm_factory import build_corporate_hierarchy_llms

        llms = build_corporate_hierarchy_llms(cfg, callbacks=[])
        llm = llms.get("reflection") or llms.get("market_analyst")
        if llm is None:
            logger.warning("macro: no LLM available from corporate hierarchy")
            return None

        prompt = f"{system_prompt}\n\n{user_content}"
        response = llm.invoke(prompt)
        content = getattr(response, "content", str(response))
        return str(content).strip()
    except Exception as e:
        logger.warning("macro: LLM call failed: %s", e)
        return None


def _load_existing_rules(cfg: Dict[str, Any]) -> str:
    """Load only the Macro-learned rules section from _portfolio.md."""
    path = portfolio_rules_path(cfg)
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8")
    # Extract the macro-learned rules section
    match = re.search(r'## Macro-learned rules\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
    return match.group(0) if match else ""


def _append_rules(cfg: Dict[str, Any], rules_text: str) -> int:
    """Append extracted rules to _portfolio.md. Skips duplicates by RULE_ID.
    Returns count of rules actually added."""
    if not rules_text or len(rules_text) < 10:
        return 0

    path = portfolio_rules_path(cfg)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Check for explicit duplicates the LLM flagged
    if "DUPLICATE:" in rules_text:
        dupes = [l for l in rules_text.split("\n") if "DUPLICATE:" in l]
        logger.info("macro dedup: LLM flagged %d duplicate(s): %s", len(dupes), dupes)

    if "## Macro-learned rules" not in existing:
        header = (
            "\n\n## Macro-learned rules\n\n"
            "Rules extracted from recurring macro patterns. DUrable: they "
            "persist across sessions and override general heuristics when "
            "the trigger condition matches. Rules may be flagged as CHALLENGED "
            "or INVALIDATED if subsequent events contradict them.\n\n"
        )
        separator = ""
    else:
        header = ""
        separator = "\n"

    lines = [l.strip() for l in rules_text.split("\n")]
    cleaned: List[str] = []
    current_rule_id = ""
    for l in lines:
        if not l or l.startswith("NO_RULES") or "DUPLICATE:" in l:
            continue
        if l.startswith("RULE_ID:"):
            rule_id = l[8:].strip()
            if rule_id and rule_id in existing:
                logger.info("macro dedup: skipping existing rule %s", rule_id)
                current_rule_id = ""
                continue
            current_rule_id = rule_id
            continue
        if current_rule_id and (l.startswith("-") or l.startswith("*")):
            # Compute confidence from supporting event count
            n = _count_supporting_events(l, cfg)
            conf = "high" if n >= 5 else ("medium" if n >= 3 else "low")
            tag = f"[{current_rule_id}] [n={n}, confidence: {conf}] "
            cleaned.append(tag + l)
            current_rule_id = ""
            continue
        if (l.startswith("-") or l.startswith("*")
                or (len(l) < 120 and any(kw in l.lower() for kw in
                    ["rule", "when", "if", "reduce", "increase", "hold", "exit", "size"]))):
            cleaned.append(l)

    if not cleaned:
        return 0

    rule_block = "\n".join(f"  {line}" if not line.startswith("-") else line for line in cleaned)
    new_section = f"{header}{separator}### {today}\n{rule_block}\n"
    path.write_text(existing + new_section, encoding="utf-8")
    return len(cleaned)


def _count_supporting_events(rule_text: str, cfg: Dict[str, Any]) -> int:
    """Count how many market events match this rule's pattern keywords.

    Used to tag rules with [n=X, confidence: low/medium/high].
    This is the single source of truth for rule quality until Phase 2
    performance data exists.
    """
    from tradingagents.portfolio_advisor.market_memory import load_recent_market_events

    events = load_recent_market_events(cfg, days=90)
    if not events:
        return 0

    # Extract significant keywords from the rule (words 5+ chars, lowercase)
    keywords = {w.lower() for w in rule_text.split() if len(w) >= 5}
    if not keywords:
        return 0

    matches = sum(
        1 for e in events
        if any(kw in str(e.get("cause", "")).lower() or
               kw in ",".join(e.get("pattern_tags", [])).lower()
               for kw in keywords)
    )
    return matches


def _flag_challenged_rules(cfg: Dict[str, Any], invalidation_text: str) -> None:
    """Append invalidation flags to _portfolio.md under each affected rule."""
    path = portfolio_rules_path(cfg)
    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Parse verdicts: look for "RULE:" / "VERDICT:" / "NOTE:" triples
    lines = invalidation_text.split("\n")
    current_rule = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("RULE:"):
            current_rule = stripped[5:].strip()[:80]
        elif stripped.startswith("VERDICT:") and "SUPPORT" not in stripped:
            verdict = stripped[8:].strip()
            # Find next NOTE line
            note = ""
            idx = lines.index(line) if line in lines else -1
            if idx >= 0 and idx + 1 < len(lines):
                next_line = lines[idx + 1].strip()
                if next_line.startswith("NOTE:"):
                    note = next_line[5:].strip()[:200]
            if current_rule and verdict:
                flag = f"\n  ⚠ [{today}] {verdict}: {note}\n"
                # Insert after the rule text in _portfolio.md
                if current_rule[:40] in content:
                    # Find the rule and append the flag after it
                    rule_pos = content.find(current_rule[:40])
                    if rule_pos >= 0:
                        # Find end of that line
                        line_end = content.find("\n", rule_pos)
                        if line_end >= 0:
                            content = content[:line_end + 1] + flag + content[line_end + 1:]

    path.write_text(content, encoding="utf-8")


def _fallback_risk_score(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute a basic risk score without LLM, from event metadata."""
    if not events:
        return {
            "risk_score": 0, "trend": "stable", "primary_driver": "none",
            "sizing_guidance": "normal sizing", "ep_recommendation": "normal",
            "rationale": "no recent macro events",
        }

    # Count bear events in last 7 days
    recent = [e for e in events if e.get("date", "") >=
              (datetime.now(timezone.utc).strftime("%Y-%m-%d"))]
    bear_count = sum(1 for e in events if e.get("market_move") == "bear")
    strong_count = sum(1 for e in events if e.get("magnitude") == "strong")
    has_tariff = any("tariff" in str(e.get("cause", "")).lower() for e in events)

    if bear_count >= 2 and strong_count >= 2:
        score = 7
        trend = "deteriorating"
        driver = "recurring macro selloffs"
        sizing = "reduce new entries by 50% until trend improves"
        ep = "pause"
    elif bear_count >= 1 or strong_count >= 1:
        score = 5
        trend = "deteriorating" if bear_count > 0 else "stable"
        driver = "active macro driver" if has_tariff else "mixed signals"
        sizing = "reduce new entries by 25%"
        ep = "caution"
    else:
        score = 3
        trend = "stable"
        driver = "moderate macro activity"
        sizing = "normal sizing"
        ep = "normal"

    return {
        "risk_score": score, "trend": trend, "primary_driver": driver,
        "sizing_guidance": sizing, "ep_recommendation": ep,
        "rationale": f"{bear_count} bear event(s), {strong_count} strong event(s) in recent window",
    }
