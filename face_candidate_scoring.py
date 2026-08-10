"""Integrated local face-candidate scoring and safe learning helpers.

No external API calls are made here.  The module combines local face similarity
with local quality/history signals, records the reasons, and safely promotes
administrator-confirmed faces into the learning registry.
"""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from photo_database import get_connection

RAW_SIMILARITY_FLOOR = 0.62
INTEGRATED_CANDIDATE_THRESHOLD = 0.72
STRONG_THRESHOLD = 0.88
MEDIUM_THRESHOLD = 0.78
MAX_ACTIVE_REFERENCES_PER_PERSON = 600
MAX_REFERENCES_PER_BLOG = 24
MIN_LEARNING_QUALITY = 0.55


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_candidate_scoring_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_face_candidate_scores (
                face_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                face_similarity REAL NOT NULL DEFAULT 0,
                person_quality REAL NOT NULL DEFAULT 0,
                reference_count INTEGER NOT NULL DEFAULT 0,
                acceptance_rate REAL NOT NULL DEFAULT 0,
                author_match INTEGER NOT NULL DEFAULT 0,
                integrated_score REAL NOT NULL DEFAULT 0,
                confidence_band TEXT NOT NULL DEFAULT '',
                reason_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(face_id, person_id)
            );
            CREATE INDEX IF NOT EXISTS idx_face_candidate_scores_rank
              ON photo_face_candidate_scores(face_id, integrated_score DESC);
            CREATE INDEX IF NOT EXISTS idx_face_candidate_scores_person
              ON photo_face_candidate_scores(person_id, integrated_score DESC);
            """
        )
        con.commit()


def _table_exists(con: Any, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _person_metrics(person_id: int, person_name: str) -> dict[str, Any]:
    """Return local-only quality/history metrics for one person."""
    init_candidate_scoring_schema()
    with closing(get_connection()) as con:
        quality = 0.70
        if _table_exists(con, "phase3_person_quality"):
            row = con.execute(
                "SELECT quality_score FROM phase3_person_quality WHERE person_id=?",
                (int(person_id),),
            ).fetchone()
            if row:
                quality = max(0.0, min(float(row[0] or 0), 1.0))

        if _table_exists(con, "photo_face_learning_registry"):
            reference_count = int(con.execute(
                "SELECT COUNT(*) FROM photo_face_learning_registry WHERE person_id=? AND is_active=1",
                (int(person_id),),
            ).fetchone()[0] or 0)
        else:
            reference_count = int(con.execute(
                """SELECT COUNT(*) FROM photo_faces
                   WHERE confirmed_person_id=? AND TRIM(COALESCE(face_embedding,''))<>''
                     AND confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')""",
                (int(person_id),),
            ).fetchone()[0] or 0)

        acceptance_rate = 0.75
        accepted = corrected = 0
        if _table_exists(con, "photo_ai_decision_log"):
            row = con.execute(
                """SELECT
                       SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN decision='corrected' THEN 1 ELSE 0 END)
                   FROM photo_ai_decision_log
                   WHERE suggested_person=?""",
                (str(person_name),),
            ).fetchone()
            if row:
                accepted, corrected = int(row[0] or 0), int(row[1] or 0)
                total = accepted + corrected
                if total:
                    # Beta(2,1) smoothing prevents a tiny sample from dominating.
                    acceptance_rate = (accepted + 2.0) / (total + 3.0)

    return {
        "person_quality": quality,
        "reference_count": reference_count,
        "acceptance_rate": max(0.0, min(float(acceptance_rate), 1.0)),
        "accepted": accepted,
        "corrected": corrected,
    }


