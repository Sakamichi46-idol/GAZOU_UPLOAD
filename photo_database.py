import json
import os
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from photo_search_tags import searchable_aliases


# =========================
# データベース設定
# =========================

# RailwayでVolumeを使う場合は /data に保存する。
# ローカル実行などで /data が使えない場合は、
# プロジェクト内の data フォルダへ保存する。
RAILWAY_DATA_DIR = "/data"

LOCAL_DATA_DIR = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "data",
)


def get_data_dir() -> str:
    """
    写真検索用データの保存先を返す。

    Railway:
        /data

    ローカル:
        ./data
    """

    if os.path.isdir(
        RAILWAY_DATA_DIR
    ):

        return RAILWAY_DATA_DIR

    os.makedirs(
        LOCAL_DATA_DIR,
        exist_ok=True,
    )

    return LOCAL_DATA_DIR


DATA_DIR = get_data_dir()

PHOTO_DB_PATH = os.getenv(
    "PHOTO_DB_PATH",
    os.path.join(
        DATA_DIR,
        "photo_archive.db",
    ),
)


# =========================
# 共通処理
# =========================

def utc_now_text() -> str:
    """
    現在時刻をUTCのISO形式で返す。
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


def get_connection() -> sqlite3.Connection:
    """
    SQLite接続を作成する。
    """

    from db_runtime import connect
    return connect(PHOTO_DB_PATH)




def add_photo_favorite(
    image_id: int,
    discord_user_id: str | int,
) -> bool:
    """画像をユーザーのお気に入りへ追加する。

    新規登録できた場合はTrue、すでに登録済みの場合はFalseを返す。
    """
    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO photo_favorites (
                image_id,
                discord_user_id,
                created_at
            ) VALUES (?, ?, ?)
            """,
            (
                int(image_id),
                str(discord_user_id),
                utc_now_text(),
            ),
        )
        connection.commit()
        return cursor.rowcount > 0


def row_to_dict(
    row: sqlite3.Row | None,
) -> dict[str, Any] | None:
    """
    sqlite3.Rowを辞書へ変換する。
    """

    if row is None:

        return None

    return dict(
        row
    )


