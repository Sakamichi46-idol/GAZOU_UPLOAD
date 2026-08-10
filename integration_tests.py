"""Executable offline integration tests using temporary SQLite databases."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _check(name, fn, checks):
    try:
        fn()
        checks.append((name, True, ""))
    except Exception as exc:
        checks.append((name, False, f"{type(exc).__name__}: {exc}"))


def _run_subprocess(code: str, db_path: str, timeout: int = 30) -> None:
    env = os.environ.copy()
    env["PHOTO_DB_PATH"] = db_path
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout)[-3000:]
        raise RuntimeError(detail)


def run() -> dict:
    checks = []

    def init_db():
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "test.db")
            _run_subprocess(
                'import photo_database; photo_database.init_photo_db(); print("ok")',
                db,
            )
            con = sqlite3.connect(db)
            required = {
                "photo_images",
                "photo_review_queue",
                "photo_faces",
                "photo_face_candidates",
            }
            actual = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            con.close()
            missing = required - actual
            if missing:
                raise AssertionError(f"不足テーブル: {sorted(missing)}")

    _check("fresh_database_initialization", init_db, checks)

    def transaction_rollback():
        from db_runtime import transaction

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "x.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE t(v INTEGER)")
            con.commit()
            con.close()
            try:
                with transaction(db, immediate=True) as tx:
                    tx.execute("INSERT INTO t VALUES(1)")
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
            con = sqlite3.connect(db)
            n = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            con.close()
            if n != 0:
                raise AssertionError("rollback failed")

    _check("transaction_rollback", transaction_rollback, checks)

    def view_source():
        text = (ROOT / "photo_review_view.py").read_text(encoding="utf-8")
        if "row=3" not in text and "row = 3" not in text:
            raise AssertionError("Select専用行なし")

    _check("review_view_row_safety", view_source, checks)

    def person_confirm_state_sync():
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "person.db")
            code = r'''
import photo_database
photo_database.init_photo_db()
now = photo_database.utc_now_text()
with photo_database.get_connection() as con:
    cur = con.execute(
        "INSERT INTO photo_blogs(blog_url,group_name,member_name,title,published_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ('https://example.invalid/blog','櫻坂46','投稿者','test','',now,now),
    )
    blog_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO photo_images(blog_id,image_url,image_index,created_at,updated_at) VALUES(?,?,?,?,?)",
        (blog_id,'https://example.invalid/a.jpg',1,now,now),
    )
    image_id = cur.lastrowid
    con.commit()
photo_database.set_confirmed_image_people(
    image_id,
    ['小島凪紗','増本綺良'],
    confirmed_by='test',
    note='integration',
)
with photo_database.get_connection() as con:
    q = con.execute(
        "SELECT status,selected_value FROM photo_review_queue WHERE image_id=?",
        (image_id,),
    ).fetchone()
    people = [
        r[0]
        for r in con.execute(
            "SELECT person_name FROM photo_image_people WHERE image_id=? AND relation_status='confirmed' ORDER BY person_name",
            (image_id,),
        ).fetchall()
    ]
assert q and q[0] == 'completed', q
assert '小島凪紗' in q[1] and '増本綺良' in q[1], q
assert set(people) == {'小島凪紗','増本綺良'}, people
print('ok')
'''
            _run_subprocess(code, db)

    _check("person_confirm_state_sync", person_confirm_state_sync, checks)

    def operation_lock_double_acquire():
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "lock.db")
            code = r'''
import photo_database
photo_database.init_photo_db()
from operation_locks import resource_lock
failed = False
with resource_lock('image_people_confirm', 1, 123, ttl_seconds=60):
    try:
        with resource_lock('image_people_confirm', 1, 123, ttl_seconds=60):
            pass
    except RuntimeError:
        failed = True
assert failed, 'same owner duplicate lock was not rejected'
print('ok')
'''
            _run_subprocess(code, db)

    _check("operation_lock_double_acquire", operation_lock_double_acquire, checks)

    failed = [x for x in checks if not x[1]]
    return {
        "ok": not failed,
        "total": len(checks),
        "failed": failed,
        "checks": checks,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run(), ensure_ascii=False, indent=2))
