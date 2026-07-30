import asyncio
import re

from datetime import datetime
from urllib.parse import (
    parse_qs,
    urljoin,
    urlsplit,
    urlunsplit,
)

import aiohttp
from bs4 import BeautifulSoup

from archive_config import (
    ARCHIVE_TEST_LIMIT,
    DETAIL_REQUEST_DELAY,
    HTTP_TIMEOUT,
    PAGE_REQUEST_DELAY,
)

from archive_parsers.utils import (
    normalize_datetime,
    normalize_member_name,
)


# =========================
# 基本設定
# =========================

BASE_URL = "https://www.hinatazaka46.com"

BLOG_LIST_URL = (
    "https://www.hinatazaka46.com/"
    "s/official/diary/member/list"
)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": (
        "https://www.hinatazaka46.com/"
        "s/official/diary/member/list"
    ),
}


# 詳細ページの同時取得数
DETAIL_CONCURRENCY = 5


# =========================
# URL正規化
# =========================

def normalize_blog_url(
    url: str
) -> str:
    """
    imaやcdなどの変動するクエリを削除する。

    例:
    detail/70143?ima=0000&cd=member
        ↓
    detail/70143
    """

    if not url:
        return ""

    full_url = urljoin(
        BASE_URL,
        url
    )

    parts = urlsplit(
        full_url
    )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            "",
            ""
        )
    )


# =========================
# 日時ソート用
# =========================

def datetime_key(
    blog: dict
) -> datetime:
    """
    ブログのdateをdatetimeへ変換する。

    変換できない場合は最後へ回す。
    """

    date_text = blog.get(
        "date",
        ""
    )

    formats = (
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
    )

    for date_format in formats:

        try:

            return datetime.strptime(
                date_text,
                date_format
            )

        except ValueError:

            continue

    return datetime.max


# =========================
# HTML取得
# =========================

async def fetch_html(
    session: aiohttp.ClientSession,
    url: str,
) -> str:

    timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT
    )

    async with session.get(
        url,
        headers=HEADERS,
        timeout=timeout,
        allow_redirects=True
    ) as response:

        response.raise_for_status()

        return await response.text(
            errors="replace"
        )


# =========================
# 最大ページ取得
# =========================

async def get_max_page(
    session: aiohttp.ClientSession
) -> int:

    url = (
        f"{BLOG_LIST_URL}"
        "?ima=0000&page=0"
    )

    try:

        html = await fetch_html(
            session,
            url
        )

    except Exception as e:

        print(
            "日向坂 最大ページ取得エラー:",
            e
        )

        return 850


    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    max_page = 0


    pager_links = soup.select(
        ".c-pager__item a[href], "
        ".p-pager a[href], "
        ".c-pager a[href], "
        "a[href*='page=']"
    )


    for anchor in pager_links:

        href = anchor.get(
            "href",
            ""
        )

        if not href:
            continue


        query = parse_qs(
            urlsplit(href).query
        )

        page_values = query.get(
            "page"
        )

        if not page_values:
            continue


        try:

            page_number = int(
                page_values[0]
            )

        except (
            TypeError,
            ValueError
        ):

            continue


        max_page = max(
            max_page,
            page_number
        )


    if max_page <= 0:

        print(
            "⚠️ 日向坂の最大ページを"
            "HTMLから取得できませんでした。"
            " 暫定値850を使用します。"
        )

        return 850


    print(
        f"日向坂 最大ページ: {max_page}"
    )

    return max_page


# =========================
# 記事カード解析
# =========================

