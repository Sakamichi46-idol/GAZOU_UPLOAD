import os
import sqlite3

DB_DIR = "/data"
DB_NAME = "archive.db"

os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, DB_NAME)

print(f"ARCHIVE DB PATH: {DB_PATH}")


# =========================
# DB接続
# =========================

def get_connection():
    return sqlite3.connect(DB_PATH)


# =========================
# 初期化
# =========================

def init_archive_db():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS archive(
            url TEXT PRIMARY KEY,
            group_name TEXT,
            member TEXT,
            title TEXT,
            date TEXT,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


    # Discordへ送信した画像を、記事・送信先チャンネル単位で記録する。
    # status='sending' も送信済み扱いにすることで、Railwayが送信直後に
    # 再起動しても同じ画像を自動再送しない。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS archive_image_deliveries(
            blog_url TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            image_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sending',
            message_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            PRIMARY KEY(blog_url, channel_id, image_url)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_archive_image_deliveries_status
        ON archive_image_deliveries(status)
    """)

    conn.commit()
    conn.close()


# =========================
# 保存済み確認
# =========================

def is_archived(url):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM archive WHERE url=?",
        (url,)
    )

    result = cur.fetchone()

    conn.close()

    return result is not None


# =========================
# 保存
# =========================

def save_archive(blog):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO archive
        (
            url,
            group_name,
            member,
            title,
            date
        )
        VALUES
        (?, ?, ?, ?, ?)
        """,
        (
            blog["url"],
            blog["group"],
            blog["member"],
            blog["title"],
            blog["date"]
        )
    )

    conn.commit()
    conn.close()


# =========================
# 未保存だけ返す
# =========================

def filter_not_archived(blogs):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT url FROM archive")

    archived = {
        row[0]
        for row in cur.fetchall()
    }

    conn.close()

    return [
        blog
        for blog in blogs
        if blog["url"] not in archived
    ]


# =========================
# 画像送信の重複防止
# =========================

def get_unsent_image_urls(blog_url, channel_id, image_urls):
    """
    この記事・チャンネルで未送信の画像URLだけを返す。

    sending は「送信結果が不確定だが、二重送信防止のため再送しない」状態。
    sent と sending のどちらも自動再送対象から除外する。
    """
    clean_urls = list(dict.fromkeys(
        str(url).strip()
        for url in (image_urls or [])
        if str(url).strip()
    ))

    if not clean_urls:
        return []

    conn = get_connection()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in clean_urls)
    cur.execute(
        f"""
        SELECT image_url
        FROM archive_image_deliveries
        WHERE blog_url = ?
          AND channel_id = ?
          AND image_url IN ({placeholders})
          AND status IN ('sending', 'sent')
        """,
        (str(blog_url), str(channel_id), *clean_urls),
    )
    blocked = {row[0] for row in cur.fetchall()}
    conn.close()
    return [url for url in clean_urls if url not in blocked]


def claim_image_urls(blog_url, channel_id, image_urls):
    """
    送信直前にURLを sending として予約する。
    予約できたURLだけを返す。
    """
    clean_urls = list(dict.fromkeys(
        str(url).strip()
        for url in (image_urls or [])
        if str(url).strip()
    ))
    if not clean_urls:
        return []

    conn = get_connection()
    cur = conn.cursor()
    claimed = []
    try:
        cur.execute("BEGIN IMMEDIATE")
        for image_url in clean_urls:
            cur.execute(
                """
                INSERT OR IGNORE INTO archive_image_deliveries
                (blog_url, channel_id, image_url, status)
                VALUES (?, ?, ?, 'sending')
                """,
                (str(blog_url), str(channel_id), image_url),
            )
            if cur.rowcount == 1:
                claimed.append(image_url)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return claimed


def mark_image_urls_sent(blog_url, channel_id, image_urls, message_id=""):
    clean_urls = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if not clean_urls:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.executemany(
        """
        UPDATE archive_image_deliveries
        SET status='sent', message_id=?, sent_at=CURRENT_TIMESTAMP
        WHERE blog_url=? AND channel_id=? AND image_url=?
        """,
        [
            (str(message_id or ""), str(blog_url), str(channel_id), image_url)
            for image_url in clean_urls
        ],
    )
    conn.commit()
    conn.close()


def release_image_claims(blog_url, channel_id, image_urls):
    """Discord送信自体が失敗した場合だけ予約を解除し、次回再試行可能にする。"""
    clean_urls = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if not clean_urls:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.executemany(
        """
        DELETE FROM archive_image_deliveries
        WHERE blog_url=? AND channel_id=? AND image_url=? AND status='sending'
        """,
        [(str(blog_url), str(channel_id), image_url) for image_url in clean_urls],
    )
    conn.commit()
    conn.close()


# =========================
# 件数
# =========================

def archive_count():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM archive"
    )

    count = cur.fetchone()[0]

    conn.close()

    return count


# =========================
# 全削除
# =========================

def reset_archive():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM archive"
    )

    cur.execute(
        "DELETE FROM archive_image_deliveries"
    )

    conn.commit()
    conn.close()
