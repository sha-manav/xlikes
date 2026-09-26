"""Profile-timeline parsing: engagement counts, reply/repost/quote shapes."""

import json
import subprocess
import sys
from datetime import date, timedelta
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


# --- diagnosing an empty result --------------------------------------------

def test_profile_state_recognises_why_a_timeline_is_empty():
    from xlikes.fetch import profile_state

    assert profile_state("Home\nThis account doesn’t exist\nTry searching") == "missing"
    assert profile_state("This account doesn't exist") == "missing"
    assert profile_state("Account suspended\nX suspends accounts that violate") == "suspended"
    assert profile_state("These posts are protected") == "protected"
    assert profile_state("Caution: This account is temporarily restricted") == "restricted"
    assert profile_state("Sign in to X\nSee what's happening") == "login_wall"
    assert profile_state("Damnang2\n1,204 posts\nFollowing") == "ok"
    assert profile_state("") == "ok"
    assert profile_state(None) == "ok"


def test_capture_does_not_depend_on_the_endpoint_name():
    """X renames its GraphQL operations; author identity is the real test."""
    from xlikes.fetch import collect_posts, graphql_operation

    collected, handles = {}, {}
    added = collect_posts(FIXTURE, TARGET, collected, handles)
    # returns the new records themselves, so callers can persist them at once
    assert len(added) == 5 and len(collected) == 5
    assert all(isinstance(r, dict) and r["handle"] == TARGET for r in added)
    assert handles["someoneelse"] == 1  # tallied, not stored
    assert TARGET not in [h for h in handles if h != TARGET] and handles[TARGET] == 5
    assert all(r["handle"] == TARGET for r in collected.values())

    # re-ingesting the same payload adds nothing new but refreshes in place
    assert collect_posts(FIXTURE, TARGET, collected, handles) == []

    assert graphql_operation("https://x.com/i/api/graphql/abc/SomeRenamedOp?x=1") == "SomeRenamedOp"
    assert graphql_operation("https://x.com/home") is None


def test_wrong_handle_is_distinguishable_from_an_empty_profile():
    from xlikes.fetch import collect_posts

    collected, handles = {}, {}
    collect_posts(FIXTURE, "notthisperson", collected, handles)
    assert collected == {}
    # the tally is what lets the error say "posts found, but none by @you"
    assert sum(handles.values()) > 0 and "damnang2" in handles


def test_empty_result_messages_name_the_actual_cause():
    from xlikes.fetch import no_posts_message

    # wrong handle: posts arrived, just not theirs
    msg = no_posts_message("damnang2", [], {"someoneelse": 12, "damnang2": 0}, {"UserTweets": 2}, [])
    assert "none by @damnang2" in msg and "@someoneelse (12)" in msg

    # profile unreadable
    msg = no_posts_message("damnang2", ["protected"], {}, {"UserTweets": 1}, [])
    assert "only approved followers" in msg

    # X answered but served no posts
    msg = no_posts_message("damnang2", [], {}, {"UserTweets": 3, "UserByScreenName": 1}, [])
    assert "no posts were in the response" in msg and "UserTweets x3" in msg

    # nothing loaded at all
    msg = no_posts_message("damnang2", [], {}, {}, ["boom"], debug_dir="/tmp/d")
    assert "No GraphQL responses at all" in msg
    assert "First response error: boom" in msg and "/tmp/d" in msg


# --- exhaustive capture via search windows ----------------------------------

def test_date_windows_tile_without_gaps_or_overlap():
    from datetime import date
    from xlikes.fetch import date_windows

    windows = date_windows(date(2026, 1, 1), date(2026, 3, 1), 14)
    assert windows[0] == ("2026-02-15", "2026-03-01")   # newest first
    assert windows[-1][0] == "2026-01-01"               # reaches the floor exactly
    # `since:` is inclusive and `until:` exclusive, so each window's start is
    # the previous one's end — no day is scanned twice or skipped
    for newer, older in zip(windows, windows[1:]):
        assert older[1] == newer[0]

    # a range shorter than one window is still covered
    assert date_windows(date(2026, 1, 1), date(2026, 1, 3), 14) == [("2026-01-01", "2026-01-03")]
    assert date_windows(date(2026, 1, 1), date(2026, 1, 1), 14) == []


