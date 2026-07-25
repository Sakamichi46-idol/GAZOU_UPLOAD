import os
import sqlite3
from contextlib import closing
from typing import Any
from urllib.parse import urlparse


# =========================
# データベース設定
# =========================

DB_DIR = "/app/data"

DB_NAME = os.path.join(
    DB_DIR,
    "blogs.db",
)


# =========================
# URL正規化
# =========================

def normalize_url(url: str) -> str:
    """
    URLからクエリパラメータとフラグメントを除外し、
    通知済み判定に使用するURLへ正規化する。

    例:
    https://example.com/detail/123?cd=blog
    ↓
    https://example.com/detail/123
    """

    clean_url = str(
        url or ""
    ).strip()

    if not clean_url:
        return ""

    parsed = urlparse(
        clean_url
    )

    if not parsed.scheme or not parsed.netloc:
        return clean_url

    return (
        f"{parsed.scheme}://"
        f"{parsed.netloc}"
        f"{parsed.path}"
    )


# =========================
# DB接続
# =========================

def get_connection() -> sqlite3.Connection:
    """
    SQLiteへの接続を作成する。
    """

    os.makedirs(
        DB_DIR,
        exist_ok=True,
    )

    connection = sqlite3.connect(
        DB_NAME,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    return connection


# =========================
# DB初期化
# =========================

def init_db() -> bool:
    """
    新着ブログ通知用データベースを初期化する。

    戻り値:
        True:
            初期化前からDBファイルが存在していた

        False:
            今回初めてDBファイルが作成された
    """

    os.makedirs(
        DB_DIR,
        exist_ok=True,
    )

    db_existed = os.path.isfile(
        DB_NAME
    )

    print(
        "DB PATH:",
        DB_NAME,
    )

    print(
        "DB EXISTS:",
        db_existed,
    )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blogs (
                url TEXT PRIMARY KEY,
                group_name TEXT NOT NULL DEFAULT '',
                member TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT ''
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_blogs_group_name
            ON blogs(group_name)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_blogs_member
            ON blogs(member)
            """
        )

        connection.commit()

    return db_existed


# =========================
# 通知済み確認
# =========================

def is_notified(url: str) -> bool:
    """
    指定したブログURLが通知済みか確認する。
    """

    normalized_url = normalize_url(
        url
    )

    if not normalized_url:
        return False

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT 1
            FROM blogs
            WHERE url = ?
            LIMIT 1
            """,
            (
                normalized_url,
            ),
        )

        result = cursor.fetchone()

    return result is not None


# =========================
# ブログ保存
# =========================

def save_blog(
    url: str,
    group_name: str,
    member: str,
    title: str,
    date: str,
) -> bool:
    """
    通知が完了したブログをデータベースへ保存する。

    戻り値:
        True:
            新しく登録された

        False:
            すでに登録済みだった
    """

    normalized_url = normalize_url(
        url
    )

    if not normalized_url:
        print(
            "通知済みDB保存スキップ: URLが空です"
        )
        return False

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT OR IGNORE INTO blogs
            (
                url,
                group_name,
                member,
                title,
                date
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalized_url,
                str(group_name or "").strip(),
                str(member or "").strip(),
                str(title or "").strip(),
                str(date or "").strip(),
            ),
        )

        inserted = (
            cursor.rowcount > 0
        )

        connection.commit()

    return inserted


def save_blogs(
    blogs: list[dict[str, Any]],
) -> int:
    """
    複数のブログをまとめて通知済みDBへ登録する。

    初回起動時に、現在取得できる記事を
    通知せず一括登録するために使用する。

    戻り値:
        新しく登録された件数
    """

    if not blogs:
        return 0

    rows: list[
        tuple[
            str,
            str,
            str,
            str,
            str,
        ]
    ] = []

    seen_urls: set[str] = set()

    for blog in blogs:

        if not isinstance(
            blog,
            dict,
        ):
            continue

        normalized_url = normalize_url(
            str(
                blog.get("url")
                or ""
            )
        )

        if not normalized_url:
            continue

        if normalized_url in seen_urls:
            continue

        seen_urls.add(
            normalized_url
        )

        rows.append(
            (
                normalized_url,
                str(
                    blog.get("group")
                    or blog.get("group_name")
                    or ""
                ).strip(),
                str(
                    blog.get("member")
                    or ""
                ).strip(),
                str(
                    blog.get("title")
                    or ""
                ).strip(),
                str(
                    blog.get("date")
                    or ""
                ).strip(),
            )
        )

    if not rows:
        return 0

    inserted_count = 0

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        for row in rows:

            cursor.execute(
                """
                INSERT OR IGNORE INTO blogs
                (
                    url,
                    group_name,
                    member,
                    title,
                    date
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                row,
            )

            if cursor.rowcount > 0:
                inserted_count += 1

        connection.commit()

    return inserted_count


# =========================
# DB件数
# =========================

def get_blog_count() -> int:
    """
    通知済みブログの登録件数を返す。
    """

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM blogs
            """
        )

        result = cursor.fetchone()

    if result is None:
        return 0

    return int(
        result[0]
    )


def is_database_empty() -> bool:
    """
    ブログ通知DBが空か確認する。
    """

    return get_blog_count() == 0


# =========================
# アプリ状態
# =========================

def get_state(
    key: str,
    default: str = "",
) -> str:
    """
    app_stateテーブルから状態を取得する。
    """

    clean_key = str(
        key or ""
    ).strip()

    if not clean_key:
        return default

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT value
            FROM app_state
            WHERE key = ?
            LIMIT 1
            """,
            (
                clean_key,
            ),
        )

        result = cursor.fetchone()

    if result is None:
        return default

    return str(
        result["value"]
        or default
    )


def set_state(
    key: str,
    value: str,
) -> None:
    """
    app_stateテーブルへ状態を保存する。
    """

    clean_key = str(
        key or ""
    ).strip()

    if not clean_key:
        raise ValueError(
            "状態キーが空です。"
        )

    with closing(
        get_connection()
    ) as connection:

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO app_state
            (
                key,
                value
            )
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value
            """,
            (
                clean_key,
                str(value or ""),
            ),
        )

        connection.commit()


def is_initial_sync_completed() -> bool:
    """
    初回同期が完了済みか確認する。
    """

    return (
        get_state(
            "initial_sync_completed",
            "false",
        ).lower()
        == "true"
    )


def mark_initial_sync_completed() -> None:
    """
    初回同期完了状態を保存する。
    """

    set_state(
        "initial_sync_completed",
        "true",
    )


# =========================
# 手動実行時の確認用
# =========================

if __name__ == "__main__":

    init_db()

    print(
        "通知済みブログ件数:",
        get_blog_count(),
    )

    print(
        "初回同期完了:",
        is_initial_sync_completed(),
    )
