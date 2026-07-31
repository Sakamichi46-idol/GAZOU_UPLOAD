"""Discord常設コントロールパネル。

一般ユーザー用と管理者用の2種類を提供する。
既存のprefix commandをそのまま呼び出すため、機能の二重実装を避ける。
"""

from __future__ import annotations

import os
import re
from typing import Iterable

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

ADMIN_ROLE_ID = int(os.getenv("PHOTO_BOT_ADMIN_ROLE_ID", "0") or 0)
ADMIN_ROLE_NAME = os.getenv("PHOTO_BOT_ADMIN_ROLE_NAME", "PhotoBot Admin").strip() or "PhotoBot Admin"


def _member_role_names(member: discord.abc.User) -> set[str]:
    roles = getattr(member, "roles", None) or []
    return {str(getattr(role, "name", "")) for role in roles}


def has_admin_role(user: discord.abc.User) -> bool:
    roles = getattr(user, "roles", None) or []
    if ADMIN_ROLE_ID and any(int(getattr(role, "id", 0)) == ADMIN_ROLE_ID for role in roles):
        return True
    return ADMIN_ROLE_NAME in _member_role_names(user)


async def is_panel_admin(bot: commands.Bot, user: discord.abc.User) -> bool:
    if has_admin_role(user):
        return True
    try:
        return bool(await bot.is_owner(user))
    except Exception:
        return False


def install_admin_role_owner_bridge(bot: commands.Bot) -> None:
    """既存の @commands.is_owner() を管理者ロールにも対応させる。"""
    if getattr(bot, "_photo_admin_owner_bridge", False):
        return

    original_is_owner = bot.is_owner

    async def bridged_is_owner(user: discord.abc.User) -> bool:
        if has_admin_role(user):
            return True
        return bool(await original_is_owner(user))

    bot.is_owner = bridged_is_owner  # type: ignore[method-assign]
    bot._photo_admin_owner_bridge = True  # type: ignore[attr-defined]


async def _reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


async def invoke_existing_command(
    interaction: discord.Interaction,
    command_name: str,
    raw_arguments: str = "",
    *,
    admin_required: bool = False,
) -> None:
    """ボタン／Modalから既存のprefix commandを安全に実行する。

    ``Context.from_interaction`` はスラッシュコマンド専用であり、
    ButtonやModalのInteractionにはcommand dataがないため使用しない。
    代わりに、Interactionを保持したContextを手動で組み立てて
    prefix command本来の引数変換・check・cooldownをそのまま利用する。
    """
    bot = interaction.client
    if not isinstance(bot, commands.Bot):
        await _reply(interaction, "⚠️ Botのコマンドシステムを取得できませんでした。")
        return

    if admin_required and not await is_panel_admin(bot, interaction.user):
        await _reply(interaction, "⚠️ この操作は管理者専用です。")
        return

    command = bot.get_command(command_name)
    if command is None:
        await _reply(interaction, f"⚠️ `{command_name}` コマンドが見つかりません。")
        return

    # ボタンInteractionの interaction.message は、パネルを送信したBot自身が
    # author になっている。そのMessageをそのままContextへ渡すと、ctx.authorも
    # Botになり、顔レビューなどの「開始者本人」判定が必ず失敗する。
    #
    # 元Messageの属性は可能な限り委譲しつつ、author/content/channel/guildだけを
    # Interaction実行者の情報で上書きしたMessage互換オブジェクトを使用する。
    class _PanelMessage:
        def __init__(self, original: object | None) -> None:
            self._original = original
            self.author = interaction.user
            self.channel = interaction.channel
            self.guild = interaction.guild
            self.content = f"!{command_name} {raw_arguments}".rstrip()
            self.attachments = list(getattr(original, "attachments", []) or [])
            self.id = int(getattr(interaction, "id", 0))
            self._state = getattr(bot, "_connection", None)

        def __getattr__(self, name: str):
            original = object.__getattribute__(self, "_original")
            if original is None:
                raise AttributeError(name)
            return getattr(original, name)

    message = _PanelMessage(interaction.message)

    ctx = commands.Context(
        message=message,
        bot=bot,
        view=StringView(raw_arguments.strip()),
        args=[],
        kwargs={},
        prefix="!",
        command=command,
        invoked_with=command_name,
        invoked_parents=[],
        invoked_subcommand=None,
        subcommand_passed=None,
        command_failed=False,
        interaction=interaction,
    )

    try:
        # 3秒以内に応答を確定させる。Context.sendは以後followupを利用できる。
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await command.invoke(ctx)
    except commands.CommandError as error:
        ctx.command_failed = True
        await bot.on_command_error(ctx, error)
    except Exception as error:
        ctx.command_failed = True
        await _reply(interaction, f"⚠️ 操作中にエラーが発生しました。\n`{type(error).__name__}: {error}`")


