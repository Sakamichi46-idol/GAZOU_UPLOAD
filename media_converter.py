import io
import os
import asyncio
import tempfile
from urllib.parse import urlparse

import aiohttp
import discord
import imageio_ffmpeg

from low_egress_media import LOW_EGRESS_MODE, send_url_gallery
from bucket_storage import (
    bucket_is_enabled,
    build_blog_image_key,
    upload_bytes_if_missing,
)


# =========================
# 設定
# =========================

DOWNLOAD_TIMEOUT = 60

# Discordの上限ぎりぎりを避けるための余裕
UPLOAD_MARGIN = 512 * 1024

# Guild情報を取得できない場合の予備値
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024


# =========================
# 補助関数
# =========================

def format_size(size_bytes):

    return (
        f"{size_bytes / (1024 * 1024):.2f} MB"
    )


def is_gif(
    data,
    content_type,
    url
):

    content_type = (
        content_type
        or ""
    ).lower()

    path = urlparse(
        url
    ).path.lower()

    if "image/gif" in content_type:
        return True

    if path.endswith(".gif"):
        return True

    if data.startswith(b"GIF87a"):
        return True

    if data.startswith(b"GIF89a"):
        return True

    return False


def get_image_extension(
    content_type,
    url
):

    content_type = (
        content_type
        or ""
    ).lower()

    path = urlparse(
        url
    ).path.lower()

    if "image/png" in content_type:
        return ".png"

    if "image/webp" in content_type:
        return ".webp"

    if (
        "image/jpeg" in content_type
        or "image/jpg" in content_type
    ):
        return ".jpg"

    if path.endswith(".png"):
        return ".png"

    if path.endswith(".webp"):
        return ".webp"

    if (
        path.endswith(".jpeg")
        or path.endswith(".jpg")
    ):
        return ".jpg"

    return ".jpg"


def get_upload_limit(channel):

    guild = getattr(
        channel,
        "guild",
        None
    )

    if guild:

        limit = getattr(
            guild,
            "filesize_limit",
            None
        )

        if limit:

            return limit

    return DEFAULT_UPLOAD_LIMIT


# =========================
# FFmpeg実行
# =========================

async def run_ffmpeg(
    input_path,
    output_path,
    strong_compression=False
):

    ffmpeg_path = (
        imageio_ffmpeg.get_ffmpeg_exe()
    )

    if strong_compression:

        video_filter = (
            "scale="
            "'min(1280,iw)':"
            "trunc(oh/a/2)*2"
        )

        crf = "30"

        preset = "slow"

    else:

        video_filter = (
            "scale="
            "trunc(iw/2)*2:"
            "trunc(ih/2)*2"
        )

        crf = "24"

        preset = "medium"


    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-an",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path
    ]


    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )


    stdout, stderr = await process.communicate()


    if process.returncode != 0:

        error_text = stderr.decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"FFmpeg変換失敗: {error_text}"
        )


# =========================
# GIF → MP4変換
# =========================

async def convert_gif_to_mp4(
    gif_data,
    index,
    max_bytes
):

    with tempfile.TemporaryDirectory() as temp_dir:

        input_path = os.path.join(
            temp_dir,
            f"input_{index}.gif"
        )

        output_path = os.path.join(
            temp_dir,
            f"output_{index}.mp4"
        )


        with open(
            input_path,
            "wb"
        ) as file:

            file.write(
                gif_data
            )


        # 1回目：通常変換
        await run_ffmpeg(
            input_path,
            output_path,
            strong_compression=False
        )


        with open(
            output_path,
            "rb"
        ) as file:

            mp4_data = file.read()


        print(
            f"GIF→MP4変換: "
            f"{format_size(len(gif_data))}"
            f" → "
            f"{format_size(len(mp4_data))}"
        )


        if len(mp4_data) <= max_bytes:

            return mp4_data


        # 2回目：縮小して強めに圧縮
        print(
            "MP4が容量上限を超えたため、"
            "再圧縮します。"
        )


        await run_ffmpeg(
            input_path,
            output_path,
            strong_compression=True
        )


        with open(
            output_path,
            "rb"
        ) as file:

            mp4_data = file.read()


        print(
            f"GIF→MP4再圧縮: "
            f"{format_size(len(mp4_data))}"
        )


        if len(mp4_data) <= max_bytes:

            return mp4_data


        return None


# =========================
# 添付ファイル作成
# =========================

