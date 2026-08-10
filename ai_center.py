from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import discord

from photo_database import get_connection, save_ai_tag
from embed_safety import safe_add_field


PROFILE_SAVER = "saver"
PROFILE_STANDARD = "standard"
PROFILE_ACCURACY = "accuracy"

PROFILE_LABELS = {
    PROFILE_SAVER: "💰 節約モード",
    PROFILE_STANDARD: "⚖️ 標準モード",
    PROFILE_ACCURACY: "🎯 高精度モード",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_phase3_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS phase3_ai_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                profile TEXT NOT NULL DEFAULT 'saver',
                local_face_threshold REAL NOT NULL DEFAULT 0.92,
                min_local_tags INTEGER NOT NULL DEFAULT 3,
                batch_api_enabled INTEGER NOT NULL DEFAULT 0,
                batch_size INTEGER NOT NULL DEFAULT 4,
                scheduled_enabled INTEGER NOT NULL DEFAULT 0,
                scheduled_hour INTEGER NOT NULL DEFAULT 3,
                scheduled_limit INTEGER NOT NULL DEFAULT 20,
                current_model TEXT NOT NULL DEFAULT '',
                current_prompt_version TEXT NOT NULL DEFAULT 'default',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_prompt_versions (
                version TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_model_registry (
                model_name TEXT PRIMARY KEY,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_local_decisions (
                image_id INTEGER PRIMARY KEY,
                image_hash TEXT NOT NULL DEFAULT '',
                local_face_person TEXT NOT NULL DEFAULT '',
                local_face_score REAL NOT NULL DEFAULT 0,
                local_tag_count INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_cache_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL DEFAULT 0,
                cache_kind TEXT NOT NULL,
                hit INTEGER NOT NULL DEFAULT 0,
                saved_api_call INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_tag_quality (
                tag TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                quality_score REAL NOT NULL DEFAULT 0,
                usage_count INTEGER NOT NULL DEFAULT 0,
                avg_confidence REAL NOT NULL DEFAULT 0,
                auto_approve_candidate INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(tag, category)
            );

            CREATE TABLE IF NOT EXISTS phase3_person_quality (
                person_id INTEGER PRIMARY KEY,
                reference_faces INTEGER NOT NULL DEFAULT 0,
                confirmed_faces INTEGER NOT NULL DEFAULT 0,
                avg_candidate_score REAL NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_photo_quality (
                image_id INTEGER PRIMARY KEY,
                quality_score REAL NOT NULL DEFAULT 0,
                resolution_score REAL NOT NULL DEFAULT 0,
                file_score REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_recommendations (
                image_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(image_id, person_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_schedule_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_kind TEXT NOT NULL DEFAULT 'scheduled',
                requested_limit INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_phase3_cache_events_time
              ON phase3_cache_events(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_phase3_tag_quality_score
              ON phase3_tag_quality(quality_score DESC);

            CREATE INDEX IF NOT EXISTS idx_phase3_recommend_score
              ON phase3_recommendations(score DESC);
            """
        )

        con.execute(
            """
            INSERT OR IGNORE INTO phase3_ai_settings(
                id,
                profile,
                local_face_threshold,
                min_local_tags,
                batch_api_enabled,
                batch_size,
                scheduled_enabled,
                scheduled_hour,
                scheduled_limit,
                current_model,
                current_prompt_version,
                updated_at
            )
            VALUES(
                1,
                'saver',
                0.92,
                3,
                0,
                4,
                0,
                3,
                20,
                '',
                'default',
                ?
            )
            """,
            (_now(),),
        )

        con.commit()


def get_settings() -> dict[str, Any]:
    init_phase3_schema()

    with closing(get_connection()) as con:
        row = con.execute(
            "SELECT * FROM phase3_ai_settings WHERE id=1"
        ).fetchone()

        return dict(row) if row else {}


def update_settings(**values: Any) -> dict[str, Any]:
    init_phase3_schema()

    allowed = {
        "profile",
        "local_face_threshold",
        "min_local_tags",
        "batch_api_enabled",
        "batch_size",
        "scheduled_enabled",
        "scheduled_hour",
        "scheduled_limit",
        "current_model",
        "current_prompt_version",
    }

    pairs = [
        (key, value)
        for key, value in values.items()
        if key in allowed
    ]

    if not pairs:
        return get_settings()

    sql = ",".join(
        f"{key}=?" for key, _ in pairs
    )

    with closing(get_connection()) as con:
        con.execute(
            f"""
            UPDATE phase3_ai_settings
            SET {sql},
                updated_at=?
            WHERE id=1
            """,
            tuple(value for _, value in pairs) + (_now(),),
        )

        con.commit()

    return get_settings()


def effective_model(default_model: str) -> str:
    settings = get_settings()

    return (
        str(
            settings.get("current_model")
            or default_model
        ).strip()
        or default_model
    )


def effective_prompt(
    default_prompt: str,
) -> tuple[str, str]:
    settings = get_settings()

    version = str(
        settings.get("current_prompt_version")
        or "default"
    )

    if version == "default":
        return default_prompt, "default"

    with closing(get_connection()) as con:
        row = con.execute(
            """
            SELECT prompt_text
            FROM phase3_prompt_versions
            WHERE version=?
            """,
            (version,),
        ).fetchone()

    if row:
        return str(row[0]), version

    return default_prompt, "default"


def _normalize_local_tag(text: str) -> str:
    return " ".join(
        str(text or "")
        .replace("　", " ")
        .split()
    ).strip()


def derive_local_tags(
    image_id: int,
) -> list[tuple[str, str, float]]:
    """
    画像自体をAIへ送らず、
    DBのブログ情報だけから確実なタグを作る。
    """

    with closing(get_connection()) as con:
        row = con.execute(
            """
            SELECT
                b.group_name,
                b.member_name,
                b.title,
                b.published_at
            FROM photo_images i
            JOIN photo_blogs b
              ON b.id=i.blog_id
            WHERE i.id=?
            """,
            (int(image_id),),
        ).fetchone()

    if not row:
        return []

    group, member, title, published = map(
        lambda value: _normalize_local_tag(value),
        row,
    )

    tags: list[
        tuple[str, str, float]
    ] = []

    if group:
        tags.append(
            (
                "group",
                group,
                1.0,
            )
        )

    if (
        member
        and member
        not in {
            "不明",
            "投稿者不明",
        }
    ):
        tags.append(
            (
                "poster",
                member,
                1.0,
            )
        )

    if (
        published
        and len(published) >= 7
    ):
        year = published[:4]
        month = published[5:7]

        if year.isdigit():
            tags.append(
                (
                    "date",
                    f"{year}年",
                    1.0,
                )
            )

        if month.isdigit():
            month_number = int(month)

            tags.append(
                (
                    "date",
                    f"{month_number}月",
                    1.0,
                )
            )

            if month_number in (
                12,
                1,
                2,
            ):
                season = "冬"

            elif month_number in (
                3,
                4,
                5,
            ):
                season = "春"

            elif month_number in (
                6,
                7,
                8,
            ):
                season = "夏"

            else:
                season = "秋"

            tags.append(
                (
                    "season",
                    season,
                    0.98,
                )
            )

    keyword_map = {
        "ライブ": "ライブ",
        "コンサート": "ライブ",
        "ツアー": "ライブ",
        "クリスマス": "クリスマス",
        "ハロウィン": "ハロウィン",
        "誕生日": "誕生日",
        "生誕": "誕生日",
        "浴衣": "浴衣",
    }

    for keyword, tag in keyword_map.items():
        if keyword in title:
            tags.append(
                (
                    "event",
                    tag,
                    0.95,
                )
            )

    output: list[
        tuple[str, str, float]
    ] = []

    seen: set[
        tuple[str, str]
    ] = set()

    for item in tags:
        key = (
            item[0],
            item[1],
        )

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


def persist_local_tags(
    image_id: int,
) -> int:
    tags = derive_local_tags(
        image_id
    )

    for (
        category,
        tag,
        confidence,
    ) in tags:

        save_ai_tag(
            image_id=image_id,
            tag=tag,
            confidence=confidence,
            category=category,
            model_name="local_rules_v1",
            raw_value=tag,
        )

    return len(tags)


def get_best_local_face(
    image_id: int,
) -> tuple[str, float]:
    """
    保存済みローカル顔候補のうち、
    画像内で最も高い候補を返す。
    """

    with closing(get_connection()) as con:
        row = con.execute(
            """
            SELECT
                p.person_name,
                MAX(c.confidence)
            FROM photo_faces f
            JOIN photo_face_candidates c
              ON c.face_id=f.id
            JOIN photo_people p
              ON p.id=c.person_id
            WHERE
                f.image_id=?
                AND COALESCE(
                    c.score_source,
                    'local_face'
                )
                IN (
                    'local_face',
                    'combined',
                    ''
                )
            GROUP BY
                p.id,
                p.person_name
            ORDER BY
                MAX(c.confidence) DESC
            LIMIT 1
            """,
            (int(image_id),),
        ).fetchone()

    if not row:
        return "", 0.0

    return (
        str(row[0]),
        float(row[1] or 0),
    )


def phase3_preflight(
    image_id: int,
    image_hash: str = "",
) -> dict[str, Any]:
    """
    APIの前にローカル情報を集め、
    節約モードならAPI省略可否を決める。
    """

    init_phase3_schema()

    settings = get_settings()

    profile = str(
        settings.get("profile")
        or PROFILE_SAVER
    )

    local_tag_count = persist_local_tags(
        image_id
    )

    (
        person,
        face_score,
    ) = get_best_local_face(
        image_id
    )

    threshold = float(
        settings.get(
            "local_face_threshold"
        )
        or 0.92
    )

    min_tags = int(
        settings.get(
            "min_local_tags"
        )
        or 3
    )

    local_face_complete = False
    reason = ""

    if (
        profile == PROFILE_SAVER
        and face_score >= threshold
        and local_tag_count >= min_tags
    ):
        local_face_complete = True
        reason = (
            "人物判定はローカルで十分: "
            f"顔候補{face_score:.3f}、ローカル補助タグ{local_tag_count}件。"
            "画像内容タグはOpenAIで生成します。"
        )

    decision = (
        "local_face_complete_tag_api"
        if local_face_complete
        else "api_needed"
    )

    now = _now()

    with closing(get_connection()) as con:
        con.execute(
            """
            INSERT INTO phase3_local_decisions(
                image_id,
                image_hash,
                local_face_person,
                local_face_score,
                local_tag_count,
                decision,
                reason,
                profile,
                created_at,
                updated_at
            )
            VALUES(
                ?,?,?,?,?,?,?,?,?,?
            )
            ON CONFLICT(image_id)
            DO UPDATE SET
                image_hash=
                    excluded.image_hash,
                local_face_person=
                    excluded.local_face_person,
                local_face_score=
                    excluded.local_face_score,
                local_tag_count=
                    excluded.local_tag_count,
                decision=
                    excluded.decision,
                reason=
                    excluded.reason,
                profile=
                    excluded.profile,
                updated_at=
                    excluded.updated_at
            """,
            (
                image_id,
                image_hash,
                person,
                face_score,
                local_tag_count,
                decision,
                reason,
                profile,
                now,
                now,
            ),
        )

        con.commit()

    return {
        "profile": profile,
        "local_tag_count":
            local_tag_count,
        "local_face_person":
            person,
        "local_face_score":
            face_score,
        # 互換キーは残すが、タグ生成を維持するため画像全体APIはスキップしない。
        "skip_api": False,
        "local_face_complete": local_face_complete,
        "tag_api_required": True,
        "reason": reason,
    }


def record_cache_event(
    image_id: int,
    cache_kind: str,
    hit: bool,
    saved_api_call: bool,
    note: str = "",
) -> None:
    init_phase3_schema()

    with closing(get_connection()) as con:
        con.execute(
            """
            INSERT INTO phase3_cache_events(
                image_id,
                cache_kind,
                hit,
                saved_api_call,
                note,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                int(image_id),
                str(cache_kind)[:40],
                1 if hit else 0,
                1 if saved_api_call else 0,
                str(note)[:500],
                _now(),
            ),
        )

        con.commit()


def cache_diagnostics() -> dict[str, Any]:
    init_phase3_schema()

    with closing(get_connection()) as con:
        row = con.execute(
            """
            SELECT
                COUNT(*),
                SUM(hit),
                SUM(saved_api_call)
            FROM phase3_cache_events
            """
        ).fetchone()

        local = con.execute(
            """
            SELECT
                COUNT(*),
                SUM(
                    CASE
                        WHEN decision IN ('local_complete','local_face_complete_tag_api')
                        THEN 1
                        ELSE 0
                    END
                )
            FROM phase3_local_decisions
            """
        ).fetchone()

    total = int(row[0] or 0)
    hits = int(row[1] or 0)
    saved = int(row[2] or 0)

    return {
        "events": total,
        "hits": hits,
        "saved_api_calls": saved,
        "hit_rate":
            hits * 100 / total
            if total
            else 0.0,
        "local_checked":
            int(local[0] or 0),
        "local_completed":
            int(local[1] or 0),
    }


def refresh_quality_scores() -> dict[str, int]:
    init_phase3_schema()

    now = _now()

    with closing(get_connection()) as con:
        tags = con.execute(
            """
            SELECT
                tag,
                category,
                COUNT(*) AS usage_count,
                AVG(confidence) AS avg_conf
            FROM photo_ai_tags
            GROUP BY
                tag,
                category
            """
        ).fetchall()

        for (
            tag,
            category,
            usage,
            avg_conf,
        ) in tags:

            usage = int(
                usage or 0
            )

            avg_conf = float(
                avg_conf or 0
            )

            frequency_component = (
                min(
                    1.0,
                    usage / 100.0,
                )
            )

            score = max(
                0.0,
                min(
                    1.0,
                    avg_conf * 0.8
                    + frequency_component
                    * 0.2,
                ),
            )

            candidate = (
                1
                if (
                    usage >= 100
                    and avg_conf >= 0.95
                )
                else 0
            )

            con.execute(
                """
                INSERT INTO phase3_tag_quality(
                    tag,
                    category,
                    quality_score,
                    usage_count,
                    avg_confidence,
                    auto_approve_candidate,
                    updated_at
                )
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(tag,category)
                DO UPDATE SET
                    quality_score=
                        excluded.quality_score,
                    usage_count=
                        excluded.usage_count,
                    avg_confidence=
                        excluded.avg_confidence,
                    auto_approve_candidate=
                        excluded.auto_approve_candidate,
                    updated_at=
                        excluded.updated_at
                """,
                (
                    str(tag),
                    str(category),
                    score,
                    usage,
                    avg_conf,
                    candidate,
                    now,
                ),
            )

        people = con.execute(
            """
            SELECT
                p.id,
                SUM(
                    CASE
                        WHEN f.confirmation_status
                        IN (
                            'manually_confirmed',
                            'auto_seeded'
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS confirmed,
                COUNT(f.id) AS refs,
                AVG(
                    COALESCE(
                        c.confidence,
                        0
                    )
                ) AS avg_score
            FROM photo_people p
            LEFT JOIN photo_faces f
              ON f.confirmed_person_id=p.id
            LEFT JOIN photo_face_candidates c
              ON
                  c.face_id=f.id
                  AND c.person_id=p.id
            GROUP BY p.id
            """
        ).fetchall()

        for (
            person_id,
            confirmed,
            refs,
            avg_score,
        ) in people:

            confirmed = int(
                confirmed or 0
            )

            refs = int(
                refs or 0
            )

            avg_score = float(
                avg_score or 0
            )

            data_component = min(
                1.0,
                confirmed / 50.0,
            )

            score = max(
                0.0,
                min(
                    1.0,
                    avg_score * 0.6
                    + data_component
                    * 0.4,
                ),
            )

            con.execute(
                """
                INSERT INTO phase3_person_quality(
                    person_id,
                    reference_faces,
                    confirmed_faces,
                    avg_candidate_score,
                    quality_score,
                    updated_at
                )
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(person_id)
                DO UPDATE SET
                    reference_faces=
                        excluded.reference_faces,
                    confirmed_faces=
                        excluded.confirmed_faces,
                    avg_candidate_score=
                        excluded.avg_candidate_score,
                    quality_score=
                        excluded.quality_score,
                    updated_at=
                        excluded.updated_at
                """,
                (
                    int(person_id),
                    refs,
                    confirmed,
                    avg_score,
                    score,
                    now,
                ),
            )

        images = con.execute(
            """
            SELECT
                id,
                width,
                height,
                file_size
            FROM photo_images
            WHERE download_status='completed'
            """
        ).fetchall()

        for (
            image_id,
            width,
            height,
            file_size,
        ) in images:

            width = int(
                width or 0
            )
            height = int(
                height or 0
            )
            file_size = int(
                file_size or 0
            )

            pixels = (
                width * height
            )

            resolution = (
                min(
                    1.0,
                    pixels
                    / 2_000_000.0,
                )
                if pixels
                else 0.0
            )

            file_score = (
                min(
                    1.0,
                    file_size
                    / 500_000.0,
                )
                if file_size
                else 0.0
            )

            score = (
                resolution * 0.75
                + file_score * 0.25
            )

            if score >= 0.8:
                note = "高解像度"

            elif score >= 0.5:
                note = "標準"

            else:
                note = "低品質候補"

            con.execute(
                """
                INSERT INTO phase3_photo_quality(
                    image_id,
                    quality_score,
                    resolution_score,
                    file_score,
                    note,
                    updated_at
                )
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(image_id)
                DO UPDATE SET
                    quality_score=
                        excluded.quality_score,
                    resolution_score=
                        excluded.resolution_score,
                    file_score=
                        excluded.file_score,
                    note=
                        excluded.note,
                    updated_at=
                        excluded.updated_at
                """,
                (
                    int(image_id),
                    score,
                    resolution,
                    file_score,
                    note,
                    now,
                ),
            )

        con.commit()

    # 品質再計算を検索へ即時反映。手動タグは常に優先し、AIタグだけ品質閾値を使う。
    try:
        from tag_master import rebuild_cache
        with closing(get_connection()) as cache_con:
            rebuild_cache(cache_con)
            cache_con.commit()
    except Exception as exc:
        print("タグ品質の検索キャッシュ反映に失敗:", type(exc).__name__, exc)

    return {
        "tags": len(tags),
        "people": len(people),
        "images": len(images),
    }


def rebuild_recommendations(
    limit: int = 5000,
) -> int:
    init_phase3_schema()

    now = _now()

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT
                q.image_id,
                q.quality_score,
                ip.person_id
            FROM phase3_photo_quality q
            LEFT JOIN photo_image_people ip
              ON
                  ip.image_id=q.image_id
                  AND ip.relation_status='confirmed'
            ORDER BY
                q.quality_score DESC
            LIMIT ?
            """,
            (
                max(
                    1,
                    min(
                        int(limit),
                        20000,
                    ),
                ),
            ),
        ).fetchall()

        con.execute(
            """
            DELETE FROM
                phase3_recommendations
            """
        )

        for (
            image_id,
            quality,
            person_id,
        ) in rows:

            score = float(
                quality or 0
            )

            con.execute(
                """
                INSERT OR REPLACE INTO
                phase3_recommendations(
                    image_id,
                    person_id,
                    score,
                    reason,
                    updated_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    int(image_id),
                    int(person_id or 0),
                    score,
                    "写真品質＋確定人物",
                    now,
                ),
            )

        con.commit()

    return len(rows)


def usage_prediction() -> dict[str, Any]:
    from ai_cost_control import (
        get_ai_cost_status,
    )

    status = (
        get_ai_cost_status()
    )

    with closing(get_connection()) as con:
        pending = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM photo_images
                WHERE
                    analysis_status='pending'
                    AND download_status='completed'
                """
            ).fetchone()[0]
            or 0
        )

        usage = con.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(
                    SUM(estimated_cost_usd),
                    0
                ),
                COALESCE(
                    AVG(estimated_cost_usd),
                    0
                ),
                COALESCE(
                    AVG(total_tokens),
                    0
                )
            FROM photo_ai_usage
            WHERE
                request_kind='api'
                AND status
                IN (
                    'completed',
                    'review'
                )
            """
        ).fetchone()

    calls = int(
        usage[0] or 0
    )

    total_cost = float(
        usage[1] or 0
    )

    avg_cost = float(
        usage[2] or 0
    )

    projected = (
        pending * avg_cost
        if avg_cost > 0
        else 0.0
    )

    return {
        "pending": pending,
        "historical_calls": calls,
        "historical_cost":
            total_cost,
        "avg_cost":
            avg_cost,
        "projected_remaining_cost":
            projected,
        "daily_remaining":
            int(
                status.get(
                    "daily_remaining",
                    0,
                )
            ),
        "monthly_remaining":
            int(
                status.get(
                    "monthly_remaining",
                    0,
                )
            ),
    }


def model_prompt_summary() -> dict[str, Any]:
    settings = get_settings()

    with closing(get_connection()) as con:
        models = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM phase3_model_registry
                WHERE is_enabled=1
                """
            ).fetchone()[0]
            or 0
        )

        prompts = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM phase3_prompt_versions
                """
            ).fetchone()[0]
            or 0
        )

    return {
        "settings": settings,
        "models": models,
        "prompts": prompts,
    }


def dashboard_embed() -> discord.Embed:
    init_phase3_schema()

    settings = get_settings()
    cache = cache_diagnostics()
    prediction = usage_prediction()

    with closing(get_connection()) as con:
        tag_quality = con.execute(
            """
            SELECT
                COUNT(*),
                SUM(auto_approve_candidate)
            FROM phase3_tag_quality
            """
        ).fetchone()

        person_quality = con.execute(
            """
            SELECT
                COUNT(*),
                AVG(quality_score)
            FROM phase3_person_quality
            """
        ).fetchone()

        photos = con.execute(
            """
            SELECT
                COUNT(*),
                AVG(quality_score)
            FROM phase3_photo_quality
            """
        ).fetchone()

        recommendations = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM phase3_recommendations
                """
            ).fetchone()[0]
            or 0
        )

    embed = discord.Embed(
        title="🧠 AI管理センター",
        color=0x5865F2,
    )

    embed.description = (
        "ローカル判定を先に使い、"
        "必要な画像だけOpenAIへ送る"
        "低コスト設計です。"
    )

    safe_add_field(
        embed,
        name="⚙️ プロファイル",
        value=PROFILE_LABELS.get(
            str(
                settings.get(
                    "profile"
                )
            ),
            str(
                settings.get(
                    "profile"
                )
            ),
        ),
        inline=True,
    )

    safe_add_field(
        embed,
        name="💾 キャッシュ",
        value=(
            f"命中率 **{cache['hit_rate']:.1f}%**\n"
            f"API回避 **{cache['saved_api_calls']:,}回**\n"
            f"ローカル完結 **{cache['local_completed']:,}件**"
        ),
        inline=True,
    )

    safe_add_field(
        embed,
        name="💰 API予測",
        value=(
            f"未解析 **{prediction['pending']:,}枚**\n"
            f"平均 **${prediction['avg_cost']:.6f}/枚**\n"
            f"残り推定 **${prediction['projected_remaining_cost']:.2f}**"
        ),
        inline=True,
    )

    safe_add_field(
        embed,
        name="🏷️ タグ品質",
        value=(
            f"評価済み **{int(tag_quality[0] or 0):,}**\n"
            f"自動承認候補 **{int(tag_quality[1] or 0):,}**"
        ),
        inline=True,
    )

    safe_add_field(
        embed,
        name="👤 人物品質",
        value=(
            f"評価済み **{int(person_quality[0] or 0):,}人**\n"
            f"平均 **{float(person_quality[1] or 0) * 100:.1f}%**"
        ),
        inline=True,
    )

    safe_add_field(
        embed,
        name="📷 写真品質",
        value=(
            f"評価済み **{int(photos[0] or 0):,}枚**\n"
            f"平均 **{float(photos[1] or 0) * 100:.1f}%**\n"
            f"おすすめ **{recommendations:,}件**"
        ),
        inline=True,
    )

    embed.set_footer(
        text=(
            "この画面の集計は"
            "OpenAI APIを呼びません。"
        )
    )

    return embed


class ProfileSelect(
    discord.ui.Select
):
    def __init__(
        self,
        owner_id: int,
    ):
        self.owner_id = int(
            owner_id
        )

        options = [
            discord.SelectOption(
                label=label.split(
                    " ",
                    1,
                )[-1],
                emoji=label.split(
                    " ",
                    1,
                )[0],
                value=value,
            )
            for (
                value,
                label,
            ) in PROFILE_LABELS.items()
        ]

        super().__init__(
            placeholder=(
                "AI設定プロファイルを選択"
            ),
            options=options,
        )

    async def callback(
        self,
        interaction:
            discord.Interaction,
    ) -> None:

        update_settings(
            profile=self.values[0]
        )

        await (
            interaction.response
            .edit_message(
                embed=dashboard_embed(),
                view=Phase3AIView(
                    self.owner_id
                ),
            )
        )


class Phase3AIView(
    discord.ui.View
):
    def __init__(
        self,
        owner_id: int,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(
            owner_id
        )

        self.add_item(
            ProfileSelect(
                owner_id
            )
        )

    async def interaction_check(
        self,
        interaction:
            discord.Interaction,
    ) -> bool:

        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await (
            interaction.response
            .send_message(
                "この画面は開いた管理者だけが操作できます。",
                ephemeral=True,
            )
        )

        return False

    @discord.ui.button(
        label="品質を再計算",
        emoji="🧪",
        style=
            discord.ButtonStyle.primary,
        row=1,
    )
    async def quality(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        await (
            interaction.response
            .defer(
                ephemeral=True
            )
        )

        result = await asyncio.to_thread(
            refresh_quality_scores
        )

        recommendations = (
            await asyncio.to_thread(
                rebuild_recommendations
            )
        )

        await (
            interaction.followup
            .send(
                (
                    "✅ 品質を再計算しました。\n"
                    f"タグ {result['tags']:,}\n"
                    f"人物 {result['people']:,}\n"
                    f"写真 {result['images']:,}\n"
                    f"おすすめ {recommendations:,}"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="API使用予測",
        emoji="💰",
        style=
            discord.ButtonStyle.secondary,
        row=1,
    )
    async def prediction(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        prediction = (
            await asyncio.to_thread(
                usage_prediction
            )
        )

        embed = discord.Embed(
            title="💰 API使用予測",
            color=0xFEE75C,
        )

        embed.description = (
            f"未解析 **{prediction['pending']:,}枚**\n"
            f"過去API解析 **{prediction['historical_calls']:,}回**\n"
            f"平均推定 **${prediction['avg_cost']:.6f}/枚**\n"
            f"全残りを同条件で解析した場合 "
            f"**約${prediction['projected_remaining_cost']:.2f}**\n"
            f"本日残り **{prediction['daily_remaining']:,}枚** "
            f"/ 今月残り **{prediction['monthly_remaining']:,}枚**"
        )

        embed.set_footer(
            text=(
                "過去実績からの単純推定です。"
                "実料金を保証するものではありません。"
            )
        )

        await (
            interaction.response
            .send_message(
                embed=embed,
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="キャッシュ診断",
        emoji="💾",
        style=
            discord.ButtonStyle.secondary,
        row=1,
    )
    async def cache(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        cache = (
            await asyncio.to_thread(
                cache_diagnostics
            )
        )

        await (
            interaction.response
            .send_message(
                (
                    "💾 キャッシュ診断\n"
                    f"イベント **{cache['events']:,}**\n"
                    f"命中 **{cache['hits']:,}**"
                    f"（{cache['hit_rate']:.1f}%）\n"
                    f"API回避 **{cache['saved_api_calls']:,}**\n"
                    f"ローカル判定 **{cache['local_checked']:,}** "
                    f"/ 完結 **{cache['local_completed']:,}**"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="モデル・プロンプト",
        emoji="🧾",
        style=
            discord.ButtonStyle.secondary,
        row=2,
    )
    async def model_prompt(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        data = (
            await asyncio.to_thread(
                model_prompt_summary
            )
        )

        settings = data[
            "settings"
        ]

        await (
            interaction.response
            .send_message(
                (
                    "🧾 モデル・プロンプト管理\n"
                    f"現在モデル: "
                    f"`{settings.get('current_model') or '環境変数の既定モデル'}`\n"
                    f"Prompt: "
                    f"`{settings.get('current_prompt_version') or 'default'}`\n"
                    f"登録モデル **{data['models']}** "
                    f"/ Prompt版 **{data['prompts']}**\n"
                    "変更はDBへ履歴を残す設計です。"
                    "未登録モデルへ勝手に切り替えません。"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="解析予約設定",
        emoji="🕒",
        style=
            discord.ButtonStyle.secondary,
        row=2,
    )
    async def schedule(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        settings = get_settings()

        await (
            interaction.response
            .send_message(
                (
                    "🕒 解析予約\n"
                    f"状態 **{'ON' if int(settings.get('scheduled_enabled', 0)) else 'OFF'}**\n"
                    f"時刻 **{int(settings.get('scheduled_hour', 3)):02d}:00**\n"
                    f"上限 **{int(settings.get('scheduled_limit', 20))}枚/回**\n"
                    "安全のため初期状態はOFFです。"
                    "日次/月次API上限は常に優先されます。"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="更新",
        emoji="♻️",
        style=
            discord.ButtonStyle.secondary,
        row=2,
    )
    async def refresh(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:

        await (
            interaction.response
            .edit_message(
                embed=dashboard_embed(),
                view=Phase3AIView(
                    self.owner_id
                ),
            )
        )


async def send_phase3_ai_center(
    interaction:
        discord.Interaction,
) -> None:

    init_phase3_schema()

    if (
        interaction.response
        .is_done()
    ):
        await (
            interaction.followup
            .send(
                embed=dashboard_embed(),
                view=Phase3AIView(
                    interaction.user.id
                ),
                ephemeral=True,
            )
        )

    else:
        await (
            interaction.response
            .send_message(
                embed=dashboard_embed(),
                view=Phase3AIView(
                    interaction.user.id
                ),
                ephemeral=True,
            )
        )


# -------------------------
# AI 詳細管理
# -------------------------

def tag_quality_rows(
    limit: int = 25,
) -> list[
    dict[str, Any]
]:
    init_phase3_schema()

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT
                tag,
                category,
                quality_score,
                usage_count,
                avg_confidence,
                auto_approve_candidate
            FROM phase3_tag_quality
            ORDER BY
                quality_score DESC,
                usage_count DESC
            LIMIT ?
            """,
            (
                max(
                    1,
                    min(
                        int(limit),
                        100,
                    ),
                ),
            ),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def person_quality_rows(
    limit: int = 25,
) -> list[
    dict[str, Any]
]:
    init_phase3_schema()

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT
                p.person_name,
                q.reference_faces,
                q.confirmed_faces,
                q.avg_candidate_score,
                q.quality_score
            FROM phase3_person_quality q
            JOIN photo_people p
              ON p.id=q.person_id
            ORDER BY
                q.quality_score DESC,
                q.confirmed_faces DESC
            LIMIT ?
            """,
            (
                max(
                    1,
                    min(
                        int(limit),
                        100,
                    ),
                ),
            ),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def recommendation_rows(
    limit: int = 25,
) -> list[
    dict[str, Any]
]:
    init_phase3_schema()

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT
                r.image_id,
                r.score,
                r.reason,
                p.person_name,
                b.group_name,
                b.member_name,
                b.title
            FROM phase3_recommendations r
            LEFT JOIN photo_people p
              ON p.id=r.person_id
            JOIN photo_images i
              ON i.id=r.image_id
            JOIN photo_blogs b
              ON b.id=i.blog_id
            ORDER BY
                r.score DESC,
                r.image_id DESC
            LIMIT ?
            """,
            (
                max(
                    1,
                    min(
                        int(limit),
                        100,
                    ),
                ),
            ),
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def learning_cleanup_diagnostics() -> dict[str, int]:
    """
    削除はせず、
    整理候補だけ数える。
    """

    with closing(get_connection()) as con:
        low_quality = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM photo_faces f
                LEFT JOIN phase3_photo_quality q
                  ON q.image_id=f.image_id
                WHERE
                    f.confirmed_person_id
                    IS NOT NULL
                    AND COALESCE(
                        q.quality_score,
                        0
                    ) < 0.35
                """
            ).fetchone()[0]
            or 0
        )

        no_embedding = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM photo_faces
                WHERE
                    confirmed_person_id
                    IS NOT NULL
                    AND TRIM(
                        COALESCE(
                            face_embedding,
                            ''
                        )
                    )=''
                """
            ).fetchone()[0]
            or 0
        )

        heavy_people = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT
                        confirmed_person_id,
                        COUNT(*) AS count
                    FROM photo_faces
                    WHERE
                        confirmed_person_id
                        IS NOT NULL
                    GROUP BY
                        confirmed_person_id
                    HAVING count > 1000
                )
                """
            ).fetchone()[0]
            or 0
        )

    return {
        "low_quality":
            low_quality,
        "no_embedding":
            no_embedding,
        "heavy_people":
            heavy_people,
    }


def tag_suggestions(
    query: str,
    limit: int = 20,
) -> list[
    tuple[str, int]
]:
    query = str(
        query or ""
    ).strip()

    if not query:
        return []

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT
                tag,
                COUNT(*) AS count
            FROM photo_ai_tags
            WHERE tag LIKE ?
            GROUP BY tag
            ORDER BY
                count DESC,
                tag
            LIMIT ?
            """,
            (
                f"%{query}%",
                max(
                    1,
                    min(
                        int(limit),
                        50,
                    ),
                ),
            ),
        ).fetchall()

    return [
        (
            str(row[0]),
            int(row[1] or 0),
        )
        for row in rows
    ]


