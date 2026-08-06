"""Cheap local-learning queue. No external API calls."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from photo_database import get_connection

def _now(): return datetime.now(timezone.utc).isoformat()
def enqueue_face_learning(face_id:int)->None:
    with closing(get_connection()) as con:
        con.execute("""INSERT INTO face_learning_queue(face_id,status,attempts,last_error,created_at,updated_at) VALUES(?,'pending',0,'',?,?) ON CONFLICT(face_id) DO UPDATE SET status='pending',updated_at=excluded.updated_at""",(int(face_id),_now(),_now())); con.commit()
def queue_stats()->dict:
    with closing(get_connection()) as con:
        rows=con.execute('SELECT status,COUNT(*) c FROM face_learning_queue GROUP BY status').fetchall()
    return {str(r[0]):int(r[1]) for r in rows}