def rows_to_dicts(
    rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    """
    sqlite3.Rowの一覧を辞書一覧へ変換する。
    """

    return [
        dict(row)
        for row in rows
    ]


def clamp_confidence(
    confidence: float,
) -> float:
    """
    信頼度を0.0から1.0の範囲に収める。
    """

    return max(
        0.0,
        min(
            float(confidence),
            1.0,
        ),
    )


def ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    """
    既存テーブルに不足列がある場合だけ追加する。

    CREATE TABLE IF NOT EXISTSでは、
    すでに存在するテーブルへ新しい列は追加されないため、
    Railway Volume上の既存DBを安全に更新する目的で使用する。
    """

    columns = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    existing_names = {
        str(row["name"])
        for row in columns
    }

    if column_name in existing_names:

        return

    connection.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN {column_name} {column_definition}
        """
    )


# =========================
# DB初期化
# =========================

def init_photo_db() -> None:
    """
    写真検索用DBと必要なテーブルを作成する。

    既存のphoto_archive.dbが存在する場合でも、
    データを削除せず不足テーブル・不足列だけ追加する。
    """

    os.makedirs(
        os.path.dirname(
            PHOTO_DB_PATH
        ),
        exist_ok=True,
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        # -------------------------
        # ブログ記事
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_blogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                blog_url TEXT NOT NULL UNIQUE,

                group_name TEXT NOT NULL DEFAULT '',
                member_name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # -------------------------
        # 画像
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                blog_id INTEGER NOT NULL,

                image_url TEXT NOT NULL,
                image_index INTEGER NOT NULL DEFAULT 0,

                local_path TEXT NOT NULL DEFAULT '',
                file_name TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',

                file_size INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,

                image_hash TEXT NOT NULL DEFAULT '',

                download_status TEXT NOT NULL DEFAULT 'pending',
                download_error TEXT NOT NULL DEFAULT '',

                analysis_status TEXT NOT NULL DEFAULT 'pending',
                analysis_error TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(blog_id, image_url),

                FOREIGN KEY(blog_id)
                    REFERENCES photo_blogs(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # AIタグ
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL,

                category TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,

                model_name TEXT NOT NULL DEFAULT '',
                raw_value TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(image_id, category, tag),

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )


        # -------------------------
        # AI API使用量・推定料金
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER,
                source_image_id INTEGER,

                model_name TEXT NOT NULL DEFAULT '',
                request_kind TEXT NOT NULL DEFAULT 'api',
                status TEXT NOT NULL DEFAULT 'completed',

                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,

                input_cost_usd REAL NOT NULL DEFAULT 0,
                cached_input_cost_usd REAL NOT NULL DEFAULT 0,
                output_cost_usd REAL NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,

                response_id TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE SET NULL,

                FOREIGN KEY(source_image_id)
                    REFERENCES photo_images(id)
                    ON DELETE SET NULL
            )
            """
        )

        # -------------------------
        # 人間が追加・修正したタグ
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_manual_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL,

                category TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL,

                created_by TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(image_id, category, tag),

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # AIの画像解析結果
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL UNIQUE,

                model_name TEXT NOT NULL DEFAULT '',
                raw_response TEXT NOT NULL DEFAULT '',

                person_name TEXT NOT NULL DEFAULT '',
                clothing TEXT NOT NULL DEFAULT '',
                expression TEXT NOT NULL DEFAULT '',
                background TEXT NOT NULL DEFAULT '',
                pose TEXT NOT NULL DEFAULT '',
                objects TEXT NOT NULL DEFAULT '',

                person_count INTEGER NOT NULL DEFAULT 0,

                overall_confidence REAL NOT NULL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # 画像単位の確認待ち
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL UNIQUE,

                review_type TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                candidates TEXT NOT NULL DEFAULT '',

                status TEXT NOT NULL DEFAULT 'pending',

                reviewed_by TEXT NOT NULL DEFAULT '',
                selected_value TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT '',

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # 画像に写っている人物
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_image_people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL,
                person_name TEXT NOT NULL,
                relation_status TEXT NOT NULL DEFAULT 'candidate',
                source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                confirmed_by TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(image_id, person_name),
                FOREIGN KEY(image_id) REFERENCES photo_images(id) ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # お気に入り
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL,
                discord_user_id TEXT NOT NULL,

                created_at TEXT NOT NULL,

                UNIQUE(image_id, discord_user_id),

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )

        # =========================
        # 人物確認用テーブル
        # =========================

        # -------------------------
        # 人物マスター
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                person_name TEXT NOT NULL UNIQUE,
                group_name TEXT NOT NULL DEFAULT '',
                generation_name TEXT NOT NULL DEFAULT '',

                is_active INTEGER NOT NULL DEFAULT 1,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # -------------------------
        # 画像内で検出された顔
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                image_id INTEGER NOT NULL,
                face_index INTEGER NOT NULL DEFAULT 0,

                box_x REAL NOT NULL DEFAULT 0,
                box_y REAL NOT NULL DEFAULT 0,
                box_width REAL NOT NULL DEFAULT 0,
                box_height REAL NOT NULL DEFAULT 0,

                detection_confidence REAL NOT NULL DEFAULT 0,

                confirmed_person_id INTEGER,

                confirmation_status TEXT
                    NOT NULL DEFAULT 'unconfirmed',

                confirmed_by TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',

                model_name TEXT NOT NULL DEFAULT '',
                face_embedding TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(image_id, face_index),

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(confirmed_person_id)
                    REFERENCES photo_people(id)
                    ON DELETE SET NULL
            )
            """
        )

        # -------------------------
        # 画像単位の顔スキャン履歴
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_face_scans (
                image_id INTEGER PRIMARY KEY,

                status TEXT NOT NULL DEFAULT 'pending',
                detected_faces INTEGER NOT NULL DEFAULT 0,
                auto_confirmed_faces INTEGER NOT NULL DEFAULT 0,

                model_name TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',

                scanned_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY(image_id)
                    REFERENCES photo_images(id)
                    ON DELETE CASCADE
            )
            """
        )

        # Phase 6初期版で既に顔が保存されている画像は、
        # 完了済みとして安全に引き継ぐ。顔が0件だった過去画像だけは
        # 履歴が存在しないため、次回バッチで一度だけ再スキャンされる。
        now = utc_now_text()
        cursor.execute(
            """
            INSERT OR IGNORE INTO photo_face_scans (
                image_id, status, detected_faces, auto_confirmed_faces,
                model_name, error_message, scanned_at, created_at, updated_at
            )
            SELECT
                photo_faces.image_id,
                'completed',
                COUNT(photo_faces.id),
                SUM(CASE WHEN photo_faces.confirmation_status = 'auto_seeded' THEN 1 ELSE 0 END),
                MAX(photo_faces.model_name),
                '',
                ?, ?, ?
            FROM photo_faces
            GROUP BY photo_faces.image_id
            """,
            (now, now, now),
        )

        # -------------------------
        # 顔ごとの人物候補
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_face_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                face_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,

                confidence REAL NOT NULL DEFAULT 0,
                candidate_rank INTEGER NOT NULL DEFAULT 0,

                model_name TEXT NOT NULL DEFAULT '',
                raw_value TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(face_id, person_id),

                FOREIGN KEY(face_id)
                    REFERENCES photo_faces(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(person_id)
                    REFERENCES photo_people(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------
        # 顔単位の確認待ち
        # -------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_face_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                face_id INTEGER NOT NULL UNIQUE,

                question TEXT NOT NULL DEFAULT '',
                candidates TEXT NOT NULL DEFAULT '',

                status TEXT NOT NULL DEFAULT 'pending',

                selected_person_id INTEGER,

                reviewed_by TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT '',

                FOREIGN KEY(face_id)
                    REFERENCES photo_faces(id)
                    ON DELETE CASCADE,

                FOREIGN KEY(selected_person_id)
                    REFERENCES photo_people(id)
                    ON DELETE SET NULL
            )
            """
        )

        # =========================
        # 既存DB向けマイグレーション
        # =========================

        ensure_column(
            connection,
            "photo_images",
            "download_error",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_images",
            "bucket_key",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_images",
            "bucket_status",
            "TEXT NOT NULL DEFAULT 'pending'",
        )

        ensure_column(
            connection,
            "photo_images",
            "bucket_error",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_images",
            "storage_backend",
            "TEXT NOT NULL DEFAULT 'local'",
        )

        ensure_column(
            connection,
            "photo_blogs",
            "is_hidden",
            "INTEGER NOT NULL DEFAULT 0",
        )

        ensure_column(
            connection,
            "photo_blogs",
            "hidden_reason",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_blogs",
            "hidden_note",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_blogs",
            "hidden_at",
            "TEXT NOT NULL DEFAULT ''",
        )

        ensure_column(
            connection,
            "photo_blogs",
            "hidden_by",
            "TEXT NOT NULL DEFAULT ''",
        )

        # 日向坂46で投稿者情報が欠けている既存レコードは、削除せず除外へ移す。
        connection.execute(
            """
            UPDATE photo_blogs
            SET is_hidden = 1,
                hidden_reason = CASE WHEN TRIM(COALESCE(hidden_reason, '')) = ''
                                     THEN 'UNKNOWN_MEMBER' ELSE hidden_reason END,
                hidden_note = CASE WHEN TRIM(COALESCE(hidden_note, '')) = ''
                                   THEN '日向坂46の投稿者不明レコードを自動除外' ELSE hidden_note END,
                hidden_at = CASE WHEN TRIM(COALESCE(hidden_at, '')) = ''
                                 THEN ? ELSE hidden_at END
            WHERE group_name = '日向坂46'
              AND TRIM(COALESCE(member_name, '')) IN ('', '不明', '投稿者不明')
              AND COALESCE(is_hidden, 0) = 0
            """,
            (utc_now_text(),),
        )

        # =========================
        # 検索高速化用インデックス
        # =========================

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_blogs_group
            ON photo_blogs(group_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_photo_blogs_hidden
            ON photo_blogs(is_hidden, hidden_reason, group_name, member_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_photo_image_people_name
            ON photo_image_people(person_name, relation_status)
            """
        )



        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_photo_image_people_status_image
            ON photo_image_people(relation_status, image_id)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_priority_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                mode TEXT NOT NULL DEFAULT 'oldest',
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cursor.execute(
            "INSERT OR IGNORE INTO photo_ai_priority_settings(id,mode,updated_by,updated_at) VALUES(1,'oldest',0,'')"
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_blogs_member
            ON photo_blogs(member_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_blogs_published
            ON photo_blogs(published_at)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_images_blog
            ON photo_images(blog_id)
            """
        )


        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_images_image_hash
            ON photo_images(image_hash)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_ai_usage_created
            ON photo_ai_usage(created_at)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_ai_usage_model
            ON photo_ai_usage(model_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_images_download_status
            ON photo_images(download_status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_images_analysis_status
            ON photo_images(analysis_status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_ai_tags_tag
            ON photo_ai_tags(tag)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_ai_tags_category
            ON photo_ai_tags(category)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_manual_tags_tag
            ON photo_manual_tags(tag)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_review_status
            ON photo_review_queue(status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_people_name
            ON photo_people(person_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_people_group
            ON photo_people(group_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_faces_image
            ON photo_faces(image_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_faces_person
            ON photo_faces(confirmed_person_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_faces_status
            ON photo_faces(confirmation_status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_face_scans_status
            ON photo_face_scans(status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_face_candidates_face
            ON photo_face_candidates(face_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_face_candidates_person
            ON photo_face_candidates(person_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_photo_face_reviews_status
            ON photo_face_reviews(status)
            """
        )

        # 過去バージョンで「確定人物は存在するが確認キューが未確認／未作成」になった
        # データを非破壊で修復する。候補だけの画像は対象にしない。
        now_for_repair = utc_now_text()
        cursor.execute(
            """
            UPDATE photo_review_queue
               SET status='completed',
                   selected_value=COALESCE((
                       SELECT GROUP_CONCAT(pip.person_name, '、')
                         FROM photo_image_people pip
                        WHERE pip.image_id=photo_review_queue.image_id
                          AND pip.relation_status='confirmed'
                   ), selected_value),
                   updated_at=?
             WHERE EXISTS (
                       SELECT 1 FROM photo_image_people pip
                        WHERE pip.image_id=photo_review_queue.image_id
                          AND pip.relation_status='confirmed'
                   )
               AND status <> 'completed'
            """,
            (now_for_repair,),
        )
        cursor.execute(
            """
            INSERT INTO photo_review_queue(
                image_id, review_type, question, candidates, status,
                reviewed_by, selected_value, review_note,
                created_at, updated_at, reviewed_at
            )
            SELECT pi.id,
                   'person_identity',
                   'この写真に写っている人物を確認してください。',
                   '',
                   'completed',
                   'system_repair',
                   COALESCE((
                       SELECT GROUP_CONCAT(pip.person_name, '、')
                         FROM photo_image_people pip
                        WHERE pip.image_id=pi.id
                          AND pip.relation_status='confirmed'
                   ), ''),
                   '人物登録済みデータから確認状態を自動修復',
                   ?, ?, ?
              FROM photo_images pi
             WHERE EXISTS (
                       SELECT 1 FROM photo_image_people pip
                        WHERE pip.image_id=pi.id
                          AND pip.relation_status='confirmed'
                   )
               AND NOT EXISTS (
                       SELECT 1 FROM photo_review_queue prq
                        WHERE prq.image_id=pi.id
                   )
            """,
            (now_for_repair, now_for_repair, now_for_repair),
        )

        # タグマスターは既存タグを物理的に書き換えず、別名・承認状態を管理する。
        from tag_master import bootstrap_from_existing
        bootstrap_from_existing(connection)

        connection.commit()

    print(
        "写真検索DB初期化完了:",
        PHOTO_DB_PATH,
    )


# =========================
# ブログ登録
# =========================

def save_photo_blog(
    blog: dict[str, Any],
) -> int:
    """
    ブログ記事を登録または更新し、
    photo_blogs.idを返す。
    """

    blog_url = str(
        blog.get(
            "url",
            "",
        )
    ).strip()

    if not blog_url:

        raise ValueError(
            "ブログURLが空です。"
        )

    group_name = str(
        blog.get(
            "group",
            "",
        )
    ).strip()

    member_name = str(
        blog.get(
            "member",
            "",
        )
    ).strip()

    title = str(
        blog.get(
            "title",
            "",
        )
    ).strip()

    published_at = str(
        blog.get(
            "date",
            "",
        )
    ).strip()

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO photo_blogs (
                blog_url,
                group_name,
                member_name,
                title,
                published_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(blog_url)
            DO UPDATE SET
                group_name = excluded.group_name,
                member_name = excluded.member_name,
                title = excluded.title,
                published_at = excluded.published_at,
                updated_at = excluded.updated_at
            """,
            (
                blog_url,
                group_name,
                member_name,
                title,
                published_at,
                now,
                now,
            ),
        )

        cursor.execute(
            """
            SELECT id
            FROM photo_blogs
            WHERE blog_url = ?
            """,
            (
                blog_url,
            ),
        )

        row = cursor.fetchone()

        connection.commit()

    if row is None:

        raise RuntimeError(
            "ブログ情報の保存に失敗しました。"
        )

    return int(
        row["id"]
    )


def get_photo_blog_by_url(
    blog_url: str,
) -> dict[str, Any] | None:
    """
    URLからブログ情報を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT *
            FROM photo_blogs
            WHERE blog_url = ?
            """,
            (
                blog_url,
            ),
        )

        return row_to_dict(
            cursor.fetchone()
        )


def get_zero_image_photo_blogs(
    limit: int = 100,
    group_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    photo_blogsへ登録済みだが、photo_imagesが1件もない記事を返す。

    過去に画像抽出の不具合や一時的な取得失敗によって
    「画像なし」として登録された記事を再判定するために使用する。
    """

    selected_limit = max(1, min(int(limit), 1000))
    normalized_group = str(group_name or "").strip()

    sql = """
        SELECT
            photo_blogs.id,
            photo_blogs.blog_url AS url,
            photo_blogs.group_name AS "group",
            photo_blogs.member_name AS member,
            photo_blogs.title AS title,
            photo_blogs.published_at AS date,
            photo_blogs.created_at,
            photo_blogs.updated_at
        FROM photo_blogs
        LEFT JOIN photo_images
            ON photo_images.blog_id = photo_blogs.id
    """
    params: list[Any] = []

    if normalized_group and normalized_group != "all":
        sql += " WHERE photo_blogs.group_name = ?"
        params.append(normalized_group)

    sql += """
        GROUP BY photo_blogs.id
        HAVING COUNT(photo_images.id) = 0
        ORDER BY
            CASE WHEN photo_blogs.published_at = '' THEN 1 ELSE 0 END,
            photo_blogs.published_at DESC,
            photo_blogs.id DESC
        LIMIT ?
    """
    params.append(selected_limit)

    with closing(get_connection()) as connection:
        rows = connection.execute(sql, tuple(params)).fetchall()

    return rows_to_dicts(rows)


def get_registered_photo_blog_urls(
    group_name: str | None = None,
) -> set[str]:
    """
    写真DBへ登録済みのブログURLをまとめて返す。

    1件ずつSELECTする代わりに巡回開始時に一括取得し、
    一覧巡回中の判定をメモリ上で行うために使用する。
    """

    sql = "SELECT blog_url FROM photo_blogs"
    params: tuple[Any, ...] = ()

    normalized_group = str(group_name or "").strip()

    if normalized_group and normalized_group != "all":
        sql += " WHERE group_name = ?"
        params = (normalized_group,)

    with closing(get_connection()) as connection:
        rows = connection.execute(sql, params).fetchall()

    return {
        str(row["blog_url"]).strip()
        for row in rows
        if row["blog_url"] and str(row["blog_url"]).strip()
    }


# =========================
# 画像登録
# =========================

def is_supported_remote_image_url(image_url: str) -> bool:
    """HTTP/HTTPS の画像URLだけを保存対象として許可する。"""

    clean_url = str(image_url or "").strip()
    if not clean_url:
        return False

    parsed = urlparse(clean_url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def save_photo_image(
    blog_id: int,
    image_url: str,
    image_index: int = 0,
    local_path: str = "",
    file_name: str = "",
    mime_type: str = "",
    file_size: int = 0,
    width: int = 0,
    height: int = 0,
    image_hash: str = "",
    download_status: str = "pending",
) -> int:
    """
    ブログ画像を登録または更新し、
    photo_images.idを返す。
    """

    image_url = str(
        image_url
    ).strip()

    if not image_url:

        raise ValueError(
            "画像URLが空です。"
        )

    if not is_supported_remote_image_url(image_url):
        raise ValueError(
            f"HTTP/HTTPS以外の画像URLは登録できません: {image_url[:200]}"
        )

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO photo_images (
                blog_id,
                image_url,
                image_index,
                local_path,
                file_name,
                mime_type,
                file_size,
                width,
                height,
                image_hash,
                download_status,
                download_error,
                analysis_status,
                analysis_error,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                '',
                'pending',
                '',
                ?, ?
            )

            ON CONFLICT(blog_id, image_url)
            DO UPDATE SET
                image_index = excluded.image_index,

                local_path = CASE
                    WHEN excluded.local_path != ''
                    THEN excluded.local_path
                    ELSE photo_images.local_path
                END,

                file_name = CASE
                    WHEN excluded.file_name != ''
                    THEN excluded.file_name
                    ELSE photo_images.file_name
                END,

                mime_type = CASE
                    WHEN excluded.mime_type != ''
                    THEN excluded.mime_type
                    ELSE photo_images.mime_type
                END,

                file_size = CASE
                    WHEN excluded.file_size > 0
                    THEN excluded.file_size
                    ELSE photo_images.file_size
                END,

                width = CASE
                    WHEN excluded.width > 0
                    THEN excluded.width
                    ELSE photo_images.width
                END,

                height = CASE
                    WHEN excluded.height > 0
                    THEN excluded.height
                    ELSE photo_images.height
                END,

                image_hash = CASE
                    WHEN excluded.image_hash != ''
                    THEN excluded.image_hash
                    ELSE photo_images.image_hash
                END,

                download_status = CASE
                    WHEN photo_images.download_status = 'completed'
                    THEN photo_images.download_status
                    ELSE excluded.download_status
                END,

                download_error = CASE
                    WHEN excluded.download_status = 'completed'
                    THEN ''
                    ELSE photo_images.download_error
                END,

                updated_at = excluded.updated_at
            """,
            (
                blog_id,
                image_url,
                image_index,
                local_path,
                file_name,
                mime_type,
                file_size,
                width,
                height,
                image_hash,
                download_status,
                now,
                now,
            ),
        )

        cursor.execute(
            """
            SELECT id
            FROM photo_images
            WHERE blog_id = ?
            AND image_url = ?
            """,
            (
                blog_id,
                image_url,
            ),
        )

        row = cursor.fetchone()

        connection.commit()

    if row is None:

        raise RuntimeError(
            "画像情報の保存に失敗しました。"
        )

    return int(
        row["id"]
    )


def save_photo_images(
    blog_id: int,
    image_urls: list[str],
) -> list[dict[str, Any]]:
    """
    ブログ画像URL一覧をまとめて登録する。

    戻り値例:
        [
            {
                "image_id": 1,
                "image_url": "...",
                "image_index": 1,
            }
        ]
    """

    records: list[dict[str, Any]] = []

    for image_index, image_url in enumerate(
        image_urls,
        start=1,
    ):

        clean_url = str(
            image_url
        ).strip()

        if not clean_url:

            continue

        if not is_supported_remote_image_url(clean_url):
            print(
                "不正な画像URLを登録対象から除外:",
                clean_url[:300],
            )
            continue

        image_id = save_photo_image(
            blog_id=blog_id,
            image_url=clean_url,
            image_index=image_index,
        )

        records.append(
            {
                "image_id": image_id,
                "image_url": clean_url,
                "image_index": image_index,
            }
        )

    return records


def get_photo_image(
    image_id: int,
) -> dict[str, Any] | None:
    """
    画像IDから画像情報を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                photo_images.*,

                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at

            FROM photo_images

            INNER JOIN photo_blogs
                ON photo_images.blog_id
                = photo_blogs.id

            WHERE photo_images.id = ?
            """,
            (
                image_id,
            ),
        )

        return row_to_dict(
            cursor.fetchone()
        )


def get_pending_analysis_images(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    ダウンロード済みで、
    AI解析がまだ一度も完了していない画像を取得する。

    failedは自動では再試行せず、
    必要に応じて明示的にpendingへ戻してから再解析する。
    """

    limit = max(
        int(limit),
        1,
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                photo_images.*,

                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at

            FROM photo_images

            INNER JOIN photo_blogs
                ON photo_images.blog_id
                = photo_blogs.id

            WHERE
                photo_images.download_status = 'completed'

            AND
                photo_images.analysis_status = 'pending'

            AND
                (photo_images.local_path != '' OR photo_images.bucket_key != '')

            ORDER BY
                CASE COALESCE((SELECT mode FROM photo_ai_priority_settings WHERE id=1), 'oldest')
                    WHEN 'reviewed_first' THEN CASE WHEN EXISTS(SELECT 1 FROM photo_review_queue q WHERE q.image_id=photo_images.id AND q.status='completed') THEN 0 ELSE 1 END
                    ELSE 0
                END ASC,
                CASE COALESCE((SELECT mode FROM photo_ai_priority_settings WHERE id=1), 'oldest')
                    WHEN 'newest' THEN photo_images.id
                    WHEN 'new_blog_first' THEN photo_blogs.id
                    ELSE 0
                END DESC,
                CASE COALESCE((SELECT mode FROM photo_ai_priority_settings WHERE id=1), 'oldest')
                    WHEN 'oldest' THEN photo_images.id
                    WHEN 'reviewed_first' THEN photo_images.id
                    ELSE 0
                END ASC

            LIMIT ?
            """,
            (
                limit,
            ),
        )

        return rows_to_dicts(
            cursor.fetchall()
        )


# =========================
# 画像状態更新
# =========================

def update_image_download(
    image_id: int,
    *,
    local_path: str,
    file_name: str,
    mime_type: str,
    file_size: int,
    width: int,
    height: int,
    image_hash: str,
    status: str = "completed",
    bucket_key: str = "",
    bucket_status: str = "pending",
    bucket_error: str = "",
    storage_backend: str = "local",
) -> None:
    """
    画像ダウンロード後の情報を更新する。
    成功時は以前のダウンロードエラーを消す。
    """

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_images

            SET
                local_path = ?,
                file_name = ?,
                mime_type = ?,
                file_size = ?,
                width = ?,
                height = ?,
                image_hash = ?,
                download_status = ?,
                download_error = '',
                bucket_key = ?,
                bucket_status = ?,
                bucket_error = ?,
                storage_backend = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                local_path,
                file_name,
                mime_type,
                file_size,
                width,
                height,
                image_hash,
                status,
                bucket_key,
                bucket_status,
                bucket_error,
                storage_backend,
                utc_now_text(),
                image_id,
            ),
        )

        connection.commit()


def update_image_download_failure(
    image_id: int,
    error_message: str,
) -> None:
    """
    画像ダウンロード失敗状態とエラー内容を保存する。
    """

    error_text = str(
        error_message
    ).strip()[:1000]

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_images

            SET
                download_status = 'failed',
                download_error = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                error_text,
                utc_now_text(),
                image_id,
            ),
        )

        connection.commit()


def update_image_download_terminal_failure(
    image_id: int,
    status: str,
    error_message: str,
) -> None:
    """再試行しても直らない失敗を、通常の failed から分離して保存する。"""

    allowed_statuses = {"invalid_url", "permanent_failed"}
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in allowed_statuses:
        raise ValueError(f"未対応の終了状態です: {status}")

    error_text = str(error_message or "").strip()[:1000]

    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE photo_images
            SET
                download_status = ?,
                download_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (normalized_status, error_text, utc_now_text(), image_id),
        )
        connection.commit()


def reset_image_download_status(
    image_id: int,
) -> None:
    """
    画像のダウンロード状態をpendingへ戻す。
    手動再試行用。
    """

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_images

            SET
                download_status = 'pending',
                download_error = '',
                updated_at = ?

            WHERE id = ?
            """,
            (
                utc_now_text(),
                image_id,
            ),
        )

        connection.commit()


def update_image_analysis_status(
    image_id: int,
    status: str,
    error_message: str = "",
) -> None:
    """
    AI解析状態を更新する。

    主なstatus:
        pending
        processing
        completed
        review
        failed
    """

    error_text = str(
        error_message
    ).strip()[:2000]

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_images

            SET
                analysis_status = ?,
                analysis_error = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                status,
                error_text,
                utc_now_text(),
                image_id,
            ),
        )

        connection.commit()


def reset_failed_analysis_images(
    limit: int = 10,
) -> int:
    """
    失敗済みのAI解析を、古い順に指定件数だけpendingへ戻す。

    !ai_analyzeで未解析画像が0件だった場合の
    自動再試行に使用する。
    戻した件数を返す。
    """

    limit = max(
        int(limit),
        1,
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT id
            FROM photo_images
            WHERE
                download_status = 'completed'
            AND
                analysis_status = 'failed'
            AND
                (local_path != '' OR bucket_key != '')
            ORDER BY id ASC
            LIMIT ?
            """,
            (
                limit,
            ),
        )

        image_ids = [
            int(row["id"])
            for row in cursor.fetchall()
        ]

        if not image_ids:
            return 0

        placeholders = ",".join(
            "?" for _ in image_ids
        )

        connection.execute(
            f"""
            UPDATE photo_images
            SET
                analysis_status = 'pending',
                analysis_error = '',
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (
                utc_now_text(),
                *image_ids,
            ),
        )

        connection.commit()

        return len(image_ids)


def reset_image_analysis_status(
    image_id: int,
) -> None:
    """
    AI解析状態をpendingへ戻す。
    手動再解析用。
    """

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_images

            SET
                analysis_status = 'pending',
                analysis_error = '',
                updated_at = ?

            WHERE id = ?
            """,
            (
                utc_now_text(),
                image_id,
            ),
        )

        connection.commit()


# =========================
# AI解析結果
# =========================

def save_ai_analysis(
    image_id: int,
    *,
    model_name: str = "",
    raw_response: str = "",
    person_name: str = "",
    clothing: str = "",
    expression: str = "",
    background: str = "",
    pose: str = "",
    objects: str = "",
    person_count: int = 0,
    overall_confidence: float = 0,
    needs_review: bool = False,
) -> None:
    """
    画像全体のAI解析結果を保存する。
    """

    now = utc_now_text()

    overall_confidence = clamp_confidence(
        overall_confidence
    )

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_ai_analysis (
                image_id,
                model_name,
                raw_response,
                person_name,
                clothing,
                expression,
                background,
                pose,
                objects,
                person_count,
                overall_confidence,
                needs_review,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(image_id)
            DO UPDATE SET
                model_name = excluded.model_name,
                raw_response = excluded.raw_response,
                person_name = excluded.person_name,
                clothing = excluded.clothing,
                expression = excluded.expression,
                background = excluded.background,
                pose = excluded.pose,
                objects = excluded.objects,
                person_count = excluded.person_count,
                overall_confidence
                    = excluded.overall_confidence,
                needs_review = excluded.needs_review,
                updated_at = excluded.updated_at
            """,
            (
                image_id,
                model_name,
                raw_response,
                person_name,
                clothing,
                expression,
                background,
                pose,
                objects,
                max(
                    int(person_count),
                    0,
                ),
                overall_confidence,
                1 if needs_review else 0,
                now,
                now,
            ),
        )

        connection.commit()


