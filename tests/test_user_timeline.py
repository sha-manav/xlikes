"""Profile-timeline parsing: engagement counts, reply/repost/quote shapes."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xlikes import db  # noqa: E402
from xlikes.fetch import is_likes_response, is_user_timeline_response  # noqa: E402
from xlikes.parse import extract_timeline_posts, metrics, post_kind, view_count  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "user_timeline.json").read_text())
TARGET = "damnang2"


def posts():
    return {r["id"]: r for r in extract_timeline_posts(FIXTURE)}


def mine():
    return {i: r for i, r in posts().items() if r["handle"] == TARGET}


def test_only_the_profile_owners_posts_are_theirs():
    """A conversation module carries the post being replied to, by someone else."""
    everyone = posts()
    assert "3000000000000000001" in everyone           # context tweet is parsed
    assert everyone["3000000000000000001"]["handle"] == "someoneelse"
    assert set(mine()) == {
        "3000000000000000010",  # plain post
        "3000000000000000011",  # quote
        "3000000000000000012",  # repost
        "3000000000000000021",  # reply inside the conversation module
        "3000000000000000030",  # old post, no views
    }


def test_the_original_of_a_repost_is_not_counted_as_their_post():
    everyone = posts()
    # the reposted original and the quoted post belong to other accounts,
    # and must never be attributed to the profile owner
    assert everyone.get("3000000000000000003", {}).get("handle") != TARGET
    assert everyone.get("3000000000000000002", {}).get("handle") != TARGET


def test_kinds():
    rows = mine()
    assert rows["3000000000000000010"]["kind"] == "post"
    assert rows["3000000000000000011"]["kind"] == "quote"
    assert rows["3000000000000000012"]["kind"] == "repost"
    assert rows["3000000000000000021"]["kind"] == "reply"


def test_engagement_counts_and_views():
    row = mine()["3000000000000000010"]
    assert (row["likes"], row["reposts"], row["replies"]) == (1240, 88, 41)
    assert (row["quotes"], row["bookmarks"], row["views"]) == (7, 112, 98000)
    assert row["created_at"] == "2026-09-10T12:00:00+00:00"
    assert row["url"] == "https://x.com/Damnang2/status/3000000000000000010"


def test_views_absent_is_none_not_zero():
    """A post with no view count must not read as zero views."""
    assert mine()["3000000000000000030"]["views"] is None
    assert view_count({"views": {"state": "Enabled"}}) is None
    assert view_count({"views": {"count": "1234"}}) == 1234


def test_reply_context_is_recorded():
    row = mine()["3000000000000000021"]
    assert row["in_reply_to_handle"] == "someoneelse"
    assert row["in_reply_to_id"] == "3000000000000000001"
    assert row["conversation_id"] == "3000000000000000001"


def test_quote_context_is_recorded():
    row = mine()["3000000000000000011"]
    assert row["quoted_handle"] == "thirdparty"
    assert row["quoted_id"] == "3000000000000000002"
    assert "worth quoting" in row["quoted_text"]


def test_metrics_of_an_empty_tweet_are_none_not_zero():
    assert metrics({}) == {"likes": None, "reposts": None, "replies": None,
                           "quotes": None, "bookmarks": None, "views": None}
    assert post_kind({}) == "post"


def test_endpoint_matching_keeps_likes_and_timelines_apart():
    assert is_user_timeline_response("https://x.com/i/api/graphql/abc/UserTweets?variables=%7B%7D")
    assert is_user_timeline_response("https://x.com/i/api/graphql/abc/UserTweetsAndReplies")
    assert is_user_timeline_response("https://x.com/i/api/graphql/abc/UserWithProfileTweetsQueryV2")
    assert not is_user_timeline_response("https://x.com/i/api/graphql/abc/Likes")
    assert not is_user_timeline_response("https://x.com/i/api/graphql/abc/HomeTimeline")
    # and the likes matcher must not swallow profile timelines
    assert not is_likes_response("https://x.com/i/api/graphql/abc/UserTweets")


def test_counts_refresh_on_a_later_fetch(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    row = dict(mine()["3000000000000000010"])
    assert db.upsert_post(conn, row) == "new"
    row["likes"], row["views"] = 2000, 150000
    assert db.upsert_post(conn, row) == "updated"
    stored = conn.execute("SELECT likes, views FROM posts WHERE id = ?", (row["id"],)).fetchone()
    assert (stored["likes"], stored["views"]) == (2000, 150000)


def _cli(db_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "xlikes.cli", "--db", str(db_path), *args],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    )


def test_csv_export_end_to_end(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    for row in mine().values():
        db.upsert_post(conn, row)
    conn.commit()
    conn.close()

    result = _cli(tmp_path / "u.db", "user-export", "Damnang2", "--format", "csv")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("created_at,kind,handle")
    assert len(lines) == 6  # header + 5 posts
    assert "98000" in result.stdout and "1240" in result.stdout
    # newest first
    assert lines[1].startswith("2026-09-12")

    replies = _cli(tmp_path / "u.db", "user-export", "Damnang2", "--kind", "replies")
    assert len(replies.stdout.strip().splitlines()) == 2

    since = _cli(tmp_path / "u.db", "user-export", "Damnang2", "--since", "2026-09-10")
    assert len(since.stdout.strip().splitlines()) == 4  # the 2019 post and the reply drop out

    missing = _cli(tmp_path / "u.db", "user-export", "nobody")
    assert missing.returncode == 1 and "Nothing stored" in missing.stderr
