"""Pull your Likes timeline using your own logged-in browser session.

X's API no longer exposes liked_tweets on free tiers, so we drive a real
browser and read the JSON the Likes page already fetches for itself. Nothing
leaves your machine: the session cookie lives in a local Chromium profile and
the posts land in a local SQLite file.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
import time
from pathlib import Path
from urllib.parse import quote

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


def collect_posts(payload, target: str, collected: dict, seen_handles: dict) -> list[dict]:
    """Keep the target's posts from one payload; tally whose posts we skipped.

    Deliberately indifferent to which endpoint the payload came from: X renames
    its GraphQL operations, and a name-based filter silently captures nothing
    when it guesses wrong. Author identity is the real test.
    """
    from .parse import extract_timeline_posts

    added = []
    for rec in extract_timeline_posts(payload):
        handle = rec.get("handle") or "?"
        seen_handles[handle] = seen_handles.get(handle, 0) + 1
        if handle != target:
            continue
        if rec["id"] not in collected:
            added.append(rec)
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


def _scroll_collect(page, count_fn, *, pause_ms=1500, max_stalls=6, verbose=False,
                    label="", stop_fn=None, max_posts=None, max_rate_limits=5) -> dict:
    """Scroll until the page stops yielding new posts.

    A stall is only believed after trying to clear a rate-limit card, because
    throttling and the true end of a timeline look the same from here.
    """
    limits: dict = {}
    stalls, previous, gave_up = 0, count_fn(), False
    while stalls < max_stalls:
        if max_posts is not None and count_fn() >= max_posts:
            break
        page.keyboard.press("End")
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(pause_ms)
        count = count_fn()
        if count == previous:
            stalls += 1
            if _recover_if_stuck(page, verbose, limits):
                stalls = max(0, stalls - 2)  # it was backpressure, not the end
                if limits.get("hits", 0) >= max_rate_limits:
                    # Still throttled after backing off repeatedly. Stop here so
                    # the caller can move on with what it has, instead of
                    # burning the remaining quota on this one tab.
                    gave_up = True
                    if verbose:
                        print(f"\r  {label}still rate limited after "
                              f"{limits['hits']} waits — moving on." + " " * 8)
                    break
            else:
                page.wait_for_timeout(pause_ms)
        else:
            stalls, previous = 0, count
            if verbose:
                print(f"\r  {label}{count} posts…", end="", flush=True)
        if stop_fn and stop_fn():
            break
    return {"count": previous, "rate_limits": limits.get("hits", 0), "gave_up": gave_up}


class Sink:
    """Writes posts to SQLite as they arrive.

    A long walk gets rate limited and interrupted often, and holding several
    thousand posts in memory until the end means one Ctrl-C throws away
    everything. Committing in batches makes every run resumable: posts are
    keyed by id, so re-running picks up where it left off.
    """

    def __init__(self, conn, batch: int = 40):
        self.conn = conn
        self.batch = batch
        self.stats = {"new": 0, "updated": 0, "unchanged": 0}
        self._uncommitted = 0

    def write(self, records) -> None:
        for rec in records:
            self.stats[db.upsert_post(self.conn, rec)] += 1
            self._uncommitted += 1
        if self._uncommitted >= self.batch:
            self.commit()

    def write_profile(self, profile: dict) -> None:
        if profile.get("handle"):
            db.upsert_account(self.conn, profile)
            self.commit()

    def commit(self) -> None:
        if self._uncommitted or True:
            self.conn.commit()
            self._uncommitted = 0


def capture_profile(payload, target: str, holder: dict) -> None:
    """Keep the richest profile read we see; only the profile response itself
    carries statuses_count, while tweets embed a thinner copy of the author."""
    from .parse import extract_user_profile

    found = extract_user_profile(payload, target)
    if not found:
        return
    if not holder or (found.get("statuses_count") is not None
                      and holder.get("statuses_count") is None):
        holder.clear()
        holder.update(found)


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
    profile: dict = {}
    seen_handles: dict[str, int] = {}
    seen_ops: dict[str, int] = {}
    errors: list[str] = []
    sink = Sink(conn)
    interrupted = False
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
        had_profile = bool(profile.get("statuses_count"))
        capture_profile(payload, target, profile)
        if profile.get("statuses_count") and not had_profile:
            sink.write_profile(profile)  # save the denominator before anything else
        added = collect_posts(payload, target, collected, seen_handles)
        sink.write(added)
        if debug_dir and added:
            import json as _json

            path = debug_dir / f"{operation}-{seen_ops[operation]}.json"
            path.write_text(_json.dumps(payload, indent=1)[:4_000_000])

    with sync_playwright() as p:
        context = _launch_any(p, profile_dir, headless, channel, verbose)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        _wait_for_login(context, page)

        try:
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

                def past_cutoff():
                    if not since:
                        return False
                    oldest = _oldest(collected)
                    return bool(oldest and oldest < since)  # timeline is newest-first

                _scroll_collect(
                    page, lambda: len(collected), pause_ms=scroll_pause_ms,
                    verbose=verbose, stop_fn=past_cutoff, max_posts=max_posts,
                )
                if verbose:
                    print(f"\r  {label}: {len(collected)} total so far." + " " * 12)
        except KeyboardInterrupt:
            interrupted = True
            print("\n  interrupted — keeping the posts captured so far.")
        if debug_dir and not interrupted:
            try:
                page.screenshot(path=str(debug_dir / "final.png"), full_page=False)
            except Exception:
                pass
        try:
            context.close()
        except Exception:
            pass

    if not collected:
        raise FetchError(
            no_posts_message(target, states, seen_handles, seen_ops, errors, debug_dir)
        )

    sink.write(list(collected.values()))  # refresh counts for repeat sightings
    sink.write_profile(profile)
    sink.commit()
    stats = dict(sink.stats)
    stats["interrupted"] = interrupted
    stats["profile"] = dict(profile)
    stats["total"] = len(collected)
    stats["skipped_other_authors"] = sum(
        n for h, n in seen_handles.items() if h != target
    )
    stats["oldest"] = _oldest(collected)
    stats["missing_views"] = sum(1 for r in collected.values() if r.get("views") is None)
    stats["operations"] = seen_ops
    stats["debug_dir"] = str(debug_dir) if debug_dir else None
    return stats


# --- exhaustive capture via dated search windows ----------------------------
#
# A profile timeline paginates to a cursor that dead-ends long before the full
# history — X serves roughly 3200 posts and a rate-limited page looks exactly
# like the end of the timeline. Search is a different index with its own
# (per-query) limit, so slicing one account's history into short date windows
# reaches posts the timeline will never hand over.

SEARCH_BASE = "https://x.com/search"
FLOOR = date(2006, 3, 21)  # X's first post; no point walking past it


def search_url(handle: str, start: str, end: str, replies: str = "include") -> str:
    """Latest-tab search URL for one account over one date window.

    `since:` is inclusive and `until:` exclusive, so consecutive windows tile
    without overlapping or dropping a day. f=live is the Latest tab, which is
    chronological and far more complete than Top.
    """
    query = f"from:{handle} since:{start} until:{end}"
    if replies == "only":
        query += " filter:replies"
    elif replies == "exclude":
        query += " -filter:replies"
    return f"{SEARCH_BASE}?q={quote(query)}&src=typed_query&f=live"


def date_windows(since: date, until: date, days: int) -> list[tuple[str, str]]:
    """Tile [since, until) into newest-first windows of `days` each."""
    if days < 1:
        raise ValueError("window must be at least 1 day")
    windows, end = [], until
    while end > since:
        start = max(since, end - timedelta(days=days))
        windows.append((start.isoformat(), end.isoformat()))
        end = start
    return windows


def _add_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _eta(started: float, done: int, total: int) -> str:
    """A rough finish estimate, because a silent 30-minute walk reads as hung."""
    if not total or done < 1 or done >= total:
        return ""
    elapsed = time.monotonic() - started
    remaining = elapsed / done * (total - done)
    minutes = int(remaining // 60)
    return f"  ~{minutes}m left" if minutes else f"  ~{int(remaining)}s left"


def window_coverage(collected: dict, start: str, end: str) -> str | None:
    """The oldest post captured inside [start, end), as a date, or None."""
    days = [
        rec["created_at"][:10]
        for rec in collected.values()
        if rec.get("created_at") and start <= rec["created_at"][:10] < end
    ]
    return min(days) if days else None


def continuation_window(start: str, end: str, covered: str | None, attempted) -> tuple | None:
    """The range a truncated window failed to reach, or None if it reached back.

    Search returns newest-first and stops at its own limit, so the honest test
    for truncation is whether the oldest post it returned reaches the window's
    start — not how many posts came back. Counting results can't tell a full
    window from a truncated one, and halving a window that was already complete
    just re-scans ranges we have.
    """
    if covered is None or covered <= start:
        return None
    # Re-include the oldest day covered: it may be only partly captured.
    candidate = (start, _add_day(covered))
    if candidate == (start, end) or candidate in attempted:
        candidate = (start, covered)  # that day resists narrowing; accept it
    if candidate in attempted or candidate[0] >= candidate[1]:
        return None
    return candidate


RATE_LIMIT_BASE_S = 20
RATE_LIMIT_MAX_S = 240


def _recover_if_stuck(page, verbose: bool = False, state: dict | None = None) -> bool:
    """X answers rate limits with an error card that mimics an empty timeline.

    Clicking through it is the difference between stopping early and carrying
    on, so treat it as backpressure. Waits grow each time: repeated limits mean
    X wants a longer pause, and retrying every 20s just burns the quota.
    """
    state = state if state is not None else {}
    try:
        text = page.inner_text("body", timeout=3000).lower()
    except Exception:
        return False
    if "something went wrong" not in text and "try again" not in text:
        return False
    state["hits"] = state.get("hits", 0) + 1
    wait_s = min(RATE_LIMIT_BASE_S * 2 ** (state["hits"] - 1), RATE_LIMIT_MAX_S)
    if verbose:
        print(f"\r  rate limited ({state['hits']}) — waiting {wait_s}s…    ",
              end="", flush=True)
    page.wait_for_timeout(wait_s * 1000)
    for selector in ('div[role="button"]:has-text("Retry")', 'button:has-text("Retry")'):
        try:
            page.click(selector, timeout=2000)
            page.wait_for_timeout(3000)
            return True
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        return True
    except Exception:
        return False


def fetch_user_search(
    conn,
    handle: str,
    since: str | None = None,
    until: str | None = None,
    window_days: int = 14,
    max_posts: int = 20000,
    max_empty_windows: int = 8,
    replies: str = "include",
    windows: list | None = None,
    order: str = "newest",
    headless: bool = False,
    profile_dir: Path | None = None,
    channel: str | None = None,
    scroll_pause_ms: int = 1500,
    verbose: bool = True,
    debug: bool = False,
) -> dict:
    """Capture an account's posts by walking dated search windows.

    Reaches history the profile timeline won't serve. Windows that come back
    full are split and re-run, because a full window is indistinguishable from
    a truncated one. Without `since` it walks backwards until it sees
    `max_empty_windows` consecutive empty windows.
    """
    sync_playwright = _require_playwright()
    profile_dir = Path(profile_dir or PROFILE_DIR)
    target = handle.lstrip("@").lower()

    end_date = date.fromisoformat(until[:10]) if until else date.today() + timedelta(days=1)
    since_date = date.fromisoformat(since[:10]) if since else None
    open_ended = since_date is None

    collected: dict[str, dict] = {}
    profile: dict = {}
    seen_handles: dict[str, int] = {}
    seen_ops: dict[str, int] = {}
    errors: list[str] = []
    sink = Sink(conn)
    interrupted = False

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
        had_profile = bool(profile.get("statuses_count"))
        capture_profile(payload, target, profile)
        if profile.get("statuses_count") and not had_profile:
            sink.write_profile(profile)
        sink.write(collect_posts(payload, target, collected, seen_handles))

    pending: list[tuple[str, str, bool]] = []  # (start, end, is_continuation)
    if windows is not None:
        pending = [(s, e, False) for s, e in windows]
        open_ended = False
    elif not open_ended:
        pending = [(s, e, False) for s, e in date_windows(since_date, end_date, window_days)]
    if order == "oldest":
        # Oldest first, so the history you're missing arrives before the
        # recent months you probably already have.
        pending.reverse()
    planned = len(pending) or None
    attempted: set[tuple[str, str]] = set()
    cursor = end_date
    windows_done, extra, empty_streak, truncated = 0, 0, 0, []
    started = time.monotonic()

    with sync_playwright() as p:
        context = _launch_any(p, profile_dir, headless, channel, verbose)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        _wait_for_login(context, page)

        try:
            # Visit the profile first: its creation date bounds the walk, so an
            # open-ended search stops at the account's first day instead of
            # guessing from empty windows.
            page.goto(f"https://x.com/{target}", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            floor = FLOOR
            if profile.get("created_at"):
                floor = max(FLOOR, date.fromisoformat(profile["created_at"][:10]))
                if verbose:
                    print(f"  account created {floor.isoformat()} — walking back to there")
            if since_date:
                floor = max(floor, since_date)

            while len(collected) < max_posts:
                if not pending:
                    if not open_ended or cursor <= floor:
                        break
                    start = max(floor, cursor - timedelta(days=window_days))
                    pending.append((start.isoformat(), cursor.isoformat(), False))
                    cursor = start

                start, end, is_continuation = pending.pop(0)
                attempted.add((start, end))
                before = len(collected)

                position = (f"{windows_done + 1}/{planned + extra}" if planned
                            else f"{windows_done + 1}")
                label = f"[{position}] {start} → {end}  "
                if verbose:
                    print(f"  {label}loading…", end="\r", flush=True)
                page.goto(search_url(target, start, end, replies), wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                _scroll_collect(
                    page, lambda: len(collected), pause_ms=scroll_pause_ms,
                    verbose=verbose, label=label, max_posts=max_posts,
                )
                added = len(collected) - before
                windows_done += 1

                # Did this window reach its own start date, or stop short?
                covered = window_coverage(collected, start, end)
                follow_up = continuation_window(start, end, covered, attempted)
                if follow_up:
                    extra += 1
                    pending.insert(0, (follow_up[0], follow_up[1], True))
                elif covered and covered > start:
                    truncated.append((start, end))

                if verbose:
                    note = f" cut off at {covered}, continuing" if follow_up else ""
                    print(f"  {label}+{added} (total {len(collected)}){note}"
                          f"{_eta(started, windows_done, (planned or 0) + extra)}"
                          + " " * 10)

                if not is_continuation:
                    empty_streak = empty_streak + 1 if added == 0 else 0
                    if open_ended and empty_streak >= max_empty_windows:
                        if verbose:
                            print(f"  {empty_streak} empty windows in a row — stopping.")
                        break
        except KeyboardInterrupt:
            interrupted = True
            print("\n  interrupted — keeping the posts captured so far.")
        try:
            context.close()
        except Exception:
            pass

    sink.write(list(collected.values()))
    sink.write_profile(profile)
    sink.commit()
    stats = dict(sink.stats)
    stats.update(
        interrupted=interrupted,
        profile=dict(profile),
        total=len(collected),
        oldest=_oldest(collected),
        windows=windows_done,
        continuations=extra,
        truncated=truncated,
        missing_views=sum(1 for r in collected.values() if r.get("views") is None),
        skipped_other_authors=sum(n for h, n in seen_handles.items() if h != target),
        operations=seen_ops,
        debug_dir=None,
    )
    if not collected:
        stats["error"] = no_posts_message(target, [], seen_handles, seen_ops, errors)
    return stats


def gap_ranges(stored_days: set, start: str, end: str, min_gap_days: int = 3) -> list:
    """Runs of at least `min_gap_days` consecutive days with nothing stored.

    Walking the whole history newest-first puts the months you're missing last,
    which is backwards when the point is to fill a hole. This finds the holes.

    A one- or two-day silence is normal for any account, so short runs are
    ignored; only sustained blanks are treated as gaps worth re-scanning.
    """
    runs, current = [], None
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    while day < last:
        key = day.isoformat()
        if key in stored_days:
            if current:
                runs.append((current, key))
                current = None
        elif current is None:
            current = key
        day += timedelta(days=1)
    if current:
        runs.append((current, end))
    return [
        (a, b) for a, b in runs
        if (date.fromisoformat(b) - date.fromisoformat(a)).days >= min_gap_days
    ]


def gap_windows(stored_days: set, start: str, end: str, window_days: int,
                min_gap_days: int = 3) -> list:
    """Search windows covering only the days with nothing stored."""
    windows = []
    for range_start, range_end in gap_ranges(stored_days, start, end, min_gap_days):
        windows.extend(
            date_windows(date.fromisoformat(range_start), date.fromisoformat(range_end),
                         window_days)
        )
    return windows
