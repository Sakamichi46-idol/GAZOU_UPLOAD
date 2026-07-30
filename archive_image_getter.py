import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup


# =========================
# 共通設定
# =========================

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
}


# =========================
# URL正規化
# =========================

def normalize_image_url(
    base_url: str,
    image_url: str
) -> str:
    """
    相対URLを絶対URLへ変換し、
    URLのフラグメントを取り除く。
    """

    if not image_url:
        return ""

    image_url = image_url.strip()

    if not image_url:
        return ""

    if image_url.startswith(
        (
            "data:",
            "blob:",
            "javascript:"
        )
    ):
        return ""

    full_url = urljoin(
        base_url,
        image_url
    )

    parts = urlsplit(
        full_url
    )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.query,
            ""
        )
    )


# =========================
# srcset解析
# =========================

def get_srcset_url(
    srcset: str
) -> str:
    """
    srcsetから一番最後の画像URLを取得する。
    通常、一番大きい画像が最後に書かれている。
    """

    if not srcset:
        return ""

    candidates = []

    for item in srcset.split(","):

        item = item.strip()

        if not item:
            continue

        image_url = item.split()[0]

        if image_url:
            candidates.append(
                image_url
            )

    if not candidates:
        return ""

    return candidates[-1]


# =========================
# style属性の背景画像取得
# =========================

def get_background_image(
    style_text: str
) -> str:
    """
    style="background-image: url(...)" から
    画像URLを取得する。
    """

    if not style_text:
        return ""

    match = re.search(
        r"""background-image\s*:\s*url\(
            \s*['"]?
            (.*?)
            ['"]?\s*
        \)""",
        style_text,
        flags=re.IGNORECASE | re.VERBOSE
    )

    if not match:
        return ""

    return match.group(1).strip()


# =========================
# 不要画像判定
# =========================

def is_unwanted_image(
    image_url: str
) -> bool:
    """
    ロゴ・アイコン・プロフィール画像など、
    記事本文とは関係しにくい画像を除外する。
    """

    if not image_url:
        return True

    lower_url = image_url.lower()

    unwanted_keywords = (
        "/assets/",
        "/common/",
        "/icon/",
        "/icons/",
        "/logo/",
        "logo.",
        "favicon",
        "loading",
        "spinner",
        "blank.",
        "dummy.",
        "noimage",
        "no-image",
        "placeholder",
        "avatar",
        "profile",
        "prof_",
        "member_img",
        "member-image",
        "sns_",
        "share_",
        "btn_",
        "arrow",
    )

    for keyword in unwanted_keywords:

        if keyword in lower_url:
            return True

    return False


# =========================
# サイト別画像判定
# =========================

def is_allowed_article_image(page_url: str, image_url: str) -> bool:
    """記事本文画像として許可するURLかをサイト別に判定する。"""

    if not image_url:
        return False

    page_host = urlsplit(page_url).netloc.lower()
    image_parts = urlsplit(image_url)
    image_host = image_parts.netloc.lower()
    image_path = image_parts.path.lower()

    if "sakurazaka46.com" in page_host:
        if image_host not in {"sakurazaka46.com", "www.sakurazaka46.com"}:
            return False
        # 旧記事は /files/、新記事は /images/ を使用する。
        return image_path.startswith("/images/") or "/files/" in image_path

    return True


# =========================
# 画像URL抽出
# =========================

def extract_image_urls(
    container,
    page_url: str
) -> list[str]:
    """
    指定された本文コンテナ内から画像URLを取得する。
    """

    image_urls = []

    seen = set()


    # -------------------------
    # imgタグ
    # -------------------------

    for img in container.find_all(
        "img"
    ):

        raw_url = ""

        # lazy loadを含む候補
        for attribute in (
            "data-src",
            "data-original",
            "data-lazy-src",
            "data-lazy",
            "data-image",
            "src"
        ):

            value = img.get(
                attribute
            )

            if value:

                raw_url = value

                break


        # 通常属性に画像がない場合はsrcset
        if not raw_url:

            raw_url = get_srcset_url(
                img.get(
                    "data-srcset",
                    ""
                )
                or img.get(
                    "srcset",
                    ""
                )
            )


        image_url = normalize_image_url(
            page_url,
            raw_url
        )


        if not image_url:
            continue


        if is_unwanted_image(
            image_url
        ):
            continue


        if not is_allowed_article_image(page_url, image_url):
            continue


        if image_url in seen:
            continue


        seen.add(
            image_url
        )

        image_urls.append(
            image_url
        )


    # -------------------------
    # sourceタグ
    # -------------------------

    for source in container.find_all(
        "source"
    ):

        raw_url = get_srcset_url(
            source.get(
                "data-srcset",
                ""
            )
            or source.get(
                "srcset",
                ""
            )
        )


        image_url = normalize_image_url(
            page_url,
            raw_url
        )


        if not image_url:
            continue


        if is_unwanted_image(
            image_url
        ):
            continue


        if not is_allowed_article_image(page_url, image_url):
            continue


        if image_url in seen:
            continue


        seen.add(
            image_url
        )

        image_urls.append(
            image_url
        )


    # -------------------------
    # background-image
    # -------------------------

    for tag in container.find_all(
        style=True
    ):

        raw_url = get_background_image(
            tag.get(
                "style",
                ""
            )
        )


        image_url = normalize_image_url(
            page_url,
            raw_url
        )


        if not image_url:
            continue


        if is_unwanted_image(
            image_url
        ):
            continue


        if not is_allowed_article_image(page_url, image_url):
            continue


        if image_url in seen:
            continue


        seen.add(
            image_url
        )

        image_urls.append(
            image_url
        )


    return image_urls


