from __future__ import annotations

import asyncio
import os
from contextlib import closing
from typing import Any

import discord

from person_labels import normalize_people_for_storage
from discord.ext import commands

from bucket_storage import bucket_is_configured, create_presigned_get_url
from photo_database import get_connection


def _text(value: Any, fallback: str = "未登録") -> str:
    text = "" if value is None else str(value).strip()
    return text or fallback


def _truncate(value: Any, limit: int = 900, fallback: str = "未登録") -> str:
    text = _text(value, fallback)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _format_bytes(size: Any) -> str:
    try:
        value = float(max(int(size or 0), 0))
    except (TypeError, ValueError):
        value = 0.0

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} TB"


def _load_photo_details(image_id: int) -> dict[str, Any] | None:
    with closing(get_connection()) as connection:
        image = connection.execute(
            """
            SELECT
                photo_images.*,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                COALESCE(photo_ai_analysis.person_name, '') AS ai_person_name,
                COALESCE(photo_ai_analysis.clothing, '') AS clothing,
                COALESCE(photo_ai_analysis.expression, '') AS expression,
                COALESCE(photo_ai_analysis.background, '') AS background,
                COALESCE(photo_ai_analysis.pose, '') AS pose,
                COALESCE(photo_ai_analysis.objects, '') AS objects,
                COALESCE(photo_ai_analysis.person_count, 0) AS person_count,
                COALESCE(photo_ai_analysis.overall_confidence, 0) AS overall_confidence,
                COALESCE(photo_ai_analysis.needs_review, 0) AS needs_review
            FROM photo_images
            JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_ai_analysis
                ON photo_ai_analysis.image_id = photo_images.id
            WHERE photo_images.id = ?
            """,
            (int(image_id),),
        ).fetchone()

        if image is None:
            return None

        people = connection.execute(
            """
            SELECT person_name, relation_status, confidence
            FROM photo_image_people
            WHERE image_id = ?
            ORDER BY
                CASE relation_status WHEN 'confirmed' THEN 0 ELSE 1 END,
                confidence DESC,
                id ASC
            """,
            (int(image_id),),
        ).fetchall()

        ai_tags = connection.execute(
            """
            SELECT category, tag, confidence
            FROM photo_ai_tags
            WHERE image_id = ?
            ORDER BY confidence DESC, id ASC
            """,
            (int(image_id),),
        ).fetchall()

        manual_tags = connection.execute(
            """
            SELECT category, tag
            FROM photo_manual_tags
            WHERE image_id = ?
            ORDER BY id ASC
            """,
            (int(image_id),),
        ).fetchall()

        faces = connection.execute(
            """
            SELECT
                photo_faces.id AS face_id,
                photo_faces.face_index,
                photo_faces.confirmation_status,
                COALESCE(photo_people.person_name, '') AS confirmed_person_name
            FROM photo_faces
            LEFT JOIN photo_people
                ON photo_people.id = photo_faces.confirmed_person_id
            WHERE photo_faces.image_id = ?
            ORDER BY photo_faces.face_index ASC
            """,
            (int(image_id),),
        ).fetchall()

    result = dict(image)
    result["people"] = [dict(row) for row in people]
    result["ai_tags"] = [dict(row) for row in ai_tags]
    result["manual_tags"] = [dict(row) for row in manual_tags]
    result["faces"] = [dict(row) for row in faces]
    return result


