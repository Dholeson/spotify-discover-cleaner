"""
Research Queue repository.

CRUD for the interests + interest_reasons tables.
An "interest" is any track, artist, or album the user wants to investigate
further — explicitly separate from liked songs, playlists, or queue actions.
"""

import json
import time
from typing import Optional

from .db import get_conn

# ---------------------------------------------------------------------------
# Seed reason tags
# ---------------------------------------------------------------------------

SEED_REASON_TAGS = [
    "interesting vocals",
    "guitar tone",
    "atmosphere",
    "production",
    "lyrics",
    "emotional pull",
    "revisit later",
    "artist curiosity",
    "album curiosity",
    "unusual structure",
    "sonic texture",
    "era / period",
    "live version",
    "cover / rendition",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_dict(row) -> dict:
    d = dict(row)
    # Attach reasons in a second query; callers use get_interest() for full detail
    return d


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def _clean_reasons(reasons) -> list:
    """Reason tags as a de-duplicated list of non-empty strings (ValueError otherwise)."""
    if reasons is None:
        return []
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, (list, tuple)) or not all(isinstance(t, str) for t in reasons):
        raise ValueError("reasons must be a list of strings")
    out = []
    for tag in reasons:
        tag = tag.strip()
        if tag and tag not in out:
            out.append(tag[:80])
    return out


def _clean_note(note):
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("note must be a string")
    return note.strip() or None


def add_interest(
    entity_type: str,
    entity_name: str,
    entity_id: str = None,
    reasons: list = None,
    note: str = None,
) -> int:
    """
    Create a new research interest. Returns the interest id.
    entity_type: 'track' | 'artist' | 'album'

    Adding something that is already an *active* interest returns the existing row
    (with any new reason tags merged in) instead of creating a duplicate.
    """
    if entity_type not in ("track", "artist", "album"):
        raise ValueError(f"Invalid entity_type: {entity_type}")
    if not isinstance(entity_name, str) or not entity_name.strip():
        raise ValueError("entity_name is required")
    entity_name = entity_name.strip()
    tags = _clean_reasons(reasons)
    note = _clean_note(note)

    now = _now_ms()
    conn = get_conn()
    conn.commit()
    with conn:  # one transaction: commits on success, rolls back on any error
        if entity_id:
            existing = conn.execute(
                "SELECT id FROM interests WHERE status = 'active' AND entity_type = ? AND entity_id = ? "
                "ORDER BY id LIMIT 1", (entity_type, entity_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM interests WHERE status = 'active' AND entity_type = ? AND entity_id IS NULL "
                "AND lower(entity_name) = lower(?) ORDER BY id LIMIT 1", (entity_type, entity_name),
            ).fetchone()
        if existing:
            interest_id = existing[0]
            have = {r[0] for r in conn.execute(
                "SELECT tag FROM interest_reasons WHERE interest_id = ?", (interest_id,))}
            for tag in tags:
                if tag not in have:
                    conn.execute("INSERT INTO interest_reasons (interest_id, tag) VALUES (?, ?)",
                                 (interest_id, tag))
            conn.execute("UPDATE interests SET updated_at = ? WHERE id = ?", (now, interest_id))
            return interest_id

        cur = conn.execute(
            """
            INSERT INTO interests
                (entity_type, entity_id, entity_name, status, note, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (entity_type, entity_id or None, entity_name, note, now, now),
        )
        interest_id = cur.lastrowid
        for tag in tags:
            conn.execute(
                "INSERT INTO interest_reasons (interest_id, tag) VALUES (?, ?)",
                (interest_id, tag),
            )
    return interest_id


def update_interest(
    interest_id: int,
    status: str = None,
    note: str = None,
    reasons: list = None,
) -> bool:
    """
    Update an existing interest. Pass only the fields you want to change.
    Returns False when the interest does not exist; raises ValueError on bad input.
    """
    if status is not None and status not in ("active", "promoted", "archived"):
        raise ValueError(f"Invalid status: {status}")
    note_given = note is not None
    note = _clean_note(note)
    tags = _clean_reasons(reasons) if reasons is not None else None

    conn = get_conn()
    conn.commit()
    if not conn.execute("SELECT 1 FROM interests WHERE id = ?", (interest_id,)).fetchone():
        return False

    updates = ["updated_at = ?"]
    params = [_now_ms()]
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if note_given:
        updates.append("note = ?")
        params.append(note)
    params.append(interest_id)

    with conn:
        conn.execute(f"UPDATE interests SET {', '.join(updates)} WHERE id = ?", params)
        if tags is not None:
            conn.execute("DELETE FROM interest_reasons WHERE interest_id = ?", (interest_id,))
            for tag in tags:
                conn.execute(
                    "INSERT INTO interest_reasons (interest_id, tag) VALUES (?, ?)",
                    (interest_id, tag),
                )
    return True


def archive_interest(interest_id: int) -> None:
    update_interest(interest_id, status="archived")


def promote_interest(interest_id: int) -> None:
    update_interest(interest_id, status="promoted")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _attach_reasons(conn, interests: list) -> list:
    if not interests:
        return interests
    ids = [i["id"] for i in interests]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT interest_id, tag FROM interest_reasons WHERE interest_id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    reason_map: dict[int, list] = {}
    for r in rows:
        reason_map.setdefault(r["interest_id"], []).append(r["tag"])
    for i in interests:
        i["reasons"] = reason_map.get(i["id"], [])
    return interests


def get_interests(status: str = None, entity_type: str = None, limit: int = 200) -> list:
    """Return research items, newest first, with reasons attached."""
    conn = get_conn()
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""
        SELECT id, entity_type, entity_id, entity_name, status, note,
               created_at, updated_at
        FROM interests
        {clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [dict(r) for r in rows]
    return _attach_reasons(conn, items)


def get_interest(interest_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT id, entity_type, entity_id, entity_name, status, note,
               created_at, updated_at
        FROM interests WHERE id = ?
        """,
        (interest_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    return _attach_reasons(conn, [item])[0]


def get_count_by_status() -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM interests GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