def test_search_url_operators():
    from xlikes.fetch import search_url

    url = search_url("damnang2", "2026-09-01", "2026-09-15")
    assert "from%3Adamnang2" in url and "since%3A2026-09-01" in url
    assert "until%3A2026-09-15" in url
    assert "f=live" in url            # Latest tab: chronological, most complete
    assert "filter" not in url        # replies included by default
    assert "-filter%3Areplies" in search_url("d", "2026-01-01", "2026-02-01", "exclude")
    assert "filter%3Areplies" in search_url("d", "2026-01-01", "2026-02-01", "only")


def test_invalid_window_is_rejected():
    from datetime import date
    import pytest as _pytest
    from xlikes.fetch import date_windows

    with _pytest.raises(ValueError):
        date_windows(date(2026, 1, 1), date(2026, 2, 1), 0)


def test_coverage_report_names_missing_months(tmp_path):
    conn = db.connect(tmp_path / "c.db")
    rows = list(mine().values())
    for row in rows:
        db.upsert_post(conn, row)
    conn.commit()
    conn.close()

    result = _cli(tmp_path / "c.db", "user-coverage", "Damnang2")
    assert result.returncode == 0, result.stderr
    assert "2026-09" in result.stdout and "2019-03" in result.stdout
    # the long silence between 2019 and 2026 must be reported, not glossed over
    assert "month(s) with nothing stored" in result.stdout
    assert "2019-04" in result.stdout
    assert "--mode search --since 2019-04-01" in result.stdout


# --- account profile: the denominator for completeness ----------------------

PROFILE_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "user_profile.json").read_text()
)


def test_profile_is_read_from_the_account_response():
    from xlikes.parse import extract_user_profile

    got = extract_user_profile(PROFILE_FIXTURE, "Damnang2")
    assert got["handle"] == "damnang2"
    assert got["created_at"] == "2025-10-15T08:12:00+00:00"
    assert got["statuses_count"] == 842
    assert got["followers_count"] == 15300
    assert got["protected"] == 0
    assert extract_user_profile(PROFILE_FIXTURE, "someoneelse") is None


def test_thin_author_copy_inside_tweets_does_not_overwrite_the_real_profile():
    """Tweets embed an author object with no statuses_count; the profile
    response is the only one that has it, and must win."""
    from xlikes.fetch import capture_profile

    holder = {}
    capture_profile(FIXTURE, TARGET, holder)          # timeline first
    assert holder["handle"] == TARGET
    assert holder["statuses_count"] is None
    capture_profile(PROFILE_FIXTURE, TARGET, holder)  # then the profile
    assert holder["statuses_count"] == 842
    capture_profile(FIXTURE, TARGET, holder)          # a later tweet must not clobber it
    assert holder["statuses_count"] == 842


def test_coverage_measures_against_the_profile_count(tmp_path):
    from xlikes.parse import extract_user_profile

    conn = db.connect(tmp_path / "cov.db")
    for row in mine().values():
        db.upsert_post(conn, row)
    db.upsert_account(conn, extract_user_profile(PROFILE_FIXTURE, "Damnang2"))
    conn.commit()
    conn.close()

    out = _cli(tmp_path / "cov.db", "user-coverage", "Damnang2")
    assert out.returncode == 0, out.stderr
    assert "account created: 2025-10-15" in out.stdout
    assert "stored 5 of the 842 posts the profile reports (1%)" in out.stdout
    assert "--window 3" in out.stdout  # under 90%, so it suggests narrowing
    # months before the account existed are not gaps
    assert "2019-04" not in out.stdout
    assert "2025-11" in out.stdout     # but months since creation are