_IMAGE_ID_COMMANDS = {"photo_id", "favorite_add", "favorite_remove"}


def normalize_image_id_argument(value: str) -> str:
    """画像ID入力を既存コマンドが受け取れる数字文字列へ正規化する。

    対応例: ``100`` / ``ID 100`` / ``ID:100`` / ``画像ID：100``。
    数字が複数含まれる曖昧な入力は、そのまま返してコマンド側の
    引数エラーに任せる。
    """
    text = str(value or "").strip()
    match = re.fullmatch(
        r"(?i)\s*(?:(?:画像\s*)?id\s*[:：#]?\s*)?(\d+)\s*",
        text,
    )
    return match.group(1) if match else text


class CommandArgumentsModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        title: str,
        command_name: str,
        label: str,
        placeholder: str = "",
        required: bool = True,
        admin_required: bool = False,
    ) -> None:
        super().__init__(title=title, timeout=300)
        self.command_name = command_name
        self.admin_required = admin_required
        self.arguments = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            required=required,
            max_length=1000,
        )
        self.add_item(self.arguments)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        arguments = str(self.arguments.value)
        if self.command_name in _IMAGE_ID_COMMANDS:
            arguments = normalize_image_id_argument(arguments)

        await invoke_existing_command(
            interaction,
            self.command_name,
            arguments,
            admin_required=self.admin_required,
        )


class AdminCommandModal(discord.ui.Modal, title="管理コマンドを実行"):
    command_text = discord.ui.TextInput(
        label="コマンド名と引数（先頭の ! は不要）",
        placeholder="例: photo_archive_run 5 乃木坂46",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if not isinstance(bot, commands.Bot) or not await is_panel_admin(bot, interaction.user):
            await _reply(interaction, "⚠️ この操作は管理者専用です。")
            return

        text = str(self.command_text.value).strip()
        if text.startswith("!"):
            text = text[1:].lstrip()
        if not text:
            await _reply(interaction, "⚠️ コマンド名を入力してください。")
            return
        name, _, arguments = text.partition(" ")
        await invoke_existing_command(interaction, name, arguments, admin_required=True)


class FavoriteView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    @discord.ui.button(label="追加", emoji="⭐", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="お気に入り追加", command_name="favorite_add",
            label="画像ID", placeholder="例: 125 / ID 125", required=True,
        ))

    @discord.ui.button(label="削除", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="お気に入り削除", command_name="favorite_remove",
            label="画像ID", placeholder="例: 125 / ID 125", required=True,
        ))

    @discord.ui.button(label="一覧", emoji="📂", style=discord.ButtonStyle.primary)
    async def list_items(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "favorite_list")


class UserPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="写真検索", emoji="🔍", style=discord.ButtonStyle.primary, custom_id="photo:user:search")
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="写真検索", command_name="photo_search",
            label="検索語", placeholder="人物名・タグ・ブログタイトルなど", required=True,
        ))

    @discord.ui.button(label="人物で探す", emoji="👤", style=discord.ButtonStyle.primary, custom_id="photo:user:person")
    async def person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="人物で写真検索", command_name="person",
            label="人物名", placeholder="例: 井上和", required=True,
        ))

    @discord.ui.button(label="タグで探す", emoji="🏷️", style=discord.ButtonStyle.primary, custom_id="photo:user:tag")
    async def tag(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # DBに存在するカテゴリー → タグの順で選べる検索画面を開く。
        await invoke_existing_command(interaction, "photo_tags")

    @discord.ui.button(label="画像ID", emoji="🖼️", style=discord.ButtonStyle.secondary, custom_id="photo:user:id")
    async def image_id(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="画像IDから表示", command_name="photo_id",
            label="画像ID", placeholder="例: 125 / ID 125", required=True,
        ))

    @discord.ui.button(label="お気に入り", emoji="⭐", style=discord.ButtonStyle.success, custom_id="photo:user:favorites")
    async def favorites(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _reply(interaction, "⭐ お気に入り操作を選んでください。", ephemeral=True)
        await interaction.followup.send("お気に入りメニュー", view=FavoriteView(), ephemeral=True)

    @discord.ui.button(label="最近の写真", emoji="🕒", style=discord.ButtonStyle.secondary, custom_id="photo:user:recent")
    async def recent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "photo_recent", "10")

    @discord.ui.button(label="人物一覧", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="photo:user:people")
    async def people(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "person_list")


