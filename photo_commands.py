import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from photo_ai_analyzer import analyze_photo_image
from photo_database import (
    add_review_item,
    complete_review_item,
    get_ai_cost_summary,
    get_all_people,
    get_image_people,
    get_person_by_name,
    confirm_face_person,
    set_confirmed_image_people,
    get_connection,
    get_photo_db_counts,
    get_photo_image,
    get_photo_storage_stats,
    init_photo_db,
    PHOTO_DB_PATH,
    reset_image_analysis_status,
    reset_image_download_status,
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
from local_face_recognition import (
    FaceEngineUnavailable,
    detect_faces_for_image,
    get_face_engine_status,
    get_face_summary,
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
            photo_blogs.published_at
        FROM photo_images
        JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
        WHERE photo_images.id = ?
        """,
        (image_id,),
    )


async def _redownload_one(session: aiohttp.ClientSession, image_id: int) -> dict[str, Any]:
    record = await asyncio.to_thread(_get_redownload_record, image_id)
    if not record:
        return {"success": False, "error": "画像IDが見つかりません。"}

    await asyncio.to_thread(reset_image_download_status, image_id)

    return await download_photo_image(
        session,
        image_id=int(record["id"]),
        blog_id=int(record["blog_id"]),
        image_url=str(record["image_url"]),
        image_index=int(record["image_index"]),
        group_name=str(record["group_name"]),
        member_name=str(record["member_name"]),
        published_at=str(record["published_at"]),
    )


def register_photo_commands(bot: commands.Bot) -> None:

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
        await ctx.send(
            "✅ **ローカル顔認識: 利用可能**\n"
            f"モデル: `{status.get('model_name')}`\n"
            f"OpenCV: `{status.get('opencv_version')}`\n"
            f"NumPy: `{status.get('numpy_version')}`\n"
            "OpenAI APIは使用しません。処理はコマンド実行時だけです。"
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
                "SELECT id FROM photo_images WHERE download_status = 'failed' ORDER BY id LIMIT ?",
                (limit,),
            )
        else:
            targets = [{"id": image_id}]

        if not targets:
            await ctx.send("✅ 再ダウンロード対象はありません。")
            return

        await ctx.send(f"🔄 {len(targets)}件の再ダウンロードを開始します。")
        succeeded = 0
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for item in targets:
                result = await _redownload_one(session, int(item["id"]))
                if result.get("success"):
                    succeeded += 1
        await ctx.send(f"✅ 再ダウンロード終了: 成功 **{succeeded}件** / 対象 **{len(targets)}件**")

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
