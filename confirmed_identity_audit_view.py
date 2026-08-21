from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any, Literal

import discord

from photo_database import (
    complete_face_review,
    get_connection,
    get_person_by_name,
    set_confirmed_image_people,
)
from photo_search import build_photo_attachment_files

PAGE_SIZE = 9
AuditMode = Literal["blog", "face"]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _reviewer(user: discord.abc.User) -> str:
    name = _text(getattr(user, "display_name", "")) or _text(getattr(user, "name", ""))
    return f"{name} ({user.id})"


def _split_people(value: Any) -> list[str]:
    text = _text(value).replace("，", "、").replace(",", "、")
    result: list[str] = []
    for part in text.split("、"):
        name = part.strip()
        if name and name not in result:
            result.append(name)
    return result


def _mode_label(mode: AuditMode) -> str:
    return "ブログ別の人物確定" if mode == "blog" else "顔ごとの人物確定"


def _mode_title(mode: AuditMode) -> str:
    return "📖 ブログ別の確定人物を見直す" if mode == "blog" else "🙂 顔ごとの確定人物を見直す"


def get_confirmed_identity_audit_rows(
    mode: AuditMode,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """指定モードの手動確定結果だけを新しい順で返す。"""
    safe_limit = max(1, min(int(limit), 2000))

    with closing(get_connection()) as con:
        if mode == "blog":
            rows = con.execute(
                """
                SELECT
                    'image' AS audit_kind,
                    i.id AS image_id,
                    NULL AS face_id,
                    i.image_index,
                    i.local_path,
                    i.image_url,
                    i.bucket_key,
                    b.id AS blog_id,
                    b.blog_url,
                    b.group_name,
                    b.member_name,
                    b.title,
                    b.published_at,
                    q.reviewed_at AS confirmed_at,
                    GROUP_CONCAT(p.person_name, '、') AS confirmed_name,
                    NULL AS confirmed_person_id
                FROM photo_images i
                JOIN photo_blogs b ON b.id=i.blog_id
                JOIN photo_review_queue q ON q.image_id=i.id
                LEFT JOIN photo_image_people p
                  ON p.image_id=i.id AND p.relation_status='confirmed'
                WHERE q.review_type='person_identity'
                  AND q.status='completed'
                  AND COALESCE(b.is_hidden, 0)=0
                GROUP BY i.id
                ORDER BY COALESCE(q.reviewed_at, '') DESC, i.id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT
                    'face' AS audit_kind,
                    i.id AS image_id,
                    f.id AS face_id,
                    i.image_index,
                    i.local_path,
                    i.image_url,
                    i.bucket_key,
                    b.id AS blog_id,
                    b.blog_url,
                    b.group_name,
                    b.member_name,
                    b.title,
                    b.published_at,
                    f.confirmed_at AS confirmed_at,
                    COALESCE(pp.person_name, '') AS confirmed_name,
                    f.confirmed_person_id AS confirmed_person_id
                FROM photo_faces f
                JOIN photo_images i ON i.id=f.image_id
                JOIN photo_blogs b ON b.id=i.blog_id
                LEFT JOIN photo_people pp ON pp.id=f.confirmed_person_id
                WHERE f.confirmed_person_id IS NOT NULL
                  AND f.confirmation_status IN ('confirmed','auto_confirmed','manually_confirmed')
                  AND COALESCE(b.is_hidden, 0)=0
                ORDER BY COALESCE(f.confirmed_at, '') DESC, f.id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

    return [dict(row) for row in rows]


def _name_label(row: dict[str, Any]) -> str:
    name = _text(row.get("confirmed_name"))
    if row.get("audit_kind") == "image" and not name:
        return "人物なし"
    return name or "不明"


def _page_text(rows: list[dict[str, Any]], page: int, mode: AuditMode) -> str:
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    current = rows[start : start + PAGE_SIZE]

    if mode == "blog":
        guide = (
            "ブログ別の人物確認で確定した内容だけを表示しています。\n"
            "画像と確定名を見比べ、修正したい項目を選んでください。"
        )
    else:
        guide = (
            "顔ごとの人物確認で確定した内容だけを表示しています。\n"
            "画像と確定名を見比べ、修正したい顔を選んでください。"
        )

    lines = [_mode_title(mode), guide, ""]
    for index, row in enumerate(current, 1):
        name = discord.utils.escape_markdown(_name_label(row))
        image_id = int(row.get("image_id") or 0)
        if mode == "face":
            face_id = int(row.get("face_id") or 0)
            lines.append(f"**{index}. {name}** — 写真ID {image_id} / 顔ID {face_id}")
        else:
            blog_title = discord.utils.escape_markdown(_text(row.get("title")) or "無題")
            lines.append(f"**{index}. {name}** — 写真ID {image_id} / {blog_title}")

    lines.append("")
    lines.append(f"ページ **{page + 1}/{total_pages}** / 全 **{len(rows)}件**")
    return "\n".join(lines)


class AuditNameModal(discord.ui.Modal):
    def __init__(
        self,
        parent: "ConfirmedIdentityAuditView",
        row: dict[str, Any],
    ) -> None:
        self.parent_view = parent
        self.row = row
        is_face = parent.mode == "face"
        super().__init__(
            title="顔の確定人物を修正" if is_face else "ブログ写真の確定人物を修正",
            timeout=300,
        )
        self.people = discord.ui.TextInput(
            label="人物名" if is_face else "人物名（複数は「、」区切り）",
            placeholder=(
                "例: 岩本蓮加"
                if is_face
                else "例: 岩本蓮加、与田祐希（空欄なら人物なし）"
            ),
            default=_text(row.get("confirmed_name"))[:4000],
            required=is_face,
            max_length=4000,
        )
        self.add_item(self.people)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        reviewer = _reviewer(interaction.user)

        if self.parent_view.mode == "face":
            name = _text(self.people.value)
            person = await asyncio.to_thread(get_person_by_name, name)
            if not person:
                await interaction.followup.send(
                    f"⚠️ 人物マスターに **{discord.utils.escape_markdown(name)}** が見つかりません。正確な名前を入力してください。",
                    ephemeral=True,
                )
                return
            await asyncio.to_thread(
                complete_face_review,
                int(self.row["face_id"]),
                int(person["id"]),
                reviewer,
                "顔ごとの確定済み一覧から再確認・修正",
            )
        else:
            names = _split_people(self.people.value)
            await asyncio.to_thread(
                set_confirmed_image_people,
                int(self.row["image_id"]),
                names,
                confirmed_by=reviewer,
                note="ブログ別の確定済み一覧から再確認・修正",
            )

        await self.parent_view.reload()
        await interaction.followup.send(
            "✅ 確定内容を修正しました。元の一覧も最新状態へ更新します。",
            ephemeral=True,
        )
        try:
            await self.parent_view.refresh_message()
        except Exception:
            pass


class AuditItemSelect(discord.ui.Select):
    def __init__(self, parent: "ConfirmedIdentityAuditView") -> None:
        self.parent_view = parent
        current = parent.current_rows()
        options: list[discord.SelectOption] = []

        for index, row in enumerate(current, 1):
            name = _name_label(row)
            if parent.mode == "face":
                detail = (
                    f"写真ID {int(row.get('image_id') or 0)} / "
                    f"顔ID {int(row.get('face_id') or 0)}"
                )
            else:
                detail = (
                    f"写真ID {int(row.get('image_id') or 0)} / "
                    f"{_text(row.get('title')) or '無題'}"
                )
            options.append(
                discord.SelectOption(
                    label=f"{index}. {name}"[:100],
                    description=detail[:100],
                    value=str(index - 1),
                )
            )

        if not options:
            options.append(discord.SelectOption(label="確定済みデータなし", value="none"))

        super().__init__(
            placeholder=(
                "見直す顔を選択"
                if parent.mode == "face"
                else "見直すブログ写真を選択"
            ),
            options=options,
            min_values=1,
            max_values=1,
            row=0,
            disabled=not bool(current),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message("確定済みデータはありません。", ephemeral=True)
            return

        index = int(self.values[0])
        current = self.parent_view.current_rows()
        if index < 0 or index >= len(current):
            await interaction.response.send_message("対象を再取得してください。", ephemeral=True)
            return

        row = current[index]
        embed = discord.Embed(
            title=(
                "🙂 顔ごとの確定内容"
                if self.parent_view.mode == "face"
                else "📖 ブログ別の確定内容"
            ),
            description=(
                f"確定人物: **{discord.utils.escape_markdown(_name_label(row))}**\n"
                f"写真ID: **{int(row.get('image_id') or 0)}**"
                + (
                    f" / 顔ID: **{int(row.get('face_id') or 0)}**"
                    if self.parent_view.mode == "face"
                    else ""
                )
            ),
            color=discord.Color.blurple(),
        )
        if row.get("blog_url"):
            embed.add_field(name="ブログ", value=f"[元記事を開く]({row['blog_url']})", inline=False)

        await interaction.response.send_message(
            embed=embed,
            view=AuditSelectedItemView(self.parent_view, row),
            ephemeral=True,
        )


class AuditSelectedItemView(discord.ui.View):
    def __init__(self, parent: "ConfirmedIdentityAuditView", row: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.parent_view = parent
        self.row = row

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.parent_view.owner_id:
            return True
        await interaction.response.send_message(
            "この見直し画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="この確定でOK", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        reviewer = _reviewer(interaction.user)

        if self.parent_view.mode == "face":
            person_id = int(self.row.get("confirmed_person_id") or 0)
            if person_id <= 0:
                await interaction.response.send_message("確定人物IDを取得できません。", ephemeral=True)
                return
            await asyncio.to_thread(
                complete_face_review,
                int(self.row["face_id"]),
                person_id,
                reviewer,
                "顔ごとの確定済み一覧で再確認: 内容OK",
            )
        else:
            await asyncio.to_thread(
                set_confirmed_image_people,
                int(self.row["image_id"]),
                _split_people(self.row.get("confirmed_name")),
                confirmed_by=reviewer,
                note="ブログ別の確定済み一覧で再確認: 内容OK",
            )

        await interaction.response.edit_message(
            content="✅ この確定内容は正しいものとして再確認しました。",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="人物名を修正", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_name(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AuditNameModal(self.parent_view, self.row))


class ConfirmedIdentityAuditView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        rows: list[dict[str, Any]],
        mode: AuditMode,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.rows = rows
        self.mode = mode
        self.page = max(0, int(page))
        self.message: discord.Message | None = None
        self._rebuild_items()

    def current_rows(self) -> list[dict[str, Any]]:
        start = self.page * PAGE_SIZE
        return self.rows[start : start + PAGE_SIZE]

    def _rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(AuditItemSelect(self))

        prev = discord.ui.Button(label="前の9件", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        nxt = discord.ui.Button(label="次の9件", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
        refresh = discord.ui.Button(label="一覧を更新", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
        prev.disabled = self.page <= 0
        nxt.disabled = (self.page + 1) * PAGE_SIZE >= len(self.rows)

        async def prev_cb(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            self._rebuild_items()
            await self._edit(interaction)

        async def next_cb(interaction: discord.Interaction) -> None:
            self.page += 1
            self._rebuild_items()
            await self._edit(interaction)

        async def refresh_cb(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.reload()
            await self.refresh_message()

        prev.callback = prev_cb
        nxt.callback = next_cb
        refresh.callback = refresh_cb
        self.add_item(prev)
        self.add_item(nxt)
        self.add_item(refresh)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この一覧は開いた管理者だけが操作できます。",
            ephemeral=True,
        )
        return False

    async def reload(self) -> None:
        self.rows = await asyncio.to_thread(
            get_confirmed_identity_audit_rows,
            self.mode,
            500,
        )
        max_page = max(0, (len(self.rows) - 1) // PAGE_SIZE)
        self.page = min(self.page, max_page)
        self._rebuild_items()

    async def _attachments(self) -> list[discord.File]:
        prepared: list[dict[str, Any]] = []
        for index, row in enumerate(self.current_rows(), 1):
            item = dict(row)
            item["id"] = self.page * PAGE_SIZE + index
            prepared.append(item)
        return await build_photo_attachment_files(prepared)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        files = await self._attachments()
        await interaction.edit_original_response(
            content=_page_text(self.rows, self.page, self.mode),
            attachments=files,
            view=self,
        )

    async def refresh_message(self) -> None:
        if self.message is None:
            return
        files = await self._attachments()
        await self.message.edit(
            content=_page_text(self.rows, self.page, self.mode),
            attachments=files,
            view=self,
        )


async def send_confirmed_identity_audit(
    interaction: discord.Interaction,
    mode: AuditMode,
) -> None:
    if mode not in ("blog", "face"):
        raise ValueError(f"unknown audit mode: {mode}")

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    rows = await asyncio.to_thread(get_confirmed_identity_audit_rows, mode, 500)
    if not rows:
        await interaction.followup.send(
            f"✅ {_mode_label(mode)}には、まだ見直せる確定済みデータがありません。",
            ephemeral=True,
        )
        return

    view = ConfirmedIdentityAuditView(interaction.user.id, rows, mode)
    files = await view._attachments()
    message = await interaction.followup.send(
        content=_page_text(rows, 0, mode),
        files=files,
        view=view,
        ephemeral=True,
        wait=True,
    )
    view.message = message
