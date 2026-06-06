"""Single entry point for portfolio advisor outbound messages (webhook + email).

Every call to ``send_advisor_message`` also appends a row to a JSONL message
log (default ``~/.tradingagents/memory/messages.jsonl``) so the UI can show
recent notifications without depending on the user's inbox.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:  # Python 3.9+ stdlib; absent only in stripped-down environments.
    from zoneinfo import ZoneInfo  # type: ignore[import]
except ImportError:  # pragma: no cover - exotic envs
    ZoneInfo = None  # type: ignore[assignment]

from tradingagents.advisor.email_notify import analysis_smtp_ready, send_plain_email
from tradingagents.advisor.notify import send_webhook

logger = logging.getLogger(__name__)


# Default quiet-hours config. Routine PM messages only push during these windows;
# anything outside is logged to the dashboard and held. Calls with urgent=True
# (chat replies, watchdog price triggers, system failures, PM push_note) bypass.
_DEFAULT_SEND_WINDOWS_UK = [(time(10, 0), time(11, 0)), (time(22, 0), time(23, 0))]
_DEFAULT_QUIET_TZ = "Europe/London"


def _parse_hhmm(s: str) -> Optional[time]:
    try:
        hh, mm = s.strip().split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _send_windows(cfg: Dict[str, Any]) -> List[tuple]:
    raw = cfg.get("portfolio_advisor_send_windows")
    if not raw:
        return _DEFAULT_SEND_WINDOWS_UK
    out: List[tuple] = []
    if isinstance(raw, list):
        for pair in raw:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                start = _parse_hhmm(str(pair[0]))
                end = _parse_hhmm(str(pair[1]))
                if start and end:
                    out.append((start, end))
    return out or _DEFAULT_SEND_WINDOWS_UK


def _within_send_window(cfg: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when local time (Europe/London) sits inside any configured window.

    On exotic environments without zoneinfo we open the gate (return True) so
    quiet hours never silently break delivery.
    """
    if not bool(cfg.get("portfolio_advisor_quiet_hours_enabled", True)):
        return True
    if ZoneInfo is None:
        return True
    tz_name = str(cfg.get("portfolio_advisor_quiet_hours_tz") or _DEFAULT_QUIET_TZ)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return True
    moment = (now or datetime.now(timezone.utc)).astimezone(tz).time()
    for start, end in _send_windows(cfg):
        if start <= end:
            if start <= moment <= end:
                return True
        else:  # window crosses midnight
            if moment >= start or moment <= end:
                return True
    return False