def similar_tags(
    tag: str,
    limit: int = 15,
) -> list[
    tuple[str, float]
]:
    from difflib import (
        SequenceMatcher,
    )

    source = str(
        tag or ""
    ).strip()

    if not source:
        return []

    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT canonical_tag
            FROM tag_master
            WHERE canonical_tag<>?
            ORDER BY canonical_tag
            LIMIT 5000
            """,
            (source,),
        ).fetchall()

    scored: list[
        tuple[str, float]
    ] = []

    for row in rows:
        candidate = str(
            row[0]
        )

        score = (
            SequenceMatcher(
                None,
                source,
                candidate,
            )
            .ratio()
        )

        if score >= 0.45:
            scored.append(
                (
                    candidate,
                    score,
                )
            )

    scored.sort(
        key=lambda item: (
            -item[1],
            item[0],
        )
    )

    return scored[
        :max(
            1,
            min(
                int(limit),
                25,
            ),
        )
    ]


class TagSearchModal(
    discord.ui.Modal,
    title="タグ候補・類似タグ検索",
):
    query = discord.ui.TextInput(
        label="タグまたは一部文字",
        placeholder="例：笑顔 / 制服",
        max_length=80,
    )

    async def on_submit(
        self,
        interaction:
            discord.Interaction,
    ) -> None:

        query = str(
            self.query.value
        ).strip()

        suggestions = (
            await asyncio.to_thread(
                tag_suggestions,
                query,
                20,
            )
        )

        similars = (
            await asyncio.to_thread(
                similar_tags,
                query,
                15,
            )
        )

        embed = discord.Embed(
            title=(
                f"🔎 タグ検索: {query}"
            ),
            color=0x3498DB,
        )

        safe_add_field(
            embed,
            name="部分一致",
            value=(
                "\n".join(
                    f"・{tag}（{count:,}件）"
                    for (
                        tag,
                        count,
                    ) in suggestions
                )
                or "なし"
            ),
            inline=False,
        )

        safe_add_field(
            embed,
            name="類似候補",
            value=(
                "\n".join(
                    f"・{tag}（{score * 100:.1f}%）"
                    for (
                        tag,
                        score,
                    ) in similars
                )
                or "なし"
            ),
            inline=False,
        )

        await (
            interaction.response
            .send_message(
                embed=embed,
                ephemeral=True,
            )
        )


class PromptRegisterModal(
    discord.ui.Modal,
    title="プロンプト版を登録",
):
    version = discord.ui.TextInput(
        label="バージョン名",
        placeholder="例: v6",
        max_length=40,
    )

    prompt = discord.ui.TextInput(
        label="システムプロンプト",
        style=
            discord.TextStyle.paragraph,
        max_length=4000,
    )

    async def on_submit(
        self,
        interaction:
            discord.Interaction,
    ) -> None:

        version = str(
            self.version.value
        ).strip()

        prompt = str(
            self.prompt.value
        ).strip()

        if (
            not version
            or not prompt
        ):
            await (
                interaction.response
                .send_message(
                    "バージョン名とプロンプトが必要です。",
                    ephemeral=True,
                )
            )
            return

        init_phase3_schema()

        with closing(get_connection()) as con:
            con.execute(
                """
                INSERT INTO phase3_prompt_versions(
                    version,
                    prompt_text,
                    is_active,
                    created_by,
                    created_at
                )
                VALUES(
                    ?,?,
                    0,
                    ?,
                    ?
                )
                ON CONFLICT(version)
                DO UPDATE SET
                    prompt_text=
                        excluded.prompt_text,
                    created_by=
                        excluded.created_by
                """,
                (
                    version,
                    prompt,
                    str(
                        interaction.user.id
                    ),
                    _now(),
                ),
            )

            con.commit()

        await (
            interaction.response
            .send_message(
                (
                    f"✅ Prompt `{version}` を保存しました。"
                    "切替は設定から行います。"
                ),
                ephemeral=True,
            )
        )


class Phase3DetailsView(
    discord.ui.View
):
    def __init__(
        self,
        owner_id: int,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(
            owner_id
        )

    async def interaction_check(
        self,
        interaction:
            discord.Interaction,
    ) -> bool:

        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await (
            interaction.response
            .send_message(
                "この画面は開いた管理者だけが操作できます。",
                ephemeral=True,
            )
        )

        return False

    @discord.ui.button(
        label="タグ品質",
        emoji="🏷️",
        style=
            discord.ButtonStyle.primary,
    )
    async def tags(
        self,
        interaction,
        _,
    ):
        rows = (
            await asyncio.to_thread(
                tag_quality_rows,
                25,
            )
        )

        lines = [
            (
                f"**{row['tag']}** "
                f"/ {row['category']} "
                f"— {row['quality_score'] * 100:.1f}%"
                f"（{row['usage_count']:,}件）"
                + (
                    " ⭐承認候補"
                    if row[
                        "auto_approve_candidate"
                    ]
                    else ""
                )
            )
            for row in rows
        ]

        await (
            interaction.response
            .send_message(
                embed=discord.Embed(
                    title="🏷️ タグ品質",
                    description=(
                        "\n".join(lines)
                        or
                        "品質計算を先に実行してください。"
                    ),
                    color=0xF1C40F,
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="人物品質",
        emoji="👤",
        style=
            discord.ButtonStyle.primary,
    )
    async def people(
        self,
        interaction,
        _,
    ):
        rows = (
            await asyncio.to_thread(
                person_quality_rows,
                25,
            )
        )

        lines = [
            (
                f"**{row['person_name']}** "
                f"— 品質{row['quality_score'] * 100:.1f}% "
                f"/ 確定顔{row['confirmed_faces']:,} "
                f"/ 参照{row['reference_faces']:,}"
            )
            for row in rows
        ]

        await (
            interaction.response
            .send_message(
                embed=discord.Embed(
                    title="👤 人物品質",
                    description=(
                        "\n".join(lines)
                        or
                        "品質計算を先に実行してください。"
                    ),
                    color=0xEB459E,
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="おすすめ写真",
        emoji="🌟",
        style=
            discord.ButtonStyle.secondary,
    )
    async def recs(
        self,
        interaction,
        _,
    ):
        rows = (
            await asyncio.to_thread(
                recommendation_rows,
                25,
            )
        )

        lines = [
            (
                f"画像`{row['image_id']}` "
                f"{row.get('person_name') or row.get('member_name') or '人物未指定'} "
                f"— {row['score'] * 100:.1f}%"
            )
            for row in rows
        ]

        await (
            interaction.response
            .send_message(
                embed=discord.Embed(
                    title="🌟 おすすめ写真",
                    description=(
                        "\n".join(lines)
                        or
                        "品質再計算を先に実行してください。"
                    ),
                    color=0x57F287,
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="学習整理診断",
        emoji="🧹",
        style=
            discord.ButtonStyle.secondary,
    )
    async def cleanup(
        self,
        interaction,
        _,
    ):
        diagnostics = (
            await asyncio.to_thread(
                learning_cleanup_diagnostics
            )
        )

        await (
            interaction.response
            .send_message(
                (
                    "🧹 学習データ整理候補\n"
                    f"低品質候補 **{diagnostics['low_quality']:,}**\n"
                    f"特徴量なし **{diagnostics['no_embedding']:,}**\n"
                    f"参照顔1000件超の人物 **{diagnostics['heavy_people']:,}**\n"
                    "※この診断は削除しません。"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="タグ検索",
        emoji="🔎",
        style=
            discord.ButtonStyle.secondary,
    )
    async def tag_search(
        self,
        interaction,
        _,
    ):
        await (
            interaction.response
            .send_modal(
                TagSearchModal()
            )
        )

    @discord.ui.button(
        label="Prompt登録",
        emoji="📝",
        style=
            discord.ButtonStyle.secondary,
        row=1,
    )
    async def prompt_add(
        self,
        interaction,
        _,
    ):
        await (
            interaction.response
            .send_modal(
                PromptRegisterModal()
            )
        )


# 既存Viewへ「詳細」ボタンを追加するための派生View
class Phase3AIViewFull(
    Phase3AIView
):
    @discord.ui.button(
        label="品質・おすすめ詳細",
        emoji="📚",
        style=
            discord.ButtonStyle.success,
        row=3,
    )
    async def details(
        self,
        interaction,
        _,
    ):
        await (
            interaction.response
            .send_message(
                "見たい項目を選んでください。",
                view=Phase3DetailsView(
                    self.owner_id
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="顔候補診断",
        emoji="🔍",
        style=
            discord.ButtonStyle.secondary,
        row=3,
    )
    async def face_diagnostics(
        self,
        interaction,
        _,
    ):
        # Discordへ最優先で応答する。
        # face_candidate_diagnostics / local_face_recognition のimportやDB処理が
        # 遅くても「アプリケーションが応答しませんでした」にならないようにする。
        if not interaction.response.is_done():
            await interaction.response.defer(
                ephemeral=True,
                thinking=True,
            )

        try:
            from face_candidate_diagnostics import (
                send_face_candidate_diagnostics,
            )

            await send_face_candidate_diagnostics(
                interaction
            )
        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ 顔候補診断画面を開けませんでした。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )

    @discord.ui.button(
        label="解析予約ON/OFF",
        emoji="⏰",
        style=
            discord.ButtonStyle.secondary,
        row=3,
    )
    async def toggle_schedule(
        self,
        interaction,
        _,
    ):
        settings = get_settings()

        new_value = (
            0
            if int(
                settings.get(
                    "scheduled_enabled",
                    0,
                )
            )
            else 1
        )

        update_settings(
            scheduled_enabled=
                new_value
        )

        await (
            interaction.response
            .edit_message(
                embed=dashboard_embed(),
                view=Phase3AIViewFull(
                    self.owner_id
                ),
            )
        )


async def send_phase3_ai_center_full(
    interaction:
        discord.Interaction,
) -> None:

    init_phase3_schema()

    view = Phase3AIViewFull(
        interaction.user.id
    )

    if (
        interaction.response
        .is_done()
    ):
        await (
            interaction.followup
            .send(
                embed=dashboard_embed(),
                view=view,
                ephemeral=True,
            )
        )

    else:
        await (
            interaction.response
            .send_message(
                embed=dashboard_embed(),
                view=view,
                ephemeral=True,
            )
        )


# -------------------------
# 解析予約ワーカー
# -------------------------

_SCHEDULE_TASK: (
    asyncio.Task
    | None
) = None


async def _scheduled_loop() -> None:
    from zoneinfo import (
        ZoneInfo,
    )

    last_day = ""

    while True:
        try:
            settings = (
                get_settings()
            )

            if int(
                settings.get(
                    "scheduled_enabled",
                    0,
                )
            ):
                now = datetime.now(
                    ZoneInfo(
                        "Asia/Tokyo"
                    )
                )

                hour = int(
                    settings.get(
                        "scheduled_hour",
                        3,
                    )
                    or 3
                )

                day = now.strftime(
                    "%Y-%m-%d"
                )

                if (
                    now.hour == hour
                    and day != last_day
                ):
                    from photo_ai_analyzer import (
                        analyze_pending_images,
                    )

                    limit = max(
                        1,
                        min(
                            int(
                                settings.get(
                                    "scheduled_limit",
                                    20,
                                )
                                or 20
                            ),
                            500,
                        ),
                    )

                    result = (
                        await analyze_pending_images(
                            limit,
                            manual_api=True,
                        )
                    )

                    with closing(
                        get_connection()
                    ) as con:
                        con.execute(
                            """
                            INSERT INTO phase3_schedule_runs(
                                run_kind,
                                requested_limit,
                                result_json,
                                created_at
                            )
                            VALUES(
                                'scheduled',
                                ?,
                                ?,
                                ?
                            )
                            """,
                            (
                                limit,
                                json.dumps(
                                    result,
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                _now(),
                            ),
                        )

                        con.commit()

                    last_day = day

            await asyncio.sleep(
                300
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print(
                "AI解析予約ワーカーエラー:",
                type(exc).__name__,
                exc,
            )

            await asyncio.sleep(
                300
            )


def start_phase3_schedule_worker() -> None:
    global _SCHEDULE_TASK

    if (
        _SCHEDULE_TASK is None
        or _SCHEDULE_TASK.done()
    ):
        _SCHEDULE_TASK = (
            asyncio.create_task(
                _scheduled_loop(),
                name=(
                    "phase3-ai-schedule"
                ),
            )
        )
