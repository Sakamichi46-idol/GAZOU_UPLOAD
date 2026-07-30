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

BASE_URL = "https://sakurazaka46.com"

BLOG_LIST_URL = (
    "https://sakurazaka46.com/"
    "s/s46/diary/blog/list"
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
    "Referer": "https://sakurazaka46.com/",
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
    imaなどの変動するクエリを取り除き、
    同じ記事を同一URLとして扱う。
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
        "%Y %m %d %H:%M",
        "%Y %m %d",
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
            "櫻坂 最大ページ取得エラー:",
            e
        )

        return 381


    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    max_page = 0

    pager_links = soup.select(
        ".com-pager a[href], "
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
            "⚠️ 櫻坂 最大ページを"
            "HTMLから取得できませんでした。"
            " 暫定値381を使用します。"
        )

        return 381


    print(
        f"櫻坂 最大ページ: {max_page}"
    )

    return max_page


# =========================
# 一覧カード解析
# =========================

def parse_blog_card(
    card
) -> dict | None:

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
            ".name"
        )
        or card.select_one(
            ".blog-name"
        )
    )


    date_tag = (
        card.select_one(
            ".date.wf-a"
        )
        or card.select_one(
            ".date"
        )
        or card.select_one(
            "time"
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
        "group": "櫻坂46",
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
            f"櫻坂 page={page} 取得エラー:",
            e
        )

        return []


    soup = BeautifulSoup(
        html,
        "html.parser"
    )


    cards = soup.select(
        "ul.com-blog-part li.box"
    )


    if not cards:

        cards = soup.select(
            "li.box"
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


    # 一覧は新しい順なので反転
    page_blogs.reverse()


    print(
        f"櫻坂 page={page}: "
        f"{len(page_blogs)}件"
    )


    return page_blogs


# =========================
# 詳細ページの日時抽出
# =========================

def extract_detail_datetime(
    soup: BeautifulSoup,
    fallback_date: str = ""
) -> str:
    """
    櫻坂46の詳細ページから
    時刻付き投稿日時を取得する。

    例:
        2020/10/16 18:45
            ↓
        2020年10月16日 18:45
    """

    # ページ内の全テキストから
    # YYYY/MM/DD HH:MM を直接探す。
    #
    # 詳細ページ上部には
    # 「2020 10」「16」のような分割日付があるため、
    # p.dateなどの最初の要素だけを使わない。
    page_text = soup.get_text(
        " ",
        strip=True
    )

    datetime_patterns = (
        # 2020/10/16 18:45
        r"\b"
        r"(\d{4})"
        r"\s*/\s*"
        r"(\d{1,2})"
        r"\s*/\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"\b",

        # 2020.10.16 18:45
        r"\b"
        r"(\d{4})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})"
        r"\b",

        # 2020年10月16日 18:45
        r"(\d{4})"
        r"\s*年\s*"
        r"(\d{1,2})"
        r"\s*月\s*"
        r"(\d{1,2})"
        r"\s*日\s*"
        r"(\d{1,2})"
        r"\s*:\s*"
        r"(\d{2})",
    )

    for pattern in datetime_patterns:

        match = re.search(
            pattern,
            page_text
        )

        if not match:
            continue

        year = int(
            match.group(1)
        )

        month = int(
            match.group(2)
        )

        day = int(
            match.group(3)
        )

        hour = int(
            match.group(4)
        )

        minute = int(
            match.group(5)
        )

        try:

            parsed_datetime = datetime(
                year,
                month,
                day,
                hour,
                minute
            )

        except ValueError:

            continue

        return parsed_datetime.strftime(
            "%Y年%m月%d日 %H:%M"
        )


    # metaタグやdatetime属性も確認
    attribute_selectors = (
        "time[datetime]",
        "meta[property='article:published_time']",
        "meta[name='article:published_time']",
        "meta[property='og:published_time']",
    )
    
def extract_detail_datetime(
    soup: BeautifulSoup,
    fallback_date: str = ""
) -> str:
    """
    櫻坂46の詳細ページから、
    時刻付き投稿日時を取得する。

    例:
        2020/10/16 18:45
            ↓
        2020年10月16日 18:45
    """

    # =========================
    # 1. 日時が入りやすい要素を優先
    # =========================

    selectors = (
        "p.date.wf-a",
        ".box-article p.date",
        ".box-article .date.wf-a",
        ".box-article .date",
        ".blog-article p.date",
        ".blog-article .date",
        "time",
    )

    candidate_texts = []


    for selector in selectors:

        for tag in soup.select(
            selector
        ):

            # datetime属性も取得
            datetime_attr = tag.get(
                "datetime",
                ""
            )

            if datetime_attr:

                candidate_texts.append(
                    datetime_attr
                )


            text = tag.get_text(
                " ",
                strip=True
            )

            if text:

                candidate_texts.append(
                    text
                )


    # =========================
    # 2. metaタグを確認
    # =========================

    meta_selectors = (
        "meta[property='article:published_time']",
        "meta[name='article:published_time']",
        "meta[property='og:published_time']",
        "meta[name='date']",
        "meta[itemprop='datePublished']",
    )


    for selector in meta_selectors:

        for tag in soup.select(
            selector
        ):

            content = tag.get(
                "content",
                ""
            )

            if content:

                candidate_texts.append(
                    content
                )


    # =========================
    # 3. ページ全体も最後に確認
    # =========================

    page_text = soup.get_text(
        " ",
        strip=True
    )

    if page_text:

        candidate_texts.append(
            page_text
        )


    # =========================
    # 4. 時刻付き日時を抽出
    # =========================

    patterns = (
        # 2020/10/16 18:45
        # 2020／10／16 18:45
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*[\/／]\s*"
        r"(\d{1,2})"
        r"\s*[\/／]\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*[:：]\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2020.10.16 18:45
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s*\.\s*"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"\s*[:：]\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2020-10-16 18:45
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*-\s*"
        r"(\d{1,2})"
        r"\s*-\s*"
        r"(\d{1,2})"
        r"[T\s]+"
        r"(\d{1,2})"
        r"\s*[:：]\s*"
        r"(\d{2})"
        r"(?!\d)",

        # 2020年10月16日 18:45
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*年\s*"
        r"(\d{1,2})"
        r"\s*月\s*"
        r"(\d{1,2})"
        r"\s*日"
        r"\s+"
        r"(\d{1,2})"
        r"\s*[:：]\s*"
        r"(\d{2})"
        r"(?!\d)",
    )


    for candidate in candidate_texts:

        # 改行・タブ・連続空白を統一
        cleaned_text = re.sub(
            r"\s+",
            " ",
            candidate
        ).strip()


        for pattern in patterns:

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
                    int(match.group(5)),
                )

            except ValueError:

                continue


            result = parsed_datetime.strftime(
                "%Y年%m月%d日 %H:%M"
            )

            return result


    # =========================
    # 5. 時刻なしの日付を整える
    # =========================

    fallback_patterns = (
        # 2020年10月16日
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*年\s*"
        r"(\d{1,2})"
        r"\s*月\s*"
        r"(\d{1,2})"
        r"\s*日"
        r"(?!\d)",

        # 2020/10/16
        r"(?<!\d)"
        r"(\d{4})"
        r"\s*[\/／]\s*"
        r"(\d{1,2})"
        r"\s*[\/／]\s*"
        r"(\d{1,2})"
        r"(?!\d)",

        # 2020 10 16
        r"(?<!\d)"
        r"(\d{4})"
        r"\s+"
        r"(\d{1,2})"
        r"\s+"
        r"(\d{1,2})"
        r"(?!\d)",
    )


    fallback_text = (
        fallback_date
        or ""
    )


    for pattern in fallback_patterns:

        match = re.search(
            pattern,
            fallback_text
        )

        if not match:
            continue


        try:

            parsed_date = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )

        except ValueError:

            continue


        return parsed_date.strftime(
            "%Y年%m月%d日"
        )


    return fallback_date


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
                    "p.name"
                )
                or soup.select_one(
                    ".box-article .name"
                )
                or soup.select_one(
                    ".blog-name"
                )
                or soup.select_one(
                    ".name"
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
                    "h1.title"
                )
                or soup.select_one(
                    ".box-article h1"
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
                    ".box-article"
                )
                or soup.select_one(
                    ".blog-article"
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
                f"櫻坂 詳細取得 "
                f"{index}/{total}: "
                f"{blog.get('date', '不明')} / "
                f"{blog.get('member', '不明')} / "
                f"{status} / "
                f"{url}"
            )


        except Exception as e:

            print(
                "櫻坂詳細ページ取得エラー:",
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
        f"櫻坂 詳細日時取得開始: "
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
                "櫻坂詳細補完タスクエラー:",
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
        f"櫻坂 詳細日時取得完了: "
        f"{len(enriched_blogs)}件"
    )

    print(
        f"櫻坂 時刻取得成功: "
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

    max_page = await get_max_page(
        session
    )


    print(
        f"櫻坂 page={max_page} から"
        "古い順に巡回します。"
    )


    blogs = []

    seen_urls = set()


    # テスト中は20件より少し多めに取得する
    # 同じ日付の記事を時刻で並べ替えるため余裕を持たせる
    if ARCHIVE_TEST_LIMIT > 0:

        test_candidate_limit = max(
            ARCHIVE_TEST_LIMIT + 24,
            ARCHIVE_TEST_LIMIT * 2
        )

        print(
            "櫻坂 テスト候補取得上限:",
            f"{test_candidate_limit}件"
        )

    else:

        test_candidate_limit = 0


    for page in range(
        max_page,
        -1,
        -1
    ):

        page_blogs = await get_page_blogs(
            session,
            page
        )


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


        print(
            f"櫻坂 進捗 page={page} / "
            f"合計{len(blogs)}件"
        )


        if (
            test_candidate_limit > 0
            and len(blogs)
            >= test_candidate_limit
        ):

            print(
                "櫻坂 テスト用候補が"
                "必要数に達したため、"
                "一覧巡回を終了します。"
            )

            break


        await asyncio.sleep(
            PAGE_REQUEST_DELAY
        )


    print(
        f"櫻坂 重複除去後: "
        f"{len(blogs)}件"
    )


    # 詳細ページから時刻を取得
    if enrich_details:
        blogs = await enrich_all_details(
            session,
            blogs
        )
    else:
        print("櫻坂 詳細取得を省略しました。")


    # 時刻込みで正確に並べ替え
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
            "櫻坂 最古:",
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
            "櫻坂 最新:",
            blogs[-1].get(
                "date",
                ""
            ),
            blogs[-1].get(
                "url",
                ""
            )
        )


    return blogs
