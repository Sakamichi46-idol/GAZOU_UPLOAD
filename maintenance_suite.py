"""Backup/restore verification and feature checklist without external services."""
from __future__ import annotations
import json, os, shutil, sqlite3, zipfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from photo_database import PHOTO_DB_PATH, get_connection
from app_settings import SETTINGS

def _now(): return datetime.now(timezone.utc).isoformat()
def backup_dir()->Path:
    p=Path(PHOTO_DB_PATH).parent/'backups'; p.mkdir(parents=True,exist_ok=True); return p

def create_verified_backup()->dict:
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dest=backup_dir()/f'photo_archive_{stamp}.db'
    src=sqlite3.connect(PHOTO_DB_PATH); out=sqlite3.connect(dest)
    try: src.backup(out)
    finally: out.close(); src.close()
    verify=verify_database(str(dest))
    if not verify['ok']: dest.unlink(missing_ok=True); raise RuntimeError('バックアップ整合性検査に失敗: '+ '; '.join(verify['issues']))
    files=sorted(backup_dir().glob('photo_archive_*.db'),reverse=True)
    for old in files[SETTINGS.backup_keep:]: old.unlink(missing_ok=True)
    return {'path':str(dest),'size':dest.stat().st_size,'verify':verify}

def verify_database(path:str)->dict:
    issues=[]
    try:
        con=sqlite3.connect(path)
        check=con.execute('PRAGMA integrity_check').fetchone()[0]
        if check!='ok': issues.append(str(check))
        tables=int(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        con.close()
    except Exception as e: issues.append(f'{type(e).__name__}: {e}'); tables=0
    return {'ok':not issues,'issues':issues,'tables':tables}

def feature_checklist()->list[dict]:
    defaults=[('admin_help','管理者使い方パネル','implemented'),('ai_center','AI育成センター','implemented'),('tag_master','タグマスター','implemented'),('face_candidate_center','AI顔候補確認','implemented'),('backup_restore','バックアップ復元検証','implemented'),('full_click_e2e','Discord実クリックE2E','partial')]
    now=_now()
    with closing(get_connection()) as con:
        for key,label,status in defaults:
            con.execute('INSERT INTO feature_registry(feature_key,label,status,notes,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(feature_key) DO UPDATE SET label=excluded.label,status=excluded.status,updated_at=excluded.updated_at',(key,label,status,'',now))
        con.commit(); rows=con.execute('SELECT * FROM feature_registry ORDER BY label').fetchall()
    return [dict(r) for r in rows]
