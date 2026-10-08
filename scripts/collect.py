"""Signal Desk collector.

Reads sources.json, pulls the newest items from each enabled source,
skips anything already seen, asks Gemini (with Groq as a backup once
Gemini's daily quota runs out) to summarize and tag the new items,
and writes:

  data/items.json    - the feed shown on the Feed tab
  data/sources.json  - each source plus its last check result (Sources tab)

Run locally:  GEMINI_API_KEY=... python scripts/collect.py
Add GROQ_API_KEY=... too to use Groq as a fallback once Gemini's quota runs out.
Without any key it still runs, using the feed's own description as the summary.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ITEMS_FILE = DATA / "items.json"
STATUS_FILE = DATA / "sources.json"
SOURCES_FILE = ROOT / "sources.json"

MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "45"))       # drop stories older than this
DEFAULT_LIMIT = int(os.getenv("PER_SOURCE_LIMIT", "8"))   # newest N per source per run
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "80")) # cap on summarization calls
MODELS = [m.strip() for m in (os.getenv("GEMINI_MODELS") or os.getenv("GEMINI_MODEL")
                               or "gemini-3.8-flash").split(",") if m.strip()]
GROQ_MODELS = [m.strip() for m in (os.getenv("GROQ_MODELS") or os.getenv("GROQ_MODEL")
                                    or "openai/gpt-oss-20b,openai/gpt-oss-120b,qwen/qwen3.6-27b").split(",") if m.strip()]
TOPICS = ["models", "agents", "dev", "research", "industry"]
CLUSTER_WINDOW_DAYS = 4   # stories further apart than this are never clustered together
CLUSTER_BUZZ_CAP = 3      # max bonus buzzScore gets from being covered by many sources
CLUSTER_JACCARD = 0.5          # title-token overlap needed to cluster two different sources
SAME_SOURCE_JACCARD = 0.2      # ...needed when both items are from the same source - a single-
                                # repo release feed only ever posts about one project, so even a
                                # small shared-token signal reliably means "same project, new release"
ENGAGEMENT_REFRESH_DAYS = 3   # re-check HN points / GitHub stars for items at most this old
ENGAGEMENT_BONUS_CAP = 3      # max bonus buzzScore gets from a single source's own engagement
ENGAGEMENT_THRESHOLDS = [(1000, 3), (500, 2), (200, 1)]  # (points/stars >=, bonus), checked in order
GEMINI_CALL_TIMEOUT = 25  # seconds - the genai SDK sets no timeout of its own, so a dropped/stalled
                          # connection to Gemini can otherwise hang a single call indefinitely
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 256          # truncated (Matryoshka) output - calibrated against the full 3072-dim
                          # vector and separates same-story/different-story pairs just as well,
                          # at a fraction of the size to cache and commit
EMBED_SIMILARITY_THRESHOLD = 0.88  # calibrated on real items: same-story pairs (same event, two
                          # outlets with very different wording) scored ~0.92-0.93; different
                          # stories, even from the same company, scored ~0.81-0.84 - 0.88 sits
                          # cleanly between the two with margin on both sides
EMBED_CACHE_FILE = DATA / "embeddings_cache.json"  # not used by the frontend - collect.py's own
                          # cross-run cache so new items can be compared against recent ones
                          # without re-embedding them every run
EMBED_BACKFILL_CAP = 150  # per-run safety cap on backfilling embeddings for pre-existing recent
                          # items that predate this feature (or just missed it) - only matters on
                          # the first run or two; after that, almost everything is already cached
UA = {"User-Agent": "SignalDesk/1.0 (+https://github.com; personal news dashboard)"}
NOW = dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- helpers
def iso(d: dt.datetime | None) -> str | None:
    return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if d else None


def parse_date(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, time.struct_time):
        return dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(clean_url(url).encode()).hexdigest()[:16]


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:80]


def slugify(text: str, limit: int = 60) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:limit]


_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is", "are", "with",
    "at", "by", "from", "its", "it", "new", "now", "how", "why", "what",
}


def title_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOPWORDS and len(w) > 2}


def strip_html(text: str, limit: int = 1200) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def get(url: str, **kw) -> requests.Response:
    """A couple of retries for transient errors - YouTube's feed endpoint in particular
    intermittently 404s/500s on otherwise-working channel URLs."""
    headers = {**UA, **kw.pop("headers", {})}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=25, **kw)
            r.raise_for_status()
            return r
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def call_with_timeout(fn, timeout: float, *args, **kwargs):
    """Runs fn(*args, **kwargs) with a hard wall-clock ceiling, raising TimeoutError if it's
    not back by then. For SDK calls (like Gemini's) that set no timeout of their own and can
    otherwise hang indefinitely on a stalled/dropped connection. The worker thread is a daemon
    so an abandoned call that never returns doesn't keep the process alive waiting for it."""
    result: list = []
    error: list[BaseException] = []

    def target():
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            error.append(exc)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"timed out after {timeout}s")
    if error:
        raise error[0]
    return result[0]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def embed_text(client, text: str) -> list[float] | None:
    """Best-effort semantic embedding for cross-source duplicate detection - title-token
    overlap alone misses two outlets covering the same event in very different words (e.g.
    "ChatGPT's Intelligent UI update..." vs "GPT-6 and Intelligent UI for everyone"), which an
    embedding comparison catches. Never raises - a missing embedding just means that one item
    can't contribute to this check, falling back to the existing text-based clustering signals."""
    try:
        from google.genai import types
        resp = call_with_timeout(
            client.models.embed_content, GEMINI_CALL_TIMEOUT,
            model=EMBED_MODEL, contents=text,
            config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=EMBED_DIM),
        )
        return [round(v, 6) for v in resp.embeddings[0].values]
    except Exception as exc:  # noqa: BLE001 - embeddings are an aid, not critical path
        print(f"    embedding failed: {exc}", file=sys.stderr)
        return None


