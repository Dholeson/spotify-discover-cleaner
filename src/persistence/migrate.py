"""
One-time migration: .config.json history → SQLite.

Runs at startup and is a no-op once the migration marker is set.
All original data stays in .config.json; this only copies into SQLite.
"""

import json
import logging
from pathlib import Path

from .db import DATA_DIR, get_conn
from .events import write_from_legacy

log = logging.getLogger(__name__)

CONFIG_PATH = Path(DATA_DIR) / ".config.json"
MIGRATION_KEY = "migrated_config_history_v1"


def _already_migrated() -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (MIGRATION_KEY,)
    ).fetchone()
    return row is not None


def _mark_migrated(count: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
        (MIGRATION_KEY, json.dumps({"entries": count})),
    )
    conn.commit()


def migrate_if_needed() -> None:
    """
    If not already migrated, read history from .config.json and write each
    entry as a canonical event row.  Safe to call on every startup.
    """
    if _already_migrated():
        return

    if not CONFIG_PATH.exists():
        _mark_migrated(0)
        return

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("migrate_if_needed: could not read .config.json: %s", exc)
        _mark_migrated(0)
        return

    history = cfg.get("history") or []
    count = 0
    errors = 0

    for entry in history:
        try:
            write_from_legacy(entry)
            count += 1
        except Exception as exc:
            log.debug("migrate entry failed: %s — %s", entry.get("track_id"), exc)
            errors += 1

    _mark_migrated(count)
    log.info(
        "SQLite migration complete: %d events imported, %d skipped", count, errors
    )
    if errors:
        log.warning("  %d entries could not be imported (see debug log)", errors)