def parse_blog_card(
    card
) -> dict | None:
    """
    一覧ページの記事カードから、

    ・URL
    ・メンバー
    ・タイトル
    ・日付

    を取得する。
    """

    link_tag = card.select_one(
        "a[href*='/diary/detail/']"
    )


    if not link_tag:

        if (
            getattr(
                card,
                "name",
                None
            ) == "a"
            and "/diary/detail/"
            in card.get(
                "href",
                ""
            )
        ):

            link_tag = card

        else:

            return None


    href = link_tag.get(
        "href",
        ""
    )

    blog_url = normalize_blog_url(
        href
    )


    if not blog_url:
        return None


    title_tag = (
        card.select_one(
            ".c-blog-top__title"
        )
        or card.select_one(
            ".c-blog-article__title"
        )
        or card.select_one(
            ".p-blog-top__title"
        )
        or card.select_one(
            ".title"
        )
        or card.select_one(
            "h3"
        )
        or card.select_one(
            "h2"
        )
    )


    member_tag = (
        card.select_one(
            ".c-blog-top__name"
        )
        or card.select_one(
            ".c-blog-article__name"
        )
        or card.select_one(
            ".p-blog-top__name"
        )
        or card.select_one(
            ".name"
        )
    )


    date_tag = (
        card.select_one(
            ".c-blog-article__date time"
        )
        or card.select_one(
            ".c-blog-top__date time"
        )
        or card.select_one(
            ".p-blog-top__date time"
        )
        or card.select_one(
            "time"
        )
        or card.select_one(
            ".date"
        )
    )


    title = (
        title_tag.get_text(
            " ",
            strip=True
        )
        if title_tag
        else ""
    )


    member = normalize_member_name(
        member_tag.get_text(
            " ",
            strip=True
        )
        if member_tag
        else ""
    )


    raw_date = (
        date_tag.get_text(
            " ",
            strip=True
        )
        if date_tag
        else ""
    )


    date = normalize_datetime(
        raw_date
    )


    return {
        "group": "日向坂46",
        "url": blog_url,
        "member": member,
        "title": title,
        "date": date,
        "text": ""
    }


# =========================
# 一覧1ページ取得
# =========================

async def get_page_blogs(
    session: aiohttp.ClientSession,
    page: int,
) -> list[dict]:

    url = (
        f"{BLOG_LIST_URL}"
        f"?ima=0000&page={page}"
    )


    try:

        html = await fetch_html(
            session,
            url
        )

    except Exception as e:

        print(
            f"日向坂 page={page} 取得エラー:",
            e
        )

        return []


    soup = BeautifulSoup(
        html,
        "html.parser"
    )


    cards = soup.select(
        "li.p-blog-top__item"
    )


    if not cards:

        cards = soup.select(
            ".p-blog-top__item"
        )


    if not cards:

        cards = soup.select(
            "li.c-blog-top__item"
        )


    if not cards:

        cards = soup.select(
            "article"
        )


    if not cards:

        cards = soup.select(
            "a[href*='/diary/detail/']"
        )


    page_blogs = []

    seen_urls = set()


    for card in cards:

        blog = parse_blog_card(
            card
        )

        if not blog:
            continue


        blog_url = blog.get(
            "url",
            ""
        )


        if not blog_url:
            continue


        if blog_url in seen_urls:
            continue


        seen_urls.add(
            blog_url
        )

        page_blogs.append(
            blog
        )


    # 一覧ページ内は新しい順なので反転
    page_blogs.reverse()


    print(
        f"日向坂 page={page}: "
        f"{len(page_blogs)}件"
    )


    return page_blogs


# =========================
# 詳細ページ日時抽出
# =========================

def extract_detail_datetime(
    soup: BeautifulSoup,
    fallback_date: str = ""
) -> str:
    """
    日向坂46の詳細ページから
    時刻付き投稿日時を取得する。

    例:
        2026.7.9 14:37
            ↓
        2026年07月09日 14:37
    """

    # 最初に、日付要素を優先して確認する
    date_selectors = (
        ".c-blog-article__date time",
        ".c-blog-article__date",
        ".p-blog-article__date time",
        ".p-blog-article__date",
        "time[datetime]",
        "time",
    )


    for selector in date_selectors:

        for date_tag in soup.select(
            selector
        ):

            raw_values = []


            datetime_attribute = date_tag.get(
                "datetime",
                ""
            )

            if datetime_attribute:

                raw_values.append(
                    datetime_attribute
                )


            date_text = date_tag.get_text(
                " ",
                strip=True
            )

            if date_text:

                raw_values.append(
                    date_text
                )


            for raw_value in raw_values:

                parsed = parse_hinata_datetime_text(
                    raw_value
                )

                if parsed:

                    return parsed


    # metaタグを確認する
    attribute_selectors = (
        "meta[property='article:published_time']",
        "meta[name='article:published_time']",
        "meta[property='og:published_time']",
        "meta[itemprop='datePublished']",
    )


    for selector in attribute_selectors:

        for tag in soup.select(
            selector
        ):

            raw_value = tag.get(
                "content",
                ""
            )

            if not raw_value:
                continue


            parsed = parse_hinata_datetime_text(
                raw_value
            )

            if parsed:

                return parsed


    # ページ全体のテキストから探す
    page_text = soup.get_text(
        " ",
        strip=True
    )


    parsed = parse_hinata_datetime_text(
        page_text
    )


    if parsed:

        return parsed


    # 時刻が見つからなければ一覧の日付を使う
    fallback_normalized = normalize_datetime(
        fallback_date
    )


    if fallback_normalized:

        return fallback_normalized


    return fallback_date


