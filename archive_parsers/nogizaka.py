import asyncio
import json
import re
import unicodedata

from datetime import datetime
from html import unescape
from urllib.parse import (
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

BASE_URL = "https://www.nogizaka46.com"

API_URL = (
    "https://www.nogizaka46.com"
    "/s/n46/api/list/blog"
)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": (
        "https://www.nogizaka46.com/"
        "s/n46/diary/MEMBER"
    ),
}


# 詳細ページの同時取得数
DETAIL_CONCURRENCY = 1


# APIページ巡回の安全上限
API_MAX_PAGE = 3000


# =========================
# 除外する投稿者
# =========================

IGNORED_MEMBER_NAMES = {
    "運営スタッフ",
    "スタッフ",
    "乃木坂46運営委員会",
}


# =========================
# 期別表記
# =========================

GENERATION_NAMES = {
    "3期生",
    "4期生",
    "新4期生",
    "5期生",
    "6期生",
}


# =========================
# 乃木坂メンバー一覧
# =========================
#
# 過去記事のアーカイブにも使うため、
# 卒業済みメンバーも残している。
# =========================

NOGIZAKA_MEMBER_NAMES = [
    # -------------------------
    # 3期生
    # -------------------------
    "伊藤理々杏",
    "岩本蓮加",
    "梅澤美波",
    "大園桃子",
    "久保史緒里",
    "阪口珠美",
    "佐藤楓",
    "中村麗乃",
    "向井葉月",
    "山下美月",
    "吉田綾乃クリスティー",
    "与田祐希",

    # -------------------------
    # 4期生・新4期生
    # -------------------------
    "遠藤さくら",
    "賀喜遥香",
    "掛橋沙耶香",
    "金川紗耶",
    "北川悠理",
    "黒見明香",
    "佐藤璃果",
    "柴田柚菜",
    "清宮レイ",
    "田村真佑",
    "筒井あやめ",
    "早川聖来",
    "林瑠奈",
    "松尾美佑",
    "矢久保美緒",
    "弓木奈於",

    # -------------------------
    # 5期生
    # -------------------------
    "五百城茉央",
    "池田瑛紗",
    "一ノ瀬美空",
    "井上和",
    "岡本姫奈",
    "小川彩",
    "奥田いろは",
    "川﨑桜",
    "菅原咲月",
    "冨里奈央",
    "中西アルノ",

    # -------------------------
    # 6期生
    # -------------------------
    "愛宕心響",
    "大越ひなの",
    "小津玲奈",
    "海邉朱莉",
    "川端晃菜",
    "鈴木佑捺",
    "瀬戸口心月",
    "長嶋凛桜",
    "増田三莉音",
    "森平麗心",
    "矢田萌華",
]


# =========================
# 名前の別表記
# =========================

NOGIZAKA_MEMBER_ALIASES = {
    # 一覧タイトルで「理」が省略されている記事
    "伊藤理々杏": [
        "伊藤々杏",
    ],

    # 崎の字形違いへの保険
    "川﨑桜": [
        "川崎桜",
    ],
}


# =========================
# URL正規化
# =========================

