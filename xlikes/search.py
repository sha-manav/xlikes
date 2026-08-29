"""Query the like index."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

# text and quoted_text matter most; the article title lives in quoted_text.
# Order matches the FTS columns: text, quoted_text, author_handle, author_name,
# quoted_handle, quoted_name, urls.
BM25_WEIGHTS = (8.0, 5.0, 2.0, 2.0, 1.5, 1.5, 1.0)

_REL = re.compile(r"^(\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years|h|hour|hours)$")
_UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "w": 7, "week": 7, "weeks": 7,
              "m": 30, "month": 30, "months": 30, "y": 365, "year": 365, "years": 365}


def parse_when(value: str | None) -> str | None:
    """Accept '3w', '21 days', 'yesterday' or an ISO date -> ISO8601 UTC."""
    if not value:
        return None
    text = value.strip().lower()
    now = datetime.now(timezone.utc)
    if text in ("today",):
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if text in ("yesterday",):
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if match := _REL.match(text.replace(" ", "")):
        count, unit = int(match.group(1)), match.group(2)
        if unit.startswith("h"):
            return (now - timedelta(hours=count)).isoformat()
        return (now - timedelta(days=count * _UNIT_DAYS[unit])).isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    raise ValueError(f"can't read date {value!r} — try '3w', '21d' or '2026-08-01'")


def _tokens(query: str) -> list[str]:
    """Split a query, keeping "quoted phrases" intact."""
    return [t for t in re.findall(r'"[^"]*"|\S+', query) if t.strip('"').strip()]


def _terms(query: str) -> list[str]:
    """Each token as a valid FTS5 term: bare words get quoted (apostrophes,
    hyphens and colons are all FTS operators), trailing * stays a prefix match."""
    out = []
    for token in _tokens(query):
        if token.startswith('"') and token.endswith('"') and len(token) > 1:
            out.append(token)
            continue
        prefix = "*" if token.endswith("*") else ""
        cleaned = token.rstrip("*").replace('"', "").strip()
        if cleaned:
            out.append(f'"{cleaned}"{prefix}')
    if not out:
        raise ValueError("empty search query")
    return out


def build_match(query: str, mode: str = "all") -> str:
    """Turn plain words into a valid FTS5 expression."""
    return (" OR " if mode == "any" else " AND ").join(_terms(query))


def search(
    conn: sqlite3.Connection,
    query: str | None = None,
    *,
    mode: str = "all",
    raw: bool = False,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    quotes_only: bool = False,
    articles_only: bool = False,
    links_only: bool = False,
    recent: int | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    where, params = [], []
    if query:
        match = query if raw else build_match(query, mode)
        where.append("likes_fts MATCH ?")
        params.append(match)
    if since:
        where.append("l.created_at >= ?")
        params.append(since)
    if until:
        where.append("l.created_at <= ?")
        params.append(until)
    if author:
        where.append("(lower(l.author_handle) LIKE ? OR lower(l.author_name) LIKE ?)")
        needle = f"%{author.lstrip('@').lower()}%"
        params += [needle, needle]
    if quotes_only:
        where.append("l.is_quote = 1")
    if articles_only:
        where.append("l.has_article = 1")
    if links_only:
        where.append("COALESCE(l.urls,'') != ''")
    if recent is not None:
        where.append("l.like_rank IS NOT NULL AND l.like_rank < ?")
        params.append(recent)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    if query:
        sql = f"""
            SELECT l.*,
                   bm25(likes_fts, {','.join(str(w) for w in BM25_WEIGHTS)}) AS score,
                   snippet(likes_fts, 0, '\x02', '\x03', '…', 18) AS snip,
                   snippet(likes_fts, 1, '\x02', '\x03', '…', 18) AS qsnip
            FROM likes_fts JOIN likes l ON l.rowid = likes_fts.rowid
            {clause}
            ORDER BY score
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT l.*, 0 AS score, '' AS snip, '' AS qsnip
            FROM likes l
            {clause}
            ORDER BY CASE WHEN l.like_rank IS NULL THEN 1 ELSE 0 END,
                     l.like_rank, l.created_at DESC
            LIMIT ?
        """
    return conn.execute(sql, [*params, limit]).fetchall()


def _rungs(terms: list[str]) -> list[str]:
    """Match expressions for one set of terms, most precise first.

    You rarely remember a post word for word, so precision is a ladder: an exact
    phrase beats words sitting close together, which beats words merely
    co-occurring somewhere in the post.
    """
    rungs = []
    if len(terms) > 1 and not any(t.endswith("*") for t in terms):
        rungs.append('"' + " ".join(t.strip('"') for t in terms) + '"')
        joined = " ".join(terms)
        rungs.append(f"NEAR({joined}, 3)")
        rungs.append(f"NEAR({joined}, 10)")
    rungs.append(" AND ".join(terms))
    return rungs


def _run(conn, match: str, limit: int, kwargs: dict):
    try:
        return search(conn, match, raw=True, limit=limit, **kwargs)
    except sqlite3.OperationalError:
        return []  # a rung this tokenizer won't accept; looser ones still apply


def _collect(conn, term_sets: list[list[str]], limit: int, kwargs: dict, seen: set):
    """Walk every rung across every term set, precision rung by precision rung,
    so a phrase hit in one set still outranks a loose hit in another."""
    rows = []
    ladders = [_rungs(t) for t in term_sets]
    for depth in range(max((len(l) for l in ladders), default=0)):
        for ladder in ladders:
            if depth >= len(ladder):
                continue
            for row in _run(conn, ladder[depth], limit, kwargs):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def smart_search(conn, query: str | None, *, limit: int = 20, mode: str = "all", **kwargs):
    """Find posts by half-remembered wording. Returns (rows, relaxed).

    relaxed is True when no post contained everything you typed and the search
    had to loosen — usually because one remembered word wasn't the real one.
    """
    if not query:
        return search(conn, None, limit=limit, **kwargs), False
    if kwargs.get("raw"):
        return search(conn, query, limit=limit, **kwargs), False
    kwargs.pop("raw", None)

    terms = _terms(query)
    if mode == "any":
        return _rank_by_coverage(conn, terms, limit, kwargs), False

    seen: set = set()
    rows = _collect(conn, [terms], limit, kwargs, seen)
    if rows:
        return rows[:limit], False

    # Nothing matched every word — drop one word at a time. A single wrong word
    # in a remembered quote shouldn't sink the whole search.
    if 2 < len(terms) <= 6:
        subsets = [terms[:i] + terms[i + 1:] for i in range(len(terms))]
        rows = _collect(conn, subsets, limit, kwargs, seen)
        if rows:
            return rows[:limit], True

    return _rank_by_coverage(conn, terms, limit, kwargs), True


def _rank_by_coverage(conn, terms: list[str], limit: int, kwargs: dict):
    """Last resort: any word matches, ranked by how many of them a post has.

    bm25 alone favours short posts, so a one-word coincidence would beat a post
    matching four of five words.
    """
    rows = _run(conn, " OR ".join(terms), max(limit * 5, 50), kwargs)
    if not rows:
        return []
    coverage: dict[str, int] = {}
    for term in terms:
        for row in _run(conn, term, max(limit * 5, 50), kwargs):
            coverage[row["id"]] = coverage.get(row["id"], 0) + 1
    rows.sort(key=lambda r: (-coverage.get(r["id"], 0), r["score"]))
    return rows[:limit]
