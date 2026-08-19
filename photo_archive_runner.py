import asyncio
import os
from typing import Any

import aiohttp

from archive_checker import get_photo_archive_candidates, enrich_selected_blogs
from archive_image_getter import get_images
from photo_ai_analyzer import analyze_photo_image, get_photo_ai_status
from photo_database import (
    get_photo_blog_by_url,
    get_registered_photo_blog_urls,
    get_zero_image_photo_blogs,
    add_image_person_candidate,
    init_photo_db,
    save_photo_blog,
    save_photo_images,
)
from photo_image_downloader import download_blog_images


# =========================
# 環境変数
# =========================


def get_env_int(name: str, default: int, minimum: int = 0) -> int:
    """整数の環境変数を安全に読み込む。"""

    value = os.getenv(name, "").strip()

    if not value:
        return default

    try:
        parsed = int(value)
    except ValueError:
        print(f"環境変数{name}が整数ではないため、{default}を使用します: {value!r}")
        return default

    return max(parsed, minimum)


def get_env_bool(name: str, default: bool) -> bool:
    """真偽値の環境変数を安全に読み込む。"""

    value = os.getenv(name, "").strip().lower()

    if not value:
        return default

    if value in {"1", "true", "yes", "on", "有効"}:
        return True

    if value in {"0", "false", "no", "off", "無効"}:
        return False

    print(f"環境変数{name}の値を判定できないため、{default}を使用します: {value!r}")
    return default


PHOTO_ARCHIVE_LIMIT = get_env_int(
    "PHOTO_ARCHIVE_LIMIT",
    5,
    minimum=1,
)

PHOTO_ARCHIVE_GROUP = os.getenv(
    "PHOTO_ARCHIVE_GROUP",
    "all",
).strip()

PHOTO_ARCHIVE_AI_ANALYZE = get_env_bool(
    "PHOTO_ARCHIVE_AI_ANALYZE",
    True,
)

PHOTO_ARCHIVE_REQUEST_TIMEOUT = get_env_int(
    "PHOTO_ARCHIVE_REQUEST_TIMEOUT",
    120,
    minimum=10,
)

PHOTO_ARCHIVE_BLOG_DELAY = get_env_int(
    "PHOTO_ARCHIVE_BLOG_DELAY",
    1,
    minimum=0,
)


# =========================
# 状態管理
# =========================

_photo_archive_lock = asyncio.Lock()
_photo_archive_stop_requested = False


def request_photo_archive_stop() -> None:
    """実行中の写真アーカイブに停止を要求する。"""

    global _photo_archive_stop_requested
    _photo_archive_stop_requested = True


def clear_photo_archive_stop_request() -> None:
    """停止要求を解除する。"""

    global _photo_archive_stop_requested
    _photo_archive_stop_requested = False


def is_photo_archive_running() -> bool:
    """写真アーカイブが実行中か返す。"""

    return _photo_archive_lock.locked()


# =========================
# 補助関数
# =========================


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_group_filter(value: str) -> str:
    """環境変数で指定されたグループ名を正規化する。"""

    text = str(value or "").strip().lower()

    aliases = {
        "": "all",
        "all": "all",
        "全部": "all",
        "全て": "all",
        "乃木坂": "乃木坂46",
        "乃木坂46": "乃木坂46",
        "nogizaka": "乃木坂46",
        "nogi": "乃木坂46",
        "櫻坂": "櫻坂46",
        "桜坂": "櫻坂46",
        "櫻坂46": "櫻坂46",
        "桜坂46": "櫻坂46",
        "sakurazaka": "櫻坂46",
        "sakura": "櫻坂46",
        "日向坂": "日向坂46",
        "日向坂46": "日向坂46",
        "hinatazaka": "日向坂46",
        "hinata": "日向坂46",
    }

    return aliases.get(text, str(value or "all").strip())


def filter_blogs_by_group(
    blogs: list[dict[str, Any]],
    group_filter: str,
) -> list[dict[str, Any]]:
    """指定グループの記事だけを残す。"""

    normalized = normalize_group_filter(group_filter)

    if normalized == "all":
        return blogs

    return [
        blog
        for blog in blogs
        if str(blog.get("group", "")).strip() == normalized
    ]