def test_account_upsert_keeps_fields_a_later_read_could_not_see(tmp_path):
    conn = db.connect(tmp_path / "a.db")
    db.upsert_account(conn, {"handle": "damnang2", "statuses_count": 842,
                             "created_at": "2025-10-15T08:12:00+00:00"})
    db.upsert_account(conn, {"handle": "damnang2", "name": "Dam Nang"})
    row = conn.execute("SELECT * FROM accounts WHERE handle='damnang2'").fetchone()
    assert row["statuses_count"] == 842 and row["name"] == "Dam Nang"
    assert row["created_at"] == "2025-10-15T08:12:00+00:00"


def test_posts_are_persisted_as_they_arrive_not_at_the_end(tmp_path):
    """A rate-limited walk gets interrupted; work already done must survive."""
    from xlikes.fetch import Sink, collect_posts

    conn = db.connect(tmp_path / "sink.db")
    sink = Sink(conn, batch=2)
    collected, handles = {}, {}

    sink.write(collect_posts(FIXTURE, TARGET, collected, handles))
    sink.commit()

    # a *separate* connection sees them, i.e. they really are committed
    other = db.connect(tmp_path / "sink.db")
    assert other.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 5
    assert sink.stats["new"] == 5

    # re-running is resumable rather than duplicating
    sink.write(collect_posts(FIXTURE, TARGET, {}, {}))
    sink.commit()
    assert other.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 5


