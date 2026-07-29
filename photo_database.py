import json
import os
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from typing import Any


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

    connection = sqlite3.connect(
        PHOTO_DB_PATH,
        timeout=30,
    )

    connection.row_factory = (
        sqlite3.Row
    )

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    connection.execute(
        "PRAGMA journal_mode = WAL"
    )

    connection.execute(
        "PRAGMA synchronous = NORMAL"
    )

    return connection


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


# =========================
# 画像登録
# =========================

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
                photo_images.id ASC

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

        connection.execute(
            """
            DELETE FROM photo_ai_tags
            WHERE image_id = ?
            """,
            (
                image_id,
            ),
        )

        connection.commit()


def save_ai_tag(
    image_id: int,
    tag: str,
    confidence: float,
    category: str = "",
    model_name: str = "",
    raw_value: str = "",
) -> None:
    """
    AIが判定したタグを保存する。
    """

    tag = str(
        tag
    ).strip()

    category = str(
        category
    ).strip()

    if not tag:

        return

    confidence = clamp_confidence(
        confidence
    )

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_ai_tags (
                image_id,
                category,
                tag,
                confidence,
                model_name,
                raw_value,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(image_id, category, tag)
            DO UPDATE SET
                confidence = excluded.confidence,
                model_name = excluded.model_name,
                raw_value = excluded.raw_value,
                updated_at = excluded.updated_at
            """,
            (
                image_id,
                category,
                tag,
                confidence,
                model_name,
                raw_value,
                now,
                now,
            ),
        )

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
    """
    人間が設定したタグを保存する。
    """

    tag = str(
        tag
    ).strip()

    category = str(
        category
    ).strip()

    if not tag:

        return

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            INSERT INTO photo_manual_tags (
                image_id,
                category,
                tag,
                created_by,
                note,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(image_id, category, tag)
            DO UPDATE SET
                created_by = excluded.created_by,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (
                image_id,
                category,
                tag,
                created_by,
                note,
                now,
                now,
            ),
        )

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

        connection.execute(
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

        connection.commit()


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

        return rows_to_dicts(
            cursor.fetchall()
        )


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
                status = 'pending',
                selected_person_id = NULL,
                reviewed_by = '',
                review_note = '',
                reviewed_at = '',
                updated_at = excluded.updated_at
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
                photo_images.image_url,

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
        connection.execute(
            """UPDATE photo_review_queue SET status='completed', selected_value=?, reviewed_by=?,
               review_note=?, reviewed_at=?, updated_at=? WHERE image_id=?""",
            ("、".join(names), confirmed_by, note, now, now, image_id),
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
    """AIタグと手動タグを横断して検索する。"""

    clean_tag = str(tag or '').strip()
    if not clean_tag:
        return []

    like_value = f'%{clean_tag}%'
    return _search_photo_images_with_where(
        """
        EXISTS (
            SELECT 1 FROM photo_ai_tags
            WHERE photo_ai_tags.image_id = photo_images.id
              AND photo_ai_tags.tag LIKE ?
        )
        OR EXISTS (
            SELECT 1 FROM photo_manual_tags
            WHERE photo_manual_tags.image_id = photo_images.id
              AND photo_manual_tags.tag LIKE ?
        )
        """,
        (like_value, like_value),
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

        "total_size": int(
            row["total_size"]
            or 0
        ),
    }



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
