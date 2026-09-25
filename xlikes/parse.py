"""Turn X GraphQL timeline JSON into flat like records.

X reshapes these payloads without warning, so everything here is defensive:
we locate tweets structurally rather than by a hard-coded path, and every
field lookup falls back through the shapes X has used.
"""

from __future__ import annotations

from datetime import datetime, timezone

TWEET_TYPES = {"Tweet", "TweetWithVisibilityResults"}
# Subtrees that hold *other* people's tweets, not the one that was liked.
NESTED_KEYS = {"quoted_status_result", "retweeted_status_result", "quoted_status"}


def _unwrap(node):
    """TweetWithVisibilityResults wraps the real tweet one level down."""
    while isinstance(node, dict) and node.get("__typename") == "TweetWithVisibilityResults":
        node = node.get("tweet") or {}
    return node if isinstance(node, dict) else None


def _is_tweet(node) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("__typename") in TWEET_TYPES:
        return True
    # Older payloads omit __typename but always carry these two.
    return "rest_id" in node and "legacy" in node


def parse_created_at(raw: str | None) -> str | None:
    """'Wed Oct 10 20:19:24 +0000 2018' -> '2018-10-10T20:19:24+00:00'."""
    if not raw:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _user(tweet: dict) -> tuple[str | None, str | None]:
    """Return (handle, display name), trying each shape X has shipped."""
    user = (
        tweet.get("core", {}).get("user_results", {}).get("result")
        or tweet.get("author", {}).get("user_results", {}).get("result")
        or {}
    )
    for src in (user.get("core") or {}, user.get("legacy") or {}, user):
        handle = src.get("screen_name")
        if handle:
            return handle, src.get("name")
    return None, None


def _full_text(tweet: dict) -> str:
    """Prefer the untruncated long-form body over the 280-char legacy text."""
    note = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    legacy = tweet.get("legacy") or {}
    text = note or legacy.get("full_text") or tweet.get("full_text") or ""
    # Strip the trailing t.co self-link X appends to quote tweets and media posts.
    for url in (legacy.get("entities") or {}).get("urls") or []:
        if url.get("url") and url.get("expanded_url", "").startswith("https://x.com/"):
            text = text.replace(url["url"], url.get("expanded_url", ""))
    return text.strip()


def _urls(tweet: dict) -> list[str]:
    entities = (tweet.get("legacy") or {}).get("entities") or {}
    out = []
    for url in entities.get("urls") or []:
        expanded = url.get("expanded_url") or url.get("url")
        if expanded:
            out.append(expanded)
    return out


def _article_title(tweet: dict) -> str | None:
    article = tweet.get("article") or {}
    result = article.get("article_results", {}).get("result") or article
    return result.get("title") or result.get("preview_text")


def _looks_like_article(tweet: dict, urls: list[str]) -> bool:
    """X Articles are long-form posts; they surface as an `article` object or
    as an /i/article/ link when quoted from another client."""
    if tweet.get("article") or tweet.get("articleResults"):
        return True
    if tweet.get("note_tweet"):  # long-form post
        return True
    return any("/i/article/" in u or "/article/" in u for u in urls)


def _has_media(tweet: dict) -> bool:
    legacy = tweet.get("legacy") or {}
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    return bool(entities.get("media"))


