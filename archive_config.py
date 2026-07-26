import os


# =========================
# 巡回間隔
# =========================

# アーカイブ巡回が一度完了したあと、
# 次の巡回を開始するまでの秒数
ARCHIVE_INTERVAL = int(
    os.getenv(
        "ARCHIVE_INTERVAL",
        "600"
    )
)


# =========================
# Discord送信間隔
# =========================

# Embed・画像・次の記事の送信間隔
# 短すぎるとDiscordのレート制限に
# かかりやすくなるため、基本は1秒以上
SEND_DELAY = float(
    os.getenv(
        "SEND_DELAY",
        "1.5"
    )
)


# =========================
# テスト件数
# =========================

# 0なら未保存記事をすべて処理
# 20なら古い順に20件だけ処理
ARCHIVE_TEST_LIMIT = int(
    os.getenv(
        "ARCHIVE_TEST_LIMIT",
        "0"
    )
)


# =========================
# グループ指定
# =========================

# 例:
# nogizaka
# sakurazaka
# hinatazaka
# all
ARCHIVE_TARGET_GROUP = os.getenv(
    "ARCHIVE_TARGET_GROUP",
    "all"
).strip().lower()


# =========================
# ページ取得間隔
# =========================

# 各公式サイトの一覧ページを
# 連続取得するときの待機時間
PAGE_REQUEST_DELAY = float(
    os.getenv(
        "PAGE_REQUEST_DELAY",
        "0.5"
    )
)


# =========================
# 詳細ページ取得間隔
# =========================

DETAIL_REQUEST_DELAY = float(
    os.getenv(
        "DETAIL_REQUEST_DELAY",
        "2.0"
    )
)


# =========================
# HTTPタイムアウト
# =========================

HTTP_TIMEOUT = int(
    os.getenv(
        "HTTP_TIMEOUT",
        "20"
    )
)


# =========================
# 設定表示
# =========================

def print_archive_config():

    print("=" * 50)

    print(
        f"ARCHIVE_INTERVAL: "
        f"{ARCHIVE_INTERVAL}秒"
    )

    print(
        f"SEND_DELAY: "
        f"{SEND_DELAY}秒"
    )

    print(
        f"ARCHIVE_TEST_LIMIT: "
        f"{ARCHIVE_TEST_LIMIT}件"
    )

    print(
        f"ARCHIVE_TARGET_GROUP: "
        f"{ARCHIVE_TARGET_GROUP}"
    )

    print(
        f"PAGE_REQUEST_DELAY: "
        f"{PAGE_REQUEST_DELAY}秒"
    )

    print(
        f"DETAIL_REQUEST_DELAY: "
        f"{DETAIL_REQUEST_DELAY}秒"
    )

    print(
        f"HTTP_TIMEOUT: "
        f"{HTTP_TIMEOUT}秒"
    )

    print("=" * 50)
