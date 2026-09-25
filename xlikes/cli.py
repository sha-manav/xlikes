"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import textwrap

from . import db, search as search_mod

HI_ON, HI_OFF = "\x02", "\x03"
COMMANDS = {"fetch", "search", "import-archive", "recent", "stats", "export",
            "user", "user-export", "user-coverage"}


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.on else text

    bold = lambda self, t: self._wrap("1", t)
    dim = lambda self, t: self._wrap("2", t)
    cyan = lambda self, t: self._wrap("36", t)
    green = lambda self, t: self._wrap("32", t)
    yellow = lambda self, t: self._wrap("33", t)
    hit = lambda self, t: self._wrap("1;43;30", t)


def _highlight(text: str, style: Style) -> str:
    if not text:
        return ""
    if not style.on:
        return text.replace(HI_ON, "«").replace(HI_OFF, "»")
    out, rest = [], text
    while HI_ON in rest and HI_OFF in rest:
        before, rest = rest.split(HI_ON, 1)
        match, rest = rest.split(HI_OFF, 1)
        out.append(before + style.hit(match))
    return "".join(out) + rest


def _fmt_date(iso: str | None) -> str:
    return iso[:10] if iso else "unknown date"


def print_results(rows, style: Style, query: str | None, show_full: bool = False) -> None:
    if not rows:
        return
    width = 96
    for i, row in enumerate(rows, 1):
        author = f"@{row['author_handle']}" if row["author_handle"] else "(author unknown)"
        name = f" {row['author_name']}" if row["author_name"] else ""
        header = f"{style.bold(f'{i:>2}.')} {style.cyan(author)}{style.dim(name)}  {style.dim(_fmt_date(row['created_at']))}"
        if row["is_quote"]:
            header += style.dim("  · quote post")
        if row["has_article"]:
            header += style.yellow("  · article")
        print(header)

        body = row["text"] or ""
        if query and row["snip"] and HI_ON in row["snip"]:
            body = _highlight(row["snip"], style)
        elif not show_full and len(body) > 320:
            body = body[:320].rstrip() + "…"
        for line in textwrap.wrap(body, width, initial_indent="    ", subsequent_indent="    ") or ["    (no text)"]:
            print(line)

        quoted = row["quoted_text"] or ""
        if query and row["qsnip"] and HI_ON in row["qsnip"]:
            quoted = _highlight(row["qsnip"], style)
        elif not show_full and len(quoted) > 200:
            quoted = quoted[:200].rstrip() + "…"
        if quoted:
            q_author = f"@{row['quoted_handle']}" if row["quoted_handle"] else "quoted"
            print(style.dim(f"    ┌ quoting {q_author}:"))
            for line in textwrap.wrap(quoted, width - 6, initial_indent="    │ ", subsequent_indent="    │ "):
                print(style.dim(line))

        print(f"    {style.green(row['url'] or '')}")
        print()


def cmd_fetch(args, conn) -> int:
    from .fetch import FetchError, fetch_likes

    try:
        stats = fetch_likes(
            conn,
            handle=args.handle,
            max_posts=args.max,
            headless=args.headless,
            profile_dir=args.profile,
            channel=None if args.browser == "chromium" else args.browser,
            debug=args.debug,
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"\n{stats['total']} likes read — {stats['new']} new, {stats['updated']} enriched.")
    return 0


def cmd_import_archive(args, conn) -> int:
    from .archive import import_archive

    try:
        stats = import_archive(conn, args.path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{stats['total']} likes in archive — {stats['new']} new, {stats['updated']} enriched.")
    return 0


def cmd_search(args, conn) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color)
    try:
        kwargs = dict(
            raw=args.raw,
            since=search_mod.parse_when(args.since),
            until=search_mod.parse_when(args.until),
            author=args.author,
            quotes_only=args.quotes,
            articles_only=args.articles,
            links_only=args.links,
            recent=args.recent,
            limit=args.limit,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    query = " ".join(args.query).strip() or None
    mode = "any" if args.any else "all"
    try:
        rows, relaxed, total = search_mod.smart_search(conn, query, mode=mode, **kwargs)
    except Exception as exc:
        print(f"error: bad search query ({exc})", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([{k: r[k] for k in r.keys() if k not in ("snip", "qsnip")} for r in rows], indent=2))
        return 0

    if not rows:
        total = conn.execute("SELECT COUNT(*) c FROM likes").fetchone()["c"]
        print("No matches." if total else "No likes indexed yet — run `xlikes fetch` first.")
        if total:
            print(f"({total} likes indexed. Try fewer words, or --any to match any of them.)")
        return 1

    indexed = search_mod.count(conn)
    if args.recent and args.recent > indexed:
        print(style.yellow(f"note: --recent {args.recent} but only {indexed} likes are indexed") +
              style.dim(f" — run `xlikes fetch --max {max(args.recent, indexed * 2)}` to go further back\n"))
    if relaxed:
        print(style.dim("No post had all of those words — showing best partial matches:\n"))
    print_results(rows, style, query, show_full=args.full)

    shown, cap = len(rows), search_mod.COUNT_CAP
    if total > shown:
        amount = f"{cap}+" if total >= cap else str(total)
        print(style.yellow(f"showing {shown} of {amount} matches") +
              style.dim(f" — add -n {min(total, 100)} to see more"))
    else:
        print(style.dim(f"{shown} result{'s' if shown != 1 else ''}"))
    return 0


def cmd_recent(args, conn) -> int:
    style = Style(sys.stdout.isatty() and not args.no_color)
    rows = search_mod.search(conn, None, limit=args.limit)
    if not rows:
        print("No likes indexed yet — run `xlikes fetch` first.")
        return 1
    print_results(rows, style, None)
    return 0


def cmd_stats(args, conn) -> int:
    row = conn.execute(
        """SELECT COUNT(*) total, SUM(is_quote) quotes, SUM(has_article) articles,
                  MIN(created_at) oldest, MAX(created_at) newest
           FROM likes"""
    ).fetchone()
    print(f"likes indexed : {row['total']}")
    print(f"quote posts   : {row['quotes'] or 0}")
    print(f"with articles : {row['articles'] or 0}")
    print(f"post dates    : {_fmt_date(row['oldest'])} → {_fmt_date(row['newest'])}")
    print(f"account       : @{db.get_meta(conn, 'handle', '?')}")
    print(f"last fetch    : {db.get_meta(conn, 'last_fetch', 'never')}")
    return 0


def cmd_export(args, conn) -> int:
    rows = conn.execute("SELECT * FROM likes ORDER BY like_rank, created_at DESC").fetchall()
    print(json.dumps([dict(r) for r in rows], indent=2))
    return 0


EXPORT_COLS = ("created_at kind handle author_name text likes views reposts replies "
               "quotes bookmarks in_reply_to_handle in_reply_to_id quoted_handle quoted_id "
               "quoted_text has_media lang urls url id").split()


def cmd_user(args, conn) -> int:
    from .fetch import FetchError, fetch_user_posts, fetch_user_search

    try:
        since = search_mod.parse_when(args.since)
        until = search_mod.parse_when(args.until)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    shared = dict(
        headless=args.headless,
        profile_dir=args.profile,
        channel=None if args.browser == "chromium" else args.browser,
    )
    ran, failures, stopped = [], [], False

    if args.mode in ("timeline", "both"):
        print("== profile timeline (fast; the only source of reposts) ==")
        try:
            ran.append(("timeline", fetch_user_posts(
                conn, handle=args.handle, max_posts=args.max, since=since,
                include_replies=not args.no_replies, debug=args.debug, **shared)))
            stopped = ran[-1][1].get("interrupted", False)
        except FetchError as exc:
            failures.append(str(exc))
            print(f"timeline: {exc}", file=sys.stderr)

    if stopped:
        print("\nStopped at your request — skipping the search pass. "
              "Everything captured is saved; re-run to carry on.")
    if not stopped and args.mode in ("search", "both"):
        windows, order = None, args.order or "newest"
        if args.fill_gaps:
            windows, order = _gap_plan(conn, args, since, until), args.order or "oldest"
            if windows is None:
                return 1
            if not windows:
                print("\nNo gaps of "
                      f"{args.min_gap}+ days to fill — nothing to search.")
                return 0
            print(f"\n== filling {len(windows)} window(s) with no posts stored "
                  f"({order}-first) ==")
        else:
            print("\n== dated search windows (reaches what the timeline won't serve) ==")
        try:
            stats = fetch_user_search(
                conn, handle=args.handle, since=since, until=until,
                window_days=args.window, max_posts=args.max,
                max_empty_windows=args.max_empty,
                replies="include" if not args.no_replies else "exclude",
                windows=windows, order=order,
                debug=args.debug, **shared)
            if stats.get("error") and not stats["total"]:
                failures.append(stats["error"])
                print(f"search: {stats['error']}", file=sys.stderr)
            else:
                ran.append(("search", stats))
        except FetchError as exc:
            failures.append(str(exc))
            print(f"search: {exc}", file=sys.stderr)

    if not ran:
        return 1

    for label, stats in ran:
        extra = ""
        if label == "search":
            extra = (f", {stats['windows']} windows"
                     f" ({stats['continuations']} continued past a cut-off)")
            if stats.get("truncated"):
                extra += f", {len(stats['truncated'])} still short"
        if stats.get("interrupted"):
            extra += " (interrupted, saved)"
        print(f"\n{label}: {stats['total']} captured — {stats['new']} new, "
              f"{stats['updated']} refreshed{extra}")
        if args.debug:
            ops = ", ".join(f"{op} x{n}" for op, n in sorted(stats["operations"].items()))
            print(f"  operations: {ops}")
            print(f"  other authors skipped: {stats['skipped_other_authors']}")
            if stats.get("debug_dir"):
                print(f"  debug output: {stats['debug_dir']}")

    handle = args.handle.lstrip("@").lower()
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(created_at) oldest, MAX(created_at) newest, "
        "SUM(views IS NULL) no_views FROM posts WHERE handle = ?", (handle,)).fetchone()
    print(f"\nstored for @{handle}: {row['n']} posts, "
          f"{(row['oldest'] or '?')[:10]} → {(row['newest'] or '?')[:10]}")
    if row["no_views"]:
        print(f"  {row['no_views']} have no view count "
              "(X only reports views for posts from late 2022 onward)")
    account = conn.execute(
        "SELECT created_at, statuses_count FROM accounts WHERE handle = ?", (handle,)
    ).fetchone()
    if account and account["created_at"]:
        print(f"  account created {account['created_at'][:10]}")
    if account and account["statuses_count"]:
        pct = round(row["n"] / account["statuses_count"] * 100)
        print(f"  that's {pct}% of the {account['statuses_count']} posts the profile "
              "reports (which includes replies and reposts, and excludes deletions)")
    print(f"  check for gaps with: xlikes user-coverage {handle}")
    return 0


def _gap_plan(conn, args, since, until):
    """Search windows for the days with nothing stored. None means: can't tell."""
    from datetime import date, timedelta

    from .fetch import gap_windows

    handle = args.handle.lstrip("@").lower()
    stored = {
        row["day"] for row in conn.execute(
            "SELECT DISTINCT substr(created_at,1,10) day FROM posts "
            "WHERE handle = ? AND created_at IS NOT NULL", (handle,))
    }
    account = conn.execute(
        "SELECT created_at FROM accounts WHERE handle = ?", (handle,)).fetchone()

    start = (since or "")[:10] or (account["created_at"][:10] if account and
                                   account["created_at"] else min(stored, default=""))
    if not start:
        print(f"error: nothing stored for @{handle} and no --since given, so there's "
              "no range to fill.\n"
              f"Run a plain pass first, or pass --since (e.g. --since 2025-10-01).",
              file=sys.stderr)
        return None
    end = (until or "")[:10] or (date.today() + timedelta(days=1)).isoformat()
    return gap_windows(stored, start, end, args.window, args.min_gap)


def cmd_user_coverage(args, conn) -> int:
    """Posts per month, measured against the profile's own post count."""
    handle = args.handle.lstrip("@").lower()
    rows = conn.execute(
        """SELECT substr(created_at,1,7) month, COUNT(*) n,
                  SUM(kind='reply') replies, SUM(kind='repost') reposts
           FROM posts WHERE handle = ? AND created_at IS NOT NULL
           GROUP BY month ORDER BY month DESC""", (handle,)).fetchall()
    if not rows:
        print(f"Nothing stored for @{handle} — run `xlikes user {handle}` first.",
              file=sys.stderr)
        return 1

    account = conn.execute("SELECT * FROM accounts WHERE handle = ?", (handle,)).fetchone()
    stored = sum(r["n"] for r in rows)

    widest = max(r["n"] for r in rows)
    print(f"{'month':8} {'posts':>6} {'repl':>5} {'rt':>4}")
    months = {r["month"] for r in rows}
    for row in rows:
        bar = "\u2588" * max(1, round(row["n"] / widest * 28))
        print(f"{row['month']:8} {row['n']:>6} {row['replies'] or 0:>5} "
              f"{row['reposts'] or 0:>4} {bar}")

    print()
    # Months before the account existed aren't gaps, so the window to check
    # starts at whichever is later: account creation or the oldest post stored.
    first = rows[-1]["month"]
    created_month = None
    if account and account["created_at"]:
        created_month = account["created_at"][:7]
        first = max(first, created_month) if created_month > first else first
        first = created_month
    last = rows[0]["month"]

    if account and account["created_at"]:
        print(f"account created: {account['created_at'][:10]}")
    if account and account["statuses_count"]:
        claimed = account["statuses_count"]
        pct = round(stored / claimed * 100)
        print(f"stored {stored} of the {claimed} posts the profile reports ({pct}%)")
        if pct < 90:
            print("  Under 90% — try narrower windows: "
                  f"xlikes user {handle} --mode search --window 3")
        print("  That figure counts posts, replies and reposts together and "
              "excludes anything deleted, so it's a ceiling, not an exact target.")
    else:
        print(f"{stored} posts stored across {len(months)} months ({first} → {last})")
        print(f"  No profile post count recorded yet — re-run `xlikes user {handle}` "
              "to capture it and get a completeness figure.")

    missing = []
    year, month = int(first[:4]), int(first[5:7])
    while f"{year:04d}-{month:02d}" <= last:
        key = f"{year:04d}-{month:02d}"
        if key not in months:
            missing.append(key)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    if missing:
        print(f"\n{len(missing)} month(s) with nothing stored since "
              f"{'account creation' if created_month else 'the oldest post'}: "
              f"{', '.join(missing[:14])}" + (" \u2026" if len(missing) > 14 else ""))
        print("A silent month and a missed month look identical here. To re-check one:")
        print(f"  xlikes user {handle} --mode search --since {missing[0]}-01 "
              f"--until {missing[0]}-28 --window 7")
    else:
        span = f"{first} \u2192 {last}"
        print(f"\nEvery month from {span} has at least one post stored.")
    return 0


def cmd_user_export(args, conn) -> int:
    try:
        since, until = search_mod.parse_when(args.since), search_mod.parse_when(args.until)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    where, params = ["handle = ?"], [args.handle.lstrip("@").lower()]
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at <= ?")
        params.append(until)
    if args.kind != "all":
        if args.kind == "posts":
            where.append("kind IN ('post','quote','repost')")
        elif args.kind == "replies":
            where.append("kind = 'reply'")
        else:
            where.append("kind = ?")
            params.append(args.kind)

    rows = conn.execute(
        f"SELECT * FROM posts WHERE {' AND '.join(where)} ORDER BY created_at DESC", params
    ).fetchall()
    if not rows:
        total = conn.execute("SELECT COUNT(*) c FROM posts WHERE handle = ?",
                             (args.handle.lstrip('@').lower(),)).fetchone()["c"]
        if total:
            print(f"No posts match those filters ({total} stored for "
                  f"@{args.handle.lstrip('@')}).", file=sys.stderr)
        else:
            print(f"Nothing stored for @{args.handle.lstrip('@')} — run "
                  f"`xlikes user {args.handle.lstrip('@')}` first.", file=sys.stderr)
        return 1

    stream = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.format == "json":
            json.dump([{c: r[c] for c in EXPORT_COLS} for r in rows], stream, indent=2)
            stream.write("\n")
        else:
            writer = csv.writer(stream)
            writer.writerow(EXPORT_COLS)
            for row in rows:
                writer.writerow(["" if row[c] is None else row[c] for c in EXPORT_COLS])
    finally:
        if args.out:
            stream.close()
            span = f"{rows[-1]['created_at'][:10]} → {rows[0]['created_at'][:10]}"
            print(f"{len(rows)} rows ({span}) written to {args.out}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xlikes",
        description="Search your X likes locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              xlikes fetch                          pull your recent likes into the index
              xlikes "article is really good"       search them
              xlikes search article good --quotes   only quote posts
              xlikes search --recent 300 --articles the last 300 likes that involve an article
            """
        ),
    )
    parser.add_argument("--db", help="path to the index (default ~/.xlikes/likes.db)")
    sub = parser.add_subparsers(dest="command")

    f = sub.add_parser("fetch", help="pull likes from x.com using your browser session")
    f.add_argument("--handle", help="your handle (auto-detected if omitted)")
    f.add_argument("--max", type=int, default=600, help="how many likes to walk back (default 600)")
    f.add_argument("--headless", action="store_true", help="no visible window (only after first login)")
    f.add_argument("--profile", help="browser profile dir (default ~/.xlikes/browser-profile)")
    f.add_argument("--browser", choices=["chromium", "chrome", "msedge"],
                   help="which browser to drive (default: whichever is available)")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("import-archive", help="import like.js from an X data archive")
    a.add_argument("path", help="the archive .zip, its folder, or like.js itself")
    a.set_defaults(func=cmd_import_archive)

    s = sub.add_parser("search", help="search indexed likes")
    s.add_argument("query", nargs="*", help="words to look for")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--any", action="store_true", help="match any word instead of all")
    s.add_argument("--raw", action="store_true", help="pass the query straight to FTS5")
    s.add_argument("--since", help="posted after: 3w, 21d, 2026-08-01")
    s.add_argument("--until", help="posted before")
    s.add_argument("--recent", type=int, metavar="N", help="only your N most recent likes")
    s.add_argument("--author", help="filter by who posted it")
    s.add_argument("--quotes", action="store_true", help="only quote posts")
    s.add_argument("--articles", action="store_true", help="only posts involving an X article")
    s.add_argument("--links", action="store_true", help="only posts containing a link")
    s.add_argument("--full", action="store_true", help="don't truncate post text")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-color", action="store_true")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("recent", help="show your most recent likes")
    r.add_argument("-n", "--limit", type=int, default=20)
    r.add_argument("--no-color", action="store_true")
    r.set_defaults(func=cmd_recent)

    u = sub.add_parser("user", help="pull another account's posts and replies, with engagement counts")
    u.add_argument("handle", help="the account to read, e.g. Damnang2")
    u.add_argument("--mode", default="both", choices=["timeline", "search", "both"],
                   help="timeline = fast partial pass (and the only source of reposts); "
                        "search = dated windows, reaches far more history; both (default)")
    u.add_argument("--since", help="oldest date to reach: 3w, 2026-08-01 "
                                   "(omitted: walk back until the account goes quiet)")
    u.add_argument("--until", help="newest date to search (default today)")
    u.add_argument("--window", type=int, default=14,
                   help="days per search window (default 14; lower for prolific accounts)")
    u.add_argument("--fill-gaps", action="store_true",
                   help="only search date ranges with no posts stored, oldest first — "
                        "skips months you already have")
    u.add_argument("--min-gap", type=int, default=3, metavar="DAYS",
                   help="with --fill-gaps, ignore silences shorter than this (default 3)")
    u.add_argument("--order", choices=["newest", "oldest"],
                   help="which end of the range to search first "
                        "(default: newest, or oldest with --fill-gaps)")
    u.add_argument("--max-empty", type=int, default=8, dest="max_empty",
                   help="consecutive empty windows before assuming the history ended")
    u.add_argument("--max", type=int, default=20000, help="cap on posts (default 20000)")
    u.add_argument("--no-replies", action="store_true", help="posts tab only, skip replies")
    u.add_argument("--headless", action="store_true")
    u.add_argument("--profile", help="browser profile dir")
    u.add_argument("--browser", choices=["chromium", "chrome", "msedge"])
    u.add_argument("--debug", action="store_true",
                   help="save raw responses and a screenshot to ~/.xlikes/debug")
    u.set_defaults(func=cmd_user)

    ue = sub.add_parser("user-export", help="export a stored account's posts as CSV or JSON")
    ue.add_argument("handle")
    ue.add_argument("--since", help="only posts written after: 3w, 2026-08-01")
    ue.add_argument("--until")
    ue.add_argument("--kind", default="all",
                    choices=["all", "posts", "replies", "post", "reply", "quote", "repost"])
    ue.add_argument("--format", default="csv", choices=["csv", "json"])
    ue.add_argument("--out", help="write to a file instead of stdout")
    ue.set_defaults(func=cmd_user_export)

    uc = sub.add_parser("user-coverage", help="posts per month for a stored account, to spot gaps")
    uc.add_argument("handle")
    uc.set_defaults(func=cmd_user_coverage)

    sub.add_parser("stats", help="what's in the index").set_defaults(func=cmd_stats)
    sub.add_parser("export", help="dump the whole index as JSON").set_defaults(func=cmd_export)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `xlikes some words` is shorthand for `xlikes search some words`.
    # Skip values belonging to global options, or `--db path` would look
    # like the command.
    takes_value = {"--db"}
    index, skip = None, False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg in takes_value:
            skip = True
            continue
        if not arg.startswith("-"):
            index = i
            break
    if index is not None and argv[index] not in COMMANDS:
        argv.insert(index, "search")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    conn = db.connect(args.db)
    try:
        return args.func(args, conn)
    except BrokenPipeError:
        # piped into head/less and it closed early — not an error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