# =========================
# AIタグ
# =========================

def clear_ai_tags(
    image_id: int,
) -> None:
    """
    指定画像の既存AIタグをすべて削除する。

    再解析前に呼び出すことで、
    古い解析結果のタグが残ることを防ぐ。
    """

    with closing(
        get_connection()
    ) as connection:

        from tag_master import archive_ai_tags
        archive_ai_tags(connection, image_id, action="reanalysis")
        connection.execute(
            """
            DELETE FROM photo_ai_tags
            WHERE image_id = ?
            """,
            (
                image_id,
            ),
        )

        from tag_master import refresh_image_cache
        refresh_image_cache(connection, image_id)
        connection.commit()


def save_ai_tag(
    image_id: int,
    tag: str,
    confidence: float,
    category: str = "",
    model_name: str = "",
    raw_value: str = "",
) -> None:
    """AIが判定したタグを正規化して保存する。"""
    tag = str(tag or "").strip()
    category = str(category or "").strip()
    if not tag:
        return
    confidence = clamp_confidence(confidence)
    now = utc_now_text()

    with closing(get_connection()) as connection:
        from tag_master import prepare_tag, refresh_image_cache
        prepared = prepare_tag(connection, tag, category, source="ai", confidence=confidence)
        if prepared["blocked"]:
            refresh_image_cache(connection, image_id)
            connection.commit()
            return
        if not raw_value:
            raw_value = tag
        canonical_tag = str(prepared["canonical_tag"])
        normalized_category = str(prepared["category"])
        connection.execute(
            """
            INSERT INTO photo_ai_tags (
                image_id, category, tag, confidence, model_name,
                raw_value, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id, category, tag)
            DO UPDATE SET
                confidence=excluded.confidence,
                model_name=excluded.model_name,
                raw_value=excluded.raw_value,
                updated_at=excluded.updated_at
            """,
            (
                image_id, normalized_category, canonical_tag, confidence,
                model_name, raw_value, now, now,
            ),
        )
        refresh_image_cache(connection, image_id)
        connection.commit()


# =========================
# 人間タグ
# =========================

def save_manual_tag(
    image_id: int,
    tag: str,
    category: str = "",
    created_by: str = "",
    note: str = "",
) -> None:
    """管理者が設定したタグを代表タグへ正規化して保存する。"""
    tag = str(tag or "").strip()
    category = str(category or "").strip()
    if not tag:
        return
    now = utc_now_text()

    with closing(get_connection()) as connection:
        from tag_master import prepare_tag, refresh_image_cache
        prepared = prepare_tag(connection, tag, category, source="manual")
        if prepared["blocked"]:
            refresh_image_cache(connection, image_id)
            connection.commit()
            return
        canonical_tag = str(prepared["canonical_tag"])
        normalized_category = str(prepared["category"])
        connection.execute(
            """
            INSERT INTO photo_manual_tags (
                image_id, category, tag, created_by, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id, category, tag)
            DO UPDATE SET
                created_by=excluded.created_by,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                image_id, normalized_category, canonical_tag,
                created_by, note, now, now,
            ),
        )
        refresh_image_cache(connection, image_id)
        connection.commit()


# =========================
# 人物マスター
# =========================

def save_person(
    person_name: str,
    group_name: str = "",
    generation_name: str = "",
    is_active: bool = True,
) -> int:
    """
    人物を登録または更新し、
    photo_people.idを返す。
    """

    person_name = str(
        person_name
    ).strip()

    group_name = str(
        group_name
    ).strip()

    generation_name = str(
        generation_name
    ).strip()

    if not person_name:

        raise ValueError(
            "人物名が空です。"
        )

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO photo_people (
                person_name,
                group_name,
                generation_name,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(person_name)
            DO UPDATE SET
                group_name = CASE
                    WHEN excluded.group_name != ''
                    THEN excluded.group_name
                    ELSE photo_people.group_name
                END,

                generation_name = CASE
                    WHEN excluded.generation_name != ''
                    THEN excluded.generation_name
                    ELSE photo_people.generation_name
                END,

                is_active = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (
                person_name,
                group_name,
                generation_name,
                1 if is_active else 0,
                now,
                now,
            ),
        )

        cursor.execute(
            """
            SELECT id
            FROM photo_people
            WHERE person_name = ?
            """,
            (
                person_name,
            ),
        )

        row = cursor.fetchone()

        connection.commit()

    if row is None:

        raise RuntimeError(
            "人物情報の保存に失敗しました。"
        )

    return int(
        row["id"]
    )


def get_person_by_name(
    person_name: str,
) -> dict[str, Any] | None:
    """
    人物名から人物情報を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT *
            FROM photo_people
            WHERE person_name = ?
            """,
            (
                person_name,
            ),
        )

        return row_to_dict(
            cursor.fetchone()
        )



def get_frequent_confirmed_people(
    group_name: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """確認済み写真でよく使われる人物を、使用回数順で返す。

    OpenAIは呼ばずSQLiteだけで集計するため、レビュー候補の表示に使っても
    API料金は増えない。グループ指定時は同じグループの写真に絞る。
    """
    safe_limit = max(1, min(int(limit), 25))
    group_name = str(group_name or "").strip()

    where = "WHERE pip.relation_status = 'confirmed'"
    params: list[Any] = []
    if group_name:
        where += " AND photo_blogs.group_name = ?"
        params.append(group_name)
    params.append(safe_limit)

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                pip.person_name,
                COUNT(DISTINCT pip.image_id) AS confirmed_count
            FROM photo_image_people AS pip
            JOIN photo_images
                ON photo_images.id = pip.image_id
            JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id
            {where}
              AND pip.person_name NOT IN ('人物不明', '')
            GROUP BY pip.person_name
            ORDER BY confirmed_count DESC, pip.person_name ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    return rows_to_dicts(rows)

def get_all_people(
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """
    人物マスターの一覧を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        if active_only:

            cursor = connection.execute(
                """
                SELECT *
                FROM photo_people
                WHERE is_active = 1
                ORDER BY
                    group_name ASC,
                    person_name ASC
                """
            )

        else:

            cursor = connection.execute(
                """
                SELECT *
                FROM photo_people
                ORDER BY
                    group_name ASC,
                    person_name ASC
                """
            )

        return rows_to_dicts(
            cursor.fetchall()
        )


# =========================
# 顔検出
# =========================

def save_detected_face(
    image_id: int,
    face_index: int,
    *,
    box_x: float = 0,
    box_y: float = 0,
    box_width: float = 0,
    box_height: float = 0,
    detection_confidence: float = 0,
    model_name: str = "",
    face_embedding: str = "",
) -> int:
    """
    画像内で検出された顔を保存する。

    座標は次のどちらでも保存できる。

    ・実際のピクセル座標
    ・画像幅・高さを1.0とした比率

    顔番号は1から始めることを推奨する。
    """

    now = utc_now_text()

    detection_confidence = clamp_confidence(
        detection_confidence
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO photo_faces (
                image_id,
                face_index,
                box_x,
                box_y,
                box_width,
                box_height,
                detection_confidence,
                confirmation_status,
                model_name,
                face_embedding,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, 'unconfirmed',
                ?, ?, ?, ?
            )

            ON CONFLICT(image_id, face_index)
            DO UPDATE SET
                box_x = excluded.box_x,
                box_y = excluded.box_y,
                box_width = excluded.box_width,
                box_height = excluded.box_height,
                detection_confidence
                    = excluded.detection_confidence,
                model_name = excluded.model_name,

                face_embedding = CASE
                    WHEN excluded.face_embedding != ''
                    THEN excluded.face_embedding
                    ELSE photo_faces.face_embedding
                END,

                updated_at = excluded.updated_at
            """,
            (
                image_id,
                int(face_index),
                float(box_x),
                float(box_y),
                float(box_width),
                float(box_height),
                detection_confidence,
                model_name,
                face_embedding,
                now,
                now,
            ),
        )

        cursor.execute(
            """
            SELECT id
            FROM photo_faces
            WHERE image_id = ?
            AND face_index = ?
            """,
            (
                image_id,
                int(face_index),
            ),
        )

        row = cursor.fetchone()

        connection.commit()

    if row is None:

        raise RuntimeError(
            "顔情報の保存に失敗しました。"
        )

    return int(
        row["id"]
    )


def save_face_candidate(
    face_id: int,
    person_id: int,
    confidence: float,
    candidate_rank: int = 0,
    model_name: str = "",
    raw_value: str = "",
) -> None:
    """
    顔に対する人物候補を保存する。
    """

    confidence = clamp_confidence(
        confidence
    )

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_face_candidates (
                face_id,
                person_id,
                confidence,
                candidate_rank,
                model_name,
                raw_value,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(face_id, person_id)
            DO UPDATE SET
                confidence = excluded.confidence,
                candidate_rank = excluded.candidate_rank,
                model_name = excluded.model_name,
                raw_value = excluded.raw_value,
                updated_at = excluded.updated_at
            """,
            (
                face_id,
                person_id,
                confidence,
                int(candidate_rank),
                model_name,
                raw_value,
                now,
                now,
            ),
        )

        connection.commit()


def confirm_face_person(
    face_id: int,
    person_id: int,
    *,
    confirmed_by: str = "",
    confirmation_status: str = "confirmed",
) -> None:
    """
    顔に写っている人物を確定する。
    """

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE photo_faces

            SET
                confirmed_person_id = ?,
                confirmation_status = ?,
                confirmed_by = ?,
                confirmed_at = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                person_id,
                confirmation_status,
                confirmed_by,
                now,
                now,
                face_id,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise ValueError(f"顔ID {face_id} が見つかりません。")
        connection.commit()

    # 重い特徴量品質更新はDiscord応答経路から分離する。
    try:
        from face_learning_queue import enqueue_face_learning
        enqueue_face_learning(int(face_id))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("顔学習キュー登録に失敗しました face_id=%s", face_id)


def clear_face_confirmation(
    face_id: int,
) -> None:
    """
    顔の人物確定を解除する。
    """

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_faces

            SET
                confirmed_person_id = NULL,
                confirmation_status = 'unconfirmed',
                confirmed_by = '',
                confirmed_at = '',
                updated_at = ?

            WHERE id = ?
            """,
            (
                utc_now_text(),
                face_id,
            ),
        )

        connection.commit()


def get_image_faces(
    image_id: int,
) -> list[dict[str, Any]]:
    """
    画像に含まれる顔と、
    確定済み人物名を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                photo_faces.*,
                photo_people.person_name
                    AS confirmed_person_name,
                photo_people.group_name
                    AS confirmed_group_name

            FROM photo_faces

            LEFT JOIN photo_people
                ON photo_faces.confirmed_person_id
                = photo_people.id

            WHERE photo_faces.image_id = ?

            ORDER BY
                photo_faces.face_index ASC
            """,
            (
                image_id,
            ),
        )

        return rows_to_dicts(
            cursor.fetchall()
        )


def get_face_candidates(
    face_id: int,
) -> list[dict[str, Any]]:
    """
    顔に登録されている人物候補を取得する。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                photo_face_candidates.*,
                photo_people.person_name,
                photo_people.group_name,
                photo_people.generation_name

            FROM photo_face_candidates

            INNER JOIN photo_people
                ON photo_face_candidates.person_id
                = photo_people.id

            WHERE photo_face_candidates.face_id = ?

            ORDER BY
                photo_face_candidates.candidate_rank ASC,
                photo_face_candidates.confidence DESC
            """,
            (
                face_id,
            ),
        )

        items = rows_to_dicts(
            cursor.fetchall()
        )

    # 統合スコアの内訳があれば候補へ付加する。古いDBでも表示を壊さない。
    try:
        from face_candidate_scoring import get_face_score_details
        details = get_face_score_details(int(face_id))
        for item in items:
            detail = details.get(int(item.get("person_id") or 0), {})
            if detail:
                item["score_detail"] = detail
                item["face_similarity"] = float(detail.get("face_similarity") or 0)
                item["person_quality"] = float(detail.get("person_quality") or 0)
                item["reference_count"] = int(detail.get("reference_count") or 0)
                item["acceptance_rate"] = float(detail.get("acceptance_rate") or 0)
                item["author_match"] = bool(detail.get("author_match"))
                item["confidence_band"] = str(detail.get("confidence_band") or "")
                item["score_reason"] = str(detail.get("reason") or "")
    except Exception:
        pass
    return items


# =========================
# 画像単位の確認待ち
# =========================

def add_review_item(
    image_id: int,
    review_type: str,
    question: str,
    candidates: str = "",
) -> None:
    """
    人間による画像単位の確認待ちを登録する。
    """

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_review_queue (
                image_id,
                review_type,
                question,
                candidates,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?)

            ON CONFLICT(image_id)
            DO UPDATE SET
                review_type = excluded.review_type,
                question = excluded.question,
                candidates = excluded.candidates,
                status = 'pending',
                updated_at = excluded.updated_at
            """,
            (
                image_id,
                review_type,
                question,
                candidates,
                now,
                now,
            ),
        )

        connection.commit()


def complete_review_item(
    image_id: int,
    selected_value: str,
    reviewed_by: str = "",
    review_note: str = "",
) -> None:
    """
    画像単位の確認待ちを完了状態にする。
    """

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_review_queue

            SET
                status = 'completed',
                selected_value = ?,
                reviewed_by = ?,
                review_note = ?,
                reviewed_at = ?,
                updated_at = ?

            WHERE image_id = ?
            """,
            (
                selected_value,
                reviewed_by,
                review_note,
                now,
                now,
                image_id,
            ),
        )

        connection.commit()


# =========================
# 顔単位の確認待ち
# =========================

def add_face_review(
    face_id: int,
    question: str,
    candidates: list[dict[str, Any]] | str,
) -> None:
    """
    顔単位の確認待ちを登録する。

    candidatesがリストの場合は、
    JSON文字列へ自動変換する。
    """

    if isinstance(
        candidates,
        str,
    ):

        candidate_text = candidates

    else:

        candidate_text = json.dumps(
            candidates,
            ensure_ascii=False,
        )

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_face_reviews (
                face_id,
                question,
                candidates,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?)

            ON CONFLICT(face_id)
            DO UPDATE SET
                question = excluded.question,
                candidates = excluded.candidates,
                updated_at = excluded.updated_at

            -- 候補を再計算しただけで、完了済み・保留済みのレビューを
            -- pendingへ戻さない。statusや確定人物、確認者などは保持する。
            """,
            (
                face_id,
                question,
                candidate_text,
                now,
                now,
            ),
        )

        connection.commit()


def complete_face_review(
    face_id: int,
    person_id: int,
    reviewed_by: str = "",
    review_note: str = "",
) -> None:
    """
    顔の人物確認を完了し、
    photo_faces側にも確定人物を保存する。
    """

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_face_reviews

            SET
                status = 'completed',
                selected_person_id = ?,
                reviewed_by = ?,
                review_note = ?,
                reviewed_at = ?,
                updated_at = ?

            WHERE face_id = ?
            """,
            (
                person_id,
                reviewed_by,
                review_note,
                now,
                now,
                face_id,
            ),
        )

        connection.execute(
            """
            UPDATE photo_faces

            SET
                confirmed_person_id = ?,
                confirmation_status = 'manually_confirmed',
                confirmed_by = ?,
                confirmed_at = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                person_id,
                reviewed_by,
                now,
                now,
                face_id,
            ),
        )

        connection.commit()

    # 管理者の本確定結果を誤学習防止ポリシー経由で参照顔へ反映する。
    try:
        from face_candidate_scoring import register_confirmed_face_learning
        register_confirmed_face_learning(int(face_id), int(person_id), source="manual_review")
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "確定顔の安全学習反映に失敗しました face_id=%s", face_id
        )


