"""Short-lived DB locks for duplicate admin actions.

The lock is intentionally non-reentrant: even the same Discord administrator cannot
start a second write for the same resource until the first one finishes.  This
prevents double taps and two separately opened ephemeral views from overwriting one
another.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta

from photo_database import get_connection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_table(con) -> None:
    """Keep the lock usable even before a newer migration has run."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_operation_locks(
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(resource_type, resource_id)
        )
        """
    )


@contextmanager
def resource_lock(
    resource_type: str,
    resource_id: str | int,
    owner_id: str | int,
    ttl_seconds: int = 300,
):
    """Acquire an exclusive short-lived lock for one logical resource.

    A second operation is rejected even when it comes from the same administrator.
    Expired rows are removed on every acquisition, so a crashed process does not
    leave a permanent lock behind.
    """
    now = _now()
    expiry = (now + timedelta(seconds=max(30, ttl_seconds))).isoformat()
    key = (str(resource_type), str(resource_id))
    owner = str(owner_id)

    with closing(get_connection()) as con:
        _ensure_table(con)
        con.execute("DELETE FROM photo_operation_locks WHERE expires_at < ?", (now.isoformat(),))
        row = con.execute(
            "SELECT owner_id FROM photo_operation_locks WHERE resource_type=? AND resource_id=?",
            key,
        ).fetchone()
        if row:
            current_owner = str(row[0])
            if current_owner == owner:
                raise RuntimeError("この対象はすでに処理中です。連続タップせず、完了を待ってください。")
            raise RuntimeError(f"この対象は現在、管理者 {current_owner} が操作中です。")
        con.execute(
            """
            INSERT INTO photo_operation_locks(
                resource_type, resource_id, owner_id, expires_at, created_at
            ) VALUES(?,?,?,?,?)
            """,
            (*key, owner, expiry, now.isoformat()),
        )
        con.commit()

    try:
        yield
    finally:
        with closing(get_connection()) as con:
            _ensure_table(con)
            con.execute(
                "DELETE FROM photo_operation_locks WHERE resource_type=? AND resource_id=? AND owner_id=?",
                (*key, owner),
            )
            con.commit()