_META_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og|twitter):image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og|twitter):image(?::secure_url)?["\']',
    re.I,
)


def extract_meta_image(html_text: str) -> str | None:
    m = _META_IMAGE_RE.search(html_text or "")
    return html.unescape(m.group(1) or m.group(2)) if m else None


def fetch_page_image(url: str) -> str | None:
    """Best-effort og:image lookup for sources whose feed carries no photo of its own.
    Never raises - a missing/unreachable photo should just mean no photo, not a failed run."""
    try:
        return extract_meta_image(get(url).text)
    except Exception:
        return None


def feed_entry_image(e) -> str | None:
    thumb = e.get("media_thumbnail")
    if thumb and thumb[0].get("url"):
        return thumb[0]["url"]
    for m in e.get("media_content") or []:
        if (m.get("type") or "").startswith("image") or not m.get("type"):
            if m.get("url"):
                return m["url"]
    return None


# ---------------------------------------------------------------- fetchers
def fetch_rss(src: dict) -> list[dict]:
    feed = feedparser.parse(get(src["feed"]).content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"could not read feed ({feed.bozo_exception})")
    out = []
    for e in feed.entries:
        link = e.get("link") or ""
        if not link:
            continue
        out.append({
            "title": strip_html(e.get("title", ""), 300),
            "url": link,
            "date": parse_date(e.get("published_parsed") or e.get("updated_parsed")),
            "desc": strip_html(e.get("summary") or e.get("description") or ""),
            "image": feed_entry_image(e),
        })
    return out


