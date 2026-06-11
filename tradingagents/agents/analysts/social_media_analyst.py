"""Backwards-compatibility shim for the renamed module.

The agent is now ``sentiment_analyst`` and aggregates Yahoo Finance news,
StockTwits cashtag streams, and Reddit posts into a single sentiment
report. Import from ``tradingagents.agents.analysts.sentiment_analyst``
going forward; this module will be removed in a future release.

Renamed from social_media_analyst because the old prompt requested social-media analysis but only Yahoo Finance news was available, causing LLMs to fabricate Reddit/X/StockTwits content under prompt pressure.
"""

import warnings as _warnings

from tradingagents.agents.analysts.sentiment_analyst import (  # noqa: F401
    create_sentiment_analyst,
    create_social_media_analyst,
)

_warnings.warn(
    "tradingagents.agents.analysts.social_media_analyst is deprecated. "
    "Import from tradingagents.agents.analysts.sentiment_analyst instead.",
    DeprecationWarning,
    stacklevel=2,
)
