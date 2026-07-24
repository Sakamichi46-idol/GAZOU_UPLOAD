import asyncio
from typing import Any

from blog_checker import get_latest_blog
from config import ALL_BLOG_CHANNEL, BLOG_CHANNELS
from database import (
    is_initial_sync_completed,
    is_notified,
    mark_initial_sync_completed,
    save_blog,
    save_blogs,
)
from image_getter import get_images
from media_converter import send_blog_media


# =========================
# 監視設定
# =========================

CHECK_INTERVAL = 600


# =========================
# 通知文
# =========================

def build_notification_text(
    blog: dict[str, Any],
    image_count: int,
) -> str:
    """
    Discordへ送信するブログ通知文を作成する。
    """

    return (
        f"🏷️ {blog.get('group', '')}\n"
        f"👤 {blog.get('member', '')}\n"
        f"📝 {blog.get('title', '')}\n"
        f"📅 {blog.get('date', '')}\n"
        f"🔗 {blog.get('url', '')}\n\n"
        f"📷 ブログ画像（{image_count}枚）"
    )


# =========================
# 一覧・詳細情報の統合
# =========================

def merge_blog_detail(
    blog: dict[str, Any],
    detail: Any,
) -> dict[str, Any]:
    """
    一覧ページの情報と詳細ページの情報を統合する。

    詳細ページ側に値がある場合は、
    グループ名・メンバー名・タイトル・日時などを
    詳細情報で補完する。
    """

    merged = dict(
        blog
    )

    if not isinstance(
        detail,
        dict,
    ):
        return merged

    fields = [
        "group",
        "member",
        "title",
        "date",
        "text",
    ]

    for field in fields:
        detail_value = detail.get(
            field,
            "",
        )

        if detail_value:
            merged[field] = detail_value

    # URLは一覧側のURLを優先する。
    # 一覧側にURLがない場合のみ詳細側を使用する。
    if not merged.get("url"):
        merged["url"] = detail.get(
            "url",
            "",
        )

    return merged


# =========================
# ブログデータ整理
# =========================

def normalize_blog_list(
    blogs: Any,
) -> list[dict[str, Any]]:
    """
    get_latest_blogから取得したデータを検証し、
    有効なブログ辞書だけを返す。
    """

    if not isinstance(
        blogs,
        list,
    ):
        print(
            "ブログ取得結果がlistではありません:",
            type(blogs).__name__,
        )
        return []

    valid_blogs: list[dict[str, Any]] = []

    for blog in blogs:
        if not isinstance(
            blog,
            dict,
        ):
            print(
                "不正データ:",
                blog,
            )
            continue

        url = str(
            blog.get("url")
            or ""
        ).strip()

        if not url:
            print(
                "URLが空のブログを除外:",
                blog,
            )
            continue

        valid_blogs.append(
            blog
        )

    return valid_blogs


# =========================
# 初回同期
# =========================

def initialize_current_blogs(
    blogs: list[dict[str, Any]],
) -> bool:
    """
    現在取得できるブログを通知せずDBへ登録する。

    初回起動時やDB移行後に、過去記事が大量通知されることを
    防ぐための処理。

    戻り値:
        True:
            初回同期が完了した

        False:
            記事を取得できず、初回同期を見送った
    """

    if not blogs:
        print(
            "初回同期を見送ります:"
            " ブログを1件も取得できませんでした。"
        )
        return False

    inserted_count = save_blogs(
        blogs
    )

    mark_initial_sync_completed()

    print(
        "初回同期完了:",
        len(blogs),
        "件を確認、",
        inserted_count,
        "件を新規登録しました。",
    )

    print(
        "初回同期ではDiscord通知を送信しません。"
    )

    return True


# =========================
# 通知先取得
# =========================

def get_channel_ids(
    blog: dict[str, Any],
) -> list[int]:
    """
    ブログの通知先チャンネルIDを取得する。

    グループ別チャンネルと全体チャンネルを統合し、
    重複するチャンネルIDは1つにまとめる。
    """

    group = str(
        blog.get("group")
        or ""
    ).strip()

    configured_channels = BLOG_CHANNELS.get(
        group,
        [],
    )

    if isinstance(
        configured_channels,
        (list, tuple, set),
    ):
        channel_ids = list(
            configured_channels
        )
    elif configured_channels:
        channel_ids = [
            configured_channels
        ]
    else:
        channel_ids = []

    if ALL_BLOG_CHANNEL:
        channel_ids.append(
            ALL_BLOG_CHANNEL
        )

    normalized_ids: list[int] = []

    for channel_id in channel_ids:
        try:
            normalized_id = int(
                channel_id
            )
        except (
            TypeError,
            ValueError,
        ):
            print(
                "不正なチャンネルID:",
                channel_id,
            )
            continue

        if normalized_id not in normalized_ids:
            normalized_ids.append(
                normalized_id
            )

    return normalized_ids


