import os
from contextlib import closing
from typing import Any

import discord
from discord.ext import commands

from photo_database import (
    get_all_people,
    get_connection,
    get_image_people,
    get_pending_person_reviews,
    set_confirmed_image_people,
    utc_now_text,
)


# =========================
# 表示設定
# =========================

REVIEW_EMBED_COLOR = discord.Color.blue()
SUCCESS_EMBED_COLOR = discord.Color.green()
SKIP_EMBED_COLOR = discord.Color.orange()
ERROR_EMBED_COLOR = discord.Color.red()

MAX_SELECT_OPTIONS = 25
MAX_CANDIDATE_DISPLAY = 10


# =========================
# 共通処理
# =========================

def normalize_text(
    value: Any,
) -> str:
    """
    値を安全な文字列へ変換する。
    """

    if value is None:
        return ""

    return str(value).strip()


def split_person_names(
    value: Any,
) -> list[str]:
    """
    「、」「,」「改行」区切りの人物名を
    重複を除いて一覧へ変換する。
    """

    text = normalize_text(value)

    if not text:
        return []

    normalized = (
        text.replace("\n", "、")
        .replace(",", "、")
        .replace("，", "、")
    )

    names: list[str] = []

    for item in normalized.split("、"):

        name = item.strip()

        if (
            name
            and name not in names
        ):
            names.append(name)

    return names


def truncate_text(
    value: Any,
    max_length: int,
) -> str:
    """
    Discordの文字数制限に収まるように短縮する。
    """

    text = normalize_text(value)

    if len(text) <= max_length:
        return text

    return (
        text[: max_length - 1]
        + "…"
    )


def get_reviewer_name(
    user: discord.abc.User,
) -> str:
    """
    確認者としてDBへ保存する文字列を返す。
    """

    display_name = normalize_text(
        getattr(
            user,
            "display_name",
            "",
        )
    )

    if not display_name:

        display_name = normalize_text(
            getattr(
                user,
                "name",
                "",
            )
        )

    return (
        f"{display_name} "
        f"({user.id})"
    ).strip()


def build_candidate_names(
    review: dict[str, Any],
) -> list[str]:
    """
    レビュー情報から人物候補一覧を作成する。
    """

    candidates: list[str] = []

    source_values = [
        review.get(
            "candidate_people",
            "",
        ),
        review.get(
            "ai_person_name",
            "",
        ),
        review.get(
            "candidates",
            "",
        ),
    ]

    for source_value in source_values:

        for name in split_person_names(
            source_value
        ):

            if name not in candidates:
                candidates.append(name)

    return candidates


def mark_person_review_skipped(
    image_id: int,
    reviewed_by: str = "",
    note: str = "",
) -> None:
    """
    人物確認をスキップ状態にする。

    画像情報や候補情報は削除せず、
    photo_review_queueの状態だけを変更する。
    """

    now = utc_now_text()

    with closing(
        get_connection()
    ) as connection:

        connection.execute(
            """
            UPDATE photo_review_queue

            SET
                status = 'skipped',
                reviewed_by = ?,
                selected_value = '',
                review_note = ?,
                reviewed_at = ?,
                updated_at = ?

            WHERE image_id = ?
            """,
            (
                reviewed_by,
                note,
                now,
                now,
                image_id,
            ),
        )

        connection.commit()