def skip_face_review(
    face_id: int,
    reviewed_by: str = "",
    review_note: str = "",
) -> None:
    """顔レビューを保留状態にする。顔の人物確定情報は変更しない。"""
    now = utc_now_text()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE photo_face_reviews
            SET status = 'skipped',
                selected_person_id = NULL,
                reviewed_by = ?,
                review_note = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE face_id = ?
            """,
            (reviewed_by, review_note, now, now, int(face_id)),
        )
        connection.commit()


def get_face_debug_info(face_id: int) -> dict[str, Any] | None:
    """顔IDから、元画像と顔レビューの保存状態をまとめて取得する。"""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                photo_faces.id AS face_id,
                photo_faces.image_id,
                photo_faces.face_index,
                photo_faces.confirmation_status,
                photo_face_reviews.status AS review_status,
                photo_images.local_path,
                photo_images.bucket_key,
                photo_images.file_name,
                photo_images.image_url,
                photo_images.download_status,
                photo_images.download_error,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title
            FROM photo_faces
            JOIN photo_images ON photo_images.id = photo_faces.image_id
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_face_reviews ON photo_face_reviews.face_id = photo_faces.id
            WHERE photo_faces.id = ?
            """,
            (int(face_id),),
        ).fetchone()
        return row_to_dict(row)


def get_pending_face_reviews(
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    顔単位の未確認項目を取得する。
    """

    limit = max(
        int(limit),
        1,
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                photo_face_reviews.*,

                photo_faces.image_id,
                photo_faces.face_index,
                photo_faces.box_x,
                photo_faces.box_y,
                photo_faces.box_width,
                photo_faces.box_height,

                photo_images.local_path,
                photo_images.bucket_key,
                photo_images.file_name,
                photo_images.image_url,
                photo_images.download_status,
                photo_images.download_error,

                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at

            FROM photo_face_reviews

            INNER JOIN photo_faces
                ON photo_face_reviews.face_id
                = photo_faces.id

            INNER JOIN photo_images
                ON photo_faces.image_id
                = photo_images.id

            INNER JOIN photo_blogs
                ON photo_images.blog_id
                = photo_blogs.id

            WHERE
                photo_face_reviews.status = 'pending'

            ORDER BY
                photo_face_reviews.id ASC

            LIMIT ?
            """,
            (
                limit,
            ),
        )

        return rows_to_dicts(
            cursor.fetchall()
        )