# =========================
# 本文コンテナ取得
# =========================

def get_article_container(
    soup: BeautifulSoup,
    page_url: str
):
    """
    グループごとの記事本文コンテナを返す。
    """

    host = urlsplit(
        page_url
    ).netloc.lower()


    # -------------------------
    # 乃木坂46
    # -------------------------

    if "nogizaka46.com" in host:

        container = (
            soup.select_one(
                ".bd--edit"
            )
            or soup.select_one(
                ".bd--article"
            )
            or soup.select_one(
                ".bl--article"
            )
        )

        if container:
            return container


    # -------------------------
    # 櫻坂46
    # -------------------------

    if "sakurazaka46.com" in host:

        container = (
            soup.select_one(
                ".box-article"
            )
            or soup.select_one(
                ".blog-article"
            )
            or soup.select_one(
                ".com-blog-part"
            )
        )

        if container:
            return container


    # -------------------------
    # 日向坂46
    # -------------------------

    if "hinatazaka46.com" in host:

        container = (
            soup.select_one(
                ".c-blog-article__text"
            )
            or soup.select_one(
                ".p-blog-article__text"
            )
            or soup.select_one(
                ".p-blog-article"
            )
        )

        if container:
            return container


    # -------------------------
    # 共通フォールバック
    # -------------------------

    return (
        soup.select_one(
            "article"
        )
        or soup.select_one(
            "main"
        )
        or soup.body
        or soup
    )


# =========================
# HTML取得
# =========================

async def fetch_html(
    session: aiohttp.ClientSession,
    page_url: str
) -> str:
    """
    記事ページのHTMLを取得する。
    """

    timeout = aiohttp.ClientTimeout(
        total=25
    )

    request_headers = dict(
        HEADERS
    )

    request_headers["Referer"] = (
        f"{urlsplit(page_url).scheme}://"
        f"{urlsplit(page_url).netloc}/"
    )


    async with session.get(
        page_url,
        headers=request_headers,
        timeout=timeout,
        allow_redirects=True
    ) as response:

        response.raise_for_status()

        return await response.text(
            errors="replace"
        )


# =========================
# 外部呼び出し
# =========================

async def get_images(
    page_url: str
) -> list[str]:
    """
    記事URLを受け取り、
    記事本文の画像URL一覧を返す。

    archive_main.pyから

        image_urls = await get_images(blog["url"])

    の形で使用する。
    """

    if not page_url:

        print(
            "画像取得エラー: URLが空です。"
        )

        return []


    try:

        connector = aiohttp.TCPConnector(
            limit=10,
            ttl_dns_cache=300
        )


        async with aiohttp.ClientSession(
            connector=connector
        ) as session:

            html = await fetch_html(
                session,
                page_url
            )


    except Exception as e:

        print(
            "記事HTML取得エラー:",
            page_url,
            e
        )

        raise RuntimeError(
            f"記事HTML取得に失敗しました: {page_url}"
        ) from e


    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )


        container = get_article_container(
            soup,
            page_url
        )


        image_urls = extract_image_urls(
            container,
            page_url
        )


        print(
            f"取得画像数: {len(image_urls)} "
            f"{page_url}"
        )


        return image_urls


    except Exception as e:

        print(
            "画像解析エラー:",
            page_url,
            e
        )

        raise RuntimeError(
            f"記事画像解析に失敗しました: {page_url}"
        ) from e