def score_candidate(
    *,
    face_id: int,
    person_id: int,
    person_name: str,
    face_similarity: float,
    blog_member_name: str = "",
) -> dict[str, Any]:
    """Combine similarity with conservative local quality/history signals."""
    metrics = _person_metrics(person_id, person_name)
    sim = max(0.0, min(float(face_similarity or 0), 1.0))
    quality = float(metrics["person_quality"])
    refs = int(metrics["reference_count"])
    acceptance = float(metrics["acceptance_rate"])
    ref_score = min(1.0, math.log1p(max(0, refs)) / math.log1p(120.0))
    author_match = bool(str(blog_member_name or "").strip() and str(blog_member_name).strip() == str(person_name).strip())

    # Similarity stays dominant.  Auxiliary signals can refine, not rescue, a poor match.
    integrated = (
        sim * 0.82
        + quality * 0.06
        + ref_score * 0.05
        + acceptance * 0.05
        + (1.0 if author_match else 0.0) * 0.02
    )
    if sim < RAW_SIMILARITY_FLOOR:
        integrated = min(integrated, INTEGRATED_CANDIDATE_THRESHOLD - 0.0001)
    integrated = max(0.0, min(integrated, 1.0))

    if integrated >= STRONG_THRESHOLD:
        band = "高信頼"
    elif integrated >= MEDIUM_THRESHOLD:
        band = "中信頼"
    elif integrated >= INTEGRATED_CANDIDATE_THRESHOLD:
        band = "要確認"
    else:
        band = "しきい値未満"

    result = {
        "face_id": int(face_id),
        "person_id": int(person_id),
        "person_name": str(person_name),
        "face_similarity": sim,
        "person_quality": quality,
        "reference_count": refs,
        "reference_score": ref_score,
        "acceptance_rate": acceptance,
        "author_match": author_match,
        "integrated_score": integrated,
        "confidence_band": band,
    }
    result["reason"] = (
        f"顔{sim*100:.1f}% / 人物品質{quality*100:.1f}% / "
        f"参照{refs}件 / 過去採用率{acceptance*100:.1f}% / "
        f"投稿者一致{'あり' if author_match else 'なし'}"
    )
    return result


def save_score_detail(detail: dict[str, Any]) -> None:
    init_candidate_scoring_schema()
    with closing(get_connection()) as con:
        con.execute(
            """INSERT INTO photo_face_candidate_scores(
                   face_id,person_id,face_similarity,person_quality,reference_count,
                   acceptance_rate,author_match,integrated_score,confidence_band,reason_json,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(face_id,person_id) DO UPDATE SET
                   face_similarity=excluded.face_similarity,
                   person_quality=excluded.person_quality,
                   reference_count=excluded.reference_count,
                   acceptance_rate=excluded.acceptance_rate,
                   author_match=excluded.author_match,
                   integrated_score=excluded.integrated_score,
                   confidence_band=excluded.confidence_band,
                   reason_json=excluded.reason_json,
                   updated_at=excluded.updated_at""",
            (
                int(detail["face_id"]), int(detail["person_id"]),
                float(detail["face_similarity"]), float(detail["person_quality"]),
                int(detail["reference_count"]), float(detail["acceptance_rate"]),
                1 if detail.get("author_match") else 0, float(detail["integrated_score"]),
                str(detail["confidence_band"]), json.dumps(detail, ensure_ascii=False), _now(),
            ),
        )
        con.commit()


def get_face_score_details(face_id: int) -> dict[int, dict[str, Any]]:
    init_candidate_scoring_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            "SELECT person_id,reason_json FROM photo_face_candidate_scores WHERE face_id=?",
            (int(face_id),),
        ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            result[int(row[0])] = json.loads(str(row[1] or "{}"))
        except Exception:
            result[int(row[0])] = {}
    return result


def _face_quality(face_id: int) -> tuple[float, str]:
    with closing(get_connection()) as con:
        row = con.execute(
            "SELECT detection_confidence,box_width,box_height FROM photo_faces WHERE id=?",
            (int(face_id),),
        ).fetchone()
    if not row:
        return 0.0, "顔データなし"
    detection = max(0.0, min(float(row[0] or 0), 1.0)) or 0.65
    width, height = max(0.0, float(row[1] or 0)), max(0.0, float(row[2] or 0))
    side = min(width, height)
    size = 0.65 if side <= 0 else 1.0 if side >= 96 else 0.8 if side >= 64 else 0.55 if side >= 40 else 0.25
    score = size * 0.55 + detection * 0.45
    return score, "A" if score >= 0.82 else "B" if score >= 0.62 else "C"


