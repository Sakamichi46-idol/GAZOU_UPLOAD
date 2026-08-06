"""Non-destructive tag normalization, master data, quality and search cache.

This module never calls external APIs. Raw AI/manual tags remain in their original
assignment tables; canonicalization is represented by master/alias tables so an
administrator can change mappings without rewriting historical data.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

CATEGORY_DEFS: dict[str, str] = {
    "person": "人物", "hair": "髪型・髪色", "expression": "表情・雰囲気",
    "clothing": "服装", "accessory": "小物・アクセサリー", "location": "場所・背景",
    "event": "イベント", "food": "食べ物・飲み物", "shooting": "撮影・構図",
    "pose": "ポーズ", "season": "季節・天気", "animal": "動物", "other": "その他",
}
CATEGORY_ALIASES = {
    "background": "location", "weather": "season", "person_count": "shooting",
    "composition": "shooting", "object": "accessory", "objects": "accessory",
    "manual": "other", "": "other",
}
DEFAULT_CONFIDENCE = {
    "person": 0.90, "hair": 0.65, "expression": 0.60, "clothing": 0.65,
    "accessory": 0.65, "location": 0.60, "event": 0.65, "food": 0.70,
    "shooting": 0.60, "pose": 0.60, "season": 0.60, "animal": 0.70,
    "other": 0.75,
}
BLOCK_EXACT = {
    "", "-", "--", "---", ".", "..", "...", "?", "??", "不明", "なし",
    "その他", "unknown", "none", "null", "n/a", "na", "タグなし",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"[\s　]+", "", text)
    text = text.replace("／", "/").replace("・", "")
    text = re.sub(r"[。．\.]+$", "", text)
    return text


def clean_display(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\s　]+", " ", text)
    text = re.sub(r"[。．\.]+$", "", text)
    return text[:120]


def normalize_category(category: str, tag: str = "") -> str:
    clean = normalize_key(category).replace("_", "")
    raw = str(category or "").strip().lower()
    result = CATEGORY_ALIASES.get(raw, raw)
    if result in CATEGORY_DEFS:
        return result
    key = normalize_key(tag)
    if re.fullmatch(r"\d+人", key) or key in {"ソロ", "1人", "2人", "3人", "集合写真"}:
        return "shooting"
    return "other"


def is_meaningless(tag: str) -> bool:
    key = normalize_key(tag)
    if key in BLOCK_EXACT:
        return True
    if len(key) <= 1 and not re.fullmatch(r"[猫犬花海空]", key):
        return True
    if re.fullmatch(r"[-_.、。・/\\]+", key):
        return True
    return False


def init_schema(connection: Any) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tag_master (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_tag TEXT NOT NULL,
            normalized_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT 'other',
            status TEXT NOT NULL DEFAULT 'pending',
            searchable INTEGER NOT NULL DEFAULT 0,
            minimum_confidence REAL NOT NULL DEFAULT 0.75,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tag_aliases (
            alias_key TEXT PRIMARY KEY,
            alias_tag TEXT NOT NULL,
            canonical_tag_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(canonical_tag_id) REFERENCES tag_master(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS photo_ai_tag_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            tag TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            model_name TEXT NOT NULL DEFAULT '',
            raw_value TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'replaced',
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tag_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            normalized_key TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(image_id, normalized_key)
        );
        CREATE TABLE IF NOT EXISTS tag_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tag_search_cache (
            canonical_tag_id INTEGER NOT NULL,
            image_id INTEGER NOT NULL,
            source_priority INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(canonical_tag_id, image_id),
            FOREIGN KEY(canonical_tag_id) REFERENCES tag_master(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tag_master_status_category
          ON tag_master(status, searchable, category);
        CREATE INDEX IF NOT EXISTS idx_tag_aliases_master ON tag_aliases(canonical_tag_id);
        CREATE INDEX IF NOT EXISTS idx_tag_search_cache_image ON tag_search_cache(image_id);
        CREATE INDEX IF NOT EXISTS idx_ai_tags_category_confidence
          ON photo_ai_tags(category, confidence, image_id);
        CREATE INDEX IF NOT EXISTS idx_manual_tags_category_image
          ON photo_manual_tags(category, image_id);
        """
    )


