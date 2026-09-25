#!/usr/bin/env python3
"""Parallel multi-source web search (stdlib only).

Fires many HTTP requests at once (ThreadPoolExecutor) so internet answers do
not wait on a single sequential agent tool loop. Results are short snippets
suitable for stuffing into a Grok chat prompt for spoken TV replies.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

USER_AGENT = (
    "LaunchHomeVoice/1.0 (+https://github.com/gprot42/webos-launch-home; rooted webOS voice)"
)
DEFAULT_WORKERS = 10
DEFAULT_TIMEOUT_S = 3.5
DEFAULT_MAX_RESULTS = 8

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    source: str = ""


@dataclass
class SearchBundle:
    hits: list[SearchHit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def citations(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for h in self.hits:
            u = (h.url or "").strip()
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out


def _clean(text: str, limit: int = 400) -> str:
    t = html.unescape(_TAG_RE.sub(" ", text or ""))
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def _get_json(url: str, timeout: float) -> Optional[object]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _get_text(url: str, timeout: float) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _query_variants(user_text: str) -> list[str]:
    """A few simultaneous query shapes — news-ish, plain, and stripped."""
    q = _WS_RE.sub(" ", (user_text or "").strip())
    if not q:
        return []
    variants = [q]
    # Drop filler so search engines hit better.
    stripped = re.sub(
        r"(?i)\b(please|can you|could you|tell me|what(?:'s| is)|"
        r"search for|look up|find (?:me )?|google)\b",
        " ",
        q,
    )
    stripped = _WS_RE.sub(" ", stripped).strip(" ?!.,")
    if stripped and stripped.lower() != q.lower():
        variants.append(stripped)
    # Time-sensitive boost for news-like questions.
    if re.search(
        r"(?i)\b(news|today|latest|current|breaking|score|won|price)\b", q
    ):
        base = stripped or q
        variants.append(base + " news")
        variants.append(base + " today")
    # Dedupe preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        key = v.lower()
        if key not in seen and len(v) >= 2:
            seen.add(key)
            out.append(v)
    return out[:4]


def _ddg_instant(query: str, timeout: float) -> list[SearchHit]:
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
    )
    data = _get_json(url, timeout)
    if not isinstance(data, dict):
        return []
    hits: list[SearchHit] = []
    abstract = _clean(str(data.get("AbstractText") or ""))
    abs_url = str(data.get("AbstractURL") or "").strip()
    abs_src = str(data.get("AbstractSource") or "DuckDuckGo")
    if abstract and abs_url:
        hits.append(
            SearchHit(
                title=abs_src or "Abstract",
                url=abs_url,
                snippet=abstract,
                source="ddg-instant",
            )
        )
    # RelatedTopics can be nested.
    stack = list(data.get("RelatedTopics") or [])
    while stack and len(hits) < 5:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        if "Topics" in item and isinstance(item["Topics"], list):
            stack.extend(item["Topics"])
            continue
        text = _clean(str(item.get("Text") or ""))
        first_url = ""
        first = item.get("FirstURL") or ""
        if isinstance(first, str):
            first_url = first.strip()
        if text and first_url:
            hits.append(
                SearchHit(
                    title=text[:80],
                    url=first_url,
                    snippet=text,
                    source="ddg-related",
                )
            )
    return hits


def _wikipedia_search(query: str, timeout: float) -> list[SearchHit]:
    # Opensearch: [query, [titles], [descs], [urls]]
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "opensearch",
            "search": query,
            "limit": "4",
            "namespace": "0",
            "format": "json",
        }
    )
    data = _get_json(url, timeout)
    if not (isinstance(data, list) and len(data) >= 4):
        return []
    titles, descs, urls = data[1], data[2], data[3]
    hits: list[SearchHit] = []
    for i, title in enumerate(titles or []):
        u = (urls[i] if i < len(urls) else "") or ""
        snip = (descs[i] if i < len(descs) else "") or ""
        if not u:
            continue
        hits.append(
            SearchHit(
                title=str(title),
                url=str(u),
                snippet=_clean(str(snip) or str(title)),
                source="wikipedia",
            )
        )
    return hits


def _wikipedia_summary(title: str, timeout: float) -> list[SearchHit]:
    """Fetch a page summary for a Wikipedia title (parallel follow-up)."""
    if not title:
        return []
    path = urllib.parse.quote(title.replace(" ", "_"), safe="")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{path}"
    data = _get_json(url, timeout)
    if not isinstance(data, dict):
        return []
    extract = _clean(str(data.get("extract") or ""), 500)
    page_url = ""
    urls = data.get("content_urls") or {}
    if isinstance(urls, dict):
        desktop = urls.get("desktop") or {}
        if isinstance(desktop, dict):
            page_url = str(desktop.get("page") or "")
    if not page_url:
        page_url = str(data.get("content_urls") or "") or (
            "https://en.wikipedia.org/wiki/" + path
        )
    if not extract:
        return []
    return [
        SearchHit(
            title=str(data.get("title") or title),
            url=page_url,
            snippet=extract,
            source="wikipedia-summary",
        )
    ]


def _google_news_rss(query: str, timeout: float) -> list[SearchHit]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    raw = _get_text(url, timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    hits: list[SearchHit] = []
    # RSS 2.0: channel/item
    for item in root.findall(".//item")[:6]:
        title = _clean((item.findtext("title") or ""), 160)
        link = (item.findtext("link") or "").strip()
        desc = _clean((item.findtext("description") or ""), 320)
        if title and link:
            hits.append(
                SearchHit(
                    title=title,
                    url=link,
                    snippet=desc or title,
                    source="google-news",
                )
            )
    return hits


def _ddg_html(query: str, timeout: float) -> list[SearchHit]:
    """Lightweight organic results from DuckDuckGo HTML endpoint."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(
        {"q": query}
    )
    raw = _get_text(url, timeout)
    hits: list[SearchHit] = []
    # result__a href + result__snippet
    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?(?:class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>)?',
        raw,
        re.I | re.S,
    ):
        href = html.unescape(m.group(1) or "").strip()
        title = _clean(m.group(2) or "", 120)
        snip = _clean(m.group(3) or "", 280)
        # DDG wraps redirects: //duckduckgo.com/l/?uddg=<urlencoded>
        if "uddg=" in href:
            try:
                parsed = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("uddg"):
                    href = urllib.parse.unquote(qs["uddg"][0])
            except Exception:
                pass
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith("http"):
            continue
        if title:
            hits.append(
                SearchHit(
                    title=title,
                    url=href,
                    snippet=snip or title,
                    source="ddg-html",
                )
            )
        if len(hits) >= 5:
            break
    return hits


