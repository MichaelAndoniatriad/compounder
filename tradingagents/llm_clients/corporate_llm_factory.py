"""Build per-agent LLM instances for corporate-hierarchy routing via OpenRouter.

All logical agents use the OpenAI-compatible OpenRouter endpoint. Model IDs are
OpenRouter slugs (``provider/model``). Routing is defined in ``model_catalog`` and
overridable only through application config (``agent_llm_routing``), not env vars.
"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional

from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.fallback_chat_model import RateLimitFallbackChatModel
from tradingagents.llm_clients.model_catalog import DEFAULT_CORPORATE_AGENT_ROUTING

logger = logging.getLogger(__name__)

# Graph internal role keys -> keys in DEFAULT_CORPORATE_AGENT_ROUTING / overrides
_ROLE_TO_SPEC_KEY: Dict[str, str] = {
    "market": "market_analyst",
    "social": "sentiment_analyst",
    "news": "news_analyst",
    "fundamentals": "fundamentals_analyst",
    "bull": "bull_researcher",
    "bear": "bear_researcher",
    "trader": "trader",
    "risk_aggressive": "risk_aggressive",
    "risk_neutral": "risk_neutral",
    "risk_conservative": "risk_conservative",
    "research_manager": "research_manager",
    # Combined Research Manager + Trader node — reuses research_manager routing spec.
    "research_execution": "research_manager",
    "portfolio_manager": "portfolio_manager",
    "reflection": "reflection",
}


def _openrouter_base_url(config: Dict[str, Any]) -> Optional[str]:
    url = config.get("corporate_openrouter_base_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _effective_routing_table(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    base = deepcopy(DEFAULT_CORPORATE_AGENT_ROUTING)
    overrides = config.get("agent_llm_routing") or {}
    if isinstance(overrides, dict):
        for key, spec in overrides.items():
            if spec is None:
                continue
            if key not in base:
                base[key] = {}
            if isinstance(spec, dict):
                merged = {**base.get(key, {}), **spec}
                merged["provider"] = "openrouter"
                base[key] = merged
    for spec in base.values():
        if isinstance(spec, dict):
            spec["provider"] = "openrouter"
    return base


def _build_openrouter_fallback_llm(
    config: Dict[str, Any],
    callbacks: List[Any],
) -> Optional[Any]:
    model = (config.get("llm_fallback_openrouter_model") or "openai/gpt-4o-mini").strip()
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.warning(
            "Corporate hierarchy: OPENROUTER_API_KEY missing; rate-limit fallback disabled."
        )
        return None
    kwargs: Dict[str, Any] = {"callbacks": callbacks}
    client = create_llm_client(
        "openrouter",
        model,
        base_url=_openrouter_base_url(config),
        **kwargs,
    )
    return client.get_llm()


def _vault_max_tokens(config: Dict[str, Any]) -> int:
    return int(config.get("openai_vault_max_completion_tokens") or 16000)


def _make_llm_for_spec(
    spec: Dict[str, Any],
    config: Dict[str, Any],
    callbacks: List[Any],
    *,
    role: str,
) -> Any:
    model = (spec.get("model") or "").strip()
    if not model:
        raise ValueError(f"Corporate routing for {role}: missing OpenRouter model id")

    client_kwargs: Dict[str, Any] = {"callbacks": callbacks}

    extra_body = spec.get("extra_body")
    if extra_body:
        client_kwargs["extra_body"] = extra_body

    if "/o1" in model or "/o3" in model:
        client_kwargs.setdefault("max_completion_tokens", _vault_max_tokens(config))

    # Low, fixed temperature for more repeatable verdicts across runs. Applies to
    # all roles; reasoning models accept it (and largely sample-invariantly).
    temp = config.get("graph_temperature")
    if temp is not None:
        client_kwargs.setdefault("temperature", float(temp))

    client = create_llm_client(
        "openrouter",
        model,
        base_url=_openrouter_base_url(config),
        **client_kwargs,
    )
    primary = client.get_llm()

    if not config.get("llm_rate_limit_fallback_enabled", True):
        return primary

    fallback_model = (config.get("llm_fallback_openrouter_model") or "openai/gpt-4o-mini").strip()
    if not os.environ.get("OPENROUTER_API_KEY"):
        return primary
    if fallback_model == model:
        return primary

    fb = create_llm_client(
        "openrouter",
        fallback_model,
        base_url=_openrouter_base_url(config),
        callbacks=callbacks,
    ).get_llm()
    return RateLimitFallbackChatModel(primary, fb)


def build_corporate_hierarchy_llms(
    config: Dict[str, Any],
    callbacks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Return a dict mapping graph role keys to OpenRouter-backed LLMs."""
    cb = callbacks or []
    routing = _effective_routing_table(config)
    out: Dict[str, Any] = {}
    for role, spec_key in _ROLE_TO_SPEC_KEY.items():
        spec = routing.get(spec_key)
        if not spec:
            raise KeyError(f"Missing corporate routing entry for {spec_key!r}")
        out[role] = _make_llm_for_spec(spec, config, cb, role=role)
    return out
