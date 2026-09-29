"""Signal Desk collector.

Reads sources.json, pulls the newest items from each enabled source,
skips anything already seen, asks Gemini (with Grok as a backup once
Gemini's daily quota runs out) to summarize and tag the new items,
and writes:

  data/items.json    - the feed shown on the Feed tab
  data/sources.json  - each source plus its last check result (Sources tab)

Run locally:  GEMINI_API_KEY=... python scripts/collect.py
Add XAI_API_KEY=... too to use Grok as a fallback once Gemini's quota runs out.
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
                               or "gemini-3.8-flash,gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
XAI_MODELS = [m.strip() for m in (os.getenv("XAI_MODELS") or os.getenv("XAI_MODEL")
                                   or "grok-4-fast").split(",") if m.strip()]
TOPICS = ["models", "dev", "research", "industry"]
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


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


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
        })
    return out


def fetch_html_links(src: dict) -> list[dict]:
    """For sites without a feed: collect links whose path matches a pattern."""
    page = get(src["feed"]).text
    pattern = re.compile(src["link_pattern"])
    seen, out = set(), []
    for href, inner in re.findall(r'<a[^>]+href="([^"#?]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I):
        if not pattern.search(href) or href in seen:
            continue
        text = strip_html(inner, 400)
        if len(text) < 12:
            continue
        seen.add(href)
        url = href if href.startswith("http") else src["base"].rstrip("/") + href
        # Link text often runs together category + title + date; the summarizer cleans the title.
        date_match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
        date = dt.datetime.strptime(date_match.group(1), "%b %d, %Y").replace(tzinfo=dt.timezone.utc) if date_match else None
        out.append({"title": text[:200], "url": url, "date": date, "desc": text, "needs_title": True})
    return out


def fetch_github_search(src: dict) -> list[dict]:
    since = (NOW - dt.timedelta(days=src.get("since_days", 7))).date().isoformat()
    headers = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = get("https://api.github.com/search/repositories", headers=headers,
            params={"q": src["query"].format(since=since), "sort": "stars", "order": "desc", "per_page": 20})
    out = []
    for repo in r.json().get("items", []):
        out.append({
            "title": repo["full_name"],
            "url": repo["html_url"],
            "date": parse_date(repo.get("created_at")),
            "desc": f"{repo.get('description') or ''} (Language: {repo.get('language') or 'n/a'}; "
                    f"{repo.get('stargazers_count', 0)} stars)",
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
                    "discussion": f"https://news.ycombinator.com/item?id={hit['objectID']}"})
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
- "summary": 1-2 plain sentences in English on what happened. Use only facts in the title and feed text; do not invent numbers or claims.
- "why": one short sentence on why it matters to someone following AI, or "" if you can't say without guessing.
- "topic": one of "models" (LLMs, model releases, AI products), "dev" (developer tools, open source, libraries), "research" (papers, science, benchmarks), "industry" (business, funding, policy, safety incidents, regulation, hardware).
- "tags": 2-4 short lowercase tags.
- "importance": 1-5, where 5 is a major release or event most people in AI will hear about this week."""


def fallback(raw: dict, src: dict) -> dict:
    """Used when there's no AI client, or every model failed: reuses the feed's own
    text verbatim (untranslated, no "why"), so it's marked as needing a retry later."""
    desc = raw.get("desc") or ""
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=2)
    return {"relevant": True, "title": raw["title"], "summary": " ".join(first[:2])[:320], "why": "",
            "topic": src["topics"][0], "tags": [], "importance": 2, "ai": False}


_model_state = {"idx": 0}      # sticks with the first working Gemini model; only advances when it's unusable
_xai_state = {"idx": 0}        # same, for the Grok backup


def _should_switch_model(text: str) -> bool:
    """True when retrying the same model won't help: quota exhausted, or the model
    name itself is invalid/retired (so plain retries would just repeat the same error)."""
    return ("RESOURCE_EXHAUSTED" in text or "429" in text or "quota" in text.lower()
            or "NOT_FOUND" in text or "404" in text)