def _clean_trim(text: str, limit: int) -> str:
    """Trim to a word boundary with an ellipsis — never cut mid-word/sentence."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def ntfy_verdict(text: str, ticker: str) -> str:
    """Reduce analysis text to a short actionable line for phone notifications."""
    import re

    verdict = ""
    action = ""
    in_verdict = in_action = False
    for line in text.splitlines():
        s = line.strip()
        upper = s.upper()
        if upper == "VERDICT":
            in_verdict, in_action = True, False
            continue
        if upper in ("REQUIRED ACTION", "REQUIRED ACTIONS"):
            in_action, in_verdict = True, False
            continue
        if s and upper == s and len(s) > 2:
            in_verdict = in_action = False
            continue
        if in_verdict and s and not verdict:
            verdict = s
        if in_action and s and not action and s.lower() != "none":
            action = s

    if verdict:
        out = _clean_trim(verdict, 160)
        if action and action.lower() not in verdict.lower():
            out = f"{out} — {_clean_trim(action, 100)}"
        return out

    # Fall back: find BUY/SELL/HOLD/TRIM/EXIT keyword and surrounding sentence
    for kw in re.findall(r'\b(BUY|SELL|HOLD|TRIM|WATCH|EXIT)\b', text[:1500], re.IGNORECASE):
        kw_upper = kw.upper()
        for sent in re.split(r'[.!\n]', text[:1000]):
            if kw_upper in sent.upper():
                clean = _clean_trim(sent, 160)
                if clean:
                    return clean
        return kw_upper

    return _clean_trim(text, 160)

_MAX_BODY_PERSISTED = 50000
_TELEGRAM_MAX_TEXT = 4096


def message_log_path(cfg: Dict[str, Any]) -> Path:
    """Resolve the JSONL message log path (sibling of memory log by default)."""
    raw = cfg.get("message_log_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    mem = cfg.get("memory_log_path")
    if isinstance(mem, str) and mem.strip():
        return Path(mem).expanduser().parent / "messages.jsonl"
    return Path.home() / ".tradingagents" / "memory" / "messages.jsonl"


def _derive_level(subject: str) -> str:
    s = (subject or "").upper()
    if "CRITICAL" in s or "FAILED" in s or "BROKEN" in s:
        return "critical"
    if "HIGH" in s or "WEAKENING" in s or "OVERDUE" in s:
        return "high"
    if "REVIEW" in s or "ATTENTION" in s:
        return "review"
    return "info"


def telegram_ready(cfg: Dict[str, Any]) -> bool:
    """Return True when Telegram bot delivery has the required config."""
    return bool(
        str(cfg.get("analysis_telegram_bot_token") or "").strip()
        and str(cfg.get("analysis_telegram_chat_id") or "").strip()
    )


def _telegram_text(subject: str, body: str) -> str:
    title = str(subject or "TradingAgents").strip() or "TradingAgents"
    text = f"{title}\n\n{body or ''}".strip()
    if len(text) <= _TELEGRAM_MAX_TEXT:
        return text
    suffix = "\n\n[truncated - see dashboard for full text]"
    return text[: _TELEGRAM_MAX_TEXT - len(suffix)] + suffix


def send_telegram_message(cfg: Dict[str, Any], subject: str, body: str) -> bool:
    """Send one advisor message via Telegram Bot API."""
    token = str(cfg.get("analysis_telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("analysis_telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return False
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": _telegram_text(subject, body),
        "disable_web_page_preview": True,
    }
    thread_id = str(cfg.get("analysis_telegram_thread_id") or "").strip()
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            payload["message_thread_id"] = thread_id
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=30,
        )
        if 200 <= r.status_code < 300:
            data = r.json()
            return bool(data.get("ok", True))
        logger.warning("Telegram returned %s: %s", r.status_code, r.text[:500])
        return False
    except (requests.RequestException, ValueError) as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def _message_dedupe_minutes(cfg: Dict[str, Any]) -> int:
    # 24h default (was 180min). Telegram users complained about repeats.
    try:
        return max(0, int(cfg.get("portfolio_advisor_message_dedupe_minutes", 1440) or 0))
    except (TypeError, ValueError):
        return 1440


def _normalize_message_for_dedupe(text: str) -> str:
    s = str(text or "").lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _is_correction(subject: str, body: str) -> bool:
    txt = f"{subject}\n{body}".lower()
    return any(word in txt for word in ("correction:", "correcting", "ignore earlier", "superseded"))


def _recent_duplicate_message(cfg: Dict[str, Any], subject: str, body: str) -> Optional[Dict[str, Any]]:
    """Return a recent near-duplicate message record, if one should suppress delivery."""
    minutes = _message_dedupe_minutes(cfg)
    if minutes <= 0 or _is_correction(subject, body):
        return None
    now = datetime.now(timezone.utc)
    subj = str(subject or "").strip().lower()
    norm = _normalize_message_for_dedupe(body)
    if not norm:
        return None
    for row in load_recent_messages(cfg, limit=80):
        if str(row.get("subject") or "").strip().lower() != subj:
            continue
        if row.get("suppressed_duplicate"):
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("timestamp") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (now - ts).total_seconds() > minutes * 60:
            continue
        old = _normalize_message_for_dedupe(str(row.get("body") or ""))
        if old == norm or SequenceMatcher(None, old, norm).ratio() >= 0.75:
            return row
    return None


def append_message_record(
    cfg: Dict[str, Any],
    *,
    subject: str,
    body: str,
    webhook_ok: bool,
    telegram_ok: bool,
    smtp_ok: bool,
    webhook_attempted: bool,
    telegram_attempted: bool,
    smtp_attempted: bool,
    suppressed_duplicate: bool = False,
) -> None:
    """Append one JSON object as a single line. Never raises."""
    path = message_log_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("message log mkdir failed: %s", e)
        return
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subject": str(subject or "")[:500],
        "body": str(body or "")[:_MAX_BODY_PERSISTED],
        "level": _derive_level(str(subject or "")),
        "channels_attempted": [
            c
            for c, ok in (
                ("webhook", webhook_attempted),
                ("telegram", telegram_attempted),
                ("smtp", smtp_attempted),
            )
            if ok
        ],
        "webhook_ok": bool(webhook_ok),
        "telegram_ok": bool(telegram_ok),
        "smtp_ok": bool(smtp_ok),
        "delivered": bool(webhook_ok or telegram_ok or smtp_ok),
        "suppressed_duplicate": bool(suppressed_duplicate),
    }
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("message log append failed: %s", e)


def load_recent_messages(
    cfg: Dict[str, Any],
    *,
    limit: int = 200,
    level_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Newest-first window over the JSONL message log; capped for UI safety."""
    path = message_log_path(cfg)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines()[-5000:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if level_filter and row.get("level") != level_filter:
            continue
        out.append(row)
    out.reverse()
    if limit and limit > 0:
        out = out[:limit]
    return out


def send_advisor_message(
    cfg: Dict[str, Any],
    subject: str,
    body: str,
    *,
    urgent: bool = False,
    log_as_recommendation: bool = False,
    rec_trigger: str = "",
    rec_type: str = "",
    rec_action: str = "",
    rec_ticker: Optional[str] = None,
    rec_rule_ref: Optional[str] = None,
    rec_rationale: str = "",
) -> bool:
    """Post to analysis webhook and/or Telegram and/or SMTP when configured.

    Call this whenever the portfolio advisor needs to reach the user (alerts,
    weekly heartbeat, job lifecycle, manual ``portfolio alert``, etc.).

    When ``log_as_recommendation=True``, the message is also written to the
    recommendation log BEFORE any channel delivery. If the log write fails,
    the function returns False without sending — untracked advice is worse
    than no advice.

    ``urgent=True`` bypasses the quiet-hours window.
    Returns True if at least one channel succeeded.
    """
    # Phase 2: log as recommendation before sending
    if log_as_recommendation:
        try:
            from tradingagents.portfolio_advisor.recommendation_log import log_recommendation
            rec_id = log_recommendation(
                cfg,
                trigger=rec_trigger or "manual",
                type=rec_type or "macro_alert",
                action=rec_action or body[:80],
                rationale=rec_rationale or body[:600],
                ticker=rec_ticker,
                rule_ref=rec_rule_ref,
            )
            if rec_id is None:
                logger.error("recommendation log write failed — aborting send")
                return False
        except Exception as e:
            logger.error("recommendation log failed: %s", e)
            return False
    duplicate = _recent_duplicate_message(cfg, subject, body)
    if duplicate is not None:
        append_message_record(
            cfg,
            subject=subject,
            body=body,
            webhook_ok=False,
            telegram_ok=False,
            smtp_ok=False,
            webhook_attempted=False,
            telegram_attempted=False,
            smtp_attempted=False,
            suppressed_duplicate=True,
        )
        logger.info("suppressed duplicate advisor message: %s", subject[:120])
        return False

    if not urgent and not _within_send_window(cfg):
        # Quiet hours: record to dashboard so it isn't lost, but skip push channels.
        append_message_record(
            cfg,
            subject=subject,
            body=body,
            webhook_ok=False,
            telegram_ok=False,
            smtp_ok=False,
            webhook_attempted=False,
            telegram_attempted=False,
            smtp_attempted=False,
            suppressed_duplicate=False,
        )
        logger.info("held outside send window (quiet hours): %s", subject[:120])
        return False

    webhook_ok = False
    url = (cfg.get("analysis_webhook_url") or "").strip()
    webhook_attempted = bool(url)
    if url:
        try:
            webhook_ok = bool(send_webhook(url, body[:4000], extra={"title": subject}))
        except Exception as e:
            logger.warning("advisor webhook failed: %s", e)
    telegram_ok = False
    telegram_attempted = telegram_ready(cfg)
    if telegram_attempted:
        telegram_ok = send_telegram_message(cfg, subject, body)
    smtp_ok = False
    smtp_attempted = analysis_smtp_ready(cfg)
    if smtp_attempted:
        to_addrs = (cfg.get("analysis_email_to") or "").strip()
        smtp_user = (cfg.get("smtp_user") or "").strip()
        from_addr = (cfg.get("analysis_email_from") or "").strip() or smtp_user
        try:
            smtp_ok = bool(
                send_plain_email(
                    to_addrs=to_addrs,
                    from_addr=from_addr,
                    subject=subject,
                    body=body[:_MAX_BODY_PERSISTED],
                    smtp_host=(cfg.get("smtp_host") or "").strip(),
                    smtp_port=int(cfg.get("smtp_port") or 587),
                    smtp_user=smtp_user,
                    smtp_password=(cfg.get("smtp_password") or "").strip(),
                    use_tls=bool(cfg.get("smtp_use_tls", True)),
                )
            )
        except Exception as e:
            logger.warning("advisor SMTP failed: %s", e)
    sent = webhook_ok or telegram_ok or smtp_ok
    if not sent and (not url) and (not telegram_attempted) and (not smtp_attempted):
        logger.warning(
            "Portfolio advisor message not sent (configure TRADINGAGENTS_ANALYSIS_WEBHOOK_URL "
            "and/or Telegram bot/chat and/or analysis email + SMTP): %s",
            subject[:120],
        )
    try:
        append_message_record(
            cfg,
            subject=subject,
            body=body,
            webhook_ok=webhook_ok,
            telegram_ok=telegram_ok,
            smtp_ok=smtp_ok,
            webhook_attempted=webhook_attempted,
            telegram_attempted=telegram_attempted,
            smtp_attempted=smtp_attempted,
        )
    except Exception:
        logger.debug("message log persist skipped", exc_info=True)
    # Log every actual push to the conversation history so the PM remembers
    # what it already told the human and avoids repeating itself next cycle.
    try:
        if sent:
            from tradingagents.portfolio_advisor import pm_workspace as _pmws
            _pmws.record_pm_message(cfg, body, kind="push", subject=subject)
    except Exception:
        logger.debug("conversation log on send failed", exc_info=True)
    return sent