def _hn_algolia(query: str, timeout: float) -> list[SearchHit]:
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "hitsPerPage": "4", "tags": "story"}
    )
    data = _get_json(url, timeout)
    if not isinstance(data, dict):
        return []
    hits: list[SearchHit] = []
    for h in data.get("hits") or []:
        if not isinstance(h, dict):
            continue
        title = _clean(str(h.get("title") or ""), 140)
        link = str(h.get("url") or "").strip()
        if not link:
            obj = h.get("objectID")
            if obj:
                link = f"https://news.ycombinator.com/item?id={obj}"
        snip = _clean(str(h.get("story_text") or h.get("comment_text") or title), 280)
        if title and link:
            hits.append(
                SearchHit(
                    title=title, url=link, snippet=snip, source="hn"
                )
            )
    return hits


def _fetch_page_snippet(url: str, timeout: float) -> list[SearchHit]:
    """Grab a plain-text excerpt from a result page (best-effort)."""
    try:
        raw = _get_text(url, timeout)
    except Exception:
        return []
    # Prefer meta description.
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        raw,
        re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
            raw,
            re.I,
        )
    snip = _clean(m.group(1) if m else "", 360)
    if not snip:
        # First paragraph-ish text block.
        body = _TAG_RE.sub(" ", raw)
        snip = _clean(body, 360)
    if len(snip) < 40:
        return []
    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = _clean(title_m.group(1) if title_m else url, 120)
    return [
        SearchHit(title=title, url=url, snippet=snip, source="page-fetch")
    ]


