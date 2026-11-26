"""Text-Umwandlung: Steam-BBCode und RSS-HTML -> Discord-Markdown.

Bewusst ohne Discord-Abhängigkeit, damit es sich leicht testen lässt.
"""

from __future__ import annotations

import html
import re

STEAM_CLAN_IMAGE = "https://clan.akamai.steamstatic.com/images"

_FLAGS = re.IGNORECASE | re.DOTALL

# Begriffe, an denen wir erkennen, dass eine Meldung ein Patch/Update ist.
PATCH_RE = re.compile(
    r"\b(patch(?:notes?)?|hotfix(?:es)?|update|changelog|release notes|bug ?fix(?:es)?"
    r"|aktualisierung|v?\d+\.\d+(?:\.\d+)*[a-z]?)\b",
    re.IGNORECASE,
)


def looks_like_patch(title: str) -> bool:
    """Grobe Heuristik: Sieht der Titel nach Patch Notes aus?"""
    return bool(PATCH_RE.search(title or ""))


def _is_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and " " not in url


def _cleanup(text: str) -> str:
    """Leerzeichen am Zeilenende entfernen und Leerzeilen zusammenfassen."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"•[ \t]+", "• ", text)          # "•   Text" -> "• Text"
    text = re.sub(r"\n\s*\n(?=• )", "\n", text)    # keine Leerzeilen zwischen Listenpunkten
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Steam BBCode
# --------------------------------------------------------------------------- #

def steam_bbcode_to_markdown(text: str) -> tuple[str, list[str]]:
    """Wandelt Steam-BBCode in Discord-Markdown um.

    Gibt (markdown, bilder) zurück. Bilder werden aus dem Text entfernt und
    separat geliefert, damit das erste davon als Embed-Bild dienen kann.
    """
    if not text:
        return "", []

    text = text.replace("{STEAM_CLAN_IMAGE}", STEAM_CLAN_IMAGE)
    images: list[str] = []

    # Bilder: [img]URL[/img], [img src="URL"][/img], [img=URL][/img]
    def _img(m: re.Match) -> str:
        attrs, inner = m.group(1), m.group(2)
        src = re.search(r'src="?([^"\s\]]+)', attrs)
        if src:
            url = src.group(1)
        elif attrs.startswith("="):
            url = attrs[1:].strip().strip('"')
        else:
            url = inner.strip()
        if _is_http_url(url):
            images.append(url)
        return ""

    text = re.sub(r"\[img([^\]]*)\](.*?)\[/img\]", _img, text, flags=_FLAGS)

    # YouTube-Vorschauen -> normaler Link
    text = re.sub(
        r'\[previewyoutube=?"?([\w-]+)[^\]]*\].*?\[/previewyoutube\]',
        r"\nhttps://youtu.be/\1\n",
        text,
        flags=_FLAGS,
    )

    # Dynamische Links (Steam-Store-Widgets usw.)
    text = re.sub(
        r'\[dynamiclink[^\]]*href="([^"]+)"[^\]]*\].*?\[/dynamiclink\]',
        r"\n\1\n",
        text,
        flags=_FLAGS,
    )

    # Überschriften -> fett
    text = re.sub(r"\[h[1-6][^\]]*\](.*?)\[/h[1-6]\]", r"\n**\1**\n", text, flags=_FLAGS)

    # Einfache Formatierung
    for tag, md in (
        ("b", "**"),
        ("i", "*"),
        ("u", "__"),
        ("strike", "~~"),
        ("s", "~~"),
        ("spoiler", "||"),
    ):
        text = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", rf"{md}\1{md}", text, flags=_FLAGS)

    text = re.sub(r"\[code\](.*?)\[/code\]", r"\n```\n\1\n```\n", text, flags=_FLAGS)

    def _quote(m: re.Match) -> str:
        body = m.group(1).strip()
        return "\n" + "\n".join(f"> {line}" for line in body.split("\n")) + "\n"

    text = re.sub(r"\[quote[^\]]*\](.*?)\[/quote\]", _quote, text, flags=_FLAGS)

    # Listen
    text = re.sub(r"\[\*\]", "\n• ", text)
    text = re.sub(r"\[/\*\]", "", text)
    text = re.sub(r"\[/?(?:list|olist)[^\]]*\]", "\n", text, flags=re.IGNORECASE)

    # Absätze, Trennlinien, Tabellen-Zeilen
    text = re.sub(r"\[/p\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[hr\]\s*(\[/hr\])?", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/tr\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/t[dh]\]", "  ", text, flags=re.IGNORECASE)

    # Links: [url=X]Text[/url], [url="X"]Text[/url], [url href="X"]Text[/url], [url]X[/url]
    def _url(m: re.Match) -> str:
        url, label = m.group(1).strip(), m.group(2).strip()
        if not _is_http_url(url):
            return label
        if not label or label == url:
            return url
        return f"[{label}]({url})"

    text = re.sub(
        r'\[url(?:=|\s+href=)"?([^"\]\s]+)"?[^\]]*\](.*?)\[/url\]', _url, text, flags=_FLAGS
    )
    text = re.sub(r"\[url\](.*?)\[/url\]", r"\1", text, flags=_FLAGS)

    # Übrige bekannte Tags einfach entfernen (nur eine feste Liste, damit
    # Markdown-Links wie [Patch Notes](...) nicht kaputtgehen).
    text = re.sub(
        r"\[/?(?:p|table|tr|td|th|noparse|center|left|right|size|color|font|emoticon"
        r"|carousel|video|expand|br|img|url|h[1-6])(?:[=\s][^\]]*)?\]",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return _cleanup(html.unescape(text)), images


# --------------------------------------------------------------------------- #
# RSS / HTML
# --------------------------------------------------------------------------- #

def html_to_markdown(text: str) -> tuple[str, list[str]]:
    """Sehr einfache HTML -> Markdown-Umwandlung für RSS-Inhalte.

    Gibt (markdown, bilder) zurück.
    """
    if not text:
        return "", []

    images = [
        html.unescape(u)
        for u in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
    ]
    images = [u for u in images if _is_http_url(u)]

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=_FLAGS)
    text = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n**\1**\n", text, flags=_FLAGS)
    text = re.sub(r"<(b|strong)(?:\s[^>]*)?>(.*?)</\1>", r"**\2**", text, flags=_FLAGS)
    text = re.sub(r"<(i|em)(?:\s[^>]*)?>(.*?)</\1>", r"*\2*", text, flags=_FLAGS)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=_FLAGS)

    def _link(m: re.Match) -> str:
        url = html.unescape(m.group(1))
        label = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not _is_http_url(url):
            return label
        if not label or label == url:
            return url
        return f"[{label}]({url})"

    text = re.sub(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', _link, text, flags=_FLAGS)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|ul|ol|blockquote|tr|table)\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    return _cleanup(html.unescape(text)), images


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #

def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Kürzt Text auf `limit` Zeichen, möglichst an einer Zeilen-/Wortgrenze.

    Gibt (text, wurde_gekürzt) zurück.
    """
    if len(text) <= limit:
        return text, False
    cut = text[: limit - 1]
    for sep in ("\n", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            cut = cut[:pos]
            break
    # Keine offenen Code-Blöcke stehen lassen
    if cut.count("```") % 2 == 1:
        cut += "\n```"
    return cut.rstrip() + "…", True
