"""Startpunkt: `python -m patchpost`"""

import logging
import os
import sys

from dotenv import load_dotenv

from .bot import PatchPost
from .db import Database


def acquire_single_instance_lock(path: str):
    """Verhindert, dass der Bot zweimal gleichzeitig läuft.

    Zwei Instanzen mit demselben Token reagieren beide auf jeden Befehl und
    stören sich gegenseitig. Gibt die offene Lock-Datei zurück (muss offen
    bleiben, solange der Bot läuft) oder None, wenn schon eine Instanz läuft.
    """
    lock_file = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Fehler: DISCORD_TOKEN fehlt. Kopiere .env.example nach .env und trag deinen Token ein.",
              file=sys.stderr)
        sys.exit(1)

    db_path = os.getenv("DB_PATH", "patchpost.db")
    lock = acquire_single_instance_lock(db_path + ".lock")
    if lock is None:
        print("Fehler: PatchPost läuft bereits (in einem anderen Terminal-Fenster?).\n"
              "Beende zuerst die laufende Instanz mit Strg+C und starte dann neu.", file=sys.stderr)
        sys.exit(1)

    poll_minutes = max(2, int(os.getenv("POLL_MINUTES", "10")))
    dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
    auto_publish = os.getenv("AUTO_PUBLISH", "true").lower() in ("1", "true", "yes", "ja")

    bot = PatchPost(
        db=Database(db_path),
        poll_minutes=poll_minutes,
        dev_guild_id=int(dev_guild) if dev_guild else None,
        auto_publish=auto_publish,
    )
    bot.run(token, log_handler=None)  # log_handler=None: unser Logging oben behalten


if __name__ == "__main__":
    main()
