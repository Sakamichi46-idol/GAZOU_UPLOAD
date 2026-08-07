"""Cheap local-learning queue. No external API calls."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from photo_database import get_connection
from app_settings import SETTINGS

def _now(): return datetime.now(timezone.utc).isoformat()

def enqueue_face_learning(face_id:int)->None:
    with closing(get_connection()) as con:
        con.execute("""INSERT INTO face_learning_queue(face_id,status,attempts,last_error,created_at,updated_at)
                       VALUES(?,'pending',0,'',?,?)
                       ON CONFLICT(face_id) DO UPDATE SET status='pending',last_error='',updated_at=excluded.updated_at""",
                    (int(face_id),_now(),_now())); con.commit()

def queue_stats()->dict:
    with closing(get_connection()) as con:
        rows=con.execute('SELECT status,COUNT(*) c FROM face_learning_queue GROUP BY status').fetchall()
    result={str(r[0]):int(r[1]) for r in rows}
    for key in ('pending','processing','completed','failed','dead'):
        result.setdefault(key,0)
    return result

def process_batch(limit:int|None=None, max_attempts:int=3)->dict:
    """Validate and activate confirmed embeddings. This never calls an external API."""
    batch=max(1,min(int(limit or SETTINGS.face_learning_batch),200))
    result={'found':0,'completed':0,'failed':0,'dead':0}
    with closing(get_connection()) as con:
        con.execute('BEGIN IMMEDIATE')
        rows=con.execute("""SELECT q.id,q.face_id,q.attempts,f.confirmed_person_id,f.confirmation_status,f.face_embedding
                            FROM face_learning_queue q LEFT JOIN photo_faces f ON f.id=q.face_id
                            WHERE q.status IN ('pending','failed') AND q.attempts<? ORDER BY q.id LIMIT ?""",
                         (max_attempts,batch)).fetchall()
        result['found']=len(rows)
        for row in rows:
            qid=int(row['id']); attempts=int(row['attempts'])+1
            try:
                if row['confirmed_person_id'] is None:
                    raise ValueError('確定人物がありません')
                if str(row['confirmation_status'] or '') not in {'confirmed','manually_confirmed','auto_seeded'}:
                    raise ValueError('本確定状態ではありません')
                if not str(row['face_embedding'] or '').strip():
                    raise ValueError('顔特徴量がありません')
                con.execute("UPDATE face_learning_queue SET status='completed',attempts=?,last_error='',updated_at=? WHERE id=?",
                            (attempts,_now(),qid)); result['completed']+=1
            except Exception as exc:
                status='dead' if attempts>=max_attempts else 'failed'
                con.execute('UPDATE face_learning_queue SET status=?,attempts=?,last_error=?,updated_at=? WHERE id=?',
                            (status,attempts,str(exc)[:1000],_now(),qid))
                result[status]+=1
        con.commit()
    return result

def retry_failed()->int:
    with closing(get_connection()) as con:
        cur=con.execute("UPDATE face_learning_queue SET status='pending',last_error='',updated_at=? WHERE status IN ('failed','dead')",(_now(),))
        con.commit(); return int(cur.rowcount)
