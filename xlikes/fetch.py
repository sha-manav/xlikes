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


# What x.com renders instead of a timeline. Checked as page text because these
# states return HTTP 200 with no timeline request at all.
PROFILE_MARKERS = (
    ("missing", ("this account doesn\u2019t exist", "this account doesn't exist")),
    ("suspended", ("account suspended", "this account is suspended")),
    ("protected", ("these posts are protected", "this account's posts are protected")),
    ("restricted", ("caution: this account is temporarily restricted",)),
    ("login_wall", ("sign in to x", "don\u2019t miss what\u2019s happening")),
)

PROFILE_DIAGNOSIS = {
    "missing": "That account doesn't exist — check the spelling, or it was renamed or deleted.",
    "suspended": "That account is suspended, so X serves no posts for it.",
    "protected": "That account is protected — only approved followers can read it.",
    "restricted": "X has temporarily restricted that account, which hides the timeline.",
    "login_wall": "X showed a logged-out page. The session may have expired; "
                  "re-run and sign in when the window opens.",
}


def profile_state(page_text: str | None) -> str:
    """Classify a profile page from its visible text: 'ok', or why it's empty."""
    low = (page_text or "").lower()
    for state, markers in PROFILE_MARKERS:
        if any(marker in low for marker in markers):
            return state
    return "ok"


def graphql_operation(url: str) -> str | None:
    """The operation name from a GraphQL URL, for diagnostics."""
    if "/graphql/" not in url:
        return None
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def collect_posts(payload, target: str, collected: dict, seen_handles: dict) -> int:
    """Keep the target's posts from one payload; tally whose posts we skipped.

    Deliberately indifferent to which endpoint the payload came from: X renames
    its GraphQL operations, and a name-based filter silently captures nothing
    when it guesses wrong. Author identity is the real test.
    """
    from .parse import extract_timeline_posts

    added = 0
    for rec in extract_timeline_posts(payload):
        handle = rec.get("handle") or "?"
        seen_handles[handle] = seen_handles.get(handle, 0) + 1
        if handle != target:
            continue
        if rec["id"] not in collected:
            added += 1
        collected[rec["id"]] = rec  # last write wins: freshest counts
    return added


def no_posts_message(target, states, seen_handles, seen_ops, errors, debug_dir=None) -> str:
    """Explain an empty result from what we actually observed.

    The three cases look identical from the outside but need different fixes:
    the profile can't be read, the handle is wrong, or the timeline never
    loaded. Saying which one is the whole point of this message.
    """
    lines = [f"No posts captured from x.com/{target}."]
    for state in dict.fromkeys(states or []):
        lines.append(f"  {PROFILE_DIAGNOSIS[state]}")

    others = {h: n for h, n in (seen_handles or {}).items() if h != target}
    if others:
        top = sorted(others.items(), key=lambda kv: -kv[1])[:6]
        lines.append(
            f"  Posts were found, but none by @{target}. Authors seen: "
            + ", ".join(f"@{h} ({n})" for h, n in top)
        )
        lines.append("  If one of those is the account you meant, re-run with that handle.")
    elif seen_ops:
        lines.append(
            "  X replied, but no posts were in the response. Operations seen: "
            + ", ".join(f"{op} x{n}" for op, n in sorted(seen_ops.items())[:8])
        )
        lines.append("  Re-run with --debug to save the raw responses and a screenshot.")
    else:
        lines.append("  No GraphQL responses at all — the page never loaded a timeline.")
        lines.append("  Re-run with --debug to save a screenshot of what the browser saw.")

    if errors:
        lines.append(f"  First response error: {errors[0]}")
    if debug_dir:
        lines.append(f"  Debug output: {debug_dir}")
    return "\n".join(lines)


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
    debug: bool = False,
) -> dict:
    """Walk a profile's Posts and Replies tabs, recording engagement counts.

    `since` (ISO8601) stops scrolling once the timeline passes it — profile
    timelines are reverse-chronological, so there's no need to walk the rest.
    """
    sync_playwright = _require_playwright()
    profile_dir = Path(profile_dir or PROFILE_DIR)
    target = handle.lstrip("@").lower()

    collected: dict[str, dict] = {}
    seen_handles: dict[str, int] = {}
    seen_ops: dict[str, int] = {}
    errors: list[str] = []
    debug_dir = None
    if debug:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        debug_dir = Path.home() / ".xlikes" / "debug" / f"{target}-{stamp}"
        debug_dir.mkdir(parents=True, exist_ok=True)

    def on_response(response):
        operation = graphql_operation(response.url)
        if operation is None:
            return
        seen_ops[operation] = seen_ops.get(operation, 0) + 1
        try:
            payload = response.json()
        except Exception as exc:
            errors.append(f"{operation}: unreadable response ({exc})")
            return
        added = collect_posts(payload, target, collected, seen_handles)
        if debug_dir and added:
            import json as _json

            path = debug_dir / f"{operation}-{seen_ops[operation]}.json"
            path.write_text(_json.dumps(payload, indent=1)[:4_000_000])

    with sync_playwright() as p:
        context = _launch_any(p, profile_dir, headless, channel, verbose)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        _wait_for_login(context, page)

        states: list[str] = []
        tabs = [("posts", f"https://x.com/{target}")]
        if include_replies:
            tabs.append(("replies", f"https://x.com/{target}/with_replies"))

        for label, url in tabs:
            if verbose:
                print(f"  {label}: {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)

            try:
                state = profile_state(page.inner_text("body", timeout=5000))
            except Exception:
                state = "ok"
            if state in ("missing", "suspended"):
                if debug_dir:
                    page.screenshot(path=str(debug_dir / f"{label}.png"), full_page=False)
                context.close()
                raise FetchError(f"x.com/{target}: {PROFILE_DIAGNOSIS[state]}")
            if state != "ok":
                states.append(state)

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
        if debug_dir:
            page.screenshot(path=str(debug_dir / "final.png"), full_page=False)
        context.close()

    if not collected:
        raise FetchError(
            no_posts_message(target, states, seen_handles, seen_ops, errors, debug_dir)
        )

    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for rec in collected.values():
        stats[db.upsert_post(conn, rec)] += 1
    conn.commit()
    stats["total"] = len(collected)
    stats["skipped_other_authors"] = sum(
        n for h, n in seen_handles.items() if h != target
    )
    stats["oldest"] = _oldest(collected)
    stats["missing_views"] = sum(1 for r in collected.values() if r.get("views") is None)
    stats["operations"] = seen_ops
    stats["debug_dir"] = str(debug_dir) if debug_dir else None
    return stats