# =========================
# 日向坂日時文字列の解析
# =========================

def parse_hinata_datetime_text(
    text: str
) -> str:
    """
    日向坂で使われる複数の日付形式から
    時刻付き日時を取り出す。
    """

    if not text:
        return ""


    cleaned_text = (
        text
        .replace("\u3000", " ")
        .replace("\xa0", " ")
    )


    datetime_patterns = (
        # 2026.7.9 14:37
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2026/7/9 14:37
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*/\s*"
        r"(\d{1,2})"
        r"\s*/\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2026-07-09 14:37
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*-\s*"
        r"(\d{1,2})"
        r"\s*-\s*"
        r"(\d{1,2})"
        r"[T\s]+"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2026年7月9日 14:37
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*年\s*"
        r"(\d{1,2})"
        r"\s*月\s*"
        r"(\d{1,2})"
        r"\s*日\s*"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"(?!\d)",
    )


    for pattern in datetime_patterns:

        match = re.search(
            pattern,
            cleaned_text
        )


        if not match:
            continue


        try:

            parsed_datetime = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5))
            )

        except ValueError:

            continue


        return parsed_datetime.strftime(
            "%Y年%m月%d日 %H:%M"
        )


    # ISO 8601形式などはnormalize_datetimeにも渡す
    normalized = normalize_datetime(
        cleaned_text
    )


    if (
        normalized
        and re.search(
            r"\d{1,2}:\d{2}",
            normalized
        )
    ):

        return normalized


    return ""


# =========================
# 詳細ページ情報補完
# =========================

async def enrich_blog_detail(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    blog: dict,
    index: int,
    total: int,
) -> dict:

    url = blog.get(
        "url",
        ""
    )


    if not url:
        return blog


    async with semaphore:

        try:

            html = await fetch_html(
                session,
                url
            )


            soup = BeautifulSoup(
                html,
                "html.parser"
            )


            # -------------------------
            # 投稿日時
            # -------------------------

            detail_date = extract_detail_datetime(
                soup,
                blog.get(
                    "date",
                    ""
                )
            )


            if detail_date:

                blog["date"] = detail_date


            # -------------------------
            # メンバー
            # -------------------------

            member_tag = (
                soup.select_one(
                    ".c-blog-article__name a"
                )
                or soup.select_one(
                    ".c-blog-article__name"
                )
                or soup.select_one(
                    ".p-blog-article__name a"
                )
                or soup.select_one(
                    ".p-blog-article__name"
                )
                or soup.select_one(
                    ".blog-name"
                )
            )


            if member_tag:

                member = normalize_member_name(
                    member_tag.get_text(
                        " ",
                        strip=True
                    )
                )


                if member:

                    blog["member"] = member


            # -------------------------
            # タイトル
            # -------------------------

            title_tag = (
                soup.select_one(
                    ".c-blog-article__title"
                )
                or soup.select_one(
                    ".p-blog-article__title"
                )
                or soup.select_one(
                    ".blog-title"
                )
                or soup.select_one(
                    "h1"
                )
            )


            if title_tag:

                title = title_tag.get_text(
                    " ",
                    strip=True
                )


                if title:

                    blog["title"] = title


            # -------------------------
            # 本文
            # -------------------------

            body_tag = (
                soup.select_one(
                    ".c-blog-article__text"
                )
                or soup.select_one(
                    ".p-blog-article__text"
                )
                or soup.select_one(
                    ".c-blog-article"
                )
                or soup.select_one(
                    "article"
                )
            )


            if body_tag:

                blog["text"] = str(
                    body_tag
                )


            if re.search(
                r"\d{2}:\d{2}",
                blog.get(
                    "date",
                    ""
                )
            ):

                status = "時刻取得成功"

            else:

                status = "時刻なし"


            print(
                f"日向坂 詳細取得 "
                f"{index}/{total}: "
                f"{blog.get('date', '不明')} / "
                f"{blog.get('member', '不明')} / "
                f"{status} / "
                f"{url}"
            )


        except Exception as e:

            print(
                "日向坂詳細ページ取得エラー:",
                url,
                e
            )


        await asyncio.sleep(
            DETAIL_REQUEST_DELAY
        )


    return blog


