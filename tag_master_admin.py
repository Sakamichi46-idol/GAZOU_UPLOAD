from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import discord

from photo_database import get_connection
from tag_master import (
    approve_tag,
    block_tag,
    bootstrap_from_existing,
    diagnostic_summary,
    merge_candidates,
    merge_tags,
    rebuild_cache,
    normalize_key,
)


PAGE_SIZE = 20


# =========================
# タグ状態
# =========================

def _summary_embed() -> discord.Embed:
    with closing(get_connection()) as con:
        bootstrap_from_existing(con)
        stats = diagnostic_summary(con)

    embed = discord.Embed(
        title="📊 タグの状態",
        color=0xF1C40F,
    )

    embed.description = (
        "タグDBの現在の状態を確認する画面です。\n"
        "整理を始める場合は「未承認タグを整理」、"
        "似たタグをまとめる場合は「タグを統合・修正」を使ってください。"
    )

    embed.add_field(
        name="代表タグ",
        value=f"{stats['master']:,}件",
        inline=True,
    )

    embed.add_field(
        name="承認済み",
        value=f"{stats['approved']:,}件",
        inline=True,
    )

    embed.add_field(
        name="未承認",
        value=f"{stats['pending']:,}件",
        inline=True,
    )

    embed.add_field(
        name="除外",
        value=f"{stats['blocked']:,}件",
        inline=True,
    )

    embed.add_field(
        name="別表記",
        value=f"{stats['aliases']:,}件",
        inline=True,
    )

    embed.add_field(
        name="AI判定が不確かなタグ",
        value=f"{stats['low_confidence']:,}件",
        inline=True,
    )

    embed.add_field(
        name="検索キャッシュ",
        value=f"{stats['cache']:,}件",
        inline=True,
    )

    embed.set_footer(
        text=(
            "優先順位："
            "手動タグ ＞ 承認済みAIタグ ＞ 未承認AIタグ"
        )
    )

    return embed


# =========================
# タグ統合
# =========================

def _merge(
    source: int,
    target: int,
    actor: str,
) -> None:
    with closing(get_connection()) as con:
        merge_tags(
            con,
            source,
            target,
            actor=actor,
        )

        rebuild_cache(con)
        con.commit()


def _find_tag_id_by_name(
    value: str,
) -> tuple[int, str] | None:
    key = normalize_key(
        str(value or "").strip()
    )

    if not key:
        return None

    with closing(get_connection()) as con:
        bootstrap_from_existing(con)

        row = con.execute(
            """
            SELECT
                m.id,
                m.canonical_tag

            FROM tag_aliases a

            JOIN tag_master m
                ON m.id = a.canonical_tag_id

            WHERE a.alias_key = ?

            LIMIT 1
            """,
            (
                key,
            ),
        ).fetchone()

        if row:
            return (
                int(row[0]),
                str(row[1]),
            )

        row = con.execute(
            """
            SELECT
                id,
                canonical_tag

            FROM tag_master

            WHERE normalized_key = ?

            LIMIT 1
            """,
            (
                key,
            ),
        ).fetchone()

        if row:
            return (
                int(row[0]),
                str(row[1]),
            )

    return None


def _load_merge_candidates() -> list[Any]:
    with closing(get_connection()) as con:
        return merge_candidates(
            con,
            25,
        )


# =========================
# 未承認タグ取得
# =========================

def _load_pending_tags(
    page: int,
) -> tuple[int, list[Any]]:
    safe_page = max(
        0,
        int(page),
    )

    with closing(get_connection()) as con:
        total = int(
            con.execute(
                """
                SELECT COUNT(*)

                FROM tag_master

                WHERE status = 'pending'
                """
            ).fetchone()[0]
            or 0
        )

        rows = con.execute(
            """
            SELECT
                m.id,
                m.canonical_tag,
                m.category,

                (
                    SELECT COUNT(*)

                    FROM tag_aliases a

                    WHERE
                        a.canonical_tag_id = m.id
                ) AS aliases

            FROM tag_master m

            WHERE
                m.status = 'pending'

            ORDER BY
                aliases DESC,
                m.id ASC

            LIMIT ?
            OFFSET ?
            """,
            (
                PAGE_SIZE,
                safe_page * PAGE_SIZE,
            ),
        ).fetchall()

    return total, rows


# =========================
# 検索インデックス
# =========================

def _rebuild_search_index() -> dict[str, Any]:
    with closing(get_connection()) as con:
        bootstrap_from_existing(
            con
        )

        result = rebuild_cache(
            con
        )

        con.commit()

        return result


# =========================
# ID指定統合
# =========================