def _build_embed(photo: dict[str, Any]) -> discord.Embed:
    image_id = int(photo["id"])
    blog_url = _text(photo.get("blog_url"), "")
    title = _truncate(photo.get("title"), 240, "タイトルなし")

    embed = discord.Embed(
        title=f"📷 画像ID {image_id}",
        description=f"**{title}**",
        url=blog_url or None,
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="ブログ情報",
        value=(
            f"グループ: **{_text(photo.get('group_name'))}**\n"
            f"投稿者: **{_text(photo.get('member_name'))}**\n"
            f"投稿日: **{_text(photo.get('published_at'))}**\n"
            f"記事内番号: **{int(photo.get('image_index') or 0)}**"
        ),
        inline=False,
    )

    people = photo.get("people", [])
    confirmed = normalize_people_for_storage([
        _text(item.get("person_name"), "")
        for item in people
        if item.get("relation_status") == "confirmed"
        and _text(item.get("person_name"), "")
    ])
    candidates = [
        f"{_text(item.get('person_name'), '')} ({float(item.get('confidence') or 0) * 100:.1f}%)"
        for item in people
        if item.get("relation_status") == "candidate"
        and _text(item.get("person_name"), "")
    ]

    embed.add_field(
        name="人物",
        value=(
            f"確定: **{'、'.join(confirmed) if confirmed else '未確定'}**\n"
            f"候補: {_truncate('、'.join(candidates), 700, '候補なし')}\n"
            f"AI人物名: **{_text(photo.get('ai_person_name'))}**"
        ),
        inline=False,
    )

    ai_tags = [
        f"{_text(item.get('tag'), '')} ({float(item.get('confidence') or 0) * 100:.0f}%)"
        for item in photo.get("ai_tags", [])
        if _text(item.get("tag"), "")
    ]
    manual_tags = [
        _text(item.get("tag"), "")
        for item in photo.get("manual_tags", [])
        if _text(item.get("tag"), "")
    ]

    embed.add_field(
        name="タグ",
        value=(
            f"AI: {_truncate('、'.join(ai_tags), 700, 'なし')}\n"
            f"手動: {_truncate('、'.join(manual_tags), 700, 'なし')}"
        ),
        inline=False,
    )

    analysis_parts = []
    for label, key in (
        ("服装", "clothing"),
        ("表情", "expression"),
        ("背景", "background"),
        ("ポーズ", "pose"),
        ("物体", "objects"),
    ):
        value = _text(photo.get(key), "")
        if value:
            analysis_parts.append(f"{label}: {value}")

    if analysis_parts:
        embed.add_field(
            name="AI解析",
            value=_truncate("\n".join(analysis_parts), 950),
            inline=False,
        )

    faces = photo.get("faces", [])
    face_lines = []
    for face in faces[:20]:
        person = _text(face.get("confirmed_person_name"), "未確定")
        face_lines.append(
            f"顔ID {int(face['face_id'])}: {person} / {_text(face.get('confirmation_status'))}"
        )
    if len(faces) > 20:
        face_lines.append(f"ほか {len(faces) - 20}件")

    embed.add_field(
        name=f"検出顔（{len(faces)}件）",
        value=_truncate("\n".join(face_lines), 950, "顔未検出"),
        inline=False,
    )

    embed.add_field(
        name="保存状態",
        value=(
            f"ダウンロード: **{_text(photo.get('download_status'))}**\n"
            f"AI解析: **{_text(photo.get('analysis_status'))}**\n"
            f"保存先: **{_text(photo.get('storage_backend'))}**\n"
            f"サイズ: **{_format_bytes(photo.get('file_size'))}** / "
            f"**{int(photo.get('width') or 0)}×{int(photo.get('height') or 0)}**"
        ),
        inline=False,
    )

    source_url = _text(photo.get("image_url"), "")
    if source_url:
        embed.add_field(
            name="元画像URL",
            value=f"[画像を開く]({source_url})",
            inline=False,
        )

    embed.set_footer(text="検索コマンド: !photo_id 画像ID")
    return embed


def _resolve_image_source(photo: dict[str, Any]) -> tuple[str, str]:
    local_path = _text(photo.get("local_path"), "")
    if local_path and os.path.isfile(local_path):
        return "local", local_path

    bucket_key = _text(photo.get("bucket_key"), "")
    if bucket_key and bucket_is_configured():
        try:
            signed_url = create_presigned_get_url(bucket_key)
        except Exception as error:
            print(f"[WARNING] photo_id Bucket URL生成失敗: {error!r}")
        else:
            if signed_url:
                return "url", signed_url

    image_url = _text(photo.get("image_url"), "")
    if image_url:
        return "url", image_url

    return "", ""


async def send_photo_by_id(ctx: commands.Context, image_id: int) -> bool:
    """画像IDに一致する元画像と登録情報をDiscordへ表示する。"""

    if int(image_id) <= 0:
        await ctx.send("⚠️ 画像IDは1以上の整数で指定してください。")
        return False

    photo = await asyncio.to_thread(_load_photo_details, int(image_id))
    if not photo:
        await ctx.send(f"⚠️ 画像ID **{int(image_id)}** は見つかりませんでした。")
        return False

    embed = _build_embed(photo)
    source_type, source = await asyncio.to_thread(_resolve_image_source, photo)

    if source_type == "local":
        filename = os.path.basename(source) or f"photo_{int(image_id)}.jpg"
        file = discord.File(source, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
        await ctx.send(embed=embed, file=file)
        return True

    if source_type == "url":
        embed.set_image(url=source)
        await ctx.send(embed=embed)
        return True

    embed.description = (embed.description or "") + "\n\n⚠️ 表示可能な画像ファイルまたはURLがありません。"
    await ctx.send(embed=embed)
    return True
