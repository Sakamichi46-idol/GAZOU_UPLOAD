import asyncio
from datetime import datetime
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from blog_checker import get_latest_blog
from config import ALL_BLOG_CHANNEL, BLOG_CHANNELS
from database import (
    is_initial_sync_completed,
    is_notified,
    mark_initial_sync_completed,
    save_blog,
    save_blogs,
)
from image_getter import get_images
from media_converter import send_blog_media


# =========================
# 監視設定
# =========================

CHECK_INTERVAL = 600
RETRY_DELAYS = (5, 10)
JST = ZoneInfo("Asia/Tokyo")


# =========================
# 監視状態
# =========================

_monitor_status: dict[str, Any] = {
    "running": False,
    "last_check_started_at": None,
    "last_check_completed_at": None,
    "last_result": "未実行",
    "last_blog_count": 0,
    "last_new_blog_count": 0,
    "last_error": "",
}


def _now_jst() -> datetime:
    return datetime.now(JST)


def _format_datetime(value: Any) -> str:
    if not isinstance(value, datetime):
        return "未実行"
    return value.strftime("%Y年%m月%d日 %H:%M:%S")


def get_monitor_status() -> dict[str, Any]:
    """!statusコマンド用に監視状態のコピーを返す。"""

    status = dict(_monitor_status)
    status["last_check_started_at_text"] = _format_datetime(
        status.get("last_check_started_at")
    )
    status["last_check_completed_at_text"] = _format_datetime(
        status.get("last_check_completed_at")
    )
    return status


# =========================
# ログ
# =========================


def log_info(message: str) -> None:
    print(f"[INFO] {message}")


def log_success(message: str) -> None:
    print(f"[SUCCESS] {message}")


def log_warning(message: str) -> None:
    print(f"[WARNING] {message}")


def log_error(message: str) -> None:
    print(f"[ERROR] {message}")


# =========================
# 自動再試行
# =========================

T = TypeVar("T")


