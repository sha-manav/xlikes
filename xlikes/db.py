"""SQLite storage + FTS5 index for liked posts."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("XLIKES_DB", Path.home() / ".xlikes" / "likes.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS likes (
    id             TEXT PRIMARY KEY,       -- tweet id
    url            TEXT,
    author_handle  TEXT,
    author_name    TEXT,
    text           TEXT,
    created_at     TEXT,                   -- ISO8601 UTC, when the post was written
    like_rank      INTEGER,                -- 0 = most recently liked (from fetch order)
    is_quote       INTEGER DEFAULT 0,
    quoted_id      TEXT,
    quoted_handle  TEXT,
    quoted_name    TEXT,
    quoted_text    TEXT,
    quoted_url     TEXT,
    has_article    INTEGER DEFAULT 0,      -- post or its quoted post is/links an X Article
    has_media      INTEGER DEFAULT 0,
    urls           TEXT,                   -- newline separated expanded links
    source         TEXT,                   -- 'fetch' | 'archive'
    fetched_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_likes_rank    ON likes(like_rank);
CREATE INDEX IF NOT EXISTS idx_likes_created ON likes(created_at);
CREATE INDEX IF NOT EXISTS idx_likes_author  ON likes(author_handle);

-- External-content FTS: every indexed column must also exist on `likes`,
-- otherwise snippet()/highlight() can't read the original text back.
CREATE VIRTUAL TABLE IF NOT EXISTS likes_fts USING fts5(
    text, quoted_text, author_handle, author_name, quoted_handle, quoted_name, urls,
    content='likes', content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS likes_ai AFTER INSERT ON likes BEGIN
    INSERT INTO likes_fts(rowid, text, quoted_text, author_handle, author_name,
                          quoted_handle, quoted_name, urls)
    VALUES (new.rowid, new.text, new.quoted_text, new.author_handle, new.author_name,
            new.quoted_handle, new.quoted_name, new.urls);
END;

CREATE TRIGGER IF NOT EXISTS likes_ad AFTER DELETE ON likes BEGIN
    INSERT INTO likes_fts(likes_fts, rowid, text, quoted_text, author_handle, author_name,
                          quoted_handle, quoted_name, urls)
    VALUES ('delete', old.rowid, old.text, old.quoted_text, old.author_handle, old.author_name,
            old.quoted_handle, old.quoted_name, old.urls);
END;

CREATE TRIGGER IF NOT EXISTS likes_au AFTER UPDATE ON likes BEGIN
    INSERT INTO likes_fts(likes_fts, rowid, text, quoted_text, author_handle, author_name,
                          quoted_handle, quoted_name, urls)
    VALUES ('delete', old.rowid, old.text, old.quoted_text, old.author_handle, old.author_name,
            old.quoted_handle, old.quoted_name, old.urls);
    INSERT INTO likes_fts(rowid, text, quoted_text, author_handle, author_name,
                          quoted_handle, quoted_name, urls)
    VALUES (new.rowid, new.text, new.quoted_text, new.author_handle, new.author_name,
            new.quoted_handle, new.quoted_name, new.urls);
END;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Posts scraped from someone's profile timeline. Separate from `likes`: these
-- are one account's own output, with engagement counts that change over time.
CREATE TABLE IF NOT EXISTS posts (
    id                 TEXT PRIMARY KEY,
    handle             TEXT,        -- author, lowercased
    author_name        TEXT,
    created_at         TEXT,        -- ISO8601 UTC
    kind               TEXT,        -- post | reply | quote | repost
    text               TEXT,
    url                TEXT,
    in_reply_to_handle TEXT,
    in_reply_to_id     TEXT,
    conversation_id    TEXT,
    quoted_id          TEXT,
    quoted_handle      TEXT,
    quoted_text        TEXT,
    likes              INTEGER,
    reposts            INTEGER,
    replies            INTEGER,
    quotes             INTEGER,
    bookmarks          INTEGER,
    views              INTEGER,     -- NULL when X exposes no count for the post
    has_media          INTEGER DEFAULT 0,
    urls               TEXT,
    lang               TEXT,
    fetched_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_handle  ON posts(handle, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
"""

# Columns a richer source is allowed to fill in over a sparser one.
_MERGE_COLS = (
    "url author_handle author_name text created_at is_quote quoted_id quoted_handle "
    "quoted_name quoted_text quoted_url has_article has_media urls"
).split()


def _check_fts5(conn: sqlite3.Connection) -> None:
    """Full-text search is the whole point, so fail with something actionable
    rather than a syntax error from deep inside a CREATE statement."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"This Python's SQLite was built without the FTS5 extension ({exc}).\n"
            f"  python: {sys.executable}\n"
            f"  sqlite: {sqlite3.sqlite_version}\n"
            "Installing Python from python.org or Homebrew (`brew install python`) "
            "gets you a build that includes it."
        ) from exc


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _check_fts5(conn)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, rec: dict) -> str:
    """Insert a like, or enrich an existing row without losing data.

    Returns 'new', 'updated' or 'unchanged'. A row scraped from the timeline is
    richer than one from the data archive, so we never let empty fields
    overwrite populated ones.
    """
    existing = conn.execute("SELECT * FROM likes WHERE id = ?", (rec["id"],)).fetchone()
    if existing is None:
        cols = [c for c in rec if c != "rowid"]
        conn.execute(
            f"INSERT INTO likes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [rec[c] for c in cols],
        )
        return "new"

    updates = {}
    for col in _MERGE_COLS:
        new = rec.get(col)
        if new in (None, "", 0):
            continue
        if existing[col] in (None, "", 0):
            updates[col] = new
        elif col == "text" and len(str(new)) > len(str(existing[col])):
            updates[col] = new  # untruncated long-form body beats the 280-char one
    # like_rank always reflects the newest fetch ordering
    if rec.get("like_rank") is not None:
        updates["like_rank"] = rec["like_rank"]
    if rec.get("source") == "fetch":
        updates["source"] = "fetch"
        updates["fetched_at"] = rec.get("fetched_at")

    if not updates:
        return "unchanged"
    conn.execute(
        f"UPDATE likes SET {','.join(f'{k}=?' for k in updates)} WHERE id = ?",
        [*updates.values(), rec["id"]],
    )
    return "updated"


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


POST_COLS = (
    "id handle author_name created_at kind text url in_reply_to_handle in_reply_to_id "
    "conversation_id quoted_id quoted_handle quoted_text likes reposts replies quotes "
    "bookmarks views has_media urls lang fetched_at"
).split()

# Engagement counts drift, so a re-fetch should refresh them rather than keep
# the first numbers seen.
_POST_REFRESH = "likes reposts replies quotes bookmarks views text fetched_at".split()


def upsert_post(conn: sqlite3.Connection, rec: dict) -> str:
    """Insert a post, refreshing engagement counts if we've seen it before."""
    existing = conn.execute("SELECT id FROM posts WHERE id = ?", (rec["id"],)).fetchone()
    values = [rec.get(c) for c in POST_COLS]
    if existing is None:
        conn.execute(
            f"INSERT INTO posts ({','.join(POST_COLS)}) "
            f"VALUES ({','.join('?' * len(POST_COLS))})",
            values,
        )
        return "new"
    updates = {c: rec.get(c) for c in _POST_REFRESH if rec.get(c) is not None}
    if not updates:
        return "unchanged"
    conn.execute(
        f"UPDATE posts SET {','.join(f'{k}=?' for k in updates)} WHERE id = ?",
        [*updates.values(), rec["id"]],
    )
    return "updated"
