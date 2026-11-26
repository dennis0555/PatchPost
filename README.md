# PatchPost

Ein freier, quelloffener Discord-Bot, der Patch Notes und Spiele-News automatisch in deine Kanäle postet. Du hostest ihn selbst. Es gibt keine Premium-Stufe, keine Limits außer denen, die du in der Config setzt, und keinen Code, den du nicht lesen kannst.

> PatchPost ist ein eigenständiges Projekt und hat nichts mit patchbot.io zu tun.

## Was er kann

- **Steam:** offizielle Ankündigungen jedes Steam-Spiels (Suche per Name mit Autovervollständigung, App-ID oder Store-Link). Ein API-Key ist nicht nötig.
- **Riot Games:** Patch Notes (oder alle News) von **League of Legends** und **VALORANT**.
- **Epic Games:**
  - **Gratis-Spiele** im Epic Store: Sobald ein Spiel verschenkt wird, kommt eine Meldung mit Store-Link und Enddatum.
  - **Fortnite:** Updates und Wartungen von der offiziellen Epic-Statusseite sowie die In-Game-News.
  - **Rocket League:** Patch Notes. Epic veröffentlicht sie weiterhin auf Steam.
- **RSS/Atom:** jeder beliebige Feed, zum Beispiel Entwickler-Blogs, Subreddits (`https://www.reddit.com/r/NAME/.rss`), YouTube-Kanäle (`https://www.youtube.com/feeds/videos.xml?channel_id=...`) oder GitHub-Releases (`https://github.com/OWNER/REPO/releases.atom`).
- Filter **„nur Patch Notes“**: Er erkennt Patches am Steam-Tag oder an Wörtern wie *Patch*, *Hotfix*, *Update* oder an Versionsnummern.
- Optional wird bei jeder neuen Meldung eine **Rolle gepingt**.
- In **Ankündigungskanälen** werden Posts automatisch veröffentlicht, damit Server, die dem Kanal folgen, sie auch bekommen.
- Beim Abonnieren wird **nicht das ganze Archiv gepostet**, nur Meldungen, die danach erscheinen.

## Befehle

Alle Befehle brauchen die Berechtigung **Server verwalten**.

| Befehl | Was er tut |
|---|---|
| `/patch steam spiel:[kanal] [nur_patches] [rolle]` | Steam-Spiel abonnieren |
| `/patch riot spiel:[kanal] [nur_patches] [rolle]` | League of Legends oder VALORANT abonnieren (Standard: nur Patch Notes) |
| `/patch epic was:[kanal] [nur_patches] [rolle]` | Epic-Gratisspiele, Fortnite-Updates, Fortnite-News oder Rocket League abonnieren |
| `/patch rss url:[name] [kanal] [nur_patches] [rolle]` | RSS/Atom-Feed abonnieren |
| `/patch liste` | Alle Abos des Servers anzeigen |
| `/patch entfernen abo:` | Ein Abo löschen |
| `/patch test abo:` | Neueste Meldung sofort in den Kanal posten (zum Ausprobieren) |

## Einrichten

### 1. Bot bei Discord anlegen

1. Öffne <https://discord.com/developers/applications> und klicke auf **New Application**.
2. Klicke links auf **Bot**, dann auf **Reset Token** und kopiere den Token. Er kommt gleich in die `.env`.
3. Klicke links auf **OAuth2**. Dort steht die **Client ID**.
4. Lade den Bot mit diesem Link auf deinen Server ein (ersetze `CLIENT_ID`):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=19456
   ```

   (`19456` = Kanal ansehen + Nachrichten senden + Links einbetten)

### 2. Starten

Du brauchst Python 3.10 oder neuer.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # dann DISCORD_TOKEN in .env eintragen
python -m patchpost
```

Unter Linux/macOS: `source .venv/bin/activate` und `cp` statt `copy`.

