import asyncio
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp
import discord
from discord.ext import commands, tasks

from archive_checker import get_archive_targets
from archive_config import (
    ARCHIVE_INTERVAL,
    SEND_DELAY,
    archive_active_hours_text,
    get_archive_local_now,
    is_archive_active_time,
)
from archive_database import (
    archive_count,
    claim_image_urls,
    get_unsent_image_urls,
    init_archive_db,
    mark_image_urls_sent,
    release_image_claims,
    reset_archive,
    save_archive,
)
from archive_image_getter import get_images
from archive_media import download_attachment
from archive_parsers.utils import normalize_member_name

from photo_ai_analyzer import (
    analyze_pending_images,
    analyze_photo_image,
    get_photo_ai_status,
)
from photo_database import (
    get_photo_db_counts,
    get_photo_storage_stats,
    init_photo_db,
    save_photo_blog,
    save_photo_images,
)
from photo_image_downloader import (
    download_blog_images,
    get_photo_storage_path,
)
from low_egress_media import (
    IMAGE_EMBEDS_PER_MESSAGE,
    LOW_EGRESS_MODE,
    build_image_embeds,
    clean_http_urls,
)

from photo_archive_runner import (
    is_photo_archive_running,
    request_photo_archive_stop,
    repair_zero_image_photo_blogs,
    run_photo_archive_once,
)
from photo_commands import register_photo_commands
from control_panel import (
    add_persistent_control_panel_views,
    register_control_panel,
)


# =========================
# Discord設定
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

register_photo_commands(bot)
register_control_panel(bot)

# =========================
# チャンネル設定
# =========================

ARCHIVE_ALL_CHANNEL = 1527929665480556635

ARCHIVE_GROUP_CHANNELS = {

    "乃木坂46": 1527929694220193812,
    "櫻坂46": 1527929780891160696,
    "日向坂46": 1527929807986364556

}





# =========================
# メンバー別
# =========================

ARCHIVE_MEMBER_CHANNELS = {


    # 乃木坂46

    "伊藤理々杏": 1527930403518812170,

    "岩本蓮加": 1527930443612033024,

    "吉田綾乃クリスティー": 1527930466496155658,

    "遠藤さくら": 1527930489233473607,

    "賀喜遥香": 1527930520850137107,

    "金川紗耶": 1527930862715404289,

    "黒見明香": 1527930918457708574,

    "柴田柚菜": 1527931048263028827,

    "田村真佑": 1527931130471518349,

    "筒井あやめ": 1527931187488755823,

    "林瑠奈": 1527931233911308338,

    "弓木奈於": 1527931294929916045,


    "五百城茉央": 1527931357546938368,

    "池田瑛紗": 1527931417827479582,

    "一ノ瀬美空": 1527931501210112040,

    "井上和": 1527931556411211947,

    "岡本姫奈": 1527931609045536818,

    "小川彩": 1527931658936783028,

    "奥田いろは": 1527931702859665438,

    "川﨑桜": 1527931748330242108,

    "菅原咲月": 1527931809097060463,

    "冨里奈央": 1527931859898597527,

    "中西アルノ": 1527931911287214140,


    # 乃木坂6期生

    "愛宕心響": 1527931959962112080,

    "大越ひなの": 1527932018355077175,

    "小津玲奈": 1527932077205618698,

    "海邉朱莉": 1527932192095731832,

    "川端晃菜": 1527932323860058202,

    "鈴木佑捺": 1527932379316883537,

    "瀬戸口心月": 1527932423181041754,

    "長嶋凛桜": 1527932466906665002,

    "増田三莉音": 1527932511156568164,

    "森平麗心": 1527932554429206580,

    "矢田萌華": 1527932598284845168,

    # =========================
    # 櫻坂46
    # =========================

    "遠藤光莉": 1527934512791683165,

    "大園玲": 1527934565002383502,

    "大沼晶保": 1527934615514513538,

    "幸阪茉里乃": 1527934682229116968,

    "田村保乃": 1527934735563755580,

    "藤吉夏鈴": 1527934782183571626,

    "増本綺良": 1527934840035348480,

    "松田里奈": 1527934887456407793,

    "森田ひかる": 1527934948332408903,

    "守屋麗奈": 1527935003214872636,

    "山﨑天": 1527935052036571156,

    "石森璃花": 1527935110538596483,

    "遠藤理子": 1527935160077520936,

    "小田倉麗奈": 1527935206940737556,

    "小島凪紗": 1527935254197833779,

    "谷口愛季": 1527935315816353842,

    "中嶋優月": 1527935370669461636,

    "的野美青": 1527935423903698945,

    "向井純葉": 1527935468921032784,

    "村井優": 1527935517788999680,

    "村山美羽": 1527935561006972948,

    "山下瞳月": 1527935621283450930,


    # =========================
    # 櫻坂46 4期生
    # =========================

    "浅井恋乃未": 1527935664300097636,

    "稲熊ひな": 1527935720944042034,

    "勝又春": 1527935775604477993,

    "佐藤愛桜": 1527935838401466368,

    "中川智尋": 1527935892184891524,

    "松本和子": 1527935942734774293,

    "目黒陽色": 1527935996291846204,

    "山川宇衣": 1527936046854180994,

    "山田桃実": 1527936102722306068,



    # =========================
    # 日向坂46
    # =========================

    "金村美玖": 1527937333377237143,

    "小坂菜緒": 1527937391648837733,

    "上村ひなの": 1527937465405673523,

    "髙橋未来虹": 1527937516609601636,

    "森本茉莉": 1527937567268536430,

    "山口陽世": 1527937615720874034,

    "石塚瑶季": 1527937664681119894,

    "小西夏菜実": 1527937713343434843,

    "清水理央": 1527937766573215834,

    "正源司陽子": 1527937810902810685,

    "竹内希来里": 1527937861339451556,

    "平尾帆夏": 1527937938216714412,

    "平岡海月": 1527938000330428476,

    "藤嶌果歩": 1527938049181220954,

    "宮地すみれ": 1527938101484453908,

    "山下葉留花": 1527938150880772096,

    "渡辺莉奈": 1527938196829376674,


    # 日向坂46 5期生

    "大田美月": 1527938246846320762,

    "大野愛実": 1527938290831851691,

    "片山紗希": 1527938341402579185,

    "蔵盛妃那乃": 1527938389024702465,

    "坂井新奈": 1527938431651414066,

    "佐藤優羽": 1527938483669303296,

    "下田衣珠季": 1527938588367650817,

    "高井俐香": 1527938644201963642,

    "鶴崎仁香": 1527938689055854635,

    "松尾桜": 1527938740738064434,


    # =========================
    # その他
    # =========================

    "ポカ": 1527938802826477589

}