async def run_with_retry(
    function: Callable[..., T],
    *args: Any,
    operation_name: str,
) -> T:
    """
    同期関数を別スレッドで実行し、一時的な失敗時に再試行する。

    初回 + 2回再試行の最大3回実行。
    """

    total_attempts = len(RETRY_DELAYS) + 1

    for attempt in range(1, total_attempts + 1):
        try:
            return await asyncio.to_thread(
                function,
                *args,
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            if attempt >= total_attempts:
                log_error(
                    f"{operation_name}に失敗しました "
                    f"({attempt}/{total_attempts}回目): {error!r}"
                )
                raise

            delay = RETRY_DELAYS[attempt - 1]
            log_warning(
                f"{operation_name}に失敗しました "
                f"({attempt}/{total_attempts}回目): {error!r} / "
                f"{delay}秒後に再試行します"
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"{operation_name}の再試行処理に失敗しました。")


# =========================
# 通知文
# =========================


def build_notification_text(
    blog: dict[str, Any],
    image_count: int,
) -> str:
    """3グループ共通のDiscord通知文を作成する。"""

    group = str(blog.get("group") or "").strip()
    member = str(blog.get("member") or "").strip()
    title = str(blog.get("title") or "").strip()
    date = str(blog.get("date") or "").strip()
    url = str(blog.get("url") or "").strip()

    return (
        f"🏷️ {group}\n"
        f"👤 {member}\n"
        f"📝 {title}\n"
        f"📅 {date}\n"
        f"🔗 {url}\n"
        f"📷 ブログ画像（{image_count}枚）"
    )


# =========================
# 一覧・詳細情報の統合
# =========================


def merge_blog_detail(
    blog: dict[str, Any],
    detail: Any,
) -> dict[str, Any]:
    """一覧ページの情報と詳細ページの情報を統合する。"""

    merged = dict(blog)

    if not isinstance(detail, dict):
        return merged

    for field in (
        "group",
        "member",
        "title",
        "date",
        "text",
    ):
        detail_value = detail.get(field, "")
        if detail_value:
            merged[field] = detail_value

    if not merged.get("url"):
        merged["url"] = detail.get("url", "")

    return merged


# =========================
# ブログデータ整理
# =========================


def normalize_blog_list(
    blogs: Any,
) -> list[dict[str, Any]]:
    """取得結果を検証し、有効なブログ辞書だけを返す。"""

    if not isinstance(blogs, list):
        log_warning(
            "ブログ取得結果がlistではありません: "
            f"{type(blogs).__name__}"
        )
        return []

    valid_blogs: list[dict[str, Any]] = []

    for blog in blogs:
        if not isinstance(blog, dict):
            log_warning(f"不正なブログデータを除外しました: {blog!r}")
            continue

        url = str(blog.get("url") or "").strip()

        if not url:
            log_warning(f"URLが空のブログを除外しました: {blog!r}")
            continue

        valid_blogs.append(blog)

    return valid_blogs


# =========================
# 初回同期
# =========================


def initialize_current_blogs(
    blogs: list[dict[str, Any]],
) -> bool:
    """現在取得できるブログを通知せずDBへ登録する。"""

    if not blogs:
        log_warning(
            "初回同期を見送ります: ブログを1件も取得できませんでした"
        )
        return False

    inserted_count = save_blogs(blogs)
    mark_initial_sync_completed()

    log_success(
        f"初回同期完了: {len(blogs)}件を確認し、"
        f"{inserted_count}件を新規登録しました"
    )
    log_info("初回同期ではDiscord通知を送信しません")
    return True


# =========================
# 通知先取得
# =========================


def get_channel_ids(
    blog: dict[str, Any],
) -> list[int]:
    """グループ別と全体の通知先を重複なしで返す。"""

    group = str(blog.get("group") or "").strip()
    configured_channels = BLOG_CHANNELS.get(group, [])

    if isinstance(configured_channels, (list, tuple, set)):
        channel_ids = list(configured_channels)
    elif configured_channels:
        channel_ids = [configured_channels]
    else:
        channel_ids = []

    if ALL_BLOG_CHANNEL:
        channel_ids.append(ALL_BLOG_CHANNEL)

    normalized_ids: list[int] = []

    for channel_id in channel_ids:
        try:
            normalized_id = int(channel_id)
        except (TypeError, ValueError):
            log_warning(f"不正なチャンネルIDを除外しました: {channel_id!r}")
            continue

        if normalized_id not in normalized_ids:
            normalized_ids.append(normalized_id)

    return normalized_ids


# =========================
# Discord通知
# =========================


async def notify_channel(
    channel: Any,
    blog: dict[str, Any],
    images: list[str],
) -> None:
    """指定チャンネルへブログ情報と画像を送信する。"""

    text = build_notification_text(blog, len(images))

    await send_blog_media(
        channel=channel,
        text=text,
        embed=None,
        image_urls=images,
        send_delay=1.0,
        article_url=str(blog.get("url") or ""),
        group=str(blog.get("group") or ""),
    )


# =========================
# 1記事の処理
# =========================


async def process_blog(
    bot: Any,
    blog: dict[str, Any],
) -> bool:
    """未通知ブログ1件を取得・通知・DB保存する。"""

    url = str(blog.get("url") or "").strip()

    if not url:
        return False

    if is_notified(url):
        return False

    channel_ids = get_channel_ids(blog)

    if not channel_ids:
        log_warning(
            f"通知先がありません: group={blog.get('group', '')} url={url}"
        )
        return False

    detail = await run_with_retry(
        get_images,
        url,
        operation_name=f"ブログ詳細取得 {url}",
    )

    complete_blog = merge_blog_detail(blog, detail)

    raw_images = detail.get("images", []) if isinstance(detail, dict) else []
    images = (
        [
            str(image_url).strip()
            for image_url in raw_images
            if str(image_url).strip()
        ]
        if isinstance(raw_images, list)
        else []
    )

    log_info(
        "新着詳細: "
        f"{complete_blog.get('group', '')} / "
        f"{complete_blog.get('member', '')} / "
        f"{complete_blog.get('title', '')} / "
        f"画像{len(images)}枚"
    )

    all_succeeded = True
    notified_count = 0

    for channel_id in channel_ids:
        channel = bot.get_channel(channel_id)

        if channel is None:
            log_error(f"チャンネル取得失敗: {channel_id}")
            all_succeeded = False
            continue

        try:
            await notify_channel(channel, complete_blog, images)
            notified_count += 1
            log_success(f"Discord通知完了: channel={channel_id}")

        except asyncio.CancelledError:
            raise

        except Exception as error:
            all_succeeded = False
            log_error(
                f"Discord通知失敗: channel={channel_id} "
                f"url={url} error={error!r}"
            )

    if notified_count > 0 and all_succeeded:
        inserted = save_blog(
            url,
            str(complete_blog.get("group", "")),
            str(complete_blog.get("member", "")),
            str(complete_blog.get("title", "")),
            str(complete_blog.get("date", "")),
        )

        if inserted:
            log_success(f"通知済みDBへ保存しました: {url}")
        else:
            log_info(f"通知済みDBに登録済みです: {url}")

        return True

    log_warning(f"通知失敗のためDB保存を見送りました: {url}")
    return False


# =========================
# 監視ループ
# =========================


async def check_blog(
    bot: Any,
) -> None:
    """ブログを定期確認し、新着記事だけを通知する。"""

    _monitor_status["running"] = True
    log_success(f"ブログ監視タスク開始: 確認間隔={CHECK_INTERVAL}秒")

    try:
        while not bot.is_closed():
            check_started = _now_jst()
            _monitor_status["last_check_started_at"] = check_started
            _monitor_status["last_result"] = "確認中"
            _monitor_status["last_error"] = ""

            log_info(
                "ブログ確認開始: "
                f"{check_started.strftime('%Y-%m-%d %H:%M:%S JST')}"
            )

            try:
                raw_blogs = await run_with_retry(
                    get_latest_blog,
                    operation_name="ブログ一覧取得",
                )

                blogs = normalize_blog_list(raw_blogs)
                _monitor_status["last_blog_count"] = len(blogs)

                log_info(f"ブログ一覧取得完了: {len(blogs)}件")

                if not is_initial_sync_completed():
                    completed = initialize_current_blogs(blogs)
                    new_blog_count = 0
                    _monitor_status["last_result"] = (
                        "初回同期完了" if completed else "初回同期見送り"
                    )

                else:
                    candidates = [
                        blog
                        for blog in blogs
                        if not is_notified(str(blog.get("url") or "").strip())
                    ]
                    new_blog_count = len(candidates)

                    log_info(f"未通知記事確認: {new_blog_count}件")

                    succeeded_count = 0
                    for blog in candidates:
                        try:
                            if await process_blog(bot, blog):
                                succeeded_count += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            log_error(
                                "記事処理エラー: "
                                f"url={blog.get('url', '')} error={error!r}"
                            )

                    _monitor_status["last_result"] = (
                        f"正常（新着{new_blog_count}件、通知成功{succeeded_count}件）"
                    )

                _monitor_status["last_new_blog_count"] = new_blog_count
                _monitor_status["last_check_completed_at"] = _now_jst()

                elapsed = (
                    _monitor_status["last_check_completed_at"] - check_started
                ).total_seconds()
                log_success(
                    f"ブログ確認完了: 新着{new_blog_count}件 / "
                    f"処理時間{elapsed:.2f}秒"
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                _monitor_status["last_result"] = "エラー"
                _monitor_status["last_error"] = repr(error)
                _monitor_status["last_check_completed_at"] = _now_jst()
                log_error(f"ブログ監視エラー: {error!r}")

            await asyncio.sleep(CHECK_INTERVAL)

    except asyncio.CancelledError:
        log_info("ブログ監視タスクを終了します")
        raise

    finally:
        _monitor_status["running"] = False
