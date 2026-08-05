"""Discord常設コントロールパネル。

一般ユーザー用と管理者用の2種類を提供する。
既存のprefix commandをそのまま呼び出すため、機能の二重実装を避ける。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable

LOGGER = logging.getLogger(__name__)
PANEL_MARKER = "photo-archive-control-panel-v2"


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    """整数の環境変数を安全に読み込む。誤設定でもBot起動を止めない。"""
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else int(default)
    except ValueError:
        LOGGER.warning("%s=%r は整数ではないため既定値 %s を使用します。", name, raw, default)
        value = int(default)
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


PANEL_HISTORY_SCAN_LIMIT = _env_int("PHOTO_PANEL_HISTORY_SCAN_LIMIT", 200, minimum=1, maximum=1000)
USER_PANEL_COOLDOWN_SECONDS = 1.5

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

from community_features import FeedbackModal
from user_experience import HelpHomeView, PersonProfileModal, help_home_embed
from runtime_guard import user_operation

ADMIN_ROLE_ID = _env_int("PHOTO_BOT_ADMIN_ROLE_ID", 0, minimum=0)
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


class EphemeralPanelContext(commands.Context):
    """パネル経由の ``ctx.send`` を本人だけに見える応答へ変換する。

    既存のprefix command側を個別に書き換えず、検索結果・画像・Embed・Viewを
    すべてInteractionのフォローアップとして送信する。これにより、一般ユーザーが
    何人操作しても公開チャンネルに操作結果が積み重ならない。
    """

    async def send(self, content: str | None = None, **kwargs):  # type: ignore[override]
        interaction = self.interaction
        if interaction is None:
            return await super().send(content, **kwargs)

        # Webhookのfollowup.sendではprefix command向けの返信指定を使えないため除外する。
        kwargs.pop("reference", None)
        kwargs.pop("mention_author", None)
        kwargs.pop("stickers", None)

        # パネル経由では呼び出し側の指定にかかわらず、必ず本人だけに表示する。
        kwargs["ephemeral"] = True
        kwargs["wait"] = True
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())

        if not interaction.response.is_done():
            response_kwargs = dict(kwargs)
            response_kwargs.pop("wait", None)
            await interaction.response.send_message(content, **response_kwargs)
            message = await interaction.original_response()
        else:
            message = await interaction.followup.send(content, **kwargs)

        delete_after = kwargs.get("delete_after")
        if delete_after is not None and message is not None:
            # followup.send側でもdelete_afterが処理されるが、戻り値互換を保つため
            # Messageをそのまま返す。
            return message
        return message


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

    ctx = EphemeralPanelContext(
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
        if admin_required:
            await command.invoke(ctx)
        else:
            try:
                async with user_operation(interaction.user.id):
                    await command.invoke(ctx)
            except RuntimeError as exc:
                await _reply(interaction, f"⏳ {exc}")
    except commands.CommandError as error:
        ctx.command_failed = True
        await bot.on_command_error(ctx, error)
    except Exception:
        ctx.command_failed = True
        LOGGER.exception("一般／管理パネルからのコマンド実行中に予期しないエラーが発生しました: %s", command_name)
        await _reply(
            interaction,
            "⚠️ 操作中に予期しないエラーが発生しました。時間を置いてもう一度お試しください。",
        )


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

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("入力フォーム処理中にエラーが発生しました", exc_info=error)
        await _reply(
            interaction,
            "⚠️ 入力内容の処理中にエラーが発生しました。時間を置いてもう一度お試しください。",
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

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("管理コマンド入力フォームでエラーが発生しました", exc_info=error)
        await _reply(interaction, "⚠️ 管理コマンドの処理中にエラーが発生しました。")


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


class CombinedSearchModal(discord.ui.Modal):
    def __init__(self, *, source: str, person_only: bool = False) -> None:
        title = "人物で写真検索" if person_only else "写真検索"
        super().__init__(title=title, timeout=300)
        self.source = source
        self.person_only = person_only
        self.query_input = discord.ui.TextInput(
            label="人物名" if person_only else "検索語",
            placeholder="例: 山口陽世" if person_only else "人物名・投稿者・タイトル・キャプションなど",
            required=True,
            max_length=100,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from combined_photo_search import send_combined_search
        await send_combined_search(interaction, self.source, str(self.query_input.value))


class PhotoSourceSelectView(discord.ui.View):
    def __init__(self, *, owner_id: int, person_only: bool = False) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.person_only = person_only

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _reply(interaction, "この選択画面は操作した本人専用です。")
            return False
        return True

    async def _open(self, interaction: discord.Interaction, source: str) -> None:
        await interaction.response.send_modal(
            CombinedSearchModal(source=source, person_only=self.person_only)
        )

    @discord.ui.button(label="ブログ", emoji="📚", style=discord.ButtonStyle.primary)
    async def blog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "blog")

    @discord.ui.button(label="Instagram", emoji="📸", style=discord.ButtonStyle.primary)
    async def instagram(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "instagram")

    @discord.ui.button(label="両方", emoji="🔎", style=discord.ButtonStyle.success)
    async def all_sources(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "all")


async def send_source_selector(interaction: discord.Interaction, *, person_only: bool = False) -> None:
    text = "検索対象を選んでください。結果は操作した本人だけに表示されます。"
    await interaction.response.send_message(
        text,
        view=PhotoSourceSelectView(owner_id=interaction.user.id, person_only=person_only),
        ephemeral=True,
    )


class UserPanelView(discord.ui.View):
    _last_action_at: dict[int, float] = {}

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 連打で同じ重い検索が複数起動するのを防ぐ。
        now = time.monotonic()
        user_id = int(interaction.user.id)
        previous = self._last_action_at.get(user_id, 0.0)
        if now - previous < USER_PANEL_COOLDOWN_SECONDS:
            await _reply(interaction, "⏳ 操作が早すぎます。少し待ってからもう一度押してください。")
            return False
        self._last_action_at[user_id] = now

        # 古い利用履歴が無制限に残らないよう、ときどき掃除する。
        if len(self._last_action_at) > 2000:
            threshold = now - 300
            self._last_action_at = {
                key: value for key, value in self._last_action_at.items() if value >= threshold
            }
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        LOGGER.exception("一般ユーザーパネルの操作でエラーが発生しました", exc_info=error)
        await _reply(
            interaction,
            "⚠️ パネル操作中にエラーが発生しました。時間を置いてもう一度お試しください。",
        )

    @discord.ui.button(label="写真検索", emoji="🔍", style=discord.ButtonStyle.primary, custom_id="photo:user:search")
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await send_source_selector(interaction, person_only=False)

    @discord.ui.button(label="人物で探す", emoji="👤", style=discord.ButtonStyle.primary, custom_id="photo:user:person")
    async def person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await send_source_selector(interaction, person_only=True)

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
        await invoke_existing_command(interaction, "favorite_list", "100")

    @discord.ui.button(label="最近の写真", emoji="🕒", style=discord.ButtonStyle.secondary, custom_id="photo:user:recent")
    async def recent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "photo_recent", "10")

    @discord.ui.button(label="人物一覧", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="photo:user:people")
    async def people(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "person_list")

    @discord.ui.button(
        label="検索履歴",
        emoji="🕘",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:history",
    )
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "search_history", "15")

    @discord.ui.button(
        label="コレクション",
        emoji="📚",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:collections",
    )
    async def collections(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "collection_list")

    @discord.ui.button(
        label="人気写真",
        emoji="📈",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:popular",
    )
    async def popular(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "popular_photos", "10")

    @discord.ui.button(
        label="不具合・要望",
        emoji="📮",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:feedback",
    )
    async def feedback(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FeedbackModal())

    @discord.ui.button(
        label="おすすめ・探索",
        emoji="✨",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:explore",
    )
    async def explore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "user_explore")

    @discord.ui.button(
        label="最近見た",
        emoji="🕘",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:recently_viewed",
    )
    async def recently_viewed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "recently_viewed", "20")

    @discord.ui.button(
        label="人物プロフィール",
        emoji="👤",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:profile",
    )
    async def profile(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PersonProfileModal())

    @discord.ui.button(
        label="使い方",
        emoji="❓",
        style=discord.ButtonStyle.secondary,
        custom_id="photo:user:help",
    )
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=help_home_embed(),
            view=HelpHomeView(interaction.user.id),
            ephemeral=True,
        )



class AdminQuickView(discord.ui.View):
    """管理者クイックメニュー。Bot稼働中は時間制限なく操作できる。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if isinstance(bot, commands.Bot) and await is_panel_admin(bot, interaction.user):
            return True
        await _reply(interaction, "⚠️ このメニューは管理者専用です。")
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        LOGGER.exception("管理者クイックメニューの操作でエラーが発生しました", exc_info=error)
        await _reply(interaction, "⚠️ 管理操作中にエラーが発生しました。もう一度お試しください。")

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
        await invoke_existing_command(interaction, "face_review", "5", admin_required=True)

    @discord.ui.button(label="確認待ちレビュー", emoji="✅", style=discord.ButtonStyle.primary)
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "review_panel", "5", admin_required=True)

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

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        LOGGER.exception("管理者パネルの操作でエラーが発生しました", exc_info=error)
        await _reply(interaction, "⚠️ 管理者パネルの操作中にエラーが発生しました。もう一度お試しください。")

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

    @discord.ui.button(label="選択式管理", emoji="🧭", style=discord.ButtonStyle.success, custom_id="photo:admin:guided")
    async def guided(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from admin_workflow import make_admin_category_view
        await _reply(interaction, "🧭 管理カテゴリーを選択してください。", ephemeral=True)
        await interaction.followup.send("選択式管理メニュー", view=make_admin_category_view(), ephemeral=True)

    @discord.ui.button(label="報告・要望箱", emoji="📬", style=discord.ButtonStyle.secondary, custom_id="photo:admin:feedback")
    async def feedback_admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "feedback_admin", admin_required=True)

    @discord.ui.button(label="運営・AI", emoji="🧠", style=discord.ButtonStyle.secondary, custom_id="photo:admin:operations")
    async def operations(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await invoke_existing_command(interaction, "operations_dashboard", admin_required=True)

    @discord.ui.button(label="全コマンド", emoji="⌨️", style=discord.ButtonStyle.danger, custom_id="photo:admin:all_commands")
    async def all_commands(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminCommandModal())


def _is_control_panel_message(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    """このBotが送信した写真アーカイブ用パネルかを判定する。"""
    if bot_user is None or message.author.id != bot_user.id:
        return False

    for embed in message.embeds:
        footer_text = str(getattr(embed.footer, "text", "") or "")
        if footer_text == PANEL_MARKER:
            return True

        # 旧版パネルにも対応し、初回更新時に重複を解消する。
        if embed.title in {"📸 写真アーカイブBot", "📷 写真検索パネル", "👑 管理者パネル"}:
            return True

    return False


async def remove_existing_control_panels(
    channel: discord.abc.Messageable,
    bot_user: discord.ClientUser | None,
    *,
    scan_limit: int = PANEL_HISTORY_SCAN_LIMIT,
) -> tuple[int, int]:
    """直近の履歴から既存パネルを削除する。

    戻り値は ``(削除数, 削除失敗数)``。履歴取得に対応しないMessageableでは
    何もせず ``(0, 0)`` を返す。
    """
    history = getattr(channel, "history", None)
    if history is None:
        return 0, 0

    deleted = 0
    failed = 0
    try:
        async for message in history(limit=max(1, int(scan_limit))):
            if not _is_control_panel_message(message, bot_user):
                continue
            try:
                await message.delete()
                deleted += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
    except (discord.Forbidden, discord.HTTPException):
        failed += 1

    return deleted, failed


async def send_control_panels(channel: discord.abc.Messageable) -> list[discord.Message]:
    """一般用・管理者用パネルを送信し、送信したMessageを返す。

    2枚目の送信だけ失敗した場合は、先に送信した1枚目を削除して
    中途半端なパネル構成を残さない。
    """
    sent_messages: list[discord.Message] = []
    try:
        user_embed = discord.Embed(
            title="📷 写真検索パネル",
            description=(
                "下のボタンから写真を検索できます。\n"
                "検索結果や操作結果は、操作した本人にだけ表示されます。\n"
                "お気に入りはユーザーごとに保存されます。\n"
                "⚠️画像を表示するまでに多少時間がかかる場合があります。"
            ),
            color=0x3498DB,
        )
        user_embed.add_field(
            name="表示について",
            value="検索結果は公開チャンネルには投稿されません。各操作は本人専用画面で進みます。",
            inline=False,
        )
        sent_messages.append(await channel.send(
            embed=user_embed,
            view=UserPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        ))

        admin_embed = discord.Embed(
            title="👑 管理者パネル",
            description=(
                f"Bot所有者、または **{ADMIN_ROLE_NAME}** ロール専用です。\n"
                "「選択式管理」では用途別に操作でき、\n"
                "「全コマンド」では既存コマンドを先頭の `!` なしで実行できます。\n"
                "この常設パネルは時間が経過しても操作できます。"
            ),
            color=0xE67E22,
        )
        admin_embed.set_footer(text=PANEL_MARKER)
        sent_messages.append(await channel.send(
            embed=admin_embed,
            view=AdminPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        ))
        return sent_messages
    except Exception:
        for message in sent_messages:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        raise


def register_control_panel(bot: commands.Bot) -> None:
    install_admin_role_owner_bridge(bot)

    @bot.command(name="panel_setup", aliases=["panel_refresh"])
    @commands.is_owner()
    @commands.guild_only()
    async def panel_setup_command(ctx: commands.Context) -> None:
        """現在のチャンネルの旧パネルを整理し、常設パネルを再設置する。"""
        if bot.user is None:
            await ctx.send("⚠️ Bot情報を取得できないため、パネルを設置できませんでした。")
            return

        deleted, failed = await remove_existing_control_panels(ctx.channel, bot.user)

        try:
            messages = await send_control_panels(ctx.channel)
        except discord.Forbidden:
            await ctx.send(
                "⚠️ パネルを設置できませんでした。\n"
                "このチャンネルで `メッセージを送信`・`埋め込みリンク`・"
                "`メッセージ履歴を読む` の権限を確認してください。"
            )
            return
        except discord.HTTPException as error:
            await ctx.send(f"⚠️ Discordへの送信に失敗しました。\n`{type(error).__name__}: {error}`")
            return

        status_lines = [
            "✅ 常設パネルを再設置しました。",
            f"🧹 旧パネル削除: {deleted}件",
            f"📌 新規パネル: {len(messages)}件",
        ]
        if failed:
            status_lines.append(f"⚠️ 削除できなかったパネル: {failed}件")
        await ctx.send("\n".join(status_lines), delete_after=20)

    @bot.command(name="panel_remove")
    @commands.is_owner()
    @commands.guild_only()
    async def panel_remove_command(ctx: commands.Context) -> None:
        """現在のチャンネルから、このBotの常設パネルを削除する。"""
        deleted, failed = await remove_existing_control_panels(ctx.channel, bot.user)
        if deleted == 0 and failed == 0:
            await ctx.send("ℹ️ 直近の履歴に削除対象の常設パネルはありませんでした。", delete_after=15)
            return

        text = f"🧹 常設パネルを {deleted}件削除しました。"
        if failed:
            text += f"\n⚠️ 削除できなかったパネル: {failed}件"
        await ctx.send(text, delete_after=15)

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
