"""eToro browser automation via Playwright — Phase 1.

Phase 1 (today): one-time interactive login with SMS code prompt, save
``storage_state`` for reuse. Verify subsequent runs land on the portfolio
page without re-prompting.

Phase 2 (next session): trade entry (find ticker → enter size → submit →
verify the order ack page). Triggered only via ``executor.attempt_execute``.

Credentials (env vars on the server, in .env, NOT in code):
  - ``ETORO_LOGIN_USERNAME`` — your eToro email/username for the web UI
  - ``ETORO_LOGIN_PASSWORD`` — your eToro web password

Session file: ``~/.tradingagents/portfolio_advisor/etoro_session.json``
(mode 0600). Treat it as a credential — anyone with it can act as you on
eToro until the session expires.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple

ETORO_LOGIN_URL = "https://www.etoro.com/login"
ETORO_PORTFOLIO_URL = "https://www.etoro.com/portfolio"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def session_path(cfg: Dict[str, Any]) -> Path:
    from tradingagents.portfolio_advisor import state as pa_state
    return pa_state.advisor_dir(cfg) / "etoro_session.json"


def _new_context(p, *, storage_state: Path | None = None):
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context_kwargs = {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1280, "height": 800},
        "locale": "en-GB",
    }
    if storage_state and storage_state.is_file():
        context_kwargs["storage_state"] = str(storage_state)
    context = browser.new_context(**context_kwargs)
    return browser, context


def login_interactive(cfg: Dict[str, Any], *, sms_prompt=input) -> str:
    """One-time login. Asks the human for the SMS code via ``sms_prompt``
    (defaults to stdin ``input``). Saves storage_state on success.

    Returns a status string.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    user = (os.environ.get("ETORO_LOGIN_USERNAME") or "").strip()
    pw = (os.environ.get("ETORO_LOGIN_PASSWORD") or "").strip()
    if not user or not pw:
        return "missing ETORO_LOGIN_USERNAME / ETORO_LOGIN_PASSWORD in env"

    sp = session_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()
        try:
            page.goto(ETORO_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            # eToro can take a moment for cookie banners + the login form to render.
            time.sleep(3)

            # Best-effort dismiss of cookie banners.
            for sel in ("button:has-text('Accept all')", "button:has-text('Accept All')",
                        "button:has-text('Agree')", "#onetrust-accept-btn-handler"):
                try:
                    page.click(sel, timeout=2000)
                    break
                except Exception:
                    continue

            # Fill username + password. Multiple selectors as fallback.
            for sel in ("input[name='username']", "input[type='email']", "#username", "input[autocomplete='username']"):
                try:
                    page.fill(sel, user, timeout=3000); break
                except Exception:
                    continue
            for sel in ("input[name='password']", "input[type='password']", "#password", "input[autocomplete='current-password']"):
                try:
                    page.fill(sel, pw, timeout=3000); break
                except Exception:
                    continue

            # Click login.
            for sel in ("button[type='submit']", "button:has-text('Log In')",
                        "button:has-text('Sign in')", "#login-button"):
                try:
                    page.click(sel, timeout=3000); break
                except Exception:
                    continue

            # Wait for either the SMS prompt or the portfolio (if no 2FA hit).
            sms_selectors = (
                "input[name='code']",
                "input[name='otp']",
                "input[autocomplete='one-time-code']",
                "input[type='tel']",
            )
            got_sms = False
            try:
                page.wait_for_selector(",".join(sms_selectors), timeout=20000)
                got_sms = True
            except PWTimeout:
                pass

            if got_sms:
                # Ask the human for the code; fill + submit.
                code = (sms_prompt("Enter the SMS code eToro just sent: ") or "").strip()
                if not code:
                    return "no code entered; aborting"
                for sel in sms_selectors:
                    try:
                        page.fill(sel, code, timeout=2000); break
                    except Exception:
                        continue
                for sel in ("button[type='submit']", "button:has-text('Continue')",
                            "button:has-text('Verify')", "button:has-text('Submit')"):
                    try:
                        page.click(sel, timeout=3000); break
                    except Exception:
                        continue

            # Wait for portfolio.
            try:
                page.wait_for_url("**/portfolio**", timeout=45000)
            except PWTimeout:
                # Sometimes the landing path differs; check if we're authenticated
                # by trying to navigate to /portfolio directly.
                page.goto(ETORO_PORTFOLIO_URL, wait_until="domcontentloaded", timeout=30000)
                if "login" in page.url.lower():
                    return f"login appears to have failed; still at {page.url}"

            context.storage_state(path=str(sp))
            try:
                sp.chmod(0o600)
            except Exception:
                pass
            return f"login OK; session saved to {sp} ({sp.stat().st_size} bytes)"

        except Exception as e:
            return f"login error: {type(e).__name__}: {e}"
        finally:
            try:
                browser.close()
            except Exception:
                pass


def verify_session(cfg: Dict[str, Any]) -> str:
    """Load saved storage_state, confirm we land on /portfolio without
    being redirected to /login. Returns a status string."""
    from playwright.sync_api import sync_playwright

    sp = session_path(cfg)
    if not sp.is_file():
        return "no saved session — run 'advisor portfolio executor login' first"

    with sync_playwright() as p:
        browser, context = _new_context(p, storage_state=sp)
        page = context.new_page()
        try:
            page.goto(ETORO_PORTFOLIO_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(2)
            url = page.url
            if "login" in url.lower():
                return f"session expired (redirected to {url}); run login again"
            title = page.title() or ""
            return f"session OK; landed at {url} (title: {title[:80]})"
        except Exception as e:
            return f"verify error: {type(e).__name__}: {e}"
        finally:
            try:
                browser.close()
            except Exception:
                pass


def execute_trade(cfg: Dict[str, Any], proposal: Dict[str, Any]) -> Tuple[bool, str]:
    """Phase 2 — not implemented this session.

    The interface is intentionally fixed now so ``executor.attempt_execute``
    can call it once wired: returns ``(ok, message)``. Anything that needs
    to inspect the page state should happen here, not in the executor.
    """
    raise NotImplementedError(
        "Phase 2: real trade entry not yet wired — login + session only today."
    )
