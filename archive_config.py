import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# =========================
# ブログアーカイブの稼働時間帯
# =========================

# true の場合、ブログアーカイブだけを指定時間帯に限定する。
# 写真アーカイブ・AI自動解析・Bot本体・手動コマンドは24時間利用できる。
def _env_flag(name: str, default: bool) -> bool:
    default_text = "true" if default else "false"
    return os.getenv(name, default_text).strip().lower() in {
        "1", "true", "yes", "on",
    }


ARCHIVE_ACTIVE_HOURS_ENABLED = _env_flag(
    "ARCHIVE_ACTIVE_HOURS_ENABLED",
    True,
)

ARCHIVE_ACTIVE_START_HOUR = int(
    os.getenv("ARCHIVE_ACTIVE_START_HOUR", "0")
)

ARCHIVE_ACTIVE_END_HOUR = int(
    os.getenv("ARCHIVE_ACTIVE_END_HOUR", "5")
)

ARCHIVE_TIMEZONE = os.getenv(
    "ARCHIVE_TIMEZONE",
    "Asia/Tokyo",
).strip() or "Asia/Tokyo"

if not 0 <= ARCHIVE_ACTIVE_START_HOUR <= 23:
    raise ValueError("ARCHIVE_ACTIVE_START_HOUR は0〜23で指定してください。")

if not 0 <= ARCHIVE_ACTIVE_END_HOUR <= 23:
    raise ValueError("ARCHIVE_ACTIVE_END_HOUR は0〜23で指定してください。")

try:
    ARCHIVE_TZ = ZoneInfo(ARCHIVE_TIMEZONE)
except ZoneInfoNotFoundError as error:
    raise ValueError(
        f"ARCHIVE_TIMEZONE が不正です: {ARCHIVE_TIMEZONE}"
    ) from error


def get_archive_local_now() -> datetime:
    """アーカイブ設定のタイムゾーンで現在時刻を返す。"""
    return datetime.now(ARCHIVE_TZ)


def is_archive_active_time(now: datetime | None = None) -> bool:
    """ブログアーカイブを実行してよい時間帯か判定する。

    開始時刻と終了時刻が同じ場合は24時間稼働として扱う。
    例: 0〜5 は 00:00:00 以上 05:00:00 未満。
    例: 22〜5 は 22:00:00 以上または 05:00:00 未満。
    """
    if not ARCHIVE_ACTIVE_HOURS_ENABLED:
        return True

    current = now or get_archive_local_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ARCHIVE_TZ)
    else:
        current = current.astimezone(ARCHIVE_TZ)

    hour = current.hour
    start = ARCHIVE_ACTIVE_START_HOUR
    end = ARCHIVE_ACTIVE_END_HOUR

    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def archive_active_hours_text() -> str:
    if not ARCHIVE_ACTIVE_HOURS_ENABLED:
        return "24時間（時間制限なし）"
    if ARCHIVE_ACTIVE_START_HOUR == ARCHIVE_ACTIVE_END_HOUR:
        return "24時間"
    return (
        f"{ARCHIVE_ACTIVE_START_HOUR:02d}:00〜"
        f"{ARCHIVE_ACTIVE_END_HOUR:02d}:00 "
        f"({ARCHIVE_TIMEZONE})"
    )


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

    print(
        "BLOG_ARCHIVE_ACTIVE_HOURS: "
        f"{archive_active_hours_text()}"
    )

    print("=" * 50)