**Tipp:** Trag beim Testen deine Server-ID als `DEV_GUILD_ID` in die `.env` ein. Dann erscheinen die Slash-Befehle sofort. Die Server-ID bekommst du mit Rechtsklick auf den Server → *Server-ID kopieren*; dafür muss in Discord der Entwicklermodus an sein.

### Mit Docker

```bash
docker build -t patchpost .
docker run -d --name patchpost --env-file .env -v patchpost-data:/data --restart unless-stopped patchpost
```

## Einstellungen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `DISCORD_TOKEN` | – | Bot-Token (Pflicht) |
| `POLL_MINUTES` | `10` | Prüfintervall in Minuten (mindestens 2) |
| `DEV_GUILD_ID` | leer | Befehle nur auf diesem Server registrieren (sofort verfügbar) |
| `AUTO_PUBLISH` | `true` | Posts in Ankündigungskanälen veröffentlichen |
| `DB_PATH` | `patchpost.db` | SQLite-Datenbank |
| `LOG_LEVEL` | `INFO` | `DEBUG` zeigt jeden Abruf |
| `RIOT_LOCALE` | `de-de` | Sprache der Riot-News (`en-us`, `en-gb`, `fr-fr` …) |
| `EPIC_COUNTRY` | `DE` | Land für Preise und Verfügbarkeit der Gratisspiele |
| `EPIC_LOCALE` | `de` | Sprache für Epic-Store-Links und Fortnite-News |

## Aufbau des Codes

```
patchpost/
├── __main__.py    Startpunkt, liest die .env
├── bot.py         Slash-Befehle, Abruf-Schleife, Posten der Embeds
├── sources.py     Steam-API und RSS abrufen → einheitliche NewsItems
├── riot.py        League of Legends / VALORANT (liest die News-Seiten von Riot)
├── epic.py        Epic-Gratisspiele, Fortnite-Status und Fortnite-News
├── formatting.py  Steam-BBCode / HTML → Discord-Markdown, Patch-Erkennung
└── db.py          SQLite: Abos und schon gesehene Meldungen
tests/
└── test_formatting.py
```

**So läuft ein Abruf:** Alle `POLL_MINUTES` sammelt `bot.py` die Abos und fragt jede Quelle **einmal** ab, auch wenn mehrere Server sie abonniert haben. Meldungen, deren ID noch nicht in der Tabelle `seen` steht, gelten als neu. Sie werden an alle Abos dieser Quelle gepostet, pro Abruf höchstens 5.

**Neue Quelle hinzufügen:** In `sources.py` eine `fetch_xyz()`-Funktion schreiben, die eine Liste von `NewsItem` zurückgibt. Danach in `sources.fetch()` eintragen und in `bot.py` einen Befehl dafür anlegen.

## Tests

```powershell
pip install pytest
python -m pytest
```

## Hinweise

- Eine Rolle kann nur gepingt werden, wenn sie in den Rollen-Einstellungen *„Jeder kann diese Rolle erwähnen“* aktiviert hat oder der Bot die Berechtigung *Alle erwähnen* besitzt.
- **Andere Spiele aus dem Epic Store:** Epic bietet keine öffentliche News-Schnittstelle für einzelne Store-Spiele. Die meisten dieser Spiele gibt es aber auch auf Steam, dann reicht `/patch steam`. Sonst hilft `/patch rss` mit dem Blog oder Subreddit des Spiels.
- **Riot** hat keine offizielle News-API. Der Bot liest deshalb die News-Seiten aus. Wenn Riot die Seiten umbaut, erscheint im Log ein Hinweis und `riot.py` muss angepasst werden.
- **Fortnite:** Die Patch Notes auf fortnite.com blocken automatische Abrufe. Deshalb meldet der Bot Updates über die offizielle Statusseite. Die In-Game-News kommen über das Community-Projekt fortnite-api.com.
- Steam liefert über die API nur die letzten Ankündigungen. Wenn der Bot sehr lange offline war, können ältere Meldungen fehlen.

## Lizenz

MIT. Mach damit, was du willst.