def build_review_embed(
    review: dict[str, Any],
) -> discord.Embed:
    """
    人物確認用Embedを作成する。
    """

    image_id = int(
        review.get(
            "image_id",
            0,
        )
    )

    group_name = normalize_text(
        review.get(
            "group_name",
            "",
        )
    )

    member_name = normalize_text(
        review.get(
            "member_name",
            "",
        )
    )

    title = normalize_text(
        review.get(
            "title",
            "",
        )
    )

    published_at = normalize_text(
        review.get(
            "published_at",
            "",
        )
    )

    blog_url = normalize_text(
        review.get(
            "blog_url",
            "",
        )
    )

    candidate_names = build_candidate_names(
        review
    )

    confirmed_names = split_person_names(
        review.get(
            "confirmed_people",
            "",
        )
    )

    embed = discord.Embed(
        title="🖼️ 写真の人物確認",
        description=(
            "写真に写っている人物を確認してください。\n"
            "AIやブログ投稿者の情報は、"
            "あくまで候補として表示しています。"
        ),
        color=REVIEW_EMBED_COLOR,
    )

    embed.add_field(
        name="画像ID",
        value=str(image_id),
        inline=True,
    )

    embed.add_field(
        name="グループ",
        value=group_name or "不明",
        inline=True,
    )

    embed.add_field(
        name="ブログ投稿者",
        value=member_name or "不明",
        inline=True,
    )

    embed.add_field(
        name="タイトル",
        value=(
            truncate_text(
                title,
                1000,
            )
            or "タイトルなし"
        ),
        inline=False,
    )

    if published_at:

        embed.add_field(
            name="投稿日",
            value=truncate_text(
                published_at,
                1000,
            ),
            inline=False,
        )

    if candidate_names:

        candidate_lines = []

        for index, name in enumerate(
            candidate_names[
                :MAX_CANDIDATE_DISPLAY
            ],
            start=1,
        ):

            candidate_lines.append(
                f"{index}. {name}"
            )

        if (
            len(candidate_names)
            > MAX_CANDIDATE_DISPLAY
        ):

            remaining = (
                len(candidate_names)
                - MAX_CANDIDATE_DISPLAY
            )

            candidate_lines.append(
                f"ほか {remaining}件"
            )

        candidate_text = "\n".join(
            candidate_lines
        )

    else:

        candidate_text = (
            "候補はありません。"
            "「人物を選ぶ」または「手入力」を使用してください。"
        )

    embed.add_field(
        name="🤖 人物候補",
        value=truncate_text(
            candidate_text,
            1000,
        ),
        inline=False,
    )

    embed.add_field(
        name="現在の確定人物",
        value=(
            "、".join(
                confirmed_names
            )
            if confirmed_names
            else "未確定"
        ),
        inline=False,
    )

    if blog_url:

        embed.add_field(
            name="ブログ",
            value=f"[元のブログを開く]({blog_url})",
            inline=False,
        )

    embed.set_footer(
        text=(
            "候補を採用・人物一覧から選択・"
            "手入力・スキップができます。"
        )
    )

    return embed


def build_completed_embed(
    review: dict[str, Any],
    person_names: list[str],
    reviewer: discord.abc.User,
) -> discord.Embed:
    """
    人物確定後のEmbedを作成する。
    """

    image_id = int(
        review.get(
            "image_id",
            0,
        )
    )

    embed = discord.Embed(
        title="✅ 人物確認完了",
        description=(
            "写真に写っている人物を確定しました。"
        ),
        color=SUCCESS_EMBED_COLOR,
    )

    embed.add_field(
        name="画像ID",
        value=str(image_id),
        inline=True,
    )

    embed.add_field(
        name="確定人物",
        value=(
            "、".join(person_names)
            if person_names
            else "人物なし"
        ),
        inline=False,
    )

    embed.add_field(
        name="確認者",
        value=discord.utils.escape_markdown(
            normalize_text(
                getattr(
                    reviewer,
                    "display_name",
                    reviewer.name,
                )
            )
        ),
        inline=False,
    )

    return embed


def build_skipped_embed(
    review: dict[str, Any],
    reviewer: discord.abc.User,
) -> discord.Embed:
    """
    スキップ後のEmbedを作成する。
    """

    image_id = int(
        review.get(
            "image_id",
            0,
        )
    )

    embed = discord.Embed(
        title="⏭️ 人物確認をスキップしました",
        description=(
            "この画像はスキップ状態になりました。\n"
            "候補や画像情報は削除されていません。"
        ),
        color=SKIP_EMBED_COLOR,
    )

    embed.add_field(
        name="画像ID",
        value=str(image_id),
        inline=True,
    )

    embed.add_field(
        name="操作した人",
        value=discord.utils.escape_markdown(
            normalize_text(
                getattr(
                    reviewer,
                    "display_name",
                    reviewer.name,
                )
            )
        ),
        inline=False,
    )

    return embed


async def disable_view_message(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
) -> None:
    """
    操作完了後にボタンを無効化して
    メッセージを更新する。
    """

    view = interaction.message.components

    del view

    try:

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

    except discord.InteractionResponded:

        await interaction.edit_original_response(
            embed=embed,
            view=None,
        )


# =========================
# 手入力モーダル
# =========================