def fetch_html_links(src: dict) -> list[dict]:
    """For sites without a feed: collect links whose path matches a pattern. A given href
    usually appears multiple times on a listing page (an image-only card wrapper, a title-only
    heading, a "learn more" link, ...) - gather every occurrence of each href before picking a
    title (first occurrence with real text) and an image (first occurrence with one), since
    they're often on different occurrences of the same link rather than the same one."""
    page = get(src["feed"]).text
    pattern = re.compile(src["link_pattern"])
    by_href: dict[str, list[str]] = {}
    for href, inner in re.findall(r'<a[^>]+href="([^"#?]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I):
        if pattern.search(href):
            by_href.setdefault(href, []).append(inner)

    out = []
    for href, inners in by_href.items():
        text, image = "", None
        for inner in inners:
            if not text:
                candidate = strip_html(inner, 400)
                if len(candidate) >= 12:
                    text = candidate
            if not image:
                img_match = re.search(r'<img[^>]+src="([^"]+)"', inner, flags=re.I)
                if img_match:
                    image = html.unescape(img_match.group(1))
        if not text:
            continue
        url = href if href.startswith("http") else src["base"].rstrip("/") + href
        # Link text often runs together category + title + date; the summarizer cleans the title.
        date_match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
        date = dt.datetime.strptime(date_match.group(1), "%b %d, %Y").replace(tzinfo=dt.timezone.utc) if date_match else None
        if image and not image.startswith("http"):
            image = src["base"].rstrip("/") + image
        out.append({"title": text[:200], "url": url, "date": date, "desc": text, "needs_title": True, "image": image})
    return out


def github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def fetch_github_search(src: dict) -> list[dict]:
    since = (NOW - dt.timedelta(days=src.get("since_days", 7))).date().isoformat()
    r = get("https://api.github.com/search/repositories", headers=github_headers(),
            params={"q": src["query"].format(since=since), "sort": "stars", "order": "desc", "per_page": 20})
    out = []
    for repo in r.json().get("items", []):
        out.append({
            "title": repo["full_name"],
            "url": repo["html_url"],
            "date": parse_date(repo.get("created_at")),
            "desc": f"{repo.get('description') or ''} (Language: {repo.get('language') or 'n/a'}; "
                    f"{repo.get('stargazers_count', 0)} stars)",
            "repoFullName": repo["full_name"], "stars": repo.get("stargazers_count", 0),
            "image": f"https://opengraph.githubassets.com/1/{repo['full_name']}",
        })
    return out


def fetch_hn(src: dict) -> list[dict]:
    since = int((NOW - dt.timedelta(days=2)).timestamp())
    r = get("https://hn.algolia.com/api/v1/search", params={
        "tags": "story", "hitsPerPage": 100,
        "numericFilters": f"points>{src.get('min_points', 100)},created_at_i>{since}"})
    words = [w.lower() for w in src.get("keywords", [])]
    out = []
    for hit in sorted(r.json().get("hits", []), key=lambda h: -h.get("points", 0)):
        title = hit.get("title") or ""
        low = f" {title.lower()} "
        if words and not any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            continue
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        out.append({"title": title, "url": url, "date": parse_date(hit.get("created_at_i")),
                    "desc": f"Hacker News discussion with {hit.get('points')} points and "
                            f"{hit.get('num_comments', 0)} comments.",
                    "discussion": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "hnId": hit["objectID"], "points": hit.get("points"), "comments": hit.get("num_comments", 0)})
    return out


FETCHERS = {"rss": fetch_rss, "html_links": fetch_html_links, "github_search": fetch_github_search, "hn": fetch_hn}


# ---------------------------------------------------------------- summarizer
PROMPT = """You summarize AI and technology news for a daily dashboard read by a technical professional.
Always respond in English, even when the source text below is in another language - translate first, then summarize.

Source: {source} ({stype})
Title: {title}
URL: {url}
Feed text: {desc}

Return ONLY a JSON object with these keys:
- "relevant": true if this is about AI, machine learning, developer tools, open source software, tech industry or tech policy; false for anything else (celebrity events, unrelated consumer news, sponsored posts).
- "title": a clean headline in English (fix casing, remove site names, dates or category labels stuck to it; translate it if the source isn't in English). Keep the original wording where possible.
- "summary": ONE short, plain sentence in English stating just the key finding or fact - what was actually released, found, or announced. No preamble, no scene-setting, no filler words. Use only facts in the title and feed text; do not invent numbers or claims.
- "why": one short sentence on why it matters to someone following AI, or "" if you can't say without guessing.
- "topic": one of "models" (LLMs, model releases, AI products), "agents" (AI agents, agentic coding tools, autonomous/multi-step systems), "dev" (developer tools, open source, libraries), "research" (papers, science, benchmarks), "industry" (business, funding, policy, safety incidents, regulation, hardware).
- "tags": 2-4 short lowercase tags.
- "importance": 1-5, where 5 is a major release or event most people in AI will hear about this week.
- "story_key": a short kebab-case slug identifying the underlying news event, e.g. "gpt-5-2-release" or "anthropic-claude-agent-sdk" - so the same event reported by different outlets gets the same slug. Keep it specific to the event, not the general topic."""


def fallback(raw: dict, src: dict) -> dict:
    """Used when there's no AI client, or every model failed: reuses the feed's own
    text verbatim (untranslated, no "why"), so it's marked as needing a retry later."""
    desc = raw.get("desc") or ""
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=2)
    return {"relevant": True, "title": raw["title"], "summary": " ".join(first[:2])[:320], "why": "",
            "topic": src["topics"][0], "tags": [], "importance": 2, "ai": False,
            "story_key": slugify(title_key(raw["title"])[:40])}