def register_confirmed_face_learning(face_id: int, person_id: int, *, source: str = "admin_confirmed") -> dict[str, Any]:
    """Safely add an administrator-confirmed face to the learning registry.

    Exact duplicates, low-quality faces, per-blog overrepresentation and excessive
    per-person references are rejected.  This does not call an external API.
    """
    from past_face_learning import init_past_face_learning_schema

    init_past_face_learning_schema()
    now = _now()
    with closing(get_connection()) as con:
        row = con.execute(
            """SELECT f.face_embedding,f.image_id,i.blog_id
               FROM photo_faces f JOIN photo_images i ON i.id=f.image_id
               WHERE f.id=? AND f.confirmed_person_id=?""",
            (int(face_id), int(person_id)),
        ).fetchone()
        if not row:
            return {"registered": False, "reason": "確定顔が見つかりません"}
        embedding = str(row[0] or "").strip()
        if not embedding:
            return {"registered": False, "reason": "顔特徴量なし"}
        image_id, blog_id = int(row[1]), int(row[2])
        digest = hashlib.sha256(embedding.encode("utf-8", errors="ignore")).hexdigest()
        quality, grade = _face_quality(face_id)
        if quality < MIN_LEARNING_QUALITY:
            con.execute(
                """INSERT INTO photo_face_learning_registry(
                       face_id,person_id,embedding_hash,quality_grade,quality_score,source,is_active,
                       registered_by,registered_at,updated_at)
                   VALUES(?,?,?,?,?,?,0,0,?,?)
                   ON CONFLICT(face_id) DO UPDATE SET person_id=excluded.person_id,
                       embedding_hash=excluded.embedding_hash,quality_grade=excluded.quality_grade,
                       quality_score=excluded.quality_score,source=excluded.source,is_active=0,
                       updated_at=excluded.updated_at""",
                (int(face_id), int(person_id), digest, grade, quality, source + "_low_quality", now, now),
            )
            con.commit()
            return {"registered": False, "reason": "品質不足", "quality": quality}

        duplicate = con.execute(
            "SELECT face_id FROM photo_face_learning_registry WHERE person_id=? AND embedding_hash=? AND is_active=1 AND face_id<>? LIMIT 1",
            (int(person_id), digest, int(face_id)),
        ).fetchone()
        if duplicate:
            return {"registered": False, "reason": "重複特徴量", "duplicate_face_id": int(duplicate[0])}

        active_count = int(con.execute(
            "SELECT COUNT(*) FROM photo_face_learning_registry WHERE person_id=? AND is_active=1",
            (int(person_id),),
        ).fetchone()[0] or 0)
        if active_count >= MAX_ACTIVE_REFERENCES_PER_PERSON:
            return {"registered": False, "reason": "人物ごとの参照上限", "active_count": active_count}

        same_blog = int(con.execute(
            """SELECT COUNT(*) FROM photo_face_learning_registry r
               JOIN photo_faces f ON f.id=r.face_id
               JOIN photo_images i ON i.id=f.image_id
               WHERE r.person_id=? AND r.is_active=1 AND i.blog_id=?""",
            (int(person_id), blog_id),
        ).fetchone()[0] or 0)
        if same_blog >= MAX_REFERENCES_PER_BLOG:
            return {"registered": False, "reason": "同一ブログの参照上限", "same_blog": same_blog}

        con.execute(
            """INSERT INTO photo_face_learning_registry(
                   face_id,person_id,embedding_hash,quality_grade,quality_score,source,is_active,
                   registered_by,registered_at,updated_at)
               VALUES(?,?,?,?,?,?,1,0,?,?)
               ON CONFLICT(face_id) DO UPDATE SET person_id=excluded.person_id,
                   embedding_hash=excluded.embedding_hash,quality_grade=excluded.quality_grade,
                   quality_score=excluded.quality_score,source=excluded.source,is_active=1,
                   updated_at=excluded.updated_at""",
            (int(face_id), int(person_id), digest, grade, quality, source, now, now),
        )
        con.commit()
    return {"registered": True, "reason": "学習参照へ反映", "quality": quality, "image_id": image_id}
