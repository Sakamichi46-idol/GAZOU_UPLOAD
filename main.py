import asyncio
import os
import re

import discord
from discord.ext import commands

from blog_checker import get_latest_blog
from blog_monitor import (
    build_notification_text,
    check_blog,
    get_monitor_status,
    run_with_retry,
)
from database import get_blog_count, init_db
from image_getter import get_images
from media_converter import send_blog_media


TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise RuntimeError("環境変数 TOKEN が設定されていません。")


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

url_pattern = re.compile(r"https?://[^\s<>]+")
blog_task: asyncio.Task | None = None


@bot.event
async def on_ready():
    global blog_task

    init_db()
    print(f"[SUCCESS] {bot.user} が起動しました")

    if blog_task is None or blog_task.done():
        blog_task = asyncio.create_task(check_blog(bot))
        print("[SUCCESS] ブログ監視を開始しました")
    else:
        print("[INFO] ブログ監視タスクはすでに動作中です")


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
async def status(ctx):
    """Bot、監視タスク、DBの状態を表示する。"""

    status_data = get_monitor_status()
    task_running = (
        blog_task is not None
        and not blog_task.done()
    )

    running_text = "稼働中" if task_running else "停止中"
    monitor_text = "稼働中" if status_data.get("running") else "停止中"
    last_error = str(status_data.get("last_error") or "なし")

    text = (
        "🤖 Botステータス\n"
        f"接続状態: {running_text}\n"
        f"ブログ監視: {monitor_text}\n"
        f"DB登録件数: {get_blog_count()}件\n"
        f"前回取得件数: {status_data.get('last_blog_count', 0)}件\n"
        f"前回新着件数: {status_data.get('last_new_blog_count', 0)}件\n"
        f"前回確認開始: {status_data.get('last_check_started_at_text')}\n"
        f"前回確認完了: {status_data.get('last_check_completed_at_text')}\n"
        f"前回結果: {status_data.get('last_result', '未実行')}\n"
        f"前回エラー: {last_error}"
    )

    await ctx.send(text, suppress_embeds=True)


@bot.command()
async def latest(ctx):
    try:
        blogs = await run_with_retry(
            get_latest_blog,
            operation_name="!latest ブログ一覧取得",
        )

        if not blogs:
            await ctx.send("ブログを取得できませんでした。")
            return

        for blog in blogs:
            await ctx.send(
                (
                    f"🏷️ {blog.get('group', '')}\n"
                    f"👤 {blog.get('member', '')}\n"
                    f"📝 {blog.get('title', '')}\n"
                    f"📅 {blog.get('date', '')}\n"
                    f"🔗 {blog.get('url', '')}"
                ),
                suppress_embeds=True,
            )

    except Exception as error:
        print(f"[ERROR] !latest取得エラー: {error!r}")
        await ctx.send("ブログ取得中にエラーが発生しました。")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    urls = url_pattern.findall(message.content)

    for raw_url in urls:
        url = raw_url.rstrip(".,!?、。！？)]}〉》」』")

        try:
            blog = await run_with_retry(
                get_images,
                url,
                operation_name=f"URL画像取得 {url}",
            )

            if not isinstance(blog, dict):
                await message.channel.send(
                    "ブログ情報を取得できませんでした。",
                    suppress_embeds=True,
                )
                continue

            images = blog.get("images", [])

            if not images:
                await message.channel.send(
                    "画像が見つかりませんでした。",
                    suppress_embeds=True,
                )
                continue

            blog["url"] = str(blog.get("url") or url).strip()

            text = build_notification_text(
                blog,
                len(images),
            )

            await send_blog_media(
                channel=message.channel,
                text=text,
                embed=None,
                image_urls=images,
                send_delay=1.0,
                article_url=blog.get("url", url),
                group=blog.get("group", ""),
            )

        except Exception as error:
            print(f"[ERROR] URL画像処理エラー: url={url} error={error!r}")
            await message.channel.send(
                "画像処理中にエラーが発生しました。",
                suppress_embeds=True,
            )

    await bot.process_commands(message)


bot.run(TOKEN)
