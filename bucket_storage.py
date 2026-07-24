"""Railway Storage Bucket utilities.

This module centralizes the S3-compatible Railway Bucket settings used by
both the photo archive and the new-blog notification bot.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


BUCKET_NAME = _env("PHOTO_BUCKET_NAME", "BUCKET")
BUCKET_ENDPOINT = _env(
    "PHOTO_BUCKET_ENDPOINT",
    "ENDPOINT",
    default="https://storage.railway.app",
)
BUCKET_REGION = _env("PHOTO_BUCKET_REGION", "REGION", default="auto")
BUCKET_ACCESS_KEY_ID = _env("PHOTO_BUCKET_ACCESS_KEY_ID", "ACCESS_KEY_ID")
BUCKET_SECRET_ACCESS_KEY = _env(
    "PHOTO_BUCKET_SECRET_ACCESS_KEY",
    "SECRET_ACCESS_KEY",
)
BUCKET_URL_TTL = max(
    60,
    min(int(_env("PHOTO_BUCKET_URL_TTL", default="604800")), 7_776_000),
)
STORAGE_MODE = _env("PHOTO_STORAGE_MODE", default="dual").lower()


def bucket_is_configured() -> bool:
    return bool(
        BUCKET_NAME
        and BUCKET_ACCESS_KEY_ID
        and BUCKET_SECRET_ACCESS_KEY
    )


def bucket_is_enabled() -> bool:
    return STORAGE_MODE in {"dual", "bucket"} and bucket_is_configured()


def keep_local_copy() -> bool:
    return STORAGE_MODE in {"local", "dual"}


def get_s3_client():
    if not bucket_is_configured():
        raise RuntimeError(
            "Railway Bucket credentials are missing. Set BUCKET, ACCESS_KEY_ID, "
            "SECRET_ACCESS_KEY, REGION and ENDPOINT."
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
        ),
    )


def upload_bytes(*, key: str, content: bytes, content_type: str) -> None:
    get_s3_client().put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=content,
        ContentType=content_type or "application/octet-stream",
        CacheControl="private, max-age=31536000, immutable",
    )


def upload_file(*, key: str, file_path: str, content_type: str) -> None:
    with open(file_path, "rb") as source:
        upload_bytes(
            key=key,
            content=source.read(),
            content_type=content_type,
        )


def object_exists(key: str) -> bool:
    try:
        get_s3_client().head_object(Bucket=BUCKET_NAME, Key=key)
        return True
    except ClientError as error:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        error_code = error.response.get("Error", {}).get("Code", "")
        if status == 404 or error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def upload_bytes_if_missing(
    *,
    key: str,
    content: bytes,
    content_type: str,
) -> bool:
    """Upload bytes only when the deterministic object key is not present.

    Returns True when a new object was uploaded and False when it already
    existed.
    """
    if object_exists(key):
        return False

    upload_bytes(
        key=key,
        content=content,
        content_type=content_type,
    )
    return True


def _safe_path_part(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    return normalized[:80] or fallback


def _article_identifier(article_url: str) -> str:
    parsed = urlparse(article_url)
    path_parts = [part for part in parsed.path.split("/") if part]

    if "detail" in path_parts:
        detail_index = path_parts.index("detail")
        if detail_index + 1 < len(path_parts):
            candidate = path_parts[detail_index + 1]
            if candidate:
                return _safe_path_part(candidate, "article")

    digest = hashlib.sha256(article_url.encode("utf-8")).hexdigest()[:16]
    return f"article-{digest}"


def build_blog_image_key(
    *,
    group: str,
    article_url: str,
    image_index: int,
    image_bytes: bytes,
    extension: str,
) -> str:
    """Create a stable Bucket key for one original blog image."""
    group_part = _safe_path_part(group, "unknown-group")
    article_part = _article_identifier(article_url)
    image_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
    safe_extension = extension.lower() if extension.startswith(".") else f".{extension.lower()}"

    return (
        f"blog-images/{group_part}/{article_part}/"
        f"{image_index:03d}-{image_hash}{safe_extension}"
    )


def create_presigned_get_url(key: str, expires_in: int | None = None) -> str:
    if not key:
        return ""

    ttl = (
        BUCKET_URL_TTL
        if expires_in is None
        else max(60, min(int(expires_in), 7_776_000))
    )
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=ttl,
    )


def download_to_file(*, key: str, file_path: str) -> str:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    get_s3_client().download_file(BUCKET_NAME, key, str(path))
    return str(path)


@contextmanager
def materialize_image(
    *,
    local_path: str,
    bucket_key: str,
    suffix: str = ".jpg",
) -> Iterator[str]:
    """Return a local path, downloading a temporary copy when required."""
    if local_path and os.path.isfile(local_path):
        yield local_path
        return

    if not bucket_key:
        raise FileNotFoundError(
            "Neither a local image nor a bucket object is available."
        )

    import tempfile

    file_descriptor, temp_path = tempfile.mkstemp(
        prefix="photo_ai_",
        suffix=suffix,
    )
    os.close(file_descriptor)

    try:
        download_to_file(key=bucket_key, file_path=temp_path)
        yield temp_path
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
