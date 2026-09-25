"""Pull your Likes timeline using your own logged-in browser session.

X's API no longer exposes liked_tweets on free tiers, so we drive a real
browser and read the JSON the Likes page already fetches for itself. Nothing
leaves your machine: the session cookie lives in a local Chromium profile and
the posts land in a local SQLite file.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .parse import extract_tweets

PROFILE_DIR = Path.home() / ".xlikes" / "browser-profile"


def is_likes_response(url: str) -> bool:
    """Is this the GraphQL call backing the Likes tab?

    Real shapes: /i/api/graphql/<hash>/Likes and /graphql/<hash>/Likes, with an
    optional ?variables=... query. Matching any like-ish operation name keeps
    this working when X renames the endpoint, which it does.
    """
    if "/graphql/" not in url:
        return False
    operation = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
    return "like" in operation and "favorite" not in operation


def ingest(collected: dict, payload) -> int:
    """Merge one GraphQL response into the ordered like map. Insertion order is
    timeline order, which is the order you liked things — newest first."""
    before = len(collected)
    for rec in extract_tweets(payload):
        collected.setdefault(rec["id"], rec)
    return len(collected) - before


class FetchError(RuntimeError):
    pass


# Playwright's own Chromium is a ~140MB download that likes to stall. Falling
# back to a browser you already have avoids blocking on it.
BROWSER_CHANNELS = (None, "chrome", "msedge")
CHANNEL_NAMES = {None: "Playwright's Chromium", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}


def _launch(p, profile_dir: Path, headless: bool, channel: str | None):
    """Own profile dir per browser: Chrome and Chromium don't share cleanly."""
    target = profile_dir / (channel or "chromium")
    target.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(target),
        headless=headless,
        channel=channel,
        viewport={"width": 1280, "height": 950},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _launch_any(p, profile_dir: Path, headless: bool, channel: str | None, verbose: bool):
    attempts = [channel] if channel else list(BROWSER_CHANNELS)
    failures = []
    for candidate in attempts:
        try:
            context = _launch(p, profile_dir, headless, candidate)
        except Exception as exc:
            failures.append(f"  {CHANNEL_NAMES.get(candidate, candidate)}: {str(exc).splitlines()[0][:120]}")
            continue
        if verbose and candidate:
            print(f"  Using {CHANNEL_NAMES.get(candidate, candidate)}.")
        return context
    raise FetchError(
        "Couldn't start a browser. Tried:\n"
        + "\n".join(failures)
        + "\n\nEither finish the Chromium download:\n"
        "    python3 -m playwright install chromium\n"
        "or install Google Chrome and re-run — it'll be picked up automatically."
    )


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FetchError(
            "Playwright isn't installed. Run:\n"
            "    pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def is_logged_in(cookies) -> bool:
    """X sets auth_token on a real session.

    URL checks aren't enough: a logged-out visit to /home lands on the marketing
    splash at x.com, not on anything named /login, so a missing session used to
    sail straight through and fail later with a confusing error.
    """
    return any(c.get("name") == "auth_token" and c.get("value") for c in cookies)


def _wait_for_login(context, page, timeout_s: int = 300) -> None:
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    if is_logged_in(context.cookies()):
        return

    print("\n  Not signed in yet — log in to X in the browser window that just opened.")
    print("  Waiting… (the session is saved, so this is a one-time step)\n")
    deadline = datetime.now(timezone.utc).timestamp() + timeout_s
    while datetime.now(timezone.utc).timestamp() < deadline:
        page.wait_for_timeout(2000)
        if is_logged_in(context.cookies()):
            print("  Signed in.")
            try:  # reload so the logged-in nav (and your handle) renders
                page.goto("https://x.com/home", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception:
                pass
            return
    raise FetchError("timed out waiting for login")


def handle_from_text(text: str | None) -> str | None:
    """Pull @handle out of the account switcher's label."""
    if not text:
        return None
    match = re.search(r"@([A-Za-z0-9_]{1,15})", text)
    return match.group(1) if match else None


def _discover_handle(page, attempts: int = 3) -> str | None:
    """Read your own handle off the logged-in chrome of the page.

    Several routes, because which ones render depends on window size and on
    whichever markup X is shipping this week.
    """
    for attempt in range(attempts):
        for selector in ('a[data-testid="AppTabBar_Profile_Link"]', 'a[aria-label="Profile"]'):
            try:
                href = page.get_attribute(selector, "href", timeout=4000)
            except Exception:
                continue
            if href and href.strip("/"):
                return href.strip("/").split("/")[0]
        for selector in ('[data-testid="SideNav_AccountSwitcher_Button"]', 'header[role="banner"]'):
            try:
                handle = handle_from_text(page.inner_text(selector, timeout=4000))
            except Exception:
                continue
            if handle:
                return handle
        if attempt + 1 < attempts:
            page.wait_for_timeout(2000)
    return None


def fetch_likes(
    conn,
    handle: str | None = None,
    max_posts: int = 600,
    headless: bool = False,
    profile_dir: Path | None = None,
    channel: str | None = None,
    scroll_pause_ms: int = 1400,
    verbose: bool = True,
) -> dict:
    sync_playwright = _require_playwright()
    profile_dir = Path(profile_dir or PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)

    collected: dict[str, dict] = {}   # tweet id -> record, insertion order == like order
    errors: list[str] = []

    def on_response(response):
        if not is_likes_response(response.url):
            return
        try:
            payload = response.json()
        except Exception as exc:
            errors.append(f"unreadable response: {exc}")
            return
        ingest(collected, payload)

    with sync_playwright() as p:
        context = _launch_any(p, profile_dir, headless, channel, verbose)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)

        _wait_for_login(context, page)
        handle = (handle or _discover_handle(page) or "").lstrip("@")
        if not handle:
            context.close()
            raise FetchError(
                "Signed in, but couldn't read your handle off the page.\n"
                "Pass it explicitly:  xlikes fetch --handle yourhandle"
            )

        if verbose:
            print(f"  Reading likes for @{handle} (target: {max_posts} posts)")
        page.goto(f"https://x.com/{handle}/likes", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        stalls, previous = 0, 0
        while len(collected) < max_posts and stalls < 6:
            page.keyboard.press("End")
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(scroll_pause_ms)
            count = len(collected)
            if count == previous:
                stalls += 1
                page.wait_for_timeout(scroll_pause_ms)  # let a slow response land
            else:
                stalls, previous = 0, count
                if verbose:
                    print(f"\r  {count} posts…", end="", flush=True)
        if verbose:
            print(f"\r  {len(collected)} posts collected." + " " * 12)
        context.close()

    if not collected:
        raise FetchError(
            f"No likes captured from x.com/{handle}/likes.\n"
            "  - Is that the right handle? Pass --handle to set it explicitly.\n"
            "  - You can only read your own likes, so it must be the account you "
            "logged in as.\n"
            "  - If the tab was still loading, just run it again."
            + (f"\nResponse errors: {errors[0]}" if errors else "")
        )

    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for rank, rec in enumerate(collected.values()):
        rec["like_rank"] = rank
        stats[db.upsert(conn, rec)] += 1
    db.set_meta(conn, "handle", handle)
    db.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat())
    db.set_meta(conn, "last_fetch_count", len(collected))
    conn.commit()
    stats["total"] = len(collected)
    return stats


# --- profile timelines ------------------------------------------------------


def is_user_timeline_response(url: str) -> bool:
    """Is this the GraphQL call backing a profile's posts or replies tab?

    Covers UserTweets, UserTweetsAndReplies and the UserWithProfileTweets…
    variants, without matching Likes, HomeTimeline or search.
    """
    if "/graphql/" not in url:
        return False
    operation = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
    return "user" in operation and "tweet" in operation


def _oldest(records: dict) -> str | None:
    dates = [r["created_at"] for r in records.values() if r.get("created_at")]
    return min(dates) if dates else None


def fetch_user_posts(
    conn,
    handle: str,
    max_posts: int = 2000,
    since: str | None = None,
    include_replies: bool = True,
    headless: bool = False,
    profile_dir: Path | None = None,
    channel: str | None = None,
    scroll_pause_ms: int = 1500,
    verbose: bool = True,
) -> dict:
    """Walk a profile's Posts and Replies tabs, recording engagement counts.

    `since` (ISO8601) stops scrolling once the timeline passes it — profile
    timelines are reverse-chronological, so there's no need to walk the rest.
    """
    from .parse import extract_timeline_posts

    sync_playwright = _require_playwright()
    profile_dir = Path(profile_dir or PROFILE_DIR)
    target = handle.lstrip("@").lower()

    collected: dict[str, dict] = {}
    others = 0  # conversation context by other accounts, deliberately dropped
    errors: list[str] = []

    def on_response(response):
        nonlocal others
        if not is_user_timeline_response(response.url):
            return
        try:
            payload = response.json()
        except Exception as exc:
            errors.append(f"unreadable response: {exc}")
            return
        for rec in extract_timeline_posts(payload):
            if rec["handle"] != target:
                others += 1
                continue
            collected[rec["id"]] = rec  # last write wins: freshest counts

    with sync_playwright() as p:
        context = _launch_any(p, profile_dir, headless, channel, verbose)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        _wait_for_login(context, page)

        tabs = [("posts", f"https://x.com/{target}")]
        if include_replies:
            tabs.append(("replies", f"https://x.com/{target}/with_replies"))

        for label, url in tabs:
            if verbose:
                print(f"  {label}: {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)

            stalls, previous, stop = 0, len(collected), False
            while len(collected) < max_posts and stalls < 6 and not stop:
                page.keyboard.press("End")
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(scroll_pause_ms)
                count = len(collected)
                if count == previous:
                    stalls += 1
                    page.wait_for_timeout(scroll_pause_ms)
                else:
                    stalls, previous = 0, count
                    if verbose:
                        print(f"\r  {count} posts…", end="", flush=True)
                if since and (oldest := _oldest(collected)) and oldest < since:
                    stop = True  # timeline is newest-first; we're past the cutoff
            if verbose:
                print(f"\r  {label}: {len(collected)} total so far." + " " * 12)
        context.close()

    if not collected:
        raise FetchError(
            f"No posts captured from x.com/{target}.\n"
            "  - Check the handle is spelled right.\n"
            "  - A protected (locked) account is only visible to approved followers.\n"
            "  - A suspended or renamed account won't load at all."
            + (f"\nResponse errors: {errors[0]}" if errors else "")
        )

    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for rec in collected.values():
        stats[db.upsert_post(conn, rec)] += 1
    conn.commit()
    stats["total"] = len(collected)
    stats["skipped_other_authors"] = others
    stats["oldest"] = _oldest(collected)
    stats["missing_views"] = sum(1 for r in collected.values() if r.get("views") is None)
    return stats
