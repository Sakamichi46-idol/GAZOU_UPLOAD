"""Restart-safe batched tag-master migration. No API calls."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from photo_database import get_connection
from tag_master import init_schema, prepare_tag
from app_settings import SETTINGS

def _now(): return datetime.now(timezone.utc).isoformat()
def migrate_batch(limit:int|None=None)->dict:
    batch=max(10,min(int(limit or SETTINGS.tag_migration_batch),2000))
    with closing(get_connection()) as con:
        init_schema(con)
        con.execute("CREATE TABLE IF NOT EXISTS tag_migration_state(source TEXT PRIMARY KEY,last_id INTEGER NOT NULL DEFAULT 0,processed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)")
        result={}
        for source,table in [('ai','photo_ai_tags'),('manual','photo_manual_tags')]:
            state=con.execute('SELECT last_id,processed FROM tag_migration_state WHERE source=?',(source,)).fetchone()
            last_id=int(state[0]) if state else 0; processed=int(state[1]) if state else 0
            rows=con.execute(f'SELECT id,tag,category'+(',confidence' if source=='ai' else '')+f' FROM {table} WHERE id>? ORDER BY id LIMIT ?',(last_id,batch)).fetchall()
            for r in rows:
                prepare_tag(con,str(r['tag']),str(r['category']),source=source,confidence=float(r['confidence']) if source=='ai' else 1.0)
                last_id=int(r['id']); processed+=1
            con.execute('INSERT INTO tag_migration_state(source,last_id,processed,updated_at) VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id,processed=excluded.processed,updated_at=excluded.updated_at',(source,last_id,processed,_now()))
            result[source]={'batch':len(rows),'processed':processed,'last_id':last_id,'done':len(rows)<batch}
        con.commit(); return result
