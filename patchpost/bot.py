"""Der eigentliche Discord-Bot: Slash-Befehle und der regelmäßige Abruf."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import sources
from .db import BASELINE_MARKER, Database, Subscription
from .epic import EPIC_TARGETS
from .formatting import truncate
from .riot import RIOT_GAMES
from .sources import NewsItem, SourceError

log = logging.getLogger("patchpost")

MAX_POSTS_PER_FEED = 5       # höchstens so viele neue Meldungen pro Abruf und Quelle
MAX_SUBS_PER_GUILD = 50      # Schutz gegen versehentliches Zuspammen
DESCRIPTION_LIMIT = 1500     # Zeichen im Embed, danach "Weiterlesen"
COLORS = {"steam": 0x1B2838, "rss": 0xF26522, "riot": 0xD13639, "epic": 0x2F2F2F}
SOURCE_LABEL = {"steam": "Steam", "rss": "RSS", "riot": "Riot Games", "epic": "Epic Games"}

# Rocket League gehört zu Epic, die Patch Notes erscheinen aber auf Steam
ROCKET_LEAGUE_STEAM_APPID = "252950"

# Quellen, bei denen beim Abonnieren die *aktuellen* Meldungen trotzdem
# gepostet werden sollen (ein laufendes Gratisspiel will man ja noch sehen).
POST_CURRENT_ON_SUBSCRIBE = {("epic", "free")}


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #

def build_embed(sub_name: str, source: str, item: NewsItem) -> discord.Embed:
    description, cut = truncate(item.body, DESCRIPTION_LIMIT)
    if cut and item.url:
        description += f"\n\n[**Weiterlesen →**]({item.url})"

    title, _ = truncate(item.title or "Neue Meldung", 256)
    embed = discord.Embed(
        title=title,
        url=item.url or None,
        description=description or None,
        color=COLORS.get(source, 0x5865F2),
        timestamp=datetime.fromtimestamp(item.published, tz=timezone.utc) if item.published else None,
    )
    embed.set_author(name=truncate(sub_name, 256)[0])
    if item.image:
        embed.set_image(url=item.image)
    footer = SOURCE_LABEL.get(source, source)
    if item.is_patch:
        footer += " · Patch Notes"
    embed.set_footer(text=footer)
    return embed


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

class PatchPost(commands.Bot):
    def __init__(self, db: Database, poll_minutes: int, dev_guild_id: int | None, auto_publish: bool):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.db = db
        self.poll_minutes = poll_minutes
        self.dev_guild_id = dev_guild_id
        self.auto_publish = auto_publish
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": sources.USER_AGENT},
        )
        self.tree.add_command(PatchCommands(self))
        self.tree.on_error = self.on_app_command_error

        if self.dev_guild_id:
            # Zum Entwickeln: Befehle sofort auf einem Server verfügbar machen
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash-Befehle auf Server %s synchronisiert", self.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Slash-Befehle global synchronisiert")

        self.poll_feeds.change_interval(minutes=self.poll_minutes)
        self.poll_feeds.start()

    async def close(self) -> None:
        self.poll_feeds.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Eingeloggt als %s – auf %d Server(n)", self.user, len(self.guilds))

    # ---- Aufräumen, wenn Server/Kanäle verschwinden --------------------- #

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.db.remove_guild(guild.id)
        log.info("Vom Server %s entfernt – Abos gelöscht", guild.id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.db.remove_channel(channel.id)

    # ---- Fehler bei Slash-Befehlen -------------------------------------- #

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Dafür brauchst du die Berechtigung **Server verwalten**."
        else:
            log.exception("Fehler im Befehl", exc_info=error)
            msg = "Da ist etwas schiefgelaufen. Details stehen im Log des Bots."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass  # Interaktion abgelaufen oder schon beantwortet – nichts mehr zu tun

    # ---- Regelmäßiger Abruf -------------------------------------------- #

    @tasks.loop(minutes=10)
    async def poll_feeds(self) -> None:
        try:
            await self.check_all_feeds()
        except Exception:
            # Nie die Schleife sterben lassen
            log.exception("Unerwarteter Fehler beim Abruf")

    @poll_feeds.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def check_all_feeds(self) -> None:
        feeds: dict[tuple[str, str], list[Subscription]] = defaultdict(list)
        for sub in self.db.all_subscriptions():
            feeds[(sub.source, sub.target)].append(sub)

        log.debug("Prüfe %d Quelle(n)", len(feeds))
        for (source, target), subs in feeds.items():
            try:
                items = await sources.fetch(self.session, source, target)
            except SourceError as e:
                log.warning("%s %s: %s", source, target, e)
                continue
            except Exception:
                log.exception("Fehler beim Abruf von %s %s", source, target)
                continue

            await self.handle_items(source, target, items, subs)
            await asyncio.sleep(1)  # Quellen nicht im Sekundentakt anfragen

    async def handle_items(
        self, source: str, target: str, items: list[NewsItem], subs: list[Subscription]
    ) -> None:
        seen = self.db.seen_ids(source, target)
        if BASELINE_MARKER not in seen:
            # Quelle zum ersten Mal: nur merken, nichts posten
            self.db.set_baseline(source, target, [i.id for i in items])
            return

        new = [i for i in items if i.id not in seen]
        if not new:
            return

        new.sort(key=lambda i: i.published)
        self.db.mark_seen(source, target, [i.id for i in new])

        for item in new[-MAX_POSTS_PER_FEED:]:
            log.info("Neu bei %s: %s", subs[0].name, item.title)
            for sub in subs:
                if sub.patch_only and not item.is_patch:
                    continue
                await self.post(sub, item)

    async def post(self, sub: Subscription, item: NewsItem) -> bool:
        channel = self.get_channel(sub.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(sub.channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.warning("Kanal %s für Abo #%s nicht erreichbar", sub.channel_id, sub.id)
                return False

        content = f"<@&{sub.role_id}>" if sub.role_id else None
        allowed = discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(sub.role_id)] if sub.role_id else False
        )
        try:
            msg = await channel.send(
                content=content, embed=build_embed(sub.name, sub.source, item), allowed_mentions=allowed
            )
        except discord.HTTPException as e:
            log.warning("Konnte nicht in Kanal %s posten (Abo #%s): %s", sub.channel_id, sub.id, e)
            return False

        # In Ankündigungskanälen automatisch veröffentlichen, damit
        # Server, die dem Kanal folgen, die Meldung auch bekommen.
        if self.auto_publish and isinstance(channel, discord.TextChannel) and channel.is_news():
            try:
                await msg.publish()
            except discord.HTTPException as e:
                log.info("Veröffentlichen fehlgeschlagen (Abo #%s): %s", sub.id, e)
        return True


# --------------------------------------------------------------------------- #
# Slash-Befehle: /patch ...
# --------------------------------------------------------------------------- #

class PatchCommands(app_commands.Group):
    def __init__(self, bot: PatchPost):
        super().__init__(
            name="patch",
            description="Patch Notes und Spiele-News abonnieren",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.bot = bot

    # ---- Hilfen --------------------------------------------------------- #

    REQUIRED_PERMS = {
        "view_channel": "Kanal ansehen",
        "send_messages": "Nachrichten senden",
        "embed_links": "Links einbetten",
    }

    def _check_channel(self, interaction: discord.Interaction, channel) -> str | None:
        """Gibt eine Fehlermeldung zurück, falls der Bot dort nicht posten kann."""
        if not isinstance(channel, discord.TextChannel):
            return "Bitte wähle einen Text- oder Ankündigungskanal."

        me = interaction.guild.me
        if channel.id == interaction.channel_id:
            # Für den aktuellen Kanal schickt Discord die echten Rechte des Bots mit
            perms = interaction.app_permissions
        else:
            perms = channel.permissions_for(me)

        missing = [attr for attr in self.REQUIRED_PERMS if not getattr(perms, attr)]
        if not missing:
            return None

        labels = ", ".join(self.REQUIRED_PERMS[a] for a in missing)
        lines = [f"Mir fehlen in {channel.mention} diese Rechte: **{labels}**.", ""]
        lines += self._explain_missing(channel, me, missing)
        bot_role = interaction.guild.self_role
        role_text = f"die Rolle **{bot_role.name}**" if bot_role else f"**{me.display_name}**"
        lines += [
            "",
            "**So behebst du es:** Kanal bearbeiten → *Berechtigungen* → *Rollen/Mitglieder hinzufügen* → "
            f"{role_text} auswählen → bei {labels} das **grüne Häkchen ✅** setzen.",
        ]
        return "\n".join(lines)

    def _explain_missing(self, channel: discord.TextChannel, me: discord.Member, missing: list[str]) -> list[str]:
        """Findet heraus, welche Einstellung die Rechte blockiert."""
        reasons = []
        if me.is_timed_out():
            reasons.append("• Ich bin auf diesem Server gerade **getimeoutet**.")

        for attr in missing:
            label = self.REQUIRED_PERMS[attr]
            if not getattr(me.guild_permissions, attr):
                reasons.append(f"• Meinen Rollen fehlt **{label}** schon serverweit (Servereinstellungen → Rollen).")
                continue
            # Kanal-Überschreibungen, die mich betreffen und das Recht verbieten
            for target, overwrite in channel.overwrites.items():
                applies = target == channel.guild.default_role or target == me or (
                    isinstance(target, discord.Role) and target in me.roles
                )
                if applies and getattr(overwrite, attr) is False:
                    who = "@everyone" if target == channel.guild.default_role else target.name
                    reasons.append(f"• In den Kanal-Berechtigungen ist **{label}** für **{who}** verboten ❌.")

        if channel.category and channel.permissions_synced:
            reasons.append(f"• Der Kanal übernimmt seine Rechte von der Kategorie **{channel.category.name}**.")
        if channel.is_news():
            reasons.append("• Das ist ein Ankündigungskanal – dort darf @everyone standardmäßig nicht schreiben.")
        if not reasons:
            reasons.append("• Die genaue Ursache konnte ich nicht erkennen – prüf die Kanal- und Kategorie-Berechtigungen.")
        return reasons

    async def _subscribe(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        source: str,
        target: str,
        name: str,
        items: list[NewsItem],
        patch_only: bool,
        role: discord.Role | None,
    ) -> None:
        db = self.bot.db
        if len(db.guild_subscriptions(interaction.guild_id)) >= MAX_SUBS_PER_GUILD:
            await interaction.followup.send(
                f"Dieser Server hat schon {MAX_SUBS_PER_GUILD} Abos. Entferne erst eines mit `/patch entfernen`.",
                ephemeral=True,
            )
            return

        sub_id = db.add_subscription(
            interaction.guild_id, channel.id, source, target, name, patch_only, role.id if role else None
        )
        if sub_id is None:
            await interaction.followup.send(
                f"**{name}** ist in {channel.mention} bereits abonniert.", ephemeral=True
            )
            return

        # Aktuelle Meldungen als gesehen markieren, damit nicht das ganze Archiv kommt
        if BASELINE_MARKER not in db.seen_ids(source, target):
            if (source, target) in POST_CURRENT_ON_SUBSCRIBE:
                db.set_baseline(source, target, [])  # beim nächsten Abruf posten
            else:
                db.set_baseline(source, target, [i.id for i in items])

        extras = []
        if patch_only:
            extras.append("nur Patch Notes")
        if role:
            extras.append(f"pingt {role.mention}")
        extra = f" ({', '.join(extras)})" if extras else ""

        text = (
            f"✅ **{name}** abonniert → {channel.mention}{extra}\n"
            f"Neue Meldungen werden alle {self.bot.poll_minutes} Minuten geprüft. Abo-ID: `{sub_id}`"
        )
        latest = max(items, key=lambda i: i.published) if items else None
        if latest:
            await interaction.followup.send(
                text + "\nSo sieht die neueste Meldung aus:",
                embed=build_embed(name, source, latest),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(text, ephemeral=True)

    async def _follow(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None,
        source: str,
        target: str,
        name: str,
        patch_only: bool,
        role: discord.Role | None,
    ) -> None:
        """Kanal prüfen, Quelle einmal abrufen, Abo anlegen."""
        channel = kanal or interaction.channel
        if err := self._check_channel(interaction, channel):
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            items = await sources.fetch(self.bot.session, source, target)
        except SourceError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await self._subscribe(interaction, channel, source, target, name, items, patch_only, role)

    # ---- /patch riot ---------------------------------------------------- #

    @app_commands.command(name="riot", description="Patch Notes von League of Legends oder VALORANT abonnieren")
    @app_commands.describe(
        spiel="Welches Riot-Spiel",
        kanal="Wohin gepostet wird (Standard: dieser Kanal)",
        nur_patches="Nur Patch Notes statt aller News (Standard: ja)",
        rolle="Diese Rolle bei neuen Meldungen pingen",
    )
    @app_commands.choices(
        spiel=[app_commands.Choice(name=name, value=key) for key, (name, _) in RIOT_GAMES.items()]
    )
    async def riot(
        self,
        interaction: discord.Interaction,
        spiel: app_commands.Choice[str],
        kanal: discord.TextChannel | None = None,
        nur_patches: bool = True,
        rolle: discord.Role | None = None,
    ) -> None:
        await self._follow(interaction, kanal, "riot", spiel.value, spiel.name, nur_patches, rolle)

    # ---- /patch epic ---------------------------------------------------- #

    @app_commands.command(name="epic", description="Epic-Gratisspiele, Fortnite oder Rocket League abonnieren")
    @app_commands.describe(
        was="Was abonniert werden soll",
        kanal="Wohin gepostet wird (Standard: dieser Kanal)",
        nur_patches="Nur Updates/Patch Notes (gilt für Fortnite-Updates und Rocket League)",
        rolle="Diese Rolle bei neuen Meldungen pingen",
    )
    @app_commands.choices(
        was=[
            app_commands.Choice(name="🎁 Gratis-Spiele im Epic Store", value="free"),
            app_commands.Choice(name="Fortnite – Updates & Wartungen", value="fortnite"),
            app_commands.Choice(name="Fortnite – In-Game-News", value="fortnite-news"),
            app_commands.Choice(name="Rocket League – Patch Notes", value="rocketleague"),
        ]
    )
    async def epic(
        self,
        interaction: discord.Interaction,
        was: app_commands.Choice[str],
        kanal: discord.TextChannel | None = None,
        nur_patches: bool = False,
        rolle: discord.Role | None = None,
    ) -> None:
        if was.value == "rocketleague":
            await self._follow(
                interaction, kanal, "steam", ROCKET_LEAGUE_STEAM_APPID, "Rocket League", nur_patches, rolle
            )
            return
        patch_only = nur_patches and was.value == "fortnite"
        await self._follow(interaction, kanal, "epic", was.value, EPIC_TARGETS[was.value], patch_only, rolle)

    # ---- /patch steam --------------------------------------------------- #

    @app_commands.command(name="steam", description="Offizielle Steam-News eines Spiels abonnieren")
    @app_commands.describe(
        spiel="Name, App-ID oder Store-Link des Spiels",
        kanal="Wohin gepostet wird (Standard: dieser Kanal)",
        nur_patches="Nur Meldungen, die nach Patch Notes aussehen",
        rolle="Diese Rolle bei neuen Meldungen pingen",
    )
    async def steam(
        self,
        interaction: discord.Interaction,
        spiel: str,
        kanal: discord.TextChannel | None = None,
        nur_patches: bool = False,
        rolle: discord.Role | None = None,
    ) -> None:
        channel = kanal or interaction.channel
        if err := self._check_channel(interaction, channel):
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        app = await sources.resolve_steam_app(self.bot.session, spiel)
        if not app:
            await interaction.followup.send(f"Kein Steam-Spiel zu „{spiel}“ gefunden.", ephemeral=True)
            return
        appid, name = app

        try:
            items = await sources.fetch_steam(self.bot.session, appid)
        except SourceError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        await self._subscribe(interaction, channel, "steam", appid, name, items, nur_patches, rolle)

    @steam.autocomplete("spiel")
    async def _steam_autocomplete(self, interaction: discord.Interaction, current: str):
        if len(current.strip()) < 2 or current.strip().isdigit():
            return []
        results = await sources.steam_search(self.bot.session, current, limit=10)
        return [
            app_commands.Choice(name=f"{name} ({appid})"[:100], value=appid) for appid, name in results
        ]

    # ---- /patch rss ----------------------------------------------------- #

    @app_commands.command(name="rss", description="Einen beliebigen RSS/Atom-Feed abonnieren")
    @app_commands.describe(
        url="Adresse des Feeds (z. B. https://www.reddit.com/r/Spiel/.rss)",
        name="Anzeigename (Standard: Titel des Feeds)",
        kanal="Wohin gepostet wird (Standard: dieser Kanal)",
        nur_patches="Nur Einträge, die nach Patch Notes aussehen",
        rolle="Diese Rolle bei neuen Einträgen pingen",
    )
    async def rss(
        self,
        interaction: discord.Interaction,
        url: str,
        name: str | None = None,
        kanal: discord.TextChannel | None = None,
        nur_patches: bool = False,
        rolle: discord.Role | None = None,
    ) -> None:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message(
                "Die URL muss mit `http://` oder `https://` beginnen.", ephemeral=True
            )
            return
        channel = kanal or interaction.channel
        if err := self._check_channel(interaction, channel):
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            feed_title, items = await sources.fetch_rss(self.bot.session, url)
        except SourceError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        display = (name or feed_title).strip()[:100]
        await self._subscribe(interaction, channel, "rss", url, display, items, nur_patches, rolle)

    # ---- /patch liste --------------------------------------------------- #

    @app_commands.command(name="liste", description="Alle Abos auf diesem Server anzeigen")
    async def liste(self, interaction: discord.Interaction) -> None:
        subs = self.bot.db.guild_subscriptions(interaction.guild_id)
        if not subs:
            await interaction.response.send_message(
                "Noch keine Abos. Leg eins an mit `/patch steam` oder `/patch rss`.", ephemeral=True
            )
            return

        lines = []
        for s in subs:
            flags = []
            if s.patch_only:
                flags.append("nur Patches")
            if s.role_id:
                flags.append(f"<@&{s.role_id}>")
            flag_text = f" · {', '.join(flags)}" if flags else ""
            lines.append(f"`{s.id}` **{s.name}** ({SOURCE_LABEL[s.source]}) → <#{s.channel_id}>{flag_text}")

        embed = discord.Embed(
            title=f"Abos auf {interaction.guild.name}",
            description=truncate("\n".join(lines), 4000)[0],
            color=0x5865F2,
        )
        embed.set_footer(text=f"{len(subs)}/{MAX_SUBS_PER_GUILD} · Entfernen mit /patch entfernen")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- /patch entfernen ----------------------------------------------- #

    @app_commands.command(name="entfernen", description="Ein Abo entfernen")
    @app_commands.describe(abo="Das Abo, das entfernt werden soll")
    async def entfernen(self, interaction: discord.Interaction, abo: int) -> None:
        sub = self.bot.db.get_subscription(interaction.guild_id, abo)
        if not sub or not self.bot.db.remove_subscription(interaction.guild_id, abo):
            await interaction.response.send_message(f"Kein Abo mit ID `{abo}` gefunden.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ **{sub.name}** in <#{sub.channel_id}> wurde entfernt.", ephemeral=True
        )

    # ---- /patch test ---------------------------------------------------- #

    @app_commands.command(name="test", description="Die neueste Meldung eines Abos jetzt in dessen Kanal posten")
    @app_commands.describe(abo="Das Abo, das getestet werden soll")
    async def test(self, interaction: discord.Interaction, abo: int) -> None:
        sub = self.bot.db.get_subscription(interaction.guild_id, abo)
        if not sub:
            await interaction.response.send_message(f"Kein Abo mit ID `{abo}` gefunden.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            items = await sources.fetch(self.bot.session, sub.source, sub.target)
        except SourceError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if not items:
            await interaction.followup.send("Die Quelle hat aktuell keine Meldungen.", ephemeral=True)
            return

        ok = await self.bot.post(sub, max(items, key=lambda i: i.published))
        await interaction.followup.send(
            f"✅ Test-Meldung in <#{sub.channel_id}> gepostet." if ok
            else f"❌ Konnte nicht in <#{sub.channel_id}> posten – fehlen mir dort Rechte?",
            ephemeral=True,
        )

    @entfernen.autocomplete("abo")
    @test.autocomplete("abo")
    async def _abo_autocomplete(self, interaction: discord.Interaction, current: str):
        subs = self.bot.db.guild_subscriptions(interaction.guild_id)
        current = current.lower()
        choices = []
        for s in subs:
            channel = interaction.guild.get_channel(s.channel_id)
            label = f"{s.name} ({SOURCE_LABEL[s.source]}) → #{channel.name if channel else '?'}"
            if current in label.lower() or current == str(s.id):
                choices.append(app_commands.Choice(name=label[:100], value=s.id))
        return choices[:25]
