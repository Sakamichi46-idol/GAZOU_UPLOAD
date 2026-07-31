"""Repair missing photo image storage metadata and files.

Used by face review and the owner maintenance command.  The repair order is:
1. retry the current HTTP image URL when available;
2. re-parse the original blog article and select the saved image_index;
3. update image_url and download the image to the configured storage.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any
from urllib.parse import urlparse

import aiohttp

from archive_image_getter import get_images as get_article_images
from photo_database import get_connection, get_photo_image, reset_image_download_status
from photo_image_downloader import download_photo_image


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _update_image_url(image_id: int, image_url: str) -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE photo_images
            SET image_url = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (str(image_url).strip(), int(image_id)),
        )
        connection.commit()


async def repair_photo_image(
    image_id: int,
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Try to restore one image and return a detailed result dictionary."""
    image = get_photo_image(int(image_id))
    if not image:
        return {"success": False, "image_id": int(image_id), "error": "画像IDが見つかりません。"}

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    assert session is not None
    attempts: list[dict[str, Any]] = []

    async def attempt(url: str, source: str) -> dict[str, Any]:
        clean_url = str(url or "").strip()
        if not _is_http_url(clean_url):
            result = {
                "success": False,
                "image_id": int(image_id),
                "source": source,
                "error": "利用可能なHTTP画像URLではありません。",
            }
            attempts.append(result)
            return result

        reset_image_download_status(int(image_id))
        result = await download_photo_image(
            session,
            image_id=int(image["id"]),
            blog_id=int(image["blog_id"]),
            image_url=clean_url,
            image_index=int(image.get("image_index") or 0),
            group_name=str(image.get("group_name") or ""),
            member_name=str(image.get("member_name") or ""),
            published_at=str(image.get("published_at") or ""),
        )
        result["source"] = source
        result["attempted_url"] = clean_url
        attempts.append(result)
        return result

    try:
        current_url = str(image.get("image_url") or "").strip()
        if _is_http_url(current_url):
            current_result = await attempt(current_url, "current_url")
            if current_result.get("success"):
                current_result["attempts"] = attempts
                return current_result

        blog_url = str(image.get("blog_url") or "").strip()
        if not _is_http_url(blog_url):
            return {
                "success": False,
                "image_id": int(image_id),
                "error": "元記事URLがないため画像情報を復元できません。",
                "attempts": attempts,
            }

        try:
            refreshed_urls = await get_article_images(blog_url)
        except Exception as error:
            return {
                "success": False,
                "image_id": int(image_id),
                "error": f"元記事の再解析に失敗しました: {type(error).__name__}: {error}",
                "attempts": attempts,
            }

        image_index = int(image.get("image_index") or 0)
        if image_index < 1 or image_index > len(refreshed_urls):
            return {
                "success": False,
                "image_id": int(image_id),
                "error": (
                    f"元記事に画像{image_index}枚目がありません。"
                    f"（現在の抽出数: {len(refreshed_urls)}件）"
                ),
                "attempts": attempts,
            }

        refreshed_url = str(refreshed_urls[image_index - 1] or "").strip()
        if not _is_http_url(refreshed_url):
            return {
                "success": False,
                "image_id": int(image_id),
                "error": "元記事から取得した画像URLが無効です。",
                "attempts": attempts,
            }

        refreshed_result = await attempt(refreshed_url, "article_reparse")
        if refreshed_result.get("success"):
            try:
                _update_image_url(int(image_id), refreshed_url)
            except sqlite3.IntegrityError:
                # 同じブログに同一URLのレコードがあっても、画像の復元成功は維持する。
                pass
            refreshed_result["recovered_by"] = "article_reparse"
            refreshed_result["attempts"] = attempts
            return refreshed_result

        return {
            "success": False,
            "image_id": int(image_id),
            "error": str(refreshed_result.get("error") or "画像の再取得に失敗しました。"),
            "attempts": attempts,
        }
    finally:
        if own_session:
            await session.close()
