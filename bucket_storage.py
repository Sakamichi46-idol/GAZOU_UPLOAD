"""Railway Storage Bucket utilities.

Railway Storage Bucket は S3 互換 API を使用する。
このモジュールでは、写真アーカイブと新着ブログ通知で利用する
Bucket 関連処理を一か所にまとめる。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


# =========================
# 環境変数
# =========================


def _env(*names: str, default: str = "") -> str:
    """候補名を順番に確認し、最初に見つかった環境変数を返す。"""

    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value

    return default


def _env_int(
    *names: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """整数環境変数を安全に読み込む。"""

    raw_value = _env(*names, default=str(default))

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(value, maximum))


BUCKET_NAME = _env("PHOTO_BUCKET_NAME", "BUCKET")
BUCKET_ENDPOINT = _env(
    "PHOTO_BUCKET_ENDPOINT",
    "ENDPOINT",
    default="https://storage.railway.app",
).rstrip("/")
BUCKET_REGION = _env(
    "PHOTO_BUCKET_REGION",
    "REGION",
    default="auto",
)
BUCKET_ACCESS_KEY_ID = _env(
    "PHOTO_BUCKET_ACCESS_KEY_ID",
    "ACCESS_KEY_ID",
)
BUCKET_SECRET_ACCESS_KEY = _env(
    "PHOTO_BUCKET_SECRET_ACCESS_KEY",
    "SECRET_ACCESS_KEY",
)
BUCKET_URL_TTL = _env_int(
    "PHOTO_BUCKET_URL_TTL",
    default=604800,
    minimum=60,
    maximum=7_776_000,
)
STORAGE_MODE = _env(
    "PHOTO_STORAGE_MODE",
    default="dual",
).lower()

# S3 接続に時間がかかり続けないように上限を設ける。
BUCKET_CONNECT_TIMEOUT = _env_int(
    "BUCKET_CONNECT_TIMEOUT",
    default=10,
    minimum=1,
    maximum=60,
)
BUCKET_READ_TIMEOUT = _env_int(
    "BUCKET_READ_TIMEOUT",
    default=60,
    minimum=5,
    maximum=300,
)
BUCKET_MAX_ATTEMPTS = _env_int(
    "BUCKET_MAX_ATTEMPTS",
    default=3,
    minimum=1,
    maximum=10,
)


# 同一プロセス内で、保存済みと確認できたキーを記録する。
# 同じ記事を複数チャンネルへ通知する際の HEAD リクエストを減らす。
_known_object_keys: set[str] = set()
_known_object_keys_lock = threading.Lock()


# =========================
# Bucket状態
# =========================


def bucket_is_configured() -> bool:
    """Bucket接続に必要な設定が揃っているか返す。"""

    return bool(
        BUCKET_NAME
        and BUCKET_ENDPOINT
        and BUCKET_ACCESS_KEY_ID
        and BUCKET_SECRET_ACCESS_KEY
    )


def bucket_is_enabled() -> bool:
    """現在の保存モードでBucket保存を利用するか返す。"""

    return (
        STORAGE_MODE in {"dual", "bucket"}
        and bucket_is_configured()
    )


def keep_local_copy() -> bool:
    """ローカルまたはVolumeにも画像を残す設定か返す。"""

    return STORAGE_MODE in {"local", "dual"}


def validate_bucket_settings() -> list[str]:
    """不足している必須設定名を返す。"""

    missing: list[str] = []

    if not BUCKET_NAME:
        missing.append("BUCKET")
    if not BUCKET_ENDPOINT:
        missing.append("ENDPOINT")
    if not BUCKET_ACCESS_KEY_ID:
        missing.append("ACCESS_KEY_ID")
    if not BUCKET_SECRET_ACCESS_KEY:
        missing.append("SECRET_ACCESS_KEY")

    return missing


# =========================
# S3クライアント
# =========================


@lru_cache(maxsize=1)
def get_s3_client():
    """再利用可能なS3クライアントを返す。"""

    missing = validate_bucket_settings()

    if missing:
        raise RuntimeError(
            "Railway Bucketの設定が不足しています: "
            + ", ".join(missing)
        )

    return boto3.client(
        "s3",
        endpoint_url=BUCKET_ENDPOINT,
        region_name=BUCKET_REGION,
        aws_access_key_id=BUCKET_ACCESS_KEY_ID,
        aws_secret_access_key=BUCKET_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            connect_timeout=BUCKET_CONNECT_TIMEOUT,
            read_timeout=BUCKET_READ_TIMEOUT,
            retries={
                "max_attempts": BUCKET_MAX_ATTEMPTS,
                "mode": "standard",
            },
        ),
    )


# =========================
# 汎用アップロード・取得
# =========================


def _normalize_key(key: str) -> str:
    """Bucketキーを安全な相対パスへ整形する。"""

    normalized = str(key or "").replace("\\", "/").strip("/")

    while "//" in normalized:
        normalized = normalized.replace("//", "/")

    if not normalized:
        raise ValueError("Bucket key is empty.")

    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("Invalid Bucket key.")

    return normalized


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def upload_bytes(
    *,
    key: str,
    content: bytes,
    content_type: str,
) -> None:
    """バイト列をBucketへ保存する。"""

    normalized_key = _normalize_key(key)

    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes or bytearray.")

    content_bytes = bytes(content)
    sha256 = _content_sha256(content_bytes)

    get_s3_client().put_object(
        Bucket=BUCKET_NAME,
        Key=normalized_key,
        Body=content_bytes,
        ContentType=(
            content_type
            or "application/octet-stream"
        ),
        CacheControl="private, max-age=31536000, immutable",
        Metadata={
            "sha256": sha256,
        },
    )

    with _known_object_keys_lock:
        _known_object_keys.add(normalized_key)


def upload_file(
    *,
    key: str,
    file_path: str,
    content_type: str,
) -> None:
    """ローカルファイルをBucketへ保存する。"""

    path = Path(file_path)

    if not path.is_file():
        raise FileNotFoundError(str(path))

    resolved_content_type = (
        content_type
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )

    with path.open("rb") as source:
        upload_bytes(
            key=key,
            content=source.read(),
            content_type=resolved_content_type,
        )


def object_exists(key: str) -> bool:
    """指定したキーがBucketに存在するか確認する。"""

    normalized_key = _normalize_key(key)

    with _known_object_keys_lock:
        if normalized_key in _known_object_keys:
            return True

    try:
        get_s3_client().head_object(
            Bucket=BUCKET_NAME,
            Key=normalized_key,
        )

        with _known_object_keys_lock:
            _known_object_keys.add(normalized_key)

        return True

    except ClientError as error:
        response = error.response or {}
        status = response.get(
            "ResponseMetadata",
            {},
        ).get("HTTPStatusCode")
        error_code = str(
            response.get("Error", {}).get("Code", "")
        )

        if (
            status == 404
            or error_code in {
                "404",
                "NoSuchKey",
                "NotFound",
            }
        ):
            return False

        raise


def upload_bytes_if_missing(
    *,
    key: str,
    content: bytes,
    content_type: str,
) -> bool:
    """未保存の場合だけアップロードする。

    Returns:
        新しく保存した場合は ``True``、保存済みの場合は ``False``。
    """

    normalized_key = _normalize_key(key)

    if object_exists(normalized_key):
        return False

    upload_bytes(
        key=normalized_key,
        content=content,
        content_type=content_type,
    )

    return True


def create_presigned_get_url(
    key: str,
    expires_in: int | None = None,
) -> str:
    """一時的に閲覧できる署名付きURLを生成する。"""

    if not key:
        return ""

    normalized_key = _normalize_key(key)
    ttl = (
        BUCKET_URL_TTL
        if expires_in is None
        else max(60, min(int(expires_in), 7_776_000))
    )

    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": normalized_key,
        },
        ExpiresIn=ttl,
    )


def download_to_file(
    *,
    key: str,
    file_path: str,
) -> str:
    """Bucketのオブジェクトをローカルファイルへ保存する。"""

    normalized_key = _normalize_key(key)
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    get_s3_client().download_file(
        BUCKET_NAME,
        normalized_key,
        str(path),
    )

    return str(path)


# =========================
# 新着ブログ画像用キー
# =========================


def _safe_path_part(value: str, fallback: str) -> str:
    """Bucketのパス要素に利用できる文字列へ変換する。"""

    value = unquote(str(value or "")).strip().lower()
    normalized = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "-",
        value,
    )
    normalized = normalized.strip("-")

    return normalized[:80] or fallback


def _group_identifier(group: str) -> str:
    """日本語グループ名を安定した英数字名へ変換する。"""

    compact = re.sub(r"\s+", "", str(group or "")).lower()

    aliases = {
        "乃木坂46": "nogizaka46",
        "nogizaka46": "nogizaka46",
        "nogizaka": "nogizaka46",
        "櫻坂46": "sakurazaka46",
        "桜坂46": "sakurazaka46",
        "sakurazaka46": "sakurazaka46",
        "sakurazaka": "sakurazaka46",
        "日向坂46": "hinatazaka46",
        "hinatazaka46": "hinatazaka46",
        "hinatazaka": "hinatazaka46",
    }

    if compact in aliases:
        return aliases[compact]

    return _safe_path_part(compact, "unknown-group")


def _article_identifier(article_url: str) -> str:
    """記事URLから記事IDを取り出す。"""

    parsed = urlparse(str(article_url or ""))
    path_parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    if "detail" in path_parts:
        detail_index = path_parts.index("detail")

        if detail_index + 1 < len(path_parts):
            candidate = path_parts[detail_index + 1]

            if candidate:
                return _safe_path_part(
                    candidate,
                    "article",
                )

    # detail形式でないURLでも、最後のパス要素があれば利用する。
    if path_parts:
        candidate = _safe_path_part(
            path_parts[-1],
            "",
        )

        if candidate:
            return candidate

    digest = hashlib.sha256(
        str(article_url or "").encode("utf-8")
    ).hexdigest()[:16]

    return f"article-{digest}"


def _normalize_extension(extension: str) -> str:
    """画像拡張子を安全な形式へそろえる。"""

    normalized = str(extension or ".jpg").strip().lower()

    if not normalized.startswith("."):
        normalized = f".{normalized}"

    aliases = {
        ".jpeg": ".jpg",
        ".jpe": ".jpg",
    }
    normalized = aliases.get(normalized, normalized)

    if not re.fullmatch(r"\.[a-z0-9]{1,10}", normalized):
        return ".jpg"

    return normalized


def build_blog_image_key(
    *,
    group: str,
    article_url: str,
    image_index: int,
    image_bytes: bytes,
    extension: str,
) -> str:
    """ブログ画像の安定したBucketキーを生成する。

    同じ記事・同じ並び順・同じ画像データからは、常に同じキーになる。
    """

    if not isinstance(image_bytes, (bytes, bytearray)):
        raise TypeError("image_bytes must be bytes or bytearray.")

    group_part = _group_identifier(group)
    article_part = _article_identifier(article_url)
    image_hash = hashlib.sha256(
        bytes(image_bytes)
    ).hexdigest()[:16]
    safe_extension = _normalize_extension(extension)
    safe_index = max(1, int(image_index))

    return (
        f"blog-images/{group_part}/{article_part}/"
        f"{safe_index:03d}-{image_hash}{safe_extension}"
    )


# =========================
# AI処理用一時ファイル
# =========================


@contextmanager
def materialize_image(
    *,
    local_path: str,
    bucket_key: str,
    suffix: str = ".jpg",
) -> Iterator[str]:
    """利用可能な画像のローカルパスを一時的に提供する。"""

    if local_path and os.path.isfile(local_path):
        yield local_path
        return

    if not bucket_key:
        raise FileNotFoundError(
            "ローカル画像とBucket画像のどちらも利用できません。"
        )

    import tempfile

    file_descriptor, temp_path = tempfile.mkstemp(
        prefix="photo_ai_",
        suffix=_normalize_extension(suffix),
    )
    os.close(file_descriptor)

    try:
        download_to_file(
            key=bucket_key,
            file_path=temp_path,
        )
        yield temp_path

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