def _call_model(client, model: str, msg: str) -> tuple[dict | None, bool]:
    """Try one Gemini model with a few retries. Returns (data, switch); switch=True
    means this model is unusable and the caller should move to the next one."""
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=model, contents=msg)
            text = resp.text or ""
            return json.loads(re.search(r"\{.*\}", text, re.S).group(0)), False
        except Exception as exc:  # noqa: BLE001 - keep the run going
            if _should_switch_model(str(exc)):
                print(f"    {model} unusable: {exc}", file=sys.stderr)
                return None, True
            print(f"    {model} retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None, False


def _call_xai(api_key: str, model: str, msg: str) -> tuple[dict | None, bool]:
    """Try one Grok model via xAI's OpenAI-compatible chat completions endpoint."""
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": msg}], "temperature": 0.3},
                timeout=30,
            )
            if r.status_code >= 400:
                # 4xx other than a transient rate limit means retrying the same request won't help
                if r.status_code in (400, 401, 403, 404, 429):
                    print(f"    {model} (xAI) unusable: {r.status_code} {r.text[:300]}", file=sys.stderr)
                    return None, True
                print(f"    {model} (xAI) retry {attempt + 1}: {r.status_code} {r.text[:300]}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(re.search(r"\{.*\}", text, re.S).group(0)), False
        except Exception as exc:  # noqa: BLE001 - keep the run going
            print(f"    {model} (xAI) retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None, False


def _finalize(data: dict, src: dict, provider: str) -> dict:
    data["topic"] = data.get("topic") if data.get("topic") in TOPICS else src["topics"][0]
    data["tags"] = [str(t).lower()[:24] for t in (data.get("tags") or [])][:4]
    data["importance"] = max(1, min(5, int(data.get("importance") or 2)))
    data["ai"] = True
    data["provider"] = provider
    return data


def summarize(raw: dict, src: dict, client, xai_key: str | None) -> dict:
    if client is None and not xai_key:
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

    if xai_key:
        while _xai_state["idx"] < len(XAI_MODELS):
            model = XAI_MODELS[_xai_state["idx"]]
            data, switch = _call_xai(xai_key, model, msg)
            if data is not None:
                return _finalize(data, src, "xai")
            if not switch:
                break
            _xai_state["idx"] += 1
            if _xai_state["idx"] < len(XAI_MODELS):
                print(f"    switching to Grok model {XAI_MODELS[_xai_state['idx']]}", file=sys.stderr)

    return fallback(raw, src)


# ---------------------------------------------------------------- main
def main() -> None:
    DATA.mkdir(exist_ok=True)
    sources = load_json(SOURCES_FILE, [])
    store = load_json(ITEMS_FILE, {"items": []})
    items = {it["id"]: it for it in store.get("items", [])}
    seen_titles = {title_key(it["title"]) for it in items.values()}
    old_status = {s["id"]: s for s in load_json(STATUS_FILE, {"sources": []}).get("sources", [])}

    client = None
    if os.getenv("GEMINI_API_KEY"):
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    else:
        print("GEMINI_API_KEY not set - skipping Gemini.")
    xai_key = os.getenv("XAI_API_KEY")
    if not xai_key:
        print("XAI_API_KEY not set - no Grok backup if Gemini's quota runs out.")
    if client is None and not xai_key:
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
            existing = items.get(iid)
            # Retry items that only ever got the raw-feed fallback (no "ai" flag, or ai:false)
            # once a working AI client is available, instead of leaving them stuck forever.
            if existing and (client is None or existing.get("ai", False)):
                continue
            if not existing and (title_key(raw["title"]) in seen_titles or budget <= 0):
                continue
            if existing and budget <= 0:
                continue
            budget -= 1
            s = summarize(raw, src, client, xai_key)
            if client is not None or xai_key:
                time.sleep(4)  # stay well under the free-tier requests-per-minute limit
            if not s.get("relevant", True):
                continue
            items[iid] = {
                "id": iid, "title": s.get("title") or raw["title"], "url": raw["url"],
                "source": src["id"], "sourceName": src["name"], "type": src["type"],
                "date": iso(raw["date"] or NOW), "addedAt": (existing or {}).get("addedAt", iso(NOW)),
                "summary": s.get("summary", ""), "why": s.get("why", ""),
                "topic": s["topic"], "tags": s.get("tags", []), "importance": s.get("importance", 2),
                "ai": s.get("ai", False),
                **({"discussion": raw["discussion"]} if raw.get("discussion") else {}),
            }
            seen_titles.add(title_key(items[iid]["title"]))
            if not existing:
                new_here += 1
        added += new_here
        print(f"    {len(raw_items)} found, {new_here} new")
        status.append({**row, "status": "ok", "lastChecked": iso(NOW), "lastOk": iso(NOW), "lastNew": new_here})

    kept = [it for it in items.values() if (parse_date(it.get("date")) or NOW) >= cutoff]
    kept.sort(key=lambda it: it.get("date") or "", reverse=True)
    ITEMS_FILE.write_text(json.dumps({"updated": iso(NOW), "items": kept}, indent=1, ensure_ascii=False), encoding="utf-8")
    STATUS_FILE.write_text(json.dumps({"updated": iso(NOW), "sources": status}, indent=1, ensure_ascii=False), encoding="utf-8")
    fallback_count = sum(1 for it in kept if not it.get("ai"))
    gemini_count = sum(1 for it in kept if it.get("provider") == "gemini")
    xai_count = sum(1 for it in kept if it.get("provider") == "xai")
    print(f"Done: {added} new, {len(kept)} stories in feed - {gemini_count} via Gemini, {xai_count} via Grok, "
          f"{fallback_count} still on the raw-feed fallback.")


if __name__ == "__main__":
    main()
