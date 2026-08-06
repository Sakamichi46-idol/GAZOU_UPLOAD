"""Central validated settings for the photo archive bot."""
from __future__ import annotations
import os
from dataclasses import dataclass


def _int(name:str, default:int, lo:int, hi:int)->int:
    try: v=int(str(os.getenv(name, default)).strip())
    except (TypeError,ValueError): v=default
    return max(lo,min(v,hi))

def _bool(name:str, default:bool)->bool:
    raw=str(os.getenv(name, '1' if default else '0')).strip().lower()
    return raw in {'1','true','yes','on','enabled'}

@dataclass(frozen=True)
class Settings:
    sqlite_timeout:int=_int('PHOTO_SQLITE_TIMEOUT',30,5,300)
    sqlite_busy_timeout_ms:int=_int('PHOTO_SQLITE_BUSY_TIMEOUT_MS',10000,1000,120000)
    backup_keep:int=_int('PHOTO_BACKUP_KEEP',14,1,100)
    face_learning_batch:int=_int('PHOTO_FACE_LEARNING_BATCH',25,1,200)
    tag_migration_batch:int=_int('PHOTO_TAG_MIGRATION_BATCH',250,10,2000)
    maintenance_mode:bool=_bool('PHOTO_MAINTENANCE_MODE',False)
    structured_logs:bool=_bool('PHOTO_STRUCTURED_LOGS',True)

SETTINGS=Settings()
