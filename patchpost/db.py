"""SQLite-Speicher für Abos und bereits gepostete Meldungen."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    source      TEXT    NOT NULL,              -- 'steam' oder 'rss'
    target      TEXT    NOT NULL,              -- Steam-App-ID oder Feed-URL
    name        TEXT    NOT NULL,              -- Anzeigename (Spiel / Feed)
    patch_only  INTEGER NOT NULL DEFAULT 0,    -- 1 = nur Patch Notes posten
    role_id     INTEGER,                       -- optional: Rolle, die gepingt wird
    created_at  INTEGER NOT NULL,
    UNIQUE (channel_id, source, target)
);

-- Welche Meldungen einer Quelle schon verarbeitet wurden.
-- Pro Quelle (nicht pro Abo), damit jeder Feed nur einmal abgefragt wird.
CREATE TABLE IF NOT EXISTS seen (
    source   TEXT    NOT NULL,
    target   TEXT    NOT NULL,
    item_id  TEXT    NOT NULL,
    seen_at  INTEGER NOT NULL,
    PRIMARY KEY (source, target, item_id)
);
"""

# Markiert, dass eine Quelle schon einmal "eingelesen" wurde. So posten wir
# beim ersten Abruf nicht das komplette Archiv.
BASELINE_MARKER = "__baseline__"


@dataclass
class Subscription:
    id: int
    guild_id: int
    channel_id: int
    source: str
    target: str
    name: str
    patch_only: bool
    role_id: int | None


def _row(r: sqlite3.Row) -> Subscription:
    return Subscription(
        id=r["id"],
        guild_id=r["guild_id"],
        channel_id=r["channel_id"],
        source=r["source"],
        target=r["target"],
        name=r["name"],
        patch_only=bool(r["patch_only"]),
        role_id=r["role_id"],
    )


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- Abos ----------------------------------------------------------- #

    def add_subscription(
        self,
        guild_id: int,
        channel_id: int,
        source: str,
        target: str,
        name: str,
        patch_only: bool,
        role_id: int | None,
    ) -> int | None:
        """Legt ein Abo an. Gibt die neue ID zurück oder None, falls es schon existiert."""
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO subscriptions "
                    "(guild_id, channel_id, source, target, name, patch_only, role_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, channel_id, source, target, name, int(patch_only), role_id, int(time.time())),
                )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_subscription(self, guild_id: int, sub_id: int) -> Subscription | None:
        r = self.conn.execute(
            "SELECT * FROM subscriptions WHERE guild_id = ? AND id = ?", (guild_id, sub_id)
        ).fetchone()
        return _row(r) if r else None

    def guild_subscriptions(self, guild_id: int) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE guild_id = ? ORDER BY name COLLATE NOCASE", (guild_id,)
        ).fetchall()
        return [_row(r) for r in rows]

    def all_subscriptions(self) -> list[Subscription]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM subscriptions").fetchall()]

    def remove_subscription(self, guild_id: int, sub_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND id = ?", (guild_id, sub_id)
            )
        return cur.rowcount > 0

    def remove_guild(self, guild_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM subscriptions WHERE guild_id = ?", (guild_id,))

    def remove_channel(self, channel_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM subscriptions WHERE channel_id = ?", (channel_id,))

    # ---- Gesehene Meldungen -------------------------------------------- #

    def seen_ids(self, source: str, target: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT item_id FROM seen WHERE source = ? AND target = ?", (source, target)
        ).fetchall()
        return {r["item_id"] for r in rows}

    def mark_seen(self, source: str, target: str, item_ids: list[str]) -> None:
        now = int(time.time())
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen (source, target, item_id, seen_at) VALUES (?, ?, ?, ?)",
                [(source, target, i, now) for i in item_ids],
            )

    def set_baseline(self, source: str, target: str, item_ids: list[str]) -> None:
        """Alle aktuellen Meldungen als gesehen markieren (beim ersten Abo einer Quelle)."""
        self.mark_seen(source, target, [BASELINE_MARKER, *item_ids])