def ensure_master(connection: Any, tag: str, category: str, *, status: str = "pending") -> tuple[int, str, str, bool]:
    display = clean_display(tag)
    key = normalize_key(display)
    category = normalize_category(category, display)
    blocked = is_meaningless(display)
    if blocked:
        status = "blocked"
    row = connection.execute(
        """SELECT m.id,m.canonical_tag,m.category,m.status,m.searchable
           FROM tag_aliases a JOIN tag_master m ON m.id=a.canonical_tag_id
           WHERE a.alias_key=? LIMIT 1""", (key,)
    ).fetchone()
    if row:
        return int(row[0]), str(row[1]), str(row[2]), str(row[3]) == "blocked"
    row = connection.execute("SELECT id,canonical_tag,category,status FROM tag_master WHERE normalized_key=?", (key,)).fetchone()
    now = utc_now()
    if row:
        master_id = int(row[0])
    else:
        threshold = DEFAULT_CONFIDENCE.get(category, 0.75)
        searchable = 1 if status == "approved" and not blocked else 0
        cur = connection.execute(
            """INSERT INTO tag_master(canonical_tag,normalized_key,category,status,searchable,minimum_confidence,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (display, key, category, status, searchable, threshold, now, now),
        )
        master_id = int(cur.lastrowid)
    connection.execute(
        """INSERT INTO tag_aliases(alias_key,alias_tag,canonical_tag_id,created_at,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET alias_tag=excluded.alias_tag,updated_at=excluded.updated_at""",
        (key, display, master_id, now, now),
    )
    row = connection.execute("SELECT canonical_tag,category,status FROM tag_master WHERE id=?", (master_id,)).fetchone()
    return master_id, str(row[0]), str(row[1]), str(row[2]) == "blocked"


def prepare_tag(connection: Any, tag: str, category: str, *, source: str, confidence: float | None = None) -> dict[str, Any]:
    init_schema(connection)
    display = clean_display(tag)
    master_id, canonical, normalized_category, blocked = ensure_master(
        connection, display, category, status="approved" if source == "manual" else "pending"
    )
    row = connection.execute(
        "SELECT status,searchable,minimum_confidence FROM tag_master WHERE id=?", (master_id,)
    ).fetchone()
    threshold = float(row[2] or DEFAULT_CONFIDENCE.get(normalized_category, 0.75))
    confidence_value = float(confidence or 0)
    searchable = bool(row[1]) and str(row[0]) == "approved"
    if source == "ai" and confidence_value < threshold:
        searchable = False
    return {
        "raw_tag": display, "canonical_tag": canonical, "category": normalized_category,
        "master_id": master_id, "blocked": blocked, "searchable": searchable,
        "threshold": threshold,
    }


def archive_ai_tags(connection: Any, image_id: int, action: str = "replaced") -> int:
    init_schema(connection)
    now = utc_now()
    cur = connection.execute(
        """INSERT INTO photo_ai_tag_history(image_id,category,tag,confidence,model_name,raw_value,action,recorded_at)
           SELECT image_id,category,tag,confidence,model_name,raw_value,?,? FROM photo_ai_tags WHERE image_id=?""",
        (action, now, int(image_id)),
    )
    return int(cur.rowcount or 0)


def rebuild_cache(connection: Any) -> dict[str, int]:
    init_schema(connection)
    register_sqlite_functions(connection)
    connection.execute("DELETE FROM tag_search_cache")
    now = utc_now()
    # Manual tags have priority and are always searchable unless blocked.
    connection.execute(
        """INSERT OR REPLACE INTO tag_search_cache(canonical_tag_id,image_id,source_priority,updated_at)
           SELECT a.canonical_tag_id,t.image_id,3,?
           FROM photo_manual_tags t JOIN tag_aliases a ON a.alias_key = tag_normalized_key(t.tag)
           JOIN tag_master m ON m.id=a.canonical_tag_id
           WHERE m.status='approved' AND m.searchable=1""", (now,)
    )
    # AI tags are included only above category/master threshold and not rejected.
    connection.execute(
        """INSERT OR IGNORE INTO tag_search_cache(canonical_tag_id,image_id,source_priority,updated_at)
           SELECT a.canonical_tag_id,t.image_id,2,?
           FROM photo_ai_tags t JOIN tag_aliases a ON a.alias_key = tag_normalized_key(t.tag)
           JOIN tag_master m ON m.id=a.canonical_tag_id
           WHERE m.status='approved' AND m.searchable=1
             AND t.confidence >= m.minimum_confidence
             AND NOT EXISTS(SELECT 1 FROM tag_rejections r WHERE r.image_id=t.image_id AND r.normalized_key=a.alias_key)""", (now,)
    )
    count = int(connection.execute("SELECT COUNT(*) FROM tag_search_cache").fetchone()[0])
    tags = int(connection.execute("SELECT COUNT(DISTINCT canonical_tag_id) FROM tag_search_cache").fetchone()[0])
    return {"assignments": count, "tags": tags}


def register_sqlite_functions(connection: Any) -> None:
    connection.create_function("tag_normalized_key", 1, normalize_key)


def bootstrap_from_existing(connection: Any, *, approve_known: bool = False) -> dict[str, int]:
    init_schema(connection)
    register_sqlite_functions(connection)
    added = blocked = 0
    for table, source in (("photo_ai_tags", "ai"), ("photo_manual_tags", "manual")):
        rows = connection.execute(f"SELECT DISTINCT category,tag FROM {table} WHERE TRIM(COALESCE(tag,''))<>''").fetchall()
        for category, tag in rows:
            prepared = prepare_tag(connection, str(tag), str(category), source=source)
            added += 1
            blocked += int(prepared["blocked"])
    # Curated aliases from legacy code are trusted and approved.
    try:
        from photo_search_tags import SEARCH_TAG_ALIASES
        for category, mapping in SEARCH_TAG_ALIASES.items():
            for canonical, aliases in mapping.items():
                mid, _, _, _ = ensure_master(connection, canonical, category, status="approved")
                connection.execute("UPDATE tag_master SET status='approved',searchable=1,category=?,updated_at=? WHERE id=?", (normalize_category(category), utc_now(), mid))
                for alias in (canonical, *aliases):
                    key = normalize_key(alias)
                    connection.execute(
                        """INSERT INTO tag_aliases(alias_key,alias_tag,canonical_tag_id,created_at,updated_at)
                           VALUES(?,?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET canonical_tag_id=excluded.canonical_tag_id,alias_tag=excluded.alias_tag,updated_at=excluded.updated_at""",
                        (key, clean_display(alias), mid, utc_now(), utc_now()),
                    )
    except Exception:
        pass
    connection.commit()
    return {"processed": added, "blocked": blocked}


def diagnostic_summary(connection: Any) -> dict[str, int]:
    init_schema(connection)
    register_sqlite_functions(connection)
    return {
        "master": int(connection.execute("SELECT COUNT(*) FROM tag_master").fetchone()[0]),
        "approved": int(connection.execute("SELECT COUNT(*) FROM tag_master WHERE status='approved'").fetchone()[0]),
        "pending": int(connection.execute("SELECT COUNT(*) FROM tag_master WHERE status='pending'").fetchone()[0]),
        "blocked": int(connection.execute("SELECT COUNT(*) FROM tag_master WHERE status='blocked'").fetchone()[0]),
        "aliases": int(connection.execute("SELECT COUNT(*) FROM tag_aliases").fetchone()[0]),
        "low_confidence": int(connection.execute("""SELECT COUNT(*) FROM photo_ai_tags t JOIN tag_aliases a ON a.alias_key=tag_normalized_key(t.tag) JOIN tag_master m ON m.id=a.canonical_tag_id WHERE t.confidence < m.minimum_confidence""").fetchone()[0]),
        "cache": int(connection.execute("SELECT COUNT(*) FROM tag_search_cache").fetchone()[0]),
    }


def merge_candidates(connection: Any, limit: int = 100) -> list[dict[str, Any]]:
    init_schema(connection)
    rows = connection.execute("SELECT id,canonical_tag,normalized_key,category FROM tag_master WHERE status!='blocked' ORDER BY category,canonical_tag").fetchall()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[str(row[3])].append(row)
    candidates: list[dict[str, Any]] = []
    for category, items in grouped.items():
        for i, left in enumerate(items):
            for right in items[i + 1:i + 80]:
                ratio = SequenceMatcher(None, str(left[2]), str(right[2])).ratio()
                if ratio >= 0.78 and str(left[2]) != str(right[2]):
                    candidates.append({"left_id": int(left[0]), "left": str(left[1]), "right_id": int(right[0]), "right": str(right[1]), "category": category, "similarity": ratio})
    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates[: max(1, min(limit, 500))]


def approve_tag(connection: Any, master_id: int, *, searchable: bool = True, actor: str = "") -> None:
    init_schema(connection)
    connection.execute("UPDATE tag_master SET status='approved',searchable=?,updated_at=? WHERE id=?", (1 if searchable else 0, utc_now(), int(master_id)))
    connection.execute("INSERT INTO tag_change_log(action,details_json,created_by,created_at) VALUES('approve',?,?,?)", (json.dumps({"master_id": int(master_id)}, ensure_ascii=False), actor, utc_now()))
    connection.commit()


def block_tag(connection: Any, master_id: int, *, actor: str = "") -> None:
    init_schema(connection)
    connection.execute("UPDATE tag_master SET status='blocked',searchable=0,updated_at=? WHERE id=?", (utc_now(), int(master_id)))
    connection.execute("INSERT INTO tag_change_log(action,details_json,created_by,created_at) VALUES('block',?,?,?)", (json.dumps({"master_id": int(master_id)}, ensure_ascii=False), actor, utc_now()))
    connection.commit()


def merge_tags(connection: Any, source_id: int, target_id: int, *, actor: str = "") -> None:
    if int(source_id) == int(target_id):
        return
    init_schema(connection)
    now = utc_now()
    source = connection.execute("SELECT canonical_tag FROM tag_master WHERE id=?", (int(source_id),)).fetchone()
    target = connection.execute("SELECT canonical_tag FROM tag_master WHERE id=?", (int(target_id),)).fetchone()
    if not source or not target:
        raise ValueError("統合元または統合先のタグが存在しません")
    connection.execute("UPDATE tag_aliases SET canonical_tag_id=?,updated_at=? WHERE canonical_tag_id=?", (int(target_id), now, int(source_id)))
    connection.execute("UPDATE tag_master SET status='merged',searchable=0,updated_at=? WHERE id=?", (now, int(source_id)))
    connection.execute("INSERT INTO tag_change_log(action,details_json,created_by,created_at) VALUES('merge',?,?,?)", (json.dumps({"source_id": int(source_id), "target_id": int(target_id), "source": str(source[0]), "target": str(target[0])}, ensure_ascii=False), actor, now))
    connection.commit()


def refresh_image_cache(connection: Any, image_id: int) -> dict[str, int]:
    """1画像分だけ検索キャッシュを更新する。タグ保存後の軽量更新用。"""
    init_schema(connection)
    register_sqlite_functions(connection)
    image_id = int(image_id)
    connection.execute("DELETE FROM tag_search_cache WHERE image_id=?", (image_id,))
    now = utc_now()
    connection.execute(
        """INSERT OR REPLACE INTO tag_search_cache(canonical_tag_id,image_id,source_priority,updated_at)
           SELECT a.canonical_tag_id,t.image_id,3,?
           FROM photo_manual_tags t JOIN tag_aliases a ON a.alias_key=tag_normalized_key(t.tag)
           JOIN tag_master m ON m.id=a.canonical_tag_id
           WHERE t.image_id=? AND m.status='approved' AND m.searchable=1""", (now, image_id)
    )
    connection.execute(
        """INSERT OR IGNORE INTO tag_search_cache(canonical_tag_id,image_id,source_priority,updated_at)
           SELECT a.canonical_tag_id,t.image_id,2,?
           FROM photo_ai_tags t JOIN tag_aliases a ON a.alias_key=tag_normalized_key(t.tag)
           JOIN tag_master m ON m.id=a.canonical_tag_id
           WHERE t.image_id=? AND m.status='approved' AND m.searchable=1
             AND t.confidence >= m.minimum_confidence
             AND NOT EXISTS(SELECT 1 FROM tag_rejections r WHERE r.image_id=t.image_id AND r.normalized_key=a.alias_key)""", (now, image_id)
    )
    return {"assignments": int(connection.execute("SELECT COUNT(*) FROM tag_search_cache WHERE image_id=?", (image_id,)).fetchone()[0])}
