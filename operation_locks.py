"""Short-lived DB locks for duplicate admin actions."""
from __future__ import annotations
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
from photo_database import get_connection

def _now(): return datetime.now(timezone.utc)
@contextmanager
def resource_lock(resource_type:str, resource_id:str|int, owner_id:str|int, ttl_seconds:int=300):
    now=_now(); expiry=(now+timedelta(seconds=max(30,ttl_seconds))).isoformat()
    key=(str(resource_type),str(resource_id))
    with closing(get_connection()) as con:
        con.execute('DELETE FROM photo_operation_locks WHERE expires_at < ?', (now.isoformat(),))
        row=con.execute('SELECT owner_id FROM photo_operation_locks WHERE resource_type=? AND resource_id=?',key).fetchone()
        if row and str(row[0])!=str(owner_id): raise RuntimeError(f'この対象は現在、管理者 {row[0]} が操作中です。')
        con.execute('INSERT OR REPLACE INTO photo_operation_locks(resource_type,resource_id,owner_id,expires_at,created_at) VALUES(?,?,?,?,?)',(*key,str(owner_id),expiry,now.isoformat())); con.commit()
    try: yield
    finally:
        with closing(get_connection()) as con:
            con.execute('DELETE FROM photo_operation_locks WHERE resource_type=? AND resource_id=? AND owner_id=?',(*key,str(owner_id))); con.commit()
