import asyncio
import hashlib
import os
import re

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image

from photo_database import (
    update_image_download,
    update_image_download_failure,
)
from bucket_storage import bucket_is_enabled, keep_local_copy, upload_bytes


# =========================
# 保存先設定
# =========================

RAILWAY_DATA_DIR = "/data"

LOCAL_DATA_DIR = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "data",
)


def get_env_int(
    name: str,
    default: int,
    minimum: int = 1,
) -> int:
    """
    環境変数を安全に整数へ変換する。
    不正な値の場合は既定値を使用する。
    """

    raw_value = os.getenv(
        name,
        str(default),
    )

    try:
        value = int(
            raw_value
        )

    except (
        TypeError,
        ValueError,
    ):
        value = default

    return max(
        value,
        minimum,
    )


def get_env_float(
    name: str,
    default: float,
    minimum: float = 0.0,
) -> float:
    """
    環境変数を安全に小数へ変換する。
    不正な値の場合は既定値を使用する。
    """

    raw_value = os.getenv(
        name,
        str(default),
    )

    try:
        value = float(
            raw_value
        )

    except (
        TypeError,
        ValueError,
    ):
        value = default

    return max(
        value,
        minimum,
    )


REQUEST_TIMEOUT = get_env_int(
    "PHOTO_DOWNLOAD_TIMEOUT",
    60,
)

MAX_FILE_SIZE = get_env_int(
    "PHOTO_DOWNLOAD_MAX_FILE_SIZE",
    50 * 1024 * 1024,
)

DOWNLOAD_INTERVAL = get_env_float(
    "PHOTO_DOWNLOAD_INTERVAL",
    0.2,
)

DOWNLOAD_CHUNK_SIZE = get_env_int(
    "PHOTO_DOWNLOAD_CHUNK_SIZE",
    256 * 1024,
)