# =========================
# 共通設定
# =========================

def env_flag(name: str, default: bool) -> bool:
    """環境変数を安全に真偽値へ変換する。"""

    default_text = "true" if default else "false"

    return (
        os.getenv(name, default_text)
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


PHOTO_AI_AUTO_ANALYZE = env_flag(
    "PHOTO_AI_AUTO_ANALYZE",
    True,
)

# Railwayの再起動後も巡回を自動復旧する。
# Variablesでfalseにすれば従来どおり手動開始に戻せる。
AUTO_START_ARCHIVE = env_flag(
    "AUTO_START_ARCHIVE",
    True,
)

AUTO_START_PHOTO_ARCHIVE = env_flag(
    "AUTO_START_PHOTO_ARCHIVE",
    True,
)

# 未解析画像を定期的に確認して自動解析する。
# falseにすると!ai_analyzeによる手動実行だけになる。
AUTO_START_AI_ANALYSIS = env_flag(
    "AUTO_START_AI_ANALYSIS",
    True,
)

PHOTO_AI_AUTO_LIMIT = max(
    int(
        os.getenv(
            "PHOTO_AI_AUTO_LIMIT",
            "20",
        )
    ),
    1,
)

DISCORD_FILE_MARGIN = 512 * 1024
DEFAULT_UPLOAD_LIMIT = 8 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
FAILED_URL_MESSAGE_LIMIT = 1900

PHOTO_ARCHIVE_INTERVAL = max(
    int(os.getenv("PHOTO_ARCHIVE_INTERVAL", "3600")),
    60,
)

# AI解析キューの確認間隔。APIへ画像を送るのはpending画像がある時だけ。
PHOTO_AI_AUTO_INTERVAL = max(
    int(os.getenv("PHOTO_AI_AUTO_INTERVAL", "60")),
    30,
)

archive_cycle_lock = asyncio.Lock()
ai_analysis_lock = asyncio.Lock()
startup_initialized = False
blog_archive_window_state: bool | None = None

T = TypeVar("T")

ARCHIVE_RETRY_DELAYS = (0, 5, 10)

archive_runtime_status: dict[str, Any] = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_result": "未実行",
    "last_error": None,
    "last_targets": 0,
    "last_completed": 0,
    "last_failed": 0,
    "last_duration": 0.0,
}


# =========================
# 補助関数
# =========================

def local_time_text(value: datetime | None) -> str:
    if value is None:
        return "未実行"
    return value.astimezone().strftime("%Y年%m月%d日 %H:%M:%S")


def log_event(level: str, message: str) -> None:
    print(f"[{level.upper()}] {message}")


def blog_archive_allowed() -> bool:
    """ブログアーカイブが現在の稼働時間帯か判定する。

    写真アーカイブとAI自動解析には、この時間制限を適用しない。
    """
    global blog_archive_window_state

    allowed = is_archive_active_time()

    # 状態が切り替わった時だけログを出し、短い巡回間隔でもログを汚さない。
    if blog_archive_window_state is None or blog_archive_window_state != allowed:
        blog_archive_window_state = allowed
        now_text = get_archive_local_now().strftime("%Y-%m-%d %H:%M:%S %Z")
        if allowed:
            log_event(
                "INFO",
                f"ブログアーカイブ稼働時間に入りました: {now_text} / "
                f"{archive_active_hours_text()}",
            )
        else:
            log_event(
                "INFO",
                f"ブログアーカイブは時間外のため待機します: {now_text} / "
                f"{archive_active_hours_text()}",
            )

    return allowed


