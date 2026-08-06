"""Ordered, restart-safe schema migrations."""
from __future__ import annotations
from datetime import datetime, timezone
from contextlib import closing
from photo_database import get_connection

def _now(): return datetime.now(timezone.utc).isoformat()
MIGRATIONS={
  1:"""CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL);""",
  2:"""CREATE TABLE IF NOT EXISTS photo_operation_locks(resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,owner_id TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(resource_type,resource_id));""",
  3:"""CREATE TABLE IF NOT EXISTS face_learning_queue(id INTEGER PRIMARY KEY AUTOINCREMENT,face_id INTEGER NOT NULL UNIQUE,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL); CREATE INDEX IF NOT EXISTS idx_face_learning_queue_status ON face_learning_queue(status,id);""",
  4:"""CREATE TABLE IF NOT EXISTS maintenance_state(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);""",
  5:"""CREATE INDEX IF NOT EXISTS idx_review_queue_status_type ON photo_review_queue(status,review_type,image_id); CREATE INDEX IF NOT EXISTS idx_image_people_status ON photo_image_people(relation_status,image_id,person_name); CREATE INDEX IF NOT EXISTS idx_faces_confirmed_person ON photo_faces(confirmation_status,confirmed_person_id,id);""",
  6:"""CREATE TABLE IF NOT EXISTS feature_registry(feature_key TEXT PRIMARY KEY,label TEXT NOT NULL,status TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);""",
  7:"""ALTER TABLE photo_face_candidates ADD COLUMN score_source TEXT NOT NULL DEFAULT 'local_face';""",
}

def run_migrations()->dict:
    applied=[]
    with closing(get_connection()) as con:
        con.execute(MIGRATIONS[1])
        done={int(r[0]) for r in con.execute('SELECT version FROM schema_migrations').fetchall()}
        for version in sorted(MIGRATIONS):
            if version in done: continue
            if version == 7:
                columns={str(r[1]) for r in con.execute('PRAGMA table_info(photo_face_candidates)').fetchall()}
                if 'score_source' not in columns:
                    con.execute("ALTER TABLE photo_face_candidates ADD COLUMN score_source TEXT NOT NULL DEFAULT 'local_face'")
            else:
                con.executescript(MIGRATIONS[version])
            con.execute('INSERT OR REPLACE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)',(version,f'migration_{version}',_now()))
            con.commit(); applied.append(version)
    return {'applied':applied,'latest':max(MIGRATIONS)}
