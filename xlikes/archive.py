"""Import likes from an official X data archive (Settings -> Download an archive).

The archive's like.js has only the tweet id, body text and permalink — no
author, no date. It's the complete-history fallback; `xlikes fetch` fills in
the rest for anything it can still see on the timeline.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db

_PREFIX = re.compile(r"^\s*window\.YTD\.\w+\.\w+\s*=\s*", re.S)


def _read_like_js(path: Path) -> str:
    path = Path(path)
    if path.is_dir():
        for candidate in (path / "data" / "like.js", path / "like.js"):
            if candidate.exists():
                return candidate.read_text(encoding="utf-8", errors="replace")
        matches = sorted(path.rglob("like.js"))
        if matches:
            return matches[0].read_text(encoding="utf-8", errors="replace")
        raise FileNotFoundError(f"no like.js found under {path}")
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith("like.js")]
            if not names:
                raise FileNotFoundError(f"no like.js inside {path}")
            return zf.read(names[0]).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def parse_like_js(raw: str) -> list[dict]:
    body = _PREFIX.sub("", raw).strip().rstrip(";")
    items = json.loads(body)
    out = []
    for item in items:
        like = item.get("like", item)
        tweet_id = like.get("tweetId")
        if not tweet_id:
            continue
        text = like.get("fullText", "") or ""
        url = like.get("expandedUrl") or f"https://x.com/i/status/{tweet_id}"
        urls = re.findall(r"https?://\S+", text)
        out.append(
            {
                "id": str(tweet_id),
                "url": url,
                "author_handle": None,
                "author_name": None,
                "text": text.strip(),
                "created_at": None,
                "like_rank": None,
                "is_quote": 0,
                "quoted_id": None,
                "quoted_handle": None,
                "quoted_name": None,
                "quoted_text": None,
                "quoted_url": None,
                "has_article": int(any("/i/article/" in u for u in urls)),
                "has_media": 0,
                "urls": "\n".join(urls),
                "source": "archive",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return out


def import_archive(conn, path) -> dict:
    records = parse_like_js(_read_like_js(Path(path)))
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    for rec in records:
        stats[db.upsert(conn, rec)] += 1
    conn.commit()
    stats["total"] = len(records)
    return stats
