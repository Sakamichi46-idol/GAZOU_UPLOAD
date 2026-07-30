import asyncio
import os
import traceback

import aiohttp

from archive_config import (
    ARCHIVE_TARGET_GROUP,
    ARCHIVE_TEST_LIMIT,
)

from archive_database import (
    filter_not_archived,
)

from archive_parsers.utils import (
    blog_datetime_key,
)

from archive_parsers.nogizaka import (
    get_oldest_first as get_nogizaka,
    enrich_all_details as enrich_nogizaka_details,
    get_unregistered_candidates as get_nogizaka_candidates,
)

from archive_parsers.sakurazaka import (
    get_oldest_first as get_sakurazaka,
    enrich_all_details as enrich_sakurazaka_details,
    get_unregistered_candidates as get_sakurazaka_candidates,
)

from archive_parsers.hinatazaka import (
    get_oldest_first as get_hinatazaka,
    enrich_all_details as enrich_hinatazaka_details,
    get_unregistered_candidates as get_hinatazaka_candidates,
)


# =========================
# 2レーン設定
# =========================

ARCHIVE_PRIORITY_LIMIT = max(
    int(
        os.getenv(
            "ARCHIVE_PRIORITY_LIMIT",
            "5",
        )
    ),
    0,
)


# =========================
# パーサー一覧
# =========================

ALL_PARSERS = {
    "乃木坂46": get_nogizaka,
    "櫻坂46": get_sakurazaka,
    "日向坂46": get_hinatazaka,
}


# =========================
# グループ指定
# =========================

GROUP_ALIASES = {
    "nogizaka": "乃木坂46",
    "nogizaka46": "乃木坂46",
    "乃木坂": "乃木坂46",
    "乃木坂46": "乃木坂46",
    "sakurazaka": "櫻坂46",
    "sakurazaka46": "櫻坂46",
    "櫻坂": "櫻坂46",
    "櫻坂46": "櫻坂46",
    "hinatazaka": "日向坂46",
    "hinatazaka46": "日向坂46",
    "日向坂": "日向坂46",
    "日向坂46": "日向坂46",
}


# =========================
# 使用するパーサーを選択
# =========================

def get_selected_parsers():
    target = (
        ARCHIVE_TARGET_GROUP
        or "all"
    ).strip().lower()

    if target == "all":
        return ALL_PARSERS.copy()

    group_name = GROUP_ALIASES.get(target)

    if not group_name:
        print(
            "⚠️ ARCHIVE_TARGET_GROUPの値が不正です:",
            ARCHIVE_TARGET_GROUP,
        )
        print(
            "利用可能な値: "
            "all / nogizaka / sakurazaka / hinatazaka"
        )
        return {}

    parser = ALL_PARSERS.get(group_name)

    if not parser:
        return {}

    return {group_name: parser}


# =========================
# 1グループ取得
# =========================

async def run_parser(group, parser, session, *, enrich_details=True):
    print("=" * 50)
    print(f"[{group}] 巡回開始")

    try:
        result = await parser(
            session,
            enrich_details=enrich_details,
        )

        if not result:
            print(f"[{group}] 記事なし")
            return []

        valid_blogs = []

        for blog in result:
            if not isinstance(blog, dict):
                continue

            url = str(blog.get("url", "")).strip()

            if not url:
                continue

            blog["url"] = url

            if not blog.get("group"):
                blog["group"] = group

            valid_blogs.append(blog)

        print(f"[{group}] {len(valid_blogs)}件取得")
        return valid_blogs

    except asyncio.CancelledError:
        print(f"[{group}] 巡回が停止されました。")
        raise

    except Exception as error:
        print(f"[{group}] 取得エラー:", error)
        traceback.print_exc()
        return []


# =========================
# 全対象グループ取得
# =========================

