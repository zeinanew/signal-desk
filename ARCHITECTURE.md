# Architecture

Technical reference for how Signal Desk works internally. For setup instructions, see [README.md](README.md).

Signal Desk is a static, serverless news dashboard: a scheduled script collects and
summarizes stories into two JSON files, and a single static HTML page renders them.
There is no backend, database, or build step.

## Tech stack

| Layer | Technology |
|---|---|
| Collector | Python 3.12, `feedparser`, `requests` |
| Summarization | Google Gemini (`google-genai`), with Groq's OpenAI-compatible chat API as a fallback |
| Scheduling / CI | GitHub Actions (`.github/workflows/refresh.yml`) |
| Hosting | GitHub Pages (static files served as-is) |
| Frontend | Single HTML file: vanilla JS, no framework, no bundler; `fetch` + `localStorage` |
| Data store | Two JSON files committed to the repo (`data/items.json`, `data/sources.json`) — the "database" is git itself |
| Config | `sources.json` (user-edited list of feeds) |

No `npm`, no package manager on the frontend, no server process. The only install step is
`pip install -r requirements.txt` for the Python collector.

## Repository layout

```
sources.json                    source list (user-editable config)
scripts/collect.py              the collector/summarizer — the only backend logic
data/items.json                 generated: the stories shown on the Feed/Search tabs
data/sources.json               generated: per-source health/status for the Sources tab
index.html                      the entire frontend (HTML + CSS + JS in one file)
.github/workflows/refresh.yml   cron job that runs the collector and deploys the site
requirements.txt                Python dependencies for the collector
```

`data/*.json` are build artifacts, not hand-edited — the workflow commits them back to
`main` after every run. `sources.json` is the one file a user is expected to edit directly.

## End-to-end flow

```
+-------------------+
| GitHub Actions    |  cron "0 5 * * *" (08:00 Riyadh) or manual "Run workflow"
| refresh.yml       |  also triggers on push to main (except changes under data/)
+---------+---------+
          | pip install -r requirements.txt
          v
+-------------------+
| scripts/          |  reads sources.json + existing data/items.json
| collect.py        |  fetches each enabled source, dedupes, summarizes new items
+---------+---------+
          | writes
          v
+-------------------+
| data/items.json   |---+
| data/sources.json |   |  git commit "Refresh feed YYYY-MM-DD" + push
+-------------------+   |
                        v
               +--------------------+
               | GitHub Pages       |  upload-pages-artifact (whole repo) + deploy-pages
               | (static hosting)   |
               +---------+----------+
                         |
                         v
               +--------------------+
               | index.html         |  fetch()'s data/items.json + data/sources.json
               | (browser)          |  client-side render, filter, search
               +--------------------+
```

Nothing is dynamic at request time — the browser only ever fetches two static JSON
files. All the work (fetching feeds, calling the LLM, deduplicating) happens once a
day in CI, not per page view.

## `scripts/collect.py` — the collector

This is the only piece of backend logic in the repo. Entry point: `main()`.

### 1. Fetch, per source kind

Each entry in `sources.json` has a `kind` that selects a fetcher function (`FETCHERS` dict):

| `kind` | Function | Source |
|---|---|---|
| `rss` | `fetch_rss` | Any Atom/RSS feed, via `feedparser` |
| `html_links` | `fetch_html_links` | Sites with no feed — regex-scrapes `<a href>` tags matching `link_pattern` |
| `github_search` | `fetch_github_search` | GitHub Search API, filtered by `query` + `since_days` |
| `hn` | `fetch_hn` | Hacker News via the Algolia search API, filtered by `min_points` + `keywords` |

Every fetcher normalizes its source into a common shape: `{title, url, date, desc, image, ...}`.
`get()` wraps `requests.get` with 3 retries and backoff, since feeds (YouTube's
especially) intermittently 404/500 on otherwise-working URLs.

