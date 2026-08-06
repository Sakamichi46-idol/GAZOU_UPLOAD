"""SQLite runtime helpers shared across the project."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from typing import Iterator
from app_settings import SETTINGS

def configure_connection(con:sqlite3.Connection)->sqlite3.Connection:
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute(f'PRAGMA busy_timeout={SETTINGS.sqlite_busy_timeout_ms}')
    return con

def connect(path:str)->sqlite3.Connection:
    return configure_connection(sqlite3.connect(path, timeout=SETTINGS.sqlite_timeout))

@contextmanager
def transaction(path:str, *, immediate:bool=False)->Iterator[sqlite3.Connection]:
    con=connect(path)
    try:
        con.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
        yield con
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