class PersonInputModal(
    discord.ui.Modal,
):
    """
    人物名を直接入力するモーダル。
    """

    person_names = discord.ui.TextInput(
        label="人物名",
        placeholder=(
            "例: 菅原咲月\n"
            "複数人は「、」で区切ってください。"
        ),
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    note = discord.ui.TextInput(
        label="メモ",
        placeholder="任意",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
    )

    def __init__(
        self,
        review: dict[str, Any],
    ) -> None:

        super().__init__(
            title="人物名を手入力",
            timeout=300,
        )

        self.review = review

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:

        person_names = split_person_names(
            self.person_names.value
        )

        if not person_names:

            await interaction.response.send_message(
                "人物名を入力してください。",
                ephemeral=True,
            )

            return

        image_id = int(
            self.review["image_id"]
        )

        reviewer = get_reviewer_name(
            interaction.user
        )

        try:

            await asyncio_to_thread(
                set_confirmed_image_people,
                image_id,
                person_names,
                confirmed_by=reviewer,
                note=normalize_text(
                    self.note.value
                ),
            )

        except Exception as error:

            await interaction.response.send_message(
                (
                    "人物情報の保存に失敗しました。\n"
                    f"`{type(error).__name__}: "
                    f"{truncate_text(error, 1500)}`"
                ),
                ephemeral=True,
            )

            return

        embed = build_completed_embed(
            self.review,
            person_names,
            interaction.user,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:

        message = (
            "手入力処理中にエラーが発生しました。\n"
            f"`{type(error).__name__}: "
            f"{truncate_text(error, 1500)}`"
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True,
            )


# =========================
# 人物選択メニュー
# =========================

class PersonSelect(
    discord.ui.Select,
):
    """
    人物マスターから人物を選択するメニュー。
    """

    def __init__(
        self,
        review: dict[str, Any],
        people: list[dict[str, Any]],
        page: int = 0,
    ) -> None:

        self.review = review
        self.people = people
        self.page = max(
            int(page),
            0,
        )

        start = (
            self.page
            * MAX_SELECT_OPTIONS
        )

        end = (
            start
            + MAX_SELECT_OPTIONS
        )

        page_people = people[
            start:end
        ]

        options: list[
            discord.SelectOption
        ] = []

        for person in page_people:

            person_id = int(
                person["id"]
            )

            person_name = normalize_text(
                person.get(
                    "person_name",
                    "",
                )
            )

            group_name = normalize_text(
                person.get(
                    "group_name",
                    "",
                )
            )

            generation_name = normalize_text(
                person.get(
                    "generation_name",
                    "",
                )
            )

            description_parts = [
                value
                for value in (
                    group_name,
                    generation_name,
                )
                if value
            ]

            description = (
                " / ".join(
                    description_parts
                )
                or "グループ情報なし"
            )

            options.append(
                discord.SelectOption(
                    label=truncate_text(
                        person_name,
                        100,
                    ),
                    value=str(person_id),
                    description=truncate_text(
                        description,
                        100,
                    ),
                )
            )

        super().__init__(
            placeholder="人物を選択してください",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:

        try:
            selected_person_id = int(
                self.values[0]
            )

        except (
            TypeError,
            ValueError,
            IndexError,
        ):

            await interaction.response.send_message(
                "人物の選択情報が不正です。",
                ephemeral=True,
            )

            return

        selected_person = next(
            (
                person
                for person in self.people
                if int(person["id"])
                == selected_person_id
            ),
            None,
        )

        if selected_person is None:

            await interaction.response.send_message(
                "選択された人物が見つかりません。",
                ephemeral=True,
            )

            return

        person_name = normalize_text(
            selected_person.get(
                "person_name",
                "",
            )
        )

        image_id = int(
            self.review["image_id"]
        )

        reviewer = get_reviewer_name(
            interaction.user
        )

        try:

            await asyncio_to_thread(
                set_confirmed_image_people,
                image_id,
                [person_name],
                confirmed_by=reviewer,
                note="人物一覧から選択",
            )

        except Exception as error:

            await interaction.response.send_message(
                (
                    "人物情報の保存に失敗しました。\n"
                    f"`{type(error).__name__}: "
                    f"{truncate_text(error, 1500)}`"
                ),
                ephemeral=True,
            )

            return

        embed = build_completed_embed(
            self.review,
            [person_name],
            interaction.user,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )


class PersonSelectView(
    discord.ui.View,
):
    """
    人物選択メニューを表示するView。
    """

    def __init__(
        self,
        review: dict[str, Any],
        people: list[dict[str, Any]],
        page: int = 0,
    ) -> None:

        super().__init__(
            timeout=300,
        )

        self.review = review
        self.people = people
        self.page = max(
            int(page),
            0,
        )

        self.total_pages = max(
            (
                len(people)
                + MAX_SELECT_OPTIONS
                - 1
            )
            // MAX_SELECT_OPTIONS,
            1,
        )

        self.add_item(
            PersonSelect(
                review=review,
                people=people,
                page=self.page,
            )
        )

        self.previous_page.disabled = (
            self.page <= 0
        )

        self.next_page.disabled = (
            self.page
            >= self.total_pages - 1
        )

    @discord.ui.button(
        label="前のページ",
        style=discord.ButtonStyle.secondary,
        emoji="⬅️",
        row=1,
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        new_page = max(
            self.page - 1,
            0,
        )

        new_view = PersonSelectView(
            review=self.review,
            people=self.people,
            page=new_page,
        )

        await interaction.response.edit_message(
            content=(
                "人物を選択してください。\n"
                f"ページ {new_page + 1}"
                f" / {new_view.total_pages}"
            ),
            view=new_view,
        )

    @discord.ui.button(
        label="次のページ",
        style=discord.ButtonStyle.secondary,
        emoji="➡️",
        row=1,
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        new_page = min(
            self.page + 1,
            self.total_pages - 1,
        )

        new_view = PersonSelectView(
            review=self.review,
            people=self.people,
            page=new_page,
        )

        await interaction.response.edit_message(
            content=(
                "人物を選択してください。\n"
                f"ページ {new_page + 1}"
                f" / {new_view.total_pages}"
            ),
            view=new_view,
        )

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.danger,
        emoji="✖️",
        row=1,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        await interaction.response.edit_message(
            content="人物選択をキャンセルしました。",
            view=None,
        )


# =========================
# メインレビューView
# =========================

class PersonReviewView(
    discord.ui.View,
):
    """
    写真の人物確認を行うメインView。
    """

    def __init__(
        self,
        review: dict[str, Any],
        *,
        timeout: float = 600,
    ) -> None:

        super().__init__(
            timeout=timeout,
        )

        self.review = review
        self.image_id = int(
            review["image_id"]
        )

        self.candidate_names = (
            build_candidate_names(
                review
            )
        )

        self.accept_candidate.disabled = (
            not self.candidate_names
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """
        Botによる操作などを拒否する。
        """

        if interaction.user.bot:

            await interaction.response.send_message(
                "Botからは操作できません。",
                ephemeral=True,
            )

            return False

        return True

    @discord.ui.button(
        label="候補を採用",
        style=discord.ButtonStyle.success,
        emoji="✅",
        row=0,
    )
    async def accept_candidate(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        if not self.candidate_names:

            await interaction.response.send_message(
                "採用できる人物候補がありません。",
                ephemeral=True,
            )

            return

        # 最上位候補を採用する。
        selected_names = [
            self.candidate_names[0]
        ]

        reviewer = get_reviewer_name(
            interaction.user
        )

        try:

            await asyncio_to_thread(
                set_confirmed_image_people,
                self.image_id,
                selected_names,
                confirmed_by=reviewer,
                note="表示された最上位候補を採用",
            )

        except Exception as error:

            await interaction.response.send_message(
                (
                    "人物情報の保存に失敗しました。\n"
                    f"`{type(error).__name__}: "
                    f"{truncate_text(error, 1500)}`"
                ),
                ephemeral=True,
            )

            return

        embed = build_completed_embed(
            self.review,
            selected_names,
            interaction.user,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

    @discord.ui.button(
        label="人物を選ぶ",
        style=discord.ButtonStyle.primary,
        emoji="👤",
        row=0,
    )
    async def select_person(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        try:

            people = await asyncio_to_thread(
                get_all_people,
                True,
            )

        except Exception as error:

            await interaction.response.send_message(
                (
                    "人物一覧の取得に失敗しました。\n"
                    f"`{type(error).__name__}: "
                    f"{truncate_text(error, 1500)}`"
                ),
                ephemeral=True,
            )

            return

        if not people:

            await interaction.response.send_message(
                (
                    "人物マスターが空です。\n"
                    "「手入力」を使用してください。"
                ),
                ephemeral=True,
            )

            return

        select_view = PersonSelectView(
            review=self.review,
            people=people,
            page=0,
        )

        await interaction.response.send_message(
            (
                "人物を選択してください。\n"
                f"ページ 1 / {select_view.total_pages}"
            ),
            view=select_view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="手入力",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        row=0,
    )
    async def manual_input(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        modal = PersonInputModal(
            review=self.review
        )

        await interaction.response.send_modal(
            modal
        )

    @discord.ui.button(
        label="スキップ",
        style=discord.ButtonStyle.danger,
        emoji="⏭️",
        row=0,
    )
    async def skip_review(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        del button

        reviewer = get_reviewer_name(
            interaction.user
        )

        try:

            await asyncio_to_thread(
                mark_person_review_skipped,
                self.image_id,
                reviewed_by=reviewer,
                note="Discordレビュー画面でスキップ",
            )

        except Exception as error:

            await interaction.response.send_message(
                (
                    "スキップ状態の保存に失敗しました。\n"
                    f"`{type(error).__name__}: "
                    f"{truncate_text(error, 1500)}`"
                ),
                ephemeral=True,
            )

            return

        embed = build_skipped_embed(
            self.review,
            interaction.user,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

    async def on_timeout(
        self,
    ) -> None:
        """
        タイムアウト後にボタンを無効化する。
        """

        for item in self.children:

            if hasattr(
                item,
                "disabled",
            ):
                item.disabled = True


# =========================
# 非同期ヘルパー
# =========================

async def asyncio_to_thread(
    function: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    同期DB処理をDiscordのイベントループ外で実行する。
    """

    import asyncio

    return await asyncio.to_thread(
        function,
        *args,
        **kwargs,
    )


# =========================
# レビュー送信
# =========================

async def send_person_review(
    destination: (
        commands.Context
        | discord.Interaction
        | discord.abc.Messageable
    ),
    review: dict[str, Any],
) -> discord.Message | None:
    """
    指定されたレビュー項目をDiscordへ送信する。

    local_pathが存在する場合は画像を添付する。
    存在しない場合はimage_urlをEmbedに設定する。
    """

    embed = build_review_embed(
        review
    )

    view = PersonReviewView(
        review
    )

    local_path = normalize_text(
        review.get(
            "local_path",
            "",
        )
    )

    image_url = normalize_text(
        review.get(
            "image_url",
            "",
        )
    )

    discord_file: discord.File | None = None

    if (
        local_path
        and os.path.isfile(local_path)
    ):

        file_name = os.path.basename(
            local_path
        )

        if not file_name:

            file_name = (
                f"photo_{review['image_id']}.jpg"
            )

        discord_file = discord.File(
            local_path,
            filename=file_name,
        )

        embed.set_image(
            url=f"attachment://{file_name}"
        )

    elif image_url:

        embed.set_image(
            url=image_url
        )

    try:

        if isinstance(
            destination,
            discord.Interaction,
        ):

            kwargs: dict[str, Any] = {
                "embed": embed,
                "view": view,
            }

            if discord_file is not None:
                kwargs["file"] = discord_file

            if destination.response.is_done():

                message = (
                    await destination.followup.send(
                        **kwargs,
                        wait=True,
                    )
                )

            else:

                await destination.response.send_message(
                    **kwargs
                )

                message = (
                    await destination.original_response()
                )

            return message

        kwargs = {
            "embed": embed,
            "view": view,
        }

        if discord_file is not None:
            kwargs["file"] = discord_file

        return await destination.send(
            **kwargs
        )

    except Exception:

        if discord_file is not None:

            try:
                discord_file.close()

            except Exception:
                pass

        raise


async def send_next_person_review(
    destination: (
        commands.Context
        | discord.Interaction
        | discord.abc.Messageable
    ),
) -> dict[str, Any] | None:
    """
    最も古い人物確認待ちを1件取得して送信する。

    確認待ちがなければ案内メッセージを送信する。
    """

    reviews = await asyncio_to_thread(
        get_pending_person_reviews,
        1,
    )

    if not reviews:

        message = (
            "✅ 人物確認待ちの写真はありません。"
        )

        if isinstance(
            destination,
            discord.Interaction,
        ):

            if destination.response.is_done():

                await destination.followup.send(
                    message,
                    ephemeral=True,
                )

            else:

                await destination.response.send_message(
                    message,
                    ephemeral=True,
                )

        else:

            await destination.send(
                message
            )

        return None

    review = reviews[0]

    await send_person_review(
        destination,
        review,
    )

    return review


async def send_person_review_batch(
    destination: (
        commands.Context
        | discord.Interaction
        | discord.abc.Messageable
    ),
    limit: int = 5,
) -> int:
    """
    人物確認待ちを複数件送信する。

    Discordのメッセージ過多を防ぐため、
    最大10件に制限する。
    """

    safe_limit = max(
        1,
        min(
            int(limit),
            10,
        ),
    )

    reviews = await asyncio_to_thread(
        get_pending_person_reviews,
        safe_limit,
    )

    if not reviews:

        message = (
            "✅ 人物確認待ちの写真はありません。"
        )

        if isinstance(
            destination,
            discord.Interaction,
        ):

            if destination.response.is_done():

                await destination.followup.send(
                    message,
                    ephemeral=True,
                )

            else:

                await destination.response.send_message(
                    message,
                    ephemeral=True,
                )

        else:

            await destination.send(
                message
            )

        return 0

    if isinstance(
        destination,
        discord.Interaction,
    ):

        if not destination.response.is_done():

            await destination.response.defer()

    sent_count = 0

    for review in reviews:

        await send_person_review(
            destination,
            review,
        )

        sent_count += 1

    return sent_count