async def get_all_blogs(*, enrich_details=True):
    selected_parsers = get_selected_parsers()

    if not selected_parsers:
        print("実行対象のグループがありません。")
        return []

    print(
        "今回の対象グループ:",
        ", ".join(selected_parsers.keys()),
    )

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_read=60,
    )

    connector = aiohttp.TCPConnector(
        limit=20,
        limit_per_host=8,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        tasks = [
            run_parser(
                group,
                parser,
                session,
                enrich_details=enrich_details,
            )
            for group, parser in selected_parsers.items()
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    blogs = []

    for group, result in zip(
        selected_parsers.keys(),
        results,
    ):
        if isinstance(result, asyncio.CancelledError):
            raise result

        if isinstance(result, Exception):
            print(f"[{group}] タスク実行エラー:", result)
            continue

        blogs.extend(result)

    print("=" * 50)
    print(f"取得合計: {len(blogs)}件")
    return blogs


# =========================
# 選択記事だけ詳細取得
# =========================

DETAIL_ENRICHERS = {
    "乃木坂46": enrich_nogizaka_details,
    "櫻坂46": enrich_sakurazaka_details,
    "日向坂46": enrich_hinatazaka_details,
}


async def enrich_selected_blogs(blogs):
    """
    渡された記事だけ詳細ページを取得して情報を補完する。

    写真アーカイブでは、一覧URLをDB照合した後の少数記事だけを
    詳細取得するために使用する。
    """

    if not blogs:
        return []

    grouped = {}
    original_order = []

    for blog in blogs:
        if not isinstance(blog, dict):
            continue

        url = str(blog.get("url", "")).strip()
        group = str(blog.get("group", "")).strip()

        if not url:
            continue

        original_order.append(url)
        grouped.setdefault(group, []).append(blog)

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_read=60,
    )

    connector = aiohttp.TCPConnector(
        limit=10,
        limit_per_host=5,
        ttl_dns_cache=300,
    )

    enriched_by_url = {}

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:
        for group, group_blogs in grouped.items():
            enricher = DETAIL_ENRICHERS.get(group)

            if enricher is None:
                for blog in group_blogs:
                    enriched_by_url[blog.get("url", "")] = blog
                continue

            print(
                f"[{group}] DB未登録候補だけ詳細取得: "
                f"{len(group_blogs)}件"
            )

            try:
                enriched = await enricher(session, group_blogs)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[{group}] 選択記事の詳細取得エラー:", error)
                traceback.print_exc()
                enriched = group_blogs

            for blog in enriched:
                url = str(blog.get("url", "")).strip()
                if url:
                    enriched_by_url[url] = blog

    return [
        enriched_by_url[url]
        for url in original_order
        if url in enriched_by_url
    ]


# =========================
# URL重複除去
# =========================

def remove_duplicate_urls(blogs):
    unique_blogs = []
    seen_urls = set()

    for blog in blogs:
        if not isinstance(blog, dict):
            continue

        url = str(blog.get("url", "")).strip()

        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        unique_blogs.append(blog)

    return unique_blogs


# =========================
# 記事ログ
# =========================

def print_blog_list(title, blogs):
    print("=" * 50)
    print(f"{title}: {len(blogs)}件")

    for index, blog in enumerate(blogs, start=1):
        print(
            f"{index}. "
            f"{blog.get('date', '不明')} / "
            f"{blog.get('group', '不明')} / "
            f"{blog.get('member', '不明')} / "
            f"{blog.get('title', '無題')}"
        )

    print("=" * 50)


# =========================
# 写真アーカイブ対象を取得
# =========================

async def get_photo_archive_targets():
    """
    全グループの記事を取得し、URL重複を除外して古い順で返す。

    archive.dbの通知済み履歴は参照しない。
    写真アーカイブ側でphoto_archive.dbを確認するため、
    Discordへの再通知判定には影響しない。
    """

    blogs = await get_all_blogs()

    if not blogs:
        print("写真アーカイブ用の記事を取得できませんでした。")
        return []

    before_duplicate_count = len(blogs)
    blogs = remove_duplicate_urls(blogs)

    print(
        "写真アーカイブURL重複除去:",
        f"{before_duplicate_count}件",
        "→",
        f"{len(blogs)}件",
    )

    blogs.sort(key=blog_datetime_key)

    if blogs:
        print(
            "写真アーカイブ対象の最古:",
            blogs[0].get("date", "不明"),
            blogs[0].get("url", ""),
        )
        print(
            "写真アーカイブ対象の最新:",
            blogs[-1].get("date", "不明"),
            blogs[-1].get("url", ""),
        )

    return blogs


# =========================
# 未保存記事を取得
# =========================

async def get_unarchived_blogs():
    """
    全グループの記事を取得して、
    URL重複とDB登録済み記事を除外する。

    戻り値は古い順。
    """

    blogs = await get_all_blogs()

    if not blogs:
        print("取得できたブログはありません。")
        return []

    before_duplicate_count = len(blogs)
    blogs = remove_duplicate_urls(blogs)

    print(
        "URL重複除去:",
        f"{before_duplicate_count}件",
        "→",
        f"{len(blogs)}件",
    )

    blogs.sort(key=blog_datetime_key)

    if blogs:
        print(
            "取得記事の最古:",
            blogs[0].get("date", "不明"),
            blogs[0].get("url", ""),
        )
        print(
            "取得記事の最新:",
            blogs[-1].get("date", "不明"),
            blogs[-1].get("url", ""),
        )

    print("DB登録済みURLを確認中...")

    before_archive_filter = len(blogs)
    blogs = filter_not_archived(blogs)

    print(
        "未保存記事:",
        f"{before_archive_filter}件",
        "→",
        f"{len(blogs)}件",
    )

    blogs.sort(key=blog_datetime_key)
    return blogs


# =========================
# 2レーンへ振り分け
# =========================

async def get_archive_lanes():
    """
    未保存記事を次の2レーンへ分割する。

    priority:
        新しい未保存記事。
        最新側から最大ARCHIVE_PRIORITY_LIMIT件。

    history:
        priorityに含まれなかった過去記事。
        古い順に処理する。
    """

    blogs = await get_unarchived_blogs()

    if not blogs:
        return {
            "priority": [],
            "history": [],
        }

    if ARCHIVE_PRIORITY_LIMIT > 0:
        priority_blogs = blogs[-ARCHIVE_PRIORITY_LIMIT:]
    else:
        priority_blogs = []

    priority_blogs = list(reversed(priority_blogs))

    priority_urls = {
        blog.get("url", "")
        for blog in priority_blogs
    }

    history_blogs = [
        blog
        for blog in blogs
        if blog.get("url", "") not in priority_urls
    ]

    history_blogs.sort(key=blog_datetime_key)

    if ARCHIVE_TEST_LIMIT > 0:
        original_count = len(history_blogs)
        history_blogs = history_blogs[:ARCHIVE_TEST_LIMIT]

        print(
            "過去記事レーン件数制限:",
            f"{original_count}件",
            "→",
            f"{len(history_blogs)}件",
        )

    print_blog_list("🚀 新着優先レーン", priority_blogs)
    print_blog_list("📚 過去記事レーン", history_blogs)

    print(
        "2レーン合計:",
        f"{len(priority_blogs)}件",
        "+",
        f"{len(history_blogs)}件",
        "=",
        f"{len(priority_blogs) + len(history_blogs)}件",
    )

    return {
        "priority": priority_blogs,
        "history": history_blogs,
    }


# =========================
# 新着優先レーンのみ取得
# =========================

async def get_priority_targets():
    lanes = await get_archive_lanes()
    return lanes["priority"]


# =========================
# 従来のarchive_main互換
# =========================

async def get_archive_targets():
    """
    未保存の記事を、全グループ混在で古い順に返す。

    乃木坂46・櫻坂46・日向坂46をまとめて、
    投稿日時が最も古い記事から順番に処理する。
    """

    blogs = await get_unarchived_blogs()

    if not blogs:
        print("今回の送信対象はありません。")
        return []

    blogs.sort(key=blog_datetime_key)

    if ARCHIVE_TEST_LIMIT > 0:
        original_count = len(blogs)
        blogs = blogs[:ARCHIVE_TEST_LIMIT]

        print(
            "アーカイブ対象件数制限:",
            f"{original_count}件",
            "→",
            f"{len(blogs)}件",
        )

    print_blog_list(
        "今回の送信対象（古い順）",
        blogs,
    )

    return blogs


# =========================
# 写真アーカイブ向け差分巡回
# =========================

PHOTO_CANDIDATE_PARSERS = {
    "乃木坂46": get_nogizaka_candidates,
    "櫻坂46": get_sakurazaka_candidates,
    "日向坂46": get_hinatazaka_candidates,
}


def _select_photo_candidate_parsers(group_filter: str):
    """通常の対象グループ設定と写真側の指定を両方反映する。"""

    selected = get_selected_parsers()
    normalized = str(group_filter or "all").strip()

    result = {}
    for group in selected:
        if normalized != "all" and group != normalized:
            continue
        parser = PHOTO_CANDIDATE_PARSERS.get(group)
        if parser is not None:
            result[group] = parser
    return result


async def get_photo_archive_candidates(
    *,
    known_urls: set[str],
    target_count: int,
    group_filter: str = "all",
):
    """
    最新側から一覧を巡回し、DB未登録候補が必要数に達した時点で止める。

    全記事一覧や全詳細ページを毎回取得しないため、
    写真アーカイブの通信量と実行時間を抑えられる。
    """

    target_count = max(int(target_count), 1)
    selected = _select_photo_candidate_parsers(group_filter)

    if not selected:
        print("写真差分巡回の対象グループがありません。")
        return []

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_read=60,
    )
    connector = aiohttp.TCPConnector(
        limit=12,
        limit_per_host=4,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            parser(session, known_urls, target_count)
            for parser in selected.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = []
    for group, result in zip(selected.keys(), results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            print(f"[{group}] 写真差分巡回エラー:", result)
            traceback.print_exception(type(result), result, result.__traceback__)
            continue
        for blog in result:
            if isinstance(blog, dict):
                blog.setdefault("group", group)
                candidates.append(blog)

    # 各グループから集めた候補を日付の新しい順で統合する。
    candidates.sort(key=blog_datetime_key, reverse=True)

    unique = []
    seen = set()
    for blog in candidates:
        url = str(blog.get("url", "")).strip()
        if not url or url in seen or url in known_urls:
            continue
        seen.add(url)
        unique.append(blog)
        if len(unique) >= target_count:
            break

    print(
        "写真差分巡回結果:",
        f"候補{len(unique)}件 / 登録済みURL{len(known_urls)}件",
    )
    return unique
