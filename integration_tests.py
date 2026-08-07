"""Executable offline integration tests using temporary SQLite databases."""
from __future__ import annotations
import os, sqlite3, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def _check(name, fn, checks):
    try: fn(); checks.append((name,True,''))
    except Exception as exc: checks.append((name,False,f'{type(exc).__name__}: {exc}'))

def run()->dict:
    checks=[]
    def init_db():
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'test.db'); env=os.environ.copy(); env['PHOTO_DB_PATH']=db
            code='import photo_database; photo_database.init_photo_db(); print("ok")'
            p=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
            if p.returncode: raise RuntimeError(p.stderr[-2000:])
            con=sqlite3.connect(db)
            required={'photo_images','photo_review_queue','photo_faces','photo_face_candidates'}
            actual={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            con.close()
            missing=required-actual
            if missing: raise AssertionError(f'不足テーブル: {sorted(missing)}')
    _check('fresh_database_initialization',init_db,checks)
    def transaction_rollback():
        from db_runtime import transaction
        with tempfile.TemporaryDirectory() as td:
            db=str(Path(td)/'x.db'); sqlite3.connect(db).execute('CREATE TABLE t(v INTEGER)').connection.commit()
            try:
                with transaction(db,immediate=True) as con:
                    con.execute('INSERT INTO t VALUES(1)'); raise RuntimeError('rollback')
            except RuntimeError: pass
            n=sqlite3.connect(db).execute('SELECT COUNT(*) FROM t').fetchone()[0]
            if n!=0: raise AssertionError('rollback failed')
    _check('transaction_rollback',transaction_rollback,checks)
    def view_source():
        text=(ROOT/'photo_review_view.py').read_text(encoding='utf-8')
        if 'row=3' not in text and 'row = 3' not in text: raise AssertionError('Select専用行なし')
    _check('review_view_row_safety',view_source,checks)
    failed=[x for x in checks if not x[1]]
    return {'ok':not failed,'total':len(checks),'failed':failed,'checks':checks}
if __name__=='__main__':
    import json; print(json.dumps(run(),ensure_ascii=False,indent=2))