async def download_attachment(
    session,
    image_url,
    index,
    upload_limit,
    article_url="",
    group="",
):

    try:

        timeout = aiohttp.ClientTimeout(
            total=DOWNLOAD_TIMEOUT
        )


        async with session.get(
            image_url,
            timeout=timeout
        ) as response:

            if response.status != 200:

                print(
                    f"画像取得失敗: "
                    f"status={response.status} "
                    f"url={image_url}"
                )

                return {
                    "file": None,
                    "url": image_url,
                    "reason": (
                        f"HTTP {response.status}"
                    )
                }


            content_type = response.headers.get(
                "Content-Type",
                ""
            )


            data = await response.read()


        print(
            f"画像{index}: "
            f"{format_size(len(data))} "
            f"/ {content_type}"
        )


        max_bytes = max(
            upload_limit - UPLOAD_MARGIN,
            1024 * 1024
        )


        # =====================
        # GIF
        # =====================

        if is_gif(
            data,
            content_type,
            image_url
        ):

            print(
                f"GIF検出: 画像{index}"
            )


            # Bucketには変換前のオリジナルGIFを保存する。
            if bucket_is_enabled():
                try:
                    bucket_key = build_blog_image_key(
                        group=group,
                        article_url=article_url or image_url,
                        image_index=index,
                        image_bytes=data,
                        extension=".gif",
                    )

                    uploaded = await asyncio.to_thread(
                        upload_bytes_if_missing,
                        key=bucket_key,
                        content=data,
                        content_type=(
                            content_type
                            or "image/gif"
                        ),
                    )

                    print(
                        "Bucket保存:"
                        if uploaded
                        else "Bucket保存済み:",
                        bucket_key,
                    )

                except Exception as error:
                    print(
                        f"Bucket保存エラー 画像{index}:",
                        error,
                    )


            mp4_data = await convert_gif_to_mp4(
                data,
                index,
                max_bytes
            )


            if mp4_data is None:

                return {
                    "file": None,
                    "url": image_url,
                    "reason": (
                        "MP4変換後も容量上限を"
                        "超えました。"
                    )
                }


            return {
                "file": discord.File(
                    io.BytesIO(
                        mp4_data
                    ),
                    filename=f"image{index}.mp4"
                ),
                "url": image_url,
                "reason": ""
            }


        # =====================
        # 通常画像
        # =====================

        extension = get_image_extension(
            content_type,
            image_url
        )


        # 取得した同じバイト列をBucket保存とDiscord添付で共用する。
        # 元サイトからBucket用に再ダウンロードはしない。
        if bucket_is_enabled():
            try:
                bucket_key = build_blog_image_key(
                    group=group,
                    article_url=article_url or image_url,
                    image_index=index,
                    image_bytes=data,
                    extension=extension,
                )

                uploaded = await asyncio.to_thread(
                    upload_bytes_if_missing,
                    key=bucket_key,
                    content=data,
                    content_type=(
                        content_type
                        or "application/octet-stream"
                    ),
                )

                print(
                    "Bucket保存:"
                    if uploaded
                    else "Bucket保存済み:",
                    bucket_key,
                )

            except Exception as error:
                # Phase 1.1ではDiscord通知を止めない。
                # Bucket保存失敗はログへ出し、添付送信は継続する。
                print(
                    f"Bucket保存エラー 画像{index}:",
                    error,
                )


        # Bucket保存後にDiscord側の容量上限を判定する。
        # Discordへ添付できない大容量画像も、Bucketには原本を残す。
        if len(data) > max_bytes:

            return {
                "file": None,
                "url": image_url,
                "reason": (
                    "画像容量がDiscordの"
                    "アップロード上限を超えました。"
                )
            }


        return {
            "file": discord.File(
                io.BytesIO(
                    data
                ),
                filename=(
                    f"image{index}"
                    f"{extension}"
                )
            ),
            "url": image_url,
            "reason": ""
        }


    except Exception as error:

        print(
            f"画像取得・変換エラー "
            f"画像{index}:",
            error
        )


        return {
            "file": None,
            "url": image_url,
            "reason": str(error)
        }


# =========================
# ブログ画像送信
# =========================

async def send_blog_media(
    channel,
    text,
    image_urls,
    embed=None,
    send_delay=1.0,
    article_url="",
    group="",
):

    if not image_urls:

        await channel.send(
            content=(None if embed else text),
            embed=embed,
            suppress_embeds=(embed is None),
        )

        return

    # 新着ブログ通知は、画像URLの埋め込みではなくDiscord添付として送る。
    # これにより、1メッセージにつき最大10枚の画像がギャラリー形式でまとまって表示される。
    # LOW_EGRESS_MODEは写真検索などでは引き続き利用できるが、
    # 新着ブログ通知では見た目を優先して添付方式を固定する。


    upload_limit = get_upload_limit(
        channel
    )


    print(
        f"Discord容量上限: "
        f"{format_size(upload_limit)}"
    )


    failed_images = []
    attachments = []


    async with aiohttp.ClientSession() as session:

        for index, image_url in enumerate(
            image_urls,
            start=1
        ):

            attachment = await download_attachment(
                session,
                image_url,
                index,
                upload_limit,
                article_url=article_url,
                group=group,
            )


            file = attachment.get(
                "file"
            )


            if file is None:

                failed_images.append({
                    "url": image_url,
                    "reason": attachment.get(
                        "reason",
                        "不明なエラー"
                    )
                })

                continue


            attachments.append(
                file
            )


    # Discordでは1メッセージにつき最大10ファイル
    # 10枚ごとに分けて、まとめて送信する
    text_sent = False


    for start_index in range(
        0,
        len(attachments),
        10
    ):

        file_group = attachments[
            start_index:start_index + 10
        ]


        await channel.send(
            content=(
                text
                if not text_sent and embed is None
                else None
            ),
            embed=(
                embed
                if not text_sent
                else None
            ),
            files=file_group,
            suppress_embeds=(embed is None),
        )


        text_sent = True


        if (
            send_delay > 0
            and start_index + 10 < len(attachments)
        ):

            await asyncio.sleep(
                send_delay
            )


    # 全画像が失敗した場合も記事情報は送信
    if not text_sent:

        await channel.send(
            content=(None if embed else text),
            embed=embed,
            suppress_embeds=(embed is None),
        )


    # 送れなかった画像はURLを投稿
    if failed_images:

        lines = [
            "⚠️ 添付できなかった画像があります。"
        ]


        for failed in failed_images:

            lines.append(
                (
                    f"{failed['url']}\n"
                    f"理由: {failed['reason']}"
                )
            )


        failed_text = "\n\n".join(
            lines
        )


        while failed_text:

            message_part = failed_text[
                :1900
            ]

            failed_text = failed_text[
                1900:
            ]


            await channel.send(
                content=message_part,
                suppress_embeds=True
            )


            if failed_text and send_delay > 0:

                await asyncio.sleep(
                    send_delay
                )
