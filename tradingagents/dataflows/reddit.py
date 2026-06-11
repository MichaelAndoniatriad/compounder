"""Reddit search fetcher for ticker-specific discussion posts.

Uses Reddit's public JSON endpoints (``reddit.com/r/{sub}/search.json``)
which do not require an API key. Public throughput is ~10 requests per
minute per IP, well within budget for a single agent run that queries
a handful of finance subreddits per ticker.

Returns formatted plaintext blocks ready for prompt injection. Degrades
gracefully — returns a placeholder string rather than raising, so callers
never have to special-case missing data.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import get_config

logger = logging.getLogger(__name__)

_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_UA = "compounder/1.0 (+https://github.com/MichaelAndoniatriad/compounder)"

# Default subreddits ordered roughly by signal density for ticker-specific
# discussion. wallstreetbets has the most volume but most noise; stocks /
# investing trend more measured. Caller can override.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

# Signal-quality tiers for subreddits.
# Tier 1 = measured / institutional-leaning (r/investing, r/stocks)
# Tier 2 = high-volume retail / options (r/wallstreetbets, r/options)
# Tier 3 = everything else
_SUBREDDIT_TIERS: dict[str, int] = {
    "investing": 1,
    "stocks": 1,
    "wallstreetbets": 2,
    "options": 2,
}


def _subreddit_tier(sub: str) -> int:
    return _SUBREDDIT_TIERS.get(sub.lower(), 3)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    qs = urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # last 7 days
        "limit": limit,
    })
    url = _API.format(sub=sub, qs=qs)
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as exc:
        # Reddit aggressively rate-limits/403s unauthenticated scrapers — log at
        # debug so this isn't surfaced as a warning in every full_graph. The
        # sentiment analyst handles an empty Reddit block downstream.
        logger.debug("Reddit fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []
    children = (payload.get("data") or {}).get("children") or []
    return [c.get("data", {}) for c in children if isinstance(c, dict)]


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 0.4,
) -> str:
    """Fetch recent Reddit posts mentioning ``ticker`` across finance
    subreddits and return them as a formatted plaintext block.

    Posts with an upvote score below ``min_reddit_upvotes`` (config key,
    default 20) are filtered out before formatting — low-engagement posts
    are typically noise and pollute the LLM prompt.

    Each subreddit block is tagged with its signal-quality tier:
      Tier 1 (r/investing, r/stocks) — measured, institutional-leaning
      Tier 2 (r/wallstreetbets, r/options) — high-volume retail/options
      Tier 3 — everything else

    ``inter_request_delay`` keeps us under Reddit's public rate limit
    (~10 req/min per IP) even if the caller queries many subreddits.
    """
    min_upvotes = get_config().get("min_reddit_upvotes", 20)

    blocks = []
    total_posts = 0
    for i, sub in enumerate(subreddits):
        if i > 0:
            time.sleep(inter_request_delay)
        raw_posts = _fetch_subreddit(ticker, sub, limit_per_sub, timeout)
        tier = _subreddit_tier(sub)

        posts = [p for p in raw_posts if (p.get("score") or 0) >= min_upvotes]
        dropped = len(raw_posts) - len(posts)

        if not raw_posts:
            blocks.append(
                f"r/{sub} [Tier {tier}]: <no posts found mentioning {ticker.upper()} in the past 7 days>"
            )
            continue

        if not posts:
            blocks.append(
                f"r/{sub} [Tier {tier}]: <all {len(raw_posts)} post(s) filtered out "
                f"(upvotes < {min_upvotes})>"
            )
            continue

        total_posts += len(posts)
        filter_note = f" ({dropped} low-engagement post(s) filtered)" if dropped else ""
        lines = [f"r/{sub} [Tier {tier}] — {len(posts)} posts mentioning {ticker.upper()}{filter_note}:"]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score", 0)
            comments = p.get("num_comments", 0)
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{created_str} · {score:>4}↑ · {comments:>3}c] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        return (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days "
            f"(all posts may have been filtered by the upvote threshold of {min_upvotes})>"
        )
    return "\n\n".join(blocks)