**`image`** is best-effort and often `None` - there's no placeholder, the frontend just renders a
text-only card when it's missing. Where it comes from depends on the fetcher, cheapest first:
`fetch_rss` reads `media_thumbnail`/`media_content` when feedparser exposes them (YouTube always
does; most blog RSS doesn't); `fetch_html_links` pulls the first `<img src>` out of the linked
card's own HTML (already fetched, no extra request); `fetch_github_search` doesn't need to look
anywhere - GitHub generates a social-preview image for every repo at a predictable URL
(`opengraph.githubassets.com/1/{full_name}`). Everything else that's still missing an image after
its own fetcher ran gets one more chance in `main()`: `fetch_page_image(url)` fetches the article
page itself and reads its `og:image`/`twitter:image` meta tag - but only for genuinely *new* items
(same scope as summarization, not a backlog crawl) and never for `type == "research"` sources,
since arXiv's `og:image` is confirmed to be the same generic arXiv logo on every paper, not a real
per-paper photo.

### 2. Dedupe

Two independent keys prevent duplicate stories:
- `item_id(url)` — SHA-1 of the URL after `clean_url()` strips tracking params
  (`utm_*`, `ref`, `fbclid`, `gclid`) and trailing slashes. This is the item's `id`.
- `title_key(title)` — lowercased, punctuation-stripped title, to catch the same
  story syndicated at a different URL (e.g. a press release mirrored by two outlets).

An item is skipped if either key has already been seen.

### 3. Summarize

`summarize()` sends a single prompt (`PROMPT`) with the title/URL/feed text to an LLM
and expects back a JSON object: `relevant`, `title` (cleaned/translated), `summary`,
`why`, `topic` (one of `models`, `agents`, `dev`, `research`, `industry`), `tags`, `importance`,
and `story_key` (used for clustering, see below).

Provider fallback chain, per item:
1. **Gemini** — tries each model in `GEMINI_MODELS` in order.
2. **Groq** — if Gemini fails outright (not just rate-limited), tries each model in `GROQ_MODELS`.
3. **Fallback** — if no key is set or every model failed, `fallback()` reuses the feed's
   own description verbatim (untranslated, no "why line", `ai: false`) so the item still
   appears in the feed instead of being dropped.

The model list only advances past a model on a *permanent* failure (`NOT_FOUND`/`404` —
i.e. the model name itself is retired), not on a 429/rate limit, which clears up on its
own within the free tier's per-minute window. See `_should_switch_model`'s docstring —
this distinction was added after a transient rate limit once advanced past the one
working model onto two retired ones, killing Gemini for the rest of that run.

State is tracked in module-level `_model_state`/`_groq_state` dicts so once a model is
found usable, the run sticks with it instead of re-trying earlier (dead) models for
every item.

### 4. Engagement refresh

`refresh_engagement()` runs once per run, before clustering, over every item still within
`ENGAGEMENT_REFRESH_DAYS` (3) of its publish date. HN and GitHub (`github_search`) are the only two
source kinds with a real public popularity number, so only items carrying an `hnId` or
`repoFullName` are re-checked:

- HN: `GET hn.algolia.com/api/v1/search?tags=story_{hnId}` (the lightweight hit shape, not the
  full comment tree) refreshes `points`/`comments`.
- GitHub: `GET api.github.com/repos/{repoFullName}` refreshes `stars`.

Either way, `engagementBonus` is set from `ENGAGEMENT_THRESHOLDS` (points/stars ≥ 1000/500/200 →
+3/+2/+1, capped at `ENGAGEMENT_BONUS_CAP`). This exists because these numbers are otherwise frozen
at whatever they were the moment the item was first fetched - a HN post with modest points on day 1
that climbs to 1,000+ points by day 2 would otherwise never look any more "buzzy" in our data than
it did the moment we first saw it. arXiv was deliberately left out: it has no public per-paper
view/download stats, and citation counts take months to accumulate - of no use inside a 1-3 day
window - so a paper picking up real interest fast is instead expected to surface via a HN/blog pickup,
which clustering (below) already catches.

### 5. Cluster

`cluster_items()` runs once per run, after engagement refresh, over every item that's about to be
kept (not just the new ones). It groups items that are almost certainly about the same underlying
story - a model launch five outlets all cover, say - using two cheap signals, no extra LLM calls:

- an exact match on `story_key`, a short slug the summarizer prompt also asks the LLM for
  (e.g. `"gpt-5-2-release"`), **or**
- Jaccard similarity ≥ 0.5 on each title's significant words,

and only within `CLUSTER_WINDOW_DAYS` (4) of each other, so later unrelated follow-ups don't get
swept in. Within a group, the item with the highest `importance` (earliest `date` as tie-break)
becomes `isPrimary`; every item gets `clusterSize`, `buzzScore` (`importance`, plus a
`min(clusterSize - 1, CLUSTER_BUZZ_CAP)` bonus for being corroborated by more sources, plus that
item's own `engagementBonus` from the step above), and the primary item gets `clusterSources` (the
other sources in its cluster). The frontend's Top Stories section only shows primary items, ranked
by `buzzScore` - that's what keeps a single big story from appearing five times at the top of the
page, while still letting a single-source story that's taking off on its own (HN points, GitHub
stars) outrank a low-importance, single-source story that isn't.

