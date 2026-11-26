"""Riot Games: News und Patch Notes von League of Legends und VALORANT.

Riot bietet dafür keine offizielle API und keinen RSS-Feed. Die News-Seiten
enthalten aber alle Artikel als JSON im Block `__NEXT_DATA__`, den wir auslesen.
Falls Riot die Seite umbaut, muss nur `_extract_articles()` angepasst werden.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime

import aiohttp

from .formatting import html_to_markdown, looks_like_patch
from .sources import NewsItem, SourceError

# Sprache der Riot-Seiten, z. B. de-de, en-us, en-gb, fr-fr
RIOT_LOCALE = os.getenv("RIOT_LOCALE", "de-de")

# Schlüssel -> (Anzeigename, Website)
RIOT_GAMES: dict[str, tuple[str, str]] = {
    "lol": ("League of Legends", "https://www.leagueoflegends.com"),
    "valorant": ("VALORANT", "https://playvalorant.com"),
}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


def _parse_time(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _extract_articles(data: dict) -> list[dict]:
    """Sucht in den Seitendaten alle Einträge, die wie Artikel aussehen."""
    blades = data.get("props", {}).get("pageProps", {}).get("page", {}).get("blades", [])
    articles = []
    for blade in blades:
        for item in blade.get("items") or []:
            if isinstance(item, dict) and "title" in item and "publishedAt" in item:
                articles.append(item)
    return articles


def _image(article: dict) -> str | None:
    for key in ("imageMedia", "media"):
        media = article.get(key) or {}
        url = media.get("url", "")
        if media.get("type", "image") == "image" and url.startswith("https://"):
            return url
    return None


async def fetch_riot(session: aiohttp.ClientSession, game: str, count: int = 20) -> list[NewsItem]:
    if game not in RIOT_GAMES:
        raise SourceError(f"Unbekanntes Riot-Spiel: {game}")
    _, base = RIOT_GAMES[game]
    url = f"{base}/{RIOT_LOCALE}/news/"

    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            page = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise SourceError(f"Riot-Seite nicht erreichbar: {e}") from e

    m = _NEXT_DATA_RE.search(page)
    if not m:
        raise SourceError("Riot hat die News-Seite umgebaut – riot.py muss angepasst werden.")
    articles = _extract_articles(json.loads(m.group(1)))
    if not articles:
        raise SourceError("Keine Artikel auf der Riot-Seite gefunden – riot.py muss angepasst werden.")

    items: list[NewsItem] = []
    for a in articles:
        link = ((a.get("action") or {}).get("payload") or {}).get("url", "")
        if link.startswith("/"):
            link = base + link
        item_id = (a.get("analytics") or {}).get("contentId") or link or a["title"]
        tags = {t.get("machineName") for t in a.get("tags") or []}
        body, _ = html_to_markdown((a.get("description") or {}).get("body", ""))

        items.append(
            NewsItem(
                id=str(item_id),
                title=a["title"].strip(),
                url=link if link.startswith("http") else "",
                body=body,
                published=_parse_time(a.get("publishedAt")),
                image=_image(a),
                is_patch="patch_notes" in tags or looks_like_patch(a["title"]),
            )
        )

    items.sort(key=lambda i: i.published, reverse=True)
    return items[:count]
