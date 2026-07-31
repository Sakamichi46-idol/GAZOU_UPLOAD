import asyncio
import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands

from photo_ai_analyzer import analyze_photo_image
from archive_image_getter import get_images as get_article_images
from photo_database import (
    add_review_item,
    complete_review_item,
    get_ai_cost_summary,
    get_all_people,
    get_image_people,
    get_person_by_name,
    confirm_face_person,
    complete_face_review,
    get_pending_face_reviews,
    set_confirmed_image_people,
    get_connection,
    get_photo_db_counts,
    get_photo_image,
    get_photo_storage_stats,
    init_photo_db,
    PHOTO_DB_PATH,
    reset_image_analysis_status,
    reset_image_download_status,
    update_image_download_terminal_failure,
    save_manual_tag,
)
from photo_image_downloader import download_photo_image
from photo_search import (
    send_photo_author_search_results,
    send_photo_blog_search_results,
    send_photo_person_search_results,
    send_photo_search_results,
    send_photo_tag_search_results,
)
from photo_review_view import (
    send_next_person_review,
    send_person_review,
    send_person_review_batch,
    send_skipped_person_review_batch,
)
from photo_tag_explorer import send_photo_tag_explorer
from photo_face_review_view import (
    send_face_review_batch,
    send_fast_face_review,
)
from photo_face_cluster_view import send_face_cluster_review
from photo_id_view import send_photo_by_id
from local_face_recognition import (
    FaceEngineUnavailable,
    MAX_BATCH_SCAN,
    detect_faces_for_image,
    get_face_engine_status,
    get_face_summary,
    get_face_crop_bytes,
    scan_faces_batch,
    suggest_face_candidates,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        cursor = connection.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _rows(query, params)
    return rows[0] if rows else None


def _execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with closing(get_connection()) as connection:
        cursor = connection.execute(query, params)
        connection.commit()
        return int(cursor.rowcount)


def _format_bytes(size: int) -> str:
    value = float(max(int(size or 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _get_redownload_record(image_id: int) -> dict[str, Any] | None:
    return _row(
        """
        SELECT
            photo_images.id,
            photo_images.blog_id,
            photo_images.image_url,
            photo_images.image_index,
            photo_blogs.group_name,
            photo_blogs.member_name,
            photo_blogs.published_at,
            photo_blogs.blog_url
        FROM photo_images
        JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
        WHERE photo_images.id = ?
        """,
        (image_id,),
    )


def _update_redownload_image_url(image_id: int, image_url: str) -> None:
    """再解析で見つけた現行URLを、同じ画像レコードへ反映する。"""

    clean_url = str(image_url or "").strip()
    if not clean_url:
        return

    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE photo_images
            SET image_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (clean_url, _now(), image_id),
        )
        connection.commit()


def _is_supported_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


async def _mark_terminal_download_failure(
    image_id: int,
    status: str,
    message: str,
) -> None:
    await asyncio.to_thread(
        update_image_download_terminal_failure,
        image_id,
        status,
        message,
    )


def _looks_like_not_found(error_message: str) -> bool:
    normalized = str(error_message or "").lower()
    return (
        "404" in normalized
        or "not found" in normalized
    )


async def _redownload_one(session: aiohttp.ClientSession, image_id: int) -> dict[str, Any]:
    """失敗画像を再取得する。404時は元記事を再解析してURLも更新する。"""

    record = await asyncio.to_thread(_get_redownload_record, image_id)
    if not record:
        return {
            "success": False,
            "image_id": image_id,
            "error": "画像IDが見つかりません。",
        }

    original_url = str(record.get("image_url") or "").strip()
    if not _is_supported_http_url(original_url):
        message = f"不正な画像URLのため再試行対象から除外しました: {original_url[:300]}"
        await _mark_terminal_download_failure(image_id, "invalid_url", message)
        return {
            "success": False,
            "image_id": image_id,
            "error": message,
            "terminal_status": "invalid_url",
        }

    await asyncio.to_thread(reset_image_download_status, image_id)

    async def attempt(image_url: str) -> dict[str, Any]:
        return await download_photo_image(
            session,
            image_id=int(record["id"]),
            blog_id=int(record["blog_id"]),
            image_url=image_url,
            image_index=int(record["image_index"]),
            group_name=str(record["group_name"]),
            member_name=str(record["member_name"]),
            published_at=str(record["published_at"]),
        )

    result = await attempt(original_url)
    if result.get("success"):
        result["recovered_by"] = "original_url"
        return result

    original_error = str(result.get("error") or "")
    blog_url = str(record.get("blog_url") or "").strip()

    if not blog_url or not _looks_like_not_found(original_error):
        return result

    try:
        refreshed_urls = await get_article_images(blog_url)
    except Exception as error:
        error_message = (
            f"元記事の再解析にも失敗: {type(error).__name__}: {error}"
        )
        print(
            "写真再ダウンロード再解析エラー:",
            f"image_id={image_id}",
            error_message,
        )
        result["refresh_error"] = error_message
        return result

    image_index = int(record.get("image_index") or 0)
    refreshed_url = (
        refreshed_urls[image_index - 1]
        if image_index >= 1 and image_index <= len(refreshed_urls)
        else ""
    )

    if not refreshed_url:
        result["refresh_error"] = (
            f"元記事には画像{image_index}枚目が見つかりません "
            f"(現在の抽出数: {len(refreshed_urls)}件)"
        )
        return result

    refreshed_url = str(refreshed_url).strip()
    if not _is_supported_http_url(refreshed_url):
        message = f"元記事から不正な画像URLが取得されました: {refreshed_url[:300]}"
        await _mark_terminal_download_failure(image_id, "invalid_url", message)
        result["refresh_error"] = message
        result["terminal_status"] = "invalid_url"
        return result

    if refreshed_url == original_url:
        message = "元記事を再解析しても画像URLが変わらず、元URLも404でした。"
        await _mark_terminal_download_failure(
            image_id,
            "permanent_failed",
            message,
        )
        result["refresh_error"] = message
        result["terminal_status"] = "permanent_failed"
        return result

    print(
        "写真再ダウンロードURL更新候補:",
        f"image_id={image_id}",
        f"old={original_url}",
        f"new={refreshed_url}",
    )

    refreshed_result = await attempt(refreshed_url)
    if refreshed_result.get("success"):
        try:
            await asyncio.to_thread(
                _update_redownload_image_url,
                image_id,
                refreshed_url,
            )
        except sqlite3.IntegrityError as error:
            # 同一ブログ内に同じURLの別レコードがある場合でも、
            # Bucket保存自体は成功しているため成功扱いを維持する。
            print(
                "写真再ダウンロードURL更新をスキップ:",
                f"image_id={image_id}",
                f"{type(error).__name__}: {error}",
            )
        refreshed_result["recovered_by"] = "article_reparse"
        refreshed_result["old_url"] = original_url
        refreshed_result["new_url"] = refreshed_url
        return refreshed_result

    refreshed_result["original_error"] = original_error
    refreshed_result["refreshed_url"] = refreshed_url
    refreshed_error = str(refreshed_result.get("error") or "")
    if _looks_like_not_found(refreshed_error):
        message = (
            "元記事の現在URLでも404だったため、再試行対象から除外しました。"
        )
        await _mark_terminal_download_failure(
            image_id,
            "permanent_failed",
            message,
        )
        refreshed_result["refresh_error"] = message
        refreshed_result["terminal_status"] = "permanent_failed"
    return refreshed_result


def register_photo_commands(bot: commands.Bot) -> None:
    # 顔一括処理は同時に1本だけ実行する。
    # Railway上で重複実行するとCPU・メモリ・Bucket通信が競合するため。
    face_scan_lock = asyncio.Lock()
    face_scan_stop_event = asyncio.Event()
    face_relearn_lock = asyncio.Lock()

    @bot.command(name="photo_tags", aliases=["tag_search", "photo_explorer"])
    async def photo_tags_command(ctx: commands.Context) -> None:
        """ボタンと選択メニューで写真タグを絞り込む。"""
        await send_photo_tag_explorer(ctx)

    @bot.command(name="photo_search", aliases=["search"])
    async def photo_search_command(ctx: commands.Context, *, query: str = "") -> None:
        """人物・タグ・ブログ情報・AI解析を横断検索する。"""
        await send_photo_search_results(ctx, query)

    @bot.command(name="person")
    async def photo_person_search_command(ctx: commands.Context, *, person_name: str = "") -> None:
        """確認済み人物だけを対象に写真を検索する。"""
        await send_photo_person_search_results(ctx, person_name)

    @bot.command(name="tag")
    async def photo_tag_search_command(ctx: commands.Context, *, tag: str = "") -> None:
        """AIタグと手動タグを横断検索する。"""
        await send_photo_tag_search_results(ctx, tag)

    @bot.command(name="blog")
    async def photo_blog_search_command(ctx: commands.Context, *, query: str = "") -> None:
        """ブログ投稿者・タイトル・グループを検索する。"""
        await send_photo_blog_search_results(ctx, query)

    @bot.command(name="photo_search_author")
    async def photo_search_author_command(ctx: commands.Context, *, author_name: str = "") -> None:
        await send_photo_author_search_results(ctx, author_name)


    @bot.command(name="photo_id")
    async def photo_id_command(ctx: commands.Context, image_id: int) -> None:
        """画像IDから元画像と登録情報を表示する。"""
        await send_photo_by_id(ctx, image_id)

    @bot.command(name="photo_person_set")
    @commands.is_owner()
    async def photo_person_set_command(ctx: commands.Context, image_id: int, *, person_names: str = "") -> None:
        names = [name.strip() for name in person_names.replace("、", ",").split(",") if name.strip()]
        if not names:
            await ctx.send("使い方: `!photo_person_set 画像ID 人物名`\n複数人: `!photo_person_set 125 菅原咲月,井上和`")
            return
        if not await asyncio.to_thread(get_photo_image, image_id):
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return
        await asyncio.to_thread(
            set_confirmed_image_people, image_id, names,
            confirmed_by=str(ctx.author.id), note="Discord command",
        )
        await ctx.send(f"✅ 画像ID **{image_id}** の人物を **{'、'.join(names)}** として確定しました。")

    @bot.command(name="photo_person_clear")
    @commands.is_owner()
    async def photo_person_clear_command(ctx: commands.Context, image_id: int) -> None:
        if not await asyncio.to_thread(get_photo_image, image_id):
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return
        await asyncio.to_thread(
            set_confirmed_image_people, image_id, [],
            confirmed_by=str(ctx.author.id), note="人物なし・判定解除",
        )
        await ctx.send(f"🧹 画像ID **{image_id}** の確定人物を解除しました。")

    @bot.command(name="person_list")
    
    async def person_list_command(ctx: commands.Context) -> None:
        people = await asyncio.to_thread(get_all_people)
        if not people:
            await ctx.send("👤 登録人物はまだありません。")
            return

        lines = [f"{index}. {person.get('person_name', '名称不明')}" for index, person in enumerate(people, 1)]
        text = "👤 **登録人物一覧**\n" + "\n".join(lines)
        for start in range(0, len(text), 1900):
            await ctx.send(text[start:start + 1900])

    @bot.command(name="person_info")
    
    async def person_info_command(ctx: commands.Context, *, person_name: str = "") -> None:
        person_name = person_name.strip()
        if not person_name:
            await ctx.send("使い方: `!person_info 人物名`")
            return

        info = await asyncio.to_thread(
            _row,
            """
            SELECT
                photo_people.id,
                photo_people.person_name,
                photo_people.group_name,
                photo_people.generation_name,
                COUNT(DISTINCT photo_faces.id) AS confirmed_faces
            FROM photo_people
            LEFT JOIN photo_faces
                ON photo_faces.confirmed_person_id = photo_people.id
            WHERE photo_people.person_name = ?
            GROUP BY photo_people.id
            """,
            (person_name,),
        )
        ai_count = await asyncio.to_thread(
            _row,
            "SELECT COUNT(*) AS count FROM photo_ai_analysis WHERE person_name = ?",
            (person_name,),
        )
        if not info and not ai_count:
            await ctx.send("⚠️ その人物は見つかりませんでした。")
            return

        await ctx.send(
            "👤 **人物情報**\n"
            f"名前: **{person_name}**\n"
            f"グループ: **{(info or {}).get('group_name', '') or '未登録'}**\n"
            f"世代: **{(info or {}).get('generation_name', '') or '未登録'}**\n"
            f"AI解析画像: **{int((ai_count or {}).get('count', 0))}件**\n"
            f"顔確認済み: **{int((info or {}).get('confirmed_faces', 0))}件**"
        )

    @bot.command(name="tag_add")
    @commands.is_owner()
    async def tag_add_command(ctx: commands.Context, image_id: int, *, tag: str = "") -> None:
        tag = tag.strip()
        if not tag:
            await ctx.send("使い方: `!tag_add 画像ID タグ`")
            return
        image = await asyncio.to_thread(get_photo_image, image_id)
        if not image:
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return
        await asyncio.to_thread(
            save_manual_tag,
            image_id,
            category="manual",
            tag=tag,
            created_by=str(ctx.author.id),
            note="Discord command",
        )
        await ctx.send(f"🏷️ 画像ID **{image_id}** に `{tag}` を追加しました。")

    @bot.command(name="tag_remove")
    @commands.is_owner()
    async def tag_remove_command(ctx: commands.Context, image_id: int, *, tag: str = "") -> None:
        tag = tag.strip()
        if not tag:
            await ctx.send("使い方: `!tag_remove 画像ID タグ`")
            return
        deleted = await asyncio.to_thread(
            _execute,
            "DELETE FROM photo_manual_tags WHERE image_id = ? AND tag = ?",
            (image_id, tag),
        )
        if deleted:
            await ctx.send(f"🗑️ 画像ID **{image_id}** から `{tag}` を削除しました。")
        else:
            await ctx.send("⚠️ 指定されたタグは見つかりませんでした。")


    @bot.command(name="face_status")
    @commands.is_owner()
    async def face_status_command(ctx: commands.Context) -> None:
        status = await asyncio.to_thread(get_face_engine_status)
        if not status.get("available"):
            await ctx.send(
                "🧩 **ローカル顔認識: 未導入**\n"
                f"{status.get('error', 'OpenCVを利用できません。')}\n"
                "`requirements-face.txt` の依存関係を `requirements.txt` に追加すると有効化できます。"
            )
            return
        counts = await asyncio.to_thread(get_photo_db_counts)
        eligible_row = await asyncio.to_thread(
            _row,
            """
            SELECT COUNT(*) AS count
            FROM photo_images
            WHERE download_status = 'completed'
              AND (local_path != '' OR bucket_key != '')
            """,
        )
        images_with_faces_row = await asyncio.to_thread(
            _row,
            "SELECT COUNT(DISTINCT image_id) AS count FROM photo_faces",
        )

        eligible_images = int((eligible_row or {}).get("count") or 0)
        scanned_images = int(counts.get("face_scanned_images") or 0)
        failed_images = int(counts.get("face_scan_failed_images") or 0)
        remaining_images = max(eligible_images - scanned_images, 0)
        images_with_faces = int((images_with_faces_row or {}).get("count") or 0)
        progress = (scanned_images / eligible_images * 100.0) if eligible_images else 0.0
        detection_rate = (images_with_faces / scanned_images * 100.0) if scanned_images else 0.0

        await ctx.send(
            "✅ **ローカル顔認識: 利用可能**\n"
            f"モデル: `{status.get('model_name')}`\n"
            f"OpenCV: `{status.get('opencv_version')}` / NumPy: `{status.get('numpy_version')}`\n\n"
            "📊 **顔スキャン進捗**\n"
            f"対象画像: **{eligible_images:,}枚**\n"
            f"スキャン済み: **{scanned_images:,}枚**（{progress:.1f}%）\n"
            f"未スキャン: **{remaining_images:,}枚**\n"
            f"失敗記録: **{failed_images:,}枚**\n\n"
            "👤 **顔データ**\n"
            f"顔を検出した画像: **{images_with_faces:,}枚**（スキャン済みの{detection_rate:.1f}%）\n"
            f"検出した顔: **{int(counts.get('faces') or 0):,}件**\n"
            f"参照登録済み: **{int(counts.get('confirmed_faces') or 0):,}件**\n"
            f"人物候補: **{int(counts.get('face_candidates') or 0):,}件**\n"
            f"顔確認待ち: **{int(counts.get('pending_face_reviews') or 0):,}件**\n\n"
            "OpenAI APIは使用しません。顔が0件だった画像もスキャン済みとして記録します。"
        )

    @bot.command(name="face_scan")
    @commands.is_owner()
    async def face_scan_command(ctx: commands.Context, image_id: int) -> None:
        await ctx.send(f"🔍 画像ID **{image_id}** をローカル顔検出しています…")
        try:
            result = await asyncio.to_thread(detect_faces_for_image, image_id)
        except FaceEngineUnavailable as error:
            await ctx.send(f"⚠️ {error}")
            return
        except Exception as error:
            await ctx.send(f"❌ 顔検出に失敗しました: `{type(error).__name__}: {error}`")
            return
        await ctx.send(
            f"✅ 顔検出完了: **{result['detected']}件**\n"
            f"顔ID: **{', '.join(map(str, result['face_ids'])) or 'なし'}**\n"
            f"安全条件で自動参照登録: **{result['auto_confirmed']}件**"
        )

    @bot.command(name="face_suggest")
    @commands.is_owner()
    async def face_suggest_command(ctx: commands.Context, image_id: int) -> None:
        try:
            results = await asyncio.to_thread(suggest_face_candidates, image_id, 5)
        except FaceEngineUnavailable as error:
            await ctx.send(f"⚠️ {error}")
            return
        except Exception as error:
            await ctx.send(f"❌ 候補照合に失敗しました: `{type(error).__name__}: {error}`")
            return
        if not results:
            await ctx.send("⚠️ 照合できる顔または確定済み参照顔がありません。")
            return
        lines = [f"🧠 **画像ID {image_id} のローカル顔候補**"]
        for item in results:
            candidates = item.get("candidates", [])
            text = " / ".join(
                f"{candidate['person_name']} {float(candidate['confidence'])*100:.1f}%"
                for candidate in candidates
            ) or "候補なし"
            lines.append(f"顔ID **{item['face_id']}**: {text}")
        lines.append("※ OpenAI APIは使用していません。候補は必ず人間が確認してください。")
        await ctx.send("\n".join(lines)[:1900])

    @bot.command(name="face_info")
    @commands.is_owner()
    async def face_info_command(ctx: commands.Context, image_id: int) -> None:
        faces = await asyncio.to_thread(get_face_summary, image_id)
        if not faces:
            await ctx.send("⚠️ この画像には保存済みの顔情報がありません。")
            return
        lines = [f"👤 **画像ID {image_id} の顔情報**"]
        for face in faces:
            confirmed = face.get("confirmed_person_name") or "未確定"
            candidates = face.get("candidates", [])
            candidate_text = " / ".join(
                f"{item.get('person_name')} {float(item.get('confidence') or 0)*100:.1f}%"
                for item in candidates
            ) or "なし"
            lines.append(
                f"顔ID **{face['id']}** / 番号 {face.get('face_index')} / "
                f"確定: **{confirmed}** / 候補: {candidate_text}"
            )
        await ctx.send("\n".join(lines)[:1900])

    async def _run_face_scan_job(
        ctx: commands.Context,
        *,
        requested_limit: int | None,
        group_name: str,
    ) -> None:
        """顔一括処理を100枚ずつ進め、同じメッセージへ進捗を表示する。"""
        if face_scan_lock.locked():
            await ctx.send(
                "⚠️ 別の顔一括検出が実行中です。停止する場合は `!face_scan_stop` を使ってください。"
            )
            return

        group_name = group_name.strip()
        is_all = requested_limit is None
        target_limit = None if is_all else max(1, min(int(requested_limit), MAX_BATCH_SCAN))
        label = f" / {group_name}" if group_name else ""
        mode_text = "未スキャン画像をすべて" if is_all else f"最大{target_limit:,}枚"

        async with face_scan_lock:
            face_scan_stop_event.clear()
            progress_message = await ctx.send(
                f"🔍 顔一括検出を開始します: **{mode_text}{label}**\n"
                "100枚ずつ処理し、進捗をこのメッセージへ更新します。\n"
                "OpenAI APIは使用しません。"
            )

            total_targets = 0
            total_scanned = 0
            total_detected = 0
            total_auto_confirmed = 0
            total_failed = 0
            errors: list[str] = []

            while True:
                if face_scan_stop_event.is_set():
                    break
                remaining = None if target_limit is None else target_limit - total_targets
                if remaining is not None and remaining <= 0:
                    break

                chunk_size = 100 if remaining is None else min(100, remaining)
                result = await asyncio.to_thread(scan_faces_batch, chunk_size, group_name)
                chunk_targets = int(result.get("targets") or 0)
                if chunk_targets == 0:
                    break

                total_targets += chunk_targets
                total_scanned += int(result.get("scanned") or 0)
                total_detected += int(result.get("detected") or 0)
                total_auto_confirmed += int(result.get("auto_confirmed") or 0)
                total_failed += int(result.get("failed") or 0)
                for item in result.get("errors") or []:
                    if len(errors) < 5:
                        errors.append(str(item))

                target_text = "すべて" if target_limit is None else f"{target_limit:,}枚"
                try:
                    await progress_message.edit(
                        content=(
                            "🔍 **顔一括検出を実行中**\n"
                            f"指定: **{target_text}{label}**\n"
                            f"取得済み対象: **{total_targets:,}枚**\n"
                            f"処理成功: **{total_scanned:,}枚** / 失敗: **{total_failed:,}枚**\n"
                            f"検出した顔: **{total_detected:,}件**\n"
                            f"安全条件で参照登録: **{total_auto_confirmed:,}件**\n"
                            "停止: `!face_scan_stop`"
                        )
                    )
                except discord.HTTPException:
                    pass

                # Discord APIとRailwayの負荷をわずかに緩和する。
                await asyncio.sleep(0.25)

            stopped = face_scan_stop_event.is_set()
            title = "⏹️ **顔一括検出を停止しました**" if stopped else "✅ **顔一括検出完了**"
            lines = [
                title,
                f"対象: **{total_targets:,}枚**",
                f"処理成功: **{total_scanned:,}枚**",
                f"検出した顔: **{total_detected:,}件**",
                f"安全条件で参照登録: **{total_auto_confirmed:,}件**",
                f"失敗: **{total_failed:,}枚**",
            ]
            if is_all and not stopped and total_targets == 0:
                lines.append("未スキャン画像はありませんでした。")
            if errors:
                lines.append("\n**先頭のエラー**")
                lines.extend(f"・{item}" for item in errors)
            lines.append("\n続きは未スキャン画像から再開されます。OpenAI APIは使用していません。")
            await progress_message.edit(content="\n".join(lines)[:1900])
            face_scan_stop_event.clear()

    @bot.command(name="face_scan_batch")
    @commands.is_owner()
    async def face_scan_batch_command(
        ctx: commands.Context,
        limit: int = 100,
        *,
        group_name: str = "",
    ) -> None:
        """未スキャン画像を最大1000枚、100枚ずつローカル顔検出する。"""
        await _run_face_scan_job(
            ctx,
            requested_limit=max(1, min(int(limit), MAX_BATCH_SCAN)),
            group_name=group_name,
        )

    @bot.command(name="face_scan_all")
    @commands.is_owner()
    async def face_scan_all_command(
        ctx: commands.Context,
        *,
        group_name: str = "",
    ) -> None:
        """未スキャン画像がなくなるまで100枚ずつ処理する。"""
        await _run_face_scan_job(ctx, requested_limit=None, group_name=group_name)

    @bot.command(name="face_scan_stop")
    @commands.is_owner()
    async def face_scan_stop_command(ctx: commands.Context) -> None:
        """実行中の顔一括検出を、現在の100枚単位の処理後に停止する。"""
        if not face_scan_lock.locked():
            await ctx.send("ℹ️ 実行中の顔一括検出はありません。")
            return
        face_scan_stop_event.set()
        await ctx.send("⏹️ 停止を受け付けました。現在処理中のまとまりが終わり次第停止します。")

    @bot.command(name="face_crop")
    @commands.is_owner()
    async def face_crop_command(ctx: commands.Context, face_id: int) -> None:
        """顔IDの切り出し画像を一時表示する。"""
        try:
            data, filename = await asyncio.to_thread(get_face_crop_bytes, face_id)
        except Exception as error:
            await ctx.send(f"❌ 顔画像の作成に失敗しました: `{type(error).__name__}: {error}`")
            return
        await ctx.send(
            f"🖼️ 顔ID **{face_id}** の確認用切り出し",
            file=discord.File(io.BytesIO(data), filename=filename),
        )

    @bot.command(name="face_review")
    @commands.is_owner()
    async def face_review_command(ctx: commands.Context, limit: int = 1) -> None:
        """切り出し画像・候補選択・投稿者ボタン付きで顔を確認する。"""
        await send_face_review_batch(ctx, max(1, min(int(limit), 5)))

    @bot.command(name="face_review_fast")
    @commands.is_owner()
    async def face_review_fast_command(
        ctx: commands.Context,
        limit: int = 20,
        min_confidence: float = 95.0,
    ) -> None:
        """高信頼度の1位候補をプレビューして一括確定する。"""
        await send_fast_face_review(
            ctx,
            max(1, min(int(limit), 100)),
            max(90.0, min(float(min_confidence), 100.0)),
        )

    @bot.command(name="face_review_person", aliases=["face_review_same"])
    @commands.is_owner()
    async def face_review_person_command(
        ctx: commands.Context,
        person_name: str = "",
        limit: int = 50,
        min_confidence: float = 90.0,
    ) -> None:
        """指定人物が1位候補の顔をまとめてプレビューし、一括確定する。"""
        # Phase 8-3 is imported lazily so a partial/mixed deployment does not
        # crash the entire Bot during startup. The complete release includes
        # the matching implementation in photo_face_review_view.py.
        try:
            from photo_face_review_view import send_person_group_face_review
        except ImportError as error:
            await ctx.send(
                "❌ 人物別一括確認のファイル構成が一致していません。"
                " `photo_commands.py` と `photo_face_review_view.py` を"
                "同じ完成版から更新してください。"
            )
            raise RuntimeError(
                "send_person_group_face_review is missing from "
                "photo_face_review_view.py"
            ) from error

        await send_person_group_face_review(
            ctx,
            person_name,
            max(1, min(int(limit), 100)),
            max(80.0, min(float(min_confidence), 100.0)),
        )

    @bot.command(name="face_review_list")
    @commands.is_owner()
    async def face_review_list_command(ctx: commands.Context, limit: int = 5) -> None:
        reviews = await asyncio.to_thread(get_pending_face_reviews, max(1, min(int(limit), 10)))
        if not reviews:
            await ctx.send("✅ 顔の確認待ちはありません。")
            return
        lines = ["👤 **顔確認待ち**"]
        for item in reviews:
            candidates = str(item.get("candidates") or "").strip()
            lines.append(
                f"Review **{item['id']}** / 顔ID **{item['face_id']}** / 画像ID **{item['image_id']}**\n"
                f"{item.get('group_name','')} {item.get('member_name','')} / 候補: {candidates or 'なし'}"
            )
        lines.append("\nボタンレビュー: `!face_review`\n確認画像: `!face_crop 顔ID`\n手動確定: `!face_review_done 顔ID 人物名`")
        await ctx.send("\n\n".join(lines)[:1900])

    @bot.command(name="face_review_done")
    @commands.is_owner()
    async def face_review_done_command(ctx: commands.Context, face_id: int, *, person_name: str = "") -> None:
        person_name = person_name.strip()
        if not person_name:
            await ctx.send("使い方: `!face_review_done 顔ID 人物名`")
            return
        person = await asyncio.to_thread(get_person_by_name, person_name)
        if not person:
            await ctx.send("⚠️ 人物マスターにその名前がありません。")
            return
        await asyncio.to_thread(
            complete_face_review,
            face_id,
            int(person["id"]),
            str(ctx.author.id),
            "Discord face review",
        )
        await ctx.send(f"✅ 顔ID **{face_id}** を **{person_name}** としてレビュー完了しました。")

    @bot.command(name="face_confirm")
    @commands.is_owner()
    async def face_confirm_command(ctx: commands.Context, face_id: int, *, person_name: str = "") -> None:
        person_name = person_name.strip()
        if not person_name:
            await ctx.send("使い方: `!face_confirm 顔ID 人物名`")
            return
        person = await asyncio.to_thread(get_person_by_name, person_name)
        if not person:
            await ctx.send("⚠️ 人物マスターにその名前がありません。")
            return
        await asyncio.to_thread(
            confirm_face_person,
            face_id,
            int(person["id"]),
            confirmed_by=str(ctx.author.id),
            confirmation_status="manually_confirmed",
        )
        await ctx.send(f"✅ 顔ID **{face_id}** を **{person_name}** として確定しました。")

    @bot.command(name="face_learning_status")
    @commands.is_owner()
    async def face_learning_status_command(ctx: commands.Context, *, person_name: str = "") -> None:
        """確定済み顔がローカル学習用参照として何件使えるか表示する。"""
        person_name = person_name.strip()
        params: tuple[Any, ...] = ()
        where = ""
        if person_name:
            where = " AND photo_people.person_name = ?"
            params = (person_name,)

        rows = await asyncio.to_thread(
            _rows,
            f"""
            SELECT photo_people.person_name, photo_people.group_name,
                   COUNT(photo_faces.id) AS reference_count,
                   MAX(photo_faces.confirmed_at) AS last_learned_at
            FROM photo_people
            JOIN photo_faces ON photo_faces.confirmed_person_id = photo_people.id
            WHERE photo_faces.face_embedding <> ''
              AND photo_faces.confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
              {where}
            GROUP BY photo_people.id, photo_people.person_name, photo_people.group_name
            ORDER BY reference_count DESC, photo_people.person_name ASC
            LIMIT 25
            """,
            params,
        )
        total = await asyncio.to_thread(
            _row,
            """
            SELECT COUNT(*) AS reference_total, COUNT(DISTINCT confirmed_person_id) AS people
            FROM photo_faces
            WHERE confirmed_person_id IS NOT NULL
              AND face_embedding <> ''
              AND confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
            """,
        )
        pending = await asyncio.to_thread(
            _row,
            "SELECT COUNT(*) AS count FROM photo_face_reviews WHERE status = 'pending'",
        )
        lines = [
            "🧠 **顔学習状況**",
            f"学習用の確定顔: **{int((total or {}).get('reference_total') or 0):,}件**",
            f"学習済み人物: **{int((total or {}).get('people') or 0):,}人**",
            f"再判定待ち: **{int((pending or {}).get('count') or 0):,}件**",
            "",
            "**人物別の学習用顔数**",
        ]
        if rows:
            for item in rows:
                group = str(item.get("group_name") or "")
                prefix = f"[{group}] " if group else ""
                lines.append(f"・{prefix}{item['person_name']}: **{int(item['reference_count']):,}件**")
        else:
            lines.append("該当する学習データはありません。")
        lines.append("\n確定した顔は自動的に次回のローカル候補計算へ使われます。OpenAI APIは使用しません。")
        await ctx.send("\n".join(lines)[:1900])

    @bot.command(name="face_cluster", aliases=["face_cluster_review"])
    @commands.is_owner()
    async def face_cluster_command(
        ctx: commands.Context,
        limit: int = 200,
        similarity_percent: float = 90.0,
    ) -> None:
        """確認待ちの顔をローカル特徴量でクラスタリングし、まとめて確認する。"""
        await send_face_cluster_review(ctx, limit, similarity_percent)

    @bot.command(name="face_relearn")
    @commands.is_owner()
    async def face_relearn_command(ctx: commands.Context, limit: int = 20) -> None:
        """最新の確定顔を使い、確認待ちの候補をローカルで再計算する。"""
        limit = max(1, min(int(limit), 100))
        if face_relearn_lock.locked():
            await ctx.send("⚠️ 顔候補の再学習処理は現在実行中です。")
            return

        async with face_relearn_lock:
            targets = await asyncio.to_thread(
                _rows,
                """
                SELECT DISTINCT photo_faces.image_id
                FROM photo_face_reviews
                JOIN photo_faces ON photo_faces.id = photo_face_reviews.face_id
                WHERE photo_face_reviews.status = 'pending'
                  AND photo_faces.face_embedding <> ''
                ORDER BY photo_face_reviews.id ASC
                LIMIT ?
                """,
                (limit,),
            )
            if not targets:
                await ctx.send("✅ 再学習できる顔確認待ちはありません。")
                return

            message = await ctx.send(
                f"🧠 最新の確定顔を使って候補を再学習しています… **0/{len(targets)}画像**\n"
                "OpenAI APIは使用しません。"
            )
            completed = 0
            failed = 0
            updated_faces = 0
            for index, item in enumerate(targets, 1):
                try:
                    result = await asyncio.to_thread(
                        suggest_face_candidates, int(item["image_id"]), 5
                    )
                    updated_faces += len(result)
                    completed += 1
                except Exception as error:
                    failed += 1
                    print(
                        f"顔再学習失敗 image_id={item['image_id']}: "
                        f"{type(error).__name__}: {error}"
                    )
                if index == len(targets) or index % 10 == 0:
                    try:
                        await message.edit(
                            content=(
                                f"🧠 最新の確定顔を使って候補を再学習しています… "
                                f"**{index}/{len(targets)}画像**\nOpenAI APIは使用しません。"
                            )
                        )
                    except discord.HTTPException:
                        pass

            await message.edit(
                content=(
                    "✅ **顔候補の再学習が完了しました**\n"
                    f"処理画像: **{completed:,}件**\n"
                    f"候補を再計算した顔: **{updated_faces:,}件**\n"
                    f"失敗: **{failed:,}件**\n"
                    "手動確定・一括確定した顔が、今後の候補計算に反映されます。"
                )
            )

    @bot.command(name="ai_cost")
    @commands.is_owner()
    async def ai_cost_command(ctx: commands.Context, days: int | None = 30) -> None:
        """AI使用量と推定料金を表示する。0で全期間。"""

        period = None if days is None or int(days) <= 0 else min(int(days), 3650)
        summary = await asyncio.to_thread(get_ai_cost_summary, period)
        total = summary.get("total", {})
        title = "全期間" if period is None else f"過去{period}日"

        lines = [
            f"💰 **AI使用量・推定料金（{title}）**",
            f"API呼び出し: **{int(total.get('api_calls') or 0)}回**",
            f"重複画像の再利用: **{int(total.get('reused') or 0)}回**",
            f"入力トークン: **{int(total.get('input_tokens') or 0):,}**",
            f"キャッシュ入力: **{int(total.get('cached_input_tokens') or 0):,}**",
            f"出力トークン: **{int(total.get('output_tokens') or 0):,}**",
            f"合計トークン: **{int(total.get('total_tokens') or 0):,}**",
            f"推定料金: **${float(total.get('estimated_cost_usd') or 0):.6f} USD**",
        ]

        models = summary.get("models", [])
        if models:
            lines.append("\n**モデル別**")
            for item in models[:8]:
                lines.append(
                    f"・{item.get('model_name', '不明')}: "
                    f"API {int(item.get('api_calls') or 0)}回 / "
                    f"再利用 {int(item.get('reused') or 0)}回 / "
                    f"${float(item.get('estimated_cost_usd') or 0):.6f}"
                )

        lines.append("\n※ OpenAIのusage値と設定単価から計算した推定額です。")
        await ctx.send("\n".join(lines))

    @bot.command(name="ai_retry")
    @commands.is_owner()
    async def ai_retry_command(ctx: commands.Context, count: int = 10) -> None:
        """failed・review状態の画像を、指定した件数だけ再解析する。"""

        count = max(1, min(int(count), 1000))
        targets = await asyncio.to_thread(
            _rows,
            "SELECT id FROM photo_images WHERE analysis_status IN ('failed', 'review') ORDER BY id LIMIT ?",
            (count,),
        )
        if not targets:
            await ctx.send("✅ 再解析対象はありません。")
            return

        await ctx.send(
            f"🤖 **{len(targets)}件**の再解析を開始します。"
            f"（指定件数: {count}件）"
        )

        completed = 0
        review = 0
        failed_count = 0

        for item in targets:
            target_id = int(item["id"])
            await asyncio.to_thread(reset_image_analysis_status, target_id)
            result = await analyze_photo_image(target_id)
            status = str(result.get("status") or "failed")

            if status == "completed":
                completed += 1
            elif status == "review":
                review += 1
            else:
                failed_count += 1

        success = completed + review
        await ctx.send(
            "✅ 再解析終了\n"
            f"成功: **{success}件** / 対象 **{len(targets)}件**\n"
            f"（completed: {completed}件 / review: {review}件 / "
            f"failed: {failed_count}件）"
        )

    @bot.command(name="ai_retry_id")
    @commands.is_owner()
    async def ai_retry_id_command(ctx: commands.Context, image_id: int) -> None:
        """指定した画像IDを1件だけ再解析する。"""

        image = await asyncio.to_thread(get_photo_image, image_id)
        if not image:
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return

        await asyncio.to_thread(reset_image_analysis_status, image_id)
        await ctx.send(f"🤖 画像ID **{image_id}** を再解析します。")
        result = await analyze_photo_image(image_id)
        status = str(result.get("status") or "failed")
        await ctx.send(f"✅ 画像ID **{image_id}** の解析結果: **{status}**")

    @bot.command(name="photo_redownload")
    @commands.is_owner()
    async def photo_redownload_command(ctx: commands.Context, image_id: int | None = None, limit: int = 10) -> None:
        if image_id is None:
            limit = max(1, min(int(limit), 50))
            targets = await asyncio.to_thread(
                _rows,
                "SELECT id FROM photo_images WHERE download_status = 'failed' ORDER BY updated_at ASC, id ASC LIMIT ?",
                (limit,),
            )
        else:
            targets = [{"id": image_id}]

        if not targets:
            await ctx.send("✅ 再ダウンロード対象はありません。")
            return

        await ctx.send(f"🔄 {len(targets)}件の再ダウンロードを開始します。")
        succeeded = 0
        reparsed = 0
        invalid_urls = 0
        permanent_failures = 0
        failures: list[str] = []
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item in targets:
                target_id = int(item["id"])
                result = await _redownload_one(session, target_id)
                if result.get("success"):
                    succeeded += 1
                    if result.get("recovered_by") == "article_reparse":
                        reparsed += 1
                    continue

                terminal_status = str(result.get("terminal_status") or "")
                if terminal_status == "invalid_url":
                    invalid_urls += 1
                elif terminal_status == "permanent_failed":
                    permanent_failures += 1

                error_text = str(
                    result.get("refresh_error")
                    or result.get("error")
                    or "不明なエラー"
                ).replace("\n", " ")
                failures.append(f"ID {target_id}: {error_text[:180]}")
                print(
                    "写真再ダウンロード失敗:",
                    f"image_id={target_id}",
                    error_text,
                )

        message = (
            f"✅ 再ダウンロード終了: 成功 **{succeeded}件** "
            f"/ 対象 **{len(targets)}件**"
        )
        if reparsed:
            message += f"\n🔎 元記事の再解析で復旧: **{reparsed}件**"
        if invalid_urls:
            message += f"\n🚫 不正URLとして除外: **{invalid_urls}件**"
        if permanent_failures:
            message += f"\n⛔ 復旧不能として除外: **{permanent_failures}件**"
        if failures:
            detail_lines = failures[:8]
            if len(failures) > len(detail_lines):
                detail_lines.append(f"ほか {len(failures) - len(detail_lines)}件")
            message += "\n\n❌ **失敗理由**\n" + "\n".join(detail_lines)

        await ctx.send(message[:1900])

    @bot.command(name="photo_stats")
    
    async def photo_stats_command(ctx: commands.Context) -> None:
        counts, storage = await asyncio.gather(
            asyncio.to_thread(get_photo_db_counts),
            asyncio.to_thread(get_photo_storage_stats),
        )
        analysis = await asyncio.to_thread(
            _row,
            """
            SELECT
                SUM(CASE WHEN analysis_status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN analysis_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN analysis_status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN analysis_status = 'review' THEN 1 ELSE 0 END) AS review
            FROM photo_images
            """,
        ) or {}
        await ctx.send(
            "📊 **写真DB統計**\n"
            f"ブログ: **{counts.get('blogs', 0)}件**\n"
            f"画像: **{counts.get('images', 0)}件**\n"
            f"人物: **{counts.get('people', 0)}人**\n"
            f"AI完了: **{int(analysis.get('completed') or 0)}件**\n"
            f"AI待ち: **{int(analysis.get('pending') or 0)}件**\n"
            f"AI確認待ち: **{int(analysis.get('review') or 0)}件**\n"
            f"AI失敗: **{int(analysis.get('failed') or 0)}件**\n"
            f"保存容量: **{_format_bytes(storage.get('total_size', 0))}**"
        )

    @bot.command(name="photo_recent")
    
    async def photo_recent_command(ctx: commands.Context, limit: int = 10) -> None:
        limit = max(1, min(int(limit), 30))
        recent = await asyncio.to_thread(
            _rows,
            """
            SELECT photo_images.id, photo_images.image_index,
                   photo_blogs.group_name, photo_blogs.member_name,
                   photo_blogs.title, photo_blogs.published_at
            FROM photo_images
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            ORDER BY photo_images.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        if not recent:
            await ctx.send("📷 保存画像はまだありません。")
            return
        lines = [
            f"`ID {item['id']}` {item['group_name']} / {item['member_name']} / {item['published_at']}"
            for item in recent
        ]
        await ctx.send("🕒 **最近登録された画像**\n" + "\n".join(lines))

    @bot.command(name="favorite_add")
    
    async def favorite_add_command(ctx: commands.Context, image_id: int) -> None:
        if not await asyncio.to_thread(get_photo_image, image_id):
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return
        await asyncio.to_thread(
            _execute,
            "INSERT OR IGNORE INTO photo_favorites (image_id, discord_user_id, created_at) VALUES (?, ?, ?)",
            (image_id, str(ctx.author.id), _now()),
        )
        await ctx.send(f"⭐ 画像ID **{image_id}** をお気に入りに追加しました。")

    @bot.command(name="favorite_remove")
    
    async def favorite_remove_command(ctx: commands.Context, image_id: int) -> None:
        deleted = await asyncio.to_thread(
            _execute,
            "DELETE FROM photo_favorites WHERE image_id = ? AND discord_user_id = ?",
            (image_id, str(ctx.author.id)),
        )
        await ctx.send("⭐ お気に入りから削除しました。" if deleted else "⚠️ お気に入りに登録されていません。")

    @bot.command(name="favorite_list")
    
    async def favorite_list_command(ctx: commands.Context, limit: int = 20) -> None:
        limit = max(1, min(int(limit), 50))
        favorites = await asyncio.to_thread(
            _rows,
            """
            SELECT photo_images.id, photo_blogs.group_name, photo_blogs.member_name,
                   photo_blogs.published_at
            FROM photo_favorites
            JOIN photo_images ON photo_images.id = photo_favorites.image_id
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            WHERE photo_favorites.discord_user_id = ?
            ORDER BY photo_favorites.id DESC
            LIMIT ?
            """,
            (str(ctx.author.id), limit),
        )
        if not favorites:
            await ctx.send("⭐ お気に入りはまだありません。")
            return
        lines = [f"`ID {x['id']}` {x['group_name']} / {x['member_name']} / {x['published_at']}" for x in favorites]
        await ctx.send("⭐ **お気に入り一覧**\n" + "\n".join(lines))

    @bot.command(name="photo_reset")
    @commands.is_owner()
    async def photo_reset_command(ctx: commands.Context, confirmation: str = "") -> None:
        """写真検索用DBだけを初期化する。誤操作防止のため確認語が必要。"""

        if confirmation != "confirm":
            await ctx.send(
                "⚠️ **写真検索データベースを完全に初期化します。**\n"
                "ブログ通知用の `archive.db` と `blogs.db` は変更しません。\n"
                "実行する場合は `!photo_reset confirm` と入力してください。"
            )
            return

        await ctx.send("⏳ 写真検索データベースを初期化しています...")

        def reset_database() -> None:
            # SQLiteの本体と一時ファイルだけを削除する。
            # 画像ファイル、archive.db、blogs.dbには触れない。
            for path in (PHOTO_DB_PATH, f"{PHOTO_DB_PATH}-wal", f"{PHOTO_DB_PATH}-shm"):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except FileNotFoundError:
                    pass

            init_photo_db()

        try:
            await asyncio.to_thread(reset_database)
        except Exception as exc:
            await ctx.send(
                "❌ 写真検索データベースの初期化に失敗しました。\n"
                f"`{type(exc).__name__}: {exc}`"
            )
            return

        counts = await asyncio.to_thread(get_photo_db_counts)
        await ctx.send(
            "✅ **写真検索データベースを初期化しました。**\n"
            f"対象: `{PHOTO_DB_PATH}`\n"
            f"ブログ: {counts['blogs']}件 / 画像: {counts['images']}件 / "
            f"確認待ち: {counts['pending_reviews']}件\n\n"
            "次に `!photo_archive_run` を実行してください。"
        )

    @bot.command(name="photo_person_show")
    async def photo_person_show_command(ctx: commands.Context, image_id: int) -> None:
        """画像に登録されている人物候補・確定人物を表示する。"""
        image = await asyncio.to_thread(get_photo_image, image_id)
        if not image:
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return

        people = await asyncio.to_thread(get_image_people, image_id)
        confirmed = [
            str(item.get("person_name", "")).strip()
            for item in people
            if item.get("relation_status") == "confirmed"
            and str(item.get("person_name", "")).strip()
        ]
        candidates = [
            f"{str(item.get('person_name', '')).strip()} ({float(item.get('confidence') or 0):.0%})"
            for item in people
            if item.get("relation_status") == "candidate"
            and str(item.get("person_name", "")).strip()
        ]

        await ctx.send(
            f"👤 **画像ID {image_id} の人物情報**\n"
            f"確定: **{'、'.join(confirmed) if confirmed else '未確定'}**\n"
            f"候補: **{'、'.join(candidates) if candidates else '候補なし'}**"
        )

    @bot.command(name="review_next")
    @commands.is_owner()
    async def review_next_command(ctx: commands.Context) -> None:
        """最も古い人物確認待ちを1件表示する。"""
        await send_next_person_review(ctx)

    @bot.command(name="review_list")
    @commands.is_owner()
    async def review_list_command(ctx: commands.Context, limit: int = 5) -> None:
        """人物確認待ちを複数件表示する（最大10件）。"""
        await send_person_review_batch(ctx, limit=max(1, min(int(limit), 10)))

    @bot.command(name="review_list_ng")
    @commands.is_owner()
    async def review_list_ng_command(ctx: commands.Context, limit: int = 5) -> None:
        """乃木坂46の人物確認待ちだけを複数件表示する（最大10件）。"""
        await send_person_review_batch(
            ctx,
            limit=max(1, min(int(limit), 10)),
            group_name="乃木坂46",
        )

    @bot.command(name="review_list_skr")
    @commands.is_owner()
    async def review_list_skr_command(ctx: commands.Context, limit: int = 5) -> None:
        """櫻坂46の人物確認待ちだけを複数件表示する（最大10件）。"""
        await send_person_review_batch(
            ctx,
            limit=max(1, min(int(limit), 10)),
            group_name="櫻坂46",
        )

    @bot.command(name="review_list_hnt")
    @commands.is_owner()
    async def review_list_hnt_command(ctx: commands.Context, limit: int = 5) -> None:
        """日向坂46の人物確認待ちだけを複数件表示する（最大10件）。"""
        await send_person_review_batch(
            ctx,
            limit=max(1, min(int(limit), 10)),
            group_name="日向坂46",
        )

    @bot.command(name="review_skipped")
    @commands.is_owner()
    async def review_skipped_command(ctx: commands.Context, limit: int = 5) -> None:
        """過去にスキップした人物レビューを再表示する（最大10件）。"""
        await send_skipped_person_review_batch(
            ctx,
            limit=max(1, min(int(limit), 10)),
        )

    @bot.command(name="review_stats")
    @commands.is_owner()
    async def review_stats_command(ctx: commands.Context) -> None:
        stats = await asyncio.to_thread(
            _row,
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
            FROM photo_review_queue
            """,
        ) or {}
        await ctx.send(
            "🧾 **レビュー統計**\n"
            f"合計: **{int(stats.get('total') or 0)}件**\n"
            f"確認待ち: **{int(stats.get('pending') or 0)}件**\n"
            f"完了: **{int(stats.get('completed') or 0)}件**\n"
            f"スキップ: **{int(stats.get('skipped') or 0)}件**"
        )

    @bot.command(name="photo_edit")
    @commands.is_owner()
    async def photo_edit_command(ctx: commands.Context, image_id: int) -> None:
        """指定画像を人物確認待ちへ戻し、レビュー画面を表示する。"""
        image = await asyncio.to_thread(get_photo_image, image_id)
        if not image:
            await ctx.send("⚠️ 画像IDが見つかりません。")
            return

        people = await asyncio.to_thread(get_image_people, image_id)
        candidate_names = [
            str(item.get("person_name", "")).strip()
            for item in people
            if str(item.get("person_name", "")).strip()
        ]
        await asyncio.to_thread(
            add_review_item,
            image_id,
            "person_identity",
            "この写真に写っている人物を確認してください。",
            "、".join(dict.fromkeys(candidate_names)),
        )
        review = await asyncio.to_thread(
            _row,
            """
            SELECT
                photo_review_queue.id AS review_id,
                photo_review_queue.image_id,
                photo_review_queue.question,
                photo_review_queue.candidates,
                photo_images.image_url,
                photo_images.local_path,
                photo_images.image_index,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                COALESCE(photo_ai_analysis.person_name, '') AS ai_person_name,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people
                    WHERE image_id = photo_images.id
                      AND relation_status = 'candidate'
                ), '') AS candidate_people,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people
                    WHERE image_id = photo_images.id
                      AND relation_status = 'confirmed'
                ), '') AS confirmed_people
            FROM photo_review_queue
            JOIN photo_images ON photo_images.id = photo_review_queue.image_id
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_ai_analysis ON photo_ai_analysis.image_id = photo_images.id
            WHERE photo_review_queue.image_id = ?
            """,
            (image_id,),
        )
        if not review:
            await ctx.send("❌ レビュー画面の作成に失敗しました。")
            return
        await send_person_review(ctx, review)

    @bot.command(name="review_done")
    @commands.is_owner()
    async def review_done_command(ctx: commands.Context, review_id: int, *, selected_value: str = "") -> None:
        review = await asyncio.to_thread(
            _row,
            "SELECT * FROM photo_review_queue WHERE id = ? AND status = 'pending'",
            (review_id,),
        )
        if not review:
            await ctx.send("⚠️ 指定された確認待ちは見つかりません。")
            return

        selected_value = selected_value.strip()
        if not selected_value:
            await ctx.send(
                "⚠️ 人物名を入力してください。\n"
                f"例: `!review_done {review_id} 井上和`\n"
                f"複数人: `!review_done {review_id} 菅原咲月,井上和`\n"
                f"人物なし: `!review_done {review_id} なし`"
            )
            return

        names = [] if selected_value in {"なし", "人物なし", "不明"} else [
            name.strip()
            for name in selected_value.replace("、", ",").split(",")
            if name.strip()
        ]
        await asyncio.to_thread(
            set_confirmed_image_people,
            int(review["image_id"]),
            names,
            confirmed_by=str(ctx.author.id),
            note="Discord review command",
        )
        await asyncio.to_thread(
            complete_review_item,
            int(review["image_id"]),
            "人物なし" if not names else "、".join(names),
            reviewed_by=str(ctx.author.id),
            review_note="Discord command",
        )
        display = "人物なし" if not names else "、".join(names)
        await ctx.send(
            f"✅ Review **{review_id}** を完了し、画像ID **{review['image_id']}** を "
            f"**{display}** として確定しました。"
        )

    @bot.command(name="review_skip")
    @commands.is_owner()
    async def review_skip_command(ctx: commands.Context, review_id: int, *, note: str = "") -> None:
        updated = await asyncio.to_thread(
            _execute,
            """
            UPDATE photo_review_queue
            SET status = 'skipped', reviewed_by = ?, review_note = ?,
                reviewed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (str(ctx.author.id), note.strip() or "Discord skip", _now(), _now(), review_id),
        )
        if updated:
            await ctx.send(f"⏭️ Review **{review_id}** をスキップしました。")
        else:
            await ctx.send("⚠️ 指定された確認待ちは見つかりません。")
