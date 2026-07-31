"""ZIP42 管理者向け選択式ワークフロー。"""
from __future__ import annotations

import asyncio
import discord
from discord.ext import commands

from photo_ai_analyzer import analyze_photo_image
from local_face_recognition import detect_faces_for_image
from photo_database import (
    get_blog_authors_for_admin,
    get_blogs_for_admin,
    get_blog_image_ids,
)

GROUPS = ("乃木坂46", "櫻坂46", "日向坂46")

async def _admin(interaction: discord.Interaction) -> bool:
    from control_panel import is_panel_admin
    bot = interaction.client
    return isinstance(bot, commands.Bot) and await is_panel_admin(bot, interaction.user)

async def _deny(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        await interaction.followup.send("⚠️ 管理者専用です。", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ 管理者専用です。", ephemeral=True)

class ImageIdModal(discord.ui.Modal):
    def __init__(self, command_name: str, title: str):
        super().__init__(title=title, timeout=300)
        self.command_name = command_name
        self.image_id = discord.ui.TextInput(label="写真ID", placeholder="例: 125 / ID 125", max_length=30)
        self.add_item(self.image_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from control_panel import invoke_existing_command, normalize_image_id_argument
        await invoke_existing_command(interaction, self.command_name, normalize_image_id_argument(str(self.image_id.value)), admin_required=True)

class CategoryAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    async def interaction_check(self, interaction):
        if await _admin(interaction): return True
        await _deny(interaction); return False

    @discord.ui.button(label="写真管理", emoji="📷", style=discord.ButtonStyle.primary)
    async def photo(self, interaction, _):
        await interaction.response.send_message("📷 写真管理", view=PhotoAdminView(), ephemeral=True)

    @discord.ui.button(label="人物確認", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, interaction, _):
        await interaction.response.send_message("✅ 人物確認", view=ReviewAdminView(), ephemeral=True)

    @discord.ui.button(label="ブログ単位解析", emoji="📖", style=discord.ButtonStyle.success)
    async def blog(self, interaction, _):
        await interaction.response.send_message("📖 グループを選択してください。", view=GroupSelectView(), ephemeral=True)

    @discord.ui.button(label="タグ管理", emoji="🏷️", style=discord.ButtonStyle.secondary)
    async def tags(self, interaction, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "photo_tags", admin_required=True)

    @discord.ui.button(label="状態・修復", emoji="🛠️", style=discord.ButtonStyle.secondary)
    async def status(self, interaction, _):
        await interaction.response.send_message("🛠️ 状態・修復", view=StatusAdminView(), ephemeral=True)

class PhotoAdminView(discord.ui.View):
    def __init__(self): super().__init__(timeout=300)
    @discord.ui.button(label="写真IDを表示", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def show(self, i, _): await i.response.send_modal(ImageIdModal("photo_id", "写真IDを表示"))
    @discord.ui.button(label="人物を設定", emoji="👤", style=discord.ButtonStyle.success)
    async def person(self, i, _):
        from control_panel import CommandArgumentsModal
        await i.response.send_modal(CommandArgumentsModal(title="人物を設定", command_name="photo_person_set", label="写真IDと人物名", placeholder="125 井上和（複数はカンマ区切り）", admin_required=True))
    @discord.ui.button(label="タグを追加", emoji="🏷️", style=discord.ButtonStyle.success)
    async def tag(self, i, _):
        from control_panel import CommandArgumentsModal
        await i.response.send_modal(CommandArgumentsModal(title="手動タグ追加", command_name="tag_add", label="写真IDとタグ", placeholder="125 制服", admin_required=True))
    @discord.ui.button(label="AI再解析", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def analyze(self, i, _): await i.response.send_modal(ImageIdModal("ai_retry_id", "写真1枚をAI再解析"))
    @discord.ui.button(label="顔認証", emoji="🙂", style=discord.ButtonStyle.secondary)
    async def face(self, i, _): await i.response.send_modal(ImageIdModal("face_scan", "写真1枚を顔認証"))

class ReviewAdminView(discord.ui.View):
    def __init__(self): super().__init__(timeout=300)
    @discord.ui.button(label="人物確認を開始", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, i, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(i, "review_next", admin_required=True)
    @discord.ui.button(label="顔確認を開始", emoji="🙂", style=discord.ButtonStyle.primary)
    async def face(self, i, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(i, "face_review", admin_required=True)
    @discord.ui.button(label="AI推定人物で検索", emoji="🤖", style=discord.ButtonStyle.success)
    async def search(self, i, _):
        from control_panel import CommandArgumentsModal
        await i.response.send_modal(CommandArgumentsModal(title="確認済み＋AI推定人物検索", command_name="person", label="人物名", placeholder="例: 賀喜遥香", admin_required=True))

class StatusAdminView(discord.ui.View):
    def __init__(self): super().__init__(timeout=300)
    @discord.ui.button(label="統合ステータス", emoji="📊", style=discord.ButtonStyle.primary)
    async def status(self, i, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(i, "status", admin_required=True)
    @discord.ui.button(label="AI状況", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def ai(self, i, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(i, "ai_status", admin_required=True)
    @discord.ui.button(label="保存状況", emoji="💾", style=discord.ButtonStyle.secondary)
    async def storage(self, i, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(i, "photo_storage", admin_required=True)
    @discord.ui.button(label="画像0件を修復", emoji="🛠️", style=discord.ButtonStyle.success)
    async def repair(self, i, _):
        from control_panel import CommandArgumentsModal
        await i.response.send_modal(CommandArgumentsModal(title="画像0件の記事を修復", command_name="photo_archive_repair_zero", label="上限件数とグループ（省略可）", required=False, admin_required=True))

class GroupSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="グループを選択", options=[discord.SelectOption(label=x) for x in GROUPS])
    async def callback(self, interaction):
        await interaction.response.edit_message(content=f"📖 **{self.values[0]}** のブログ投稿者を選択してください。", view=AuthorSelectView(self.values[0]))

class GroupSelectView(discord.ui.View):
    def __init__(self): super().__init__(timeout=300); self.add_item(GroupSelect())

class AuthorSelect(discord.ui.Select):
    def __init__(self, group):
        self.group=group
        authors=get_blog_authors_for_admin(group,25)
        options=[discord.SelectOption(label=a['member_name'][:100], description=f"記事 {a['blog_count']}件") for a in authors]
        if not options: options=[discord.SelectOption(label="投稿者が見つかりません", value="__none__")]
        super().__init__(placeholder="ブログ投稿者を選択", options=options)
    async def callback(self, interaction):
        author=self.values[0]
        if author=='__none__': return await interaction.response.send_message("対象がありません。", ephemeral=True)
        await interaction.response.edit_message(content=f"📖 **{self.group} / {author}** の記事を選択してください。", view=BlogSelectView(self.group, author))

class AuthorSelectView(discord.ui.View):
    def __init__(self, group): super().__init__(timeout=300); self.add_item(AuthorSelect(group))

class BlogSelect(discord.ui.Select):
    def __init__(self, group, author):
        self.group,self.author=group,author
        blogs=get_blogs_for_admin(group,author,25)
        options=[]
        for b in blogs:
            title=(b.get('title') or '無題')[:75]
            desc=f"{b.get('published_at','')} / 画像{b.get('image_count',0)}枚"[:100]
            options.append(discord.SelectOption(label=title, description=desc, value=str(b['id'])))
        if not options: options=[discord.SelectOption(label="記事が見つかりません", value="0")]
        super().__init__(placeholder="記事を選択", options=options)
    async def callback(self, interaction):
        blog_id=int(self.values[0])
        if not blog_id: return await interaction.response.send_message("対象がありません。", ephemeral=True)
        await interaction.response.edit_message(content=f"📖 **{self.group} / {self.author}**\n実行する処理を選んでください。", view=BlogActionView(blog_id))

class BlogSelectView(discord.ui.View):
    def __init__(self, group, author): super().__init__(timeout=300); self.add_item(BlogSelect(group,author))

class BlogActionView(discord.ui.View):
    def __init__(self, blog_id): super().__init__(timeout=600); self.blog_id=int(blog_id)

    async def _run(self, interaction, mode, only_pending):
        await interaction.response.defer(ephemeral=True)
        ids=await asyncio.to_thread(get_blog_image_ids, self.blog_id, only_unanalyzed=(mode=='ai' and only_pending), only_unscanned=(mode=='face' and only_pending))
        if not ids:
            return await interaction.followup.send("✅ 対象画像はありません。", ephemeral=True)
        ok=review=failed=0
        errors=[]
        for image_id in ids:
            try:
                if mode=='ai':
                    result=await analyze_photo_image(image_id)
                    if result.get('status')=='review': review+=1
                    elif result.get('status')=='completed': ok+=1
                    else: failed+=1
                else:
                    await asyncio.to_thread(detect_faces_for_image,image_id)
                    ok+=1
            except Exception as e:
                failed+=1
                if len(errors)<3: errors.append(f"ID {image_id}: {type(e).__name__}: {e}")
        text=f"✅ 処理完了\n対象 **{len(ids)}枚** / 成功 **{ok}枚** / 確認待ち **{review}枚** / 失敗 **{failed}枚**"
        if errors: text += "\n" + "\n".join(errors)
        await interaction.followup.send(text[:1900], ephemeral=True)

    @discord.ui.button(label="未解析だけ人物判定", emoji="🤖", style=discord.ButtonStyle.primary)
    async def ai_pending(self,i,_): await self._run(i,'ai',True)
    @discord.ui.button(label="全画像を人物再判定", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def ai_all(self,i,_): await self._run(i,'ai',False)
    @discord.ui.button(label="未処理だけ顔認証", emoji="🙂", style=discord.ButtonStyle.primary)
    async def face_pending(self,i,_): await self._run(i,'face',True)
    @discord.ui.button(label="全画像を顔再認証", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def face_all(self,i,_): await self._run(i,'face',False)

def make_admin_category_view() -> discord.ui.View:
    return CategoryAdminView()
