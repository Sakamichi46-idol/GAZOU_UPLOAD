from __future__ import annotations

import asyncio
import math
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import discord

from photo_database import get_connection
from photo_search import get_display_image_url


PAGE_SIZE = 5
OPTIONS_PER_PAGE = 25

CATEGORY_DEFS: dict[str, tuple[str, str]] = {
    "person": ("ð¤", "äººç©"),
    "clothing": ("ð", "æè£"),
    "expression": ("ð", "è¡¨æ"),
    "location": ("ð", "å ´æã»èæ¯"),
    "composition": ("ð·", "æ®å½±ã»æ§å³"),
    "pose": ("ðº", "ãã¼ãº"),
    "event": ("ðª", "ã¤ãã³ã"),
    "season": ("ð¸", "å­£ç¯ã»å¤©å"),
    "object": ("ð", "å°ç©"),
    "other": ("â¨", "ãã®ä»"),
}

CATEGORY_ALIASES = {
    "background": "location",
    "weather": "season",
    "person_count": "composition",
    "manual": "other",
    "": "other",
}


def _normalized_category(category: str) -> str:
    clean = str(category or "").strip().lower()
    clean = CATEGORY_ALIASES.get(clean, clean)
    return clean if clean in CATEGORY_DEFS else "other"


def _short(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "â¦"


def _all_image_ids() -> set[int]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM photo_images
            WHERE download_status = 'completed'
              AND local_path != ''
            """
        ).fetchall()

    return {int(row[0]) for row in rows}


def _load_tag_index() -> dict[str, dict[str, set[int]]]:
    index: dict[str, dict[str, set[int]]] = {
        category: {} for category in CATEGORY_DEFS
    }

    with closing(get_connection()) as connection:
        people = connection.execute(
            """
            SELECT image_id, person_name
            FROM photo_image_people
            WHERE relation_status = 'confirmed'
              AND TRIM(person_name) != ''
            """
        ).fetchall()

        ai_tags = connection.execute(
            """
            SELECT image_id, category, tag
            FROM photo_ai_tags
            WHERE TRIM(tag) != ''
            """
        ).fetchall()

        manual_tags = connection.execute(
            """
            SELECT image_id, category, tag
            FROM photo_manual_tags
            WHERE TRIM(tag) != ''
            """
        ).fetchall()

    for image_id, person_name in people:
        tag = str(person_name).strip()
        index["person"].setdefault(tag, set()).add(int(image_id))

    for image_id, category, tag_value in [*ai_tags, *manual_tags]:
        tag = str(tag_value).strip()
        category_key = _normalized_category(str(category or ""))
        index[category_key].setdefault(tag, set()).add(int(image_id))

    return index


def _matching_ids(
    all_ids: set[int],
    index: dict[str, dict[str, set[int]]],
    selections: dict[str, set[str]],
    *,
    exclude_category: str | None = None,
) -> set[int]:
    result = set(all_ids)

    for category, selected_tags in selections.items():
        if category == exclude_category or not selected_tags:
            continue

        category_ids: set[int] = set()

        for tag in selected_tags:
            category_ids.update(
                index.get(category, {}).get(tag, set())
            )

        result.intersection_update(category_ids)

        if not result:
            break

    return result


def _option_counts(
    all_ids: set[int],
    index: dict[str, dict[str, set[int]]],
    selections: dict[str, set[str]],
    category: str,
) -> list[tuple[str, int]]:
    base = _matching_ids(
        all_ids,
        index,
        selections,
        exclude_category=category,
    )

    selected = selections.get(category, set())

    values: list[tuple[str, int]] = []

    for tag, image_ids in index.get(category, {}).items():
        count = len(base.intersection(image_ids))

        if count > 0 or tag in selected:
            values.append((tag, count))

    values.sort(
        key=lambda item: (
            item[0] not in selected,
            -item[1],
            item[0],
        )
    )

    return values


def _load_results(image_ids: set[int]) -> list[dict[str, Any]]:
    if not image_ids:
        return []

    placeholders = ",".join("?" for _ in image_ids)
    params = tuple(sorted(image_ids))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                photo_images.id,
                photo_images.image_url,
                photo_images.image_index,
                photo_images.local_path,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,

                COALESCE((
                    SELECT GROUP_CONCAT(person_name, 'ã')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'confirmed'
                ), '') AS confirmed_people,

                COALESCE(photo_ai_analysis.clothing, '') AS clothing,
                COALESCE(photo_ai_analysis.expression, '') AS expression,
                COALESCE(photo_ai_analysis.background, '') AS background,
                COALESCE(photo_ai_analysis.pose, '') AS pose,
                COALESCE(photo_ai_analysis.objects, '') AS objects,

                COALESCE((
                    SELECT GROUP_CONCAT(tag, 'ã')
                    FROM (
                        SELECT tag
                        FROM photo_ai_tags t
                        WHERE t.image_id = photo_images.id
                        ORDER BY confidence DESC, id ASC
                        LIMIT 15
                    )
                ), '') AS ai_tags,

                COALESCE((
                    SELECT GROUP_CONCAT(tag, 'ã')
                    FROM (
                        SELECT tag
                        FROM photo_manual_tags m
                        WHERE m.image_id = photo_images.id
                        ORDER BY id ASC
                        LIMIT 15
                    )
                ), '') AS manual_tags

            FROM photo_images

            JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id

            LEFT JOIN photo_ai_analysis
                ON photo_ai_analysis.image_id = photo_images.id

            WHERE photo_images.id IN ({placeholders})

            ORDER BY
                photo_blogs.published_at DESC,
                photo_images.image_index ASC,
                photo_images.id DESC
            """,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


@dataclass
class ExplorerState:
    owner_id: int

    selections: dict[str, set[str]] = field(
        default_factory=lambda: {
            key: set() for key in CATEGORY_DEFS
        }
    )

    all_ids: set[int] = field(default_factory=set)

    index: dict[str, dict[str, set[int]]] = field(
        default_factory=dict
    )

    @classmethod
    async def create(cls, owner_id: int) -> "ExplorerState":
        all_ids, index = await asyncio.gather(
            asyncio.to_thread(_all_image_ids),
            asyncio.to_thread(_load_tag_index),
        )

        return cls(
            owner_id=owner_id,
            all_ids=all_ids,
            index=index,
        )

    def result_ids(self) -> set[int]:
        return _matching_ids(
            self.all_ids,
            self.index,
            self.selections,
        )

    def selected_lines(self) -> list[str]:
        lines: list[str] = []

        for category, tags in self.selections.items():
            if not tags:
                continue

            emoji, label = CATEGORY_DEFS[category]

            lines.append(
                f"{emoji} **{label}:** {'ã»'.join(sorted(tags))}"
            )

        return lines

    def clear(self) -> None:
        for tags in self.selections.values():
            tags.clear()


class OwnedView(discord.ui.View):
    def __init__(
        self,
        state: ExplorerState,
        *,
        timeout: float = 600,
    ):
        super().__init__(timeout=timeout)
        self.state = state

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message(
                "â ï¸ ãã®æ¤ç´¢ç»é¢ã¯ãã³ãã³ããå®è¡ããæ¬äººã ããæä½ã§ãã¾ãã",
                ephemeral=True,
            )
            return False

        return True


def build_explorer_embed(
    state: ExplorerState,
) -> discord.Embed:
    result_count = len(state.result_ids())
    selected_lines = state.selected_lines()

    embed = discord.Embed(
        title="ð åçã¿ã°æ¤ç´¢",
        description=(
            "ä¸ã®ã«ãã´ãªã¼ããæ¡ä»¶ãé¸ãã§ãã ããã\n"
            "**åãã«ãã´ãªã¼åã¯ORãã«ãã´ãªã¼åå£«ã¯AND**ã§æ¤ç´¢ãã¾ãã"
        ),
        color=0x2B90D9,
    )

    embed.add_field(
        name="ð ã©ã¤ãã©ãª",
        value=(
            f"ç»é²ç»å: **{len(state.all_ids):,}æ**\n"
            f"åè£ç»å: **{result_count:,}æ**"
        ),
        inline=False,
    )

    embed.add_field(
        name="ç¾å¨ã®æ¡ä»¶",
        value=(
            "\n".join(selected_lines)
            if selected_lines
            else "æ¡ä»¶ã¯ã¾ã é¸æããã¦ãã¾ããã"
        ),
        inline=False,
    )

    recommendations: list[tuple[str, str, int]] = []
    current_ids = state.result_ids()

    for category, tag_map in state.index.items():
        selected = state.selections.get(category, set())

        for tag, ids in tag_map.items():
            if tag in selected:
                continue

            count = len(current_ids.intersection(ids))

            if count:
                recommendations.append(
                    (category, tag, count)
                )

    recommendations.sort(
        key=lambda item: (
            -item[2],
            item[1],
        )
    )

    recommendation_lines: list[str] = []

    for category, tag, count in recommendations[:5]:
        emoji, _ = CATEGORY_DEFS[category]

        recommendation_lines.append(
            f"{emoji} {tag} **({count:,})**"
        )

    embed.add_field(
        name="â¨ ããããã¿ã°",
        value=(
            "\n".join(recommendation_lines)
            if recommendation_lines
            else "ç¾å¨ã®æ¡ä»¶ã§ã¯åè£ãããã¾ããã"
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            "ã«ãã´ãªã¼ãæä½ãããã³ã«ã"
            "ç¾å¨ã®æ¡ä»¶ã«å¿ããä»¶æ°ã¸æ´æ°ããã¾ãã"
        )
    )

    return embed


class ExplorerView(OwnedView):
    def __init__(self, state: ExplorerState):
        super().__init__(state)
        self._build_items()

    def _build_items(self) -> None:
        for category, (emoji, label) in CATEGORY_DEFS.items():
            selected_count = len(
                self.state.selections.get(category, set())
            )

            tag_count = len(
                self.state.index.get(category, {})
            )

            if selected_count:
                text = f"{label} ({selected_count})"
            else:
                text = f"{label} ({tag_count})"

            button = discord.ui.Button(
                label=text[:80],
                emoji=emoji,
                style=(
                    discord.ButtonStyle.primary
                    if selected_count
                    else discord.ButtonStyle.secondary
                ),
                custom_id=f"tag_category:{category}",
            )

            async def callback(
                interaction: discord.Interaction,
                key: str = category,
            ) -> None:
                view = CategoryView(
                    self.state,
                    key,
                    page=0,
                )

                await interaction.response.edit_message(
                    embed=view.build_embed(),
                    view=view,
                )

            button.callback = callback
            self.add_item(button)

    @discord.ui.button(
        label="æ¤ç´¢çµæãè¦ã",
        emoji="ð",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def search(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        result_ids = self.state.result_ids()

        results = await asyncio.to_thread(
            _load_results,
            result_ids,
        )

        if not results:
            await interaction.response.send_message(
                "ð æ¡ä»¶ã«ä¸è´ããç»åã¯ããã¾ããã§ããã"
                "æ¡ä»¶ãæ¸ããã¦ã¿ã¦ãã ããã",
                ephemeral=True,
            )
            return

        view = ResultsView(
            self.state,
            results,
            page=0,
        )

        await interaction.response.edit_message(
            embeds=view.build_embeds(),
            view=view,
        )

    @discord.ui.button(
        label="ãªã»ãã",
        emoji="ð§¹",
        style=discord.ButtonStyle.danger,
        row=2,
    )
    async def reset(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.state.clear()

        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            embed=build_explorer_embed(self.state),
            view=view,
        )

    @discord.ui.button(
        label="çµäº",
        emoji="âï¸",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def close(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            view=self
        )

        self.stop()


class TagToggleSelect(discord.ui.Select):
    def __init__(
        self,
        parent_view: "CategoryView",
        options: list[discord.SelectOption],
    ):
        self.parent_view = parent_view

        super().__init__(
            placeholder="è¿½å ã»è§£é¤ããã¿ã°ãé¸æï¼è¤æ°å¯ï¼",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            row=0,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        selected = self.parent_view.state.selections[
            self.parent_view.category
        ]

        for value in self.values:
            try:
                tag = self.parent_view.visible_tags[
                    int(value)
                ]
            except (ValueError, IndexError):
                continue

            if tag in selected:
                selected.remove(tag)
            else:
                selected.add(tag)

        new_view = CategoryView(
            self.parent_view.state,
            self.parent_view.category,
            page=self.parent_view.page,
        )

        await interaction.response.edit_message(
            embed=new_view.build_embed(),
            view=new_view,
        )


class CategoryView(OwnedView):
    def __init__(
        self,
        state: ExplorerState,
        category: str,
        page: int = 0,
    ):
        super().__init__(state)

        self.category = category
        self.page = max(0, page)

        self.options_data = _option_counts(
            state.all_ids,
            state.index,
            state.selections,
            category,
        )

        max_page = max(
            0,
            math.ceil(
                len(self.options_data) / OPTIONS_PER_PAGE
            ) - 1,
        )

        self.page = min(
            self.page,
            max_page,
        )

        start = self.page * OPTIONS_PER_PAGE

        visible = self.options_data[
            start : start + OPTIONS_PER_PAGE
        ]

        self.visible_tags = [
            tag for tag, _count in visible
        ]

        selected = state.selections[category]

        if visible:
            options = [
                discord.SelectOption(
                    label=_short(tag, 85),
                    value=str(index),
                    description=f"è©²å½ {count:,}ä»¶"[:100],
                    emoji="â" if tag in selected else None,
                )
                for index, (tag, count) in enumerate(visible)
            ]

            self.add_item(
                TagToggleSelect(
                    self,
                    options,
                )
            )

        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= max_page
        self.clear_category.disabled = not bool(selected)

    def build_embed(self) -> discord.Embed:
        emoji, label = CATEGORY_DEFS[self.category]

        selected = sorted(
            self.state.selections[self.category]
        )

        total_pages = max(
            1,
            math.ceil(
                len(self.options_data) / OPTIONS_PER_PAGE
            ),
        )

        embed = discord.Embed(
            title=f"{emoji} {label}",
            description=(
                "ã¿ã°ãé¸ã¶ã¨è¿½å ãããä¸åº¦é¸ã¶ã¨è§£é¤ããã¾ãã\n"
                "è¤æ°ãã¾ã¨ãã¦é¸æã§ãã¾ãã"
            ),
            color=0x5865F2,
        )

        embed.add_field(
            name="ãã®ã«ãã´ãªã¼ã®é¸æ",
            value=(
                "ã»".join(selected)
                if selected
                else "æªé¸æ"
            ),
            inline=False,
        )

        embed.add_field(
            name="ç¾å¨ã®åè£ç»å",
            value=f"**{len(self.state.result_ids()):,}æ**",
            inline=True,
        )

        embed.add_field(
            name="ãã¼ã¸",
            value=f"**{self.page + 1}/{total_pages}**",
            inline=True,
        )

        if not self.options_data:
            embed.add_field(
                name="ã¿ã°",
                value=(
                    "ãã®ã«ãã´ãªã¼ã«ã¯"
                    "å©ç¨ã§ããã¿ã°ãããã¾ããã"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "åã¿ã°ã®ä»¶æ°ã¯ãã»ãã®ã«ãã´ãªã¼ã§"
                "é¸ãã æ¡ä»¶ãåæ ãã¦ãã¾ãã"
            )
        )

        return embed

    @discord.ui.button(
        label="åã¸",
        emoji="âï¸",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = CategoryView(
            self.state,
            self.category,
            self.page - 1,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="æ¬¡ã¸",
        emoji="â¶ï¸",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = CategoryView(
            self.state,
            self.category,
            self.page + 1,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="ãã®åé¡ãè§£é¤",
        emoji="ð§¹",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def clear_category(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.state.selections[
            self.category
        ].clear()

        view = CategoryView(
            self.state,
            self.category,
            0,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="Explorerã¸æ»ã",
        emoji="â©ï¸",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            embed=build_explorer_embed(self.state),
            view=view,
        )


class ResultsView(OwnedView):
    def __init__(
        self,
        state: ExplorerState,
        results: list[dict[str, Any]],
        page: int = 0,
    ):
        super().__init__(state)

        self.results = results

        max_page = max(
            0,
            math.ceil(
                len(results) / PAGE_SIZE
            ) - 1,
        )

        self.page = max(
            0,
            min(page, max_page),
        )

        self._add_number_buttons()

        self.previous.disabled = self.page <= 0

        self.next.disabled = (
            (self.page + 1) * PAGE_SIZE
            >= len(self.results)
        )

    def current_results(self) -> list[dict[str, Any]]:
        start = self.page * PAGE_SIZE

        return self.results[
            start : start + PAGE_SIZE
        ]

    def build_embeds(self) -> list[discord.Embed]:
        start = self.page * PAGE_SIZE
        embeds: list[discord.Embed] = []

        for offset, result in enumerate(
            self.current_results(),
            1,
        ):
            absolute_index = start + offset
            title = result.get("title") or "ç¡é¡"

            embed = discord.Embed(
                title=(
                    f"{absolute_index}. "
                    f"{_short(title, 220)}"
                ),
                url=result.get("blog_url") or None,
                description=(
                    f"**ç»åID:** {result.get('id')}\n"
                    f"**äººç©:** "
                    f"{result.get('confirmed_people') or 'æªç¢ºå®'}\n"
                    f"**æç¨¿è:** "
                    f"{result.get('member_name') or 'ä¸æ'}\n"
                    f"**æ¥æ:** "
                    f"{result.get('published_at') or 'ä¸æ'}"
                ),
                color=0x00AAFF,
            )

            image_url = str(
                result.get("image_url") or ""
            ).strip()

            if image_url:
                embed.set_thumbnail(
                    url=image_url
                )

            embed.set_footer(
                text=(
                    f"æ¤ç´¢çµæ "
                    f"{absolute_index}/{len(self.results)}"
                )
            )

            embeds.append(embed)

        return embeds

    def _add_number_buttons(self) -> None:
        for offset, _result in enumerate(
            self.current_results(),
            1,
        ):
            button = discord.ui.Button(
                label=str(offset),
                style=discord.ButtonStyle.primary,
                row=0,
            )

            async def callback(
                interaction: discord.Interaction,
                item_offset: int = offset,
            ) -> None:
                index = (
                    self.page * PAGE_SIZE
                    + item_offset
                    - 1
                )

                view = DetailView(
                    self.state,
                    self.results,
                    index=index,
                    return_page=self.page,
                )

                await interaction.response.edit_message(
                    embeds=[view.build_embed()],
                    view=view,
                )

            button.callback = callback
            self.add_item(button)

    @discord.ui.button(
        label="åã®5ä»¶",
        emoji="âï¸",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ResultsView(
            self.state,
            self.results,
            self.page - 1,
        )

        await interaction.response.edit_message(
            embeds=view.build_embeds(),
            view=view,
        )

    @discord.ui.button(
        label="æ¬¡ã®5ä»¶",
        emoji="â¶ï¸",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ResultsView(
            self.state,
            self.results,
            self.page + 1,
        )

        await interaction.response.edit_message(
            embeds=view.build_embeds(),
            view=view,
        )

    @discord.ui.button(
        label="æ¡ä»¶å¤æ´",
        emoji="ð§",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def explorer(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            embeds=[build_explorer_embed(self.state)],
            view=view,
        )


class DetailView(OwnedView):
    def __init__(
        self,
        state: ExplorerState,
        results: list[dict[str, Any]],
        *,
        index: int,
        return_page: int,
    ):
        super().__init__(state)

        self.results = results

        self.index = max(
            0,
            min(
                index,
                len(results) - 1,
            ),
        )

        self.return_page = return_page

        self.previous.disabled = self.index <= 0

        self.next.disabled = (
            self.index >= len(results) - 1
        )

    def build_embed(self) -> discord.Embed:
        result = self.results[self.index]

        embed = discord.Embed(
            title=_short(
                result.get("title") or "ç¡é¡",
                256,
            ),
            url=result.get("blog_url") or None,
            color=0xF1C40F,
        )

        embed.add_field(
            name="ð¼ï¸ ç»åID",
            value=str(result.get("id")),
            inline=True,
        )

        embed.add_field(
            name="ð·ï¸ ã°ã«ã¼ã",
            value=result.get("group_name") or "ä¸æ",
            inline=True,
        )

        embed.add_field(
            name="âï¸ æç¨¿è",
            value=result.get("member_name") or "ä¸æ",
            inline=True,
        )

        embed.add_field(
            name="ð¤ åã£ã¦ããäººç©",
            value=(
                result.get("confirmed_people")
                or "æªç¢ºå®"
            ),
            inline=False,
        )

        details: list[str] = []

        for label, key in (
            ("æè£", "clothing"),
            ("è¡¨æ", "expression"),
            ("å ´æã»èæ¯", "background"),
            ("ãã¼ãº", "pose"),
            ("å°ç©", "objects"),
        ):
            value = str(
                result.get(key) or ""
            ).strip()

            if value:
                details.append(
                    f"**{label}:** {value}"
                )

        if details:
            embed.add_field(
                name="ð AIè§£æ",
                value=_short(
                    "\n".join(details),
                    1024,
                ),
                inline=False,
            )

        tag_lines: list[str] = []

        if result.get("ai_tags"):
            tag_lines.append(
                f"**AI:** {result['ai_tags']}"
            )

        if result.get("manual_tags"):
            tag_lines.append(
                f"**æå:** {result['manual_tags']}"
            )

        if tag_lines:
            embed.add_field(
                name="ð·ï¸ ã¿ã°",
                value=_short(
                    "\n".join(tag_lines),
                    1024,
                ),
                inline=False,
            )

        embed.add_field(
            name="ð æç¨¿æ¥æ",
            value=result.get("published_at") or "ä¸æ",
            inline=False,
        )

        image_url = get_display_image_url(result)

        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(
            text=(
                f"æ¤ç´¢çµæ "
                f"{self.index + 1}/{len(self.results)}"
                f"ã»è¨äºå "
                f"{result.get('image_index', 0)}æç®"
            )
        )

        return embed

    @discord.ui.button(
        label="åã®ç»å",
        emoji="âï¸",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = DetailView(
            self.state,
            self.results,
            index=self.index - 1,
            return_page=self.return_page,
        )

        await interaction.response.edit_message(
            embeds=[view.build_embed()],
            view=view,
        )

    @discord.ui.button(
        label="æ¬¡ã®ç»å",
        emoji="â¶ï¸",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = DetailView(
            self.state,
            self.results,
            index=self.index + 1,
            return_page=self.return_page,
        )

        await interaction.response.edit_message(
            embeds=[view.build_embed()],
            view=view,
        )

    @discord.ui.button(
        label="ä¸è¦§ã¸æ»ã",
        emoji="ðï¸",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def list_back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ResultsView(
            self.state,
            self.results,
            self.index // PAGE_SIZE,
        )

        await interaction.response.edit_message(
            embeds=view.build_embeds(),
            view=view,
        )

    @discord.ui.button(
        label="æ¡ä»¶å¤æ´",
        emoji="ð§",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def explorer(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            embeds=[build_explorer_embed(self.state)],
            view=view,
        )


async def send_photo_tag_explorer(ctx) -> None:
    message = await ctx.send(
        "ð ã¿ã°æå ±ãèª­ã¿è¾¼ãã§ãã¾ãâ¦"
    )

    try:
        state = await ExplorerState.create(
            ctx.author.id
        )

        view = ExplorerView(state)

        await message.edit(
            content=None,
            embed=build_explorer_embed(state),
            view=view,
        )

    except Exception as error:
        await message.edit(
            content=(
                "â ï¸ ã¿ã°æ¤ç´¢ç»é¢ã®ä½æã«å¤±æãã¾ããã\n"
                f"`{type(error).__name__}: {error}`"
            )
        )