This is a heuristic, not real NLP dedup - two outlets covering the same event with very different
wording and no matching `story_key` can end up in separate clusters. Good enough for "don't repeat
the same headline five times at the top," not a general-purpose story-clustering system.

### 6. Backlog retry

After the main per-source loop, a second pass retries any *existing* item still on the
`fallback()` path (`ai: false`) using its saved `desc`, spending whatever summarization
budget (`MAX_NEW_PER_RUN`) is left. This specifically catches items that have aged out
of a fast-moving source's "latest N" window (so the main loop would never see them
again) but are still within `MAX_AGE_DAYS` — without this pass they'd be stuck on raw
feed text forever.

### 7. Write

Items older than `MAX_AGE_DAYS` (default 45) are dropped. Both JSON files are
overwritten in place: `data/items.json` (sorted newest-first) and `data/sources.json`
(one status row per source — `ok`/`error`/`off`, with `lastChecked`/`lastOk`/`lastNew`).

### Key environment variables / config

| Name | Where set | Purpose |
|---|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | repo Secrets | LLM auth; omit either to disable that provider |
| `GEMINI_MODELS`, `GROQ_MODELS` | repo Variables | ordered comma-separated model fallback lists |
| `MAX_AGE_DAYS`, `PER_SOURCE_LIMIT`, `MAX_NEW_PER_RUN` | env or edit the script constants | retention window, per-source fetch cap, per-run summarization budget |
| `GITHUB_TOKEN` | auto-provided in Actions | raises the GitHub Search API rate limit for `github_search` sources |

## `.github/workflows/refresh.yml` — scheduling & deploy

- Triggers: daily cron, manual `workflow_dispatch`, or a push to `main` that touches
  anything outside `data/` (so editing `sources.json` triggers an immediate refresh,
  but the bot's own data-only commits don't re-trigger themselves).
- `concurrency: { group: refresh, cancel-in-progress: false }` — queues runs instead of
  cancelling, so a manual run and the nightly cron can't race and corrupt `data/*.json`.
- Runs `collect.py`, then commits `data/` back to `main` as `signal-desk-bot`
  (`git pull --rebase --autostash && git push` guards against a concurrent commit).
- Deploys the whole repo as a GitHub Pages artifact (`actions/upload-pages-artifact`),
  since `index.html` fetches `data/*.json` as same-origin static files — no API, no CDN cache to invalidate.

## `index.html` — the frontend

One file, no build step: inline `<style>` and `<script>`, loaded straight by the browser.
All state lives in a single `S` object; there's no framework/virtual DOM — each
`render*()` function re-stringifies its section's `innerHTML` from `S` on every change.

### Data loading

On load, `fetch()`s `data/items.json` and `data/sources.json` in parallel, stores them
in `S.items`/`S.sources`, then calls every `render*()` function once. If the fetch fails
(e.g. the file was opened directly from disk, where `fetch` can't read local files),
it shows a message suggesting GitHub Pages or `python -m http.server` instead.

### Visual direction

The frontend is styled as a dense, wire-service-style dashboard (muted neutral palette, small
sans-serif type, thin hairline borders, sharp 2-4px corners) rather than a soft editorial page -
topic colour (`tp-*`/`--c`) is used as a small left-border accent and a plain-text mono-caps label,
never as a tinted card background, so a page full of stories still reads calmly at a glance.

### Shared card templates

Two functions build every story card in the page, so there's exactly one place that knows how to
render a photo (or gracefully not render one) and one place that knows the "top of a list" card
look:

- `topStoryCard(it, rank)` — the big ranked card used by the homepage Top Stories section **and**
  the "top of category" section on every category tab. Full, un-clamped title/summary/why, a rank
  number, topic/source/time, and the buzz pills (`buzzPills()`).
- `resultCard(it, ws)` — the dense single-line row used by Search results **and** the "rest of the
  articles" list on every category tab (`ws` is the active search words to `<mark>`-highlight;
  category tabs pass `[]`, i.e. no highlighting).

Both render an `<img>` only when `it.image` looks like a valid `http(s)` URL, with an inline
`onerror="this.remove()"` - a 404 or a host that blocks hotlinking just quietly falls back to the
existing text-only look instead of a broken-image icon. The Feed tab's tiles (`tileHTML`) get the
same photo treatment inline, since their paging mechanic is distinct enough not to share the
`topStoryCard`/`resultCard` templates.

