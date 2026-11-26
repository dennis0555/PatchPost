"""Epic Games: Gratisspiele im Epic Games Store und Fortnite.

- Gratisspiele: offizielle (undokumentierte) Schnittstelle, die auch die
  Store-Seite "Kostenlose Spiele" benutzt.
- Fortnite-Updates: die offizielle Statusseite status.epicgames.com kündigt jede
  Update-Wartung an ("Fortnite: Downtime for v43.11 Update").
- Fortnite-News: die In-Game-News, über das Community-Projekt fortnite-api.com
  (fortnite.com selbst blockt automatische Abrufe).

Rocket League braucht hier nichts Eigenes: Epic veröffentlicht die Patch Notes
weiterhin auf Steam (App 252950), siehe bot.py.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

import aiohttp

from .formatting import looks_like_patch
from .sources import NewsItem, SourceError, fetch_rss

# Land für Preise und Verfügbarkeit der Gratisspiele
EPIC_COUNTRY = os.getenv("EPIC_COUNTRY", "DE").upper()
EPIC_LOCALE = os.getenv("EPIC_LOCALE", "de")

FREE_GAMES_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STATUS_RSS_URL = "https://status.epicgames.com/history.rss"
FORTNITE_NEWS_URL = "https://fortnite-api.com/v2/news/br"

# Ziele, die der Bot unter der Quelle "epic" kennt: Schlüssel -> Anzeigename
EPIC_TARGETS: dict[str, str] = {
    "free": "Epic Games – Gratis-Spiele",
    "fortnite": "Fortnite – Updates & Wartungen",
    "fortnite-news": "Fortnite – News",
}


def _parse_time(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


async def _get_json(session: aiohttp.ClientSession, url: str, **params) -> dict:
    try:
        async with session.get(url, params=params or None) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise SourceError(f"Epic nicht erreichbar: {e}") from e


# --------------------------------------------------------------------------- #
# Gratisspiele
# --------------------------------------------------------------------------- #

def _store_url(el: dict) -> str:
    slug = el.get("productSlug")
    if not slug or slug == "[]":
        for mapping in (el.get("catalogNs") or {}).get("mappings") or el.get("offerMappings") or []:
            if mapping.get("pageSlug"):
                slug = mapping["pageSlug"]
                break
    if slug and slug != "[]":
        return f"https://store.epicgames.com/{EPIC_LOCALE}/p/{slug.split('/')[0]}"
    return f"https://store.epicgames.com/{EPIC_LOCALE}/free-games"


def _key_image(el: dict) -> str | None:
    images = {img.get("type"): img.get("url", "") for img in el.get("keyImages") or []}
    for kind in ("OfferImageWide", "DieselStoreFrontWide", "featuredMedia", "Thumbnail"):
        url = images.get(kind, "")
        if url.startswith("https://"):
            return url.replace(" ", "%20")
    return None


async def fetch_free_games(session: aiohttp.ClientSession) -> list[NewsItem]:
    """Spiele, die gerade jetzt kostenlos sind."""
    data = await _get_json(
        session, FREE_GAMES_URL, locale=EPIC_LOCALE, country=EPIC_COUNTRY, allowCountries=EPIC_COUNTRY
    )
    elements = (((data.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
    now = time.time()

    items: list[NewsItem] = []
    for el in elements:
        promotions = (el.get("promotions") or {}).get("promotionalOffers") or []
        for group in promotions:
            for offer in group.get("promotionalOffers") or []:
                # discountPercentage 0 heißt bei Epic: Endpreis 0 € (also 100 % Rabatt)
                if (offer.get("discountSetting") or {}).get("discountPercentage") != 0:
                    continue
                start, end = _parse_time(offer.get("startDate")), _parse_time(offer.get("endDate"))
                if not (start <= now < end):
                    continue

                title = el.get("title", "Unbekanntes Spiel").strip()
                desc = (el.get("description") or "").strip()
                price = (((el.get("price") or {}).get("totalPrice") or {}).get("fmtPrice") or {}).get(
                    "originalPrice"
                )
                lines = []
                if desc and desc != title:
                    lines.append(desc)
                price_text = f"~~{price}~~ → " if price and price not in ("0", "0,00 €", "0.00") else ""
                lines.append(f"{price_text}**Kostenlos** bis <t:{end}:f> (<t:{end}:R>)")

                items.append(
                    NewsItem(
                        id=f"{el.get('id')}:{offer.get('startDate')}",
                        title=f"🎁 Gratis: {title}",
                        url=_store_url(el),
                        body="\n\n".join(lines),
                        published=start,
                        image=_key_image(el),
                        is_patch=False,
                    )
                )
    return items


# --------------------------------------------------------------------------- #
# Fortnite
# --------------------------------------------------------------------------- #

async def fetch_fortnite_status(session: aiohttp.ClientSession) -> list[NewsItem]:
    """Updates, Wartungen und Störungen von Fortnite laut Epic-Statusseite."""
    _, items = await fetch_rss(session, STATUS_RSS_URL, count=50)
    result = []
    for item in items:
        if not item.title.lower().startswith("fortnite"):
            continue
        item.is_patch = "update" in item.title.lower() or looks_like_patch(item.title)
        result.append(item)
    return result


async def fetch_fortnite_news(session: aiohttp.ClientSession) -> list[NewsItem]:
    """In-Game-News von Fortnite Battle Royale."""
    data = await _get_json(session, FORTNITE_NEWS_URL, language=EPIC_LOCALE)
    news = data.get("data") or {}
    published = _parse_time(news.get("date"))
    items = []
    for motd in news.get("motds") or []:
        if not motd.get("id") or motd.get("hidden"):
            continue
        title = (motd.get("title") or "Fortnite News").strip()
        items.append(
            NewsItem(
                id=str(motd["id"]),
                title=title,
                url="https://www.fortnite.com/news",
                body=(motd.get("body") or "").strip(),
                published=published,
                image=motd["image"] if str(motd.get("image", "")).startswith("https://") else None,
                is_patch=looks_like_patch(title),
            )
        )
    return items


async def fetch_epic(session: aiohttp.ClientSession, target: str) -> list[NewsItem]:
    if target == "free":
        return await fetch_free_games(session)
    if target == "fortnite":
        return await fetch_fortnite_status(session)
    if target == "fortnite-news":
        return await fetch_fortnite_news(session)
    raise SourceError(f"Unbekanntes Epic-Ziel: {target}")
