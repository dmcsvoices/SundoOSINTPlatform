"""Social media monitoring module for Sundo Pi OSINT platform.

Monitors X / Twitter and TikTok for coordination indicators and
hashtag bursts related to hasbara campaigns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

try:
    import tweepy
except Exception:  # pragma: no cover
    tweepy = None  # type: ignore[misc,assignment]

try:
    import snscrape.modules.twitter as sn_twitter
except Exception:  # pragma: no cover
    sn_twitter = None  # type: ignore[misc,assignment]

from sundo.config import BASE_DIR, LOG_FORMAT, LOG_LEVEL
from sundo.db.sqlite_store import init_db, insert_many

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("social_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WATCHLIST_HASHTAGS: List[str] = [
    "#StandWithIsrael",
    "#Israel",
    "#BringThemHome",
    "#HamasIsISIS",
    "#DefendIsrael",
]

# API credentials follow the same env-var pattern as sundo.config
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")

_MIN_DELAY = 2.0
_MAX_DELAY = 5.0


def _sleep_between_requests() -> None:
    """Pause for a random duration between outbound requests."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    time.sleep(delay)


def _content_hash(text: str) -> str:
    """Return SHA-256 hex digest of *text*.

    Args:
        text: Raw post content.

    Returns:
        Lowercase hex digest string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_sponsored(text: str) -> bool:
    """Detect sponsored-content disclosures in post text.

    Args:
        text: Post content.

    Returns:
        ``True`` if ``#ad``, ``#sponsored``, ``#partner``, or ``#gifted``
        is present (case-insensitive).
    """
    if not text:
        return False
    return any(tag in text.lower() for tag in ("#ad", "#sponsored", "#partner", "#gifted"))


def _extract_hashtags(text: str) -> List[str]:
    """Pull hashtag tokens out of post text.

    Args:
        text: Post content.

    Returns:
        List of hashtag strings (including leading ``#``).
    """
    if not text:
        return []
    return [w for w in text.split() if w.startswith("#") and len(w) > 1]


def _extract_mentions(text: str) -> List[str]:
    """Pull mention tokens out of post text.

    Args:
        text: Post content.

    Returns:
        List of mention strings (including leading ``@``).
    """
    if not text:
        return []
    return [w for w in text.split() if w.startswith("@") and len(w) > 1]


def _extract_urls(text: str) -> List[str]:
    """Pull URL-like tokens out of post text.

    Args:
        text: Post content.

    Returns:
        List of URL strings.
    """
    if not text:
        return []
    urls: List[str] = []
    for word in text.split():
        clean = word.strip(".,;:!?()[]{}\"'")
        if clean.startswith(("http://", "https://", "www.")):
            urls.append(clean)
    return urls


def _build_social_post(
    platform: str,
    post_id: str,
    author_handle: str,
    author_name: Optional[str],
    content: str,
    posted_at: Optional[str],
    language: Optional[str],
    is_reply: bool,
    reply_to_post_id: Optional[str],
    likes: int,
    retweets: int,
    replies: int,
    quotes: int,
    follower_count: Optional[int],
    verified: bool,
    raw_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Construct a row dict for the ``social_posts`` schema.

    Fields that do not exist as top-level columns (e.g.
    ``follower_count``, ``content_hash``, ``disclosed_sponsored``)
    are serialised into ``raw_json``.

    Args:
        platform: Platform name (e.g. ``'twitter'``).
        post_id: Unique post identifier.
        author_handle: Screen name / handle.
        author_name: Display name.
        content: Raw post text.
        posted_at: ISO-formatted timestamp.
        language: BCP-47 language code.
        is_reply: Whether the post is a reply.
        reply_to_post_id: Parent post ID if reply.
        likes: Like count.
        retweets: Retweet / repost count.
        replies: Reply count.
        quotes: Quote-post count.
        follower_count: Author follower count.
        verified: Author verified status.
        raw_data: Additional payload to embed in ``raw_json``.

    Returns:
        Dict ready for ``insert_many("social_posts", ...)``.
    """
    hashtags = _extract_hashtags(content)
    mentions = _extract_mentions(content)
    urls = _extract_urls(content)

    enriched = {
        **raw_data,
        "content_hash": _content_hash(content),
        "disclosed_sponsored": _check_sponsored(content),
        "follower_count": follower_count,
        "verified": verified,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "quotes": quotes,
    }

    return {
        "platform": platform,
        "post_id": post_id,
        "author_handle": author_handle,
        "author_name": author_name,
        "content": content,
        "hashtags": ", ".join(hashtags) if hashtags else None,
        "mentions": ", ".join(mentions) if mentions else None,
        "urls": ", ".join(urls) if urls else None,
        "posted_at": posted_at,
        "language": language,
        "is_reply": is_reply,
        "reply_to_post_id": reply_to_post_id,
        "engagement_score": likes + retweets + replies + quotes,
        "raw_json": json.dumps(enriched, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Twitter / X ingestion paths
# ---------------------------------------------------------------------------


def _twitter_api_v2_search(hashtag: str, bearer_token: str) -> List[Dict[str, Any]]:
    """Search recent tweets via Twitter API v2 (requests-based).

    Args:
        hashtag: Hashtag to search (including ``#``).
        bearer_token: X API v2 bearer token.

    Returns:
        List of ``social_posts`` row dicts.
    """
    posts: List[Dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {bearer_token}"}
    query = urllib.parse.quote(f"{hashtag} -is:retweet lang:en")
    url = (
        "https://api.twitter.com/2/tweets/search/recent?"
        f"query={query}&max_results=100&"
        "tweet.fields=created_at,public_metrics,lang,referenced_tweets,entities,author_id&"
        "expansions=author_id&"
        "user.fields=username,public_metrics,verified"
    )

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 429:
            logger.warning("Twitter API v2 rate limited for %s", hashtag)
            return posts
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Twitter API v2 request failed for %s: %s", hashtag, exc)
        return posts
    except json.JSONDecodeError as exc:
        logger.warning("Twitter API v2 JSON error for %s: %s", hashtag, exc)
        return posts

    tweets = data.get("data", [])
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    for tweet in tweets:
        try:
            author_id = tweet.get("author_id", "")
            user = users.get(author_id, {})
            metrics = tweet.get("public_metrics", {})

            ref_tweets = tweet.get("referenced_tweets", [])
            is_reply = any(r.get("type") == "replied_to" for r in ref_tweets)
            reply_to_id = next(
                (r.get("id") for r in ref_tweets if r.get("type") == "replied_to"),
                None,
            )

            user_metrics = user.get("public_metrics", {})

            post = _build_social_post(
                platform="twitter",
                post_id=tweet.get("id", ""),
                author_handle=user.get("username", author_id),
                author_name=user.get("name"),
                content=tweet.get("text", ""),
                posted_at=tweet.get("created_at"),
                language=tweet.get("lang"),
                is_reply=is_reply,
                reply_to_post_id=reply_to_id,
                likes=metrics.get("like_count", 0),
                retweets=metrics.get("retweet_count", 0),
                replies=metrics.get("reply_count", 0),
                quotes=metrics.get("quote_count", 0),
                follower_count=user_metrics.get("followers_count"),
                verified=user.get("verified", False),
                raw_data={"tweet": tweet, "user": user},
            )
            posts.append(post)
        except Exception as exc:
            logger.warning("Error parsing Twitter API tweet: %s", exc)
            continue

    logger.info("Twitter API v2 returned %d posts for %s", len(posts), hashtag)
    return posts


def _twitter_tweepy_search(hashtag: str, bearer_token: str) -> List[Dict[str, Any]]:
    """Search recent tweets via ``tweepy`` (API v2 Client).

    Args:
        hashtag: Hashtag to search.
        bearer_token: X API v2 bearer token.

    Returns:
        List of ``social_posts`` row dicts.
    """
    if tweepy is None:
        return []

    posts: List[Dict[str, Any]] = []
    try:
        client = tweepy.Client(
            bearer_token=bearer_token,
            wait_on_rate_limit=True,
        )
        query = f"{hashtag} -is:retweet lang:en"
        paginator = tweepy.Paginator(
            client.search_recent_tweets,
            query=query,
            tweet_fields=[
                "created_at",
                "public_metrics",
                "lang",
                "referenced_tweets",
                "entities",
                "author_id",
            ],
            expansions=["author_id"],
            user_fields=["username", "public_metrics", "verified"],
            max_results=100,
        )
        # Build user lookup from the *last* response in the paginator
        # (tweepy Paginator does not expose expansions per-item easily)
        user_map: Dict[str, Any] = {}
        tweets: List[Any] = []
        for response in paginator:
            if response.includes and response.includes.get("users"):
                for u in response.includes["users"]:
                    user_map[str(u.id)] = u
            if response.data:
                tweets.extend(response.data)
            if len(tweets) >= 300:
                break

        for tweet in tweets:
            user = user_map.get(str(tweet.author_id), {})
            metrics = tweet.public_metrics or {}
            is_reply = False
            reply_to_id: Optional[str] = None
            if tweet.referenced_tweets:
                for r in tweet.referenced_tweets:
                    if r.type == "replied_to":
                        is_reply = True
                        reply_to_id = str(r.id)

            user_metrics = user.public_metrics or {} if hasattr(user, "public_metrics") else {}
            post = _build_social_post(
                platform="twitter",
                post_id=str(tweet.id),
                author_handle=getattr(user, "username", str(tweet.author_id)),
                author_name=getattr(user, "name", None),
                content=tweet.text or "",
                posted_at=tweet.created_at.isoformat() if tweet.created_at else None,
                language=tweet.lang,
                is_reply=is_reply,
                reply_to_post_id=reply_to_id,
                likes=metrics.get("like_count", 0),
                retweets=metrics.get("retweet_count", 0),
                replies=metrics.get("reply_count", 0),
                quotes=metrics.get("quote_count", 0),
                follower_count=user_metrics.get("followers_count"),
                verified=getattr(user, "verified", False),
                raw_data={"tweet_id": str(tweet.id)},
            )
            posts.append(post)
    except Exception as exc:
        logger.warning("tweepy search failed for %s: %s", hashtag, exc)

    logger.info("tweepy returned %d posts for %s", len(posts), hashtag)
    return posts


def _twitter_nitter_scrape(hashtag: str) -> List[Dict[str, Any]]:
    """Scrape Twitter via Nitter (no authentication required).

    Args:
        hashtag: Hashtag to search (including ``#``).

    Returns:
        List of ``social_posts`` row dicts.
    """
    posts: List[Dict[str, Any]] = []
    nitter_instances = [
        "https://nitter.net",
        "https://nitter.it",
        "https://nitter.cz",
    ]

    for instance in nitter_instances:
        url = f"{instance}/search?f=tweets&q={urllib.parse.quote(hashtag)}"
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux arm64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    )
                },
                timeout=30,
            )
            if resp.status_code != 200:
                continue
        except requests.RequestException:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        timeline = soup.find("div", class_="timeline")
        if not timeline:
            continue

        for item in timeline.find_all("div", class_="timeline-item"):
            try:
                tweet_link = item.find("a", class_="tweet-link")
                if not tweet_link:
                    continue
                href = tweet_link.get("href", "")
                post_id = href.split("/")[-1] if "/" in href else href

                user_el = item.find("a", class_="username")
                author_handle = user_el.get_text(strip=True) if user_el else "unknown"

                content_el = item.find("div", class_="tweet-content")
                content = content_el.get_text(strip=True) if content_el else ""

                posted_at: Optional[str] = None
                date_el = item.find("span", class_="tweet-date")
                if date_el and date_el.find("a"):
                    title = date_el.find("a").get("title", "")
                    if title:
                        try:
                            dt = datetime.strptime(title, "%b %d, %Y · %I:%M %p %Z")
                            posted_at = dt.isoformat()
                        except ValueError:
                            posted_at = title

                likes = retweets = replies = quotes = 0
                stats = item.find("div", class_="tweet-stats")
                if stats:
                    for stat in stats.find_all("div", class_="stat"):
                        label = stat.get_text(strip=True).lower()
                        val_container = stat.find("div", class_="icon-container")
                        val = 0
                        if val_container:
                            try:
                                val = int(
                                    val_container.get_text(strip=True).replace(",", "")
                                )
                            except ValueError:
                                pass
                        if "like" in label or "heart" in label:
                            likes = val
                        elif "retweet" in label:
                            retweets = val
                        elif "reply" in label:
                            replies = val
                        elif "quote" in label:
                            quotes = val

                posts.append(
                    _build_social_post(
                        platform="twitter",
                        post_id=post_id,
                        author_handle=author_handle,
                        author_name=None,
                        content=content,
                        posted_at=posted_at,
                        language=None,
                        is_reply=False,
                        reply_to_post_id=None,
                        likes=likes,
                        retweets=retweets,
                        replies=replies,
                        quotes=quotes,
                        follower_count=None,
                        verified=False,
                        raw_data={"nitter_href": href, "nitter_instance": instance},
                    )
                )
            except Exception as exc:
                logger.debug("Error parsing Nitter item: %s", exc)
                continue

        if posts:
            logger.info(
                "Nitter (%s) returned %d posts for %s", instance, len(posts), hashtag
            )
            break

    return posts


def _twitter_snscrape_search(hashtag: str) -> List[Dict[str, Any]]:
    """Search Twitter via ``snscrape`` (no authentication required).

    Args:
        hashtag: Hashtag to search.

    Returns:
        List of ``social_posts`` row dicts.
    """
    if sn_twitter is None:
        return []

    posts: List[Dict[str, Any]] = []
    try:
        scraper = sn_twitter.TwitterSearchScraper(f"{hashtag} lang:en")
        for tweet in scraper.get_items():
            try:
                post = _build_social_post(
                    platform="twitter",
                    post_id=str(tweet.id),
                    author_handle=tweet.user.username,
                    author_name=tweet.user.displayname,
                    content=tweet.rawContent or "",
                    posted_at=tweet.date.isoformat() if tweet.date else None,
                    language=None,
                    is_reply=tweet.inReplyToTweetId is not None,
                    reply_to_post_id=str(tweet.inReplyToTweetId)
                    if tweet.inReplyToTweetId
                    else None,
                    likes=tweet.likeCount or 0,
                    retweets=tweet.retweetCount or 0,
                    replies=tweet.replyCount or 0,
                    quotes=tweet.quoteCount or 0,
                    follower_count=tweet.user.followersCount,
                    verified=tweet.user.verified,
                    raw_data={
                        "url": str(tweet.url),
                        "conversation_id": str(tweet.conversationId),
                    },
                )
                posts.append(post)
                if len(posts) >= 100:
                    break
            except Exception as exc:
                logger.debug("Error parsing snscrape tweet: %s", exc)
                continue
    except Exception as exc:
        logger.warning("snscrape search failed for %s: %s", hashtag, exc)

    logger.info("snscrape returned %d posts for %s", len(posts), hashtag)
    return posts


def fetch_twitter_posts(hashtag: str) -> List[Dict[str, Any]]:
    """Fetch Twitter posts for a hashtag with cascading fallback.

    Priority:
        1. Twitter API v2 via ``requests`` (if ``X_BEARER_TOKEN`` set).
        2. ``tweepy`` Client (if installed & token set).
        3. ``snscrape`` (no auth).
        4. Nitter scraping (no auth).

    Args:
        hashtag: Hashtag to search.

    Returns:
        List of ``social_posts`` row dicts.
    """
    posts: List[Dict[str, Any]] = []

    if X_BEARER_TOKEN:
        logger.info("Trying Twitter API v2 (requests) for %s", hashtag)
        posts = _twitter_api_v2_search(hashtag, X_BEARER_TOKEN)
        if posts:
            return posts

        if tweepy is not None:
            logger.info("Trying Twitter API v2 (tweepy) for %s", hashtag)
            posts = _twitter_tweepy_search(hashtag, X_BEARER_TOKEN)
            if posts:
                return posts

    logger.info("Trying snscrape fallback for %s", hashtag)
    posts = _twitter_snscrape_search(hashtag)
    if posts:
        return posts

    logger.info("Trying Nitter fallback for %s", hashtag)
    posts = _twitter_nitter_scrape(hashtag)
    return posts


# ---------------------------------------------------------------------------
# TikTok ingestion path
# ---------------------------------------------------------------------------


def _tiktok_api_search(hashtag: str) -> List[Dict[str, Any]]:
    """Search TikTok for posts via the Research API.

    Args:
        hashtag: Hashtag to search (including ``#``).

    Returns:
        List of ``social_posts`` row dicts.  Empty if credentials are missing
        or the API call fails.
    """
    posts: List[Dict[str, Any]] = []

    if not TIKTOK_CLIENT_KEY:
        logger.warning(
            "TIKTOK_CLIENT_KEY not configured; skipping TikTok search for %s",
            hashtag,
        )
        return posts

    # TikTok Research API requires OAuth2 client-credentials flow.
    # The stub below attempts the flow; if it fails we log and return empty.
    logger.info(
        "Attempting TikTok Research API for %s (stub: full OAuth2 may be required)",
        hashtag,
    )

    try:
        # OAuth2 token endpoint (Research API)
        token_resp = requests.post(
            "https://open-api.tiktok.com/oauth/access_token/",
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("data", {}).get("access_token")
        if not access_token:
            logger.warning("TikTok OAuth2 returned no access_token")
            return posts

        # Search videos by hashtag (simplified endpoint)
        search_resp = requests.get(
            "https://open-api.tiktok.com/research/hashtag/videos/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "hashtag": hashtag.lstrip("#"),
                "max_count": 50,
            },
            timeout=30,
        )
        if search_resp.status_code == 404:
            logger.warning("TikTok Research endpoint returned 404 — likely changed")
            return posts
        search_resp.raise_for_status()
        search_data = search_resp.json()

        for item in search_data.get("data", {}).get("videos", []):
            try:
                post = _build_social_post(
                    platform="tiktok",
                    post_id=item.get("video_id", ""),
                    author_handle=item.get("author", {}).get("username", ""),
                    author_name=item.get("author", {}).get("nickname"),
                    content=item.get("title", ""),
                    posted_at=item.get("create_time"),
                    language=None,
                    is_reply=False,
                    reply_to_post_id=None,
                    likes=item.get("like_count", 0),
                    retweets=item.get("share_count", 0),
                    replies=item.get("comment_count", 0),
                    quotes=0,
                    follower_count=item.get("author", {}).get("follower_count"),
                    verified=item.get("author", {}).get("verified", False),
                    raw_data={"tiktok_video": item},
                )
                posts.append(post)
            except Exception as exc:
                logger.debug("Error parsing TikTok item: %s", exc)
                continue

        logger.info("TikTok API returned %d posts for %s", len(posts), hashtag)
    except requests.RequestException as exc:
        logger.warning("TikTok API request failed for %s: %s", hashtag, exc)
    except Exception as exc:
        logger.warning("TikTok API error for %s: %s", hashtag, exc)

    return posts


def fetch_tiktok_posts(hashtag: str) -> List[Dict[str, Any]]:
    """Fetch TikTok posts for a hashtag.

    Args:
        hashtag: Hashtag to search.

    Returns:
        List of ``social_posts`` row dicts.
    """
    return _tiktok_api_search(hashtag)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run() -> int:
    """Main entry point: monitor social platforms for watchlist hashtags.

    Returns:
        Number of newly inserted posts.
    """
    logger.info("Starting social monitor run")
    init_db()

    if not X_BEARER_TOKEN and sn_twitter is None:
        logger.warning(
            "No X_BEARER_TOKEN configured and snscrape unavailable; "
            "Twitter/X monitoring will be skipped"
        )

    all_posts: List[Dict[str, Any]] = []

    for hashtag in WATCHLIST_HASHTAGS:
        try:
            tw_posts = fetch_twitter_posts(hashtag)
            all_posts.extend(tw_posts)
            _sleep_between_requests()

            tt_posts = fetch_tiktok_posts(hashtag)
            all_posts.extend(tt_posts)
            _sleep_between_requests()
        except Exception as exc:
            logger.exception("Unhandled exception monitoring %s: %s", hashtag, exc)

    inserted = insert_many("social_posts", all_posts)
    logger.info(
        "Social monitor complete: %d total posts, %d inserted",
        len(all_posts),
        inserted,
    )
    return inserted


if __name__ == "__main__":
    run()