_model_state = {"idx": 0}      # sticks with the first working Gemini model; only advances when it's unusable
_groq_state = {"idx": 0}       # same, for the Groq backup


def _should_switch_model(exc: Exception) -> bool:
    """True when retrying the same model later won't help: the model name itself
    is invalid/retired. A 429/RESOURCE_EXHAUSTED is deliberately NOT switch-worthy -
    whether it's a per-minute cap (clears up within the run) or a per-day cap
    (doesn't, but Groq/fallback should take over for the rest of this run rather
    than the model being marked permanently dead) - treating either like a dead
    model used to permanently skip past gemini-3.8-flash onto two retired models
    that always 404, which burned through the model list and killed Gemini for
    the rest of the run.

    Checks the SDK's structured code/status fields rather than substring-matching
    the stringified error: a RESOURCE_EXHAUSTED error's retryDelay is a countdown
    in seconds (e.g. "51404s") that can contain "404" purely by coincidence as it
    ticks down, which used to false-positive this check."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if code is not None or status is not None:
        return code == 404 or status == "NOT_FOUND"
    return "NOT_FOUND" in str(exc) or "404" in str(exc)


def _call_model(client, model: str, msg: str) -> tuple[dict | None, bool]:
    """Try one Gemini model with a few retries. Returns (data, switch); switch=True
    means this model is unusable and the caller should move to the next one."""
    for attempt in range(3):
        try:
            resp = call_with_timeout(client.models.generate_content, GEMINI_CALL_TIMEOUT, model=model, contents=msg)
            text = resp.text or ""
            return json.loads(re.search(r"\{.*\}", text, re.S).group(0)), False
        except Exception as exc:  # noqa: BLE001 - keep the run going
            if _should_switch_model(exc):
                print(f"    {model} unusable: {exc}", file=sys.stderr)
                return None, True
            print(f"    {model} retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None, False


def _call_groq(api_key: str, model: str, msg: str) -> tuple[dict | None, bool]:
    """Try one Groq model via its OpenAI-compatible chat completions endpoint."""
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": msg}], "temperature": 0.3},
                timeout=30,
            )
            if r.status_code >= 400:
                # 4xx other than a transient rate limit means retrying the same request won't help
                if r.status_code in (400, 401, 403, 404, 429):
                    print(f"    {model} (Groq) unusable: {r.status_code} {r.text[:300]}", file=sys.stderr)
                    return None, True
                print(f"    {model} (Groq) retry {attempt + 1}: {r.status_code} {r.text[:300]}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(re.search(r"\{.*\}", text, re.S).group(0)), False
        except Exception as exc:  # noqa: BLE001 - keep the run going
            print(f"    {model} (Groq) retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None, False


def _finalize(data: dict, src: dict, provider: str) -> dict:
    data["topic"] = data.get("topic") if data.get("topic") in TOPICS else src["topics"][0]
    data["tags"] = [str(t).lower()[:24] for t in (data.get("tags") or [])][:4]
    data["importance"] = max(1, min(5, int(data.get("importance") or 2)))
    data["story_key"] = slugify(data.get("story_key") or "")
    data["ai"] = True
    data["provider"] = provider
    return data


def summarize(raw: dict, src: dict, client, groq_key: str | None) -> dict:
    if client is None and not groq_key:
        return fallback(raw, src)
    msg = PROMPT.format(source=src["name"], stype=src["type"], title=raw["title"], url=raw["url"], desc=raw.get("desc") or "(none)")

    if client is not None:
        while _model_state["idx"] < len(MODELS):
            model = MODELS[_model_state["idx"]]
            data, switch = _call_model(client, model, msg)
            if data is not None:
                return _finalize(data, src, "gemini")
            if not switch:
                break  # transient failure, retries used up - try the backup for this item only
            _model_state["idx"] += 1
            if _model_state["idx"] < len(MODELS):
                print(f"    switching to model {MODELS[_model_state['idx']]}", file=sys.stderr)

    if groq_key:
        while _groq_state["idx"] < len(GROQ_MODELS):
            model = GROQ_MODELS[_groq_state["idx"]]
            data, switch = _call_groq(groq_key, model, msg)
            if data is not None:
                return _finalize(data, src, "groq")
            if not switch:
                break
            _groq_state["idx"] += 1
            if _groq_state["idx"] < len(GROQ_MODELS):
                print(f"    switching to Groq model {GROQ_MODELS[_groq_state['idx']]}", file=sys.stderr)

    return fallback(raw, src)


# ---------------------------------------------------------------- engagement
def engagement_bonus(n: int | None) -> int:
    if n is None:
        return 0
    for threshold, bonus in ENGAGEMENT_THRESHOLDS:
        if n >= threshold:
            return bonus
    return 0


def refresh_engagement(items: list[dict]) -> None:
    """Re-checks current HN points / GitHub stars for recently-added items, so a story that
    was quiet when we first saw it but picks up steam over the next day or two still shows
    up as buzzy - otherwise these numbers are frozen at whatever they were the moment we
    first fetched the item. Only items within ENGAGEMENT_REFRESH_DAYS are checked, which
    keeps this to a handful of cheap follow-up requests per run (HN/github_search sources
    already cap how many items they contribute via their own per-source `limit`)."""
    cutoff = NOW - dt.timedelta(days=ENGAGEMENT_REFRESH_DAYS)
    for it in items:
        if (parse_date(it.get("date")) or NOW) < cutoff:
            continue
        try:
            if it.get("hnId"):
                r = get("https://hn.algolia.com/api/v1/search",
                        params={"tags": f"story_{it['hnId']}", "hitsPerPage": 1})
                hits = r.json().get("hits") or []
                if hits:
                    it["points"] = hits[0].get("points", it.get("points"))
                    it["comments"] = hits[0].get("num_comments", it.get("comments"))
                it["engagementBonus"] = engagement_bonus(it.get("points"))
            elif it.get("repoFullName"):
                r = get(f"https://api.github.com/repos/{it['repoFullName']}", headers=github_headers())
                it["stars"] = r.json().get("stargazers_count", it.get("stars"))
                it["engagementBonus"] = engagement_bonus(it.get("stars"))
        except Exception as exc:  # noqa: BLE001 - one bad lookup shouldn't abort the run
            print(f"    engagement refresh failed for {it.get('id')}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- clustering
def cluster_items(items: list[dict], embeddings: dict[str, list[float]] | None = None) -> None:
    """Groups items covering the same underlying story (e.g. a model launch reported by
    five different outlets, or the same project's incremental releases from one source) so
    the frontend's Top Stories section can show it once instead of several times. Two items
    cluster together when they're within CLUSTER_WINDOW_DAYS of each other and any of:
    - an exact story_key match,
    - Jaccard similarity on significant title words ≥ CLUSTER_JACCARD across different
      sources, or the much more lenient SAME_SOURCE_JACCARD when both are from the same
      source (a single-repo release feed only ever posts about one project), or
    - (when `embeddings` has a vector for both) cosine similarity ≥ EMBED_SIMILARITY_THRESHOLD -
      this is what catches two outlets covering the same event in very different words, which
      the title-overlap checks above miss on their own (see embed_text()'s docstring for a
      concrete example and the calibration behind the threshold).

    A cluster's *total* date span is also capped at CLUSTER_WINDOW_DAYS, not just each pairwise
    link - otherwise a continuously-active source (daily Ollama releases, say) can transitively
    chain A-B-C-D... into one cluster spanning weeks, even though no single pair is more than a
    few days apart. Within a cluster, the *most recent* item (not earliest) becomes primary when
    importance ties, so the cluster's representative is always its freshest development - an old
    cluster anchor would otherwise make an actively-updated story vanish from every recency-based
    view (Top Stories, category tabs, the Feed tab) once its original item ages out of range.

    Mutates each item in place with storyKey/clusterSize/clusterSources/isPrimary/buzzScore.

    Even with the embedding check, this is approximate, not perfect dedup - an item with no
    cached embedding (API hiccup, or just not re-embedded yet) still only has the text-based
    signals to go on. Good enough for "don't show the same headline five times", not a
    guaranteed general NLP solution.
    """
    embeddings = embeddings or {}
    by_date = sorted(items, key=lambda it: parse_date(it.get("date")) or NOW)
    keys = [it.get("storyKey") or slugify(title_key(it["title"])[:40]) for it in by_date]
    tokens = [title_tokens(it["title"]) for it in by_date]
    dates = [parse_date(it.get("date")) or NOW for it in by_date]
    window = dt.timedelta(days=CLUSTER_WINDOW_DAYS)

    n = len(by_date)
    parent = list(range(n))
    span = {i: (dates[i], dates[i]) for i in range(n)}  # root -> (earliest, latest) in that cluster

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            lo = min(span[ri][0], span[rj][0])
            hi = max(span[ri][1], span[rj][1])
            parent[ri] = rj
            span[rj] = (lo, hi)

    for i in range(n):
        for j in range(i + 1, n):
            if abs((dates[j] - dates[i]).total_seconds()) > window.total_seconds():
                continue
            same_key = keys[i] and keys[i] == keys[j]
            if not same_key and tokens[i] and tokens[j]:
                overlap = len(tokens[i] & tokens[j]) / len(tokens[i] | tokens[j])
                threshold = SAME_SOURCE_JACCARD if by_date[i]["source"] == by_date[j]["source"] else CLUSTER_JACCARD
                same_key = overlap >= threshold
            if not same_key:
                va, vb = embeddings.get(by_date[i]["id"]), embeddings.get(by_date[j]["id"])
                if va and vb:
                    same_key = cosine(va, vb) >= EMBED_SIMILARITY_THRESHOLD
            if not same_key:
                continue
            ri, rj = find(i), find(j)
            if ri != rj:
                lo = min(span[ri][0], span[rj][0])
                hi = max(span[ri][1], span[rj][1])
                if hi - lo > window:
                    continue  # merging would stretch this cluster's overall span too far
            union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    for members in groups.values():
        members.sort(key=lambda i: (-by_date[i].get("importance", 2), -dates[i].timestamp()))
        primary = members[0]
        size = len(members)
        primary_sources = {by_date[i]["sourceName"] for i in members if i != primary}
        for pos, i in enumerate(members):
            it = by_date[i]
            it["storyKey"] = keys[primary]
            it["clusterSize"] = size
            it["isPrimary"] = pos == 0
            it["clusterSources"] = sorted(primary_sources) if pos == 0 else []
            it["buzzScore"] = (it.get("importance", 2) + min(size - 1, CLUSTER_BUZZ_CAP)
                                + it.get("engagementBonus", 0))


# ---------------------------------------------------------------- main
def main() -> None:
    DATA.mkdir(exist_ok=True)
    sources = load_json(SOURCES_FILE, [])
    store = load_json(ITEMS_FILE, {"items": []})
    items = {it["id"]: it for it in store.get("items", [])}
    seen_titles = {title_key(it["title"]) for it in items.values()}
    old_status = {s["id"]: s for s in load_json(STATUS_FILE, {"sources": []}).get("sources", [])}
    embed_cache: dict[str, list[float]] = load_json(EMBED_CACHE_FILE, {})

    client = None
    if os.getenv("GEMINI_API_KEY"):
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    else:
        print("GEMINI_API_KEY not set - skipping Gemini.")
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        print("GROQ_API_KEY not set - no Groq backup if Gemini's quota runs out.")
    if client is None and not groq_key:
        print("No AI key set at all - using feed descriptions instead of AI summaries.")

    status, budget, added = [], MAX_NEW_PER_RUN, 0
    cutoff = NOW - dt.timedelta(days=MAX_AGE_DAYS)

    for src in sources:
        row = {k: v for k, v in src.items() if k in ("id", "name", "type", "topics", "url", "enabled")}
        prev = old_status.get(src["id"], {})
        if not src.get("enabled", True):
            status.append({**row, "status": "off", "lastChecked": prev.get("lastChecked"), "lastNew": 0})
            continue
        print(f"- {src['name']}")
        try:
            raw_items = FETCHERS[src.get("kind", "rss")](src)
        except Exception as exc:  # noqa: BLE001
            print(f"    failed: {exc}")
            status.append({**row, "status": "error", "error": str(exc)[:200], "lastChecked": iso(NOW),
                           "lastOk": prev.get("lastOk"), "lastNew": 0})
            continue

        new_here = 0
        fresh = [r for r in raw_items if not r["date"] or r["date"] >= cutoff]
        fresh.sort(key=lambda r: r["date"] or NOW, reverse=True)
        for raw in fresh[: src.get("limit", DEFAULT_LIMIT)]:
            iid = item_id(raw["url"])
            if iid in items or title_key(raw["title"]) in seen_titles or budget <= 0:
                continue
            budget -= 1
            s = summarize(raw, src, client, groq_key)
            if client is not None or groq_key:
                time.sleep(4)  # stay well under the free-tier requests-per-minute limit
            if not s.get("relevant", True):
                continue
            image = raw.get("image")
            if not image and src["type"] != "research":  # arXiv's og:image is the same stock logo on every paper
                image = fetch_page_image(raw["url"])
            items[iid] = {
                "id": iid, "title": s.get("title") or raw["title"], "url": raw["url"],
                "source": src["id"], "sourceName": src["name"], "type": src["type"],
                "date": iso(raw["date"] or NOW), "addedAt": iso(NOW),
                "summary": s.get("summary", ""), "why": s.get("why", ""),
                "topic": s["topic"], "tags": s.get("tags", []), "importance": s.get("importance", 2),
                "ai": s.get("ai", False), "provider": s.get("provider", ""), "storyKey": s.get("story_key", ""),
                "desc": (raw.get("desc") or "")[:1200],  # kept so a fallback item can be retried later
                **({"discussion": raw["discussion"]} if raw.get("discussion") else {}),
                **({"hnId": raw["hnId"], "points": raw.get("points"), "comments": raw.get("comments", 0)}
                   if raw.get("hnId") else {}),
                **({"repoFullName": raw["repoFullName"], "stars": raw.get("stars", 0)}
                   if raw.get("repoFullName") else {}),
                **({"image": image} if image else {}),
            }
            if client is not None:
                vec = embed_text(client, f"{items[iid]['title']}. {items[iid]['summary']}")
                if vec:
                    embed_cache[iid] = vec
                time.sleep(1)
            seen_titles.add(title_key(items[iid]["title"]))
            new_here += 1
        added += new_here
        print(f"    {len(raw_items)} found, {new_here} new")
        status.append({**row, "status": "ok", "lastChecked": iso(NOW), "lastOk": iso(NOW), "lastNew": new_here})

    # Retry existing fallback items with whatever budget is left, using their own saved feed
    # text - not just the ones still among each source's newest N (the loop above), which
    # would otherwise strand anything that ages out of a fast-moving feed on the fallback
    # text forever, since there'd be no later chance to see its raw description again.
    src_by_id = {s["id"]: s for s in sources}
    retried = 0
    if client is not None or groq_key:
        for iid, it in items.items():
            if budget <= 0:
                break
            if it.get("ai") or (parse_date(it.get("date")) or NOW) < cutoff:
                continue
            src = src_by_id.get(it.get("source"))
            if not src:
                continue
            raw = {"title": it["title"], "url": it["url"], "date": parse_date(it.get("date")), "desc": it.get("desc", "")}
            budget -= 1
            s = summarize(raw, src, client, groq_key)
            time.sleep(4)
            if not s.get("relevant", True):
                continue
            it.update({
                "title": s.get("title") or it["title"], "summary": s.get("summary", it["summary"]),
                "why": s.get("why", ""), "topic": s.get("topic", it["topic"]),
                "tags": s.get("tags", it.get("tags", [])), "importance": s.get("importance", it.get("importance", 2)),
                "ai": s.get("ai", False), "provider": s.get("provider", ""),
                "storyKey": s.get("story_key", it.get("storyKey", "")),
            })
            if client is not None:
                vec = embed_text(client, f"{it['title']}. {it['summary']}")
                if vec:
                    embed_cache[iid] = vec
                time.sleep(1)
            retried += 1
    if retried:
        print(f"- Backlog retry: upgraded {retried} previously-fallback stories")

    kept = [it for it in items.values() if (parse_date(it.get("date")) or NOW) >= cutoff]
    refresh_engagement(kept)

    # Backfill embeddings for recent items that predate this feature (or whose embed call
    # failed earlier) - only items still within the clustering window matter for comparison,
    # so this is naturally small after the first run or two, not a re-embed of the whole feed.
    if client is not None:
        cluster_cutoff_now = NOW - dt.timedelta(days=CLUSTER_WINDOW_DAYS)
        backfilled = 0
        for it in kept:
            if backfilled >= EMBED_BACKFILL_CAP:
                break
            if it["id"] in embed_cache or (parse_date(it.get("date")) or NOW) < cluster_cutoff_now:
                continue
            vec = embed_text(client, f"{it['title']}. {it.get('summary', '')}")
            if vec:
                embed_cache[it["id"]] = vec
            time.sleep(1)
            backfilled += 1
        if backfilled:
            print(f"- Backfilled embeddings for {backfilled} pre-existing recent stories")

    cluster_items(kept, embed_cache)
    kept.sort(key=lambda it: it.get("date") or "", reverse=True)
    ITEMS_FILE.write_text(json.dumps({"updated": iso(NOW), "items": kept}, indent=1, ensure_ascii=False), encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated": iso(NOW), "sources": status}, indent=1, ensure_ascii=False), encoding="utf-8")

    # Only cached vectors for items still within the clustering window are ever useful again -
    # keeps this file from growing forever as the embeddings API gets called run after run.
    cluster_cutoff = NOW - dt.timedelta(days=CLUSTER_WINDOW_DAYS)
    recent_ids = {it["id"] for it in kept if (parse_date(it.get("date")) or NOW) >= cluster_cutoff}
    embed_cache = {iid: vec for iid, vec in embed_cache.items() if iid in recent_ids}
    EMBED_CACHE_FILE.write_text(json.dumps(embed_cache), encoding="utf-8")
    fallback_count = sum(1 for it in kept if not it.get("ai"))
    gemini_count = sum(1 for it in kept if it.get("provider") == "gemini")
    groq_count = sum(1 for it in kept if it.get("provider") == "groq")
    print(f"Done: {added} new, {len(kept)} stories in feed - {gemini_count} via Gemini, {groq_count} via Groq, "
          f"{fallback_count} still on the raw-feed fallback.")


if __name__ == "__main__":
    main()
