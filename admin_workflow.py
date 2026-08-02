"""ZIP44 管理者向け選択式ワークフロー。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from photo_ai_analyzer import analyze_photo_image
from local_face_recognition import detect_faces_for_image
from photo_database import (
    get_blog_authors_for_admin,
    get_blogs_for_admin,
    get_blog_image_ids,
    get_blog_progress_for_admin,
    get_error_blogs_for_admin,
    get_latest_blogs_for_admin,
    get_unprocessed_blogs_for_admin,
)
from photo_review_view import send_blog_person_review_batch, send_person_review_batch

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
        text = "⚠️ 管理画面の操作中にエラーが発生しました。もう一度お試しください。"
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
        lines.append(f"⚠️ エラー画像 **{errors}枚**")
    return "\n".join(lines)


def _article_embed(blog: dict[str, Any], *, title_prefix: str = "📖 ブログ記事") -> discord.Embed:
    title = str(blog.get("title") or "無題")
    embed = discord.Embed(
        title=f"{title_prefix}: {title}"[:256],
        description=_article_summary(blog),
        color=discord.Color.red() if int(blog.get("error_count") or 0) else discord.Color.green(),
    )
    embed.add_field(name="グループ", value=str(blog.get("group_name") or "不明"), inline=True)
    embed.add_field(name="投稿者", value=str(blog.get("member_name") or "不明"), inline=True)
    embed.add_field(name="投稿日", value=str(blog.get("published_at") or "不明"), inline=False)
    if blog.get("last_reviewed_at"):
        embed.add_field(name="最終確認日時", value=str(blog["last_reviewed_at"]), inline=False)
    if blog.get("blog_url"):
        embed.add_field(name="ブログ", value=f"[元記事を開く]({blog['blog_url']})", inline=False)
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

    @discord.ui.button(label="人物確認", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("✅ 人物確認", view=ReviewAdminView(), ephemeral=True)

    @discord.ui.button(label="ブログ単位解析", emoji="📖", style=discord.ButtonStyle.success)
    async def blog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "📖 **ブログ単位解析**\n記事の探し方を選択してください。",
            view=BlogDashboardView(),
            ephemeral=True,
        )

    @discord.ui.button(label="タグ管理", emoji="🏷️", style=discord.ButtonStyle.secondary)
    async def tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command

        await invoke_existing_command(interaction, "photo_tags", admin_required=True)

    @discord.ui.button(label="状態・修復", emoji="🛠️", style=discord.ButtonStyle.secondary)
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("🛠️ 状態・修復", view=StatusAdminView(), ephemeral=True)


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

    @discord.ui.button(label="顔認証", emoji="🙂", style=discord.ButtonStyle.secondary)
    async def face(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImageIdModal("face_scan", "写真1枚を顔認証"))


class ReviewAdminView(AdminWorkflowView):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="人物確認を開始", emoji="✅", style=discord.ButtonStyle.primary)
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
                "✅ 人物確認を1枚ずつ開始しました。確定・スキップすると画面が消え、自動で次へ進みます。",
                ephemeral=True,
            )

    @discord.ui.button(label="顔確認を開始", emoji="🙂", style=discord.ButtonStyle.primary)
    async def face(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import invoke_existing_command

        await invoke_existing_command(interaction, "face_review", "5", admin_required=True)

    @discord.ui.button(label="ブログ単位人物確認", emoji="📖", style=discord.ButtonStyle.success)
    async def blog_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="📖 **ブログ単位人物確認**\n記事の探し方を選択してください。",
            view=BlogDashboardView(),
        )

    @discord.ui.button(label="スキップ済みを表示", emoji="⏭️", style=discord.ButtonStyle.secondary)
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

    @discord.ui.button(label="AI推定人物で検索", emoji="🤖", style=discord.ButtonStyle.success)
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
        blogs = await asyncio.to_thread(get_latest_blogs_for_admin, 25)
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
        blogs = await asyncio.to_thread(get_unprocessed_blogs_for_admin, 25)
        await _show_blog_list(interaction, "🆕 人物確認が未完了の記事", blogs)

    @discord.ui.button(label="エラー記事", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def errors(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        blogs = await asyncio.to_thread(get_error_blogs_for_admin, 25)
        await _show_blog_list(interaction, "⚠️ エラー記事", blogs)


async def _show_blog_list(interaction: discord.Interaction, heading: str, blogs: list[dict[str, Any]]) -> None:
    if not blogs:
        await interaction.response.edit_message(content=f"{heading}\n対象記事はありません。", embed=None, view=BlogDashboardView())
        return
    await interaction.response.edit_message(
        content=f"{heading}\n記事を選択してください。各項目に人物確認の進捗を表示しています。",
        embed=None,
        view=ProgressBlogSelectView(blogs, heading),
    )


class ProgressBlogSelect(discord.ui.Select):
    def __init__(self, blogs: list[dict[str, Any]], heading: str):
        self.blogs = {str(blog["id"]): blog for blog in blogs}
        self.heading = heading
        options: list[discord.SelectOption] = []
        for blog in blogs[:25]:
            percent = int(blog.get("progress_percent") or 0)
            total = int(blog.get("progress_total") or blog.get("image_count") or 0)
            completed = int(blog.get("progress_completed") or 0)
            errors = int(blog.get("error_count") or 0)
            title = str(blog.get("title") or "無題")[:100]
            description = f"{blog.get('member_name') or '不明'} / 人物確認 {completed}/{total} ({percent}%)"
            if errors:
                description += f" / エラー{errors}"
            options.append(discord.SelectOption(
                label=title,
                value=str(blog["id"]),
                description=description[:100],
                emoji="⚠️" if errors else ("✅" if percent == 100 and total > 0 else "📖"),
            ))
        super().__init__(placeholder="記事を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        blog_id = int(self.values[0])
        blog = await asyncio.to_thread(get_blog_progress_for_admin, blog_id)
        if not blog:
            await interaction.response.send_message("記事情報を取得できませんでした。", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=None,
            embed=_article_embed(blog),
            view=BlogArticleView(blog_id),
        )


class ProgressBlogSelectView(AdminWorkflowView):
    def __init__(self, blogs: list[dict[str, Any]], heading: str):
        super().__init__()
        self.add_item(ProgressBlogSelect(blogs, heading))

    @discord.ui.button(label="戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="📖 **ブログ単位解析**\n記事の探し方を選択してください。",
            embed=None,
            view=BlogDashboardView(),
        )


class GroupSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="グループを選択", options=[discord.SelectOption(label=x) for x in GROUPS])

    async def callback(self, interaction: discord.Interaction) -> None:
        group = self.values[0]
        await interaction.response.defer()
        authors = await asyncio.to_thread(get_blog_authors_for_admin, group, 25)
        await interaction.edit_original_response(
            content=(
                f"📖 **{group}** のブログ投稿者を選択してください。\n"
                "各投稿者には、完了記事数・全記事数・未完了数を表示しています。"
            ),
            embed=None,
            view=AuthorSelectView(group, authors),
        )


class GroupSelectView(AdminWorkflowView):
    def __init__(self):
        super().__init__()
        self.add_item(GroupSelect())


class AuthorSelect(discord.ui.Select):
    def __init__(self, group: str, authors: list[dict[str, Any]]):
        self.group = group
        self.authors = {str(author.get("member_name") or ""): author for author in authors}
        options = []
        for author in authors:
            total = int(author.get("blog_count") or 0)
            completed = int(author.get("completed_blog_count") or 0)
            pending = max(0, int(author.get("pending_blog_count") or (total - completed)))
            percent = int(author.get("completion_percent") or (round(completed * 100 / total) if total else 0))
            options.append(discord.SelectOption(
                label=str(author["member_name"])[:100],
                value=str(author["member_name"])[:100],
                description=(
                    f"完了 {completed}/{total}件 ({percent}%) ・ 未完了 {pending}件"
                )[:100],
                emoji="✅" if total > 0 and completed >= total else "👤",
            ))
        if not options:
            options = [discord.SelectOption(label="投稿者が見つかりません", value="__none__")]
        super().__init__(placeholder="投稿者を選択（完了記事数 / 全記事数）", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        author = self.values[0]
        if author == "__none__":
            await interaction.response.send_message("対象がありません。", ephemeral=True)
            return

        await interaction.response.defer()
        blogs = await asyncio.to_thread(get_blogs_for_admin, self.group, author, 25)
        progress_results = await asyncio.gather(*(
            asyncio.to_thread(get_blog_progress_for_admin, int(blog["id"]))
            for blog in blogs
        ))
        detailed = [progress for progress in progress_results if progress]

        stats = self.authors.get(author, {})
        total = int(stats.get("blog_count") or len(blogs))
        completed = int(stats.get("completed_blog_count") or 0)
        pending = max(0, int(stats.get("pending_blog_count") or (total - completed)))
        percent = int(stats.get("completion_percent") or (round(completed * 100 / total) if total else 0))
        heading = (
            f"👤 {self.group} / {author}\n"
            f"記事進捗: 完了 **{completed}/{total}件**（{percent}%）・未完了 **{pending}件**"
        )

        if not detailed:
            await interaction.edit_original_response(
                content=f"{heading}\n対象記事はありません。",
                embed=None,
                view=BlogDashboardView(),
            )
            return
        await interaction.edit_original_response(
            content=f"{heading}\n記事を選択してください。各項目には画像単位の人物確認進捗を表示しています。",
            embed=None,
            view=ProgressBlogSelectView(detailed, heading),
        )


class AuthorSelectView(AdminWorkflowView):
    def __init__(self, group: str, authors: list[dict[str, Any]]):
        super().__init__()
        self.add_item(AuthorSelect(group, authors))


class BlogArticleView(AdminWorkflowView):
    def __init__(self, blog_id: int):
        super().__init__()
        self.blog_id = int(blog_id)

    @discord.ui.button(label="人物確認を開始・続ける", emoji="✅", style=discord.ButtonStyle.success)
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
                "✅ このブログの人物確認を1枚ずつ開始しました。確定・スキップすると画面が消え、自動で次へ進みます。",
                ephemeral=True,
            )

    @discord.ui.button(label="進捗を更新", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        blog = await asyncio.to_thread(get_blog_progress_for_admin, self.blog_id)
        if not blog:
            await interaction.response.send_message("記事情報を取得できませんでした。", ephemeral=True)
            return
        await interaction.response.edit_message(content=None, embed=_article_embed(blog), view=BlogArticleView(self.blog_id))

    @discord.ui.button(label="未解析だけAI人物判定", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def ai_pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run(interaction, "ai", True)

    @discord.ui.button(label="未処理だけ顔認証", emoji="🙂", style=discord.ButtonStyle.secondary)
    async def face_pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run(interaction, "face", True)

    @discord.ui.button(label="戻る", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="📖 **ブログ単位解析**\n記事の探し方を選択してください。",
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
        ok = review = failed = 0
        errors: list[str] = []
        for image_id in ids:
            try:
                if mode == "ai":
                    result = await analyze_photo_image(image_id)
                    if result.get("status") == "review":
                        review += 1
                    elif result.get("status") == "completed":
                        ok += 1
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
            f"確認待ち **{review}枚** / 失敗 **{failed}枚**"
        )
        if errors:
            text += "\n" + "\n".join(errors)
        await interaction.followup.send(text[:1900], ephemeral=True)


def make_admin_category_view() -> discord.ui.View:
    return CategoryAdminView()
