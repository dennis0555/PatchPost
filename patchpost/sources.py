"""Nachrichtenquellen: Steam-News-API und beliebige RSS/Atom-Feeds.

Riot Games und Epic Games stehen in riot.py und epic.py.
Alle Quellen liefern eine Liste von `NewsItem`s im selben Format.
"""

from __future__ import annotations

import asyncio
import calendar
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
import feedparser

from .formatting import html_to_markdown, looks_like_patch, steam_bbcode_to_markdown

USER_AGENT = "PatchPost/1.0 (self-hosted Discord bot)"

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_HEADER_IMG = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


@dataclass
class NewsItem:
    id: str              # eindeutige ID innerhalb der Quelle (für "schon gesehen")
    title: str
    url: str
    body: str            # bereits in Discord-Markdown umgewandelt
    published: int       # Unix-Zeitstempel (0 = unbekannt)
    image: str | None
    is_patch: bool


class SourceError(Exception):
    """Quelle nicht erreichbar oder Antwort unbrauchbar."""


# --------------------------------------------------------------------------- #
# Steam
# --------------------------------------------------------------------------- #

async def fetch_steam(session: aiohttp.ClientSession, appid: str, count: int = 10) -> list[NewsItem]:
    """Holt die offiziellen Ankündigungen eines Steam-Spiels (kein API-Key nötig)."""
    params = {
        "appid": appid,
        "count": count,
        "maxlength": 0,  # 0 = vollständiger Text
        "format": "json",
        "feeds": "steam_community_announcements",  # nur offizielle Entwickler-Posts
    }
    try:
        async with session.get(STEAM_NEWS_URL, params=params) as resp:
            if resp.status == 403:
                raise SourceError(f"Steam-App {appid} existiert nicht oder ist nicht öffentlich.")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise SourceError(f"Steam nicht erreichbar: {e}") from e

    items: list[NewsItem] = []
    for n in data.get("appnews", {}).get("newsitems", []):
        body, images = steam_bbcode_to_markdown(n.get("contents", ""))
        title = n.get("title", "").strip()
        tags = n.get("tags") or []
        items.append(
            NewsItem(
                id=str(n["gid"]),
                title=title,
                url=f"https://store.steampowered.com/news/app/{appid}/view/{n['gid']}",
                body=body,
                published=int(n.get("date") or 0),
                image=images[0] if images else STEAM_HEADER_IMG.format(appid=appid),
                is_patch="patchnotes" in tags or looks_like_patch(title),
            )
        )
    return items


async def steam_search(session: aiohttp.ClientSession, term: str, limit: int = 10) -> list[tuple[str, str]]:
    """Sucht Spiele im Steam-Store. Gibt [(appid, name), ...] zurück."""
    params = {"term": term, "l": "german", "cc": "DE"}
    try:
        async with session.get(STEAM_SEARCH_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []
    return [(str(i["id"]), i["name"]) for i in data.get("items", [])[:limit]]


async def steam_app_name(session: aiohttp.ClientSession, appid: str) -> str | None:
    """Liest den Namen eines Spiels anhand seiner App-ID."""
    params = {"appids": appid, "filters": "basic", "l": "german"}
    try:
        async with session.get(STEAM_DETAILS_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    entry = (data or {}).get(appid) or {}
    if entry.get("success"):
        return entry.get("data", {}).get("name")
    return None


async def resolve_steam_app(session: aiohttp.ClientSession, query: str) -> tuple[str, str] | None:
    """Macht aus Eingabe (App-ID, Store-Link oder Name) ein (appid, name)-Paar."""
    query = query.strip()

    m = re.search(r"store\.steampowered\.com/app/(\d+)", query) or re.fullmatch(r"(\d+)", query)
    if m:
        appid = m.group(1)
        name = await steam_app_name(session, appid)
        return appid, name or f"Steam-App {appid}"

    results = await steam_search(session, query, limit=1)
    return results[0] if results else None


# --------------------------------------------------------------------------- #
# RSS / Atom
# --------------------------------------------------------------------------- #

def _entry_image(entry, fallback: list[str]) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url", "")
            if url.startswith(("http://", "https://")):
                return url
    for enc in entry.get("enclosures") or []:
        if enc.get("type", "").startswith("image/") and enc.get("href", "").startswith("http"):
            return enc["href"]
    return fallback[0] if fallback else None


async def fetch_rss(session: aiohttp.ClientSession, url: str, count: int = 15) -> tuple[str, list[NewsItem]]:
    """Liest einen RSS/Atom-Feed. Gibt (Feed-Titel, Einträge) zurück."""
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise SourceError(f"Feed nicht erreichbar: {e}") from e

    # feedparser arbeitet synchron -> in einem Thread ausführen
    parsed = await asyncio.to_thread(feedparser.parse, raw)
    if parsed.bozo and not parsed.entries:
        raise SourceError("Das sieht nicht nach einem gültigen RSS/Atom-Feed aus.")

    feed_title = parsed.feed.get("title") or urlparse(url).netloc

    items: list[NewsItem] = []
    for e in parsed.entries[:count]:
        if e.get("content"):
            raw_body = e.content[0].get("value", "")
        else:
            raw_body = e.get("summary", "")
        body, images = html_to_markdown(raw_body)

        ts = e.get("published_parsed") or e.get("updated_parsed")
        title = (e.get("title") or "").strip()
        link = e.get("link") or ""
        item_id = e.get("id") or link or title
        if not item_id:
            continue

        items.append(
            NewsItem(
                id=str(item_id),
                title=title,
                url=link if link.startswith(("http://", "https://")) else "",
                body=body,
                published=calendar.timegm(ts) if ts else 0,
                image=_entry_image(e, images),
                is_patch=looks_like_patch(title),
            )
        )
    return feed_title, items


# --------------------------------------------------------------------------- #
# Gemeinsamer Einstieg
# --------------------------------------------------------------------------- #

async def fetch(session: aiohttp.ClientSession, source: str, target: str) -> list[NewsItem]:
    if source == "steam":
        return await fetch_steam(session, target)
    if source == "rss":
        _, items = await fetch_rss(session, target)
        return items
    if source == "riot":
        from .riot import fetch_riot  # hier importiert, um Zirkelimporte zu vermeiden
        return await fetch_riot(session, target)
    if source == "epic":
        from .epic import fetch_epic
        return await fetch_epic(session, target)
    raise ValueError(f"Unbekannte Quelle: {source}")