def remove_duplicate_blog_urls(
    blogs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """URLが重複する記事を除外する。"""

    unique_blogs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for blog in blogs:
        url = str(blog.get("url", "")).strip()

        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        unique_blogs.append(blog)

    return unique_blogs


def get_unregistered_photo_blogs(
    blogs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    photo_archive.dbに記事情報がないものだけを返す。

    archive.dbは参照しないため、Discord通知履歴には影響しない。
    """

    targets: list[dict[str, Any]] = []

    for blog in blogs:
        url = str(blog.get("url", "")).strip()

        if not url:
            continue

        try:
            existing = get_photo_blog_by_url(url)
        except Exception as error:
            print("写真DB登録確認エラー:", url, error)
            continue

        if existing is None:
            targets.append(blog)

    return targets


# =========================
# 1記事の処理
# =========================


async def process_photo_blog(
    session: aiohttp.ClientSession,
    blog: dict[str, Any],
    *,
    analyze_images: bool,
) -> dict[str, Any]:
    """
    1件の記事について、画像取得・DB登録・保存・AI解析を行う。

    Discordへの送信処理とarchive.dbの更新は行わない。
    """

    result: dict[str, Any] = {
        "url": str(blog.get("url", "")).strip(),
        "group": str(blog.get("group", "")).strip(),
        "member": str(blog.get("member", "")).strip(),
        "title": str(blog.get("title", "")).strip(),
        "image_count": 0,
        "registered": 0,
        "downloaded": 0,
        "download_failed": 0,
        "analyzed": 0,
        "analysis_review": 0,
        "analysis_failed": 0,
        "status": "pending",
        "error": "",
    }

    blog_url = result["url"]

    if not blog_url:
        result["status"] = "failed"
        result["error"] = "ブログURLが空です。"
        return result

    try:
        image_urls = await get_images(blog_url)
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"画像URL取得エラー: {type(error).__name__}: {error}"
        print(result["error"], blog_url)
        return result

    image_urls = [
        str(image_url).strip()
        for image_url in image_urls
        if str(image_url).strip()
    ]

    result["image_count"] = len(image_urls)

    if not image_urls:
        # 画像がない記事も処理済みとして記録し、毎回の再確認を防ぐ。
        try:
            await asyncio.to_thread(save_photo_blog, blog)
            result["status"] = "no_images"
        except Exception as error:
            result["status"] = "failed"
            result["error"] = f"画像なし記事のDB登録エラー: {type(error).__name__}: {error}"
        return result

    try:
        blog_id = await asyncio.to_thread(save_photo_blog, blog)
        image_records = await asyncio.to_thread(
            save_photo_images,
            blog_id,
            image_urls,
        )
        result["registered"] = len(image_records)

        # 投稿者名は「写っている人物の候補」としてだけ登録する。
        # 本人確定にはせず、Discordの確認コマンドで確定する。
        author_name = str(blog.get("member", "")).strip()
        if author_name:
            for image_record in image_records:
                await asyncio.to_thread(
                    add_image_person_candidate,
                    int(image_record["image_id"]),
                    author_name,
                    source="blog_author",
                    confidence=0.35,
                )
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"写真DB登録エラー: {type(error).__name__}: {error}"
        print(result["error"], blog_url)
        return result

    try:
        download_result = await download_blog_images(
            session,
            blog_id=blog_id,
            blog=blog,
            image_records=image_records,
        )
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"画像保存エラー: {type(error).__name__}: {error}"
        print(result["error"], blog_url)
        return result

    result["downloaded"] = safe_int(download_result.get("success", 0))
    result["download_failed"] = safe_int(download_result.get("failed", 0))

    if not analyze_images:
        result["status"] = "completed"
        return result

    ai_status = get_photo_ai_status()

    if not ai_status.get("enabled"):
        print("OPENAI_API_KEY未設定のため、写真AI解析をスキップします。")
        result["status"] = "completed"
        return result

    downloaded_ids = [
        safe_int(item.get("image_id", 0))
        for item in download_result.get("results", [])
        if item.get("success")
    ]

    for image_id in downloaded_ids:
        if image_id <= 0:
            continue

        try:
            analysis_result = await analyze_photo_image(image_id)
            analysis_status = str(analysis_result.get("status", ""))

            if analysis_status == "completed":
                result["analyzed"] += 1
            elif analysis_status == "review":
                result["analysis_review"] += 1
            elif analysis_status == "waiting_person_review":
                # 新着保存直後は人物確認未完了が通常。失敗には数えない。
                pass
            else:
                result["analysis_failed"] += 1
        except Exception as error:
            result["analysis_failed"] += 1
            print("写真AI解析エラー:", image_id, error)

    result["status"] = "completed"
    return result


# =========================
# 写真アーカイブ本体
# =========================


async def run_photo_archive_once(
    *,
    limit: int | None = None,
    group: str | None = None,
    analyze_images: bool | None = None,
) -> dict[str, Any]:
    """
    写真アーカイブを1回実行する。

    ・Discordには投稿しない
    ・archive.dbは参照・更新しない
    ・photo_archive.dbに未登録の記事だけを対象にする
    """

    if is_photo_archive_running():
        return {
            "status": "already_running",
            "message": "写真アーカイブは既に実行中です。",
            "processed": 0,
        }

    async with _photo_archive_lock:
        clear_photo_archive_stop_request()

        selected_limit = max(
            safe_int(limit, PHOTO_ARCHIVE_LIMIT),
            1,
        )
        selected_group = group if group is not None else PHOTO_ARCHIVE_GROUP
        selected_ai = (
            analyze_images
            if analyze_images is not None
            else PHOTO_ARCHIVE_AI_ANALYZE
        )

        summary: dict[str, Any] = {
            "status": "running",
            "group": normalize_group_filter(selected_group),
            "limit": selected_limit,
            "ai_enabled": bool(selected_ai),
            "collected": 0,
            "group_filtered": 0,
            "unregistered": 0,
            "processed": 0,
            "completed": 0,
            "no_images": 0,
            "failed": 0,
            "downloaded": 0,
            "download_failed": 0,
            "analyzed": 0,
            "analysis_review": 0,
            "analysis_failed": 0,
            "stopped": False,
            "results": [],
        }

        print("=" * 60)
        print("写真アーカイブ巡回を開始します。")
        print("Discord通知: なし")
        print("archive.db更新: なし")
        print("対象グループ:", summary["group"])
        print("今回の処理上限:", selected_limit, "件")
        print("AI解析:", "有効" if selected_ai else "無効")
        print("=" * 60)

        try:
            await asyncio.to_thread(init_photo_db)

            normalized_group = normalize_group_filter(selected_group)

            # 登録済みURLを最初に一括取得する。
            # 各URLごとのSELECTと全記事一覧取得を避け、最新側から
            # 必要な未登録件数が見つかった時点で一覧巡回を終了する。
            known_urls = await asyncio.to_thread(
                get_registered_photo_blog_urls,
                normalized_group,
            )

            targets = await get_photo_archive_candidates(
                known_urls=known_urls,
                target_count=selected_limit,
                group_filter=normalized_group,
            )
            targets = remove_duplicate_blog_urls(targets)

            summary["collected"] = len(targets)
            summary["group_filtered"] = len(targets)
            summary["unregistered"] = len(targets)

            print("写真DB未登録記事:", summary["unregistered"], "件")
            print("今回の写真処理対象:", len(targets), "件")

            if targets:
                targets = await enrich_selected_blogs(targets)
                print("詳細取得完了後の処理対象:", len(targets), "件")

            if not targets:
                summary["status"] = "completed"
                print("写真DBに未登録の記事はありません。")
                return summary

            timeout = aiohttp.ClientTimeout(
                total=PHOTO_ARCHIVE_REQUEST_TIMEOUT,
            )

            async with aiohttp.ClientSession(timeout=timeout) as session:
                for index, blog in enumerate(targets, start=1):
                    if _photo_archive_stop_requested:
                        summary["stopped"] = True
                        summary["status"] = "stopped"
                        print("写真アーカイブの停止要求を受け付けました。")
                        break

                    print(
                        f"写真アーカイブ処理 {index}/{len(targets)}:",
                        blog.get("group", ""),
                        blog.get("member", ""),
                        blog.get("url", ""),
                    )

                    result = await process_photo_blog(
                        session,
                        blog,
                        analyze_images=bool(selected_ai),
                    )

                    summary["results"].append(result)
                    summary["processed"] += 1
                    summary["downloaded"] += safe_int(result.get("downloaded", 0))
                    summary["download_failed"] += safe_int(result.get("download_failed", 0))
                    summary["analyzed"] += safe_int(result.get("analyzed", 0))
                    summary["analysis_review"] += safe_int(result.get("analysis_review", 0))
                    summary["analysis_failed"] += safe_int(result.get("analysis_failed", 0))

                    result_status = str(result.get("status", ""))

                    if result_status == "completed":
                        summary["completed"] += 1
                    elif result_status == "no_images":
                        summary["no_images"] += 1
                    else:
                        summary["failed"] += 1

                    print("写真アーカイブ記事結果:", result)

                    if PHOTO_ARCHIVE_BLOG_DELAY > 0:
                        await asyncio.sleep(PHOTO_ARCHIVE_BLOG_DELAY)

            if summary["status"] == "running":
                summary["status"] = "completed"

        except asyncio.CancelledError:
            summary["status"] = "cancelled"
            print("写真アーカイブ処理がキャンセルされました。")
            raise
        except Exception as error:
            summary["status"] = "failed"
            summary["error"] = f"{type(error).__name__}: {error}"
            print("写真アーカイブ全体エラー:", summary["error"])

        finally:
            print("写真アーカイブ巡回結果:", summary)
            print("=" * 60)

        return summary


# =========================
# 画像0件記事の再判定
# =========================


async def repair_zero_image_photo_blogs(
    *,
    limit: int = 100,
    group: str | None = None,
    analyze_images: bool | None = None,
) -> dict[str, Any]:
    """
    登録済みだが画像が1件もない記事を再取得して修復する。

    通常巡回は登録済みURLを除外するため、過去に画像0件として
    保存された記事はこの処理で明示的に再判定する。
    """

    if is_photo_archive_running():
        return {
            "status": "already_running",
            "message": "写真アーカイブは既に実行中です。",
            "targets": 0,
            "processed": 0,
        }

    async with _photo_archive_lock:
        clear_photo_archive_stop_request()

        selected_limit = max(1, min(safe_int(limit, 100), 1000))
        selected_group = normalize_group_filter(group or "all")
        selected_ai = (
            analyze_images
            if analyze_images is not None
            else PHOTO_ARCHIVE_AI_ANALYZE
        )

        summary: dict[str, Any] = {
            "status": "running",
            "group": selected_group,
            "limit": selected_limit,
            "targets": 0,
            "processed": 0,
            "repaired": 0,
            "still_no_images": 0,
            "failed": 0,
            "downloaded": 0,
            "download_failed": 0,
            "analyzed": 0,
            "analysis_review": 0,
            "analysis_failed": 0,
            "stopped": False,
            "results": [],
        }

        try:
            await asyncio.to_thread(init_photo_db)
            targets = await asyncio.to_thread(
                get_zero_image_photo_blogs,
                selected_limit,
                selected_group,
            )
            summary["targets"] = len(targets)

            print("=" * 60)
            print("画像0件記事の再判定を開始します。")
            print("対象グループ:", selected_group)
            print("再判定対象:", len(targets), "件")
            print("=" * 60)

            if not targets:
                summary["status"] = "completed"
                return summary

            timeout = aiohttp.ClientTimeout(total=PHOTO_ARCHIVE_REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for index, blog in enumerate(targets, start=1):
                    if _photo_archive_stop_requested:
                        summary["stopped"] = True
                        summary["status"] = "stopped"
                        break

                    print(
                        f"画像0件再判定 {index}/{len(targets)}:",
                        blog.get("group", ""),
                        blog.get("member", ""),
                        blog.get("url", ""),
                    )

                    result = await process_photo_blog(
                        session,
                        blog,
                        analyze_images=bool(selected_ai),
                    )
                    summary["results"].append(result)
                    summary["processed"] += 1
                    summary["downloaded"] += safe_int(result.get("downloaded", 0))
                    summary["download_failed"] += safe_int(result.get("download_failed", 0))
                    summary["analyzed"] += safe_int(result.get("analyzed", 0))
                    summary["analysis_review"] += safe_int(result.get("analysis_review", 0))
                    summary["analysis_failed"] += safe_int(result.get("analysis_failed", 0))

                    status = str(result.get("status", ""))
                    if status == "completed" and safe_int(result.get("registered", 0)) > 0:
                        summary["repaired"] += 1
                    elif status == "no_images":
                        summary["still_no_images"] += 1
                    else:
                        summary["failed"] += 1

                    if PHOTO_ARCHIVE_BLOG_DELAY > 0:
                        await asyncio.sleep(PHOTO_ARCHIVE_BLOG_DELAY)

            if summary["status"] == "running":
                summary["status"] = "completed"

        except asyncio.CancelledError:
            summary["status"] = "cancelled"
            raise
        except Exception as error:
            summary["status"] = "failed"
            summary["error"] = f"{type(error).__name__}: {error}"
            print("画像0件記事の再判定エラー:", summary["error"])
        finally:
            print("画像0件記事の再判定結果:", summary)
            print("=" * 60)

        return summary
