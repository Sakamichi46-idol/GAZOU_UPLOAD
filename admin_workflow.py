"""ZIP44 管理者向け選択式ワークフロー。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands
from embed_safety import safe_add_field, safe_embed, safe_text

from photo_ai_analyzer import analyze_photo_image
from local_face_recognition import detect_faces_for_image
from photo_database import (
    get_photo_blog_for_admin_edit,
    update_photo_blog_info_for_admin,
    get_blog_authors_for_admin,
    get_blogs_for_admin,
    get_blogs_for_admin_filtered,
    get_blog_years_for_admin,
    get_admin_blog_browser_state,
    save_admin_blog_browser_state,
    get_blog_image_ids,
    get_blog_progress_for_admin,
    get_blog_images_for_review_admin,
    get_error_blogs_for_admin,
    get_latest_blogs_for_admin,
    get_unprocessed_blogs_for_admin,
    get_blog_workflow_stats_for_admin,
    reset_blog_processing_errors_for_admin,
    set_confirmed_image_people,
)
from photo_review_view import send_blog_person_review_batch, send_person_review_batch, send_person_review
from sakamichi_members import SAKAMICHI_MEMBERS, member_surname_kana_sort_key

GROUPS = ("乃木坂46", "櫻坂46", "日向坂46")
PROGRESS_SEGMENTS = 10
LOGGER = logging.getLogger(__name__)


async def _admin(interaction: discord.Interaction) -> bool:
    from control_panel import is_panel_admin

    bot = interaction.client
    return isinstance(bot, commands.Bot) and await is_panel_admin(bot, interaction.user)


async def _deny(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        await interaction.followup.send("⚠️ 管理者専用です。", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ 管理者専用です。", ephemeral=True)


class AdminWorkflowView(discord.ui.View):
    """時間制限なしで使える管理者ワークフロー共通View。

    これらは管理者パネルから生成されるため、Botが稼働している間は
    時間経過だけでボタンや選択メニューが無効にならない。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _admin(interaction):
            return True
        await _deny(interaction)
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        LOGGER.exception("管理者ワークフローの操作でエラーが発生しました", exc_info=error)
        from admin_quality import record_admin_error
        error_id = record_admin_error(error, area="admin_workflow", item_name=getattr(item, "custom_id", "") or getattr(item, "label", ""), user_id=interaction.user.id)
        text = f"⚠️ 管理画面の操作中にエラーが発生しました。\nエラーID: `{error_id}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


def _progress_bar(percent: int, *, has_error: bool = False) -> str:
    percent = max(0, min(int(percent), 100))
    filled = round(percent * PROGRESS_SEGMENTS / 100)
    empty = PROGRESS_SEGMENTS - filled
    marker = "🟥" if has_error else "🟩"
    return marker * filled + "⬜" * empty


def _article_summary(blog: dict[str, Any]) -> str:
    total = int(blog.get("progress_total") or blog.get("image_count") or 0)
    completed = int(blog.get("progress_completed") or 0)
    pending = max(0, total - completed)
    percent = int(blog.get("progress_percent") or 0)
    errors = int(blog.get("error_count") or 0)
    bar = _progress_bar(percent, has_error=errors > 0)
    lines = [
        f"{bar} **{percent}%**",
        f"人物確認 **{completed}/{total}**（残り{pending}）",
    ]
    if errors:
        download_errors = int(blog.get("download_error_count") or 0)
        analysis_errors = int(blog.get("analysis_error_count") or 0)
        face_errors = int(blog.get("face_error_count") or 0)
        parts = []
        if download_errors:
            parts.append(f"画像取得 {download_errors}")
        if analysis_errors:
            parts.append(f"AI解析 {analysis_errors}")
        if face_errors:
            parts.append(f"顔認証 {face_errors}")
        lines.append(f"⚠️ 処理エラー **{errors}枚**（{' / '.join(parts) or '詳細不明'}）")
    terminal = int(blog.get("terminal_excluded_count") or 0)
    if terminal:
        lines.append(f"🚫 除外済み **{terminal}枚**（不正URL・復旧不能）")
    stale = int(blog.get("stale_error_count") or 0)
    if stale:
        lines.append(f"🧹 古いエラー記録 **{stale}枚**（再試行準備で整理可能）")
    return "\n".join(lines)


def _article_embed(blog: dict[str, Any], *, title_prefix: str = "📖 ブログ記事") -> discord.Embed:
    title = str(blog.get("title") or "無題")
    embed = safe_embed(
        title=f"{title_prefix}: {title}",
        description=_article_summary(blog),
        color=discord.Color.red() if int(blog.get("error_count") or 0) else discord.Color.green(),
        context=f"admin_workflow.article.blog_id={blog.get('id')}",
    )
    safe_add_field(embed, name="グループ", value=str(blog.get("group_name") or "不明"), inline=True, context=f"article.{blog.get('id')}.group")
    safe_add_field(embed, name="投稿者", value=str(blog.get("member_name") or "不明"), inline=True, context=f"article.{blog.get('id')}.member")
    safe_add_field(embed, name="投稿日", value=str(blog.get("published_at") or "不明"), inline=False, context=f"article.{blog.get('id')}.published_at")
    if blog.get("last_reviewed_at"):
        safe_add_field(embed, name="最終確認日時", value=str(blog["last_reviewed_at"]), inline=False, context=f"article.{blog.get('id')}.last_reviewed_at")
    if blog.get("blog_url"):
        safe_add_field(embed, name="ブログ", value=f"[元記事を開く]({blog['blog_url']})", inline=False, context=f"article.{blog.get('id')}.url")
    return embed


class ImageIdModal(discord.ui.Modal):
    def __init__(self, command_name: str, title: str):
        super().__init__(title=title, timeout=300)
        self.command_name = command_name
        self.image_id = discord.ui.TextInput(label="写真ID", placeholder="例: 125 / ID 125", max_length=30)
        self.add_item(self.image_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from control_panel import invoke_existing_command, normalize_image_id_argument

        await invoke_existing_command(
            interaction,
            self.command_name,
            normalize_image_id_argument(str(self.image_id.value)),
            admin_required=True,
        )


class CategoryAdminView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="写真管理", emoji="📷", style=discord.ButtonStyle.primary)
    async def photo(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("📷 写真管理", view=PhotoAdminView(), ephemeral=True)

    @discord.ui.button(label="人物・顔チェック", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("✅ 人物・顔チェック", view=ReviewAdminView(), ephemeral=True)

    @discord.ui.button(label="ブログ別の解析・確認", emoji="📖", style=discord.ButtonStyle.success)
    async def blog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "📖 **ブログ別の解析・確認**\n記事の探し方を選択してください。",
            view=BlogDashboardView(),
            ephemeral=True,
        )

    @discord.ui.button(label="タグ管理", emoji="🏷️", style=discord.ButtonStyle.secondary)
    async def tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("🏷️ タグ管理", view=TagAdminView(), ephemeral=True)

    @discord.ui.button(label="状態・修復", emoji="🛠️", style=discord.ButtonStyle.secondary)
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("🛠️ 状態・修復", view=StatusAdminView(), ephemeral=True)


class TagAdminView(AdminWorkflowView):
    @discord.ui.button(label="タグ検索", emoji="🔎", style=discord.ButtonStyle.primary)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "photo_tags", admin_required=True)

    @discord.ui.button(label="タグマスター", emoji="🏷️", style=discord.ButtonStyle.success)
    async def master(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "tag_master", admin_required=True)

    @discord.ui.button(label="全タグ出力", emoji="📄", style=discord.ButtonStyle.secondary)
    async def export(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "export_all_tags", admin_required=True)


