"""Backup/restore verification and feature checklist without external services."""
from __future__ import annotations
import os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from photo_database import PHOTO_DB_PATH, get_connection
from app_settings import SETTINGS

def _now(): return datetime.now(timezone.utc).isoformat()
def backup_dir()->Path:
    p=Path(PHOTO_DB_PATH).parent/'backups'; p.mkdir(parents=True,exist_ok=True); return p

def verify_database(path:str)->dict:
    issues=[]; tables=0; counts={}
    try:
        con=sqlite3.connect(path)
        check=con.execute('PRAGMA integrity_check').fetchone()[0]
        if check!='ok': issues.append(str(check))
        names=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        tables=len(names)
        for name in ('photo_blogs','photo_images','photo_faces','photo_ai_tags','photo_manual_tags'):
            if name in names:
                counts[name]=int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        con.close()
    except Exception as e: issues.append(f'{type(e).__name__}: {e}')
    return {'ok':not issues,'issues':issues,'tables':tables,'counts':counts}

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

def list_backups(limit:int=20)->list[dict]:
    rows=[]
    for p in sorted(backup_dir().glob('photo_archive_*.db'),reverse=True)[:max(1,min(limit,100))]:
        rows.append({'name':p.name,'path':str(p),'size':p.stat().st_size,'verify':verify_database(str(p))})
    return rows

def diagnose_restore(backup_name:str='')->dict:
    backups=list_backups(100)
    if not backups: return {'ok':False,'issues':['バックアップがありません']}
    selected=next((x for x in backups if x['name']==backup_name),backups[0])
    current=verify_database(PHOTO_DB_PATH); candidate=selected['verify']
    differences={k:{'current':current['counts'].get(k,0),'backup':candidate['counts'].get(k,0)}
                 for k in sorted(set(current['counts'])|set(candidate['counts']))}
    return {'ok':bool(current['ok'] and candidate['ok']),'backup':selected,'current':current,'differences':differences}

def restore_verified_backup(backup_name:str)->dict:
    """Restore only after full verification, while automatically preserving the current DB."""
    selected=backup_dir()/Path(backup_name).name
    if not selected.exists(): raise FileNotFoundError('指定バックアップがありません')
    check=verify_database(str(selected))
    if not check['ok']: raise RuntimeError('復元元DBが不正です')
    safety=create_verified_backup()
    temp=Path(PHOTO_DB_PATH).with_suffix('.restore.tmp')
    src=sqlite3.connect(selected); out=sqlite3.connect(temp)
    try: src.backup(out)
    finally: out.close(); src.close()
    temp_check=verify_database(str(temp))
    if not temp_check['ok']:
        temp.unlink(missing_ok=True); raise RuntimeError('復元用一時DBの検証に失敗しました')
    os.replace(temp, PHOTO_DB_PATH)
    return {'restored':str(selected),'safety_backup':safety['path'],'verify':verify_database(PHOTO_DB_PATH)}

def feature_checklist()->list[dict]:
    defaults=[('admin_help','管理者使い方パネル','implemented'),('ai_center','AI育成センター','implemented'),('tag_master','タグマスター','implemented'),('face_candidate_center','AI顔候補確認','implemented'),('backup_restore','バックアップ作成・復元診断','implemented'),('integration_tests','仮DB統合テスト','implemented'),('railway_smoke','Railway実操作スモークテスト','partial')]
    now=_now()
    with closing(get_connection()) as con:
        for key,label,status in defaults:
            con.execute('INSERT INTO feature_registry(feature_key,label,status,notes,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(feature_key) DO UPDATE SET label=excluded.label,status=excluded.status,updated_at=excluded.updated_at',(key,label,status,'',now))
        con.commit(); rows=con.execute('SELECT * FROM feature_registry ORDER BY label').fetchall()
    return [dict(r) for r in rows]