def normalize_blog_url(
    url: str
) -> str:
    """
    URLを絶対URLへ変換し、
    imaなどの変動するクエリを削除する。

    例:
        /s/n46/diary/detail/101102?ima=0710

            ↓

        https://www.nogizaka46.com/s/n46/diary/detail/101102
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
# 名前検索用の正規化
# =========================

def normalize_name_for_search(
    value: str
) -> str:
    """
    名前の比較用に表記を統一する。

    次の表記はすべて同じ文字列になる。

        大越　ひなの
        大越 ひなの
        大越ひなの

            ↓

        大越ひなの

    全角数字なども半角へ統一する。
    """

    if not value:
        return ""

    value = unescape(
        str(value)
    )

    value = unicodedata.normalize(
        "NFKC",
        value
    )

    value = re.sub(
        r"[\s\u3000]+",
        "",
        value
    )

    return value.strip()


# =========================
# 検索対象テキストの正規化
# =========================

def normalize_search_text(
    *values
) -> str:
    """
    タイトルや本文からHTMLタグを除去し、
    空白・改行・タブをすべて削除する。
    """

    combined = " ".join(
        str(value or "")
        for value in values
    )

    if not combined:
        return ""

    plain_text = BeautifulSoup(
        combined,
        "html.parser"
    ).get_text(
        " ",
        strip=True
    )

    plain_text = unescape(
        plain_text
    )

    plain_text = unicodedata.normalize(
        "NFKC",
        plain_text
    )

    return re.sub(
        r"[\s\u3000]+",
        "",
        plain_text
    )


# =========================
# 期別表記の正規化
# =========================

def normalize_generation_name(
    member: str
) -> str:
    """
    ３期生、新４期生などの全角表記を
    3期生、新4期生へ統一する。
    """

    normalized = normalize_name_for_search(
        member
    )

    generation_aliases = {
        "3期生": "3期生",
        "三期生": "3期生",

        "4期生": "4期生",
        "四期生": "4期生",

        "新4期生": "新4期生",
        "新四期生": "新4期生",

        "5期生": "5期生",
        "五期生": "5期生",

        "6期生": "6期生",
        "六期生": "6期生",
    }

    return generation_aliases.get(
        normalized,
        normalized
    )


# =========================
# 除外対象判定
# =========================

def is_ignored_member(
    member: str
) -> bool:

    normalized_member = normalize_name_for_search(
        member
    )

    ignored_names = {
        normalize_name_for_search(
            name
        )
        for name in IGNORED_MEMBER_NAMES
    }

    return normalized_member in ignored_names


# =========================
# 正式名へ統一
# =========================

def canonicalize_member_name(
    member: str
) -> str:
    """
    すでに個人名が取得できている場合に、
    空白表記などを正式名へ統一する。

    例:
        大越　ひなの
        大越 ひなの

            ↓

        大越ひなの
    """

    normalized_member = normalize_name_for_search(
        member
    )

    if not normalized_member:
        return ""

    for official_name in NOGIZAKA_MEMBER_NAMES:

        if (
            normalize_name_for_search(
                official_name
            )
            == normalized_member
        ):

            return official_name

    for official_name, aliases in (
        NOGIZAKA_MEMBER_ALIASES.items()
    ):

        for alias in aliases:

            if (
                normalize_name_for_search(
                    alias
                )
                == normalized_member
            ):

                return official_name

    return normalize_member_name(
        member
    )


# =========================
# テキスト内のメンバー名検索
# =========================

def find_member_in_text(
    text: str
) -> str:
    """
    正式名・別名をテキストから検索する。

    複数のメンバー名が見つかった場合は、
    テキスト内で最も前に登場する名前を返す。
    """

    search_text = normalize_search_text(
        text
    )

    if not search_text:
        return ""

    matches = []


    # 正式名
    for official_name in NOGIZAKA_MEMBER_NAMES:

        normalized_name = normalize_name_for_search(
            official_name
        )

        if not normalized_name:
            continue

        position = search_text.find(
            normalized_name
        )

        if position >= 0:

            matches.append(
                (
                    position,
                    -len(normalized_name),
                    official_name
                )
            )


    # 別名
    for official_name, aliases in (
        NOGIZAKA_MEMBER_ALIASES.items()
    ):

        for alias in aliases:

            normalized_alias = (
                normalize_name_for_search(
                    alias
                )
            )

            if not normalized_alias:
                continue

            position = search_text.find(
                normalized_alias
            )

            if position >= 0:

                matches.append(
                    (
                        position,
                        -len(normalized_alias),
                        official_name
                    )
                )


    if not matches:
        return ""


    # 最も前に出た名前を優先。
    # 同じ位置なら長い名前を優先。
    matches.sort()

    return matches[0][2]


# =========================
# 期別ブログから個人を判定
# =========================

def resolve_nogizaka_member(
    member: str,
    title: str = "",
    text: str = "",
) -> str:
    """
    memberが「3期生」「新4期生」「5期生」などの場合、
    タイトルと本文から投稿者の個人名を判定する。

    判定順:
        1. タイトル
        2. 本文の冒頭
        3. 本文全体

    すでに個人名の場合は正式表記へ統一する。
    運営スタッフの場合は空文字を返す。
    """

    normalized_member = normalize_member_name(
        member
    )

    if is_ignored_member(
        normalized_member
    ):

        return ""


    generation_name = normalize_generation_name(
        normalized_member
    )


    # すでに個人名なら正式名へ統一
    if generation_name not in GENERATION_NAMES:

        return canonicalize_member_name(
            normalized_member
        )


    # -------------------------
    # 1. タイトルを最優先
    # -------------------------

    found_member = find_member_in_text(
        title
    )

    if found_member:

        return found_member


    # -------------------------
    # 本文をプレーンテキスト化
    # -------------------------

    body_plain_text = BeautifulSoup(
        str(text or ""),
        "html.parser"
    ).get_text(
        " ",
        strip=True
    )


    # -------------------------
    # 2. 本文冒頭を検索
    # -------------------------
    #
    # 本文全体には別メンバー名が
    # 多数含まれる場合があるため、
    # まず冒頭3000文字を優先する。
    # -------------------------

    body_head = body_plain_text[
        :3000
    ]

    found_member = find_member_in_text(
        body_head
    )

    if found_member:

        return found_member


    # -------------------------
    # 3. 本文全体を検索
    # -------------------------

    found_member = find_member_in_text(
        body_plain_text
    )

    if found_member:

        return found_member


    # 判定できなければ期別表記を残す
    return generation_name


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
# 日時文字列を探す
# =========================

def find_datetime_in_text(
    text: str,
    require_time: bool = False
) -> str:
    """
    文字列から日時を探して、

        2026年05月26日 21:45

    の形式で返す。
    """

    if not text:
        return ""

    text = unescape(
        str(text)
    )

    text = unicodedata.normalize(
        "NFKC",
        text
    )

    text = text.replace(
        "\u3000",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()


    # -------------------------
    # 時刻付き
    # -------------------------

    time_match = re.search(
        r"""
        (?P<year>\d{4})
        \s*
        [./\-年]
        \s*
        (?P<month>\d{1,2})
        \s*
        [./\-月]
        \s*
        (?P<day>\d{1,2})
        \s*
        日?
        [T\s]+
        (?P<hour>\d{1,2})
        \s*
        [:：]
        \s*
        (?P<minute>\d{2})
        """,
        text,
        flags=re.VERBOSE
    )


    if time_match:

        try:

            parsed_datetime = datetime(
                int(
                    time_match.group(
                        "year"
                    )
                ),
                int(
                    time_match.group(
                        "month"
                    )
                ),
                int(
                    time_match.group(
                        "day"
                    )
                ),
                int(
                    time_match.group(
                        "hour"
                    )
                ),
                int(
                    time_match.group(
                        "minute"
                    )
                )
            )

            return parsed_datetime.strftime(
                "%Y年%m月%d日 %H:%M"
            )

        except ValueError:

            pass


    if require_time:

        return ""


    # -------------------------
    # 日付のみ
    # -------------------------

    date_match = re.search(
        r"""
        (?P<year>\d{4})
        \s*
        [./\-年]
        \s*
        (?P<month>\d{1,2})
        \s*
        [./\-月]
        \s*
        (?P<day>\d{1,2})
        \s*
        日?
        """,
        text,
        flags=re.VERBOSE
    )


    if date_match:

        try:

            parsed_date = datetime(
                int(
                    date_match.group(
                        "year"
                    )
                ),
                int(
                    date_match.group(
                        "month"
                    )
                ),
                int(
                    date_match.group(
                        "day"
                    )
                )
            )

            return parsed_date.strftime(
                "%Y年%m月%d日"
            )

        except ValueError:

            pass


    return ""


# =========================
# 日付部分だけ取得
# =========================

def get_date_only(
    date_text: str
) -> str:

    if not date_text:
        return ""

    normalized = normalize_datetime(
        date_text
    )

    match = re.search(
        r"\d{4}年\d{2}月\d{2}日",
        normalized
    )

    if match:

        return match.group(0)

    return ""


# =========================
# 詳細ページの日時取得
# =========================

def extract_detail_datetime(
    soup: BeautifulSoup,
    html: str,
    fallback_date: str = ""
) -> str:

    fallback_day = get_date_only(
        fallback_date
    )

    candidates = []


    def add_candidate(
        value
    ):

        if not value:
            return

        value = str(
            value
        ).strip()

        if not value:
            return

        if value not in candidates:

            candidates.append(
                value
            )


    selectors = (
        ".bd--hd__date",
        ".bd--hd__time",
        ".bd--date",
        ".bd--time",
        ".bd--hd time",
        ".bd--hd__in",
        ".bd--hd",
        ".bd--header",
        ".blog-date",
        ".blog-time",
        "time",
    )


    for selector in selectors:

        for tag in soup.select(
            selector
        ):

            add_candidate(
                tag.get_text(
                    " ",
                    strip=True
                )
            )

            add_candidate(
                tag.get(
                    "datetime",
                    ""
                )
            )

            add_candidate(
                tag.get(
                    "content",
                    ""
                )
            )

            if tag.parent:

                add_candidate(
                    tag.parent.get_text(
                        " ",
                        strip=True
                    )
                )


    meta_selectors = (
        "meta[property='article:published_time']",
        "meta[name='article:published_time']",
        "meta[property='og:published_time']",
        "meta[name='date']",
        "meta[name='publish_date']",
        "meta[itemprop='datePublished']",
    )


    for selector in meta_selectors:

        for tag in soup.select(
            selector
        ):

            add_candidate(
                tag.get(
                    "content",
                    ""
                )
            )


    for script in soup.select(
        "script[type='application/ld+json']"
    ):

        script_text = (
            script.string
            or script.get_text(
                " ",
                strip=True
            )
        )

        if not script_text:
            continue


        try:

            json_data = json.loads(
                script_text
            )

            json_items = (
                json_data
                if isinstance(
                    json_data,
                    list
                )
                else [json_data]
            )

            for item in json_items:

                if not isinstance(
                    item,
                    dict
                ):
                    continue

                add_candidate(
                    item.get(
                        "datePublished",
                        ""
                    )
                )

                add_candidate(
                    item.get(
                        "dateCreated",
                        ""
                    )
                )

        except Exception:

            add_candidate(
                script_text
            )


    add_candidate(
        html
    )


    # APIの日付と同じ日の時刻付きを優先
    for candidate in candidates:

        found = find_datetime_in_text(
            candidate,
            require_time=True
        )

        if not found:
            continue

        if (
            fallback_day
            and get_date_only(
                found
            ) == fallback_day
        ):

            return found


    # 日付に関係なく時刻付きを探す
    for candidate in candidates:

        found = find_datetime_in_text(
            candidate,
            require_time=True
        )

        if found:

            return found


    # 同じ日の時刻なし
    for candidate in candidates:

        found = find_datetime_in_text(
            candidate,
            require_time=False
        )

        if not found:
            continue

        if (
            fallback_day
            and get_date_only(
                found
            ) == fallback_day
        ):

            return found


    return normalize_datetime(
        fallback_date
    )


# =========================
# APIレスポンス解析
# =========================

def parse_api_response(
    text: str
) -> dict | None:

    if not text:
        return None


    # JSONP形式
    match = re.search(
        r"^\s*res\((.*)\)\s*;?\s*$",
        text,
        flags=re.DOTALL
    )


    if match:

        try:

            return json.loads(
                match.group(1)
            )

        except json.JSONDecodeError as e:

            print(
                "乃木坂API JSONP解析エラー:",
                e
            )

            return None


    try:

        return json.loads(
            text
        )

    except json.JSONDecodeError:

        print(
            "乃木坂APIレスポンス解析失敗:",
            text[:200]
        )

        return None


# =========================
# API取得
# =========================

async def fetch_api_items(
    session: aiohttp.ClientSession,
    start: int = 0,
    row_count: int = 100,
):

    timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT
    )

    params = {
        "st": start,
        "rw": row_count,
        "callback": "res",
    }

    async with session.get(
        API_URL,
        params=params,
        headers=HEADERS,
        timeout=timeout,
        allow_redirects=True
    ) as response:

        print(
            f"乃木坂 APIリクエスト "
            f"st={start}: "
            f"{response.url}"
        )

        response.raise_for_status()

        text = await response.text(
            errors="replace"
        )

    data = parse_api_response(
        text
    )

    if not data:
        return [], {}

    items = data.get(
        "data",
        []
    )

    if not isinstance(
        items,
        list
    ):
        items = []

    return items, data


# =========================
# API記事を共通形式へ変換
# =========================

def convert_item(
    item: dict
) -> dict | None:

    raw_url = item.get(
        "link",
        ""
    )

    blog_url = normalize_blog_url(
        raw_url
    )

    if not blog_url:

        return None


    title = (
        item.get(
            "title",
            ""
        )
        or ""
    )


    text = (
        item.get(
            "text",
            ""
        )
        or ""
    )


    raw_member = normalize_member_name(
        item.get(
            "name",
            ""
        )
    )


    # 運営スタッフは除外
    if is_ignored_member(
        raw_member
    ):

        print(
            "乃木坂 運営スタッフ記事を除外:",
            blog_url
        )

        return None


    member = resolve_nogizaka_member(
        raw_member,
        title,
        text
    )


    if not member:

        return None


    date = normalize_datetime(
        item.get(
            "date",
            ""
        )
    )


    return {
        "group": "乃木坂46",
        "url": blog_url,
        "member": member,
        "title": title,
        "date": date,
        "text": text,
    }


# =========================
# 詳細ページHTML取得
# =========================

async def fetch_detail_html(
    session: aiohttp.ClientSession,
    url: str
) -> str:

    timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT
    )


    request_headers = dict(
        HEADERS
    )

    request_headers["Accept"] = (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )


    async with session.get(
        url,
        headers=request_headers,
        timeout=timeout,
        allow_redirects=True
    ) as response:

        response.raise_for_status()

        return await response.text(
            errors="replace"
        )


# =========================
# 詳細ページ情報補完
# =========================

async def enrich_blog_detail(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    blog: dict,
    index: int,
    total: int
) -> dict:

    url = blog.get(
        "url",
        ""
    )


    if not url:

        return blog


    async with semaphore:

        try:

            html = await fetch_detail_html(
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
                html,
                blog.get(
                    "date",
                    ""
                )
            )


            if detail_date:

                blog["date"] = detail_date


            # -------------------------
            # 詳細ページのメンバー欄
            # -------------------------

            member_tag = (
                soup.select_one(
                    ".bd--prof__name"
                )
                or soup.select_one(
                    ".bd--name"
                )
                or soup.select_one(
                    ".blog-name"
                )
            )


            if member_tag:

                detail_member = (
                    normalize_member_name(
                        member_tag.get_text(
                            " ",
                            strip=True
                        )
                    )
                )


                if is_ignored_member(
                    detail_member
                ):

                    blog["_ignore"] = True

                    print(
                        "乃木坂 運営スタッフ記事を除外:",
                        url
                    )

                    return blog


                if detail_member:

                    blog["member"] = (
                        detail_member
                    )


            # -------------------------
            # タイトル
            # -------------------------

            title_tag = (
                soup.select_one(
                    ".bd--hd__ttl"
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
                    ".bd--edit"
                )
                or soup.select_one(
                    ".bd--body"
                )
                or soup.select_one(
                    ".blog-article"
                )
                or soup.select_one(
                    "article"
                )
                or soup.select_one(
                    "main"
                )
            )


            if body_tag:

                blog["text"] = str(
                    body_tag
                )


            # -------------------------
            # 期別表記を個人名へ変換
            # -------------------------

            resolved_member = (
                resolve_nogizaka_member(
                    blog.get(
                        "member",
                        ""
                    ),
                    blog.get(
                        "title",
                        ""
                    ),
                    blog.get(
                        "text",
                        ""
                    )
                )
            )


            if not resolved_member:

                blog["_ignore"] = True

                return blog


            blog["member"] = (
                resolved_member
            )


            # -------------------------
            # ログ
            # -------------------------

            if re.search(
                r"\d{2}:\d{2}",
                blog.get(
                    "date",
                    ""
                )
            ):

                datetime_status = (
                    "時刻取得成功"
                )

            else:

                datetime_status = (
                    "時刻なし"
                )


            print(
                f"乃木坂 詳細取得 "
                f"{index}/{total}: "
                f"{blog.get('date', '不明')} / "
                f"{blog.get('member', '不明')} / "
                f"{datetime_status} / "
                f"{url}"
            )


        except Exception as e:

            print(
                "乃木坂詳細ページ取得エラー:",
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
        f"乃木坂 詳細日時取得開始: "
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
                "乃木坂詳細補完タスクエラー:",
                result
            )

            if not original_blog.get(
                "_ignore",
                False
            ):

                enriched_blogs.append(
                    original_blog
                )

            continue


        if result.get(
            "_ignore",
            False
        ):

            continue


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
        f"乃木坂 詳細日時取得完了: "
        f"{len(enriched_blogs)}件"
    )

    print(
        f"乃木坂 時刻取得成功: "
        f"{time_count}/"
        f"{len(enriched_blogs)}件"
    )


    return enriched_blogs


# =========================
# 全API記事取得
# =========================

async def get_all_blog_urls(
    session: aiohttp.ClientSession
) -> list[dict]:

    row_count = 100
    start = 0

    print(
        "乃木坂 API st=0 から"
        "全記事を巡回します。"
    )

    blogs = []
    seen_urls = set()

    previous_page_urls = None

    # 無限ループ防止
    max_requests = 3000
    request_count = 0

    while request_count < max_requests:

        request_count += 1

        try:

            items, data = await fetch_api_items(
                session,
                start=start,
                row_count=row_count
            )

        except asyncio.CancelledError:

            raise

        except Exception as e:

            print(
                f"乃木坂 API st={start} "
                f"取得エラー:",
                e
            )

            break

        if not items:

            print(
                f"乃木坂 API st={start}: "
                "記事0件のため巡回終了"
            )

            break

        page_blogs = []

        for item in items:

            blog = convert_item(
                item
            )

            if blog:

                page_blogs.append(
                    blog
                )

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

        if (
            previous_page_urls is not None
            and page_urls == previous_page_urls
        ):

            print(
                f"乃木坂 API st={start}: "
                "前回と同じ内容のため巡回終了"
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
            f"乃木坂 API st={start}: "
            f"{len(items)}件 / "
            f"新規{new_count}件 / "
            f"合計{len(blogs)}件"
        )

        if new_count == 0:

            print(
                f"乃木坂 API st={start}: "
                "新しい記事URLがないため"
                "巡回終了"
            )

            break

        total_count = data.get(
            "count"
        )

        try:

            total_count = int(
                total_count
            )

        except (
            TypeError,
            ValueError
        ):

            total_count = 0

        print(
            "乃木坂 API総件数:",
            total_count
            if total_count > 0
            else "不明"
        )

        if (
            total_count > 0
            and start + len(items) >= total_count
        ):

            print(
                "乃木坂 API総件数へ"
                "到達したため巡回終了:",
                total_count
            )

            break

        # 100件分、取得開始位置を進める
        start += row_count

        await asyncio.sleep(
            PAGE_REQUEST_DELAY
        )

    else:

        print(
            "⚠️ 乃木坂 API安全上限へ"
            "到達したため巡回終了:",
            max_requests
        )

    print(
        f"乃木坂 API一覧取得完了: "
        f"{len(blogs)}件"
    )

    return blogs


# =========================
# 全記事取得
# =========================

async def get_all_blogs(
    session: aiohttp.ClientSession,
    *,
    enrich_details: bool = True,
) -> list[dict]:

    blogs = await get_all_blog_urls(
        session
    )


    if not blogs:

        return []


    # APIの日付で古い順へ並べる
    blogs.sort(
        key=datetime_key
    )


    # -------------------------
    # テスト中は詳細取得候補を絞る
    # -------------------------

    if ARCHIVE_TEST_LIMIT > 0:

        candidate_limit = max(
            ARCHIVE_TEST_LIMIT + 24,
            ARCHIVE_TEST_LIMIT * 2
        )


        before_count = len(
            blogs
        )


        blogs = blogs[
            :candidate_limit
        ]


        print(
            "乃木坂 詳細取得候補制限:",
            f"{before_count}件 → "
            f"{len(blogs)}件"
        )


    print(
        f"乃木坂 詳細取得対象: "
        f"{len(blogs)}件"
    )


    if enrich_details:
        blogs = await enrich_all_details(
            session,
            blogs
        )
    else:
        print("乃木坂 詳細取得を省略しました。")


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
            "乃木坂 最古:",
            blogs[0].get(
                "date",
                ""
            ),
            blogs[0].get(
                "member",
                ""
            ),
            blogs[0].get(
                "url",
                ""
            )
        )


        print(
            "乃木坂 最新:",
            blogs[-1].get(
                "date",
                ""
            ),
            blogs[-1].get(
                "member",
                ""
            ),
            blogs[-1].get(
                "url",
                ""
            )
        )


    print(
        f"乃木坂 最終取得: "
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
    """最新側から巡回し、DB未登録URLが必要数に達したら終了する。"""

    target_count = max(int(target_count), 1)
    row_count = 100
    start = 0
    candidates = []
    seen_urls = set()
    previous_page_urls = None
    max_requests = 3000

    print(f"乃木坂 差分巡回開始: 未登録{target_count}件を探索")

    for request_count in range(1, max_requests + 1):
        try:
            items, data = await fetch_api_items(
                session,
                start=start,
                row_count=row_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"乃木坂 差分API st={start} 取得エラー:", error)
            break

        if not items:
            break

        page_blogs = [
            blog for blog in (convert_item(item) for item in items) if blog
        ]
        page_urls = [blog.get("url", "") for blog in page_blogs if blog.get("url")]

        if previous_page_urls is not None and page_urls == previous_page_urls:
            break
        previous_page_urls = page_urls

        for blog in page_blogs:
            url = str(blog.get("url", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if url not in known_urls:
                candidates.append(blog)
                if len(candidates) >= target_count:
                    print(
                        f"乃木坂 差分候補が必要数に到達: "
                        f"API {request_count}回 / 未登録{len(candidates)}件"
                    )
                    return candidates

        print(
            f"乃木坂 差分進捗 st={start}: "
            f"確認{len(seen_urls)}件 / 未登録{len(candidates)}件"
        )

        total_count = data.get("count")
        try:
            total_count = int(total_count)
        except (TypeError, ValueError):
            total_count = 0

        if total_count > 0 and start + len(items) >= total_count:
            break

        start += row_count
        await asyncio.sleep(PAGE_REQUEST_DELAY)

    print(f"乃木坂 差分巡回終了: 未登録{len(candidates)}件")
    return candidates
