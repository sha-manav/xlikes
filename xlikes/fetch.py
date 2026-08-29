"""Pull your Likes timeline using your own logged-in browser session.

X's API no longer exposes liked_tweets on free tiers, so we drive a real
browser and read the JSON the Likes page already fetches for itself. Nothing
leaves your machine: the session cookie lives in a local Chromium profile and
the posts land in a local SQLite file.
"""

from __future__ import annotations

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


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FetchError(
            "Playwright isn't installed. Run:\n"
            "    pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def _wait_for_login(page, timeout_s: int = 300) -> None:
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    deadline = datetime.now(timezone.utc).timestamp() + timeout_s
    warned = False
    while "/login" in page.url or "/i/flow/" in page.url:
        if not warned:
            print("\n  Log in to X in the browser window that just opened.")
            print("  Waiting… (the session is saved, so this is a one-time step)\n")
            warned = True
        if datetime.now(timezone.utc).timestamp() > deadline:
            raise FetchError("timed out waiting for login")
        page.wait_for_timeout(2000)
        if "/login" in page.url or "/i/flow/" in page.url:
            try:
                page.goto("https://x.com/home", wait_until="domcontentloaded")
            except Exception:
                pass


def _discover_handle(page) -> str | None:
    for selector in ('a[data-testid="AppTabBar_Profile_Link"]', 'a[aria-label="Profile"]'):
        try:
            href = page.get_attribute(selector, "href", timeout=5000)
        except Exception:
            continue
        if href:
            return href.strip("/").split("/")[0]
    return None


def fetch_likes(
    conn,
    handle: str | None = None,
    max_posts: int = 600,
    headless: bool = False,
    profile_dir: Path | None = None,
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
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)

        _wait_for_login(page)
        handle = (handle or _discover_handle(page) or "").lstrip("@")
        if not handle:
            context.close()
            raise FetchError("couldn't work out your handle — pass --handle yourhandle")

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
            "No likes captured. Check that the Likes tab actually loaded, and that "
            "you're logged in as the account that owns them."
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