def test_rate_limit_backoff_grows(monkeypatch):
    """Retrying every 20s burns quota; each limit should wait longer."""
    from xlikes import fetch as fetch_mod

    waits = []

    class FakePage:
        def inner_text(self, selector, timeout=0):
            return "Something went wrong. Try reloading."

        def wait_for_timeout(self, ms):
            waits.append(ms // 1000)

        def click(self, selector, timeout=0):
            raise RuntimeError("no retry button")

        def reload(self, wait_until=None):
            pass

    page, state = FakePage(), {}
    for _ in range(5):
        assert fetch_mod._recover_if_stuck(page, False, state)
    backoffs = [w for w in waits if w >= fetch_mod.RATE_LIMIT_BASE_S]
    assert backoffs == [20, 40, 80, 160, 240]      # doubling, then capped
    assert max(backoffs) <= fetch_mod.RATE_LIMIT_MAX_S
    assert state["hits"] == 5


# --- truncation detection ---------------------------------------------------

def _posts_on(*days):
    return {d: {"created_at": f"{d}T12:00:00+00:00"} for d in days}


def test_window_coverage_is_the_oldest_post_inside_the_window():
    from xlikes.fetch import window_coverage

    collected = _posts_on("2026-03-18", "2026-03-20", "2026-03-25")
    assert window_coverage(collected, "2026-03-17", "2026-03-22") == "2026-03-18"
    # posts outside the window must not count towards its coverage
    assert window_coverage(collected, "2026-03-23", "2026-03-27") == "2026-03-25"
    assert window_coverage(collected, "2026-01-01", "2026-01-05") is None
    assert window_coverage({}, "2026-03-17", "2026-03-22") is None


def test_a_window_that_reached_its_start_needs_no_follow_up():
    from xlikes.fetch import continuation_window

    # oldest captured == window start: search got all the way back
    assert continuation_window("2026-03-17", "2026-03-22", "2026-03-17", set()) is None
    # nothing captured at all: an empty window, not a truncated one
    assert continuation_window("2026-03-17", "2026-03-22", None, set()) is None


def test_a_truncated_window_continues_from_where_it_stopped():
    """The real bug: blind halving re-scanned ranges already covered, so the
    halves reported +0 and told us nothing. Continue from the cut-off point."""
    from xlikes.fetch import continuation_window

    # search reached back only to the 20th of a 17th-22nd window
    got = continuation_window("2026-03-17", "2026-03-22", "2026-03-20", set())
    # the 20th is re-included, since it may only be partly captured
    assert got == ("2026-03-17", "2026-03-21")
    assert got[1] < "2026-03-22"          # strictly narrower than the parent
    assert got[0] == "2026-03-17"         # still anchored at the unscanned start


def test_continuation_never_repeats_a_window_or_loops():
    from xlikes.fetch import continuation_window

    # a window truncated on its very last day must not re-propose itself
    got = continuation_window("2026-03-17", "2026-03-19", "2026-03-18", set())
    assert got != ("2026-03-17", "2026-03-19")   # must not re-queue itself
    assert got == ("2026-03-17", "2026-03-18")

    # already-attempted ranges are not queued again
    attempted = {("2026-03-17", "2026-03-21"), ("2026-03-17", "2026-03-20")}
    assert continuation_window("2026-03-17", "2026-03-22", "2026-03-20", attempted) is None

    # a single day that resists narrowing terminates instead of spinning
    assert continuation_window("2026-03-17", "2026-03-18", "2026-03-17", set()) is None


def test_continuation_terminates_when_walked_repeatedly():
    """Drive the loop the way the fetcher does and prove it always halts."""
    from xlikes.fetch import continuation_window

    attempted = set()
    start, end = "2026-03-01", "2026-04-01"
    window = (start, end)
    for _ in range(200):
        attempted.add(window)
        # worst case: each pass reaches back only one further day
        covered = max(start, (date.fromisoformat(window[1]) - timedelta(days=1)).isoformat())
        nxt = continuation_window(window[0], window[1], covered, attempted)
        if nxt is None:
            break
        window = nxt
    else:
        raise AssertionError("continuation did not terminate")
    assert window[0] == start


def test_eta_is_omitted_when_meaningless():
    from xlikes.fetch import _eta

    assert _eta(0.0, 0, 10) == ""      # nothing done yet
    assert _eta(0.0, 10, 10) == ""     # finished
    assert _eta(0.0, 5, 0) == ""       # unknown total (open-ended walk)


# --- filling gaps instead of re-walking what we have ------------------------

def test_gap_ranges_finds_only_sustained_blanks():
    from xlikes.fetch import gap_ranges

    stored = {f"2026-03-{d:02d}" for d in range(1, 11)}
    # everything before the stored run is one contiguous gap
    assert gap_ranges(stored, "2026-02-01", "2026-03-11") == [("2026-02-01", "2026-03-01")]
    # a fully covered span has no gaps
    assert gap_ranges(stored, "2026-03-01", "2026-03-11") == []
    # a one-day silence is normal, not a gap
    assert gap_ranges({"2026-01-01", "2026-01-03"}, "2026-01-01", "2026-01-04") == []
    # a four-day silence is
    assert gap_ranges({"2026-01-01", "2026-01-06"}, "2026-01-01", "2026-01-07") == [
        ("2026-01-02", "2026-01-06")]
    # min_gap_days is adjustable
    assert gap_ranges({"2026-01-01", "2026-01-03"}, "2026-01-01", "2026-01-04",
                      min_gap_days=1) == [("2026-01-02", "2026-01-03")]


def test_gap_windows_skip_covered_months(tmp_path):
    """The actual mistake this fixes: walking newest-first spent its windows
    re-scanning months already captured, and never reached the missing ones."""
    from xlikes.fetch import gap_windows

    # every single day from 1 March onward is covered
    day, have_march_onward = date(2026, 3, 1), set()
    while day < date(2026, 9, 28):
        have_march_onward.add(day.isoformat())
        day += timedelta(days=1)
    windows = gap_windows(have_march_onward, "2025-10-17", "2026-09-28", 5)
    assert windows, "the pre-March span is missing and must be scanned"
    # nothing inside the covered span is scheduled
    assert all(end <= "2026-03-01" for _, end in windows)
    # and the whole missing span is covered
    assert min(s for s, _ in windows) == "2025-10-17"
    assert max(e for _, e in windows) == "2026-03-01"


def test_oldest_first_ordering_puts_the_missing_history_first():
    from datetime import date
    from xlikes.fetch import date_windows

    windows = date_windows(date(2025, 10, 17), date(2026, 4, 1), 5)
    assert windows[0][1] == "2026-04-01"            # newest-first by default
    oldest_first = list(reversed(windows))
    assert oldest_first[0][0] == "2025-10-17"       # what --order oldest does


def test_gap_filling_anchors_on_the_accounts_first_day():
    """The bug this fixes: with no account row, gap-filling anchored on the
    oldest post already stored, so the whole span before it — the actual gap —
    was never examined, and it reported nothing to do."""
    from xlikes.fetch import gap_anchor, gap_windows

    stored = {f"2026-03-{d:02d}" for d in range(1, 29)}

    # the profile's creation date wins over stored data
    assert gap_anchor(None, "2025-10-17T08:12:00+00:00", stored) == "2025-10-17"
    # an explicit --since wins over everything
    assert gap_anchor("2025-09-01T00:00:00+00:00", "2025-10-17", stored) == "2025-09-01"
    # last resort only, and it can only find gaps inside what we have
    assert gap_anchor(None, None, stored) == "2026-03-01"
    assert gap_anchor(None, None, set()) is None

    # anchored on the account: the missing months are found
    anchored = gap_windows(stored, gap_anchor(None, "2025-10-17", stored), "2026-03-29", 5)
    assert anchored and min(s for s, _ in anchored) == "2025-10-17"
    # anchored on stored data: nothing to do, which is the wrong answer
    assert gap_windows(stored, gap_anchor(None, None, stored), "2026-03-29", 5) == []


# --- scaling to a long history ----------------------------------------------

def test_window_size_adapts_to_the_span():
    """Five-day windows over a nine-year account is ~700 page loads, most of
    them over quiet stretches. Start coarse; truncation narrows what it must."""
    from xlikes.fetch import auto_window_days, date_windows

    nine_years = auto_window_days(3500)
    assert nine_years == 45
    assert len(date_windows(date(2017, 2, 1), date(2026, 9, 26), nine_years)) < 90

    # a short span still gets fine-grained windows
    assert auto_window_days(140) == 5
    assert auto_window_days(30) == 5
    assert auto_window_days(0) == 5          # degenerate span
    assert auto_window_days(-10) == 5
    # never coarser than 45 days, however long the history
    assert auto_window_days(100_000) == 45
    # and monotonic in span
    spans = [30, 140, 365, 1000, 3500, 10000]
    sizes = [auto_window_days(x) for x in spans]
    assert sizes == sorted(sizes)


def test_rough_estimate_is_stated_for_long_runs_only():
    from xlikes.fetch import _rough_estimate

    assert _rough_estimate(2) == ""                 # trivially short, no noise
    assert "m" in _rough_estimate(28)
    assert _rough_estimate(77).endswith("32m")
    assert "h" in _rough_estimate(700)              # a multi-hour job says so


def test_gap_threshold_follows_the_accounts_posting_rhythm():
    """Three days is a gap for someone posting daily and normal for someone
    posting twice a week; a fixed threshold re-scans quiet stretches."""
    from xlikes.fetch import suggest_min_gap

    daily = {f"2026-03-{d:02d}" for d in range(1, 29)}
    assert suggest_min_gap(daily) == 3

    twice_weekly = {f"2026-0{m}-{d:02d}" for m in (3, 4)
                    for d in (1, 5, 8, 12, 15, 19, 22, 26)}
    assert 4 <= suggest_min_gap(twice_weekly) <= 6

    # too little data to infer anything: stay at the sensitive default
    assert suggest_min_gap({"2026-03-01", "2026-04-01"}) == 3
    assert suggest_min_gap(set()) == 3

    # a very sparse account is capped, because missing posts costs more than
    # a few redundant windows
    sparse = {"2026-03-01", "2026-03-20", "2026-04-14", "2026-05-02",
              "2026-06-30", "2026-08-11", "2026-09-05"}
    assert suggest_min_gap(sparse) == 14          # capped by the ceiling
    # its median silence is 25 days, so a higher ceiling exposes that instead
    assert suggest_min_gap(sparse, ceiling=30) == 26


def test_a_sparse_account_still_gets_scanned():
    from xlikes.fetch import gap_windows, suggest_min_gap

    sparse = {"2026-03-01", "2026-03-20", "2026-04-14", "2026-05-02"}
    windows = gap_windows(sparse, "2026-03-01", "2026-05-10", 5,
                          suggest_min_gap(sparse))
    assert windows, "long silences must still be searched, not assumed quiet"
