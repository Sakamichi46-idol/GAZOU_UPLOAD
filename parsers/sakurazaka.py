from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from parsers.utils import normalize_datetime


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


_IMAGE_ATTRIBUTES = (
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazy",
    "data-image",
    "src",
)


def _get_srcset_url(srcset: str) -> str:
    """srcset内で最後に記述された画像URLを返す。"""

    if not srcset:
        return ""

    candidates = []

    for item in srcset.split(","):
        item = item.strip()

        if not item:
            continue

        image_url = item.split()[0]

        if image_url:
            candidates.append(image_url)

    return candidates[-1] if candidates else ""


def _get_image_source(img) -> str:
    """通常画像とlazy-load画像の両方からURL候補を取得する。"""

    for attribute in _IMAGE_ATTRIBUTES:
        value = img.get(attribute)

        if value:
            return str(value).strip()

    return _get_srcset_url(
        img.get("data-srcset", "")
        or img.get("srcset", "")
    )


def _is_sakurazaka_blog_image(image_url: str) -> bool:
    """
    櫻坂46ブログ本文画像として許可するURLか判定する。

    旧記事では /files/、新しい記事では /images/ が使われるため、
    どちらも許可する。本文コンテナを .box-article に限定した上で、
    櫻坂公式ドメイン上の画像だけを採用する。
    """

    if not image_url:
        return False

    parts = urlsplit(image_url)
    host = parts.netloc.lower()
    path = parts.path.lower()

    if host not in {
        "sakurazaka46.com",
        "www.sakurazaka46.com",
    }:
        return False

    return path.startswith("/images/") or "/files/" in path


def get_sakurazaka_images(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=15,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "lxml",
    )

    blog = {
        "group": "櫻坂46",
        "member": "",
        "title": "",
        "date": "",
        "url": url,
        "images": [],
    }

    # タイトル
    title = soup.select_one("h1.title") or soup.find("h1")

    if title:
        blog["title"] = title.get_text(" ", strip=True)

    # メンバー名・投稿日
    blog_foot = soup.select_one(".blog-foot")

    if blog_foot:
        member = blog_foot.select_one("p.name")

        if member:
            blog["member"] = member.get_text(" ", strip=True)

        date = blog_foot.select_one("p.date")

        if date:
            blog["date"] = normalize_datetime(
                date.get_text(" ", strip=True)
            )

    # 本文。画像抽出時にヘッダー・プロフィール画像を混ぜないため、
    # 可能な限り本文専用コンテナを優先する。
    article = (
        soup.select_one(".box-article")
        or soup.select_one(".blog-article")
        or soup.select_one(".com-blog-part")
        or soup.select_one(".bd--edit")
        or soup.find("article")
        or soup.find("main")
    )

    if article is None:
        article = soup

    seen = set()
    img_tag_count = 0
    rejected_count = 0

    for img in article.find_all("img"):
        img_tag_count += 1
        raw_src = _get_image_source(img)

        if not raw_src:
            rejected_count += 1
            continue

        image_url = urljoin(url, raw_src)

        if not _is_sakurazaka_blog_image(image_url):
            rejected_count += 1
            continue

        if image_url in seen:
            continue

        seen.add(image_url)
        blog["images"].append(image_url)

    print(
        "櫻坂画像取得:",
        f"imgタグ={img_tag_count}",
        f"採用={len(blog['images'])}",
        f"除外={rejected_count}",
        url,
    )

    return blog
