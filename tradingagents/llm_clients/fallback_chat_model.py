"""Rate-limit fallback wrapper for chat models used in the agent graph.

Agents compose ``prompt | llm.bind_tools(...)`` or ``llm.with_structured_output``.
This wrapper exposes the same surface so callers can attach tools / structured
schemas to **both** primaries and fallbacks, then transparently retry on
HTTP 429 / provider rate-limit errors.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Optional

from langchain_core.runnables import RunnableLambda, Runnable

logger = logging.getLogger(__name__)


def is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort detection across OpenAI / Anthropic / Google / httpx."""
    name = type(exc).__name__
    if "RateLimit" in name or "ResourceExhausted" in name:
        return True
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    # OpenAI APIError often carries response with status_code
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return is_rate_limit_error(cause)
    ctx = getattr(exc, "__context__", None)
    if ctx is not None and ctx is not exc:
        return is_rate_limit_error(ctx)
    return False


def _dual_invoke(
    primary: Any,
    fallback: Any,
    input_: Any,
    config: Any = None,
    **kwargs: Any,
):
    try:
        return primary.invoke(input_, config=config, **kwargs)
    except Exception as exc:
        if fallback is None or not is_rate_limit_error(exc):
            raise
        logger.warning(
            "Primary LLM hit rate limit (%s); retrying with fallback model.",
            type(exc).__name__,
        )
        return fallback.invoke(input_, config=config, **kwargs)


def rate_limit_fallback_runnable(primary: Any, fallback: Optional[Any]) -> Any:
    """Return a Runnable that falls back on rate limits (or the primary if no fallback)."""
    if fallback is None:
        return primary
    return RunnableLambda(partial(_dual_invoke, primary, fallback))


class RateLimitFallbackChatModel(Runnable):
    """Thin façade matching ``bind_tools`` / ``with_structured_output`` / ``invoke``.

    Subclasses ``Runnable`` so it can be piped directly (``prompt | llm``) by
    analysts that don't bind tools (e.g. the social/sentiment analyst); without
    this, LCEL's ``coerce_to_runnable`` rejects the bare façade.
    """

    def __init__(self, primary: Any, fallback: Optional[Any]):
        self._primary = primary
        self._fallback = fallback

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        pb = self._primary.bind_tools(tools, **kwargs)
        fb = self._fallback.bind_tools(tools, **kwargs) if self._fallback else None
        return rate_limit_fallback_runnable(pb, fb)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        pb = self._primary.with_structured_output(schema, **kwargs)
        fb = (
            self._fallback.with_structured_output(schema, **kwargs)
            if self._fallback
            else None
        )
        return rate_limit_fallback_runnable(pb, fb)

    def invoke(self, input_: Any, config: Any = None, **kwargs: Any):
        return _dual_invoke(self._primary, self._fallback, input_, config, **kwargs)