# =========================
# Discord通知
# =========================

async def notify_channel(
    channel: Any,
    blog: dict[str, Any],
    images: list[str],
) -> None:
    """
    指定チャンネルへブログ情報と画像を送信する。
    """

    text = build_notification_text(
        blog,
        len(images),
    )

    await send_blog_media(
        channel=channel,
        text=text,
        image_urls=images,
        send_delay=1.0,
        article_url=str(
            blog.get("url")
            or ""
        ),
        group=str(
            blog.get("group")
            or ""
        ),
    )


# =========================
# 1記事の処理
# =========================

async def process_blog(
    bot: Any,
    blog: dict[str, Any],
) -> None:
    """
    未通知ブログ1件を取得・通知・DB保存する。
    """

    url = str(
        blog.get("url")
        or ""
    ).strip()

    if not url:
        return

    if is_notified(
        url
    ):
        print(
            "通知済み:",
            url,
        )
        return

    channel_ids = get_channel_ids(
        blog
    )

    if not channel_ids:
        print(
            "通知先なし:",
            blog.get(
                "group",
                "",
            ),
            url,
        )
        return

    # 詳細ページを取得
    detail = await asyncio.to_thread(
        get_images,
        url,
    )

    # 一覧情報と詳細情報を統合
    complete_blog = merge_blog_detail(
        blog,
        detail,
    )

    if isinstance(
        detail,
        dict,
    ):
        raw_images = detail.get(
            "images",
            [],
        )
    else:
        raw_images = []

    if isinstance(
        raw_images,
        list,
    ):
        images = [
            str(image_url).strip()
            for image_url in raw_images
            if str(image_url).strip()
        ]
    else:
        images = []

    print(
        "詳細情報:",
        complete_blog.get(
            "group",
            "",
        ),
        complete_blog.get(
            "member",
            "",
        ),
        complete_blog.get(
            "title",
            "",
        ),
        complete_blog.get(
            "date",
            "",
        ),
    )

    print(
        "画像取得:",
        len(images),
        "枚",
    )

    all_succeeded = True
    notified_count = 0

    for channel_id in channel_ids:
        channel = bot.get_channel(
            channel_id
        )

        if channel is None:
            print(
                "チャンネル取得失敗:",
                channel_id,
            )

            all_succeeded = False
            continue

        try:
            await notify_channel(
                channel,
                complete_blog,
                images,
            )

            notified_count += 1

            print(
                "通知完了:",
                channel_id,
            )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            all_succeeded = False

            print(
                (
                    "通知エラー "
                    f"channel={channel_id} "
                    f"url={url}:"
                ),
                error,
            )

    # 1か所以上に通知でき、
    # すべての通知先で成功した場合だけDBへ保存する。
    if (
        notified_count > 0
        and all_succeeded
    ):
        inserted = save_blog(
            url,
            str(
                complete_blog.get(
                    "group",
                    "",
                )
            ),
            str(
                complete_blog.get(
                    "member",
                    "",
                )
            ),
            str(
                complete_blog.get(
                    "title",
                    "",
                )
            ),
            str(
                complete_blog.get(
                    "date",
                    "",
                )
            ),
        )

        if inserted:
            print(
                "通知済みDB保存:",
                url,
            )
        else:
            print(
                "通知済みDB登録済み:",
                url,
            )

    else:
        print(
            "通知に失敗したためDB保存を見送ります:",
            url,
        )


# =========================
# 監視ループ
# =========================

async def check_blog(
    bot: Any,
) -> None:
    """
    ブログを定期的に確認し、
    新着記事だけをDiscordへ通知する。
    """

    while not bot.is_closed():
        try:
            raw_blogs = await asyncio.to_thread(
                get_latest_blog
            )

            blogs = normalize_blog_list(
                raw_blogs
            )

            print(
                "取得ブログ:",
                len(blogs),
                "件",
            )

            # 初回同期が未完了なら、
            # 現在取得できる記事を通知せずDBへ登録する。
            if not is_initial_sync_completed():
                initialize_current_blogs(
                    blogs
                )

            else:
                new_blog_count = 0

                for blog in blogs:
                    url = str(
                        blog.get("url")
                        or ""
                    ).strip()

                    if is_notified(
                        url
                    ):
                        print(
                            "通知済み:",
                            url,
                        )
                        continue

                    new_blog_count += 1

                    await process_blog(
                        bot,
                        blog,
                    )

                print(
                    "未通知記事:",
                    new_blog_count,
                    "件",
                )

        except asyncio.CancelledError:
            print(
                "ブログ監視タスクを終了します。"
            )
            raise

        except Exception as error:
            print(
                "ブログ監視エラー:",
                repr(error),
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )
