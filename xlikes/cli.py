"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

from . import db, search as search_mod

HI_ON, HI_OFF = "\x02", "\x03"
COMMANDS = {"fetch", "search", "import-archive", "recent", "stats", "export"}


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
        rows, relaxed = search_mod.smart_search(conn, query, mode=mode, **kwargs)
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

    if relaxed:
        print(style.dim("No post had all of those words — showing best partial matches:\n"))
    print_results(rows, style, query, show_full=args.full)
    print(style.dim(f"{len(rows)} result{'s' if len(rows) != 1 else ''}"))
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