def get_pending_face_embeddings(limit: int = 200) -> list[dict[str, Any]]:
    """顔特徴量を持つ確認待ちレビューを取得する。"""
    safe_limit = max(2, min(int(limit), 500))
    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            SELECT
                photo_face_reviews.id AS review_id,
                photo_face_reviews.face_id,
                photo_faces.image_id,
                photo_faces.face_index,
                photo_faces.face_embedding,
                photo_images.local_path,
                photo_images.bucket_key,
                photo_images.file_name,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at
            FROM photo_face_reviews
            JOIN photo_faces ON photo_faces.id = photo_face_reviews.face_id
            JOIN photo_images ON photo_images.id = photo_faces.image_id
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            WHERE photo_face_reviews.status = 'pending'
              AND photo_faces.face_embedding <> ''
              AND photo_faces.confirmed_person_id IS NULL
            ORDER BY photo_face_reviews.id ASC
            LIMIT ?
            """,
            (safe_limit,),
        )
        return rows_to_dicts(cursor.fetchall())


def complete_face_cluster(
    face_ids: list[int],
    person_id: int,
    reviewed_by: str = "",
    review_note: str = "",
) -> int:
    """同一クラスタの確認待ち顔を、1トランザクションで一括確定する。"""
    normalized_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
    if not normalized_ids:
        return 0
    now = utc_now_text()
    placeholders = ",".join("?" for _ in normalized_ids)
    with closing(get_connection()) as connection:
        valid_rows = connection.execute(
            f"""
            SELECT face_id
            FROM photo_face_reviews
            WHERE status = 'pending'
              AND face_id IN ({placeholders})
            """,
            tuple(normalized_ids),
        ).fetchall()
        valid_ids = [int(row["face_id"]) for row in valid_rows]
        if not valid_ids:
            return 0
        valid_placeholders = ",".join("?" for _ in valid_ids)
        connection.execute(
            f"""
            UPDATE photo_face_reviews
            SET status = 'completed',
                selected_person_id = ?,
                reviewed_by = ?,
                review_note = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE status = 'pending'
              AND face_id IN ({valid_placeholders})
            """,
            (int(person_id), reviewed_by, review_note, now, now, *valid_ids),
        )
        connection.execute(
            f"""
            UPDATE photo_faces
            SET confirmed_person_id = ?,
                confirmation_status = 'manually_confirmed',
                confirmed_by = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE id IN ({valid_placeholders})
            """,
            (int(person_id), reviewed_by, now, now, *valid_ids),
        )
        connection.commit()
        return len(valid_ids)


# =========================
# 人物検索
# =========================

def search_images_by_person(
    person_name: str,
    limit: int = 20,
    confirmed_only: bool = True,
) -> list[dict[str, Any]]:
    """
    画像内人物名から画像を検索する。
    """

    person_name = str(
        person_name
    ).strip()

    if not person_name:

        return []

    limit = max(
        int(limit),
        1,
    )

    with closing(
        get_connection()
    ) as connection:

        if confirmed_only:

            cursor = connection.execute(
                """
                SELECT DISTINCT
                    photo_images.*,

                    photo_blogs.blog_url,
                    photo_blogs.group_name,
                    photo_blogs.member_name,
                    photo_blogs.title,
                    photo_blogs.published_at,

                    photo_people.person_name
                        AS matched_person_name

                FROM photo_faces

                INNER JOIN photo_people
                    ON photo_faces.confirmed_person_id
                    = photo_people.id

                INNER JOIN photo_images
                    ON photo_faces.image_id
                    = photo_images.id

                INNER JOIN photo_blogs
                    ON photo_images.blog_id
                    = photo_blogs.id

                WHERE
                    photo_people.person_name = ?

                AND
                    photo_faces.confirmation_status
                    IN (
                        'confirmed',
                        'auto_confirmed',
                        'manually_confirmed'
                    )

                ORDER BY
                    photo_blogs.published_at DESC,
                    photo_images.image_index ASC

                LIMIT ?
                """,
                (
                    person_name,
                    limit,
                ),
            )

        else:

            cursor = connection.execute(
                """
                SELECT DISTINCT
                    photo_images.*,

                    photo_blogs.blog_url,
                    photo_blogs.group_name,
                    photo_blogs.member_name,
                    photo_blogs.title,
                    photo_blogs.published_at,

                    photo_people.person_name
                        AS matched_person_name,

                    photo_face_candidates.confidence
                        AS match_confidence

                FROM photo_face_candidates

                INNER JOIN photo_people
                    ON photo_face_candidates.person_id
                    = photo_people.id

                INNER JOIN photo_faces
                    ON photo_face_candidates.face_id
                    = photo_faces.id

                INNER JOIN photo_images
                    ON photo_faces.image_id
                    = photo_images.id

                INNER JOIN photo_blogs
                    ON photo_images.blog_id
                    = photo_blogs.id

                WHERE
                    photo_people.person_name = ?

                ORDER BY
                    photo_face_candidates.confidence DESC,
                    photo_blogs.published_at DESC

                LIMIT ?
                """,
                (
                    person_name,
                    limit,
                ),
            )

        return rows_to_dicts(
            cursor.fetchall()
        )


# =========================
# 写真キーワード検索
# =========================

# =========================
# 画像人物情報
# =========================

def add_image_person_candidate(image_id: int, person_name: str, *, source: str = "blog_author", confidence: float = 0.35) -> None:
    person_name = str(person_name or "").strip()
    if not person_name:
        return
    now = utc_now_text()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO photo_image_people (
                image_id, person_name, relation_status, source, confidence,
                confirmed_by, note, created_at, updated_at
            ) VALUES (?, ?, 'candidate', ?, ?, '', '', ?, ?)
            ON CONFLICT(image_id, person_name) DO UPDATE SET
                source = CASE WHEN photo_image_people.relation_status = 'confirmed'
                              THEN photo_image_people.source ELSE excluded.source END,
                confidence = CASE WHEN photo_image_people.relation_status = 'confirmed'
                                  THEN photo_image_people.confidence ELSE excluded.confidence END,
                updated_at = excluded.updated_at
            """,
            (image_id, person_name, source, clamp_confidence(confidence), now, now),
        )
        existing = connection.execute(
            "SELECT 1 FROM photo_image_people WHERE image_id = ? AND relation_status = 'confirmed' LIMIT 1",
            (image_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO photo_review_queue (
                    image_id, review_type, question, candidates, status,
                    created_at, updated_at
                ) VALUES (?, 'person_identity', 'この写真に写っている人物を確認してください。', ?, 'pending', ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    review_type = 'person_identity',
                    question = excluded.question,
                    candidates = excluded.candidates,
                    status = CASE WHEN photo_review_queue.status = 'completed' THEN photo_review_queue.status ELSE 'pending' END,
                    updated_at = excluded.updated_at
                """,
                (image_id, person_name, now, now),
            )
        connection.commit()


def set_confirmed_image_people(image_id: int, person_names: list[str], *, confirmed_by: str = "", note: str = "") -> None:
    names=[]
    for value in person_names:
        name=str(value or "").strip()
        if name and name not in names:
            names.append(name)
    now=utc_now_text()
    with closing(get_connection()) as connection:
        connection.execute("DELETE FROM photo_image_people WHERE image_id = ? AND relation_status = 'confirmed'", (image_id,))
        for name in names:
            connection.execute(
                """
                INSERT INTO photo_image_people (
                    image_id, person_name, relation_status, source, confidence,
                    confirmed_by, note, created_at, updated_at
                ) VALUES (?, ?, 'confirmed', 'manual', 1.0, ?, ?, ?, ?)
                ON CONFLICT(image_id, person_name) DO UPDATE SET
                    relation_status='confirmed', source='manual', confidence=1.0,
                    confirmed_by=excluded.confirmed_by, note=excluded.note, updated_at=excluded.updated_at
                """,
                (image_id, name, confirmed_by, note, now, now),
            )
            connection.execute(
                """INSERT INTO photo_people(person_name, group_name, generation_name, is_active, created_at, updated_at)
                   VALUES (?, '', '', 1, ?, ?)
                   ON CONFLICT(person_name) DO UPDATE SET updated_at=excluded.updated_at""",
                (name, now, now),
            )
        # 確認キューが存在しない画像でも、人物確定と同時に必ず完了状態を作る。
        # 以前は UPDATE のみだったため、候補キュー未作成の画像では人物名だけ保存され、
        # 管理画面では「未確認」のまま残る不整合が発生していた。
        connection.execute(
            """
            INSERT INTO photo_review_queue (
                image_id, review_type, question, candidates, status,
                reviewed_by, selected_value, review_note,
                created_at, updated_at, reviewed_at
            ) VALUES (?, 'person_identity', 'この写真に写っている人物を確認してください。', '',
                      'completed', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                review_type='person_identity',
                status='completed',
                reviewed_by=excluded.reviewed_by,
                selected_value=excluded.selected_value,
                review_note=excluded.review_note,
                reviewed_at=excluded.reviewed_at,
                updated_at=excluded.updated_at
            """,
            (image_id, confirmed_by, "、".join(names), note, now, now, now),
        )
        # 管理者運用ダッシュボードの監査ログが導入済みなら、
        # すべての人物確定経路を中央で記録する。
        audit_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_admin_audit_log'"
        ).fetchone()
        if audit_exists:
            connection.execute(
                """INSERT INTO photo_admin_audit_log(
                       admin_user_id, action_type, target_type, target_id, detail, created_at
                   ) VALUES(0, 'image_people_confirm', 'image', ?, ?, ?)""",
                (
                    str(image_id),
                    f"reviewer={confirmed_by}; people={'、'.join(names) or '人物なし'}; note={note}"[:2000],
                    now,
                ),
            )
        connection.commit()


def get_image_people(image_id: int) -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        rows=connection.execute(
            "SELECT * FROM photo_image_people WHERE image_id=? ORDER BY CASE relation_status WHEN 'confirmed' THEN 0 ELSE 1 END, id",
            (image_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def search_photo_images_by_author(author_name: str, limit: int = 10) -> list[dict[str, Any]]:
    author_name=str(author_name or "").strip()
    if not author_name:
        return []
    with closing(get_connection()) as connection:
        rows=connection.execute(
            """
            SELECT photo_images.*, photo_blogs.blog_url, photo_blogs.group_name,
                   photo_blogs.member_name, photo_blogs.title, photo_blogs.published_at,
                   '' AS ai_person_name,
                   COALESCE(photo_ai_analysis.clothing,'') AS clothing,
                   COALESCE(photo_ai_analysis.expression,'') AS expression,
                   COALESCE(photo_ai_analysis.background,'') AS background,
                   COALESCE(photo_ai_analysis.pose,'') AS pose,
                   COALESCE(photo_ai_analysis.objects,'') AS objects,
                   '' AS ai_tags, '' AS manual_tags,
                   COALESCE((SELECT GROUP_CONCAT(person_name, '、') FROM photo_image_people p
                             WHERE p.image_id=photo_images.id AND p.relation_status='confirmed'),'') AS confirmed_people,
                   COALESCE((SELECT GROUP_CONCAT(person_name, '、') FROM photo_image_people p
                             WHERE p.image_id=photo_images.id AND p.relation_status='candidate'),'') AS candidate_people
            FROM photo_images JOIN photo_blogs ON photo_blogs.id=photo_images.blog_id
            LEFT JOIN photo_ai_analysis ON photo_ai_analysis.image_id=photo_images.id
            WHERE photo_images.download_status='completed' AND (photo_images.local_path!='' OR photo_images.bucket_key!='')
              AND photo_blogs.member_name LIKE ?
            ORDER BY photo_blogs.published_at DESC, photo_images.image_index ASC
            LIMIT ?
            """, (f"%{author_name}%", max(1,min(int(limit),50)))
        ).fetchall()
        return rows_to_dicts(rows)

def search_photo_images(
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    ブログ情報・AI解析結果・AIタグ・手動タグを横断して画像を検索する。

    空白区切りの複数キーワードはAND検索になる。
    例:
        菅原咲月
        浴衣
        菅原咲月 浴衣
    """

    keywords = [
        keyword.strip()
        for keyword in str(query).replace("　", " ").split()
        if keyword.strip()
    ]

    if not keywords:
        return []

    limit = max(
        1,
        min(int(limit), 50),
    )

    conditions: list[str] = []
    parameters: list[Any] = []

    for keyword in keywords:
        like_value = f"%{keyword}%"

        conditions.append(
            """
            (
                photo_blogs.group_name LIKE ?
                OR photo_blogs.member_name LIKE ?
                OR photo_blogs.title LIKE ?
                OR EXISTS (
                    SELECT 1 FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                    AND pip.relation_status = 'confirmed'
                    AND pip.person_name LIKE ?
                )
                OR photo_blogs.published_at LIKE ?
                OR COALESCE(photo_ai_analysis.clothing, '') LIKE ?
                OR COALESCE(photo_ai_analysis.expression, '') LIKE ?
                OR COALESCE(photo_ai_analysis.background, '') LIKE ?
                OR COALESCE(photo_ai_analysis.pose, '') LIKE ?
                OR COALESCE(photo_ai_analysis.objects, '') LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM photo_ai_tags
                    WHERE photo_ai_tags.image_id = photo_images.id
                    AND photo_ai_tags.tag LIKE ?
                )
                OR EXISTS (
                    SELECT 1
                    FROM photo_manual_tags
                    WHERE photo_manual_tags.image_id = photo_images.id
                    AND photo_manual_tags.tag LIKE ?
                )
            )
            """
        )

        parameters.extend(
            [like_value] * 12
        )

    where_clause = " AND ".join(
        conditions
    )

    parameters.append(
        limit
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            f"""
            SELECT
                photo_images.*,

                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,

                COALESCE((SELECT GROUP_CONCAT(person_name, '、') FROM photo_image_people pip
                          WHERE pip.image_id=photo_images.id AND pip.relation_status='confirmed'), '')
                    AS confirmed_people,
                COALESCE((SELECT GROUP_CONCAT(person_name, '、') FROM photo_image_people pip
                          WHERE pip.image_id=photo_images.id AND pip.relation_status='candidate'), '')
                    AS candidate_people,

                COALESCE(photo_ai_analysis.person_name, '')
                    AS ai_person_name,
                COALESCE(photo_ai_analysis.clothing, '')
                    AS clothing,
                COALESCE(photo_ai_analysis.expression, '')
                    AS expression,
                COALESCE(photo_ai_analysis.background, '')
                    AS background,
                COALESCE(photo_ai_analysis.pose, '')
                    AS pose,
                COALESCE(photo_ai_analysis.objects, '')
                    AS objects,

                COALESCE(
                    (
                        SELECT GROUP_CONCAT(tag, '、')
                        FROM (
                            SELECT tag
                            FROM photo_ai_tags
                            WHERE photo_ai_tags.image_id = photo_images.id
                            ORDER BY confidence DESC, id ASC
                            LIMIT 12
                        )
                    ),
                    ''
                ) AS ai_tags,

                COALESCE(
                    (
                        SELECT GROUP_CONCAT(tag, '、')
                        FROM (
                            SELECT tag
                            FROM photo_manual_tags
                            WHERE photo_manual_tags.image_id = photo_images.id
                            ORDER BY id ASC
                            LIMIT 12
                        )
                    ),
                    ''
                ) AS manual_tags

            FROM photo_images

            INNER JOIN photo_blogs
                ON photo_images.blog_id = photo_blogs.id

            LEFT JOIN photo_ai_analysis
                ON photo_images.id = photo_ai_analysis.image_id

            WHERE
                photo_images.download_status = 'completed'
                AND (photo_images.local_path != '' OR photo_images.bucket_key != '')
                AND ({where_clause})

            ORDER BY
                photo_blogs.published_at DESC,
                photo_images.image_index ASC,
                photo_images.id DESC

            LIMIT ?
            """,
            tuple(parameters),
        )

        return rows_to_dicts(
            cursor.fetchall()
        )


# =========================
# Phase 5: 専用検索
# =========================

def _search_photo_images_with_where(
    where_sql: str,
    parameters: tuple[Any, ...],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """信頼済みのWHERE句を使って写真検索結果を取得する内部関数。"""

    safe_limit = max(1, min(int(limit), 50))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                photo_images.*,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'confirmed'
                ), '') AS confirmed_people,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'candidate'
                ), '') AS candidate_people,
                COALESCE(photo_ai_analysis.person_name, '') AS ai_person_name,
                COALESCE(photo_ai_analysis.clothing, '') AS clothing,
                COALESCE(photo_ai_analysis.expression, '') AS expression,
                COALESCE(photo_ai_analysis.background, '') AS background,
                COALESCE(photo_ai_analysis.pose, '') AS pose,
                COALESCE(photo_ai_analysis.objects, '') AS objects,
                COALESCE((
                    SELECT GROUP_CONCAT(tag, '、')
                    FROM (
                        SELECT tag
                        FROM photo_ai_tags
                        WHERE photo_ai_tags.image_id = photo_images.id
                        ORDER BY confidence DESC, id ASC
                        LIMIT 12
                    )
                ), '') AS ai_tags,
                COALESCE((
                    SELECT GROUP_CONCAT(tag, '、')
                    FROM (
                        SELECT tag
                        FROM photo_manual_tags
                        WHERE photo_manual_tags.image_id = photo_images.id
                        ORDER BY id ASC
                        LIMIT 12
                    )
                ), '') AS manual_tags
            FROM photo_images
            INNER JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_ai_analysis ON photo_ai_analysis.image_id = photo_images.id
            WHERE photo_images.download_status = 'completed'
              AND (photo_images.local_path != '' OR photo_images.bucket_key != '')
              AND ({where_sql})
            ORDER BY photo_blogs.published_at DESC, photo_images.image_index ASC, photo_images.id DESC
            LIMIT ?
            """,
            (*parameters, safe_limit),
        ).fetchall()

    return rows_to_dicts(rows)


def search_photo_images_by_person(person_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """確認済み人物を対象に検索する。"""

    clean_name = str(person_name or '').strip()
    if not clean_name:
        return []

    return _search_photo_images_with_where(
        """
        EXISTS (
            SELECT 1
            FROM photo_image_people pip
            WHERE pip.image_id = photo_images.id
              AND pip.relation_status = 'confirmed'
              AND pip.person_name LIKE ?
        )
        """,
        (f'%{clean_name}%',),
        limit=limit,
    )


def search_photo_images_by_tag(tag: str, limit: int = 20) -> list[dict[str, Any]]:
    """AIタグと手動タグを、検索用タグの同義語も含めて横断検索する。"""

    aliases = searchable_aliases(tag)
    if not aliases:
        return []

    ai_clauses = []
    manual_clauses = []
    params: list[str] = []
    for alias in aliases:
        ai_clauses.append("photo_ai_tags.tag LIKE ?")
        params.append(f"%{alias}%")
    for alias in aliases:
        manual_clauses.append("photo_manual_tags.tag LIKE ?")
        params.append(f"%{alias}%")

    where_sql = f"""
        EXISTS (
            SELECT 1 FROM photo_ai_tags
            WHERE photo_ai_tags.image_id = photo_images.id
              AND ({' OR '.join(ai_clauses)})
        )
        OR EXISTS (
            SELECT 1 FROM photo_manual_tags
            WHERE photo_manual_tags.image_id = photo_images.id
              AND ({' OR '.join(manual_clauses)})
        )
    """
    return _search_photo_images_with_where(
        where_sql,
        tuple(params),
        limit=limit,
    )


def search_photo_images_by_blog(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """ブログ投稿者・タイトル・グループを対象に検索する。"""

    clean_query = str(query or '').strip()
    if not clean_query:
        return []

    like_value = f'%{clean_query}%'
    return _search_photo_images_with_where(
        """
        photo_blogs.member_name LIKE ?
        OR photo_blogs.title LIKE ?
        OR photo_blogs.group_name LIKE ?
        """,
        (like_value, like_value, like_value),
        limit=limit,
    )


# =========================
# 件数確認
# =========================

def get_photo_db_counts() -> dict[str, int]:
    """
    写真検索DB内の件数を返す。
    """

    counts: dict[str, int] = {}

    with closing(
        get_connection()
    ) as connection:

        count_queries = {
            "blogs": """
                SELECT COUNT(*) AS count
                FROM photo_blogs
            """,

            "images": """
                SELECT COUNT(*) AS count
                FROM photo_images
            """,

            "ai_tags": """
                SELECT COUNT(*) AS count
                FROM photo_ai_tags
            """,

            "manual_tags": """
                SELECT COUNT(*) AS count
                FROM photo_manual_tags
            """,

            "pending_reviews": """
                SELECT COUNT(*) AS count
                FROM photo_review_queue
                WHERE status = 'pending'
            """,

            "favorites": """
                SELECT COUNT(*) AS count
                FROM photo_favorites
            """,

            "people": """
                SELECT COUNT(*) AS count
                FROM photo_people
            """,

            "face_scanned_images": """
                SELECT COUNT(*) AS count
                FROM photo_face_scans
                WHERE status = 'completed'
            """,

            "face_scan_failed_images": """
                SELECT COUNT(*) AS count
                FROM photo_face_scans
                WHERE status = 'failed'
            """,

            "faces": """
                SELECT COUNT(*) AS count
                FROM photo_faces
            """,

            "confirmed_faces": """
                SELECT COUNT(*) AS count
                FROM photo_faces
                WHERE confirmed_person_id IS NOT NULL
            """,

            "face_candidates": """
                SELECT COUNT(*) AS count
                FROM photo_face_candidates
            """,

            "pending_face_reviews": """
                SELECT COUNT(*) AS count
                FROM photo_face_reviews
                WHERE status = 'pending'
            """,
        }

        for key, query in count_queries.items():

            cursor = connection.execute(
                query
            )

            row = cursor.fetchone()

            counts[key] = (
                int(
                    row["count"]
                )
                if row
                else 0
            )

    return counts


# =========================
# 画像保存状況
# =========================

def get_photo_storage_stats() -> dict[str, int]:
    """
    画像ファイルの保存状況を返す。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.execute(
            """
            SELECT
                COUNT(*) AS total_images,

                SUM(
                    CASE
                        WHEN download_status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS completed,

                SUM(
                    CASE
                        WHEN download_status = 'pending'
                        THEN 1
                        ELSE 0
                    END
                ) AS pending,

                SUM(
                    CASE
                        WHEN download_status = 'failed'
                        THEN 1
                        ELSE 0
                    END
                ) AS failed,

                SUM(
                    CASE
                        WHEN download_status = 'invalid_url'
                        THEN 1
                        ELSE 0
                    END
                ) AS invalid_url,

                SUM(
                    CASE
                        WHEN download_status = 'permanent_failed'
                        THEN 1
                        ELSE 0
                    END
                ) AS permanent_failed,

                SUM(
                    CASE
                        WHEN download_status = 'completed'
                        THEN file_size
                        ELSE 0
                    END
                ) AS total_size

            FROM photo_images
            """
        )

        row = cursor.fetchone()

    if row is None:

        return {
            "total_images": 0,
            "completed": 0,
            "pending": 0,
            "failed": 0,
            "invalid_url": 0,
            "permanent_failed": 0,
            "total_size": 0,
        }

    return {
        "total_images": int(
            row["total_images"]
            or 0
        ),

        "completed": int(
            row["completed"]
            or 0
        ),

        "pending": int(
            row["pending"]
            or 0
        ),

        "failed": int(
            row["failed"]
            or 0
        ),

        "invalid_url": int(
            row["invalid_url"]
            or 0
        ),

        "permanent_failed": int(
            row["permanent_failed"]
            or 0
        ),

        "total_size": int(
            row["total_size"]
            or 0
        ),
    }




def ensure_pending_person_review_queue(
    group_name: str = "",
    blog_id: int | None = None,
) -> int:
    """人物未確定なのにレビューキューが無い画像へ pending 行を補完する。

    ブログ進捗では、キュー未作成の画像も「未確認」として数える。
    一方、従来の人物確認画面は photo_review_queue.status='pending' の行しか
    取得しなかったため、「残りあり」なのに「確認待ちなし」となることがあった。
    この補完処理で両者の対象条件を一致させる。

    復旧不能URLなど terminal 状態の画像と、すでに人物確定済みの画像は対象外。
    """
    normalized_group = str(group_name or "").strip()
    normalized_blog_id = int(blog_id) if blog_id is not None else None

    filters = [
        "COALESCE(pb.is_hidden, 0) = 0",
        "COALESCE(pi.download_status, '') NOT IN ('invalid_url', 'permanent_failed')",
        """NOT EXISTS (
            SELECT 1
            FROM photo_image_people pip
            WHERE pip.image_id = pi.id
              AND pip.relation_status = 'confirmed'
        )""",
        """NOT EXISTS (
            SELECT 1
            FROM photo_review_queue existing_review
            WHERE existing_review.image_id = pi.id
              AND existing_review.review_type = 'person_identity'
        )""",
    ]
    values: list[Any] = []

    if normalized_group:
        filters.append("pb.group_name = ?")
        values.append(normalized_group)

    if normalized_blog_id is not None:
        filters.append("pb.id = ?")
        values.append(normalized_blog_id)

    now = utc_now_text()

    with closing(get_connection()) as connection:
        before = int(connection.total_changes)

        connection.execute(
            f"""
            INSERT INTO photo_review_queue (
                image_id,
                review_type,
                question,
                candidates,
                status,
                created_at,
                updated_at
            )
            SELECT
                pi.id,
                'person_identity',
                'この写真に写っている人物を確認してください。',
                '',
                'pending',
                ?,
                ?
            FROM photo_images pi
            JOIN photo_blogs pb
              ON pb.id = pi.blog_id
            WHERE {' AND '.join(filters)}
            """,
            (now, now, *values),
        )

        inserted = int(connection.total_changes) - before
        connection.commit()

    return max(0, inserted)


def get_person_reviews_by_status(
    status: str,
    limit: int = 100,
    group_name: str = "",
    blog_id: int | None = None,
) -> list[dict[str, Any]]:
    """指定状態の人物レビューをDiscord画面用の情報付きで返す。

    group_name を指定した場合は、その坂道グループのブログだけに絞り込む。
    """
    normalized_status = str(status or "").strip().lower()
    allowed_statuses = {"pending", "skipped"}
    if normalized_status not in allowed_statuses:
        raise ValueError(
            f"Unsupported review status: {normalized_status!r}. "
            f"Allowed: {sorted(allowed_statuses)}"
        )

    # pending を読む前に、進捗上は未確認だがキュー未作成だった画像を補完する。
    # skipped の再確認では既存 skipped 行だけを対象にする。
    if normalized_status == "pending":
        ensure_pending_person_review_queue(group_name, blog_id)

    safe_limit = max(1, min(int(limit), 500))
    normalized_group = str(group_name or "").strip()
    normalized_blog_id = int(blog_id) if blog_id is not None else None

    filters: list[str] = []
    values: list[Any] = [normalized_status]
    if normalized_group:
        filters.append("photo_blogs.group_name = ?")
        values.append(normalized_group)
    if normalized_blog_id is not None:
        filters.append("photo_blogs.id = ?")
        values.append(normalized_blog_id)

    extra_filter = ""
    if filters:
        extra_filter = " AND " + " AND ".join(filters)
    values.append(safe_limit)
    params = tuple(values)

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                photo_review_queue.id AS review_id,
                photo_review_queue.image_id,
                photo_review_queue.status AS review_status,
                photo_review_queue.question,
                photo_review_queue.candidates,
                photo_review_queue.review_note,
                photo_images.image_url,
                photo_images.local_path,
                photo_images.image_index,
                photo_images.blog_id,
                (
                    SELECT COUNT(*)
                    FROM photo_images AS blog_images
                    WHERE blog_images.blog_id = photo_images.blog_id
                ) AS total_blog_images,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                COALESCE(photo_ai_analysis.person_name, '') AS ai_person_name,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(person_name, '、')
                        FROM photo_image_people
                        WHERE photo_image_people.image_id = photo_images.id
                          AND relation_status = 'candidate'
                    ),
                    ''
                ) AS candidate_people,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(person_name, '、')
                        FROM photo_image_people
                        WHERE photo_image_people.image_id = photo_images.id
                          AND relation_status = 'confirmed'
                    ),
                    ''
                ) AS confirmed_people
            FROM photo_review_queue
            JOIN photo_images
                ON photo_images.id = photo_review_queue.image_id
            JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_ai_analysis
                ON photo_ai_analysis.image_id = photo_images.id
            WHERE photo_review_queue.status = ?
              AND photo_review_queue.review_type = 'person_identity'
              AND NOT EXISTS (
                  SELECT 1
                  FROM photo_image_people confirmed_review
                  WHERE confirmed_review.image_id = photo_images.id
                    AND confirmed_review.relation_status = 'confirmed'
              )
              AND COALESCE(photo_blogs.is_hidden, 0) = 0
              {extra_filter}
            ORDER BY photo_blogs.id ASC, photo_images.image_index ASC, photo_review_queue.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return rows_to_dicts(rows)


