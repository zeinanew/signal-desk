# Signal Desk

A personal AI and tech news dashboard. Every morning at 8:00 (Riyadh time) GitHub:

1. checks the sources in `sources.json` (news sites, AI lab blogs, arXiv, GitHub, YouTube, Hacker News),
2. picks out stories it hasn't seen before,
3. asks Claude to write a short summary, a "why it matters" line and tags for each one,
4. publishes the result as a website with two tabs: **Feed** (the stories) and **Sources** (how each source is doing).

Click any story to open the original article, paper, repo or video.

The repo already contains stories collected on 27 Sep 2026, so the site has content as soon as it's published.

---

## Setup (about 15 minutes, no coding needed)

### 1. Create the repository
1. Sign in at [github.com](https://github.com) and click **New repository** (the **+** at the top right).
2. Name it `signal-desk`, choose **Public** (GitHub Pages is free for public repos) and click **Create repository**.
3. On the new repo's page, click **uploading an existing file**.
4. Unzip `signal-desk.zip` and drag **everything inside the folder** onto the page, then click **Commit changes**.

> On a Mac, Finder hides folders that start with a dot, so `.github` may not upload. Check that `.github/workflows/refresh.yml` appears in the repo. If it's missing, click **Add file → Create new file**, type `.github/workflows/refresh.yml` as the name, paste in the contents of that file, and commit.

### 2. Add your Claude API key
The summaries are written by Claude through the API. It costs a few cents a day.
1. Get a key at [console.anthropic.com](https://console.anthropic.com) → **API Keys**.
2. In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `ANTHROPIC_API_KEY`. Value: your key. Click **Add secret**.

Without a key the site still works, but it uses each feed's own description in place of an AI summary.

### 3. Turn on the website
1. **Settings → Pages**.
2. Under **Source**, choose **GitHub Actions**.

### 4. Run it once
1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Refresh and publish** → **Run workflow**.
3. After 1–2 minutes your site is live at `https://YOUR-USERNAME.github.io/signal-desk/`.

After that it runs by itself every morning.

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

Every source also needs `id` (unique, lowercase), `name`, `type` (`news`, `lab`, `research`, `github`, `video`, `community`), `topics` and `url` (the page people see).

**Finding feeds:** many sites have one at `/feed`, `/rss` or `/rss.xml`. Every YouTube channel has one at `https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID`. To find the channel ID, open the channel, click **More about this channel → Share channel → Copy channel ID**.

## Settings (optional)
Set these under **Settings → Secrets and variables → Actions → Variables**:

- `CLAUDE_MODEL`: which Claude model writes the summaries. The default is `claude-haiku-4-5`, which is fast and cheap. If Anthropic retires it, set this to a current model from [the models list](https://docs.claude.com/en/docs/about-claude/models).

In `scripts/collect.py` you can also change `MAX_AGE_DAYS` (default 45: older stories drop off) and `PER_SOURCE_LIMIT` (default 8 new stories per source per day).

To change the time: edit the `cron` line in `.github/workflows/refresh.yml`. It's in UTC, so 8am Riyadh is `0 5 * * *`.

## Run it on your own computer (optional)
```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-ant-... python scripts/collect.py
python -m http.server 8000     # then open http://localhost:8000
```

## Files
```
index.html                 the website (Feed + Sources tabs)
sources.json               the list of sources you control
scripts/collect.py         fetches, removes duplicates, summarizes, writes data/
data/items.json            the stories (written by the script)
data/sources.json          each source's last check result (written by the script)
.github/workflows/refresh.yml   the daily schedule
```

Summaries are short and always link to the original. Full articles are never copied.
