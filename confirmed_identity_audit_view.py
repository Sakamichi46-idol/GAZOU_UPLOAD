from __future__ import annotations

import asyncio
import io
from contextlib import closing
from typing import Any, Literal

import discord
from PIL import Image

from local_face_recognition import get_face_crop_bytes
from photo_database import (
    complete_face_review,
    get_connection,
    get_person_by_name,
    set_confirmed_image_people,
)
from photo_search import build_photo_attachment_files
from sakamichi_members import SAKAMICHI_MEMBERS

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




def _prepare_face_audit_image(data: bytes, min_side: int = 640) -> bytes:
    """顔見直し一覧用に、顔切り抜きを見やすい大きさへ拡大する。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            width, height = image.size
            if width <= 0 or height <= 0:
                return data
            scale = max(1.0, float(min_side) / float(max(width, height)))
            if scale > 1.0:
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue()
    except Exception:
        return data


async def _build_face_crop_file(
    row: dict[str, Any],
    *,
    position: int,
) -> discord.File | None:
    """顔IDの座標から対象顔だけを切り抜き、Discord添付にする。"""
    face_id = int(row.get("face_id") or 0)
    if face_id <= 0:
        return None
    try:
        data, _ = await asyncio.to_thread(get_face_crop_bytes, face_id)
        data = await asyncio.to_thread(_prepare_face_audit_image, data)
        return discord.File(
            io.BytesIO(data),
            filename=f"face_audit_{position}_{face_id}.jpg",
        )
    except Exception as exc:
        print(f"顔確定見直し用の切り抜き作成エラー: face_id={face_id} / {type(exc).__name__}: {exc}")
        return None


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
            "対象の顔だけを切り抜いて表示しています。確定名と見比べ、修正したい顔を選んでください。"
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


class AuditPersonSelect(discord.ui.Select):
    def __init__(self, picker: "AuditPersonPickerView", names: list[str]) -> None:
        self.picker = picker
        options = [
            discord.SelectOption(
                label=name[:100],
                value=name[:100],
                default=name in picker.selected_names,
            )
            for name in names[:25]
        ]
        super().__init__(
            placeholder=(
                "正しい人物を1人選択"
                if picker.parent_view.mode == "face"
                else "写っている人物を選択（複数可）"
            ),
            options=options,
            min_values=1,
            max_values=1 if picker.parent_view.mode == "face" else max(1, len(options)),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.picker.parent_view.mode == "face":
            await self.picker.apply_face_person(interaction, self.values[0])
            return

        page_names = set(self.picker.current_names())
        self.picker.selected_names.difference_update(page_names)
        self.picker.selected_names.update(self.values)
        self.picker._rebuild()
        await interaction.response.edit_message(
            content=self.picker.page_text(),
            view=self.picker,
        )


class AuditPersonPickerView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, parent: "ConfirmedIdentityAuditView", row: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.parent_view = parent
        self.row = row
        self.page = 0
        self.names = self._member_names()
        self.selected_names = set(_split_people(row.get("confirmed_name")))
        self._rebuild()

    def _member_names(self) -> list[str]:
        group = _text(self.row.get("group_name"))
        generations = SAKAMICHI_MEMBERS.get(group, {})
        names: list[str] = []
        for generation_names in generations.values():
            for name in generation_names:
                if name not in names:
                    names.append(name)

        # DBにだけ存在する人物も選択肢から落とさない。
        with closing(get_connection()) as con:
            db_rows = con.execute(
                """SELECT person_name FROM photo_people
                   WHERE is_active=1 AND (?='' OR group_name=? OR group_name='')
                   ORDER BY person_name""",
                (group, group),
            ).fetchall()
        for db_row in db_rows:
            name = _text(db_row["person_name"])
            if name and name not in names:
                names.append(name)
        return names

    def current_names(self) -> list[str]:
        start = self.page * self.PAGE_SIZE
        return self.names[start : start + self.PAGE_SIZE]

    def _rebuild(self) -> None:
        self.clear_items()
        current = self.current_names()
        if current:
            self.add_item(AuditPersonSelect(self, current))

        prev = discord.ui.Button(
            label="前の人物", emoji="◀️",
            style=discord.ButtonStyle.secondary, row=1,
        )
        nxt = discord.ui.Button(
            label="次の人物", emoji="▶️",
            style=discord.ButtonStyle.secondary, row=1,
        )
        prev.disabled = self.page <= 0
        nxt.disabled = (self.page + 1) * self.PAGE_SIZE >= len(self.names)
        prev.callback = self.previous_page
        nxt.callback = self.next_page
        self.add_item(prev)
        self.add_item(nxt)

        if self.parent_view.mode == "blog":
            confirm = discord.ui.Button(
                label="選択した人物で確定", emoji="✅",
                style=discord.ButtonStyle.success, row=2,
            )
            none_btn = discord.ui.Button(
                label="人物なしに修正", emoji="🚫",
                style=discord.ButtonStyle.danger, row=2,
            )
            confirm.callback = self.confirm_blog_people
            none_btn.callback = self.set_none
            self.add_item(confirm)
            self.add_item(none_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.parent_view.owner_id:
            return True
        await interaction.response.send_message(
            "この画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )
        return False

    def page_text(self) -> str:
        pages = max(1, (len(self.names) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self.parent_view.mode == "face":
            selected = _name_label(self.row)
            return (
                "👤 **顔の人物を選び直す**\n"
                f"現在: **{discord.utils.escape_markdown(selected)}**\n"
                f"人物一覧 **{self.page + 1}/{pages}ページ**"
            )
        selected = "、".join(sorted(self.selected_names)) or "人物なし"
        return (
            "👥 **ブログ写真の人物を選び直す**\n"
            "複数人写っている場合は複数選択できます。ページを移動しても選択は保持されます。\n"
            f"選択中: **{discord.utils.escape_markdown(selected)}**\n"
            f"人物一覧 **{self.page + 1}/{pages}ページ**"
        )

    async def previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(content=self.page_text(), view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._rebuild()
        await interaction.response.edit_message(content=self.page_text(), view=self)

    async def apply_face_person(self, interaction: discord.Interaction, name: str) -> None:
        person = await asyncio.to_thread(get_person_by_name, name)
        if not person:
            await interaction.response.send_message(
                "人物マスターを取得できませんでした。",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            complete_face_review,
            int(self.row["face_id"]),
            int(person["id"]),
            _reviewer(interaction.user),
            "顔ごとの確定済み一覧から選択修正",
        )
        await self.parent_view.reload()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        try:
            await self.parent_view.refresh_message()
        except Exception:
            pass
        await self.parent_view.cleanup_transients()

    async def confirm_blog_people(self, interaction: discord.Interaction) -> None:
        names = sorted(self.selected_names)
        await asyncio.to_thread(
            set_confirmed_image_people,
            int(self.row["image_id"]),
            names,
            confirmed_by=_reviewer(interaction.user),
            note="ブログ別の確定済み一覧から選択修正",
        )
        await self.parent_view.reload()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        try:
            await self.parent_view.refresh_message()
        except Exception:
            pass
        await self.parent_view.cleanup_transients()

    async def set_none(self, interaction: discord.Interaction) -> None:
        self.selected_names.clear()
        await self.confirm_blog_people(interaction)


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

        if self.parent_view.mode == "face":
            face_file = await _build_face_crop_file(row, position=1)
            if face_file is not None:
                embed.set_image(url=f"attachment://{face_file.filename}")
                await interaction.response.send_message(
                    embed=embed,
                    file=face_file,
                    view=AuditSelectedItemView(self.parent_view, row),
                    ephemeral=True,
                )
                try:
                    self.parent_view.register_transient(await interaction.original_response())
                except Exception:
                    pass
                return

        await interaction.response.send_message(
            embed=embed,
            view=AuditSelectedItemView(self.parent_view, row),
            ephemeral=True,
        )
        try:
            self.parent_view.register_transient(await interaction.original_response())
        except Exception:
            pass


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

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await self.parent_view.cleanup_transients()

    @discord.ui.button(label="人物を選び直す", emoji="👤", style=discord.ButtonStyle.primary)
    async def edit_name(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        picker = AuditPersonPickerView(self.parent_view, self.row)
        await interaction.response.send_message(
            picker.page_text(),
            view=picker,
            ephemeral=True,
        )
        try:
            message = await interaction.original_response()
            self.parent_view.register_transient(message)
        except Exception:
            pass


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
        self.transient_messages: list[discord.Message] = []
        self._rebuild_items()


    def register_transient(self, message: discord.Message) -> None:
        if all(getattr(x, "id", None) != getattr(message, "id", None) for x in self.transient_messages):
            self.transient_messages.append(message)

    async def cleanup_transients(self) -> None:
        messages = self.transient_messages
        self.transient_messages = []
        for message in messages:
            try:
                await message.delete()
            except Exception:
                pass

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
            await self.cleanup_transients()
            self.page = max(0, self.page - 1)
            self._rebuild_items()
            await self._edit(interaction)

        async def next_cb(interaction: discord.Interaction) -> None:
            await self.cleanup_transients()
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
        current = self.current_rows()

        # 顔ごとの見直しでは元写真ではなく、確認対象の顔だけを表示する。
        # 複数人写真でも「どの顔の確定名か」が一覧だけで分かるようにする。
        if self.mode == "face":
            files: list[discord.File] = []
            for index, row in enumerate(current, 1):
                face_file = await _build_face_crop_file(
                    row,
                    position=self.page * PAGE_SIZE + index,
                )
                if face_file is not None:
                    files.append(face_file)
            return files

        prepared: list[dict[str, Any]] = []
        for index, row in enumerate(current, 1):
            item = dict(row)
            item["id"] = self.page * PAGE_SIZE + index
            prepared.append(item)
        return await build_photo_attachment_files(prepared)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        files = await self._attachments()
        # ページ送りは新しいエフェメラルを増やさず、現在の一覧をその場で置き換える。
        await interaction.message.edit(
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