def tweet_to_record(node: dict) -> dict | None:
    """Flatten one tweet result (plus its quoted post) into a like record."""
    tweet = _unwrap(node)
    if not tweet:
        return None
    tweet_id = tweet.get("rest_id") or (tweet.get("legacy") or {}).get("id_str")
    if not tweet_id:
        return None

    legacy = tweet.get("legacy") or {}
    handle, name = _user(tweet)
    urls = _urls(tweet)
    text = _full_text(tweet)

    rec = {
        "id": str(tweet_id),
        "url": f"https://x.com/{handle or 'i'}/status/{tweet_id}",
        "author_handle": handle,
        "author_name": name,
        "text": text,
        "created_at": parse_created_at(legacy.get("created_at")),
        "like_rank": None,
        "is_quote": 0,
        "quoted_id": None,
        "quoted_handle": None,
        "quoted_name": None,
        "quoted_text": None,
        "quoted_url": None,
        "has_article": int(_looks_like_article(tweet, urls)),
        "has_media": int(_has_media(tweet)),
        "urls": "\n".join(urls),
        "source": "fetch",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    quoted = _unwrap((tweet.get("quoted_status_result") or {}).get("result"))
    if quoted:
        q_handle, q_name = _user(quoted)
        q_id = quoted.get("rest_id") or (quoted.get("legacy") or {}).get("id_str")
        q_urls = _urls(quoted)
        q_text = _full_text(quoted)
        title = _article_title(quoted)
        rec.update(
            is_quote=1,
            quoted_id=str(q_id) if q_id else None,
            quoted_handle=q_handle,
            quoted_name=q_name,
            # The article title is searchable content in its own right.
            quoted_text="\n".join(p for p in (title, q_text) if p),
            quoted_url=f"https://x.com/{q_handle or 'i'}/status/{q_id}" if q_id else None,
        )
        if _looks_like_article(quoted, q_urls):
            rec["has_article"] = 1
    elif legacy.get("is_quote_status") and legacy.get("quoted_status_id_str"):
        # Quoted post wasn't hydrated in this payload; record the link anyway.
        rec["is_quote"] = 1
        rec["quoted_id"] = legacy["quoted_status_id_str"]

    if title := _article_title(tweet):
        rec["text"] = "\n".join(p for p in (title, rec["text"]) if p)
    return rec


def _walk_entries(node, out: list):
    """Collect timeline entries whose entryId marks them as a liked post."""
    if isinstance(node, dict):
        entry_id = node.get("entryId")
        if isinstance(entry_id, str) and "content" in node:
            if entry_id.startswith(("tweet-", "likes-")) or "tweet" in entry_id:
                out.append(node)
                return  # don't descend: quoted posts live inside this entry
        for value in node.values():
            _walk_entries(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_entries(item, out)


def _entry_tweet(entry: dict) -> dict | None:
    """Pull the tweet result out of a timeline entry, at whatever depth."""
    found: list = []

    def walk(node, in_nested=False):
        if isinstance(node, dict):
            if not in_nested and _is_tweet(node):
                found.append(node)
                return
            for key, value in node.items():
                walk(value, in_nested or key in NESTED_KEYS)
        elif isinstance(node, list):
            for item in node:
                walk(item, in_nested)

    walk(entry.get("content"))
    return found[0] if found else None


def extract_tweets(payload) -> list[dict]:
    """Every liked post in a GraphQL response, in timeline order.

    Falls back to a whole-document scan if the entry structure changes, while
    still refusing to mistake a quoted post for a liked one.
    """
    entries: list = []
    _walk_entries(payload, entries)

    records, seen = [], set()
    for entry in entries:
        tweet = _entry_tweet(entry)
        if not tweet:
            continue
        rec = tweet_to_record(tweet)
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            records.append(rec)

    if not records:
        found: list = []

        def walk(node, in_nested=False):
            if isinstance(node, dict):
                if not in_nested and _is_tweet(node):
                    found.append(node)
                for key, value in node.items():
                    walk(value, in_nested or key in NESTED_KEYS)
            elif isinstance(node, list):
                for item in node:
                    walk(item, in_nested)

        walk(payload)
        for tweet in found:
            rec = tweet_to_record(tweet)
            if rec and rec["id"] not in seen:
                seen.add(rec["id"])
                records.append(rec)
    return records


# --- profile timelines: one account's own posts, with engagement counts ------


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def view_count(tweet: dict):
    """Views, or None when X exposes no count.

    Not every post has one: views only exist for posts from late 2022 onward,
    and the payload can say views are enabled while omitting the number.
    """
    for source in (tweet.get("views") or {}, tweet.get("ext_views") or {}):
        count = _int(source.get("count"))
        if count is not None:
            return count
    return None


def metrics(tweet: dict) -> dict:
    legacy = tweet.get("legacy") or {}
    return {
        "likes": _int(legacy.get("favorite_count")),
        "reposts": _int(legacy.get("retweet_count")),
        "replies": _int(legacy.get("reply_count")),
        "quotes": _int(legacy.get("quote_count")),
        "bookmarks": _int(legacy.get("bookmark_count")),
        "views": view_count(tweet),
    }


def post_kind(tweet: dict) -> str:
    """Most specific label first: a repost of a reply is still a repost."""
    legacy = tweet.get("legacy") or {}
    if legacy.get("retweeted_status_result") or tweet.get("retweeted_status_result"):
        return "repost"
    if legacy.get("in_reply_to_status_id_str") or legacy.get("in_reply_to_screen_name"):
        return "reply"
    if tweet.get("quoted_status_result") or legacy.get("is_quote_status"):
        return "quote"
    return "post"


def tweet_to_post(node: dict) -> dict | None:
    """Flatten a tweet into a profile-timeline row with engagement counts."""
    tweet = _unwrap(node)
    if not tweet:
        return None
    tweet_id = tweet.get("rest_id") or (tweet.get("legacy") or {}).get("id_str")
    if not tweet_id:
        return None

    legacy = tweet.get("legacy") or {}
    handle, name = _user(tweet)
    urls = _urls(tweet)
    quoted = _unwrap((tweet.get("quoted_status_result") or {}).get("result"))
    q_handle, _ = _user(quoted) if quoted else (None, None)
    q_id = None
    if quoted:
        q_id = quoted.get("rest_id") or (quoted.get("legacy") or {}).get("id_str")

    text = _full_text(tweet)
    if title := _article_title(tweet):
        text = "\n".join(p for p in (title, text) if p)

    rec = {
        "id": str(tweet_id),
        "handle": handle.lower() if handle else None,
        "author_name": name,
        "created_at": parse_created_at(legacy.get("created_at")),
        "kind": post_kind(tweet),
        "text": text,
        "url": f"https://x.com/{handle or 'i'}/status/{tweet_id}",
        "in_reply_to_handle": legacy.get("in_reply_to_screen_name"),
        "in_reply_to_id": legacy.get("in_reply_to_status_id_str"),
        "conversation_id": legacy.get("conversation_id_str"),
        "quoted_id": str(q_id) if q_id else legacy.get("quoted_status_id_str"),
        "quoted_handle": q_handle,
        "quoted_text": _full_text(quoted) if quoted else None,
        "has_media": int(_has_media(tweet)),
        "urls": "\n".join(urls),
        "lang": legacy.get("lang"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    rec.update(metrics(tweet))
    return rec


def extract_timeline_posts(payload) -> list[dict]:
    """Every post in a profile-timeline response, in document order.

    Profile timelines nest tweets inside conversation modules, so unlike the
    likes timeline there's no one entry shape to key off. We walk the whole
    document but refuse to descend into quoted/reposted subtrees, so the
    original of a repost is never mistaken for a post of its own.
    """
    found: list = []

    def walk(node, in_nested=False):
        if isinstance(node, dict):
            if not in_nested and _is_tweet(node):
                found.append(node)
                for key, value in node.items():
                    walk(value, True)  # anything under a tweet is context
                return
            for key, value in node.items():
                walk(value, in_nested or key in NESTED_KEYS)
        elif isinstance(node, list):
            for item in node:
                walk(item, in_nested)

    walk(payload)

    posts, seen = [], set()
    for tweet in found:
        rec = tweet_to_post(tweet)
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            posts.append(rec)
    return posts


# --- account profile: the denominator for "did we get everything?" -----------


def _profile_fields(user: dict) -> dict:
    """Read profile fields from whichever shape X is serving.

    Counts have stayed in `legacy`, while screen_name/name/created_at moved to
    `core` on newer payloads, so both are checked.
    """
    core = user.get("core") or {}
    legacy = user.get("legacy") or {}
    handle = core.get("screen_name") or legacy.get("screen_name")
    return {
        "handle": handle.lower() if handle else None,
        "name": core.get("name") or legacy.get("name"),
        "created_at": parse_created_at(core.get("created_at") or legacy.get("created_at")),
        # statuses_count counts posts, replies and reposts together, and drops
        # anything deleted — a ceiling to compare against, not an exact target.
        "statuses_count": _int(legacy.get("statuses_count")),
        "followers_count": _int(legacy.get("followers_count")),
        "following_count": _int(legacy.get("friends_count")),
        "description": legacy.get("description"),
        "protected": int(bool(legacy.get("protected"))),
    }


def extract_user_profile(payload, handle: str) -> dict | None:
    """The profile object for `handle`, from any payload that embeds one."""
    target = handle.lstrip("@").lower()
    best = None

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            if node.get("__typename") == "User" or (
                "rest_id" in node and "screen_name" in (node.get("legacy") or node.get("core") or {})
            ):
                fields = _profile_fields(node)
                if fields["handle"] == target:
                    # Tweets embed a thinner copy of their author; keep the
                    # richest one, which is the profile response itself.
                    if best is None or fields["statuses_count"] is not None:
                        if best is None or best["statuses_count"] is None:
                            best = fields
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return best
