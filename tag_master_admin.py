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
)


PAGE_SIZE = 20


def _summary_embed() -> discord.Embed:
    with closing(get_connection()) as con:
        bootstrap_from_existing(con)
        stats = diagnostic_summary(con)

    embed = discord.Embed(
        title="🏷️ タグマスター管理",
        color=0xF1C40F,
    )

    embed.description = (
        "原文タグを削除せず、"
        "代表タグ・同義語・承認状態・検索対象を管理します。"
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
        name="低信頼割当",
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
            "手動タグ > 承認済みAIタグ > 未承認AIタグ "
            "の優先順位です。"
        )
    )

    return embed


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
                WHERE status='pending'
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
                        a.canonical_tag_id=m.id
                ) AS aliases
            FROM tag_master m
            WHERE m.status='pending'
            ORDER BY
                aliases DESC,
                m.id
            LIMIT ?
            OFFSET ?
            """,
            (
                PAGE_SIZE,
                safe_page * PAGE_SIZE,
            ),
        ).fetchall()

    return total, rows


def _load_merge_candidates() -> list[Any]:
    with closing(get_connection()) as con:
        return merge_candidates(
            con,
            25,
        )


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


class MergeModal(
    discord.ui.Modal,
    title="代表タグを統合",
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
        interaction:
            discord.Interaction,
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
            await (
                interaction.response
                .send_message(
                    "⚠️ 統合元IDと統合先IDは数字で入力してください。",
                    ephemeral=True,
                )
            )
            return

        if source == target:
            await (
                interaction.response
                .send_message(
                    "⚠️ 統合元と統合先に同じIDは指定できません。",
                    ephemeral=True,
                )
            )
            return

        await (
            interaction.response
            .defer(
                ephemeral=True,
                thinking=True,
            )
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
            await (
                interaction.followup
                .send(
                    (
                        "⚠️ 統合できませんでした。\n"
                        f"`{type(exc).__name__}: {exc}`"
                    ),
                    ephemeral=True,
                )
            )
            return

        await (
            interaction.followup
            .send(
                (
                    "✅ 元データを削除せず、"
                    "別名の向き先を代表タグへ統合しました。"
                ),
                ephemeral=True,
            )
        )


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
                    f"{row[2]} / 別表記{row[3]}件"
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
        interaction:
            discord.Interaction,
    ) -> None:
        await (
            interaction.response
            .defer(
                ephemeral=True,
                thinking=True,
            )
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
            await (
                interaction.followup
                .send(
                    (
                        "⚠️ タグ状態の変更に失敗しました。\n"
                        f"`{type(exc).__name__}: {exc}`"
                    ),
                    ephemeral=True,
                )
            )
            return

        action_name = (
            "承認"
            if self.action == "approve"
            else "検索対象外"
        )

        await (
            interaction.followup
            .send(
                (
                    f"✅ {len(ids)}件を"
                    f"{action_name}にしました。"
                ),
                ephemeral=True,
            )
        )


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
            total, rows = (
                _load_pending_tags(
                    self.page
                )
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

        total, rows = (
            await asyncio.to_thread(
                _load_pending_tags,
                safe_page,
            )
        )

        return cls(
            owner_id,
            safe_page,
            total=total,
            rows=rows,
        )

    async def interaction_check(
        self,
        interaction:
            discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await (
            interaction.response
            .send_message(
                "この画面は開いた管理者だけが操作できます。",
                ephemeral=True,
            )
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
                f"— {row[2]} / "
                f"別表記{row[3]}件"
            )
            for row in self.rows
        ]

        embed = discord.Embed(
            title="🆕 未承認タグ",
            description=(
                "\n".join(
                    lines
                )
                or
                "未承認タグはありません。"
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
                f"{min(self.page + 1, total_pages)}/{total_pages}ページ"
            )
        )

        return embed

    @discord.ui.button(
        label="前へ",
        emoji="◀️",
        style=
            discord.ButtonStyle.secondary,
        row=2,
    )
    async def previous(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer()
        )

        view = (
            await PendingTagView.create(
                self.owner_id,
                self.page - 1,
            )
        )

        await (
            interaction
            .edit_original_response(
                embed=view.embed(),
                view=view,
            )
        )

    @discord.ui.button(
        label="次へ",
        emoji="▶️",
        style=
            discord.ButtonStyle.secondary,
        row=2,
    )
    async def next(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer()
        )

        view = (
            await PendingTagView.create(
                self.owner_id,
                self.page + 1,
            )
        )

        await (
            interaction
            .edit_original_response(
                embed=view.embed(),
                view=view,
            )
        )


class TagMasterView(
    discord.ui.View
):
    def __init__(
        self,
        owner_id: int,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(
            owner_id
        )

    async def interaction_check(
        self,
        interaction:
            discord.Interaction,
    ) -> bool:
        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await (
            interaction.response
            .send_message(
                "この画面は開いた管理者だけが操作できます。",
                ephemeral=True,
            )
        )

        return False

    @discord.ui.button(
        label="未承認タグ",
        emoji="🆕",
        style=
            discord.ButtonStyle.primary,
    )
    async def pending(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer(
                ephemeral=True,
                thinking=True,
            )
        )

        view = (
            await PendingTagView.create(
                self.owner_id
            )
        )

        await (
            interaction.followup
            .send(
                embed=view.embed(),
                view=view,
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="統合候補",
        emoji="🔗",
        style=
            discord.ButtonStyle.secondary,
    )
    async def candidates(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer(
                ephemeral=True,
                thinking=True,
            )
        )

        try:
            rows = (
                await asyncio.to_thread(
                    _load_merge_candidates
                )
            )

        except Exception as exc:
            await (
                interaction.followup
                .send(
                    (
                        "⚠️ 統合候補の取得に失敗しました。\n"
                        f"`{type(exc).__name__}: {exc}`"
                    ),
                    ephemeral=True,
                )
            )
            return

        lines = [
            (
                f"`{item['left_id']}` "
                f"{item['left']} "
                f"↔ "
                f"`{item['right_id']}` "
                f"{item['right']}"
                f"（{item['similarity'] * 100:.1f}%）"
            )
            for item in rows
        ]

        await (
            interaction.followup
            .send(
                embed=discord.Embed(
                    title="🔗 統合候補",
                    description=(
                        "\n".join(
                            lines
                        )
                        or
                        "候補なし"
                    ),
                    color=0x5865F2,
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="ID指定で統合",
        emoji="🧩",
        style=
            discord.ButtonStyle.success,
    )
    async def merge(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .send_modal(
                MergeModal()
            )
        )

    @discord.ui.button(
        label="検索索引を再構築",
        emoji="🔄",
        style=
            discord.ButtonStyle.secondary,
    )
    async def rebuild(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer(
                ephemeral=True,
                thinking=True,
            )
        )

        try:
            result = (
                await asyncio.to_thread(
                    _rebuild_search_index
                )
            )

        except Exception as exc:
            await (
                interaction.followup
                .send(
                    (
                        "⚠️ 検索索引の再構築に失敗しました。\n"
                        f"`{type(exc).__name__}: {exc}`"
                    ),
                    ephemeral=True,
                )
            )
            return

        await (
            interaction.followup
            .send(
                (
                    "✅ 検索索引を再構築しました。\n"
                    f"代表タグ {result['tags']:,}件\n"
                    f"対応 {result['assignments']:,}件"
                ),
                ephemeral=True,
            )
        )

    @discord.ui.button(
        label="更新",
        emoji="♻️",
        style=
            discord.ButtonStyle.secondary,
    )
    async def refresh(
        self,
        interaction:
            discord.Interaction,
        _:
            discord.ui.Button,
    ) -> None:
        await (
            interaction.response
            .defer()
        )

        embed = (
            await asyncio.to_thread(
                _summary_embed
            )
        )

        await (
            interaction
            .edit_original_response(
                embed=embed,
                view=TagMasterView(
                    self.owner_id
                ),
            )
        )


async def send_tag_master_panel(
    ctx: Any,
) -> None:
    embed = (
        await asyncio.to_thread(
            _summary_embed
        )
    )

    await (
        ctx.send(
            embed=embed,
            view=TagMasterView(
                ctx.author.id
            ),
        )
    )