def get_pending_person_reviews(
    limit: int = 100,
    group_name: str = "",
    blog_id: int | None = None,
) -> list[dict[str, Any]]:
    """未確認の人物レビューを返す。"""
    return get_person_reviews_by_status("pending", limit, group_name, blog_id)


def get_skipped_person_reviews(
    limit: int = 100,
    group_name: str = "",
    blog_id: int | None = None,
) -> list[dict[str, Any]]:
    """過去にスキップした人物レビューを返す。"""
    return get_person_reviews_by_status("skipped", limit, group_name, blog_id)


def set_confirmed_blog_people(
    blog_id: int,
    person_names: list[str],
    *,
    confirmed_by: str = "",
    note: str = "",
    statuses: tuple[str, ...] = ("pending",),
) -> int:
    """同じブログの対象レビューを、指定人物でまとめて確定する。

    既に完了済みの画像は変更しない。戻り値は今回確定した画像数。
    """
    allowed = tuple(status for status in statuses if status in {"pending", "skipped"})
    if not allowed:
        return 0

    placeholders = ",".join("?" for _ in allowed)
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT photo_review_queue.image_id
            FROM photo_review_queue
            JOIN photo_images ON photo_images.id = photo_review_queue.image_id
            WHERE photo_images.blog_id = ?
              AND photo_review_queue.review_type = 'person_identity'
              AND photo_review_queue.status IN ({placeholders})
            ORDER BY photo_images.image_index ASC
            """,
            (int(blog_id), *allowed),
        ).fetchall()

    image_ids = [int(row["image_id"]) for row in rows]
    for image_id in image_ids:
        set_confirmed_image_people(
            image_id,
            person_names,
            confirmed_by=confirmed_by,
            note=note,
        )
    return len(image_ids)



def get_blog_images_for_review_admin(blog_id: int) -> list[dict[str, Any]]:
    """ブログ内の全写真を人物確認用の情報付きで返す。完了・未確認・スキップを含む。"""
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT
                pi.id AS image_id,
                pi.image_url,
                pi.local_path,
                pi.image_index,
                pi.blog_id,
                pb.blog_url,
                pb.group_name,
                pb.member_name,
                pb.title,
                pb.published_at,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM photo_image_people confirmed_status
                        WHERE confirmed_status.image_id = pi.id
                          AND confirmed_status.relation_status = 'confirmed'
                    ) THEN 'completed'
                    ELSE COALESCE(prq.status, 'pending')
                END AS review_status,
                COALESCE(prq.candidates, '') AS candidates,
                COALESCE(prq.review_note, '') AS review_note,
                COALESCE(paa.person_name, '') AS ai_person_name,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = pi.id AND pip.relation_status = 'candidate'
                ), '') AS candidate_people,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = pi.id AND pip.relation_status = 'confirmed'
                ), '') AS confirmed_people,
                (SELECT COUNT(*) FROM photo_images x WHERE x.blog_id = pi.blog_id) AS total_blog_images
            FROM photo_images pi
            JOIN photo_blogs pb ON pb.id = pi.blog_id
            LEFT JOIN photo_review_queue prq
              ON prq.image_id = pi.id AND prq.review_type = 'person_identity'
            LEFT JOIN photo_ai_analysis paa ON paa.image_id = pi.id
            WHERE pi.blog_id = ?
            ORDER BY pi.image_index ASC, pi.id ASC
            """,
            (int(blog_id),),
        ).fetchall()
    return rows_to_dicts(rows)


def get_previous_confirmed_people_in_blog(blog_id: int, image_index: int) -> list[str]:
    """同じブログで現在写真より前に確定した、直近写真の人物名を返す。"""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT pi.id
            FROM photo_images pi
            WHERE pi.blog_id = ? AND pi.image_index < ?
              AND EXISTS (
                  SELECT 1 FROM photo_image_people pip
                  WHERE pip.image_id = pi.id AND pip.relation_status = 'confirmed'
              )
            ORDER BY pi.image_index DESC, pi.id DESC
            LIMIT 1
            """,
            (int(blog_id), int(image_index)),
        ).fetchone()
        if row is None:
            return []
        names = connection.execute(
            """SELECT person_name FROM photo_image_people
               WHERE image_id = ? AND relation_status = 'confirmed'
               ORDER BY id ASC""",
            (int(row['id']),),
        ).fetchall()
    return [str(item['person_name']).strip() for item in names if str(item['person_name']).strip()]


# =========================
# ブログ除外管理
# =========================

HIDDEN_REASON_LABELS = {
    "UNKNOWN_MEMBER": "投稿者不明",
    "INVALID_URL": "URL異常",
    "NO_IMAGES": "画像なし",
    "SCRAPE_ERROR": "取得・解析失敗",
    "NOT_A_BLOG": "ブログ以外のデータ",
    "DUPLICATE": "重複データ",
    "MANUAL_HIDE": "管理者による除外",
}


def get_photo_blog_for_admin_edit(blog_id: int) -> dict[str, Any] | None:
    """管理者編集用に、除外状態を問わずブログ情報と関連画像数を返す。"""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT pb.*, COUNT(DISTINCT pi.id) AS image_count
            FROM photo_blogs pb
            LEFT JOIN photo_images pi ON pi.blog_id = pb.id
            WHERE pb.id = ?
            GROUP BY pb.id
            """,
            (int(blog_id),),
        ).fetchone()
    return row_to_dict(row)


def update_photo_blog_info_for_admin(
    blog_id: int,
    *,
    group_name: str | None = None,
    member_name: str | None = None,
    title: str | None = None,
    published_at: str | None = None,
    restore_if_hidden: bool = False,
) -> dict[str, Any] | None:
    """ブログ情報を安全に更新し、変更前後と関連画像数を返す。

    ``None`` の項目は変更しない。投稿者を設定して復元する場合は
    ``restore_if_hidden=True`` を指定する。
    """
    before = get_photo_blog_for_admin_edit(int(blog_id))
    if not before:
        return None

    assignments: list[str] = []
    params: list[Any] = []
    for column, value, max_len in (
        ("group_name", group_name, 100),
        ("member_name", member_name, 100),
        ("title", title, 500),
        ("published_at", published_at, 100),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            params.append(str(value).strip()[:max_len])

    if restore_if_hidden:
        assignments.extend([
            "is_hidden = 0",
            "hidden_reason = ''",
            "hidden_note = ''",
            "hidden_at = ''",
            "hidden_by = ''",
        ])

    if not assignments:
        return {"before": before, "after": before, "image_count": int(before.get("image_count") or 0)}

    assignments.append("updated_at = ?")
    params.append(utc_now_text())
    params.append(int(blog_id))

    with closing(get_connection()) as connection:
        connection.execute(
            f"UPDATE photo_blogs SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        connection.commit()

    after = get_photo_blog_for_admin_edit(int(blog_id))
    return {
        "before": before,
        "after": after or {},
        "image_count": int((after or before).get("image_count") or 0),
    }


def hide_photo_blog(
    blog_id: int,
    reason: str,
    *,
    hidden_by: str = "",
    note: str = "",
) -> bool:
    """記事データを削除せず、管理者の人物確認対象から除外する。"""
    clean_reason = str(reason or "MANUAL_HIDE").strip().upper()
    if clean_reason not in HIDDEN_REASON_LABELS:
        clean_reason = "MANUAL_HIDE"
    now = utc_now_text()
    with closing(get_connection()) as connection:
        cur = connection.execute(
            """
            UPDATE photo_blogs
            SET is_hidden = 1,
                hidden_reason = ?,
                hidden_note = ?,
                hidden_at = ?,
                hidden_by = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (clean_reason, str(note or "")[:500], now, str(hidden_by or "")[:100], now, int(blog_id)),
        )
        connection.commit()
        return int(cur.rowcount or 0) > 0


def restore_hidden_photo_blog(blog_id: int) -> bool:
    """除外済み記事を人物確認対象へ戻す。"""
    now = utc_now_text()
    with closing(get_connection()) as connection:
        cur = connection.execute(
            """
            UPDATE photo_blogs
            SET is_hidden = 0,
                hidden_reason = '',
                hidden_note = '',
                hidden_at = '',
                hidden_by = '',
                updated_at = ?
            WHERE id = ?
            """,
            (now, int(blog_id)),
        )
        connection.commit()
        return int(cur.rowcount or 0) > 0


def list_hidden_photo_blogs(limit: int = 25, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    safe_limit = max(1, min(int(limit), 25))
    safe_offset = max(0, int(offset))
    with closing(get_connection()) as connection:
        total_row = connection.execute(
            "SELECT COUNT(*) AS total FROM photo_blogs WHERE COALESCE(is_hidden, 0) = 1"
        ).fetchone()
        rows = connection.execute(
            """
            SELECT pb.*,
                   COUNT(DISTINCT pi.id) AS image_count,
                   SUM(CASE WHEN pi.download_status = 'failed' THEN 1 ELSE 0 END) AS download_errors,
                   SUM(CASE WHEN pi.analysis_status = 'failed' THEN 1 ELSE 0 END) AS analysis_errors
            FROM photo_blogs pb
            LEFT JOIN photo_images pi ON pi.blog_id = pb.id
            WHERE COALESCE(pb.is_hidden, 0) = 1
            GROUP BY pb.id
            ORDER BY CASE WHEN pb.hidden_at = '' THEN 1 ELSE 0 END, pb.hidden_at DESC, pb.id DESC
            LIMIT ? OFFSET ?
            """,
            (safe_limit, safe_offset),
        ).fetchall()
    return rows_to_dicts(rows), int(total_row["total"] if total_row else 0)


def get_hidden_photo_blog(blog_id: int) -> dict[str, Any] | None:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT pb.*,
                   COUNT(DISTINCT pi.id) AS image_count,
                   SUM(CASE WHEN pi.download_status = 'failed' THEN 1 ELSE 0 END) AS download_errors,
                   SUM(CASE WHEN pi.analysis_status = 'failed' THEN 1 ELSE 0 END) AS analysis_errors
            FROM photo_blogs pb
            LEFT JOIN photo_images pi ON pi.blog_id = pb.id
            WHERE pb.id = ? AND COALESCE(pb.is_hidden, 0) = 1
            GROUP BY pb.id
            """,
            (int(blog_id),),
        ).fetchone()
    return row_to_dict(row)


def queue_hidden_blog_reanalysis(blog_id: int) -> dict[str, int]:
    """除外状態を保ったまま、取得・AI解析を再試行待ちへ戻す。"""
    now = utc_now_text()
    with closing(get_connection()) as connection:
        download = connection.execute(
            """
            UPDATE photo_images
            SET download_status = CASE
                    WHEN download_status IN ('failed', 'pending') THEN 'pending'
                    ELSE download_status END,
                download_error = CASE WHEN download_status = 'failed' THEN '' ELSE download_error END,
                updated_at = ?
            WHERE blog_id = ? AND download_status NOT IN ('invalid_url', 'permanent_failed')
            """,
            (now, int(blog_id)),
        )
        analysis = connection.execute(
            """
            UPDATE photo_images
            SET analysis_status = 'pending', analysis_error = '', updated_at = ?
            WHERE blog_id = ? AND download_status = 'completed'
            """,
            (now, int(blog_id)),
        )
        connection.commit()
    return {"download": max(0, int(download.rowcount or 0)), "analysis": max(0, int(analysis.rowcount or 0))}


def delete_hidden_photo_blog(blog_id: int) -> bool:
    """除外済み記事だけを完全削除する。画像・関連行も外部キーで削除される。"""
    with closing(get_connection()) as connection:
        row = connection.execute(
            "SELECT id FROM photo_blogs WHERE id = ? AND COALESCE(is_hidden, 0) = 1",
            (int(blog_id),),
        ).fetchone()
        if row is None:
            return False
        cur = connection.execute("DELETE FROM photo_blogs WHERE id = ?", (int(blog_id),))
        connection.commit()
        return int(cur.rowcount or 0) > 0

# =========================
# 単体実行テスト
# =========================

if __name__ == "__main__":

    init_photo_db()

    counts = get_photo_db_counts()

    print("=" * 50)
    print("写真検索DB状態")
    print(f"ブログ: {counts['blogs']}件")
    print(f"画像: {counts['images']}件")
    print(f"AIタグ: {counts['ai_tags']}件")
    print(f"手動タグ: {counts['manual_tags']}件")
    print(f"画像確認待ち: {counts['pending_reviews']}件")
    print(f"お気に入り: {counts['favorites']}件")
    print("-" * 50)
    print(f"人物マスター: {counts['people']}人")
    print(f"検出された顔: {counts['faces']}件")
    print(f"人物確定済みの顔: {counts['confirmed_faces']}件")
    print(f"人物候補: {counts['face_candidates']}件")
    print(
        "顔確認待ち:",
        f"{counts['pending_face_reviews']}件",
    )
    print("=" * 50)



# =========================
# AI料金削減・使用量記録
# =========================

def find_reusable_analysis_by_hash(
    image_id: int,
    image_hash: str,
) -> dict[str, Any] | None:
    """同一ハッシュの解析済み画像を1件返す。"""

    normalized_hash = str(image_hash or "").strip()
    if not normalized_hash:
        return None

    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT source.id AS source_image_id
            FROM photo_images AS source
            JOIN photo_ai_analysis AS analysis
              ON analysis.image_id = source.id
            WHERE source.id <> ?
              AND source.image_hash = ?
              AND source.analysis_status IN ('completed', 'review')
            ORDER BY
                CASE source.analysis_status
                    WHEN 'completed' THEN 0
                    ELSE 1
                END,
                source.id ASC
            LIMIT 1
            """,
            (int(image_id), normalized_hash),
        ).fetchone()

    return row_to_dict(row)


def copy_ai_result(
    source_image_id: int,
    target_image_id: int,
) -> bool:
    """解析結果とAIタグを別画像へコピーする。"""

    now = utc_now_text()

    with closing(get_connection()) as connection:
        source = connection.execute(
            """
            SELECT *
            FROM photo_ai_analysis
            WHERE image_id = ?
            """,
            (int(source_image_id),),
        ).fetchone()

        if source is None:
            return False

        connection.execute(
            "DELETE FROM photo_ai_analysis WHERE image_id = ?",
            (int(target_image_id),),
        )
        connection.execute(
            "DELETE FROM photo_ai_tags WHERE image_id = ?",
            (int(target_image_id),),
        )

        connection.execute(
            """
            INSERT INTO photo_ai_analysis (
                image_id, model_name, raw_response,
                person_name, clothing, expression,
                background, pose, objects, person_count,
                overall_confidence, needs_review,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(target_image_id),
                str(source["model_name"] or ""),
                str(source["raw_response"] or ""),
                str(source["person_name"] or ""),
                str(source["clothing"] or ""),
                str(source["expression"] or ""),
                str(source["background"] or ""),
                str(source["pose"] or ""),
                str(source["objects"] or ""),
                int(source["person_count"] or 0),
                float(source["overall_confidence"] or 0),
                int(source["needs_review"] or 0),
                now,
                now,
            ),
        )

        tags = connection.execute(
            """
            SELECT category, tag, confidence, model_name, raw_value
            FROM photo_ai_tags
            WHERE image_id = ?
            """,
            (int(source_image_id),),
        ).fetchall()

        for tag in tags:
            connection.execute(
                """
                INSERT OR IGNORE INTO photo_ai_tags (
                    image_id, category, tag, confidence,
                    model_name, raw_value, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(target_image_id),
                    str(tag["category"] or ""),
                    str(tag["tag"] or ""),
                    float(tag["confidence"] or 0),
                    str(tag["model_name"] or ""),
                    str(tag["raw_value"] or ""),
                    now,
                    now,
                ),
            )

        final_status = (
            "review"
            if int(source["needs_review"] or 0)
            else "completed"
        )
        connection.execute(
            """
            UPDATE photo_images
            SET analysis_status = ?, analysis_error = '', updated_at = ?
            WHERE id = ?
            """,
            (final_status, now, int(target_image_id)),
        )
        connection.commit()

    return True


def save_ai_usage(
    *,
    image_id: int | None,
    source_image_id: int | None = None,
    model_name: str = "",
    request_kind: str = "api",
    status: str = "completed",
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    input_cost_usd: float = 0.0,
    cached_input_cost_usd: float = 0.0,
    output_cost_usd: float = 0.0,
    estimated_cost_usd: float = 0.0,
    response_id: str = "",
    error_type: str = "",
) -> None:
    """AI API使用量または重複再利用実績を保存する。"""

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO photo_ai_usage (
                image_id, source_image_id, model_name,
                request_kind, status,
                input_tokens, cached_input_tokens,
                output_tokens, total_tokens,
                input_cost_usd, cached_input_cost_usd,
                output_cost_usd, estimated_cost_usd,
                response_id, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                source_image_id,
                str(model_name or ""),
                str(request_kind or "api"),
                str(status or "completed"),
                max(int(input_tokens or 0), 0),
                max(int(cached_input_tokens or 0), 0),
                max(int(output_tokens or 0), 0),
                max(int(total_tokens or 0), 0),
                max(float(input_cost_usd or 0), 0.0),
                max(float(cached_input_cost_usd or 0), 0.0),
                max(float(output_cost_usd or 0), 0.0),
                max(float(estimated_cost_usd or 0), 0.0),
                str(response_id or ""),
                str(error_type or ""),
                utc_now_text(),
            ),
        )
        connection.commit()


def get_ai_cost_summary(days: int | None = None) -> dict[str, Any]:
    """AI使用量と推定料金をモデル別に集計する。"""

    where_sql = ""
    params: tuple[Any, ...] = ()
    if days is not None:
        safe_days = max(int(days), 1)
        where_sql = "WHERE datetime(created_at) >= datetime('now', ?)"
        params = (f"-{safe_days} days",)

    with closing(get_connection()) as connection:
        total = connection.execute(
            f"""
            SELECT
                COUNT(*) AS records,
                SUM(CASE WHEN request_kind = 'api' THEN 1 ELSE 0 END) AS api_calls,
                SUM(CASE WHEN request_kind = 'cache_reuse' THEN 1 ELSE 0 END) AS reused,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
            FROM photo_ai_usage
            {where_sql}
            """,
            params,
        ).fetchone()

        models = connection.execute(
            f"""
            SELECT
                CASE
                    WHEN model_name = '' THEN '(再利用)'
                    ELSE model_name
                END AS model_name,
                COUNT(*) AS records,
                SUM(CASE WHEN request_kind = 'api' THEN 1 ELSE 0 END) AS api_calls,
                SUM(CASE WHEN request_kind = 'cache_reuse' THEN 1 ELSE 0 END) AS reused,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
            FROM photo_ai_usage
            {where_sql}
            GROUP BY model_name
            ORDER BY estimated_cost_usd DESC, records DESC
            """,
            params,
        ).fetchall()

    return {
        "days": days,
        "total": row_to_dict(total) or {},
        "models": rows_to_dicts(models),
    }

# =========================
# 高信頼度顔レビュー一括処理
# =========================

def get_high_confidence_pending_face_reviews(
    limit: int = 20,
    min_confidence: float = 0.95,
) -> list[dict[str, Any]]:
    """確認待ちの顔から、1位候補が指定信頼度以上の項目を取得する。"""
    limit = max(1, min(int(limit), 100))
    min_confidence = max(0.0, min(float(min_confidence), 1.0))

    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            SELECT
                photo_face_reviews.id AS review_id,
                photo_face_reviews.face_id,
                photo_faces.image_id,
                photo_faces.face_index,
                photo_face_candidates.person_id,
                photo_face_candidates.confidence,
                photo_face_candidates.candidate_rank,
                photo_people.person_name,
                photo_people.group_name AS person_group_name,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                photo_blogs.blog_url
            FROM photo_face_reviews
            INNER JOIN photo_faces
                ON photo_face_reviews.face_id = photo_faces.id
            INNER JOIN photo_face_candidates
                ON photo_face_reviews.face_id = photo_face_candidates.face_id
            INNER JOIN photo_people
                ON photo_face_candidates.person_id = photo_people.id
            INNER JOIN photo_images
                ON photo_faces.image_id = photo_images.id
            INNER JOIN photo_blogs
                ON photo_images.blog_id = photo_blogs.id
            WHERE photo_face_reviews.status = 'pending'
              AND photo_face_candidates.candidate_rank = 1
              AND photo_face_candidates.confidence >= ?
            ORDER BY
                photo_face_candidates.confidence DESC,
                photo_face_reviews.id ASC
            LIMIT ?
            """,
            (min_confidence, limit),
        )
        return rows_to_dicts(cursor.fetchall())


def get_person_pending_face_reviews(
    person_name: str,
    limit: int = 50,
    min_confidence: float = 0.90,
) -> list[dict[str, Any]]:
    """指定人物が1位候補の確認待ち顔を、信頼度順に取得する。"""
    person_name = str(person_name or "").strip()
    if not person_name:
        return []

    limit = max(1, min(int(limit), 100))
    min_confidence = max(0.0, min(float(min_confidence), 1.0))

    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            SELECT
                photo_face_reviews.id AS review_id,
                photo_face_reviews.face_id,
                photo_faces.image_id,
                photo_faces.face_index,
                photo_face_candidates.person_id,
                photo_face_candidates.confidence,
                photo_face_candidates.candidate_rank,
                photo_people.person_name,
                photo_people.group_name AS person_group_name,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                photo_blogs.blog_url
            FROM photo_face_reviews
            INNER JOIN photo_faces
                ON photo_face_reviews.face_id = photo_faces.id
            INNER JOIN photo_face_candidates
                ON photo_face_reviews.face_id = photo_face_candidates.face_id
            INNER JOIN photo_people
                ON photo_face_candidates.person_id = photo_people.id
            INNER JOIN photo_images
                ON photo_faces.image_id = photo_images.id
            INNER JOIN photo_blogs
                ON photo_images.blog_id = photo_blogs.id
            WHERE photo_face_reviews.status = 'pending'
              AND photo_face_candidates.candidate_rank = 1
              AND photo_face_candidates.confidence >= ?
              AND photo_people.person_name = ?
            ORDER BY
                photo_face_candidates.confidence DESC,
                photo_face_reviews.id ASC
            LIMIT ?
            """,
            (min_confidence, person_name, limit),
        )
        return rows_to_dicts(cursor.fetchall())


def complete_face_reviews_bulk(
    items: list[dict[str, Any]],
    reviewed_by: str = "",
    review_note: str = "",
) -> int:
    """顔レビューを1トランザクションで一括確定する。"""
    if not items:
        return 0

    now = utc_now_text()
    completed = 0

    with closing(get_connection()) as connection:
        for item in items:
            face_id = int(item["face_id"])
            person_id = int(item["person_id"])

            cursor = connection.execute(
                """
                UPDATE photo_face_reviews
                SET status = 'completed',
                    selected_person_id = ?,
                    reviewed_by = ?,
                    review_note = ?,
                    reviewed_at = ?,
                    updated_at = ?
                WHERE face_id = ?
                  AND status = 'pending'
                """,
                (person_id, reviewed_by, review_note, now, now, face_id),
            )
            if cursor.rowcount <= 0:
                continue

            connection.execute(
                """
                UPDATE photo_faces
                SET confirmed_person_id = ?,
                    confirmation_status = 'manually_confirmed',
                    confirmed_by = ?,
                    confirmed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (person_id, reviewed_by, now, now, face_id),
            )
            completed += 1

        connection.commit()

    # 一括確定も同じ誤学習防止ポリシーを通す。
    try:
        from face_candidate_scoring import register_confirmed_face_learning
        for item in items:
            register_confirmed_face_learning(
                int(item["face_id"]), int(item["person_id"]), source="bulk_manual_review"
            )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("一括確定顔の安全学習反映に失敗しました")

    return completed