def get_data_dir() -> str:
    """
    Railwayでは /data、
    ローカルではプロジェクト内のdataを使う。
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

PHOTO_IMAGE_DIR = os.getenv(
    "PHOTO_IMAGE_DIR",
    os.path.join(
        DATA_DIR,
        "photo_images",
    ),
).strip()


# =========================
# 文字列整理
# =========================

def sanitize_name(
    value: str,
    default: str = "unknown",
) -> str:
    """
    フォルダ名やファイル名に使えない文字を除去する。
    """

    value = str(
        value or ""
    ).strip()

    if not value:
        return default

    value = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        "_",
        value,
    )

    value = value.strip(
        "._ "
    )

    if not value:
        return default

    return value[:100]


def normalize_content_type(
    content_type: str,
) -> str:
    """
    Content-Typeから文字コードなどの余分な情報を除去する。

    例:
        image/jpeg; charset=UTF-8
        ↓
        image/jpeg
    """

    return (
        str(
            content_type or ""
        )
        .split(";")[0]
        .strip()
        .lower()
    )


def get_extension_from_content_type(
    content_type: str,
) -> str:
    """
    Content-Typeから拡張子を決める。
    """

    normalized_type = normalize_content_type(
        content_type
    )

    extension_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }

    return extension_map.get(
        normalized_type,
        "",
    )


def get_extension_from_url(
    image_url: str,
) -> str:
    """
    URLから画像拡張子を取得する。
    """

    parsed = urlparse(
        image_url
    )

    path = parsed.path.lower()

    extension = os.path.splitext(
        path
    )[1]

    allowed_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".heif",
    }

    if extension not in allowed_extensions:
        return ""

    if extension == ".jpeg":
        return ".jpg"

    if extension == ".tif":
        return ".tiff"

    return extension


def build_image_path(
    *,
    group_name: str,
    member_name: str,
    published_at: str,
    blog_id: int,
    image_index: int,
    extension: str,
) -> str:
    """
    保存先パスを作成する。

    例:
    /data/photo_images/乃木坂46/井上和/2026/07/blog_123/001.jpg
    """

    group_folder = sanitize_name(
        group_name,
        "unknown_group",
    )

    member_folder = sanitize_name(
        member_name,
        "unknown_member",
    )

    year = "unknown_year"
    month = "unknown_month"

    date_match = re.search(
        r"(\d{4})[./\-年](\d{1,2})",
        str(
            published_at or ""
        ),
    )

    if date_match:
        year = date_match.group(1)
        month = date_match.group(2).zfill(2)

    blog_folder = (
        f"blog_{int(blog_id)}"
    )

    file_name = (
        f"{int(image_index):03d}"
        f"{extension}"
    )

    folder_path = os.path.join(
        PHOTO_IMAGE_DIR,
        group_folder,
        member_folder,
        year,
        month,
        blog_folder,
    )

    os.makedirs(
        folder_path,
        exist_ok=True,
    )

    return os.path.join(
        folder_path,
        file_name,
    )


def build_bucket_key(
    *,
    blog_id: int,
    image_index: int,
    image_hash: str,
    extension: str,
) -> str:
    """Create a stable and collision-resistant object key."""
    return (
        f"photos/{int(blog_id):09d}/"
        f"{int(image_index):03d}-{image_hash[:16]}{extension}"
    )


# =========================
# 画像情報取得
# =========================

def calculate_sha256(
    content: bytes,
) -> str:
    """
    画像データのSHA-256を返す。
    """

    return hashlib.sha256(
        content
    ).hexdigest()


def get_image_dimensions(
    file_path: str,
) -> tuple[int, int]:
    """
    Pillowで画像の幅と高さを取得する。
    取得できない形式の場合は0を返す。
    """

    try:

        with Image.open(
            file_path
        ) as image:

            width, height = image.size

            return (
                int(width),
                int(height),
            )

    except Exception as error:

        print(
            "画像サイズ取得エラー:",
            file_path,
            error,
        )

        return (
            0,
            0,
        )


def remove_file_safely(
    file_path: str,
) -> None:
    """
    ファイルが存在する場合だけ削除する。
    削除失敗は処理全体を止めない。
    """

    if not file_path:
        return

    try:

        if os.path.exists(
            file_path
        ):
            os.remove(
                file_path
            )

    except OSError as error:

        print(
            "ファイル削除エラー:",
            file_path,
            error,
        )


def record_download_failure(
    image_id: int,
    error_message: str,
) -> None:
    """
    ダウンロード失敗情報をDBへ保存する。
    DB保存自体に失敗しても元のエラーを隠さない。
    """

    try:

        update_image_download_failure(
            image_id,
            str(
                error_message
            )[:1000],
        )

    except Exception as db_error:

        print(
            "画像ダウンロード失敗情報のDB保存エラー:",
            image_id,
            db_error,
        )


# =========================
# HTTP処理
# =========================

def build_request_headers(
    image_url: str,
) -> dict[str, str]:
    """
    画像取得用HTTPヘッダーを返す。
    """

    parsed = urlparse(
        image_url
    )

    referer = ""

    if parsed.scheme and parsed.netloc:
        referer = (
            f"{parsed.scheme}://"
            f"{parsed.netloc}/"
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": (
            "image/avif,image/webp,image/apng,"
            "image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Accept-Language": (
            "ja,en-US;q=0.9,en;q=0.8"
        ),
    }

    if referer:
        headers["Referer"] = referer

    return headers


async def read_response_content(
    response: aiohttp.ClientResponse,
) -> bytes:
    """
    応答本文を分割して読み込み、
    読み込み途中でもサイズ上限を監視する。
    """

    chunks: list[bytes] = []

    total_size = 0

    async for chunk in response.content.iter_chunked(
        DOWNLOAD_CHUNK_SIZE
    ):

        if not chunk:
            continue

        total_size += len(
            chunk
        )

        if total_size > MAX_FILE_SIZE:
            raise ValueError(
                "画像サイズが上限を超えています: "
                f"{total_size} bytes"
            )

        chunks.append(
            chunk
        )

    return b"".join(
        chunks
    )


# =========================
# ダウンロード本体
# =========================

async def download_photo_image(
    session: aiohttp.ClientSession,
    *,
    image_id: int,
    blog_id: int,
    image_url: str,
    image_index: int,
    group_name: str,
    member_name: str,
    published_at: str,
) -> dict[str, Any]:
    """
    画像をダウンロードし、Volumeへ保存する。
    保存後にphoto_imagesテーブルを更新する。
    """

    image_url = str(
        image_url or ""
    ).strip()

    if not image_url:

        error_message = (
            "画像URLが空です。"
        )

        record_download_failure(
            image_id,
            error_message,
        )

        return {
            "success": False,
            "image_id": image_id,
            "error": error_message,
        }

    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT
    )

    headers = build_request_headers(
        image_url
    )

    content_type = ""
    content = b""

    try:

        async with session.get(
            image_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            content_type = normalize_content_type(
                response.headers.get(
                    "Content-Type",
                    "",
                )
            )

            if (
                content_type
                and not content_type.startswith(
                    "image/"
                )
            ):

                raise ValueError(
                    "画像ではないContent-Typeです: "
                    f"{content_type}"
                )

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:

                try:
                    declared_size = int(
                        content_length
                    )

                except ValueError:
                    declared_size = 0

                if declared_size > MAX_FILE_SIZE:

                    raise ValueError(
                        "画像サイズが上限を超えています: "
                        f"{declared_size} bytes"
                    )

            content = await read_response_content(
                response
            )

    except Exception as error:

        error_message = (
            f"{type(error).__name__}: {error}"
        )

        print(
            "写真画像ダウンロードエラー:",
            image_url,
            error_message,
        )

        record_download_failure(
            image_id,
            error_message,
        )

        return {
            "success": False,
            "image_id": image_id,
            "error": error_message,
        }

    if not content:

        error_message = (
            "画像データが空です。"
        )

        record_download_failure(
            image_id,
            error_message,
        )

        return {
            "success": False,
            "image_id": image_id,
            "error": error_message,
        }

    extension = get_extension_from_content_type(
        content_type
    )

    if not extension:

        extension = get_extension_from_url(
            image_url
        )

    if not extension:
        extension = ".jpg"

    file_path = build_image_path(
        group_name=group_name,
        member_name=member_name,
        published_at=published_at,
        blog_id=blog_id,
        image_index=image_index,
        extension=extension,
    )

    temporary_path = (
        file_path
        + ".part"
    )

    try:

        with open(
            temporary_path,
            "wb",
        ) as file:

            file.write(
                content
            )

        os.replace(
            temporary_path,
            file_path,
        )

    except Exception as error:

        remove_file_safely(
            temporary_path
        )

        error_message = (
            f"{type(error).__name__}: {error}"
        )

        print(
            "写真画像保存エラー:",
            file_path,
            error_message,
        )

        record_download_failure(
            image_id,
            error_message,
        )

        return {
            "success": False,
            "image_id": image_id,
            "error": error_message,
        }

    width, height = get_image_dimensions(
        file_path
    )

    image_hash = calculate_sha256(
        content
    )

    file_size = len(
        content
    )

    file_name = os.path.basename(
        file_path
    )

    bucket_key = ""
    bucket_status = "disabled"
    bucket_error = ""
    storage_backend = "local"

    if bucket_is_enabled():
        bucket_key = build_bucket_key(
            blog_id=blog_id,
            image_index=image_index,
            image_hash=image_hash,
            extension=extension,
        )
        try:
            upload_bytes(
                key=bucket_key,
                content=content,
                content_type=content_type or "application/octet-stream",
            )
            bucket_status = "completed"
            storage_backend = "bucket"
        except Exception as error:
            bucket_status = "failed"
            bucket_error = f"{type(error).__name__}: {error}"[:1000]
            if not keep_local_copy():
                remove_file_safely(file_path)
                record_download_failure(image_id, "Bucket upload failed: " + bucket_error)
                return {"success": False, "image_id": image_id, "error": bucket_error}

    local_path_for_db = file_path
    if bucket_status == "completed" and not keep_local_copy():
        remove_file_safely(file_path)
        local_path_for_db = ""

    try:

        update_image_download(
            image_id,
            local_path=local_path_for_db,
            file_name=file_name,
            mime_type=content_type,
            file_size=file_size,
            width=width,
            height=height,
            image_hash=image_hash,
            status="completed",
            bucket_key=bucket_key,
            bucket_status=bucket_status,
            bucket_error=bucket_error,
            storage_backend=storage_backend,
        )

    except Exception as error:

        # DBへ保存できなかった画像ファイルだけが
        # Volumeへ残るのを防ぐ。
        remove_file_safely(
            file_path
        )

        error_message = (
            f"{type(error).__name__}: {error}"
        )

        print(
            "写真画像DB更新エラー:",
            image_id,
            error_message,
        )

        record_download_failure(
            image_id,
            error_message,
        )

        return {
            "success": False,
            "image_id": image_id,
            "error": error_message,
        }

    print(
        "写真画像保存完了:",
        image_id,
        file_path,
    )

    return {
        "success": True,
        "image_id": image_id,
        "file_path": local_path_for_db,
        "bucket_key": bucket_key,
        "file_name": file_name,
        "mime_type": content_type,
        "file_size": file_size,
        "width": width,
        "height": height,
        "image_hash": image_hash,
    }


async def download_blog_images(
    session: aiohttp.ClientSession,
    *,
    blog_id: int,
    blog: dict[str, Any],
    image_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    ブログ1件分の画像を順番に保存する。
    """

    success_count = 0
    failed_count = 0

    results: list[
        dict[str, Any]
    ] = []

    group_name = str(
        blog.get(
            "group",
            "",
        )
    )

    member_name = str(
        blog.get(
            "member",
            "",
        )
    )

    published_at = str(
        blog.get(
            "date",
            "",
        )
    )

    for image_record in image_records:

        try:

            image_id = int(
                image_record[
                    "image_id"
                ]
            )

            image_url = str(
                image_record[
                    "image_url"
                ]
            )

            image_index = int(
                image_record[
                    "image_index"
                ]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ) as error:

            failed_count += 1

            results.append(
                {
                    "success": False,
                    "image_id": 0,
                    "error": (
                        "画像レコード形式エラー: "
                        f"{error}"
                    ),
                }
            )

            continue

        result = await download_photo_image(
            session,
            image_id=image_id,
            blog_id=blog_id,
            image_url=image_url,
            image_index=image_index,
            group_name=group_name,
            member_name=member_name,
            published_at=published_at,
        )

        results.append(
            result
        )

        if result.get(
            "success"
        ):

            success_count += 1

        else:

            failed_count += 1

        if DOWNLOAD_INTERVAL > 0:

            await asyncio.sleep(
                DOWNLOAD_INTERVAL
            )

    return {
        "total": len(
            image_records
        ),
        "success": success_count,
        "failed": failed_count,
        "results": results,
    }


# =========================
# 保存先確認
# =========================

def get_photo_storage_path() -> str:
    """
    画像保存先を返す。
    """

    os.makedirs(
        PHOTO_IMAGE_DIR,
        exist_ok=True,
    )

    return PHOTO_IMAGE_DIR


# =========================
# 単体実行
# =========================

if __name__ == "__main__":

    path = get_photo_storage_path()

    print(
        "写真画像保存先:",
        path,
    )

    print(
        "ダウンロードタイムアウト:",
        REQUEST_TIMEOUT,
    )

    print(
        "最大ファイルサイズ:",
        MAX_FILE_SIZE,
    )

    print(
        "ダウンロード間隔:",
        DOWNLOAD_INTERVAL,
    )
