# xlikes

Search your X (Twitter) likes from the command line. Your likes get pulled into
a local SQLite full-text index, so finding a post you half-remember takes a
second instead of ten minutes of scrolling.

Everything stays on your machine: your session cookie lives in a local browser
profile, the posts live in `~/.xlikes/likes.db`, and nothing is sent anywhere.

## Setup

Needs Python 3.10 or newer — check with `python3 -V`. If it's older, `brew install python`.

```bash
git clone https://github.com/sha-manav/xlikes
cd xlikes
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
python3 -m pip install playwright
python3 -m playwright install chromium
```

That last step downloads about 140MB of Chromium and sometimes stalls. It's
optional: if you already have Google Chrome (or Edge), skip it — `xlikes fetch`
falls back to whichever browser it finds, and drives it with its own separate
profile, leaving your everyday browsing untouched. Force a specific one with
`xlikes fetch --browser chrome`.

macOS has no bare `pip` command, hence `python3 -m pip`. The virtualenv is what
puts `xlikes` on your PATH; in a new terminal, re-activate it first:

```bash
source ~/xlikes/.venv/bin/activate
```

If you'd rather not bother, run it straight out of the repo directory instead —
searching needs nothing but the standard library:

```bash
python3 -m xlikes.cli search --quotes --articles --recent 200
```

Or put the bundled wrapper on your PATH once and forget about activation
entirely — it finds the virtualenv itself, from any directory:

```bash
sudo ln -s ~/xlikes/bin/xlikes /usr/local/bin/xlikes
```

An alias works too:

```bash
echo "alias xlikes='$HOME/xlikes/bin/xlikes'" >> ~/.zshrc && source ~/.zshrc
```

## Pull your likes

```bash
xlikes fetch
```

A Chromium window opens. Log in to X once — the session is saved, so later runs
just work (add `--headless` to skip the window entirely). It then walks your
Likes tab and indexes what it finds.

By default it walks back 600 likes. For more: `xlikes fetch --max 2000`.
Re-running is cheap and safe — existing posts are updated in place, not
duplicated.

> X removed liked-posts access from its free API tiers, so this reads the same
> JSON your browser already loads when you open your own Likes tab. It needs
> your real session, which is why it drives a browser instead of calling an API.

## Find the post

```bash
xlikes "article is really good"
```

That's the shorthand; `xlikes search ...` is the same thing. Results show the
post, who wrote it, the date, any post it quotes, and a link straight back to it.

**For the one you're hunting** — someone quote-posting an X article and saying
it's good, within your last few weeks of likes:

```bash
xlikes search --quotes --articles --recent 200
```

That lists every quote post involving an article from your 200 most recent
likes — probably a short enough list to just eyeball. To narrow by wording:

```bash
xlikes "really good article" --quotes --recent 200
```

### How matching works

You rarely remember a post word for word, so searches run down a ladder and
stop at the first rung that hits:

1. your words as an exact phrase
2. your words within 3 words of each other
3. your words within 10 words of each other
4. all your words, anywhere in the post
5. all but one of your words (in case one is misremembered)
6. any of your words, ranked by how many matched

So `xlikes "the article is really good"` finds "this article is really good"
first, and still finds "genuinely good article" further down. Article titles and
quoted posts are indexed too, so you can search for what the article was called
even if you only remember that.

### Filters

| flag | effect |
| --- | --- |
| `--recent N` | only your N most recently liked posts |
| `--quotes` | only quote posts |
| `--articles` | only posts that are, or quote, an X article |
| `--links` | only posts containing a link |
| `--author name` | filter by who posted it |
| `--since 3w` `--until 2026-08-01` | when the post was **written** |
| `-n 50` | more results (default 20) |
| `--any` | match any word rather than all |
| `--full` | don't truncate long posts |
| `--json` | machine-readable output |

Note the difference between `--recent` and `--since`: `--recent 200` means
"among my last 200 likes", while `--since 3w` means "the post itself was written
in the last 3 weeks". X's timeline exposes the order you liked things but not the
date you liked them, so **`--recent` is the one to use for "I liked this
recently"** — an old post you liked yesterday still counts.

### Other commands

```bash
xlikes recent -n 40     # most recently liked
xlikes stats            # what's in the index
xlikes export > likes.json
```

## Another account's posts and replies

Separate from your likes: walk a profile's Posts and Replies tabs and record
every post with its engagement counts.

```bash
xlikes user Damnang2 --since 2026-09-01
xlikes user-export Damnang2 --since 2026-09-01 --out damnang2.csv
```

`user` scrolls both tabs and stops once the timeline passes `--since` (profile
timelines are newest-first). `user-export` writes CSV (or `--format json`) with
one row per post: date, kind, text, likes, views, reposts, replies, quotes,
bookmarks, reply/quote context, and a permalink. Narrow with `--kind posts`,
`--kind replies`, `--since` and `--until`.

Posts by other accounts that appear as conversation context — the post being
replied to, the original of a repost, a quoted post — are recorded as context
but never attributed to the profile owner.

Two limits worth knowing before you rely on a count of "every" post:

- X stops serving a profile timeline after roughly **3200 posts**. Further back
  than that isn't reachable this way, and `user` tells you the oldest date it
  managed to reach.
- **View counts only exist for posts from late 2022 onward**, and X sometimes
  omits the number even when it exists. Those rows have an empty `views` cell,
  which means "not reported" — not zero. Every count is also a snapshot from
  when you fetched; re-running `user` refreshes them in place.

Only public accounts work. A protected account is visible only to approved
followers, and a suspended or renamed one won't load at all.

## Archive import (optional)

X's official data export includes your complete like history, further back than
the Likes tab will scroll. Request it under Settings → Your account → Download an
archive of your data (it takes a day or so), then:

```bash
xlikes import-archive ~/Downloads/twitter-archive.zip
```

The archive only carries the post id, text and permalink — no author, no date —
so it's the deep-history fallback. Anything `xlikes fetch` can still see gets
filled in with the full details, and importing never overwrites richer data.

## Notes and limits

- The Likes tab only shows *your own* likes, and only to you.
- X reshapes its internal JSON without notice. The parser finds posts
  structurally rather than by fixed paths, but if a fetch ever returns nothing,
  that's the first place to look.
- Scrolling far back gets rate limited. If a fetch stalls early, wait a few
  minutes and run it again — it picks up what it can and merges.

## Tests

```bash
pip install pytest && python3 -m pytest tests -q
```

Covers parsing real-shaped GraphQL payloads, the search ladder, archive merging,
and — with Chromium present — live browser response capture.