# =========================
# ZIP42: 管理画面・推定人物検索・ブログ単位処理
# =========================

def search_photo_images_by_person_with_candidates(person_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """確認済みに加え、AI人物候補・顔認証候補も含めて人物検索する。"""
    clean_name = str(person_name or '').strip()
    if not clean_name:
        return []
    pattern = f'%{clean_name}%'
    return _search_photo_images_with_where(
        """
        EXISTS (
            SELECT 1 FROM photo_image_people pip
            WHERE pip.image_id = photo_images.id
              AND pip.person_name LIKE ?
              AND pip.relation_status IN ('confirmed', 'candidate')
        )
        OR EXISTS (
            SELECT 1
            FROM photo_faces pf
            JOIN photo_face_candidates pfc ON pfc.face_id = pf.id
            JOIN photo_people pp ON pp.id = pfc.person_id
            WHERE pf.image_id = photo_images.id
              AND pp.person_name LIKE ?
              AND pfc.candidate_rank = 1
        )
        """,
        (pattern, pattern),
        limit=limit,
    )


def get_blog_authors_for_admin(group_name: str = '', limit: int = 500) -> list[dict[str, Any]]:
    """投稿者ごとの記事数と人物確認完了記事数を返す。

    記事内の全画像について人物確認が完了している場合のみ、その記事を
    「完了」として数える。画像0件の記事は未完了扱いにする。
    """
    group_name = str(group_name or '').strip()
    params: list[Any] = []
    where = "WHERE COALESCE(pb.is_hidden, 0) = 0 AND TRIM(COALESCE(pb.member_name, '')) <> '' AND TRIM(COALESCE(pb.member_name, '')) NOT IN ('不明', '投稿者不明')"
    if group_name:
        where += " AND pb.group_name = ?"
        params.append(group_name)
    params.append(max(1, min(int(limit), 500)))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            WITH blog_progress AS (
                SELECT
                    pb.id,
                    pb.group_name,
                    pb.member_name,
                    pb.published_at,
                    COUNT(DISTINCT pi.id) AS image_count,
                    COUNT(DISTINCT CASE
                        WHEN prq.review_type = 'person_identity'
                         AND prq.status = 'completed' THEN pi.id
                    END) AS completed_image_count
                FROM photo_blogs pb
                LEFT JOIN photo_images pi ON pi.blog_id = pb.id
                LEFT JOIN photo_review_queue prq ON prq.image_id = pi.id
                {where}
                GROUP BY pb.id
            )
            SELECT
                member_name,
                group_name,
                COUNT(*) AS blog_count,
                SUM(CASE
                    WHEN image_count > 0 AND completed_image_count >= image_count THEN 1
                    ELSE 0
                END) AS completed_blog_count,
                SUM(CASE
                    WHEN image_count = 0 OR completed_image_count < image_count THEN 1
                    ELSE 0
                END) AS pending_blog_count,
                MAX(published_at) AS latest_published_at
            FROM blog_progress
            GROUP BY group_name, member_name
            ORDER BY latest_published_at DESC, member_name ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    result = rows_to_dicts(rows)
    for item in result:
        total = int(item.get('blog_count') or 0)
        completed = int(item.get('completed_blog_count') or 0)
        item['pending_blog_count'] = max(0, int(item.get('pending_blog_count') or (total - completed)))
        item['completion_percent'] = 0 if total == 0 else round(completed * 100 / total)
    return result


def get_blogs_for_admin(group_name: str, member_name: str, limit: int = 25) -> list[dict[str, Any]]:
    """互換用: 投稿者の記事を新しい順に返す。"""
    blogs, _ = get_blogs_for_admin_filtered(
        group_name,
        member_name,
        limit=limit,
        offset=0,
    )
    return blogs


def get_blogs_for_admin_filtered(
    group_name: str,
    member_name: str,
    *,
    limit: int = 25,
    offset: int = 0,
    year: int | None = None,
    month: int | None = None,
    title_query: str = "",
    only_unprocessed: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """投稿者の記事を人物確認進捗付きで絞り込み・ページ取得する。

    Discordのセレクトメニュー上限（25件）を超える記事は、offsetを使って
    ページ送りできる。戻り値は ``(記事一覧, 絞り込み後の総件数)``。
    """
    safe_limit = max(1, min(int(limit), 25))
    safe_offset = max(0, int(offset))
    filters = ["COALESCE(pb.is_hidden, 0) = 0", "pb.group_name = ?", "pb.member_name = ?"]
    params: list[Any] = [str(group_name), str(member_name)]

    if year:
        filters.append("substr(pb.published_at, 1, 4) = ?")
        params.append(f"{int(year):04d}")
    if month:
        # 保存済みブログには複数の日付形式が混在するため、
        # 2026.7 / 2026/07 / 2026-07 / 2026年7月 / 2026年07月
        # のすべてを月フィルター対象にする。
        month_value = int(month)
        filters.append(
            "("
            "pb.published_at LIKE ? OR pb.published_at LIKE ? OR "
            "pb.published_at LIKE ? OR pb.published_at LIKE ? OR "
            "pb.published_at LIKE ? OR pb.published_at LIKE ? OR "
            "pb.published_at LIKE ? OR pb.published_at LIKE ?"
            ")"
        )
        params.extend([
            f"____.{month_value}.%", f"____.{month_value:02d}.%",
            f"____/{month_value}/%", f"____/{month_value:02d}/%",
            f"____-{month_value}-%", f"____-{month_value:02d}-%",
            f"____年{month_value}月%", f"____年{month_value:02d}月%",
        ])
    clean_query = str(title_query or "").strip()
    if clean_query:
        filters.append("pb.title LIKE ?")
        params.append(f"%{clean_query}%")

    where_sql = " WHERE " + " AND ".join(filters)
    having_sql = ""
    if only_unprocessed:
        having_sql = """
        HAVING COUNT(DISTINCT pi.id) > 0
           AND COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity'
                 AND prq.status = 'completed' THEN prq.image_id
           END) < COUNT(DISTINCT pi.id)
        """

    grouped_sql = _blog_admin_progress_select() + where_sql + " GROUP BY pb.id " + having_sql
    page_sql = grouped_sql + """
        ORDER BY
            CASE WHEN pb.published_at = '' THEN 1 ELSE 0 END,
            pb.published_at DESC,
            pb.id DESC
        LIMIT ? OFFSET ?
    """
    count_sql = "SELECT COUNT(*) AS total FROM (" + grouped_sql + ") AS filtered_blogs"

    with closing(get_connection()) as connection:
        total_row = connection.execute(count_sql, tuple(params)).fetchone()
        rows = connection.execute(
            page_sql,
            tuple(params + [safe_limit, safe_offset]),
        ).fetchall()

    total = int(total_row["total"] if total_row else 0)
    return _normalize_blog_admin_rows(rows), total


def get_blog_years_for_admin(group_name: str, member_name: str) -> list[int]:
    """投稿者の記事に存在する年を新しい順で返す。"""
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT CAST(substr(published_at, 1, 4) AS INTEGER) AS year
            FROM photo_blogs
            WHERE group_name = ?
              AND member_name = ?
              AND length(published_at) >= 4
              AND substr(published_at, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'
            ORDER BY year DESC
            """,
            (str(group_name), str(member_name)),
        ).fetchall()
    return [int(row["year"]) for row in rows if int(row["year"] or 0) > 0]


def _ensure_admin_blog_browser_state(connection: Any) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_admin_blog_browser_state (
            user_id INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            member_name TEXT NOT NULL,
            page INTEGER NOT NULL DEFAULT 0,
            selected_year INTEGER NOT NULL DEFAULT 0,
            selected_month INTEGER NOT NULL DEFAULT 0,
            title_query TEXT NOT NULL DEFAULT '',
            only_unprocessed INTEGER NOT NULL DEFAULT 0,
            last_blog_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, group_name, member_name)
        )
        """
    )


def save_admin_blog_browser_state(
    user_id: int,
    group_name: str,
    member_name: str,
    *,
    page: int = 0,
    selected_year: int = 0,
    selected_month: int = 0,
    title_query: str = "",
    only_unprocessed: bool = False,
    last_blog_id: int = 0,
) -> None:
    """管理者ごとの記事ブラウザー位置を保存する。"""
    now = utc_now_text()
    with closing(get_connection()) as connection:
        _ensure_admin_blog_browser_state(connection)
        connection.execute(
            """
            INSERT INTO photo_admin_blog_browser_state (
                user_id, group_name, member_name, page,
                selected_year, selected_month, title_query,
                only_unprocessed, last_blog_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, group_name, member_name) DO UPDATE SET
                page = excluded.page,
                selected_year = excluded.selected_year,
                selected_month = excluded.selected_month,
                title_query = excluded.title_query,
                only_unprocessed = excluded.only_unprocessed,
                last_blog_id = CASE
                    WHEN excluded.last_blog_id > 0 THEN excluded.last_blog_id
                    ELSE photo_admin_blog_browser_state.last_blog_id
                END,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id), str(group_name), str(member_name), max(0, int(page)),
                max(0, int(selected_year)), max(0, int(selected_month)),
                str(title_query or "")[:200], 1 if only_unprocessed else 0,
                max(0, int(last_blog_id)), now,
            ),
        )
        connection.commit()


def get_admin_blog_browser_state(user_id: int, group_name: str, member_name: str) -> dict[str, Any] | None:
    """管理者ごとの前回の記事ブラウザー位置を返す。"""
    with closing(get_connection()) as connection:
        _ensure_admin_blog_browser_state(connection)
        row = connection.execute(
            """
            SELECT * FROM photo_admin_blog_browser_state
            WHERE user_id = ? AND group_name = ? AND member_name = ?
            """,
            (int(user_id), str(group_name), str(member_name)),
        ).fetchone()
        connection.commit()
    return dict(row) if row else None


def get_blog_image_ids(blog_id: int, *, only_unanalyzed: bool = False, only_unscanned: bool = False) -> list[int]:
    clauses = ["pi.blog_id = ?", "pi.download_status = 'completed'", "(pi.local_path <> '' OR pi.bucket_key <> '')"]
    params: list[Any] = [int(blog_id)]
    if only_unanalyzed:
        clauses.append("pi.analysis_status NOT IN ('completed','review')")
    if only_unscanned:
        clauses.append("(pfs.image_id IS NULL OR pfs.status <> 'completed')")
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT pi.id
            FROM photo_images pi
            LEFT JOIN photo_face_scans pfs ON pfs.image_id = pi.id
            WHERE {' AND '.join(clauses)}
            ORDER BY pi.image_index ASC, pi.id ASC
            """, tuple(params)
        ).fetchall()
    return [int(row['id']) for row in rows]

# =========================
# ZIP44 ブログ単位人物確認ダッシュボード
# =========================

def _blog_admin_progress_select() -> str:
    """ブログ記事ごとの人物確認進捗を集計する共通SELECT。"""
    return """
        SELECT
            pb.id,
            pb.group_name,
            pb.member_name,
            pb.title,
            pb.published_at,
            pb.blog_url,
            COUNT(DISTINCT pi.id) AS image_count,
            COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity' THEN prq.image_id
            END) AS review_target_count,
            COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity'
                 AND prq.status = 'completed' THEN prq.image_id
            END) AS review_completed_count,
            COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity'
                 AND prq.status = 'pending' THEN prq.image_id
            END) AS review_pending_count,
            COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity'
                 AND prq.status = 'skipped' THEN prq.image_id
            END) AS review_skipped_count,
            COUNT(DISTINCT CASE
                WHEN pi.download_status = 'failed' THEN pi.id
            END) AS download_error_count,
            COUNT(DISTINCT CASE
                WHEN pi.analysis_status = 'failed' THEN pi.id
            END) AS analysis_error_count,
            COUNT(DISTINCT CASE
                WHEN pfs.status = 'failed' THEN pi.id
            END) AS face_error_count,
            COUNT(DISTINCT CASE
                WHEN pi.download_status IN ('invalid_url', 'permanent_failed') THEN pi.id
            END) AS terminal_excluded_count,
            COUNT(DISTINCT CASE
                WHEN (
                    pi.download_status <> 'failed'
                    AND TRIM(COALESCE(pi.download_error, '')) <> ''
                ) OR (
                    pi.analysis_status <> 'failed'
                    AND TRIM(COALESCE(pi.analysis_error, '')) <> ''
                ) OR (
                    COALESCE(pfs.status, '') <> 'failed'
                    AND TRIM(COALESCE(pfs.error_message, '')) <> ''
                ) THEN pi.id
            END) AS stale_error_count,
            COUNT(DISTINCT CASE
                WHEN pi.download_status = 'failed'
                  OR pi.analysis_status = 'failed'
                  OR pfs.status = 'failed'
                THEN pi.id
            END) AS error_count,
            MAX(COALESCE(NULLIF(prq.reviewed_at, ''), NULLIF(prq.updated_at, ''), pi.updated_at, pb.updated_at)) AS last_reviewed_at
        FROM photo_blogs pb
        LEFT JOIN photo_images pi ON pi.blog_id = pb.id
        LEFT JOIN photo_review_queue prq ON prq.image_id = pi.id
        LEFT JOIN photo_face_scans pfs ON pfs.image_id = pi.id
    """


def _normalize_blog_admin_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result = rows_to_dicts(rows)
    for item in result:
        total = int(item.get("review_target_count") or 0)
        completed = int(item.get("review_completed_count") or 0)
        pending = int(item.get("review_pending_count") or 0)
        skipped = int(item.get("review_skipped_count") or 0)
        # レビュー行がまだ作られていない画像も未確認として扱う。
        # ただし不正URL・復旧不能など terminal 除外済み画像は人物確認対象に含めない。
        image_count = int(item.get("image_count") or 0)
        terminal = int(item.get("terminal_excluded_count") or 0)
        reviewable_image_count = max(0, image_count - terminal)
        effective_total = max(total, reviewable_image_count)
        effective_completed = min(completed, effective_total)
        unreviewed_without_queue = max(0, reviewable_image_count - total)
        effective_pending = pending + skipped + unreviewed_without_queue
        percent = 100 if effective_total == 0 else round(effective_completed * 100 / effective_total)
        item["progress_total"] = effective_total
        item["progress_completed"] = effective_completed
        item["progress_pending"] = effective_pending
        item["progress_percent"] = max(0, min(percent, 100))
        item["is_completed"] = effective_total > 0 and effective_completed >= effective_total
        item["is_unprocessed"] = effective_total > 0 and effective_completed < effective_total
    return result


def get_blog_workflow_stats_for_admin() -> dict[str, int]:
    """ブログ単位解析の実績を集計する。"""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            WITH per_blog AS (
                SELECT
                    pb.id,
                    COALESCE(pb.is_hidden, 0) AS is_hidden,
                    COALESCE(pb.hidden_reason, '') AS hidden_reason,
                    COUNT(DISTINCT CASE
                        WHEN pi.id IS NOT NULL
                         AND pi.download_status NOT IN ('invalid_url', 'permanent_failed')
                        THEN pi.id
                    END) AS reviewable_images,
                    COUNT(DISTINCT CASE
                        WHEN pi.analysis_status IN ('completed', 'review')
                         AND pi.download_status NOT IN ('invalid_url', 'permanent_failed')
                        THEN pi.id
                    END) AS ai_done_images,
                    COUNT(DISTINCT CASE
                        WHEN prq.review_type = 'person_identity'
                         AND prq.status = 'completed'
                        THEN prq.image_id
                    END) AS review_completed_images,
                    COUNT(DISTINCT CASE
                        WHEN prq.review_type = 'person_identity'
                         AND prq.status = 'skipped'
                        THEN prq.image_id
                    END) AS review_skipped_images
                FROM photo_blogs pb
                LEFT JOIN photo_images pi
                  ON pi.blog_id = pb.id
                LEFT JOIN photo_review_queue prq
                  ON prq.image_id = pi.id
                GROUP BY pb.id
            )
            SELECT
                SUM(CASE
                    WHEN is_hidden = 0 AND reviewable_images > 0
                    THEN 1 ELSE 0 END
                ) AS review_target_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND reviewable_images > 0
                     AND (review_completed_images > 0 OR review_skipped_images > 0)
                    THEN 1 ELSE 0 END
                ) AS review_started_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND reviewable_images > 0
                     AND review_completed_images >= reviewable_images
                    THEN 1 ELSE 0 END
                ) AS review_completed_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND reviewable_images > 0
                     AND review_completed_images = 0
                     AND review_skipped_images = 0
                    THEN 1 ELSE 0 END
                ) AS review_unstarted_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND review_skipped_images > 0
                    THEN 1 ELSE 0 END
                ) AS blogs_with_skipped_photos,
                SUM(CASE
                    WHEN is_hidden = 0
                    THEN review_skipped_images ELSE 0 END
                ) AS skipped_photos,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND ai_done_images > 0
                    THEN 1 ELSE 0 END
                ) AS ai_started_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                     AND reviewable_images > 0
                     AND ai_done_images >= reviewable_images
                    THEN 1 ELSE 0 END
                ) AS ai_completed_blogs,
                SUM(CASE
                    WHEN is_hidden = 0
                    THEN reviewable_images ELSE 0 END
                ) AS review_target_images,
                SUM(CASE
                    WHEN is_hidden = 1
                    THEN 1 ELSE 0 END
                ) AS hidden_blogs,
                SUM(CASE
                    WHEN is_hidden = 1
                     AND hidden_reason = 'MANUAL_HIDE'
                    THEN 1 ELSE 0 END
                ) AS manual_hidden_blogs
            FROM per_blog
            """
        ).fetchone()

    keys = (
        "review_target_blogs",
        "review_started_blogs",
        "review_completed_blogs",
        "review_unstarted_blogs",
        "blogs_with_skipped_photos",
        "skipped_photos",
        "ai_started_blogs",
        "ai_completed_blogs",
        "review_target_images",
        "hidden_blogs",
        "manual_hidden_blogs",
    )
    if row is None:
        return {key: 0 for key in keys}
    return {key: int(row[key] or 0) for key in keys}


def get_latest_blogs_for_admin(limit: int = 25) -> list[dict[str, Any]]:
    """最新記事を人物確認進捗付きで返す。"""
    safe_limit = max(1, min(int(limit), 25))
    sql = _blog_admin_progress_select() + """
        WHERE COALESCE(pb.is_hidden, 0) = 0
        GROUP BY pb.id
        ORDER BY
            CASE WHEN pb.published_at = '' THEN 1 ELSE 0 END,
            pb.published_at DESC,
            pb.id DESC
        LIMIT ?
    """
    with closing(get_connection()) as connection:
        rows = connection.execute(sql, (safe_limit,)).fetchall()
    return _normalize_blog_admin_rows(rows)


def get_unprocessed_blogs_for_admin(limit: int = 25) -> list[dict[str, Any]]:
    """人物確認が未完了の記事を返す。"""
    safe_limit = max(1, min(int(limit), 25))
    sql = _blog_admin_progress_select() + """
        WHERE COALESCE(pb.is_hidden, 0) = 0
        GROUP BY pb.id
        HAVING COUNT(DISTINCT pi.id) > 0
           AND COUNT(DISTINCT CASE
                WHEN prq.review_type = 'person_identity'
                 AND prq.status = 'completed' THEN prq.image_id
           END) < COUNT(DISTINCT pi.id)
        ORDER BY
            CASE WHEN pb.published_at = '' THEN 1 ELSE 0 END,
            pb.published_at DESC,
            pb.id DESC
        LIMIT ?
    """
    with closing(get_connection()) as connection:
        rows = connection.execute(sql, (safe_limit,)).fetchall()
    return _normalize_blog_admin_rows(rows)


def get_error_blogs_for_admin(limit: int = 25) -> list[dict[str, Any]]:
    """画像取得・AI解析・顔認証にエラーがある記事を返す。"""
    safe_limit = max(1, min(int(limit), 25))
    sql = _blog_admin_progress_select() + """
        WHERE COALESCE(pb.is_hidden, 0) = 0
        GROUP BY pb.id
        HAVING COUNT(DISTINCT CASE
                WHEN pi.download_status = 'failed'
                  OR pi.analysis_status = 'failed'
                  OR pfs.status = 'failed'
                THEN pi.id
            END) > 0
        ORDER BY error_count DESC, pb.published_at DESC, pb.id DESC
        LIMIT ?
    """
    with closing(get_connection()) as connection:
        rows = connection.execute(sql, (safe_limit,)).fetchall()
    return _normalize_blog_admin_rows(rows)



def reset_blog_processing_errors_for_admin(blog_id: int) -> dict[str, int]:
    """記事内の再試行可能な失敗を待機状態へ戻す。

    invalid_url / permanent_failed は復旧不能として除外済みなので変更しない。
    顔スキャン失敗行は削除し、次回の未処理顔認証対象へ戻す。
    """
    target_blog_id = int(blog_id)
    now = utc_now_text()
    with closing(get_connection()) as connection:
        download_cursor = connection.execute(
            """
            UPDATE photo_images
            SET download_status = 'pending', download_error = '', updated_at = ?
            WHERE blog_id = ? AND download_status = 'failed'
            """,
            (now, target_blog_id),
        )
        analysis_cursor = connection.execute(
            """
            UPDATE photo_images
            SET analysis_status = 'pending', analysis_error = '', updated_at = ?
            WHERE blog_id = ? AND analysis_status = 'failed'
            """,
            (now, target_blog_id),
        )
        face_rows = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM photo_face_scans pfs
            JOIN photo_images pi ON pi.id = pfs.image_id
            WHERE pi.blog_id = ? AND pfs.status = 'failed'
            """,
            (target_blog_id,),
        ).fetchone()
        face_count = int(face_rows['count'] or 0) if face_rows else 0
        connection.execute(
            """
            DELETE FROM photo_face_scans
            WHERE status = 'failed'
              AND image_id IN (SELECT id FROM photo_images WHERE blog_id = ?)
            """,
            (target_blog_id,),
        )
        # 成功状態なのに古い文字だけ残ったレコードも安全に掃除する。
        connection.execute(
            """
            UPDATE photo_images
            SET download_error = CASE WHEN download_status = 'failed' THEN download_error ELSE '' END,
                analysis_error = CASE WHEN analysis_status = 'failed' THEN analysis_error ELSE '' END,
                updated_at = ?
            WHERE blog_id = ?
            """,
            (now, target_blog_id),
        )
        connection.execute(
            """
            UPDATE photo_face_scans
            SET error_message = ''
            WHERE status <> 'failed'
              AND image_id IN (SELECT id FROM photo_images WHERE blog_id = ?)
            """,
            (target_blog_id,),
        )
        connection.commit()
    return {
        'download': max(0, int(download_cursor.rowcount or 0)),
        'analysis': max(0, int(analysis_cursor.rowcount or 0)),
        'face': face_count,
    }

def get_blog_progress_for_admin(blog_id: int) -> dict[str, Any] | None:
    """1記事の人物確認進捗を返す。"""
    sql = _blog_admin_progress_select() + """
        WHERE pb.id = ?
        GROUP BY pb.id
        LIMIT 1
    """
    with closing(get_connection()) as connection:
        row = connection.execute(sql, (int(blog_id),)).fetchone()
    if row is None:
        return None
    return _normalize_blog_admin_rows([row])[0]
