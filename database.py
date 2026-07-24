import os
import sqlite3
from urllib.parse import urlparse


DB_DIR = "/data"
DB_NAME = os.path.join(
    DB_DIR,
    "blogs.db",
)


def normalize_url(url):
    """
    URLからクエリパラメータやフラグメントを除外し、
    通知済み判定に使用するURLへ正規化する。
    """

    if not url:
        return ""

    parsed = urlparse(url)

    return (
        parsed.scheme
        + "://"
        + parsed.netloc
        + parsed.path
    )


def init_db():
    """
    新着ブログ通知用のデータベースを初期化する。
    """

    os.makedirs(
        DB_DIR,
        exist_ok=True,
    )

    print(
        "DB PATH:",
        DB_NAME,
    )

    print(
        "DB EXISTS:",
        os.path.exists(DB_NAME),
    )

    conn = sqlite3.connect(
        DB_NAME,
    )

    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS blogs (
            url TEXT PRIMARY KEY,
            group_name TEXT,
            member TEXT,
            title TEXT,
            date TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def is_notified(url):
    """
    指定したブログURLが通知済みか確認する。
    """

    url = normalize_url(url)

    conn = sqlite3.connect(
        DB_NAME,
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT url
        FROM blogs
        WHERE url = ?
        """,
        (
            url,
        ),
    )

    result = cur.fetchone()

    conn.close()

    return result is not None


def save_blog(
    url,
    group_name,
    member,
    title,
    date,
):
    """
    通知が完了したブログをデータベースへ保存する。
    """

    url = normalize_url(url)

    conn = sqlite3.connect(
        DB_NAME,
    )

    cur = conn.cursor()

    cur.execute(
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
            url,
            group_name,
            member,
            title,
            date,
        ),
    )

    conn.commit()
    conn.close()