A single delegated `click` listener on `document.body` handles every card type everywhere they
appear (`.tscard`/`.resrow`/`.slide` for marking read, `.moretoggle` for the summary popover,
`.tnav` for tile paging, plus the Search/category "Show more" buttons) - one place, instead of a
separate listener per view.

### Top Stories, above the tabs

`renderTopStories()` renders once, right under the header, regardless of which tab is selected -
it's meant to be the first thing read, not something you have to navigate to. `topOfSet()` takes
every item with `isPrimary !== false` (so each cluster counts once) from the last 3 days (widening
to 7 if that's fewer than 4 stories, so it's never empty on a quiet news day) and sorts by
`buzzScore`; `renderTopStories` takes the top 8 of that across *all* topics. A card shows a
"Covered by N sources" pill when part of a multi-source cluster, and/or a "🔥 N pts on HN" / "⭐ N
stars" pill when its `engagementBonus` is positive - so it's visible *why* something ranked where
it did, not just that it did.

### Tabs: Feed, one per category, Search, Sources

`TABS` is `["feed", ...Object.keys(TOPICS), "search", "sources"]` - the 5 category tabs
(Models/Agents/Dev tools/Research/Industry) aren't hand-duplicated blocks; each is just a
`<button>`+`<section>` pair in `index.html` wired to one shared `renderCategory(key)` function.
`nav.tabs` scrolls horizontally on narrow viewports (`overflow-x:auto`) rather than shrinking each
button, since 8 tabs no longer fit by shrinking alone.

- **Feed** (`renderFeed`) — stories from the last 7 days, grouped by source into
  "tiles". Each source gets one tile that pages through its items (arrow keys,
  scroll-wheel, swipe) rather than listing every item flatly — `S.tileIndex` tracks
  per-source position, `changeTile()` moves it.
- **Models / Agents / Dev tools / Research / Industry** (`renderCategory(key)`) — filters
  `S.items` to that one topic, then mirrors the homepage's own pattern: `topOfSet()` again for a
  small "top of category" row (`topStoryCard`), followed by every other story in that topic from
  the last 30 days as a dense `resultCard` list, paginated the same "Show more" way Search is
  (`S.catLimit[key]`).
- **Search & filter** (`renderSearch`) — full-text search (`hay()` concatenates
  title/summary/why/source/tags) intersected with topic/source-type/date-range/unread
  filters. Matched terms are `<mark>`-highlighted (`hl()`). Paginates client-side via
  `S.limit` / a "Show more" button.
- **Sources** (`renderSources`) — per-source health (working/failed/off), counts, and
  a last-checked timestamp, read straight from `data/sources.json`. Includes a
  generated link to edit `sources.json` directly on GitHub (worked out from
  `location.hostname`/`pathname`, since the page doesn't know its own repo name otherwise).

### Persistence (client-side only)

`localStorage` holds two things, both scoped to the browser/device:
- `sd-read` — set of story IDs the user has opened (dims them, powers "unread only")
- `sd-filters` — last-used topic/type/range/unread filter state

There is no server-side read state or per-user anything — two different visitors to
the same deployed site have entirely independent read-history and filters.

### Theming

CSS custom properties define a light palette on `:root`, overridden under
`prefers-color-scheme: dark` and again under `[data-theme="dark"]` (an explicit
override would need to be set by something outside this page, e.g. a wrapper setting
that attribute — the page itself has no theme toggle).

## Data schemas

### `data/items.json`

```jsonc
{
  "updated": "2026-10-01T05:09:07Z",   // ISO 8601 UTC, run timestamp
  "items": [
    {
      "id": "a1b2c3d4e5f6a7b8",        // sha1(clean_url(url))[:16]
      "title": "Clean headline",        // LLM-cleaned, or raw feed title on fallback
      "url": "https://...",             // original article/repo/video
      "source": "techcrunch-ai",        // sources.json id
      "sourceName": "TechCrunch AI",
      "type": "news",                   // news|lab|research|github|video|community
      "date": "2026-09-30T12:00:00Z",   // original publish date
      "addedAt": "2026-10-01T05:09:07Z",// when Signal Desk first saw it
      "summary": "1-2 sentence summary",
      "why": "why it matters, or \"\"",
      "topic": "models",                 // models|agents|dev|research|industry
      "tags": ["tag1", "tag2"],
      "importance": 3,                   // 1-5, from the LLM
      "ai": true,                        // false = still on raw-feed fallback text
      "provider": "gemini",              // gemini|groq|"" (fallback)
      "desc": "raw feed description, truncated", // kept to retry fallback items later
      "image": "https://.../photo.jpg",    // best-effort, often absent - see "Fetch" above
      "discussion": "https://news.ycombinator.com/item?id=...", // hn sources only
      "hnId": "41234567", "points": 820, "comments": 340,        // hn sources only, refreshed while recent
      "repoFullName": "org/repo", "stars": 4200,                 // github_search sources only, refreshed while recent
      "engagementBonus": 2,                // refresh_engagement() - from ENGAGEMENT_THRESHOLDS on points/stars
      "storyKey": "gpt-5-2-release",      // cluster_items() - same slug for items about the same story
      "clusterSize": 3,                   // how many items (across sources) are in this story's cluster
      "isPrimary": true,                  // the one representative item per cluster - Top Stories only shows these
      "clusterSources": ["The Verge AI", "Ars Technica AI"], // other sources in the cluster (primary item only)
      "buzzScore": 8                      // importance + min(clusterSize-1, CLUSTER_BUZZ_CAP) + engagementBonus
    }
  ]
}
```

### `data/sources.json`

```jsonc
{
  "updated": "2026-10-01T05:09:07Z",
  "sources": [
    {
      "id": "techcrunch-ai", "name": "TechCrunch AI", "type": "news",
      "topics": ["industry", "models"], "enabled": true,
      "url": "https://...",
      "status": "ok",            // ok|error|off
      "lastChecked": "2026-10-01T05:09:07Z",
      "lastOk": "2026-10-01T05:09:07Z",  // last time it succeeded (kept even through later errors)
      "lastNew": 8,                       // new items added this run
      "error": "could not read feed (...)" // present only when status is "error"
    }
  ]
}
```

### `sources.json` (user config — see README for the full field table per `kind`)

Each entry needs `id`, `name`, `type`, `kind`, `topics`, `enabled`, `url`, plus
kind-specific fields (`feed`, `link_pattern`/`base`, `query`/`since_days`, `min_points`/`keywords`).
An optional `limit` caps how many of that source's newest items are considered per run
(falls back to `PER_SOURCE_LIMIT`).

## Design decisions worth knowing

- **Why commit `data/` back to the repo instead of a database**: GitHub Pages only
  serves static files, and the goal was zero infrastructure. Git history of `data/`
  doubles as an audit log of every day's refresh.
- **Why per-source "tiles" with paging instead of one flat list**: with ~20 sources
  firing a handful of items a day, a flat feed would be dominated by whichever source
  posts most often. Tiles guarantee every source gets visible space.
- **Why two separate summarization providers**: Gemini's free tier has a low daily
  request cap; Groq is a free backup so the feed doesn't degrade to raw/untranslated
  feed text once that cap is hit mid-run.
- **Why rate-limit errors don't advance the model list but "not found" errors do**: see
  `_should_switch_model` in `collect.py` — conflating the two previously caused a single
  transient rate limit to permanently skip past the one working model.
- **Why X/Twitter sources are plain `rss` entries pointed at a bridge URL, not a dedicated
  fetcher**: the official API's cheapest timeline-reading tier costs real money; a third-party
  RSS bridge (self-hosted RSSHub, or a paid service like RSS.app) turns an account into an
  ordinary RSS feed, so `fetch_rss` already handles it — no new code, just a `sources.json` entry
  the user points at whichever bridge they choose.
- **Why clustering is a cheap heuristic instead of another LLM call**: it runs over every kept
  item on every run (not just new ones), so an extra API call per item would multiply the
  already rate-limited summarization budget. A `story_key` match plus title-word overlap, scoped
  to a few days, is enough to stop the Top Stories section from repeating one event five times —
  it doesn't need to be a general story-dedup system.
- **Why buzz also comes from re-checked HN points / GitHub stars, not just cross-source
  corroboration**: `clusterSize` alone only rewards a story once several *different* outlets have
  covered it. A story can just as easily become genuinely buzzy on a single source — a HN post
  that had modest points when first fetched but climbs past 1,000 over the next day — and that
  was invisible before, since those numbers used to be frozen at first-fetch time. `arXiv` was
  deliberately left out of this: it has no public per-paper view/download stats, and citation
  counts take months to show up, so there's no signal there that moves within the few days this
  matters for. A paper that's actually taking off is expected to surface indirectly, via a HN/blog
  pickup that clustering already catches.