async def run_with_retry(
    operation_name: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    last_error: Exception | None = None

    for attempt, delay in enumerate(ARCHIVE_RETRY_DELAYS, start=1):
        if delay > 0:
            log_event(
                "WARNING",
                f"{operation_name}を{delay}秒後に再試行します "
                f"({attempt}/{len(ARCHIVE_RETRY_DELAYS)})",
            )
            await asyncio.sleep(delay)

        try:
            return await operation()
        except Exception as error:
            last_error = error
            log_event(
                "WARNING",
                f"{operation_name}に失敗しました "
                f"({attempt}/{len(ARCHIVE_RETRY_DELAYS)}): {error}",
            )

    assert last_error is not None
    raise last_error


def format_bytes(
    size: int,
) -> str:
    """
    バイト数を読みやすい単位へ変換する。
    """

    value = float(
        max(
            int(size),
            0,
        )
    )

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:

        if (
            value < 1024
            or unit == units[-1]
        ):

            if unit == "B":
                return f"{int(value)} {unit}"

            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{int(size)} B"


def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """
    値を安全に整数へ変換する。
    """

    try:
        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def get_channel_upload_limit(
    channel: discord.abc.Messageable,
) -> int:
    """
    Discordチャンネルの添付上限を取得する。
    """

    guild = getattr(
        channel,
        "guild",
        None,
    )

    if guild:

        limit = safe_int(
            getattr(
                guild,
                "filesize_limit",
                0,
            )
        )

        if limit > 0:
            return limit

    return DEFAULT_UPLOAD_LIMIT


def close_discord_files(
    attachments: list[dict[str, Any]],
) -> None:
    """
    discord.Fileが保持するファイルを安全に閉じる。
    """

    for attachment in attachments:

        file = attachment.get(
            "file"
        )

        if not file:
            continue

        try:
            file.close()

        except Exception:
            pass


def build_file_groups(
    attachments: list[dict[str, Any]],
    upload_limit: int,
) -> list[list[dict[str, Any]]]:
    """
    添付をDiscordの件数上限・容量上限内に分割する。
    """

    groups: list[
        list[dict[str, Any]]
    ] = []

    current_group: list[
        dict[str, Any]
    ] = []

    current_size = 0

    group_size_limit = max(
        upload_limit
        - DISCORD_FILE_MARGIN,
        1 * 1024 * 1024,
    )

    for attachment in attachments:

        attachment_size = max(
            safe_int(
                attachment.get(
                    "size",
                    0,
                )
            ),
            0,
        )

        should_split = (
            len(
                current_group
            )
            >= MAX_FILES_PER_MESSAGE
            or (
                bool(
                    current_group
                )
                and (
                    current_size
                    + attachment_size
                    > group_size_limit
                )
            )
        )

        if should_split:

            groups.append(
                current_group
            )

            current_group = []
            current_size = 0

        current_group.append(
            attachment
        )

        current_size += (
            attachment_size
        )

    if current_group:

        groups.append(
            current_group
        )

    return groups


async def send_failed_urls(
    channel: discord.abc.Messageable,
    failed_urls: list[str],
) -> None:
    """
    添付できなかった画像URLをDiscordへ送信する。
    """

    if not failed_urls:
        return

    unique_urls = list(
        dict.fromkeys(
            str(url)
            for url in failed_urls
            if str(url).strip()
        )
    )

    if not unique_urls:
        return

    heading = (
        "⚠️ 容量または変換エラーのため、"
        "添付できなかった画像です。\n"
    )

    current_message = heading

    for failed_url in unique_urls:

        line = (
            f"・{failed_url}\n"
        )

        if (
            len(
                current_message
            )
            + len(
                line
            )
            > FAILED_URL_MESSAGE_LIMIT
        ):

            await channel.send(
                current_message.rstrip(),
                suppress_embeds=True,
            )

            await asyncio.sleep(
                SEND_DELAY
            )

            current_message = heading

        current_message += line

    if current_message != heading:

        await channel.send(
            current_message.rstrip(),
            suppress_embeds=True,
        )

        await asyncio.sleep(
            SEND_DELAY
        )


def build_archive_embed(
    blog: dict[str, Any],
    image_count: int,
) -> discord.Embed:
    """
    ブログ通知用Embedを作成する。
    """

    blog_url = str(
        blog.get(
            "url",
            "",
        )
    ).strip()

    embed = discord.Embed(
        title=(
            blog.get(
                "title"
            )
            or "無題"
        ),
        url=blog_url or None,
        color=0x00AAFF,
    )

    embed.add_field(
        name="🏷️ グループ",
        value=(
            blog.get(
                "group"
            )
            or "不明"
        ),
        inline=True,
    )

    embed.add_field(
        name="👤 メンバー",
        value=(
            blog.get(
                "member"
            )
            or "不明"
        ),
        inline=True,
    )

    embed.add_field(
        name="📅 投稿日時",
        value=(
            blog.get(
                "date"
            )
            or "不明"
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            "Archive BOT"
            f" • 画像総数 {image_count}枚"
        )
    )

    return embed


# =========================
# 投稿先取得
# =========================

def get_channels(
    blog: dict[str, Any],
) -> list[
    discord.abc.Messageable
]:
    """
    全体・グループ・メンバー別の送信先を重複なしで返す。
    """

    channels: list[
        discord.abc.Messageable
    ] = []

    seen: set[int] = set()

    channel_ids: list[
        tuple[str, int | None]
    ] = [
        (
            "全体",
            ARCHIVE_ALL_CHANNEL,
        ),
    ]

    group = str(
        blog.get(
            "group",
            "",
        )
    ).strip()

    channel_ids.append(
        (
            f"グループ:{group}",
            ARCHIVE_GROUP_CHANNELS.get(
                group
            ),
        )
    )

    member = normalize_member_name(
        blog.get(
            "member",
            "",
        )
    )

    channel_ids.append(
        (
            f"メンバー:{member}",
            ARCHIVE_MEMBER_CHANNELS.get(
                member
            ),
        )
    )

    for label, channel_id in channel_ids:

        if not channel_id:
            continue

        channel = bot.get_channel(
            int(
                channel_id
            )
        )

        if not channel:

            print(
                "送信先チャンネル取得失敗:",
                label,
                channel_id,
            )

            continue

        channel_real_id = safe_int(
            getattr(
                channel,
                "id",
                0,
            )
        )

        if (
            channel_real_id <= 0
            or channel_real_id in seen
        ):
            continue

        channels.append(
            channel
        )

        seen.add(
            channel_real_id
        )

    if (
        member
        and member
        not in ARCHIVE_MEMBER_CHANNELS
    ):

        print(
            "メンバー別チャンネル未設定:",
            repr(
                member
            ),
        )

    return channels


# =========================
# 写真DB・AI処理
# =========================

async def archive_photos_and_analyze(
    session: aiohttp.ClientSession,
    blog: dict[str, Any],
    image_urls: list[str],
) -> dict[str, Any]:
    """
    記事・画像URLを写真DBへ登録し、
    Railway Volumeへ保存してAI解析する。

    この処理の失敗でDiscordアーカイブ全体は止めない。
    """

    result: dict[str, Any] = {
        "blog_id": 0,
        "registered": 0,
        "downloaded": 0,
        "download_failed": 0,
        "analyzed": 0,
        "analysis_review": 0,
        "analysis_failed": 0,
    }

    if not image_urls:

        return result

    try:

        blog_id = await asyncio.to_thread(
            save_photo_blog,
            blog,
        )

        image_records = await asyncio.to_thread(
            save_photo_images,
            blog_id,
            image_urls,
        )

        result[
            "blog_id"
        ] = blog_id

        result[
            "registered"
        ] = len(
            image_records
        )

    except Exception as error:

        print(
            "写真DB登録エラー:",
            blog.get(
                "url",
                "",
            ),
            error,
        )

        return result

    try:

        download_result = (
            await download_blog_images(
                session,
                blog_id=blog_id,
                blog=blog,
                image_records=image_records,
            )
        )

        result[
            "downloaded"
        ] = safe_int(
            download_result.get(
                "success",
                0,
            )
        )

        result[
            "download_failed"
        ] = safe_int(
            download_result.get(
                "failed",
                0,
            )
        )

    except Exception as error:

        print(
            "写真画像一括保存エラー:",
            blog.get(
                "url",
                "",
            ),
            error,
        )

        return result

    if not PHOTO_AI_AUTO_ANALYZE:

        print(
            "写真AI自動解析は無効です。"
        )

        return result

    ai_status = get_photo_ai_status()

    if not ai_status.get(
        "enabled"
    ):

        print(
            "OPENAI_API_KEY未設定のため、"
            "AI解析をスキップします。"
        )

        return result

    downloaded_ids = [
        safe_int(
            item.get(
                "image_id",
                0,
            )
        )
        for item in download_result.get(
            "results",
            []
        )
        if item.get(
            "success"
        )
    ]

    for image_id in downloaded_ids:

        if image_id <= 0:
            continue

        try:

            analysis = await analyze_photo_image(
                image_id
            )

            status = str(
                analysis.get(
                    "status",
                    "",
                )
            )

            if status == "completed":

                result[
                    "analyzed"
                ] += 1

            elif status == "review":

                result[
                    "analysis_review"
                ] += 1

            else:

                result[
                    "analysis_failed"
                ] += 1

        except Exception as error:

            result[
                "analysis_failed"
            ] += 1

            print(
                "写真AI解析エラー:",
                image_id,
                error,
            )

    print(
        "写真保存・AI解析結果:",
        result,
    )

    return result


# =========================
# Discord送信
# =========================

async def prepare_attachments(
    session: aiohttp.ClientSession,
    image_urls: list[str],
    upload_limit: int,
) -> tuple[
    list[dict[str, Any]],
    list[str],
]:
    """
    Discord添付用画像を準備する。
    """

    attachments: list[
        dict[str, Any]
    ] = []

    failed_urls: list[str] = []

    for image_index, image_url in enumerate(
        image_urls,
        start=1,
    ):

        try:

            attachment = await download_attachment(
                session,
                image_url,
                image_index,
                upload_limit,
            )

        except Exception as error:

            print(
                "Discord添付準備エラー:",
                image_url,
                error,
            )

            failed_urls.append(
                image_url
            )

            continue

        if not attachment:

            failed_urls.append(
                image_url
            )

            continue

        file = attachment.get(
            "file"
        )

        if not file:

            reason = attachment.get(
                "reason",
                "送信できませんでした。",
            )

            print(
                "画像送信対象外:",
                reason,
                image_url,
            )

            failed_urls.append(
                image_url
            )

            continue

        attachments.append(
            {
                "file": file,
                "size": attachment.get(
                    "size",
                    0,
                ),
                "url": image_url,
            }
        )

    return (
        attachments,
        failed_urls,
    )


async def send_blog_to_channel(
    session: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    embed: discord.Embed,
    image_urls: list[str],
    blog_url: str,
) -> bool:
    """
    1チャンネルへEmbedと画像を送信する。

    画像は記事URL・チャンネルID・画像URLの組み合わせで送信履歴を保存する。
    途中で一部だけ成功した場合も、成功済み画像は次回送らない。
    """
    channel_id = str(getattr(channel, "id", "unknown"))

    pending_urls = await asyncio.to_thread(
        get_unsent_image_urls,
        blog_url,
        channel_id,
        image_urls,
    )

    try:
        # 記事ヘッダーは、未送信画像がある場合または画像なし記事で送る。
        if pending_urls or not image_urls:
            await channel.send(embed=embed)
            await asyncio.sleep(SEND_DELAY)

        if not pending_urls:
            print(
                "画像重複防止: このチャンネルへの未送信画像はありません:",
                channel_id,
                blog_url,
            )
            return True

        if LOW_EGRESS_MODE:
            urls = clean_http_urls(pending_urls)
            for start in range(0, len(urls), IMAGE_EMBEDS_PER_MESSAGE):
                group_urls = urls[start:start + IMAGE_EMBEDS_PER_MESSAGE]
                claimed_urls = await asyncio.to_thread(
                    claim_image_urls,
                    blog_url,
                    channel_id,
                    group_urls,
                )
                if not claimed_urls:
                    continue

                try:
                    message = await channel.send(
                        embeds=build_image_embeds(claimed_urls),
                    )
                    await asyncio.to_thread(
                        mark_image_urls_sent,
                        blog_url,
                        channel_id,
                        claimed_urls,
                        str(getattr(message, "id", "")),
                    )
                except Exception:
                    # Discord APIが明確に失敗を返した場合は、次回再試行できるよう予約解除。
                    await asyncio.to_thread(
                        release_image_claims,
                        blog_url,
                        channel_id,
                        claimed_urls,
                    )
                    raise

                if SEND_DELAY > 0 and start + IMAGE_EMBEDS_PER_MESSAGE < len(urls):
                    await asyncio.sleep(SEND_DELAY)

            return True

        upload_limit = get_channel_upload_limit(channel)
        attachments, failed_urls = await prepare_attachments(
            session,
            pending_urls,
            upload_limit,
        )

        # ダウンロードできなかった画像は予約前なので、そのまま次回再試行される。
        groups = build_file_groups(attachments, upload_limit)
        try:
            for group in groups:
                group_urls = [item["url"] for item in group]
                claimed_urls = await asyncio.to_thread(
                    claim_image_urls,
                    blog_url,
                    channel_id,
                    group_urls,
                )
                if not claimed_urls:
                    continue

                claimed_set = set(claimed_urls)
                send_group = [item for item in group if item["url"] in claimed_set]
                try:
                    message = await channel.send(
                        files=[item["file"] for item in send_group]
                    )
                    await asyncio.to_thread(
                        mark_image_urls_sent,
                        blog_url,
                        channel_id,
                        claimed_urls,
                        str(getattr(message, "id", "")),
                    )
                except Exception:
                    await asyncio.to_thread(
                        release_image_claims,
                        blog_url,
                        channel_id,
                        claimed_urls,
                    )
                    raise

                await asyncio.sleep(SEND_DELAY)

            await send_failed_urls(channel, failed_urls)
            return not failed_urls
        finally:
            close_discord_files(attachments)

    except Exception as error:
        print(
            "チャンネル送信エラー:",
            channel_id,
            error,
        )
        return False


# =========================
# AI自動解析
# =========================

async def run_ai_analysis_batch(
    limit: int | None = None,
) -> dict[str, Any]:
    """AI解析を多重起動させずに1バッチ実行する。"""

    if ai_analysis_lock.locked():
        return {
            "requested": limit or PHOTO_AI_AUTO_LIMIT,
            "found": 0,
            "completed": 0,
            "review": 0,
            "failed": 0,
            "skipped": True,
        }

    async with ai_analysis_lock:
        return await analyze_pending_images(
            limit or PHOTO_AI_AUTO_LIMIT
        )


# =========================
# 起動時処理
# =========================

@bot.event
async def on_ready() -> None:

    global startup_initialized

    # 常設ボタンを再起動後も有効にする。
    add_persistent_control_panel_views(bot)

    print(
        "=" * 50
    )

    print(
        f"ログイン成功: {bot.user}"
    )

    if not startup_initialized:

        try:

            await asyncio.to_thread(
                init_archive_db
            )

            print(
                "アーカイブDB初期化完了"
            )

        except Exception as error:

            print(
                "アーカイブDB初期化エラー:",
                error,
            )

        try:

            await asyncio.to_thread(
                init_photo_db
            )

            print(
                "写真検索DB初期化完了"
            )

        except Exception as error:

            print(
                "写真検索DB初期化エラー:",
                error,
            )

        startup_initialized = True

    ai_status = get_photo_ai_status()

    print(
        "保存済みチェック間隔:",
        f"{ARCHIVE_INTERVAL}秒",
    )

    print(
        "ブログアーカイブ稼働時間:",
        archive_active_hours_text(),
    )

    print(
        "写真保存先:",
        get_photo_storage_path(),
    )

    print(
        "AI解析:",
        (
            "有効"
            if ai_status.get(
                "enabled"
            )
            else "無効"
        ),
    )

    print(
        "AIモデル:",
        ai_status.get(
            "model",
            "不明",
        ),
    )

    print(
        "AI自動解析:",
        (
            "有効"
            if PHOTO_AI_AUTO_ANALYZE
            else "無効"
        ),
    )

    print(
        "写真アーカイブ間隔:",
        f"{PHOTO_ARCHIVE_INTERVAL}秒",
    )

    print(
        "AI解析キュー確認間隔:",
        f"{PHOTO_AI_AUTO_INTERVAL}秒",
    )

    if AUTO_START_ARCHIVE:

        if not archive_loop.is_running():
            archive_loop.start()
            print("ブログアーカイブを自動開始しました。")
        else:
            print("ブログアーカイブはすでに動作中です。")

    else:
        print(
            "ブログアーカイブ自動開始: 無効 "
            "(!archive_start で開始)"
        )

    if AUTO_START_PHOTO_ARCHIVE:

        if not photo_archive_loop.is_running():
            photo_archive_loop.start()
            print("写真アーカイブを自動開始しました。")
        else:
            print("写真アーカイブはすでに動作中です。")

    else:
        print(
            "写真アーカイブ自動開始: 無効 "
            "(!photo_archive_start で開始)"
        )

    if AUTO_START_AI_ANALYSIS:

        if not ai_analysis_loop.is_running():
            ai_analysis_loop.start()
            print("AI解析ワーカーを自動開始しました。")
        else:
            print("AI解析ワーカーはすでに動作中です。")

    else:
        print(
            "AI解析ワーカー自動開始: 無効 "
            "(!ai_analyze で手動実行)"
        )

    print(
        "=" * 50
    )


# =========================
# 統合ステータス
# =========================

@bot.command(name="status")
@commands.is_owner()
async def status_command(ctx: commands.Context) -> None:
    try:
        archive_total, photo_counts, storage_stats = await asyncio.gather(
            asyncio.to_thread(archive_count),
            asyncio.to_thread(get_photo_db_counts),
            asyncio.to_thread(get_photo_storage_stats),
        )
    except Exception as error:
        log_event("ERROR", f"ステータス取得エラー: {error}")
        await ctx.send(f"⚠️ ステータス取得に失敗しました。\n`{error}`")
        return

    ai_status = get_photo_ai_status()
    storage_bytes = int(storage_stats.get("total_size", 0) or 0)

    await ctx.send(
        "🤖 **アーカイブBotステータス**\n"
        f"Bot接続: **{'稼働中' if bot.is_ready() else '準備中'}**\n"
        f"通知アーカイブ定期巡回: **{'動作中' if archive_loop.is_running() else '停止中'}**\n"
        f"通知アーカイブ現在処理: **{'実行中' if archive_cycle_lock.locked() else '停止中'}**\n"
        f"写真アーカイブ定期巡回: **{'動作中' if photo_archive_loop.is_running() else '停止中'}**\n"
        f"AI解析ワーカー: **{'動作中' if ai_analysis_loop.is_running() else '停止中'}**\n"
        f"写真アーカイブ現在処理: **{'実行中' if is_photo_archive_running() else '停止中'}**\n"
        f"通知済み記事: **{archive_total}件**\n"
        f"写真DBブログ: **{photo_counts.get('blogs', 0)}件**\n"
        f"写真DB画像: **{photo_counts.get('images', 0)}件**\n"
        f"確認待ち: **{photo_counts.get('pending_reviews', 0)}件**\n"
        f"保存容量: **{format_bytes(storage_bytes)}**\n"
        f"AI解析: **{'有効' if ai_status.get('enabled') else '無効'}**\n"
        f"前回開始: **{local_time_text(archive_runtime_status.get('last_started'))}**\n"
        f"前回完了: **{local_time_text(archive_runtime_status.get('last_finished'))}**\n"
        f"前回結果: **{archive_runtime_status.get('last_result', '未実行')}**\n"
        f"前回対象: **{archive_runtime_status.get('last_targets', 0)}件**\n"
        f"前回成功: **{archive_runtime_status.get('last_completed', 0)}件**\n"
        f"前回失敗: **{archive_runtime_status.get('last_failed', 0)}件**\n"
        f"前回処理時間: **{archive_runtime_status.get('last_duration', 0.0):.2f}秒**\n"
        f"前回エラー: **{archive_runtime_status.get('last_error') or 'なし'}**"
    )


# =========================
# アーカイブ操作コマンド
# =========================

@bot.command(
    name="archive_start"
)
@commands.is_owner()
async def archive_start(
    ctx: commands.Context,
) -> None:

    if archive_loop.is_running():

        await ctx.send(
            "⚠️ アーカイブはすでに動作中です。"
        )

        return

    archive_loop.start()

    await ctx.send(
        "▶️ ブログアーカイブを開始しました。"
    )

    print(
        "アーカイブ巡回を開始しました。"
    )


@bot.command(
    name="archive_stop"
)
@commands.is_owner()
async def archive_stop(
    ctx: commands.Context,
) -> None:

    if not archive_loop.is_running():

        await ctx.send(
            "⚠️ アーカイブは停止中です。"
        )

        return

    archive_loop.cancel()

    await ctx.send(
        "🛑 ブログアーカイブを停止しました。"
    )

    print(
        "アーカイブ巡回を停止しました。"
    )


@bot.command(
    name="archive_reset"
)
@commands.is_owner()
async def archive_reset(
    ctx: commands.Context,
) -> None:
    """
    通知済み判定だけをリセットする。

    写真DBとRailway Volume内の画像は削除しない。
    """

    was_running = (
        archive_loop.is_running()
    )

    if was_running:

        archive_loop.cancel()

    try:

        await asyncio.to_thread(
            reset_archive
        )

        await ctx.send(
            "🧹 アーカイブの通知済み履歴をリセットしました。\n"
            "写真DBと保存画像は削除していません。"
        )

        print(
            "アーカイブ通知済み履歴をリセットしました。"
        )

    except Exception as error:

        print(
            "アーカイブDBリセットエラー:",
            error,
        )

        await ctx.send(
            "⚠️ DBリセットに失敗しました。\n"
            f"`{error}`"
        )

    if was_running:

        archive_loop.start()

        await ctx.send(
            "▶️ アーカイブ巡回を再開しました。"
        )


@bot.command(
    name="archive_count"
)
@commands.is_owner()
async def archive_count_command(
    ctx: commands.Context,
) -> None:

    count = await asyncio.to_thread(
        archive_count
    )

    await ctx.send(
        "📦 現在のアーカイブ件数: "
        f"**{count}件**"
    )


@bot.command(
    name="archive_run"
)
@commands.is_owner()
async def archive_run_once(
    ctx: commands.Context,
) -> None:

    if archive_cycle_lock.locked():

        await ctx.send(
            "⚠️ 現在アーカイブ処理中です。"
        )

        return

    await ctx.send(
        "🔄 アーカイブを1回実行します。"
    )

    await run_archive_cycle()

    await ctx.send(
        "✅ アーカイブの1回実行が完了しました。"
    )


# =========================
# 写真アーカイブ操作コマンド
# =========================

@bot.command(
    name="photo_archive_run"
)
@commands.is_owner()
async def photo_archive_run_command(
    ctx: commands.Context,
    limit: int | None = None,
    group: str | None = None,
) -> None:

    if is_photo_archive_running():

        await ctx.send(
            "⚠️ 写真アーカイブは現在処理中です。"
        )

        return

    selected_limit = None

    if limit is not None:

        selected_limit = max(
            min(int(limit), 100),
            1,
        )

    await ctx.send(
        "📷 写真アーカイブを1回実行します。\n"
        "Discordへのブログ再通知は行いません。"
    )

    result = await run_photo_archive_once(
        limit=selected_limit,
        group=group,
    )

    await ctx.send(
        "✅ **写真アーカイブ実行結果**\n"
        f"状態: **{result.get('status', '不明')}**\n"
        f"収集記事: **{result.get('collected', 0)}件**\n"
        f"写真DB未登録: **{result.get('unregistered', 0)}件**\n"
        f"処理: **{result.get('processed', 0)}件**\n"
        f"完了: **{result.get('completed', 0)}件**\n"
        f"画像なし: **{result.get('no_images', 0)}件**\n"
        f"失敗: **{result.get('failed', 0)}件**\n"
        f"画像保存: **{result.get('downloaded', 0)}件**\n"
        f"AI解析完了: **{result.get('analyzed', 0)}件**\n"
        f"AI確認待ち: **{result.get('analysis_review', 0)}件**"
    )


@bot.command(
    name="photo_archive_repair_zero"
)
@commands.is_owner()
async def photo_archive_repair_zero_command(
    ctx: commands.Context,
    limit: int = 100,
    group: str | None = None,
) -> None:
    """画像0件で登録済みの記事を再判定して修復する。"""

    if is_photo_archive_running():
        await ctx.send("⚠️ 写真アーカイブは現在処理中です。")
        return

    selected_limit = max(1, min(int(limit), 1000))
    await ctx.send(
        "🛠️ 画像0件で登録済みの記事を再判定します。\n"
        f"上限: **{selected_limit}件** / "
        f"グループ: **{group or 'all'}**"
    )

    result = await repair_zero_image_photo_blogs(
        limit=selected_limit,
        group=group,
    )

    await ctx.send(
        "✅ **画像0件記事の再判定結果**\n"
        f"状態: **{result.get('status', '不明')}**\n"
        f"対象: **{result.get('targets', 0)}件**\n"
        f"処理: **{result.get('processed', 0)}件**\n"
        f"修復: **{result.get('repaired', 0)}件**\n"
        f"再判定後も画像なし: **{result.get('still_no_images', 0)}件**\n"
        f"失敗: **{result.get('failed', 0)}件**\n"
        f"画像保存: **{result.get('downloaded', 0)}件**\n"
        f"AI解析完了: **{result.get('analyzed', 0)}件**"
    )


@bot.command(
    name="photo_archive_start"
)
@commands.is_owner()
async def photo_archive_start_command(
    ctx: commands.Context,
) -> None:

    if photo_archive_loop.is_running():

        await ctx.send(
            "⚠️ 写真アーカイブの定期巡回はすでに動作中です。"
        )

        return

    photo_archive_loop.start()

    await ctx.send(
        "▶️ 写真アーカイブの定期巡回を開始しました。\n"
        f"間隔: **{PHOTO_ARCHIVE_INTERVAL}秒**\n"
        "Discordへのブログ再通知は行いません。"
    )

    print("写真アーカイブの定期巡回を開始しました。")


@bot.command(
    name="photo_archive_stop"
)
@commands.is_owner()
async def photo_archive_stop_command(
    ctx: commands.Context,
) -> None:

    request_photo_archive_stop()

    loop_was_running = photo_archive_loop.is_running()

    if loop_was_running:
        photo_archive_loop.cancel()

    if is_photo_archive_running():

        await ctx.send(
            "🛑 写真アーカイブへ停止を要求しました。\n"
            "現在の記事処理が区切りに到達すると停止します。"
        )

    elif loop_was_running:

        await ctx.send(
            "🛑 写真アーカイブの定期巡回を停止しました。"
        )

    else:

        await ctx.send(
            "⚠️ 写真アーカイブは停止中です。"
        )

    print("写真アーカイブの停止を要求しました。")


@bot.command(
    name="photo_archive_status"
)
@commands.is_owner()
async def photo_archive_status_command(
    ctx: commands.Context,
) -> None:

    counts = await asyncio.to_thread(
        get_photo_db_counts
    )

    await ctx.send(
        "📊 **写真アーカイブ状況**\n"
        "現在の処理: "
        f"**{'実行中' if is_photo_archive_running() else '停止中'}**\n"
        "定期巡回: "
        f"**{'動作中' if photo_archive_loop.is_running() else '停止中'}**\n"
        f"巡回間隔: **{PHOTO_ARCHIVE_INTERVAL}秒**\n"
        f"登録ブログ: **{counts.get('blogs', 0)}件**\n"
        f"登録画像: **{counts.get('images', 0)}件**\n"
        f"AIタグ: **{counts.get('ai_tags', 0)}件**\n"
        f"確認待ち: **{counts.get('pending_reviews', 0)}件**"
    )


# =========================
# 写真DB・AIコマンド
# =========================

@bot.command(
    name="photo_count"
)
@commands.is_owner()
async def photo_count_command(
    ctx: commands.Context,
) -> None:

    counts = await asyncio.to_thread(
        get_photo_db_counts
    )

    message = (
        "📷 **写真検索DB件数**\n"
        f"ブログ: **{counts.get('blogs', 0)}件**\n"
        f"画像: **{counts.get('images', 0)}件**\n"
        f"AIタグ: **{counts.get('ai_tags', 0)}件**\n"
        f"手動タグ: **{counts.get('manual_tags', 0)}件**\n"
        f"人物: **{counts.get('people', 0)}件**\n"
        f"顔: **{counts.get('faces', 0)}件**\n"
        f"確認待ち: **{counts.get('pending_reviews', 0)}件**\n"
        f"顔確認待ち: **{counts.get('pending_face_reviews', 0)}件**"
    )

    await ctx.send(
        message
    )


@bot.command(
    name="photo_storage"
)
@commands.is_owner()
async def photo_storage_command(
    ctx: commands.Context,
) -> None:

    stats = await asyncio.to_thread(
        get_photo_storage_stats
    )

    storage_path = get_photo_storage_path()

    await ctx.send(
        "💾 **写真保存状況**\n"
        f"保存先: `{storage_path}`\n"
        f"画像総数: **{stats.get('total_images', 0)}件**\n"
        f"保存完了: **{stats.get('completed', 0)}件**\n"
        f"保存待ち: **{stats.get('pending', 0)}件**\n"
        f"保存失敗: **{stats.get('failed', 0)}件**\n"
        "合計容量: "
        f"**{format_bytes(stats.get('total_size', 0))}**"
    )


@bot.command(
    name="ai_status"
)
@commands.is_owner()
async def ai_status_command(
    ctx: commands.Context,
) -> None:

    status = get_photo_ai_status()

    await ctx.send(
        "🤖 **写真AI解析設定**\n"
        "APIキー: "
        f"**{'設定済み' if status.get('enabled') else '未設定'}**\n"
        f"モデル: **{status.get('model', '不明')}**\n"
        f"画像詳細度: **{status.get('detail', '不明')}**\n"
        f"一括件数: **{status.get('batch_limit', 0)}件**\n"
        "自動解析: "
        f"**{'有効' if PHOTO_AI_AUTO_ANALYZE else '無効'}**\n"
        f"自動解析上限: **{PHOTO_AI_AUTO_LIMIT}件**"
    )


@bot.command(
    name="ai_analyze"
)
@commands.is_owner()
async def ai_analyze_command(
    ctx: commands.Context,
    limit: int | None = None,
) -> None:

    status = get_photo_ai_status()

    if not status.get(
        "enabled"
    ):

        await ctx.send(
            "⚠️ OPENAI_API_KEYが設定されていません。"
        )

        return

    if limit is not None:

        limit = max(
            min(
                int(limit),
                100,
            ),
            1,
        )

    await ctx.send(
        "🤖 未解析画像のAI解析を開始します。"
    )

    try:

        result = await run_ai_analysis_batch(
            limit
        )

    except Exception as error:

        print(
            "AI一括解析エラー:",
            error,
        )

        await ctx.send(
            "⚠️ AI解析に失敗しました。\n"
            f"`{error}`"
        )

        return

    await ctx.send(
        "✅ **AI解析完了**\n"
        f"検出: **{result.get('found', 0)}件**\n"
        f"完了: **{result.get('completed', 0)}件**\n"
        f"確認待ち: **{result.get('review', 0)}件**\n"
        f"失敗: **{result.get('failed', 0)}件**"
    )


# =========================
# アーカイブ本体
# =========================

async def run_archive_cycle() -> None:
    """未アーカイブ記事を取得して順番に処理する。"""

    if archive_cycle_lock.locked():
        log_event("WARNING", "前回処理が継続中のため今回の巡回をスキップします。")
        return

    async with archive_cycle_lock:
        started_at = datetime.now().astimezone()
        started_clock = time.monotonic()
        archive_runtime_status.update({
            "running": True,
            "last_started": started_at,
            "last_finished": None,
            "last_result": "実行中",
            "last_error": None,
            "last_targets": 0,
            "last_completed": 0,
            "last_failed": 0,
        })
        log_event("INFO", "アーカイブ巡回開始")

        try:
            blogs = await run_with_retry(
                "ブログ一覧取得",
                get_archive_targets,
            )
            archive_runtime_status["last_targets"] = len(blogs)
            log_event("INFO", f"未処理記事数: {len(blogs)}件")

            if not blogs:
                archive_runtime_status["last_result"] = "正常（対象なし）"
                return

            timeout = aiohttp.ClientTimeout(total=120)
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            completed = 0
            failed = 0

            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                for index, blog in enumerate(blogs, start=1):
                    succeeded = await process_archive_blog(
                        session, blog, index, len(blogs)
                    )
                    if succeeded:
                        completed += 1
                    else:
                        failed += 1
                    archive_runtime_status["last_completed"] = completed
                    archive_runtime_status["last_failed"] = failed
                    await asyncio.sleep(SEND_DELAY)

            archive_runtime_status["last_result"] = (
                f"正常（成功{completed}件・失敗{failed}件）"
                if failed == 0
                else f"一部失敗（成功{completed}件・失敗{failed}件）"
            )
            log_event(
                "SUCCESS" if failed == 0 else "WARNING",
                f"アーカイブ巡回完了: 成功{completed}件 / 失敗{failed}件",
            )

        except Exception as error:
            archive_runtime_status["last_result"] = "エラー"
            archive_runtime_status["last_error"] = str(error)
            log_event("ERROR", f"アーカイブ巡回エラー: {error}")

        finally:
            archive_runtime_status["running"] = False
            archive_runtime_status["last_finished"] = datetime.now().astimezone()
            archive_runtime_status["last_duration"] = time.monotonic() - started_clock
            log_event(
                "INFO",
                f"アーカイブ処理時間: {archive_runtime_status['last_duration']:.2f}秒",
            )


async def process_archive_blog(
    session: aiohttp.ClientSession,
    blog: dict[str, Any],
    index: int,
    total: int,
) -> bool:
    """
    ブログ1件を処理する。
    """

    blog_url = str(
        blog.get(
            "url",
            "",
        )
    ).strip()

    print(
        "-" * 50
    )

    print(
        f"処理中 {index}/{total}"
    )

    print(
        "グループ:",
        blog.get(
            "group",
            "不明",
        ),
    )

    print(
        "メンバー:",
        blog.get(
            "member",
            "不明",
        ),
    )

    print(
        "日時:",
        blog.get(
            "date",
            "不明",
        ),
    )

    print(
        "URL:",
        blog_url,
    )

    if not blog_url:

        print(
            "URLが空のためスキップします。"
        )

        return False

    try:

        channels = get_channels(
            blog
        )

        if not channels:

            print(
                "送信先チャンネルがありません。"
            )

            return False

        try:
            image_urls = await run_with_retry(
                "記事画像URL取得",
                lambda: get_images(blog_url),
            )
        except Exception as error:
            log_event("ERROR", f"記事画像URL取得エラー: {blog_url} / {error}")
            image_urls = []

        image_urls = list(
            dict.fromkeys(
                str(url).strip()
                for url in (
                    image_urls
                    or []
                )
                if str(url).strip()
            )
        )

        print(
            "取得画像数:",
            len(
                image_urls
            ),
        )

        # 写真DB保存とAI解析はDiscord送信とは独立して実行する。
        # 失敗しても通常アーカイブ通知は続行する。
        await archive_photos_and_analyze(
            session,
            blog,
            image_urls,
        )

        embed = build_archive_embed(
            blog,
            len(
                image_urls
            ),
        )

        send_results: list[
            bool
        ] = []

        for channel in channels:

            succeeded = await send_blog_to_channel(
                session,
                channel,
                embed,
                image_urls,
                blog_url,
            )

            send_results.append(
                succeeded
            )

        if (
            send_results
            and all(
                send_results
            )
        ):

            await asyncio.to_thread(
                save_archive,
                blog,
            )

            log_event("SUCCESS", f"アーカイブ保存完了: {blog_url}")
            return True

        else:

            log_event(
                "WARNING",
                "一部の送信に失敗したため通知済みDBへ保存しませんでした。",
            )
            return False

    except Exception as error:

        log_event("ERROR", f"アーカイブ処理エラー: {blog_url} / {error}")
        return False


@tasks.loop(
    seconds=ARCHIVE_INTERVAL
)
async def archive_loop() -> None:

    if not blog_archive_allowed():
        return

    await run_archive_cycle()


@archive_loop.before_loop
async def before_archive_loop() -> None:

    await bot.wait_until_ready()


@archive_loop.error
async def archive_loop_error(
    error: BaseException,
) -> None:

    print(
        "アーカイブ巡回タスクエラー:",
        error,
    )


@tasks.loop(
    seconds=PHOTO_ARCHIVE_INTERVAL
)
async def photo_archive_loop() -> None:
    """写真アーカイブを24時間定期実行する。"""

    await run_photo_archive_once()


@photo_archive_loop.before_loop
async def before_photo_archive_loop() -> None:

    await bot.wait_until_ready()


@photo_archive_loop.error
async def photo_archive_loop_error(
    error: BaseException,
) -> None:

    print(
        "写真アーカイブ巡回タスクエラー:",
        error,
    )


@tasks.loop(
    seconds=PHOTO_AI_AUTO_INTERVAL
)
async def ai_analysis_loop() -> None:
    """24時間、pending画像がある時だけAI解析を実行する。"""

    status = get_photo_ai_status()

    if not status.get("enabled"):
        return

    result = await run_ai_analysis_batch(
        PHOTO_AI_AUTO_LIMIT
    )

    if result.get("found", 0) > 0:
        print(
            "AI自動解析完了:",
            f"検出={result.get('found', 0)}",
            f"完了={result.get('completed', 0)}",
            f"確認待ち={result.get('review', 0)}",
            f"失敗={result.get('failed', 0)}",
        )


@ai_analysis_loop.before_loop
async def before_ai_analysis_loop() -> None:

    await bot.wait_until_ready()


@ai_analysis_loop.error
async def ai_analysis_loop_error(
    error: BaseException,
) -> None:

    print(
        "AI解析ワーカーエラー:",
        error,
    )


# =========================
# コマンドエラー
# =========================

@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:

    if isinstance(
        error,
        commands.CommandNotFound,
    ):

        return

    if isinstance(
        error,
        commands.NotOwner,
    ):

        await ctx.send(
            "⚠️ このコマンドはBot所有者専用です。"
        )

        return

    if isinstance(
        error,
        commands.BadArgument,
    ):

        await ctx.send(
            "⚠️ コマンドの引数が正しくありません。"
        )

        return

    print(
        "コマンド実行エラー:",
        error,
    )

    await ctx.send(
        "⚠️ コマンド実行中にエラーが発生しました。\n"
        f"`{error}`"
    )


# =========================
# BOT起動
# =========================

if not TOKEN:

    raise RuntimeError(
        "環境変数 DISCORD_TOKEN が設定されていません。"
    )


bot.run(
    TOKEN
)

