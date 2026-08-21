from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import discord

from photo_database import (
    complete_face_review,
    get_connection,
    get_person_by_name,
    set_confirmed_image_people,
)
from photo_search import build_photo_attachment_files

PAGE_SIZE = 9


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


def get_confirmed_identity_audit_rows(limit: int = 500) -> list[dict[str, Any]]:
    """写真単位・顔単位の手動確定結果を新しい順でまとめて返す。"""
    safe_limit = max(1, min(int(limit), 2000))
    with closing(get_connection()) as con:
        image_rows = con.execute(
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
            """
        ).fetchall()

        face_rows = con.execute(
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
            """
        ).fetchall()

    rows = [dict(row) for row in image_rows] + [dict(row) for row in face_rows]
    rows.sort(
        key=lambda row: (
            _text(row.get("confirmed_at")),
            int(row.get("image_id") or 0),
            int(row.get("face_id") or 0),
        ),
        reverse=True,
    )
    return rows[:safe_limit]


def _kind_label(row: dict[str, Any]) -> str:
    return "写真単位" if row.get("audit_kind") == "image" else "顔単位"


def _name_label(row: dict[str, Any]) -> str:
    name = _text(row.get("confirmed_name"))
    if row.get("audit_kind") == "image" and not name:
        return "人物なし"
    return name or "不明"


def _page_text(rows: list[dict[str, Any]], page: int) -> str:
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    current = rows[start : start + PAGE_SIZE]
    lines = [
        "🔍 **確定済み人物の見直し**",
        "写真単位の人物確定と、顔単位の人物確定をまとめて再確認できます。",
        "下の画像は一覧の番号と同じ順番です。修正したい項目を選択してください。",
        "",
    ]
    for index, row in enumerate(current, 1):
        source = _kind_label(row)
        name = discord.utils.escape_markdown(_name_label(row))
        image_id = int(row.get("image_id") or 0)
        face_id = int(row.get("face_id") or 0)
        extra = f" / 顔ID {face_id}" if face_id else ""
        lines.append(f"**{index}. [{source}] {name}** — 写真ID {image_id}{extra}")
    lines.append("")
    lines.append(f"ページ **{page + 1}/{total_pages}** / 全 **{len(rows)}件**")
    return "\n".join(lines)


class AuditNameModal(discord.ui.Modal):
    def __init__(self, parent: "ConfirmedIdentityAuditView", row: dict[str, Any]) -> None:
        self.parent_view = parent
        self.row = row
        is_face = row.get("audit_kind") == "face"
        super().__init__(title="顔の確定人物を修正" if is_face else "写真の確定人物を修正", timeout=300)
        self.people = discord.ui.TextInput(
            label="人物名" if is_face else "人物名（複数は「、」区切り）",
            placeholder="例: 岩本蓮加" if is_face else "例: 岩本蓮加、与田祐希（空欄なら人物なし）",
            default=_text(row.get("confirmed_name"))[:4000],
            required=is_face,
            max_length=4000,
        )
        self.add_item(self.people)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        reviewer = _reviewer(interaction.user)
        if self.row.get("audit_kind") == "face":
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
                "確定済み一覧から再確認・修正",
            )
        else:
            names = _split_people(self.people.value)
            await asyncio.to_thread(
                set_confirmed_image_people,
                int(self.row["image_id"]),
                names,
                confirmed_by=reviewer,
                note="確定済み一覧から再確認・修正",
            )

        await self.parent_view.reload()
        await interaction.followup.send(
            "✅ 確定内容を修正しました。一覧も最新状態へ更新します。",
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
            label = f"{index}. {_kind_label(row)} / {_name_label(row)}"
            detail = f"写真ID {int(row.get('image_id') or 0)}"
            if row.get("face_id"):
                detail += f" / 顔ID {int(row.get('face_id') or 0)}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=detail[:100],
                    value=str(index - 1),
                )
            )
        if not options:
            options.append(discord.SelectOption(label="確定済みデータなし", value="none"))
        super().__init__(
            placeholder="見直す項目を選択",
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
        self.parent_view.selected_row = row
        embed = discord.Embed(
            title="🔎 確定内容の詳細",
            description=(
                f"判定元: **{_kind_label(row)}**\n"
                f"確定人物: **{discord.utils.escape_markdown(_name_label(row))}**\n"
                f"写真ID: **{int(row.get('image_id') or 0)}**"
                + (f" / 顔ID: **{int(row.get('face_id') or 0)}**" if row.get("face_id") else "")
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
        await interaction.response.send_message("この見直し画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="この確定内容でOK", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        reviewer = _reviewer(interaction.user)
        if self.row.get("audit_kind") == "face":
            person_id = int(self.row.get("confirmed_person_id") or 0)
            if person_id <= 0:
                await interaction.response.send_message("確定人物IDを取得できません。", ephemeral=True)
                return
            await asyncio.to_thread(
                complete_face_review,
                int(self.row["face_id"]),
                person_id,
                reviewer,
                "確定済み一覧で再確認: 内容OK",
            )
        else:
            await asyncio.to_thread(
                set_confirmed_image_people,
                int(self.row["image_id"]),
                _split_people(self.row.get("confirmed_name")),
                confirmed_by=reviewer,
                note="確定済み一覧で再確認: 内容OK",
            )
        await interaction.response.edit_message(
            content="✅ この確定内容を再確認済みとして記録しました。",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="人物名を修正", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_name(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AuditNameModal(self.parent_view, self.row))


class ConfirmedIdentityAuditView(discord.ui.View):
    def __init__(self, owner_id: int, rows: list[dict[str, Any]], page: int = 0) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.rows = rows
        self.page = max(0, int(page))
        self.message: discord.Message | None = None
        self.selected_row: dict[str, Any] | None = None
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
            self._rebuild_items()
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
        await interaction.response.send_message("この一覧は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    async def reload(self) -> None:
        self.rows = await asyncio.to_thread(get_confirmed_identity_audit_rows, 500)
        max_page = max(0, (len(self.rows) - 1) // PAGE_SIZE)
        self.page = min(self.page, max_page)
        self._rebuild_items()

    async def _attachments(self) -> list[discord.File]:
        # 同じ写真が「写真単位」と「顔単位」の両方に並ぶ場合でも、
        # Discord添付ファイル名が重複しないよう表示順の一時IDを使う。
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
            content=_page_text(self.rows, self.page),
            attachments=files,
            view=self,
        )

    async def refresh_message(self) -> None:
        if self.message is None:
            return
        files = await self._attachments()
        await self.message.edit(
            content=_page_text(self.rows, self.page),
            attachments=files,
            view=self,
        )


async def send_confirmed_identity_audit(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    rows = await asyncio.to_thread(get_confirmed_identity_audit_rows, 500)
    if not rows:
        await interaction.followup.send("✅ まだ見直せる確定済み人物データはありません。", ephemeral=True)
        return
    view = ConfirmedIdentityAuditView(interaction.user.id, rows)
    files = await view._attachments()
    message = await interaction.followup.send(
        content=_page_text(rows, 0),
        files=files,
        view=view,
        ephemeral=True,
        wait=True,
    )
    view.message = message