class MergeModal(
    discord.ui.Modal,
    title="IDを指定してタグを統合",
):
    source_id = discord.ui.TextInput(
        label="統合元ID",
        placeholder="例: 125",
    )

    target_id = discord.ui.TextInput(
        label="統合先ID",
        placeholder="例: 30",
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            source = int(
                str(
                    self.source_id.value
                ).strip()
            )

            target = int(
                str(
                    self.target_id.value
                ).strip()
            )

        except ValueError:
            await interaction.response.send_message(
                "⚠️ 統合元IDと統合先IDは数字で入力してください。",
                ephemeral=True,
            )
            return

        if source == target:
            await interaction.response.send_message(
                "⚠️ 統合元と統合先に同じIDは指定できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            await asyncio.to_thread(
                _merge,
                source,
                target,
                str(
                    interaction.user.id
                ),
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ タグを統合できませんでした。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                "✅ タグを統合しました。\n"
                "元の表記は削除せず、別表記として代表タグへ紐付けています。"
            ),
            ephemeral=True,
        )


# =========================
# タグ名指定統合
# =========================

class TagNameMergeModal(
    discord.ui.Modal,
    title="タグ名で統合",
):
    source_name = discord.ui.TextInput(
        label="統合する側のタグ名",
        placeholder="例: 制服姿",
        max_length=100,
    )

    target_name = discord.ui.TextInput(
        label="残す代表タグ名",
        placeholder="例: 制服",
        max_length=100,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        source = await asyncio.to_thread(
            _find_tag_id_by_name,
            str(
                self.source_name.value
            ),
        )

        target = await asyncio.to_thread(
            _find_tag_id_by_name,
            str(
                self.target_name.value
            ),
        )

        if not source or not target:
            missing = []

            if not source:
                missing.append(
                    f"統合元「{self.source_name.value}」"
                )

            if not target:
                missing.append(
                    f"統合先「{self.target_name.value}」"
                )

            await interaction.followup.send(
                (
                    "⚠️ "
                    + " / ".join(missing)
                    + " が見つかりませんでした。"
                    "タグ名を確認してください。"
                ),
                ephemeral=True,
            )
            return

        if source[0] == target[0]:
            await interaction.followup.send(
                (
                    f"⚠️ 「{source[1]}」は"
                    "すでに同じ代表タグです。"
                ),
                ephemeral=True,
            )
            return

        try:
            await asyncio.to_thread(
                _merge,
                source[0],
                target[0],
                str(
                    interaction.user.id
                ),
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ タグを統合できませんでした。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                f"✅ **{source[1]}** を "
                f"**{target[1]}** へ統合しました。\n"
                "元の表記は別表記として残るため、"
                "検索には引き続き利用できます。"
            ),
            ephemeral=True,
        )


# =========================
# 未承認タグ選択
# =========================

