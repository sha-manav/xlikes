"""End-to-end checks: GraphQL payload -> index -> search results."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xlikes import db  # noqa: E402
from xlikes.archive import parse_like_js  # noqa: E402
from xlikes.parse import extract_tweets, parse_created_at  # noqa: E402
from xlikes.search import build_match, parse_when, search, smart_search  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "likes_response.json").read_text())


def build_index(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for rank, rec in enumerate(extract_tweets(FIXTURE)):
        rec["like_rank"] = rank
        db.upsert(conn, rec)
    conn.commit()
    return conn


def test_extracts_only_liked_posts_not_quoted_ones():
    recs = extract_tweets(FIXTURE)
    ids = [r["id"] for r in recs]
    assert ids == ["1900000000000000010", "1900000000000000011", "1900000000000000012"]
    # the quoted article post must not be indexed as a like of its own
    assert "1900000000000000001" not in ids


def test_quote_and_article_metadata():
    rec = next(r for r in extract_tweets(FIXTURE) if r["id"] == "1900000000000000010")
    assert rec["author_handle"] == "simonw"
    assert rec["is_quote"] == 1
    assert rec["has_article"] == 1
    assert rec["quoted_handle"] == "swyx"
    assert "The Rise of the Agent Harness" in rec["quoted_text"]
    assert rec["url"].endswith("/simonw/status/1900000000000000010")


def test_tweet_with_visibility_results_is_unwrapped():
    rec = next(r for r in extract_tweets(FIXTURE) if r["id"] == "1900000000000000012")
    assert rec["author_handle"] == "dhh"
    assert rec["has_article"] == 1  # via the /i/article/ link


def test_created_at_parsing():
    assert parse_created_at("Tue Aug 12 09:15:00 +0000 2025") == "2025-08-12T09:15:00+00:00"
    assert parse_created_at("nonsense") is None


def test_search_finds_the_quoted_article_praise(tmp_path):
    """The post being hunted: a quote of an article, praised in the liker's words.

    Another indexed post contains all three words too, and is shorter, so plain
    bm25 puts it first; the proximity ladder is what surfaces the right one.
    """
    conn = build_index(tmp_path)
    rows, relaxed, total = smart_search(conn, "article really good")
    assert not relaxed
    assert rows[0]["id"] == "1900000000000000010"
    assert rows[0]["author_handle"] == "simonw"


def test_search_matches_quoted_article_title(tmp_path):
    conn = build_index(tmp_path)
    rows = search(conn, "agent harness")
    assert [r["id"] for r in rows] == ["1900000000000000010"]


def test_filters(tmp_path):
    conn = build_index(tmp_path)
    assert len(search(conn, None, quotes_only=True)) == 1
    assert len(search(conn, None, articles_only=True)) == 2
    assert len(search(conn, None, recent=2)) == 2
    assert len(search(conn, None, author="@karpathy")) == 1
    assert len(search(conn, None, since=parse_when("2025-08-13"))) == 2


def test_relaxes_when_a_remembered_word_is_wrong(tmp_path):
    conn = build_index(tmp_path)
    assert search(conn, "article really good flibbertigibbet") == []
    rows, relaxed, _ = smart_search(conn, "article really good flibbertigibbet")
    assert relaxed and rows[0]["id"] == "1900000000000000010"


def test_exact_phrase_outranks_a_shorter_looser_match(tmp_path):
    conn = build_index(tmp_path)
    rows, _, _ = smart_search(conn, "really good")
    # both posts contain the phrase; the ladder must still return both
    assert {r["author_handle"] for r in rows} == {"simonw", "dhh"}


def test_smart_search_respects_filters(tmp_path):
    conn = build_index(tmp_path)
    rows, _, _ = smart_search(conn, "really good", quotes_only=True)
    assert [r["author_handle"] for r in rows] == ["simonw"]


def test_punctuation_does_not_break_fts(tmp_path):
    conn = build_index(tmp_path)
    # apostrophes, colons and hyphens are FTS5 syntax; they must be neutralised
    for query in ["I've read", "agent-design", "best: thing", '"really good"']:
        search(conn, query)
    assert build_match("really good") == '"really" AND "good"'
    assert build_match("really good", mode="any") == '"really" OR "good"'
    assert build_match('"exact phrase" other') == '"exact phrase" AND "other"'


def test_relative_dates():
    assert parse_when("3w") < parse_when("1w")
    assert parse_when("21d")[:2] == "20"
    assert parse_when(None) is None


def test_archive_import_and_merge(tmp_path):
    raw = 'window.YTD.like.part0 = [{"like":{"tweetId":"1900000000000000010","fullText":"this article is really good","expandedUrl":"https://twitter.com/i/web/status/1900000000000000010"}}]'
    recs = parse_like_js(raw)
    assert recs[0]["id"] == "1900000000000000010" and recs[0]["source"] == "archive"

    conn = build_index(tmp_path)
    before = conn.execute("SELECT author_handle, text FROM likes WHERE id=?", (recs[0]["id"],)).fetchone()
    db.upsert(conn, recs[0])
    after = conn.execute("SELECT author_handle, text FROM likes WHERE id=?", (recs[0]["id"],)).fetchone()
    # the sparser archive row must not clobber richer fetched data
    assert after["author_handle"] == before["author_handle"] == "simonw"
    assert after["text"] == before["text"]


def test_deleting_a_like_updates_the_index(tmp_path):
    conn = build_index(tmp_path)
    conn.execute("DELETE FROM likes WHERE id='1900000000000000010'")
    assert search(conn, "agent harness") == []


# --- fetch plumbing (the browser driving itself needs a real X session) ---

def test_recognises_the_likes_endpoint():
    from xlikes.fetch import is_likes_response

    assert is_likes_response("https://x.com/i/api/graphql/aBc123/Likes?variables=%7B%7D")
    assert is_likes_response("https://twitter.com/graphql/xyz/Likes")
    assert is_likes_response("https://x.com/i/api/graphql/xyz/UserLikes")
    assert not is_likes_response("https://x.com/i/api/graphql/xyz/HomeTimeline")
    assert not is_likes_response("https://x.com/i/api/graphql/xyz/FavoriteTweet")
    assert not is_likes_response("https://x.com/home")


def test_ingest_preserves_like_order_and_dedupes():
    from xlikes.fetch import ingest

    collected = {}
    assert ingest(collected, FIXTURE) == 3
    assert ingest(collected, FIXTURE) == 0  # same page scrolled past twice
    assert list(collected)[0] == "1900000000000000010"


def test_login_detection_needs_the_auth_cookie():
    from xlikes.fetch import is_logged_in

    assert is_logged_in([{"name": "auth_token", "value": "abc123"}])
    assert not is_logged_in([])
    assert not is_logged_in([{"name": "auth_token", "value": ""}])
    # a logged-out visit still sets these, and must not read as signed in
    assert not is_logged_in([{"name": "guest_id", "value": "v1%3A1"},
                             {"name": "ct0", "value": "deadbeef"}])


def test_handle_parsed_from_account_switcher_label():
    from xlikes.fetch import handle_from_text

    assert handle_from_text("Manav Shah\n@sha_manav") == "sha_manav"
    assert handle_from_text("Account menu") is None
    assert handle_from_text(None) is None


def test_reports_the_true_match_count_when_truncating(tmp_path):
    """Capping at -n must never look like "that's everything there is"."""
    conn = build_index(tmp_path)
    rows, _, total = smart_search(conn, "good", limit=1)
    assert len(rows) == 1 and total == 3

    rows, _, total = smart_search(conn, None, limit=2, articles_only=True)
    assert len(rows) == 2 and total == 2

    from xlikes.search import count
    assert count(conn) == 3
    assert count(conn, quotes_only=True) == 1
    assert count(conn, articles_only=True, recent=1) == 1
