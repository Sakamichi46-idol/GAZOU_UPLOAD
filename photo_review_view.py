from __future__ import annotations

import asyncio
import os
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

from photo_database import (
    get_connection,
    get_pending_person_reviews,
    save_person,
    set_confirmed_image_people,
    utc_now_text,
)
from sakamichi_members import SAKAMICHI_MEMBERS, iter_members

REVIEW_EMBED_COLOR = discord.Color.blue()
SUCCESS_EMBED_COLOR = discord.Color.green()
SKIP_EMBED_COLOR = discord.Color.orange()
MAX_CANDIDATE_DISPLAY = 10


def normalize_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def truncate_text(value: Any, max_length: int) -> str:
    text = normalize_text(value)
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def split_person_names(value: Any) -> list[str]:
    text = normalize_text(value).replace("\n", "、").replace(",", "、").replace("，", "、")
    result: list[str] = []
    for item in text.split("、"):
        name = item.strip()
        if name and name not in result:
            result.append(name)
    return result


def get_reviewer_name(user: discord.abc.User) -> str:
    name = normalize_text(getattr(user, "display_name", "")) or normalize_text(getattr(user, "name", ""))
    return f"{name} ({user.id})".strip()


def build_candidate_names(review: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("candidate_people", "ai_person_name", "candidates"):
        for name in split_person_names(review.get(key, "")):
            if name not in result:
                result.append(name)
    return result


def seed_member_master() -> None:
    for group_name, generation_name, person_name in iter_members():
        save_person(person_name, group_name, generation_name, True)


def mark_person_review_skipped(image_id: int, reviewed_by: str = "", note: str = "") -> None:
    now = utc_now_text()
    with closing(get_connection()) as connection:
        connection.execute(
            """UPDATE photo_review_queue
               SET status='skipped', reviewed_by=?, selected_value='', review_note=?,
                   reviewed_at=?, updated_at=? WHERE image_id=?""",
            (reviewed_by, note, now, now, image_id),
        )
        connection.commit()


def build_review_embed(review: dict[str, Any]) -> discord.Embed:
    candidates = build_candidate_names(review)
    confirmed = split_person_names(review.get("confirmed_people", ""))
    embed = discord.Embed(
        title="🖼️ 写真の人物確認",
        description="写真に写っている人物を確認してください。複数人の選択にも対応しています。",
        color=REVIEW_EMBED_COLOR,
    )
    embed.add_field(name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    embed.add_field(name="グループ", value=normalize_text(review.get("group_name")) or "不明", inline=True)
    embed.add_field(name="ブログ投稿者", value=normalize_text(review.get("member_name")) or "不明", inline=True)
    embed.add_field(name="タイトル", value=truncate_text(review.get("title"), 1000) or "タイトルなし", inline=False)
    if review.get("published_at"):
        embed.add_field(name="投稿日", value=truncate_text(review.get("published_at"), 1000), inline=False)
    candidate_text = "\n".join(f"{i}. {name}" for i, name in enumerate(candidates[:MAX_CANDIDATE_DISPLAY], 1))
    embed.add_field(name="🤖 人物候補", value=candidate_text or "候補はありません。", inline=False)
    embed.add_field(name="現在の確定人物", value="、".join(confirmed) if confirmed else "未確定", inline=False)
    if review.get("blog_url"):
        embed.add_field(name="ブログ", value=f"[元のブログを開く]({review['blog_url']})", inline=False)
    embed.set_footer(text="人物がいない写真は「人物なし」、写っているが判別できない場合は「人物不明」を押してください。")
    return embed


def build_completed_embed(review: dict[str, Any], names: list[str], reviewer: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="✅ 人物確認完了", description="写真に写っている人物を確定しました。", color=SUCCESS_EMBED_COLOR)
    embed.add_field(name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    embed.add_field(name="確定人物", value="、".join(names) if names else "人物なし", inline=False)
    embed.add_field(name="確認者", value=discord.utils.escape_markdown(normalize_text(getattr(reviewer, "display_name", reviewer.name))), inline=False)
    return embed


def build_skipped_embed(review: dict[str, Any], reviewer: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="⏭️ 人物確認をスキップしました", color=SKIP_EMBED_COLOR)
    embed.add_field(name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    embed.add_field(name="操作した人", value=normalize_text(getattr(reviewer, "display_name", reviewer.name)), inline=False)
    return embed


class ReviewSession:
    def __init__(
        self,
        destination: commands.Context | discord.Interaction | discord.abc.Messageable,
        *,
        owner_id: int,
        batch_size: int = 5,
    ):
        self.destination = destination
        self.owner_id = owner_id
        self.batch_size = max(1, min(int(batch_size), 10))
        self.continuous = False
        self.active_image_ids: set[int] = set()
        self.completed_image_ids: set[int] = set()
        self.lock = asyncio.Lock()
        self.stopped = False

    async def send_message(self, *args: Any, **kwargs: Any) -> discord.Message | None:
        destination = self.destination
        if isinstance(destination, discord.Interaction):
            if destination.response.is_done():
                return await destination.followup.send(*args, wait=True, **kwargs)
            await destination.response.send_message(*args, **kwargs)
            return await destination.original_response()
        return await destination.send(*args, **kwargs)

    async def start_batch(self) -> int:
        if self.stopped:
            return 0
        reviews = await asyncio.to_thread(get_pending_person_reviews, self.batch_size)
        if not reviews:
            await self.send_message("✅ 人物確認待ちの写真はありません。")
            self.stopped = True
            return 0
        self.active_image_ids = {int(review["image_id"]) for review in reviews}
        self.completed_image_ids.clear()
        for review in reviews:
            await send_person_review(self.destination, review, session=self)
        return len(reviews)

    async def mark_done(self, image_id: int) -> None:
        async with self.lock:
            if self.stopped or image_id not in self.active_image_ids:
                return
            self.completed_image_ids.add(image_id)
            if self.completed_image_ids != self.active_image_ids:
                return
            finished_count = len(self.active_image_ids)
            self.active_image_ids.clear()
            self.completed_image_ids.clear()

        if self.continuous:
            await self.send_message(
                f"✅ {finished_count}件のレビューが完了しました。次の{self.batch_size}件を自動で読み込みます…"
            )
            await self.start_batch()
            return

        remaining = await asyncio.to_thread(get_pending_person_reviews, 1)
        if not remaining:
            await self.send_message(
                f"🎉 {finished_count}件のレビューが完了し、人物確認待ちは0件になりました。"
            )
            self.stopped = True
            return

        await self.send_message(
            f"✅ {finished_count}件のレビューが完了しました。\n続けて確認しますか？",
            view=ReviewContinueView(self),
        )


class ReviewContinueView(discord.ui.View):
    def __init__(self, session: ReviewSession):
        super().__init__(timeout=900)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "このレビュー操作はコマンドを実行した本人だけが使えます。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="次の5件", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_batch(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🔄 次のレビューを読み込んでいます…", view=None)
        await self.session.start_batch()
        self.stop()

    @discord.ui.button(label="連続レビュー", emoji="🔁", style=discord.ButtonStyle.success)
    async def continuous_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.session.continuous = True
        await interaction.response.edit_message(
            content="🔁 連続レビューを開始しました。各セットが終わると自動で次へ進みます。",
            view=None,
        )
        await self.session.start_batch()
        self.stop()

    @discord.ui.button(label="終了", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def stop_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.session.stopped = True
        await interaction.response.edit_message(content="⏹️ レビューを終了しました。", view=None)
        self.stop()


class PersonInputModal(discord.ui.Modal, title="人物名を手入力"):
    person_names = discord.ui.TextInput(label="人物名", placeholder="複数人は「、」で区切ってください。", style=discord.TextStyle.paragraph, max_length=500)
    note = discord.ui.TextInput(label="メモ", required=False, max_length=500)

    def __init__(self, review: dict[str, Any], session: ReviewSession | None = None):
        super().__init__(timeout=300)
        self.review = review
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = split_person_names(self.person_names.value)
        if not names:
            await interaction.response.send_message("人物名を入力してください。", ephemeral=True)
            return
        await asyncio.to_thread(set_confirmed_image_people, int(self.review["image_id"]), names, confirmed_by=get_reviewer_name(interaction.user), note=normalize_text(self.note.value))
        await interaction.response.edit_message(embed=build_completed_embed(self.review, names, interaction.user), view=None)
        if self.session:
            await self.session.mark_done(int(self.review["image_id"]))


@dataclass
class SelectionState:
    review: dict[str, Any]
    owner_id: int
    source_message: discord.Message
    session: ReviewSession | None = None
    selected_names: list[str] = field(default_factory=list)
    group_name: str = ""
    generation_name: str = ""

    def add_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.selected_names:
                self.selected_names.append(name)


class OwnedView(discord.ui.View):
    def __init__(self, state: SelectionState, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message("この選択画面はコマンドを実行した本人だけが操作できます。", ephemeral=True)
            return False
        return True


def selection_text(state: SelectionState) -> str:
    selected = "、".join(state.selected_names) if state.selected_names else "まだ選択されていません。"
    path = " → ".join(v for v in (state.group_name, state.generation_name) if v) or "グループを選んでください。"
    return f"**選択場所:** {path}\n**選択中（{len(state.selected_names)}人）:** {selected}"


class GroupSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        super().__init__(placeholder="グループを選択", options=[discord.SelectOption(label=g, value=g) for g in SAKAMICHI_MEMBERS])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.group_name = self.values[0]
        self.state.generation_name = ""
        view = GenerationView(self.state)
        await interaction.response.edit_message(content=selection_text(self.state), view=view)


class GroupView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        self.add_item(GroupSelect(state))

    @discord.ui.button(label="候補を追加", emoji="🤖", style=discord.ButtonStyle.secondary, row=1)
    async def add_candidates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.add_names(build_candidate_names(self.state.review))
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class GenerationSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        generations = SAKAMICHI_MEMBERS[state.group_name]
        super().__init__(placeholder="期生を選択", options=[discord.SelectOption(label=g, value=g) for g in generations])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.generation_name = self.values[0]
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))


class GenerationView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        self.add_item(GenerationSelect(state))

    @discord.ui.button(label="グループへ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class MemberSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        names = SAKAMICHI_MEMBERS[state.group_name][state.generation_name]
        super().__init__(placeholder="写っているメンバーを複数選択", min_values=1, max_values=len(names), options=[discord.SelectOption(label=n, value=n, default=n in state.selected_names) for n in names])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.add_names(list(self.values))
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))


class MemberView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        self.add_item(MemberSelect(state))

    @discord.ui.button(label="別の期生から追加", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
    async def generation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GenerationView(self.state))

    @discord.ui.button(label="別グループから追加", emoji="🌳", style=discord.ButtonStyle.secondary, row=1)
    async def group(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class RemoveSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        super().__init__(placeholder="解除する人物を選択", min_values=1, max_values=len(state.selected_names), options=[discord.SelectOption(label=n, value=n) for n in state.selected_names])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.selected_names = [n for n in self.state.selected_names if n not in self.values]
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class SelectedPeopleView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        if state.selected_names:
            self.add_item(RemoveSelect(state))
        self.confirm.disabled = not bool(state.selected_names)

    @discord.ui.button(label="人物を追加", emoji="➕", style=discord.ButtonStyle.primary, row=1)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="この内容で確定", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        reviewer = get_reviewer_name(interaction.user)
        await asyncio.to_thread(set_confirmed_image_people, int(self.state.review["image_id"]), self.state.selected_names, confirmed_by=reviewer, note="階層式レビュー画面から複数人を確定")
        completed = build_completed_embed(self.state.review, self.state.selected_names, interaction.user)
        await self.state.source_message.edit(embed=completed, view=None)
        await interaction.response.edit_message(content="✅ 元のレビュー画面を更新しました。", view=None)
        if self.state.session:
            await self.state.session.mark_done(int(self.state.review["image_id"]))

    @discord.ui.button(label="人物なし", emoji="🚫", style=discord.ButtonStyle.danger, row=2)
    async def nobody(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await asyncio.to_thread(set_confirmed_image_people, int(self.state.review["image_id"]), [], confirmed_by=get_reviewer_name(interaction.user), note="人物なし")
        completed = build_completed_embed(self.state.review, [], interaction.user)
        await self.state.source_message.edit(embed=completed, view=None)
        await interaction.response.edit_message(content="✅ 人物なしで確定しました。", view=None)
        if self.state.session:
            await self.state.session.mark_done(int(self.state.review["image_id"]))

    @discord.ui.button(label="人物不明", emoji="❓", style=discord.ButtonStyle.secondary, row=2)
    async def unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await asyncio.to_thread(set_confirmed_image_people, int(self.state.review["image_id"]), ["人物不明"], confirmed_by=get_reviewer_name(interaction.user), note="人物不明")
        completed = build_completed_embed(self.state.review, ["人物不明"], interaction.user)
        await self.state.source_message.edit(embed=completed, view=None)
        await interaction.response.edit_message(content="✅ 人物不明で確定しました。", view=None)
        if self.state.session:
            await self.state.session.mark_done(int(self.state.review["image_id"]))


class PersonReviewView(discord.ui.View):
    def __init__(self, review: dict[str, Any], session: ReviewSession | None = None):
        super().__init__(timeout=900)
        self.review = review
        self.session = session
        self.image_id = int(review["image_id"])
        self.candidates = build_candidate_names(review)
        self.accept_candidate.disabled = not bool(self.candidates)

        if self.session and self.session.continuous:
            stop_button = discord.ui.Button(
                label="連続停止",
                emoji="⏹️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def stop_continuous(interaction: discord.Interaction) -> None:
                self.session.continuous = False
                await interaction.response.send_message(
                    "⏹️ 連続レビューを停止しました。現在表示中のセット終了後に続行確認を表示します。",
                    ephemeral=True,
                )

            stop_button.callback = stop_continuous
            self.add_item(stop_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.session and self.session.owner_id and interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "このレビュー操作はコマンドを実行した本人だけが使えます。",
                ephemeral=True,
            )
            return False
        return True

    async def complete_review(
        self,
        interaction: discord.Interaction,
        names: list[str],
        *,
        note: str,
    ) -> None:
        await asyncio.to_thread(
            set_confirmed_image_people,
            self.image_id,
            names,
            confirmed_by=get_reviewer_name(interaction.user),
            note=note,
        )
        await interaction.response.edit_message(
            embed=build_completed_embed(self.review, names, interaction.user),
            view=None,
        )
        if self.session:
            await self.session.mark_done(self.image_id)

    @discord.ui.button(label="候補をすべて採用", emoji="✅", style=discord.ButtonStyle.success, row=0)
    async def accept_candidate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            self.candidates,
            note="表示候補をすべて採用",
        )

    @discord.ui.button(label="人物を選ぶ", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def select_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await asyncio.to_thread(seed_member_master)
        initial = split_person_names(self.review.get("confirmed_people", ""))
        state = SelectionState(
            review=self.review,
            owner_id=interaction.user.id,
            source_message=interaction.message,
            session=self.session,
            selected_names=initial,
        )
        await interaction.response.send_message(selection_text(state), view=GroupView(state), ephemeral=True)

    @discord.ui.button(label="手入力", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def manual_input(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PersonInputModal(self.review, self.session))

    @discord.ui.button(label="スキップ", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await asyncio.to_thread(
            mark_person_review_skipped,
            self.image_id,
            get_reviewer_name(interaction.user),
            "Discordレビュー画面でスキップ",
        )
        await interaction.response.edit_message(
            embed=build_skipped_embed(self.review, interaction.user),
            view=None,
        )
        if self.session:
            await self.session.mark_done(self.image_id)

    @discord.ui.button(label="人物なし", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def no_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            [],
            note="人物は写っていない",
        )

    @discord.ui.button(label="人物不明", emoji="❓", style=discord.ButtonStyle.secondary, row=1)
    async def unknown_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            ["人物不明"],
            note="人物は写っているが判別できない",
        )


async def send_person_review(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    review: dict[str, Any],
    *,
    session: ReviewSession | None = None,
) -> discord.Message | None:
    embed = build_review_embed(review)
    view = PersonReviewView(review, session=session)
    local_path = normalize_text(review.get("local_path"))
    image_url = normalize_text(review.get("image_url"))
    file: discord.File | None = None
    if local_path and os.path.isfile(local_path):
        filename = os.path.basename(local_path) or f"photo_{review['image_id']}.jpg"
        file = discord.File(local_path, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
    elif image_url:
        embed.set_image(url=image_url)
    kwargs: dict[str, Any] = {"embed": embed, "view": view}
    if file:
        kwargs["file"] = file
    if isinstance(destination, discord.Interaction):
        if destination.response.is_done():
            return await destination.followup.send(**kwargs, wait=True)
        await destination.response.send_message(**kwargs)
        return await destination.original_response()
    return await destination.send(**kwargs)


async def send_next_person_review(destination: commands.Context | discord.Interaction | discord.abc.Messageable) -> dict[str, Any] | None:
    reviews = await asyncio.to_thread(get_pending_person_reviews, 1)
    if not reviews:
        message = "✅ 人物確認待ちの写真はありません。"
        if isinstance(destination, discord.Interaction):
            await (destination.followup.send(message, ephemeral=True) if destination.response.is_done() else destination.response.send_message(message, ephemeral=True))
        else:
            await destination.send(message)
        return None
    await send_person_review(destination, reviews[0])
    return reviews[0]


async def send_person_review_batch(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    limit: int = 5,
) -> int:
    owner = getattr(destination, "author", None) or getattr(destination, "user", None)
    owner_id = int(getattr(owner, "id", 0) or 0)
    session = ReviewSession(
        destination,
        owner_id=owner_id,
        batch_size=max(1, min(int(limit), 10)),
    )
    return await session.start_batch()