def _dedupe_hits(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    out: list[SearchHit] = []
    seen_url: set[str] = set()
    seen_snip: set[str] = set()
    for h in hits:
        u = (h.url or "").strip()
        key = u.split("?")[0].rstrip("/").lower()
        snip_key = (h.snippet or "")[:80].lower()
        if key and key in seen_url:
            continue
        if snip_key and snip_key in seen_snip:
            continue
        if key:
            seen_url.add(key)
        if snip_key:
            seen_snip.add(snip_key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def parallel_web_search(
    user_text: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    workers: int = DEFAULT_WORKERS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    fetch_pages: bool = True,
) -> SearchBundle:
    """Run many search providers/query variants at once; return merged hits."""
    t0 = time.time()
    bundle = SearchBundle()
    queries = _query_variants(user_text)
    bundle.queries = list(queries)
    if not queries:
        return bundle

    # Phase 1: simultaneous provider × query searches.
    jobs: list[tuple[str, Callable[[], list[SearchHit]]]] = []
    primary = queries[0]
    for q in queries:
        jobs.append((f"ddg-instant:{q}", lambda qq=q: _ddg_instant(qq, timeout_s)))
        jobs.append((f"wikipedia:{q}", lambda qq=q: _wikipedia_search(qq, timeout_s)))
        jobs.append((f"ddg-html:{q}", lambda qq=q: _ddg_html(qq, timeout_s)))
    # News + HN once on the primary (and a news variant if present).
    jobs.append((f"news:{primary}", lambda: _google_news_rss(primary, timeout_s)))
    jobs.append((f"hn:{primary}", lambda: _hn_algolia(primary, timeout_s)))
    for q in queries[1:]:
        if "news" in q.lower() or "today" in q.lower():
            jobs.append((f"news:{q}", lambda qq=q: _google_news_rss(qq, timeout_s)))

    raw_hits: list[SearchHit] = []
    n_workers = max(2, min(int(workers or DEFAULT_WORKERS), len(jobs) + 4))
    # Hard wall-clock for the whole search so "Searching…" cannot hang chat.
    wall = max(2.0, float(timeout_s) + 2.0)
    deadline = t0 + wall

    def _time_left() -> float:
        return max(0.05, deadline - time.time())

    pool = ThreadPoolExecutor(max_workers=n_workers)
    try:
        fut_map = {pool.submit(fn): name for name, fn in jobs}
        try:
            for fut in as_completed(fut_map, timeout=_time_left()):
                name = fut_map[fut]
                try:
                    part = fut.result(timeout=0.05)
                    if part:
                        raw_hits.extend(part)
                except Exception as exc:  # noqa: BLE001
                    bundle.errors.append("%s: %s" % (name, exc))
                if time.time() >= deadline:
                    bundle.errors.append("phase1-wall-clock")
                    break
        except TimeoutError:
            bundle.errors.append("phase1-timeout")
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 on some hosts
            pool.shutdown(wait=False)

    # Prefer Wikipedia titles for summary enrichment.
    wiki_titles = [
        h.title for h in raw_hits if h.source == "wikipedia" and h.title
    ][:3]

    # Phase 2: parallel page/summary enrichment — only if time remains.
    if fetch_pages and time.time() < deadline - 0.4:
        phase2: list[tuple[str, Callable[[], list[SearchHit]]]] = []
        for title in wiki_titles:
            phase2.append(
                (
                    f"wiki-sum:{title}",
                    lambda t=title: _wikipedia_summary(t, min(timeout_s, 2.5)),
                )
            )
        page_urls: list[str] = []
        for h in raw_hits:
            if h.source in ("ddg-html", "google-news", "hn", "ddg-related"):
                if h.url and h.url not in page_urls:
                    page_urls.append(h.url)
            if len(page_urls) >= 3:
                break
        for u in page_urls:
            phase2.append(
                (
                    f"page:{u[:48]}",
                    lambda uu=u: _fetch_page_snippet(uu, min(timeout_s, 2.0)),
                )
            )
        if phase2:
            pool2 = ThreadPoolExecutor(max_workers=min(n_workers, len(phase2)))
            try:
                fut_map = {pool2.submit(fn): name for name, fn in phase2}
                try:
                    for fut in as_completed(fut_map, timeout=_time_left()):
                        name = fut_map[fut]
                        try:
                            part = fut.result(timeout=0.05)
                            if part:
                                raw_hits.extend(part)
                        except Exception as exc:  # noqa: BLE001
                            bundle.errors.append("%s: %s" % (name, exc))
                        if time.time() >= deadline:
                            break
                except TimeoutError:
                    bundle.errors.append("phase2-timeout")
            finally:
                try:
                    pool2.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool2.shutdown(wait=False)

    # Rank: summaries and abstracts first, then news, then organic.
    rank = {
        "wikipedia-summary": 0,
        "ddg-instant": 1,
        "google-news": 2,
        "wikipedia": 3,
        "page-fetch": 4,
        "ddg-html": 5,
        "ddg-related": 6,
        "hn": 7,
    }
    raw_hits.sort(key=lambda h: (rank.get(h.source, 9), -len(h.snippet or "")))
    bundle.hits = _dedupe_hits(raw_hits, max(1, int(max_results or DEFAULT_MAX_RESULTS)))
    bundle.elapsed_ms = int((time.time() - t0) * 1000)
    return bundle


def format_hits_for_prompt(hits: list[SearchHit], *, max_chars: int = 3500) -> str:
    """Compact source block for the chat system/user context."""
    lines: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        block = "%d. %s\n   URL: %s\n   %s" % (
            i,
            h.title or "Source",
            h.url or "",
            h.snippet or "",
        )
        if used + len(block) > max_chars and lines:
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines)