# =========================
# 全詳細情報補完
# =========================

async def enrich_all_details(
    session: aiohttp.ClientSession,
    blogs: list[dict]
) -> list[dict]:

    if not blogs:
        return []


    print(
        f"日向坂 詳細日時取得開始: "
        f"{len(blogs)}件"
    )


    semaphore = asyncio.Semaphore(
        DETAIL_CONCURRENCY
    )


    tasks = []


    for index, blog in enumerate(
        blogs,
        start=1
    ):

        tasks.append(
            enrich_blog_detail(
                session,
                semaphore,
                blog,
                index,
                len(blogs)
            )
        )


    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )


    enriched_blogs = []


    for original_blog, result in zip(
        blogs,
        results
    ):

        if isinstance(
            result,
            Exception
        ):

            print(
                "日向坂詳細補完タスクエラー:",
                result
            )

            enriched_blogs.append(
                original_blog
            )

        else:

            enriched_blogs.append(
                result
            )


    time_count = sum(
        1
        for blog in enriched_blogs
        if re.search(
            r"\d{2}:\d{2}",
            blog.get(
                "date",
                ""
            )
        )
    )


    print(
        f"日向坂 詳細日時取得完了: "
        f"{len(enriched_blogs)}件"
    )

    print(
        f"日向坂 時刻取得成功: "
        f"{time_count}/"
        f"{len(enriched_blogs)}件"
    )


    return enriched_blogs


# =========================
# 全記事取得
# =========================

async def get_all_blogs(
    session: aiohttp.ClientSession,
    *,
    enrich_details: bool = True,
) -> list[dict]:

    print(
        "日向坂 page=0 から"
        "最終ページまで巡回します。"
    )


    blogs = []

    seen_urls = set()

    previous_page_urls = None

    page = 0

    # 無限ループ防止用
    max_safety_page = 3000


    while page <= max_safety_page:

        page_blogs = await get_page_blogs(
            session,
            page
        )


        # -------------------------
        # 記事が0件なら終了
        # -------------------------

        if not page_blogs:

            print(
                f"日向坂 page={page}: "
                "記事が0件のため巡回終了"
            )

            break


        page_urls = [

            blog.get(
                "url",
                ""
            )

            for blog in page_blogs

            if blog.get(
                "url"
            )

        ]


        # -------------------------
        # 前ページと同一なら終了
        # -------------------------

        if (
            previous_page_urls is not None
            and page_urls == previous_page_urls
        ):

            print(
                f"日向坂 page={page}: "
                "前ページと同じ記事が返されたため"
                "巡回終了"
            )

            break


        previous_page_urls = page_urls


        new_count = 0


        for blog in page_blogs:

            blog_url = blog.get(
                "url",
                ""
            )


            if not blog_url:

                continue


            if blog_url in seen_urls:

                continue


            seen_urls.add(
                blog_url
            )

            blogs.append(
                blog
            )

            new_count += 1


        print(
            f"日向坂 進捗 page={page} / "
            f"新規{new_count}件 / "
            f"合計{len(blogs)}件"
        )


        # -------------------------
        # 新しいURLが0件なら終了
        # -------------------------

        if new_count == 0:

            print(
                f"日向坂 page={page}: "
                "新しい記事URLがなかったため"
                "巡回終了"
            )

            break


        page += 1


        # 公式サイトへの連続アクセスを抑える
        await asyncio.sleep(
            PAGE_REQUEST_DELAY
        )


    else:

        print(
            "⚠️ 日向坂 安全上限ページに"
            "到達したため巡回を終了しました:",
            max_safety_page
        )


    print(
        f"日向坂 一覧取得完了: "
        f"{len(blogs)}件"
    )


    # 一覧から取得した日付で、
    # いったん古い順に並べる
    blogs.sort(
        key=datetime_key
    )


    # -------------------------
    # テスト中は詳細取得対象を絞る
    # -------------------------

    if ARCHIVE_TEST_LIMIT > 0:

        candidate_limit = max(
            ARCHIVE_TEST_LIMIT + 24,
            ARCHIVE_TEST_LIMIT * 2
        )


        before_count = len(
            blogs
        )


        # page=0から新しい順に巡回しているため、
        # blogsの後ろ側が最も古い記事になる。
        # 一覧の日付は形式によって解析できないことがあるため、
        # 詳細取得前には日時ソートを行わない。
        if len(blogs) > candidate_limit:

            blogs = blogs[
                -candidate_limit:
            ]


        print(
            "日向坂 詳細取得候補制限:",
            f"{before_count}件 → "
            f"{len(blogs)}件"
        )


    print(
        f"日向坂 重複除去後: "
        f"{len(blogs)}件"
    )


    # 選ばれた候補だけ詳細ページを開き、
    # 正確な時刻を取得する
    if enrich_details:
        blogs = await enrich_all_details(
            session,
            blogs
        )
    else:
        print("日向坂 詳細取得を省略しました。")


    # 時刻込みで最終的に古い順へ並べる
    blogs.sort(
        key=datetime_key
    )


    return blogs