class AdminQuickView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if isinstance(bot, commands.Bot) and await is_panel_admin(bot, interaction.user):
            return True
        await _reply(interaction, "⚠️ このメニューは管理者専用です。")
        return False

    @discord.ui.button(label="写真巡回を1回", emoji="📷", style=discord.ButtonStyle.primary)
    async def photo_run(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="写真アーカイブを1回実行", command_name="photo_archive_run",
            label="上限件数とグループ（省略可）", placeholder="例: 5 乃木坂46",
            required=False, admin_required=True,
        ))

    @discord.ui.button(label="AI解析", emoji="🤖", style=discord.ButtonStyle.primary)
    async def ai(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="AI解析", command_name="ai_analyze", label="件数（省略可）",
            placeholder="例: 10", required=False, admin_required=True,
        ))

    @discord.ui.button(label="顔レビュー", emoji="👤", style=discord.ButtonStyle.primary)
    async def face_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "face_review", admin_required=True)

    @discord.ui.button(label="確認待ちレビュー", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "review_next", admin_required=True)

    @discord.ui.button(label="画像0件を再判定", emoji="🛠️", style=discord.ButtonStyle.success)
    async def repair_zero_images(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CommandArgumentsModal(
            title="画像0件の記事を再判定",
            command_name="photo_archive_repair_zero",
            label="上限件数とグループ（省略可）",
            placeholder="例: 100 櫻坂46",
            required=False,
            admin_required=True,
        ))

    @discord.ui.button(label="写真巡回停止", emoji="🛑", style=discord.ButtonStyle.danger)
    async def photo_stop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "photo_archive_stop", admin_required=True)


class AdminPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if isinstance(bot, commands.Bot) and await is_panel_admin(bot, interaction.user):
            return True
        await _reply(interaction, "⚠️ このパネルは管理者専用です。")
        return False

    @discord.ui.button(label="統合ステータス", emoji="📊", style=discord.ButtonStyle.primary, custom_id="photo:admin:status")
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "status", admin_required=True)

    @discord.ui.button(label="写真状況", emoji="📷", style=discord.ButtonStyle.primary, custom_id="photo:admin:photo_status")
    async def photo_status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "photo_archive_status", admin_required=True)

    @discord.ui.button(label="AI状況", emoji="🤖", style=discord.ButtonStyle.primary, custom_id="photo:admin:ai_status")
    async def ai_status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "ai_status", admin_required=True)

    @discord.ui.button(label="保存状況", emoji="💾", style=discord.ButtonStyle.secondary, custom_id="photo:admin:storage")
    async def storage(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "photo_storage", admin_required=True)

    @discord.ui.button(label="よく使う操作", emoji="🛠️", style=discord.ButtonStyle.success, custom_id="photo:admin:quick")
    async def quick(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _reply(interaction, "🛠️ 管理操作を選んでください。", ephemeral=True)
        await interaction.followup.send("管理クイックメニュー", view=AdminQuickView(), ephemeral=True)

    @discord.ui.button(label="全コマンド", emoji="⌨️", style=discord.ButtonStyle.danger, custom_id="photo:admin:all_commands")
    async def all_commands(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminCommandModal())


async def send_control_panels(channel: discord.abc.Messageable) -> None:
    user_embed = discord.Embed(
        title="📸 写真アーカイブBot",
        description=(
            "下のボタンから写真を検索できます。\n"
            "お気に入りはDiscordユーザーごとに保存されます。"
        ),
    )
    await channel.send(embed=user_embed, view=UserPanelView())

    admin_embed = discord.Embed(
        title="👑 管理者パネル",
        description=(
            f"Bot所有者、または **{ADMIN_ROLE_NAME}** ロール専用です。\n"
            "「全コマンド」では、既存コマンドを先頭の `!` なしで実行できます。"
        ),
    )
    await channel.send(embed=admin_embed, view=AdminPanelView())


def register_control_panel(bot: commands.Bot) -> None:
    install_admin_role_owner_bridge(bot)

    @bot.command(name="panel_setup")
    @commands.is_owner()
    async def panel_setup_command(ctx: commands.Context) -> None:
        """現在のチャンネルに一般用・管理者用の常設パネルを設置する。"""
        await send_control_panels(ctx.channel)
        await ctx.send("✅ 常設パネルを設置しました。古いパネルがある場合は手動で削除してください。")

    @bot.command(name="panel_admin_info")
    @commands.is_owner()
    async def panel_admin_info_command(ctx: commands.Context) -> None:
        role_text = f"ID `{ADMIN_ROLE_ID}`" if ADMIN_ROLE_ID else f"名前 **{ADMIN_ROLE_NAME}**"
        await ctx.send(
            "👑 **管理者判定設定**\n"
            f"対象ロール: {role_text}\n"
            "環境変数 `PHOTO_BOT_ADMIN_ROLE_ID` を設定すると、ロール名変更の影響を受けません。"
        )


def add_persistent_control_panel_views(bot: commands.Bot) -> None:
    if getattr(bot, "_photo_panel_views_added", False):
        return
    bot.add_view(UserPanelView())
    bot.add_view(AdminPanelView())
    bot._photo_panel_views_added = True  # type: ignore[attr-defined]
