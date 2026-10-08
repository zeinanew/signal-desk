# Signal Desk

A personal AI and tech news dashboard. Every morning at 8:00 (Riyadh time), the collector:

1. checks the sources in `sources.json` (news sites, AI lab blogs, arXiv, GitHub, YouTube, Hacker News, optionally X/Twitter),
2. picks out stories it hasn't seen before,
3. asks Gemini to write a short summary, a "why it matters" line, topic/tags and an importance score for each one,
4. groups stories that are really the same underlying story (e.g. a model launch five outlets all cover) so it only needs to be read once,
5. publishes the result as a website: a **Top stories** section you can skim in about two minutes, a tab per category (Models, Agents, Dev tools, Research, Industry) that follows the same "top of category, then everything else" pattern, plus **Feed**, **Search & filter** and **Sources** tabs for browsing everything.

Click any story to open the original article, paper, repo or video. Where one's available, a small photo is pulled in alongside the story (an og:image from the article, GitHub's own repo preview image, a YouTube thumbnail, ...) - not every story has one, and that's fine, the card just stays text-only.

**Top stories** is ranked by a "buzz score" - the AI's 1-5 importance rating, plus a bonus the more
distinct sources are covering the same event - so the handful of things most worth knowing about
surface at the very top of the page, above the tabs, every time you open it.

The repo already contains stories collected on 27 Sep 2026, so the site has content as soon as it's published.

---

## Setup (about 15 minutes, no coding needed)