# =========================
# archive_checker用
# =========================

async def get_oldest_first(
    session: aiohttp.ClientSession,
    *,
    enrich_details: bool = True,
) -> list[dict]:

    blogs = await get_all_blogs(
        session,
        enrich_details=enrich_details,
    )


    blogs.sort(
        key=datetime_key
    )


    if blogs:

        print(
            "日向坂 最古:",
            blogs[0].get(
                "date",
                ""
            ),
            blogs[0].get(
                "url",
                ""
            )
        )


        print(
            "日向坂 最新:",
            blogs[-1].get(
                "date",
                ""
            ),
            blogs[-1].get(
                "url",
                ""
            )
        )


    print(
        f"日向坂 最終取得: "
        f"{len(blogs)}件"
    )


    return blogs


# =========================
# 写真アーカイブ向け差分一覧取得
# =========================

async def get_unregistered_candidates(
    session: aiohttp.ClientSession,
    known_urls: set[str],
    target_count: int,
) -> list[dict]:
    """最新ページから巡回し、DB未登録URLが必要数に達したら終了する。"""

    target_count = max(int(target_count), 1)
    candidates = []
    seen_urls = set()
    previous_page_urls = None
    max_safety_page = 3000

    print(f"日向坂 差分巡回開始: page=0から未登録{target_count}件を探索")

    for page in range(0, max_safety_page + 1):
        try:
            page_blogs = await get_page_blogs(session, page)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"日向坂 差分 page={page} 取得エラー:", error)
            break

        if not page_blogs:
            break

        page_urls = [
            str(blog.get("url", "")).strip()
            for blog in page_blogs
            if str(blog.get("url", "")).strip()
        ]
        if previous_page_urls is not None and page_urls == previous_page_urls:
            break
        previous_page_urls = page_urls

        new_url_count = 0
        for blog in page_blogs:
            url = str(blog.get("url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            new_url_count += 1
            if url not in known_urls:
                candidates.append(blog)
                if len(candidates) >= target_count:
                    print(
                        f"日向坂 差分候補が必要数に到達: "
                        f"page={page} / 確認{len(seen_urls)}件 / "
                        f"未登録{len(candidates)}件"
                    )
                    return candidates

        print(
            f"日向坂 差分進捗 page={page}: "
            f"確認{len(seen_urls)}件 / 未登録{len(candidates)}件"
        )

        if new_url_count == 0:
            break
        await asyncio.sleep(PAGE_REQUEST_DELAY)

    print(f"日向坂 差分巡回終了: 未登録{len(candidates)}件")
    return candidates
