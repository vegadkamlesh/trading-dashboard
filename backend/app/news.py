"""Market news / pulse — free RSS aggregation with on-demand refresh.

No API key required. We fetch a small set of Indian market RSS feeds, parse the
headlines, and cache the result. The UI calls `/api/intel/news?refresh=1` when
the user clicks "Refresh", so we don't poll constantly.

Feeds are configurable via `APP_NEWS_FEEDS` (comma separated). If a feed fails
we simply skip it — one dead feed never breaks the panel.
"""
from __future__ import annotations

import datetime as _dt
import email.utils as _email_utils
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import httpx

from . import sentiment as _sentiment

logger = logging.getLogger("dhan.news")


def _parse_published(published: Optional[str]) -> Optional[float]:
    """Parse an RSS/Atom date string into a UTC epoch (seconds).

    Handles RFC-822 (pubDate) and ISO-8601 (updated). Returns None if it can't
    be parsed, so callers can push undated items to the bottom.
    """
    if not published:
        return None
    raw = published.strip()
    # RFC-822, e.g. "Mon, 20 Sep 2026 14:05:00 +0530"
    try:
        dt = _email_utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    # ISO-8601, e.g. "2026-09-20T14:05:00Z"
    try:
        iso = raw.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

DEFAULT_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol Markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Livemint Markets", "https://www.livemint.com/rss/markets"),
]

_TAG_RE = re.compile(r"<[^>]+>")
CACHE_TTL = 600  # seconds (10 min) — used only when refresh is NOT forced


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        # Attach an explainable sentiment tag so the trader can scan at a glance.
        sent = _sentiment.classify(self.title).to_dict()
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "published": self.published,
            # Parsed epoch (seconds) used for newest-first sorting; None if undated.
            "ts": _parse_published(self.published),
            "sentiment": sent,
        }


@dataclass
class NewsCache:
    items: List[NewsItem] = field(default_factory=list)
    fetched_at: float = 0.0
    error: Optional[str] = None

    def is_fresh(self) -> bool:
        return time.time() - self.fetched_at < CACHE_TTL

    def to_dict(self) -> Dict[str, Any]:
        items = [i.to_dict() for i in self.items]
        # Overall market read derived from all headlines.
        summary = _sentiment.summarize(items)
        # NEWEST FIRST (by published time). Items we can't date go to the bottom,
        # then impact/confidence act only as tie-breakers within the same time.
        items.sort(
            key=lambda i: (
                i.get("ts") is not None,          # dated before undated
                i.get("ts") or 0.0,               # newest timestamp first
                _impact_rank(i["sentiment"]["impact"]),
                i["sentiment"]["confidence"],
            ),
            reverse=True,
        )
        return {
            "items": items,
            "count": len(items),
            "summary": summary,
            "ageSeconds": round(time.time() - self.fetched_at, 1) if self.fetched_at else None,
            "error": self.error,
        }


def _impact_rank(impact: str) -> int:
    return {"high": 2, "medium": 1, "low": 0}.get(impact, 0)


_cache = NewsCache()


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def _parse_rss(xml_text: str, source: str) -> List[NewsItem]:
    items: List[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0 style <item>, plus Atom <entry> fallback.
    nodes = root.iter("item")
    found = list(nodes)
    if not found:
        found = list(root.iter("entry"))

    for node in found[:15]:
        def _get(tag: str) -> str:
            el = node.find(tag)
            return _clean(el.text) if el is not None and el.text else ""

        title = _get("title")
        link = _get("link")
        if not link:
            # Atom uses <link href="...">
            link_el = node.find("link")
            if link_el is not None:
                link = link_el.attrib.get("href", "")
        if title and link:
            items.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source,
                    published=_get("pubDate") or _get("updated") or None,
                )
            )
    return items


async def fetch_news(force: bool = False, feeds: Optional[List[tuple]] = None) -> Dict[str, Any]:
    """Return cached news, refreshing if stale or `force` is True."""
    global _cache
    if not force and _cache.items and _cache.is_fresh():
        return _cache.to_dict()

    feed_list = feeds or DEFAULT_FEEDS
    collected: List[NewsItem] = []
    errors: List[str] = []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for name, url in feed_list:
            try:
                resp = await client.get(url, headers={"User-Agent": "dhan-dashboard/1.0"})
                if resp.status_code == 200:
                    collected.extend(_parse_rss(resp.text, name))
            except httpx.HTTPError as exc:
                errors.append(f"{name}: {exc}")
                logger.info("news feed failed (%s): %s", name, exc)

    # De-dupe by title, keep newest first (feeds are usually newest first already).
    seen = set()
    unique: List[NewsItem] = []
    for it in collected:
        key = it.title.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    _cache = NewsCache(
        items=unique[:60],
        fetched_at=time.time(),
        error="; ".join(errors) if errors and not unique else None,
    )
    return _cache.to_dict()