### 1. Create the repository
1. Sign in at [github.com](https://github.com) and click **New repository** (the **+** at the top right).
2. Name it `signal-desk`, choose **Public** (GitHub Pages is free for public repos) and click **Create repository**.
3. On the new repo's page, click **uploading an existing file**.
4. Unzip `signal-desk.zip` and drag **everything inside the folder** onto the page, then click **Commit changes**.

> On a Mac, Finder hides folders that start with a dot, so `.github` may not upload. Check that `.github/workflows/refresh.yml` appears in the repo. If it's missing, click **Add file → Create new file**, type `.github/workflows/refresh.yml` as the name, paste in the contents of that file, and commit.

### 2. Add your Gemini API key
The summaries are written by Gemini through the API. It costs a few cents a day (Google also offers a free tier).
1. Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. In your repo, go to **Settings → Secrets and variables → Actions → Secrets tab → New repository secret** (not the Variables tab - the workflow only reads secrets).
3. Name: `GEMINI_API_KEY`. Value: your key, no quotes around it. Click **Add secret**.

Without a key the site still works, but it uses each feed's own description in place of an AI summary.

**Optional: add Groq as a backup.** Free-tier Gemini keys are capped at a small number of requests per day per model, so once that's used up the rest of that day's stories fall back to the raw feed text. Adding a Groq key (free, no card needed) lets the script fall through to it once Gemini's quota is exhausted, instead of giving up:
1. Get a key at [console.groq.com/keys](https://console.groq.com/keys).
2. Add it the same way as above, as a repository secret named `GROQ_API_KEY`.

(Don't mix this up with Grok/xAI - a similarly-named but different service that no longer has a free tier.)

### 3. Turn on the website
1. **Settings → Pages**.
2. Under **Source**, choose **GitHub Actions**.

### 4. Run it once
1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Collect and refresh** → **Run workflow**.
3. After 1–2 minutes your site is live at `https://YOUR-USERNAME.github.io/signal-desk/` (the
   separate **Deploy to Pages** workflow publishes it automatically whenever `data/` changes).

By itself, that workflow only runs again when you edit `sources.json` or click **Run workflow**
yourself - there's no cron in GitHub any more. For a hands-off daily refresh, either set up the
Windows Task Scheduler job below (runs on your own machine) or add a `schedule:` trigger back into
`.github/workflows/refresh.yml` if you'd rather it run in the cloud (just don't run both on the
same schedule - see "Run the daily refresh locally" for why).

---

## Changing the sources
Edit `sources.json` on GitHub. There's a link to it on the site's Sources tab.

- **Turn a source off:** set `"enabled": false`.
- **Remove a source:** delete its `{ ... }` block (and the comma before it).
- **Add a source:** copy a block of the same kind and change it:

| kind | Use for | Fields |
|---|---|---|
| `rss` | Any site or YouTube channel with a feed | `feed` = the feed URL |
| `github_search` | New repos matching a GitHub search | `query`, `since_days` |
| `hn` | Hacker News stories | `min_points`, `keywords` |
| `html_links` | Sites with no feed (e.g. Anthropic) | `link_pattern`, `base` |

Every source also needs `id` (unique, lowercase), `name`, `type` (`news`, `lab`, `research`, `github`, `video`, `community`), `topics` (any of `models`, `agents`, `dev`, `research`, `industry`) and `url` (the page people see).

**Finding feeds:** many sites have one at `/feed`, `/rss` or `/rss.xml`. Every YouTube channel has one at `https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID`. To find the channel ID, open the channel, click **More about this channel → Share channel → Copy channel ID**.

### Following an X/Twitter account

X doesn't offer a free RSS feed any more, and the official API's cheapest tier that can read a
timeline costs real money. `sources.json` already includes a few disabled example entries (Elon
Musk, Sam Altman, OpenAI, Anthropic, Google DeepMind) - to turn one on:

1. Get an RSS URL for that account from a bridge service - either a self-hosted
   [RSSHub](https://docs.rsshub.app/) instance (free, route looks like `/twitter/user/elonmusk`),
   or a paid service like [RSS.app](https://rss.app/). **Both are third-party**: they can go down,
   get rate-limited, or change behavior without notice, and an unofficial bridge may be against
   X's terms of service - that's the tradeoff for not paying for the official API.
2. Set that entry's `feed` to the bridge's RSS URL and `enabled` to `true`.
3. It behaves exactly like any other `rss` source from then on - same dedupe, summarizing, and
   per-source tile.

To add a different account, copy one of the `x-*` blocks and change `id`, `name`, `url` and `feed`.

## Settings (optional)
Set these under **Settings → Secrets and variables → Actions → Variables**:

- `GEMINI_MODELS`: a comma-separated list of Gemini models to try, in order (default `gemini-3.8-flash`). If a model's been retired, the script moves on to the next one in the list - a plain rate limit (the free tier allows a handful of requests per minute) doesn't advance the list, since that clears up on its own within the run. Google renames/retires these fairly often - if summaries stop working, check the Action log for the exact error and see [the models list](https://ai.google.dev/gemini-api/docs/models) for current names.
- `GROQ_MODELS`: same idea, for the Groq backup (default `openai/gpt-oss-20b,openai/gpt-oss-120b,qwen/qwen3.6-27b`). Only used once every model in `GEMINI_MODELS` has failed for an item. Groq deprecates/renames models fairly often for the free tier - see [Groq's models list](https://console.groq.com/docs/models) for current names if summaries stop coming from it.

In `scripts/collect.py` you can also change `MAX_AGE_DAYS` (default 45: older stories drop off) and `PER_SOURCE_LIMIT` (default 8 new stories per source per day).

To change the daily time: the schedule now lives in the Windows Task Scheduler job described
below (GitHub Actions no longer runs on a cron, to avoid it and the local job racing each other -
see "Run the daily refresh locally").

## Run it on your own computer (optional)
```bash
pip install -r requirements.txt
GEMINI_API_KEY=... GROQ_API_KEY=... python scripts/collect.py   # GROQ_API_KEY is optional
python -m http.server 8000     # then open http://localhost:8000
```

## Run the daily refresh locally (Windows Task Scheduler)

As an alternative (or backup) to the cloud scheduler, `scripts/refresh_local.ps1` runs `collect.py`
and then commits + pushes `data/` if anything changed - the same thing the GitHub Action/Azure
Pipeline does, just from your own machine.

1. **Set your API keys as permanent environment variables** (Task Scheduler doesn't inherit a
   terminal session's env vars): **Start → "Edit the system environment variables" → Environment
   Variables → New...** (under your user account) → add `GEMINI_API_KEY` and `GROQ_API_KEY` with
   your key values. Close and reopen any terminal afterward for it to pick them up.
2. The task is already registered (named `SignalDeskLocalRefresh`, daily at 8:00 AM, runs
   `scripts\refresh_local.ps1`) - check/edit it any time in **Task Scheduler** (`taskschd.msc`), or
   re-create it with:
   ```
   schtasks /Create /SC DAILY /ST 08:00 /TN "SignalDeskLocalRefresh" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo path>\scripts\refresh_local.ps1\"" /RL LIMITED /F
   ```
3. Output from each run is appended to `refresh-local.log` in the repo root (gitignored) - check
   it if a run doesn't seem to have done anything.

This only runs while your machine is on and you're logged in - unlike the cloud schedulers, it
won't fire if the laptop is off or asleep at 8:00. Fine as a personal/backup refresh; not a
substitute for the cloud scheduler if you need it to run no matter what.

## Running on Azure DevOps instead of GitHub

If this repo lives in Azure DevOps rather than GitHub, `azure-pipelines.yml` (repo root) replaces
`.github/workflows/refresh.yml`, and Azure Static Web Apps replaces GitHub Pages as the live host.
See `ARCHITECTURE.md`'s "Running this on Azure DevOps instead of GitHub" section for the one-time
setup (creating the Static Web App, adding Pipeline variables, repo permissions).

## Files
```
index.html                 the website (Feed + Sources tabs)
sources.json               the list of sources you control
scripts/collect.py         fetches, removes duplicates, summarizes, writes data/
data/items.json            the stories (written by the script)
data/sources.json          each source's last check result (written by the script)
scripts/refresh_local.ps1  what Task Scheduler runs daily (the actual schedule now lives here)
.github/workflows/refresh.yml   collects on demand / when sources.json changes (GitHub Actions)
.github/workflows/deploy.yml    publishes to Pages on every push (GitHub Actions)
azure-pipelines.yml        the daily schedule (Azure Pipelines) - see "Running on Azure DevOps" above
```

Summaries are short and always link to the original. Full articles are never copied.