class PhotoAdminView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="写真IDを表示", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def show(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImageIdModal("photo_id", "写真IDを表示"))

    @discord.ui.button(label="人物を設定", emoji="👤", style=discord.ButtonStyle.success)
    async def person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import CommandArgumentsModal

        await interaction.response.send_modal(CommandArgumentsModal(
            title="人物を設定", command_name="photo_person_set", label="写真IDと人物名",
            placeholder="125 井上和（複数はカンマ区切り）", admin_required=True,
        ))

    @discord.ui.button(label="タグを追加", emoji="🏷️", style=discord.ButtonStyle.success)
    async def tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import CommandArgumentsModal

        await interaction.response.send_modal(CommandArgumentsModal(
            title="手動タグ追加", command_name="tag_add", label="写真IDとタグ",
            placeholder="125 制服", admin_required=True,
        ))

    @discord.ui.button(label="AI再解析", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def analyze(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImageIdModal("ai_retry_id", "写真1枚をAI再解析"))

    @discord.ui.button(label="顔を検出・照合", emoji="🙂", style=discord.ButtonStyle.secondary)
    async def face(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImageIdModal("face_scan", "写真1枚を顔認証"))

    @discord.ui.button(label="全タグ一覧を出力", emoji="📤", style=discord.ButtonStyle.secondary)
    async def export_tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command

        await invoke_existing_command(interaction, "export_all_tags", admin_required=True)


class ReviewAdminView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="写真の人物を確認", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await send_person_review_batch(
            interaction,
            limit=1,
            queue_status="pending",
            continuous=True,
        )
        if count:
            await interaction.followup.send(
                "🖼️ 写真ごとの人物確認を開始しました。確定・スキップ後は自動で次へ進みます。",
                ephemeral=True,
            )

    @discord.ui.button(label="顔ごとに人物を確認", emoji="🙂", style=discord.ButtonStyle.primary)
    async def face(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command

        await invoke_existing_command(interaction, "face_review", "1", admin_required=True)

    @discord.ui.button(label="ブログ別に人物を確認", emoji="📖", style=discord.ButtonStyle.success)
    async def blog_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="📖 **ブログ別の人物確認**\n記事の探し方を選択してください。",
            view=BlogDashboardView(),
        )

    @discord.ui.button(label="ブログ別の確定を見直す", emoji="📖", style=discord.ButtonStyle.success)
    async def audit_blog_confirmed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from confirmed_identity_audit_view import send_confirmed_identity_audit
        await send_confirmed_identity_audit(interaction, mode="blog")

    @discord.ui.button(label="顔ごとの確定を見直す", emoji="🙂", style=discord.ButtonStyle.success)
    async def audit_face_confirmed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from confirmed_identity_audit_view import send_confirmed_identity_audit
        await send_confirmed_identity_audit(interaction, mode="face")

    @discord.ui.button(label="スキップ済みを再確認", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skipped(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await send_person_review_batch(
            interaction,
            limit=1,
            queue_status="skipped",
            continuous=True,
        )
        if count:
            await interaction.followup.send(
                "⏭️ スキップ済み写真を1枚ずつ表示します。人物設定が終わると自動で次へ進みます。",
                ephemeral=True,
            )

    @discord.ui.button(label="保留した顔を再確認", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def skipped_face(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        from photo_database import reopen_oldest_skipped_face_review
        face_id = await asyncio.to_thread(
            reopen_oldest_skipped_face_review,
            f"{interaction.user.display_name} ({interaction.user.id})",
        )
        if face_id is None:
            await interaction.followup.send("✅ 保留済みの顔はありません。", ephemeral=True)
            return
        from control_panel import invoke_existing_command
        await interaction.followup.send(
            f"🔁 顔ID **{face_id}** を確認待ちへ戻しました。続けて顔確認を開きます。",
            ephemeral=True,
        )
        await invoke_existing_command(interaction, "face_review", "1", admin_required=True)

    @discord.ui.button(label="人物名で写真を検索", emoji="🔎", style=discord.ButtonStyle.success)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import CommandArgumentsModal

        await interaction.response.send_modal(CommandArgumentsModal(
            title="確認済み＋AI推定人物検索", command_name="person", label="人物名",
            placeholder="例: 賀喜遥香", admin_required=True,
        ))


class StatusAdminView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="統合ステータス", emoji="📊", style=discord.ButtonStyle.primary)
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "status", admin_required=True)

    @discord.ui.button(label="AI状況", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def ai(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "ai_status", admin_required=True)

    @discord.ui.button(label="保存状況", emoji="💾", style=discord.ButtonStyle.secondary)
    async def storage(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "photo_storage", admin_required=True)

    @discord.ui.button(label="画像0件を修復", emoji="🛠️", style=discord.ButtonStyle.success)
    async def repair(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import CommandArgumentsModal
        await interaction.response.send_modal(CommandArgumentsModal(
            title="画像0件の記事を修復", command_name="photo_archive_repair_zero",
            label="上限件数とグループ（省略可）", required=False, admin_required=True,
        ))


class BlogDashboardView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="最新記事", emoji="📅", style=discord.ButtonStyle.primary)
    async def latest(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        blogs = await asyncio.to_thread(get_latest_blogs_for_admin, 500)
        await _show_blog_list(interaction, "📅 最新記事", blogs)

    @discord.ui.button(label="投稿者から選ぶ", emoji="👤", style=discord.ButtonStyle.primary)
    async def author(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="👤 **投稿者から選ぶ**\nグループを選択してください。",
            embed=None,
            view=GroupSelectView(),
        )

    @discord.ui.button(label="未解析記事", emoji="🆕", style=discord.ButtonStyle.success)
    async def unprocessed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        blogs = await asyncio.to_thread(get_unprocessed_blogs_for_admin, 500)
        await _show_blog_list(interaction, "🆕 人物確認が未完了の記事", blogs)

    @discord.ui.button(label="エラー記事", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def errors(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        blogs = await asyncio.to_thread(get_error_blogs_for_admin, 500)
        await _show_blog_list(interaction, "⚠️ エラー記事", blogs)

    @discord.ui.button(label="処理件数", emoji="📊", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await asyncio.to_thread(get_blog_workflow_stats_for_admin)

        embed = discord.Embed(
            title="📊 ブログ処理実績",
            color=0x5865F2,
        )
        embed.description = (
            "ブログ単位解析の実績を、AI解析と人物確認に分けて集計しています。\n"
            "「スキップあり」は写真単位の保留、「ブログ除外」は記事そのものを対象外にした件数です。"
        )
        safe_add_field(
            embed,
            name="🤖 AI解析",
            value=(
                f"解析ありブログ **{stats['ai_started_blogs']:,}件**\n"
                f"全画像解析済み **{stats['ai_completed_blogs']:,}件**"
            ),
            inline=True,
            context="blog_workflow_stats.ai",
        )
        safe_add_field(
            embed,
            name="✅ 人物確認",
            value=(
                f"着手済みブログ **{stats['review_started_blogs']:,}件**\n"
                f"完了ブログ **{stats['review_completed_blogs']:,}件**\n"
                f"未着手 **{stats['review_unstarted_blogs']:,}件**"
            ),
            inline=True,
            context="blog_workflow_stats.review",
        )
        safe_add_field(
            embed,
            name="⏭️ スキップ",
            value=(
                f"スキップ写真あり **{stats['blogs_with_skipped_photos']:,}ブログ**\n"
                f"スキップ写真 **{stats['skipped_photos']:,}枚**\n"
                f"ブログ除外 **{stats['hidden_blogs']:,}件**"
            ),
            inline=True,
            context="blog_workflow_stats.skip",
        )
        safe_add_field(
            embed,
            name="📚 対象",
            value=(
                f"人物確認対象ブログ **{stats['review_target_blogs']:,}件**\n"
                f"対象写真 **{stats['review_target_images']:,}枚**"
            ),
            inline=False,
            context="blog_workflow_stats.target",
        )
        if stats["manual_hidden_blogs"]:
            embed.set_footer(
                text=(
                    f"ブログ除外 {stats['hidden_blogs']:,}件のうち、"
                    f"管理者が手動で除外したものは {stats['manual_hidden_blogs']:,}件です。"
                )
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def _show_blog_list(interaction: discord.Interaction, heading: str, blogs: list[dict[str, Any]]) -> None:
    if not blogs:
        kwargs = dict(content=f"{heading}\n対象記事はありません。", embed=None, view=BlogDashboardView())
    else:
        view = ProgressBlogSelectView(blogs, heading)
        kwargs = dict(content=view.text(), embed=None, view=view)

    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


class ProgressBlogSelect(discord.ui.Select):
    PAGE_SIZE = 25

    def __init__(self, parent: "ProgressBlogSelectView"):
        blogs = parent.blogs
        heading = parent.heading
        self.blogs = {str(blog["id"]): blog for blog in blogs}
        self.heading = heading
        start = parent.page * self.PAGE_SIZE
        page_blogs = blogs[start:start + self.PAGE_SIZE]
        options: list[discord.SelectOption] = []
        for blog in page_blogs:
            percent = int(blog.get("progress_percent") or 0)
            total = int(blog.get("progress_total") or blog.get("image_count") or 0)
            completed = int(blog.get("progress_completed") or 0)
            errors = int(blog.get("error_count") or 0)
            title = str(blog.get("title") or "無題")[:100]
            description = f"{blog.get('member_name') or '不明'} / 人物確認 {completed}/{total} ({percent}%)"
            if errors:
                d = int(blog.get("download_error_count") or 0)
                a = int(blog.get("analysis_error_count") or 0)
                f = int(blog.get("face_error_count") or 0)
                detail = "/".join(x for x in (f"取得{d}" if d else "", f"AI{a}" if a else "", f"顔{f}" if f else "") if x)
                description += f" / {detail or f'エラー{errors}'}"
            options.append(discord.SelectOption(
                label=title,
                value=str(blog["id"]),
                description=description[:100],
                emoji="⚠️" if errors else ("✅" if percent == 100 else "📖"),
            ))
        super().__init__(placeholder="記事を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        blog_id = int(self.values[0])
        blog = await asyncio.to_thread(get_blog_progress_for_admin, blog_id)
        if not blog:
            await interaction.followup.send("記事情報を取得できませんでした。", ephemeral=True)
            return
        await interaction.edit_original_response(
            content=None,
            embed=_article_embed(blog),
            view=BlogArticleView(blog_id),
        )


class ProgressBlogSelectView(AdminWorkflowView):
    PAGE_SIZE = 25

    def __init__(self, blogs: list[dict[str, Any]], heading: str, page: int = 0):
        super().__init__()
        self.blogs = list(blogs)
        self.heading = heading
        self.page_count = max(1, (len(self.blogs) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.add_item(ProgressBlogSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        start = self.page * self.PAGE_SIZE + 1 if self.blogs else 0
        end = min((self.page + 1) * self.PAGE_SIZE, len(self.blogs))
        return (
            f"{self.heading}\n記事を選択してください。各項目に人物確認の進捗を表示しています。\n"
            f"表示 **{start}〜{end}件目 / 全{len(self.blogs)}件**（{self.page + 1}/{self.page_count}ページ）"
        )

    @discord.ui.button(label="前の25件", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ProgressBlogSelectView(self.blogs, self.heading, self.page - 1)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="次の25件", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ProgressBlogSelectView(self.blogs, self.heading, self.page + 1)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="📖 **ブログ別の解析・確認**\n記事の探し方を選択してください。",
            embed=None,
            view=BlogDashboardView(),
        )


class GroupSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="グループを選択", options=[discord.SelectOption(label=x) for x in GROUPS])

    async def callback(self, interaction: discord.Interaction) -> None:
        group = self.values[0]
        await interaction.response.defer()
        authors = await asyncio.to_thread(get_blog_authors_for_admin, group, 500)
        view = AuthorSelectView(group, authors, page=0, sort_mode="completion_desc")
        await interaction.edit_original_response(
            content=view.text(), embed=None, view=view,
        )


class GroupSelectView(AdminWorkflowView):
    def __init__(self):
        super().__init__()
        self.add_item(GroupSelect())


def _author_completion_percent(author: dict[str, Any]) -> int:
    total = int(author.get("blog_count") or 0)
    completed = int(author.get("completed_blog_count") or 0)
    return int(author.get("completion_percent") or (round(completed * 100 / total) if total else 0))


def _sort_authors(authors: list[dict[str, Any]], sort_mode: str) -> list[dict[str, Any]]:
    """投稿者一覧を管理画面で選択された基準に従って安定ソートする。"""
    rows = list(authors)
    if sort_mode == "name_asc":
        return sorted(rows, key=lambda a: member_surname_kana_sort_key(str(a.get("member_name") or "")))
    if sort_mode == "blogs_desc":
        return sorted(
            rows,
            key=lambda a: (-int(a.get("blog_count") or 0), member_surname_kana_sort_key(str(a.get("member_name") or ""))),
        )
    if sort_mode == "pending_desc":
        return sorted(
            rows,
            key=lambda a: (
                -max(0, int(a.get("pending_blog_count") or (int(a.get("blog_count") or 0) - int(a.get("completed_blog_count") or 0)))),
                -int(a.get("blog_count") or 0),
                member_surname_kana_sort_key(str(a.get("member_name") or "")),
            ),
        )
    # 既定: 完了率が高い順。同率なら完了件数、ブログ件数、メンバー名の順。
    return sorted(
        rows,
        key=lambda a: (
            -_author_completion_percent(a),
            -int(a.get("completed_blog_count") or 0),
            -int(a.get("blog_count") or 0),
            member_surname_kana_sort_key(str(a.get("member_name") or "")),
        ),
    )


AUTHOR_SORT_LABELS = {
    "completion_desc": "完了%が高い順",
    "name_asc": "メンバー名順（五十音）",
    "blogs_desc": "ブログ件数が多い順",
    "pending_desc": "未完了件数が多い順",
}


class AuthorSortSelect(discord.ui.Select):
    def __init__(self, parent: "AuthorSelectView"):
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=(value == parent.sort_mode),
            )
            for value, label in AUTHOR_SORT_LABELS.items()
        ]
        super().__init__(placeholder="並び順を変更", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        sort_mode = self.values[0]
        view = AuthorSelectView(
            self.parent_view.group,
            self.parent_view.raw_authors,
            page=0,
            sort_mode=sort_mode,
        )
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)


class AuthorSelect(discord.ui.Select):
    def __init__(self, parent: "AuthorSelectView", authors: list[dict[str, Any]]):
        self.parent_view = parent
        self.authors = {str(author.get("member_name") or ""): author for author in authors}
        options = []
        for author in authors:
            total = int(author.get("blog_count") or 0)
            completed = int(author.get("completed_blog_count") or 0)
            pending = max(0, int(author.get("pending_blog_count") or (total - completed)))
            percent = _author_completion_percent(author)
            options.append(discord.SelectOption(
                label=str(author["member_name"])[:100], value=str(author["member_name"])[:100],
                description=(f"完了 {completed}/{total}件 ({percent}%) ・ 未完了 {pending}件")[:100],
                emoji="✅" if total > 0 and completed >= total else "👤",
            ))
        if not options:
            options = [discord.SelectOption(label="投稿者が見つかりません", value="__none__")]
        super().__init__(placeholder="投稿者を選択（完了記事数 / 全記事数）", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        author = self.values[0]
        if author == "__none__":
            await interaction.response.send_message("対象がありません。", ephemeral=True); return
        await interaction.response.defer()
        state = await asyncio.to_thread(get_admin_blog_browser_state, interaction.user.id, self.parent_view.group, author) or {}
        await _open_author_blog_browser(
            interaction, self.parent_view.group, author, self.authors.get(author, {}), user_id=interaction.user.id,
            page=int(state.get("page") or 0), selected_year=int(state.get("selected_year") or 0),
            selected_month=int(state.get("selected_month") or 0), title_query=str(state.get("title_query") or ""),
            only_unprocessed=bool(state.get("only_unprocessed") or 0),
        )


class AuthorSelectView(AdminWorkflowView):
    PAGE_SIZE = 25

    def __init__(self, group: str, authors: list[dict[str, Any]], page: int = 0, sort_mode: str = "completion_desc"):
        super().__init__()
        self.group = group
        self.raw_authors = list(authors)
        self.sort_mode = sort_mode if sort_mode in AUTHOR_SORT_LABELS else "completion_desc"
        self.authors = _sort_authors(self.raw_authors, self.sort_mode)
        self.page_count = max(1, (len(self.authors) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        start = self.page * self.PAGE_SIZE
        page_authors = self.authors[start:start + self.PAGE_SIZE]
        self.add_item(AuthorSelect(self, page_authors))
        self.add_item(AuthorSortSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        start = self.page * self.PAGE_SIZE + 1 if self.authors else 0
        end = min((self.page + 1) * self.PAGE_SIZE, len(self.authors))
        return (
            f"📖 **{self.group}** のブログ投稿者を選択してください。\n"
            f"並び順: **{AUTHOR_SORT_LABELS[self.sort_mode]}**\n"
            f"表示 **{start}〜{end}人目 / 全{len(self.authors)}人**（{self.page + 1}/{self.page_count}ページ）\n"
            "各投稿者には、完了記事数・全記事数・未完了数を表示しています。"
        )

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = AuthorSelectView(self.group, self.raw_authors, self.page - 1, self.sort_mode)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=2)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = AuthorSelectView(self.group, self.raw_authors, self.page + 1, self.sort_mode)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="グループ選択へ", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="👤 **投稿者から選ぶ**\nグループを選択してください。",
            embed=None, view=GroupSelectView(),
        )


class BlogTitleSearchModal(discord.ui.Modal):
    def __init__(self, view: "AuthorBlogBrowserView"):
        super().__init__(title="ブログ記事タイトル検索", timeout=300)
        self.browser_view = view
        self.query = discord.ui.TextInput(
            label="タイトルに含まれる文字",
            placeholder="例：ツアー（空欄で検索解除）",
            required=False,
            max_length=100,
            default=view.title_query[:100],
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _admin(interaction):
            await _deny(interaction)
            return
        await interaction.response.defer()
        await _open_author_blog_browser(
            interaction,
            self.browser_view.group,
            self.browser_view.author,
            self.browser_view.author_stats,
            user_id=interaction.user.id,
            page=0,
            selected_year=self.browser_view.selected_year,
            selected_month=self.browser_view.selected_month,
            title_query=str(self.query.value or "").strip(),
            only_unprocessed=self.browser_view.only_unprocessed,
        )


class BlogArticleSelect(discord.ui.Select):
    def __init__(self, browser: "AuthorBlogBrowserView", blogs: list[dict[str, Any]]):
        self.browser = browser
        options: list[discord.SelectOption] = []
        for blog in blogs:
            percent = int(blog.get("progress_percent") or 0)
            total = int(blog.get("progress_total") or blog.get("image_count") or 0)
            completed = int(blog.get("progress_completed") or 0)
            skipped = int(blog.get("review_skipped_count") or 0)
            errors = int(blog.get("error_count") or 0)
            published = str(blog.get("published_at") or "日付不明")
            description = f"{published} / 確認 {completed}/{total} ({percent}%)"
            if skipped:
                description += f" / スキップ{skipped}"
            if errors:
                d = int(blog.get("download_error_count") or 0)
                a = int(blog.get("analysis_error_count") or 0)
                f = int(blog.get("face_error_count") or 0)
                detail = "/".join(x for x in (f"取得{d}" if d else "", f"AI{a}" if a else "", f"顔{f}" if f else "") if x)
                description += f" / {detail or f'エラー{errors}'}"
            options.append(discord.SelectOption(
                label=str(blog.get("title") or "無題")[:100],
                value=str(blog["id"]),
                description=description[:100],
                emoji="⚠️" if errors else ("✅" if percent == 100 else "📖"),
            ))
        if not options:
            options = [discord.SelectOption(label="条件に一致する記事はありません", value="__none__")]
        super().__init__(placeholder="記事を選択", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "__none__":
            await interaction.response.send_message("条件に一致する記事はありません。", ephemeral=True)
            return
        blog_id = int(value)
        await interaction.response.defer()
        blog = await asyncio.to_thread(get_blog_progress_for_admin, blog_id)
        if not blog:
            await interaction.followup.send("記事情報を取得できませんでした。", ephemeral=True)
            return
        await asyncio.to_thread(
            save_admin_blog_browser_state,
            interaction.user.id,
            self.browser.group,
            self.browser.author,
            page=self.browser.page,
            selected_year=self.browser.selected_year,
            selected_month=self.browser.selected_month,
            title_query=self.browser.title_query,
            only_unprocessed=self.browser.only_unprocessed,
            last_blog_id=blog_id,
        )
        await interaction.edit_original_response(
            content=None,
            embed=_article_embed(blog),
            view=BlogArticleView(blog_id, browser_state=self.browser.state_dict()),
        )


class BlogYearSelect(discord.ui.Select):
    def __init__(self, browser: "AuthorBlogBrowserView", years: list[int]):
        self.browser = browser
        options = [discord.SelectOption(label="すべての年", value="0", default=browser.selected_year == 0)]
        for year in years[:24]:
            options.append(discord.SelectOption(
                label=f"{year}年",
                value=str(year),
                default=browser.selected_year == year,
            ))
        super().__init__(placeholder="年で絞り込み", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        year = int(self.values[0])
        await interaction.response.defer()
        await _open_author_blog_browser(
            interaction, self.browser.group, self.browser.author, self.browser.author_stats,
            user_id=interaction.user.id, page=0, selected_year=year,
            selected_month=self.browser.selected_month, title_query=self.browser.title_query,
            only_unprocessed=self.browser.only_unprocessed,
        )


class BlogMonthSelect(discord.ui.Select):
    def __init__(self, browser: "AuthorBlogBrowserView"):
        self.browser = browser
        options = [discord.SelectOption(label="すべての月", value="0", default=browser.selected_month == 0)]
        for month in range(1, 13):
            options.append(discord.SelectOption(
                label=f"{month}月", value=str(month), default=browser.selected_month == month,
            ))
        super().__init__(placeholder="月で絞り込み", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        month = int(self.values[0])
        await interaction.response.defer()
        await _open_author_blog_browser(
            interaction, self.browser.group, self.browser.author, self.browser.author_stats,
            user_id=interaction.user.id, page=0, selected_year=self.browser.selected_year,
            selected_month=month, title_query=self.browser.title_query,
            only_unprocessed=self.browser.only_unprocessed,
        )


class AuthorBlogBrowserView(AdminWorkflowView):
    PAGE_SIZE = 25

    def __init__(
        self,
        group: str,
        author: str,
        author_stats: dict[str, Any],
        blogs: list[dict[str, Any]],
        years: list[int],
        *,
        total_filtered: int,
        page: int,
        selected_year: int,
        selected_month: int,
        title_query: str,
        only_unprocessed: bool,
        last_blog_id: int = 0,
    ):
        super().__init__()
        self.group = group
        self.author = author
        self.author_stats = author_stats
        self.total_filtered = max(0, int(total_filtered))
        self.page = max(0, int(page))
        self.selected_year = max(0, int(selected_year))
        self.selected_month = max(0, int(selected_month))
        self.title_query = str(title_query or "")
        self.only_unprocessed = bool(only_unprocessed)
        self.last_blog_id = max(0, int(last_blog_id))

        self.add_item(BlogArticleSelect(self, blogs))
        self.add_item(BlogYearSelect(self, years))
        self.add_item(BlogMonthSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * self.PAGE_SIZE >= self.total_filtered
        self.pending.label = "全記事を表示" if self.only_unprocessed else "未完了のみ"
        self.resume.disabled = self.last_blog_id <= 0

    def state_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "author": self.author,
            "author_stats": self.author_stats,
            "page": self.page,
            "selected_year": self.selected_year,
            "selected_month": self.selected_month,
            "title_query": self.title_query,
            "only_unprocessed": self.only_unprocessed,
        }

    async def _reload(self, interaction: discord.Interaction, *, page: int | None = None, **changes: Any) -> None:
        await interaction.response.defer()
        await _open_author_blog_browser(
            interaction,
            self.group,
            self.author,
            self.author_stats,
            user_id=interaction.user.id,
            page=self.page if page is None else page,
            selected_year=int(changes.get("selected_year", self.selected_year)),
            selected_month=int(changes.get("selected_month", self.selected_month)),
            title_query=str(changes.get("title_query", self.title_query)),
            only_unprocessed=bool(changes.get("only_unprocessed", self.only_unprocessed)),
        )

    @discord.ui.button(label="前の25件", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._reload(interaction, page=max(0, self.page - 1))

    @discord.ui.button(label="次の25件", emoji="▶️", style=discord.ButtonStyle.secondary, row=3)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._reload(interaction, page=self.page + 1)

    @discord.ui.button(label="未完了のみ", emoji="🆕", style=discord.ButtonStyle.success, row=3)
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._reload(interaction, page=0, only_unprocessed=not self.only_unprocessed)

    @discord.ui.button(label="タイトル検索", emoji="🔍", style=discord.ButtonStyle.primary, row=3)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(BlogTitleSearchModal(self))

    @discord.ui.button(label="前回の記事", emoji="⏯️", style=discord.ButtonStyle.primary, row=4)
    async def resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.last_blog_id <= 0:
            await interaction.response.send_message("前回開いた記事は記録されていません。", ephemeral=True)
            return
        await interaction.response.defer()
        blog = await asyncio.to_thread(get_blog_progress_for_admin, self.last_blog_id)
        if not blog:
            await interaction.followup.send("前回の記事が見つかりませんでした。", ephemeral=True)
            return
        await interaction.edit_original_response(
            content=None,
            embed=_article_embed(blog, title_prefix="⏯️ 前回の記事"),
            view=BlogArticleView(self.last_blog_id, browser_state=self.state_dict()),
        )

    @discord.ui.button(label="絞り込み解除", emoji="🧹", style=discord.ButtonStyle.secondary, row=4)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._reload(
            interaction, page=0, selected_year=0, selected_month=0,
            title_query="", only_unprocessed=False,
        )

    @discord.ui.button(label="投稿者選択へ", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="👤 **投稿者から選ぶ**\nグループを選択してください。",
            embed=None,
            view=GroupSelectView(),
        )


async def _open_author_blog_browser(
    interaction: discord.Interaction,
    group: str,
    author: str,
    author_stats: dict[str, Any],
    *,
    user_id: int,
    page: int = 0,
    selected_year: int = 0,
    selected_month: int = 0,
    title_query: str = "",
    only_unprocessed: bool = False,
) -> None:
    page = max(0, int(page))
    years_task = asyncio.to_thread(get_blog_years_for_admin, group, author)
    blogs_task = asyncio.to_thread(
        get_blogs_for_admin_filtered,
        group,
        author,
        limit=AuthorBlogBrowserView.PAGE_SIZE,
        offset=page * AuthorBlogBrowserView.PAGE_SIZE,
        year=selected_year or None,
        month=selected_month or None,
        title_query=title_query,
        only_unprocessed=only_unprocessed,
    )
    state_task = asyncio.to_thread(get_admin_blog_browser_state, user_id, group, author)
    years, (blogs, total_filtered), saved_state = await asyncio.gather(years_task, blogs_task, state_task)

    max_page = max(0, (total_filtered - 1) // AuthorBlogBrowserView.PAGE_SIZE) if total_filtered else 0
    if page > max_page:
        page = max_page
        blogs, total_filtered = await asyncio.to_thread(
            get_blogs_for_admin_filtered,
            group,
            author,
            limit=AuthorBlogBrowserView.PAGE_SIZE,
            offset=page * AuthorBlogBrowserView.PAGE_SIZE,
            year=selected_year or None,
            month=selected_month or None,
            title_query=title_query,
            only_unprocessed=only_unprocessed,
        )

    await asyncio.to_thread(
        save_admin_blog_browser_state,
        user_id,
        group,
        author,
        page=page,
        selected_year=selected_year,
        selected_month=selected_month,
        title_query=title_query,
        only_unprocessed=only_unprocessed,
    )

    total = int(author_stats.get("blog_count") or 0)
    completed = int(author_stats.get("completed_blog_count") or 0)
    pending_count = max(0, int(author_stats.get("pending_blog_count") or (total - completed)))
    percent = int(author_stats.get("completion_percent") or (round(completed * 100 / total) if total else 0))
    skipped_on_page = sum(int(blog.get("review_skipped_count") or 0) for blog in blogs)
    page_count = max(1, (total_filtered + AuthorBlogBrowserView.PAGE_SIZE - 1) // AuthorBlogBrowserView.PAGE_SIZE)
    start_no = page * AuthorBlogBrowserView.PAGE_SIZE + 1 if total_filtered else 0
    end_no = min((page + 1) * AuthorBlogBrowserView.PAGE_SIZE, total_filtered)

    filters = []
    if selected_year:
        filters.append(f"{selected_year}年")
    if selected_month:
        filters.append(f"{selected_month}月")
    if title_query:
        filters.append(f"タイトル「{title_query}」")
    if only_unprocessed:
        filters.append("未完了のみ")
    filter_text = " / ".join(filters) if filters else "なし"

    heading = (
        f"👤 **{group} / {author}**\n"
        f"記事進捗: 完了 **{completed}/{total}件**（{percent}%）・未完了 **{pending_count}件**\n"
        f"表示: **{start_no}〜{end_no}/{total_filtered}件**（{page + 1}/{page_count}ページ）\n"
        f"絞り込み: **{filter_text}** / このページのスキップ写真 **{skipped_on_page}枚**\n"
        "記事を選択してください。25件を超える場合はページ送りできます。"
    )
    view = AuthorBlogBrowserView(
        group,
        author,
        author_stats,
        blogs,
        years,
        total_filtered=total_filtered,
        page=page,
        selected_year=selected_year,
        selected_month=selected_month,
        title_query=title_query,
        only_unprocessed=only_unprocessed,
        last_blog_id=int((saved_state or {}).get("last_blog_id") or 0),
    )
    await interaction.edit_original_response(content=heading, embed=None, view=view)


class BlogPhotoSelect(discord.ui.Select):
    def __init__(self, parent: "BlogPhotoBrowserView", rows: list[dict[str, Any]]):
        options = []
        for item in rows:
            idx = int(item.get("image_index") or 0)
            status = str(item.get("review_status") or "pending")
            icon = "✅" if status == "completed" else ("⏭️" if status == "skipped" else "⏳")
            people = str(item.get("confirmed_people") or "").strip()
            if people:
                try:
                    from person_labels import format_people_for_users
                    desc = format_people_for_users(people) or "登録済み"
                except Exception:
                    desc = people
            else:
                if status == "completed":
                    desc = "人物なし"
                elif status == "skipped":
                    desc = "スキップ済み"
                else:
                    desc = "未確認"
            options.append(discord.SelectOption(label=f"{icon} {idx}枚目", value=str(item["image_id"]), description=desc[:100]))
        super().__init__(placeholder="確認する写真を選択", min_values=1, max_values=1, options=options, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        image_id = int(self.values[0])
        item = next((x for x in self.parent_view.all_rows if int(x["image_id"]) == image_id), None)
        if not item:
            await interaction.response.send_message("写真情報を取得できませんでした。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await send_person_review(interaction, item, session=None)


class BulkPhotoSelect(discord.ui.Select):
    def __init__(self, parent: "BulkPhotoSelectionView", rows: list[dict[str, Any]]):
        self.parent_view=parent
        opts=[]
        for item in rows:
            iid=int(item["image_id"]); num=int(item.get("image_index") or 0)
            status=str(item.get("review_status") or "pending")
            people = str(item.get("confirmed_people") or "").strip()
            if status == "completed":
                description = people or "人物なし"
            elif status == "skipped":
                description = "スキップ済み"
            else:
                description = "未確認"
            opts.append(discord.SelectOption(label=f"{num}枚目",value=str(iid),description=description[:100],default=iid in parent.selected_ids))
        super().__init__(placeholder="一括確定する写真を複数選択",min_values=0,max_values=len(opts),options=opts,row=0)
    async def callback(self,interaction:discord.Interaction)->None:
        page_ids={int(x["image_id"]) for x in self.parent_view.page_rows()}
        self.parent_view.selected_ids.difference_update(page_ids); self.parent_view.selected_ids.update(int(x) for x in self.values)
        v=BulkPhotoSelectionView(self.parent_view.blog_id,self.parent_view.all_rows,self.parent_view.page,self.parent_view.selected_ids)
        await interaction.response.edit_message(content=v.text(),view=v)

class BulkPeopleModal(discord.ui.Modal):
    def __init__(self,parent:"BulkPhotoSelectionView"):
        super().__init__(title="選択写真を一括確定",timeout=300); self.parent_view=parent
        self.people=discord.ui.TextInput(label="人物名（複数は読点・カンマ区切り）",placeholder="例：金村美玖、小坂菜緒",max_length=500)
        self.add_item(self.people)
    async def on_submit(self,interaction:discord.Interaction)->None:
        names=[x.strip() for x in str(self.people.value).replace(',', '、').replace('，','、').split('、') if x.strip()]
        if not names or not self.parent_view.selected_ids:
            await interaction.response.send_message("写真と人物を選択してください。",ephemeral=True); return
        selected=sorted(self.parent_view.selected_ids)
        overwrite=sum(1 for r in self.parent_view.all_rows if int(r['image_id']) in self.parent_view.selected_ids and str(r.get('review_status'))=='completed')
        await interaction.response.send_message(
            f"⚠️ **一括確定の最終確認**\n"
            f"対象 **{len(selected)}枚** / 既存登録の上書き **{overwrite}枚**\n"
            f"写真ID: {', '.join(map(str, selected))}\n"
            f"人物: {'、'.join(names)}",
            view=BulkFinalConfirmView(self.parent_view,names),ephemeral=True)

class BulkFinalConfirmView(discord.ui.View):
    def __init__(self,parent:"BulkPhotoSelectionView",names:list[str]): super().__init__(timeout=300); self.parent=parent; self.names=names
    @discord.ui.button(label="この内容で一括確定",emoji="✅",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction:discord.Interaction,_:discord.ui.Button)->None:
        await interaction.response.defer(ephemeral=True); ok=0; failed=0
        for iid in sorted(self.parent.selected_ids):
            try:
                await asyncio.to_thread(create_people_snapshot,iid,interaction.user.id,"bulk_people_confirm")
                await asyncio.to_thread(set_confirmed_image_people,iid,self.names,confirmed_by=f"{interaction.user} ({interaction.user.id})",note="ブログ内写真を選択して一括確定")
                ok+=1
            except Exception:
                LOGGER.exception("選択式一括確定に失敗 image_id=%s",iid); failed+=1
        await interaction.followup.send(
            f"✅ 一括確定完了\n成功 **{ok}枚** / 失敗 **{failed}枚**\n人物: {'、'.join(self.names)}",
            ephemeral=True,
        )
    @discord.ui.button(label="キャンセル",style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction:discord.Interaction,_:discord.ui.Button)->None:
        await interaction.response.edit_message(content="一括確定をキャンセルしました。",view=None)

class BulkPhotoSelectionView(AdminWorkflowView):
    PAGE_SIZE=25
    def __init__(self,blog_id:int,rows:list[dict[str,Any]],page:int=0,selected_ids:set[int]|None=None):
        super().__init__(); self.blog_id=int(blog_id); self.all_rows=rows; self.selected_ids=set(selected_ids or set())
        self.page_count=max(1,(len(rows)+self.PAGE_SIZE-1)//self.PAGE_SIZE); self.page=max(0,min(page,self.page_count-1))
        if self.page_rows(): self.add_item(BulkPhotoSelect(self,self.page_rows()))
        self.previous.disabled=self.page<=0; self.next.disabled=self.page>=self.page_count-1
    def page_rows(self):
        st=self.page*self.PAGE_SIZE; return self.all_rows[st:st+self.PAGE_SIZE]
    def text(self):
        nums=[str(int(r.get('image_index') or 0)) for r in self.all_rows if int(r['image_id']) in self.selected_ids]
        return (
            f"☑️ **写真を選んで一括確定**\n"
            f"選択中 **{len(nums)}枚**: {', '.join(nums[:40]) or 'なし'}\n"
            f"ページ {self.page+1}/{self.page_count}。ページを移動しても選択は保持されます。"
        )
    @discord.ui.button(label="前の25枚",emoji="◀️",style=discord.ButtonStyle.secondary,row=1)
    async def previous(self,interaction:discord.Interaction,_:discord.ui.Button):
        v=BulkPhotoSelectionView(self.blog_id,self.all_rows,self.page-1,self.selected_ids); await interaction.response.edit_message(content=v.text(),view=v)
    @discord.ui.button(label="次の25枚",emoji="▶️",style=discord.ButtonStyle.secondary,row=1)
    async def next(self,interaction:discord.Interaction,_:discord.ui.Button):
        v=BulkPhotoSelectionView(self.blog_id,self.all_rows,self.page+1,self.selected_ids); await interaction.response.edit_message(content=v.text(),view=v)
    @discord.ui.button(label="未確認を全選択",emoji="⏳",style=discord.ButtonStyle.primary,row=1)
    async def pending(self,interaction:discord.Interaction,_:discord.ui.Button):
        ids={int(r['image_id']) for r in self.all_rows if str(r.get('review_status') or 'pending')!='completed'}
        v=BulkPhotoSelectionView(self.blog_id,self.all_rows,self.page,ids); await interaction.response.edit_message(content=v.text(),view=v)
    @discord.ui.button(label="選択解除",emoji="🧹",style=discord.ButtonStyle.secondary,row=2)
    async def clear(self,interaction:discord.Interaction,_:discord.ui.Button):
        v=BulkPhotoSelectionView(self.blog_id,self.all_rows,self.page,set()); await interaction.response.edit_message(content=v.text(),view=v)
    @discord.ui.button(label="人物を設定して確定",emoji="👥",style=discord.ButtonStyle.success,row=2)
    async def people(self,interaction:discord.Interaction,_:discord.ui.Button):
        if not self.selected_ids: await interaction.response.send_message("先に写真を選択してください。",ephemeral=True); return
        await interaction.response.send_modal(BulkPeopleModal(self))


class BlogPhotoBrowserView(AdminWorkflowView):
    PAGE_SIZE = 25

    def __init__(self, blog_id: int, rows: list[dict[str, Any]], page: int = 0):
        super().__init__()
        self.blog_id = int(blog_id)
        self.all_rows = rows
        self.page_count = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        start = self.page * self.PAGE_SIZE
        page_rows = rows[start:start + self.PAGE_SIZE]
        if page_rows:
            self.add_item(BlogPhotoSelect(self, page_rows))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        completed = sum(str(x.get("review_status")) == "completed" for x in self.all_rows)
        skipped = sum(str(x.get("review_status")) == "skipped" for x in self.all_rows)
        pending = max(0, len(self.all_rows) - completed - skipped)
        start = self.page * self.PAGE_SIZE + 1 if self.all_rows else 0
        end = min((self.page + 1) * self.PAGE_SIZE, len(self.all_rows))
        return (
            f"🖼️ **ブログ内写真一覧**\n"
            f"全 **{len(self.all_rows)}枚** / ✅完了 **{completed}** / "
            f"⏳未確認 **{pending}** / ⏭️スキップ **{skipped}**\n"
            f"表示 **{start}〜{end}枚目**（{self.page + 1}/{self.page_count}ページ）\n"
            "写真番号を選ぶと、1枚だけの確認画面が開きます。"
            "長い画像一覧をスクロールする必要はありません。"
        )

    @discord.ui.button(label="前の25枚", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = BlogPhotoBrowserView(self.blog_id, self.all_rows, self.page - 1)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="次の25枚", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = BlogPhotoBrowserView(self.blog_id, self.all_rows, self.page + 1)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="未確認を連続確認", emoji="▶️", style=discord.ButtonStyle.success, row=1)
    async def start_pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await send_blog_person_review_batch(interaction, self.blog_id, 1, continuous=True, require_final_confirmation=True)
        if count:
            await interaction.followup.send("未確認写真を1枚ずつ表示します。保存またはスキップすると自動で次へ進みます。", ephemeral=True)

    @discord.ui.button(label="写真を選んで一括確定", emoji="☑️", style=discord.ButtonStyle.primary, row=2)
    async def bulk_select(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = BulkPhotoSelectionView(self.blog_id, self.all_rows)
        await interaction.response.send_message(content=view.text(), view=view, ephemeral=True)

    @discord.ui.button(label="一覧を更新", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(get_blog_images_for_review_admin, self.blog_id)
        view = BlogPhotoBrowserView(self.blog_id, rows, self.page)
        await interaction.edit_original_response(content=view.text(), embed=None, view=view)


class BlogAuthorChangeConfirmView(AdminWorkflowView):
    def __init__(self, blog_id: int, new_group: str, new_member: str, *, restore_if_hidden: bool = False, browser_state: dict[str, Any] | None = None):
        super().__init__()
        self.blog_id = int(blog_id)
        self.new_group = str(new_group)
        self.new_member = str(new_member)
        self.restore_if_hidden = bool(restore_if_hidden)
        self.browser_state = dict(browser_state or {})

    @discord.ui.button(label="この内容で変更", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(
            update_photo_blog_info_for_admin,
            self.blog_id,
            group_name=self.new_group,
            member_name=self.new_member,
            restore_if_hidden=self.restore_if_hidden,
        )
        if not result:
            await interaction.followup.send("対象記事が見つかりませんでした。", ephemeral=True)
            return
        before = result["before"]
        try:
            from admin_operations import write_audit
            await asyncio.to_thread(
                write_audit, interaction.user.id, "blog_author_update",
                target_type="blog", target_id=self.blog_id,
                detail=(f"{before.get('group_name') or '不明'}/{before.get('member_name') or '不明'}"
                        f" -> {self.new_group}/{self.new_member}; restore={self.restore_if_hidden}"),
            )
        except Exception:
            LOGGER.exception("ブログ投稿者変更の監査ログ保存に失敗しました")
        text = (
            f"✅ ブログID **{self.blog_id}** の投稿者を変更しました。\n\n"
            f"変更前：**{before.get('member_name') or '投稿者不明'}**\n"
            f"変更後：**{self.new_member}**\n"
            f"グループ：**{self.new_group}**\n"
            f"関連画像：**{int(result.get('image_count') or 0)}枚**"
        )
        if self.restore_if_hidden:
            text += "\n\n👁️ 除外状態も解除し、人物確認対象へ戻しました。"
        await interaction.followup.send(text, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="投稿者変更をキャンセルしました。", embed=None, view=None)


class BlogAuthorMemberSelect(discord.ui.Select):
    PAGE_SIZE = 25
    def __init__(self, parent: "BlogAuthorMemberSelectView") -> None:
        self.parent_view = parent
        start = parent.page * self.PAGE_SIZE
        members = parent.members[start:start + self.PAGE_SIZE]
        options = [discord.SelectOption(label=name[:100], value=name[:100], emoji="👤") for name in members]
        super().__init__(placeholder="正しい投稿者を選択", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        member = self.values[0]
        blog = await asyncio.to_thread(get_photo_blog_for_admin_edit, self.parent_view.blog_id)
        if not blog:
            await interaction.followup.send("対象記事が見つかりませんでした。", ephemeral=True)
            return
        embed = safe_embed(title="✏️ ブログ投稿者の変更確認", color=discord.Color.orange())
        safe_add_field(embed, name="記事", value=str(blog.get("title") or "無題"), inline=False)
        safe_add_field(embed, name="変更前", value=f"{blog.get('group_name') or '不明'} / {blog.get('member_name') or '投稿者不明'}", inline=False)
        safe_add_field(embed, name="変更後", value=f"{self.parent_view.group_name} / {member}", inline=False)
        safe_add_field(embed, name="関連画像", value=f"{int(blog.get('image_count') or 0)}枚", inline=True)
        if self.parent_view.restore_if_hidden:
            safe_add_field(embed, name="復元", value="投稿者変更と同時に除外状態を解除します。", inline=False)
        await interaction.edit_original_response(
            content=None, embed=embed,
            view=BlogAuthorChangeConfirmView(
                self.parent_view.blog_id, self.parent_view.group_name, member,
                restore_if_hidden=self.parent_view.restore_if_hidden,
                browser_state=self.parent_view.browser_state,
            ),
        )


class BlogAuthorMemberSelectView(AdminWorkflowView):
    PAGE_SIZE = 25
    def __init__(self, blog_id: int, group_name: str, generation_name: str, members: list[str], *, page: int = 0, restore_if_hidden: bool = False, browser_state: dict[str, Any] | None = None):
        super().__init__()
        self.blog_id = int(blog_id)
        self.group_name = str(group_name)
        self.generation_name = str(generation_name)
        self.members = list(members)
        self.page_count = max(1, (len(self.members) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.restore_if_hidden = bool(restore_if_hidden)
        self.browser_state = dict(browser_state or {})
        self.add_item(BlogAuthorMemberSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def text(self) -> str:
        start = self.page * self.PAGE_SIZE + 1
        end = min((self.page + 1) * self.PAGE_SIZE, len(self.members))
        return f"👤 **{self.group_name} / {self.generation_name}**\n正しい投稿者を選択してください。\n表示 **{start}〜{end}人目 / 全{len(self.members)}人**"

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = BlogAuthorMemberSelectView(self.blog_id, self.group_name, self.generation_name, self.members, page=self.page-1, restore_if_hidden=self.restore_if_hidden, browser_state=self.browser_state)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = BlogAuthorMemberSelectView(self.blog_id, self.group_name, self.generation_name, self.members, page=self.page+1, restore_if_hidden=self.restore_if_hidden, browser_state=self.browser_state)
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)


class BlogAuthorGenerationSelect(discord.ui.Select):
    def __init__(self, parent: "BlogAuthorGenerationSelectView") -> None:
        self.parent_view = parent
        generations = list(SAKAMICHI_MEMBERS.get(parent.group_name, {}).keys())
        options = [discord.SelectOption(label=g[:100], value=g[:100]) for g in generations[:25]]
        super().__init__(placeholder="期・区分を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        generation = self.values[0]
        members = list(SAKAMICHI_MEMBERS.get(self.parent_view.group_name, {}).get(generation, []))
        view = BlogAuthorMemberSelectView(
            self.parent_view.blog_id, self.parent_view.group_name, generation, members,
            restore_if_hidden=self.parent_view.restore_if_hidden, browser_state=self.parent_view.browser_state,
        )
        await interaction.response.edit_message(content=view.text(), embed=None, view=view)


class BlogAuthorGenerationSelectView(AdminWorkflowView):
    def __init__(self, blog_id: int, group_name: str, *, restore_if_hidden: bool = False, browser_state: dict[str, Any] | None = None):
        super().__init__()
        self.blog_id = int(blog_id)
        self.group_name = str(group_name)
        self.restore_if_hidden = bool(restore_if_hidden)
        self.browser_state = dict(browser_state or {})
        self.add_item(BlogAuthorGenerationSelect(self))


class BlogAuthorGroupSelect(discord.ui.Select):
    def __init__(self, parent: "BlogAuthorGroupSelectView") -> None:
        self.parent_view = parent
        super().__init__(placeholder="正しいグループを選択", options=[discord.SelectOption(label=g, value=g) for g in GROUPS])

    async def callback(self, interaction: discord.Interaction) -> None:
        group = self.values[0]
        await interaction.response.edit_message(
            content=f"✏️ **ブログ投稿者を変更**\n{group}の期・区分を選択してください。",
            embed=None,
            view=BlogAuthorGenerationSelectView(
                self.parent_view.blog_id, group, restore_if_hidden=self.parent_view.restore_if_hidden, browser_state=self.parent_view.browser_state,
            ),
        )


class BlogAuthorGroupSelectView(AdminWorkflowView):
    def __init__(self, blog_id: int, *, restore_if_hidden: bool = False, browser_state: dict[str, Any] | None = None):
        super().__init__()
        self.blog_id = int(blog_id)
        self.restore_if_hidden = bool(restore_if_hidden)
        self.browser_state = dict(browser_state or {})
        self.add_item(BlogAuthorGroupSelect(self))


class BlogArticleView(AdminWorkflowView):
    def __init__(self, blog_id: int, browser_state: dict[str, Any] | None = None):
        super().__init__()
        self.blog_id = int(blog_id)
        self.browser_state = dict(browser_state or {})

    @discord.ui.button(label="写真一覧・ギャラリー", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def gallery(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        rows = await asyncio.to_thread(get_blog_images_for_review_admin, self.blog_id)
        if not rows:
            await interaction.followup.send("このブログには写真がありません。", ephemeral=True)
            return
        view = BlogPhotoBrowserView(self.blog_id, rows)
        await interaction.edit_original_response(content=view.text(), embed=None, view=view)

    @discord.ui.button(label="写真の人物確認を開始・続ける", emoji="✅", style=discord.ButtonStyle.success)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await send_blog_person_review_batch(
            interaction,
            self.blog_id,
            1,
            continuous=True,
            require_final_confirmation=True,
        )
        if count:
            await interaction.followup.send(
                "✅ このブログの写真人物確認を開始しました。確定・スキップ後は自動で次へ進みます。",
                ephemeral=True,
            )

    @discord.ui.button(label="進捗を更新", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        blog = await asyncio.to_thread(get_blog_progress_for_admin, self.blog_id)
        if not blog:
            await interaction.followup.send("記事情報を取得できませんでした。", ephemeral=True)
            return
        await interaction.edit_original_response(content=None, embed=_article_embed(blog), view=BlogArticleView(self.blog_id, browser_state=self.browser_state))

    @discord.ui.button(label="未解析写真をAI解析", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def ai_pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run(interaction, "ai", True)

    @discord.ui.button(label="未処理写真の顔を検出", emoji="🙂", style=discord.ButtonStyle.secondary)
    async def face_pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run(interaction, "face", True)

    @discord.ui.button(label="ブログ投稿者を編集", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def edit_author(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "✏️ **ブログ投稿者を変更**\n正しいグループを選択してください。",
            view=BlogAuthorGroupSelectView(self.blog_id, browser_state=self.browser_state),
            ephemeral=True,
        )

    @discord.ui.button(label="エラーを再試行待ちへ", emoji="♻️", style=discord.ButtonStyle.danger, row=2)
    async def retry_errors(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        counts = await asyncio.to_thread(reset_blog_processing_errors_for_admin, self.blog_id)
        total = sum(int(value) for value in counts.values())
        await interaction.followup.send(
            (
                "♻️ 再試行可能な失敗を待機状態へ戻しました。\n"
                f"画像取得 **{counts['download']}枚** / "
                f"AI解析 **{counts['analysis']}枚** / "
                f"顔認証 **{counts['face']}枚**\n"
                "必要に応じて保存処理・AI解析・顔認証を再実行してください。"
                if total else
                "✅ 再試行可能な失敗はありませんでした。古いエラー文字だけ残っていた場合は整理済みです。"
            ),
            ephemeral=True,
        )
        blog = await asyncio.to_thread(get_blog_progress_for_admin, self.blog_id)
        if blog:
            try:
                await interaction.edit_original_response(
                    content=None,
                    embed=_article_embed(blog),
                    view=BlogArticleView(self.blog_id, browser_state=self.browser_state),
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.browser_state.get("group") and self.browser_state.get("author"):
            await interaction.response.defer()
            await _open_author_blog_browser(
                interaction,
                str(self.browser_state["group"]),
                str(self.browser_state["author"]),
                dict(self.browser_state.get("author_stats") or {}),
                user_id=interaction.user.id,
                page=int(self.browser_state.get("page") or 0),
                selected_year=int(self.browser_state.get("selected_year") or 0),
                selected_month=int(self.browser_state.get("selected_month") or 0),
                title_query=str(self.browser_state.get("title_query") or ""),
                only_unprocessed=bool(self.browser_state.get("only_unprocessed") or False),
            )
            return
        await interaction.response.edit_message(
            content="📖 **ブログ別の解析・確認**\n記事の探し方を選択してください。",
            embed=None,
            view=BlogDashboardView(),
        )

    async def _run(self, interaction: discord.Interaction, mode: str, only_pending: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        ids = await asyncio.to_thread(
            get_blog_image_ids,
            self.blog_id,
            only_unanalyzed=(mode == "ai" and only_pending),
            only_unscanned=(mode == "face" and only_pending),
        )
        if not ids:
            await interaction.followup.send("✅ 対象画像はありません。", ephemeral=True)
            return
        ok = review = failed = waiting_person_review = 0
        errors: list[str] = []
        for image_id in ids:
            try:
                if mode == "ai":
                    result = await analyze_photo_image(image_id, manual_api=True)
                    if result.get("status") == "review":
                        review += 1
                    elif result.get("status") == "completed":
                        ok += 1
                    elif result.get("status") == "waiting_person_review":
                        waiting_person_review += 1
                    else:
                        failed += 1
                else:
                    await asyncio.to_thread(detect_faces_for_image, image_id)
                    ok += 1
            except Exception as error:
                failed += 1
                if len(errors) < 3:
                    errors.append(f"ID {image_id}: {type(error).__name__}: {error}")
        text = (
            f"✅ 処理完了\n対象 **{len(ids)}枚** / 成功 **{ok}枚** / "
            f"AI確認待ち **{review}枚** / 人物確認待ち **{waiting_person_review}枚** / "
            f"失敗 **{failed}枚**"
        )
        if errors:
            text += "\n" + "\n".join(errors)
        await interaction.followup.send(text[:1900], ephemeral=True)


def make_admin_category_view() -> discord.ui.View:
    return CategoryAdminView()