class PendingTagSelect(
    discord.ui.Select
):
    def __init__(
        self,
        owner_id: int,
        rows: list[Any],
        action: str,
    ):
        self.owner_id = int(
            owner_id
        )

        self.action = str(
            action
        )

        options = [
            discord.SelectOption(
                label=str(
                    row[1]
                )[:100],
                value=str(
                    row[0]
                ),
                description=(
                    f"{row[2] or 'カテゴリ未設定'}"
                    f" / 別表記{row[3]}件"
                )[:100],
            )
            for row in rows[:25]
        ]

        placeholder = (
            "承認するタグを選択"
            if self.action == "approve"
            else "検索対象外にするタグを選択"
        )

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=min(
                25,
                len(options),
            ),
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        ids = [
            int(value)
            for value in self.values
        ]

        actor = str(
            interaction.user.id
        )

        def run() -> None:
            with closing(
                get_connection()
            ) as con:
                for master_id in ids:
                    if (
                        self.action
                        == "approve"
                    ):
                        approve_tag(
                            con,
                            master_id,
                            actor=actor,
                        )

                    else:
                        block_tag(
                            con,
                            master_id,
                            actor=actor,
                        )

                rebuild_cache(
                    con
                )

                con.commit()

        try:
            await asyncio.to_thread(
                run
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ タグ状態の変更に失敗しました。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        action_name = (
            "承認済み"
            if self.action == "approve"
            else "検索対象外"
        )

        # 成功メッセージを新しく積み上げず、
        # 現在の未承認タグ画面そのものを更新する。
        page = 0

        parent = self.view

        if parent is not None:
            page = int(
                getattr(
                    parent,
                    "page",
                    0,
                )
                or 0
            )

        refreshed = await PendingTagView.create(
            self.owner_id,
            page,
        )

        # 現在ページの全項目を処理して空になった場合は
        # ひとつ前のページへ戻す。
        if (
            not refreshed.rows
            and page > 0
        ):
            refreshed = await PendingTagView.create(
                self.owner_id,
                page - 1,
            )

        await interaction.edit_original_response(
            content=(
                f"✅ {len(ids)}件を"
                f"{action_name}にしました。"
            ),
            embed=refreshed.embed(),
            view=refreshed,
        )


# =========================
# 未承認タグ一覧
# =========================

class PendingTagView(
    discord.ui.View
):
    def __init__(
        self,
        owner_id: int,
        page: int = 0,
        *,
        total: int | None = None,
        rows: list[Any] | None = None,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(
            owner_id
        )

        self.page = max(
            0,
            int(page),
        )

        if (
            total is None
            or rows is None
        ):
            total, rows = _load_pending_tags(
                self.page
            )

        self.total = int(
            total or 0
        )

        self.rows = list(
            rows or []
        )

        if self.rows:
            self.add_item(
                PendingTagSelect(
                    owner_id,
                    self.rows,
                    "approve",
                )
            )

            self.add_item(
                PendingTagSelect(
                    owner_id,
                    self.rows,
                    "block",
                )
            )

        self.previous.disabled = (
            self.page <= 0
        )

        self.next.disabled = (
            (
                self.page + 1
            )
            * PAGE_SIZE
            >= self.total
        )

    @classmethod
    async def create(
        cls,
        owner_id: int,
        page: int = 0,
    ) -> "PendingTagView":
        safe_page = max(
            0,
            int(page),
        )

        total, rows = await asyncio.to_thread(
            _load_pending_tags,
            safe_page,
        )

        return cls(
            owner_id,
            safe_page,
            total=total,
            rows=rows,
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await interaction.response.send_message(
            "この画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )

        return False

    def embed(
        self,
    ) -> discord.Embed:
        start = (
            self.page
            * PAGE_SIZE
            + 1
            if self.rows
            else 0
        )

        lines = [
            (
                f"`{row[0]}` "
                f"**{row[1]}** "
                f"— {row[2] or 'カテゴリ未設定'}"
                f" / 別表記{row[3]}件"
            )
            for row in self.rows
        ]

        embed = discord.Embed(
            title="🆕 未承認タグを整理",
            description=(
                "\n".join(
                    lines
                )
                or
                "✅ 未承認タグはありません。"
            ),
            color=0xFEE75C,
        )

        end = (
            start
            + len(
                self.rows
            )
            - 1
            if self.rows
            else 0
        )

        total_pages = max(
            1,
            (
                self.total
                + PAGE_SIZE
                - 1
            )
            // PAGE_SIZE,
        )

        embed.set_footer(
            text=(
                f"{start}〜{end}"
                f" / {self.total}件"
                f" ・ "
                f"{min(self.page + 1, total_pages)}"
                f"/{total_pages}ページ"
            )
        )

        return embed

    @discord.ui.button(
        label="前の20件",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()

        view = await PendingTagView.create(
            self.owner_id,
            self.page - 1,
        )

        await interaction.edit_original_response(
            content=None,
            embed=view.embed(),
            view=view,
        )

    @discord.ui.button(
        label="次の20件",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()

        view = await PendingTagView.create(
            self.owner_id,
            self.page + 1,
        )

        await interaction.edit_original_response(
            content=None,
            embed=view.embed(),
            view=view,
        )


# =========================
# 共通View
# =========================

class TagMasterBaseView(
    discord.ui.View
):
    def __init__(
        self,
        owner_id: int,
        *,
        timeout: float = 900,
    ):
        super().__init__(
            timeout=timeout
        )

        self.owner_id = int(
            owner_id
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await interaction.response.send_message(
            "この画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )

        return False


# =========================
# タグ管理トップ画面
# =========================

def _home_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏷️ タグ管理",
        description=(
            "やりたい操作を選んでください。\n\n"

            "**🔍 タグを探す**\n"
            "登録済みタグを名前から検索します。\n\n"

            "**🆕 未承認タグを整理**\n"
            "AIなどから追加された未承認タグを、"
            "承認または検索対象外にします。\n\n"

            "**🧩 タグを統合・修正**\n"
            "似たタグや別表記を代表タグへまとめます。"
            "通常はタグ名だけで操作できます。\n\n"

            "**📊 タグの状態を見る**\n"
            "承認済み・未承認・除外・別表記などの"
            "件数を確認します。\n\n"

            "**⚙️ 詳細管理**\n"
            "検索インデックスの再構築など、"
            "通常は使わない保守操作です。"
        ),
        color=0x5865F2,
    )

    embed.set_footer(
        text=(
            "日常的なタグ整理と、"
            "保守操作を分けています。"
        )
    )

    return embed


# =========================
# タグ統合メニュー
# =========================

class TagMergeView(
    TagMasterBaseView
):
    @discord.ui.button(
        label="タグ名で統合",
        emoji="🧩",
        style=discord.ButtonStyle.primary,
    )
    async def merge_by_name(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            TagNameMergeModal()
        )

    @discord.ui.button(
        label="似ているタグ候補",
        emoji="🔗",
        style=discord.ButtonStyle.secondary,
    )
    async def candidates(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            rows = await asyncio.to_thread(
                _load_merge_candidates
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ 統合候補の取得に失敗しました。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        lines = [
            (
                f"**{item['left']}** "
                f"↔ **{item['right']}** "
                f"（類似 {item['similarity'] * 100:.1f}%）"
            )
            for item in rows
        ]

        embed = discord.Embed(
            title="🔗 似ているタグ候補",
            description=(
                "\n".join(
                    lines
                )
                or
                "現在、統合候補はありません。"
            ),
            color=0x95A5A6,
        )

        embed.set_footer(
            text=(
                "統合する場合は"
                "「タグ名で統合」から名前を指定してください。"
            )
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="詳細：IDで統合",
        emoji="🔢",
        style=discord.ButtonStyle.secondary,
    )
    async def merge_by_id(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            MergeModal()
        )


# =========================
# 詳細管理
# =========================

class TagMaintenanceView(
    TagMasterBaseView
):
    @discord.ui.button(
        label="検索インデックスを再構築",
        emoji="🔄",
        style=discord.ButtonStyle.danger,
    )
    async def rebuild(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        try:
            result = await asyncio.to_thread(
                _rebuild_search_index
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "⚠️ 検索インデックスの再構築に失敗しました。\n"
                    f"`{type(exc).__name__}: {exc}`"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                "✅ 検索インデックスを再構築しました。\n"
                f"代表タグ **{result['tags']:,}件**"
                f" / 対応 **{result['assignments']:,}件**"
            ),
            ephemeral=True,
        )


# =========================
# タグ管理メニュー
# =========================

class TagMasterView(
    TagMasterBaseView
):
    @discord.ui.button(
        label="未承認タグを整理",
        emoji="🆕",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def pending(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        view = await PendingTagView.create(
            self.owner_id
        )

        await interaction.followup.send(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="タグを統合・修正",
        emoji="🧩",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def merge_menu(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        embed = discord.Embed(
            title="🧩 タグを統合・修正",
            description=(
                "通常は **タグ名で統合** を使ってください。\n\n"
                "例：\n"
                "`制服姿` → `制服`\n\n"
                "似ているタグ候補を先に確認することもできます。"
            ),
            color=0x95A5A6,
        )

        await interaction.response.send_message(
            embed=embed,
            view=TagMergeView(
                self.owner_id
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="タグの状態を見る",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def status(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        embed = await asyncio.to_thread(
            _summary_embed
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="詳細管理",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def maintenance(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        embed = discord.Embed(
            title="⚙️ タグ詳細管理",
            description=(
                "通常のタグ整理では使わない保守操作です。\n\n"
                "検索結果に明らかな不整合がある場合などに"
                "使用してください。"
            ),
            color=0x95A5A6,
        )

        await interaction.response.send_message(
            embed=embed,
            view=TagMaintenanceView(
                self.owner_id
            ),
            ephemeral=True,
        )


# =========================
# 外部呼び出し
# =========================

async def send_pending_tag_panel(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    view = await PendingTagView.create(
        interaction.user.id
    )

    await interaction.followup.send(
        embed=view.embed(),
        view=view,
        ephemeral=True,
    )


async def send_tag_merge_panel(
    interaction: discord.Interaction,
) -> None:
    embed = discord.Embed(
        title="🧩 タグを統合・修正",
        description=(
            "タグ名を指定して代表タグへまとめます。\n"
            "通常はID入力不要です。"
        ),
        color=0x95A5A6,
    )

    await interaction.response.send_message(
        embed=embed,
        view=TagMergeView(
            interaction.user.id
        ),
        ephemeral=True,
    )


async def send_tag_status_panel(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    embed = await asyncio.to_thread(
        _summary_embed
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


async def send_tag_master_panel(
    ctx: Any,
) -> None:
    await ctx.send(
        embed=_home_embed(),
        view=TagMasterView(
            ctx.author.id
        ),
    )
