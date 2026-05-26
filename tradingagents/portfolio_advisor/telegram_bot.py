"""Telegram inbound chat bridge for the advisor PM."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from tradingagents.portfolio_advisor import messaging, state
from tradingagents.portfolio_advisor.advisor_pm import cancel_last_action, run_pm_cycle
from tradingagents.portfolio_advisor.models import AdvisorPMCycleResult

logger = logging.getLogger(__name__)


def telegram_state_path(cfg: Dict[str, Any]) -> Path:
    raw = cfg.get("portfolio_advisor_telegram_state_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return state.advisor_dir(cfg) / "telegram_state.json"


def _load_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = telegram_state_path(cfg)
    if not p.is_file():
        return {"last_update_id": None, "processed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_update_id": None, "processed": []}
    return data if isinstance(data, dict) else {"last_update_id": None, "processed": []}


def _save_state(cfg: Dict[str, Any], data: Dict[str, Any]) -> None:
    p = telegram_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _token(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("analysis_telegram_bot_token") or "").strip()


def _allowed_chat_id(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("analysis_telegram_chat_id") or "").strip()


def _api_get(cfg: Dict[str, Any], method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    token = _token(cfg)
    if not token:
        raise RuntimeError("Telegram bot token is not configured.")
    r = requests.get(f"https://api.telegram.org/bot{token}/{method}", params=params, timeout=60)
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError(f"Telegram {method} returned non-JSON: {r.text[:300]}") from e
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description') or data}")
    return data


def _extract_message(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("message", "edited_message"):
        msg = update.get(key)
        if isinstance(msg, dict):
            return msg
    return None


def fetch_updates(cfg: Dict[str, Any], *, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"timeout": int(timeout), "allowed_updates": json.dumps(["message", "edited_message"])}
    if offset is not None:
        params["offset"] = int(offset)
    data = _api_get(cfg, "getUpdates", params)
    rows = data.get("result") or []
    return [r for r in rows if isinstance(r, dict)]


def _is_trivial(text: str) -> bool:
    s = " ".join(text.strip().lower().split())
    return s in {"", "/start", "start", "hello", "hi", "hey", "test"}


def _format_pm_reply(result: AdvisorPMCycleResult) -> str:
    """Return the PM's conversational reply. The PM writes prose; we don't decorate it.

    The only structured tails we keep are: (1) a one-line note when jobs were queued
    so the human knows they can CANCEL, and (2) the push_note if it adds something
    beyond the summary. No section headers, no bullet stance dumps — those live in
    the dashboard, not on a phone.
    """
    summary = (result.executive_summary or "").strip()
    # Defang literal "None"/"null" — some model outputs land that way and a
    # bare "None" must never be sent to the user as an answer.
    if summary.lower() in ("none", "null", "undefined"):
        summary = ""
    lines: List[str] = [summary] if summary else []

    if result.append_jobs:
        queued = ", ".join(f"{j.ticker}" for j in result.append_jobs[:5])
        lines.append(f"\nQueued: {queued}. Reply CANCEL to undo.")

    push = (result.push_note or "").strip()
    if push and push not in summary:
        lines.append(f"\n{push}")

    out = "\n".join(lines).strip()
    return out or "I don't have a clear read on that right now."


def answer_text(cfg: Dict[str, Any], text: str) -> str:
    s = (text or "").strip()
    if not s:
        return "Send me a portfolio question, for example: what should I do?"
    if s.upper() == "CANCEL":
        return cancel_last_action(cfg)
    result = run_pm_cycle(
        cfg,
        trigger="ntfy_question",
        extra_context=f"Telegram human question:\n{s}\n\nAnswer as a direct chat reply. Be concise, clear, and advisory only.",
    )
    return _format_pm_reply(result)


def process_update(cfg: Dict[str, Any], update: Dict[str, Any]) -> Optional[str]:
    msg = _extract_message(update)
    if not msg:
        return None
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    allowed = _allowed_chat_id(cfg)
    if allowed and chat_id != allowed:
        logger.info("Ignoring Telegram message from unauthorized chat_id=%s", chat_id)
        return None
    text = str(msg.get("text") or "").strip()
    if _is_trivial(text):
        reply = "Hi. Ask me a portfolio question, for example: what should I do?"
    else:
        reply = answer_text(cfg, text)
    ok = messaging.send_telegram_message(cfg, "PM", reply)
    if not ok:
        raise RuntimeError("Telegram reply failed.")
    return reply


def poll_once(cfg: Dict[str, Any], *, timeout: int = 1) -> int:
    st = _load_state(cfg)
    last = st.get("last_update_id")
    offset = (int(last) + 1) if last is not None else None
    updates = fetch_updates(cfg, offset=offset, timeout=timeout)
    processed = 0
    max_update_id = last
    for upd in updates:
        uid = upd.get("update_id")
        if uid is None:
            continue
        max_update_id = max(int(uid), int(max_update_id)) if max_update_id is not None else int(uid)
        try:
            if process_update(cfg, upd) is not None:
                processed += 1
        finally:
            st["last_update_id"] = max_update_id
            st["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            _save_state(cfg, st)
    return processed


def run_poll_loop(cfg: Dict[str, Any], *, interval_seconds: float = 2.0, timeout: int = 25) -> None:
    if not messaging.telegram_ready(cfg):
        raise RuntimeError("Telegram bot token/chat id are not configured.")
    while True:
        try:
            poll_once(cfg, timeout=timeout)
        except Exception as e:
            logger.warning("Telegram poll failed: %s", e)
            time.sleep(max(5.0, interval_seconds))
            continue
        time.sleep(max(0.2, interval_seconds))
