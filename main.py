import asyncio
import os
import re

import discord
from discord.ext import commands

from blog_checker import get_latest_blog
from blog_monitor import build_notification_text, check_blog
from database import init_db
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
blog_task = None


@bot.event
async def on_ready():
    global blog_task

    init_db()
    print(f"{bot.user} が起動しました！")

    if blog_task is None or blog_task.done():
        blog_task = asyncio.create_task(check_blog(bot))
        print("ブログ監視開始")


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
async def latest(ctx):
    try:
        blogs = await asyncio.to_thread(get_latest_blog)

        if not blogs:
            await ctx.send("ブログ取得失敗")
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
        print("最新ブログ取得エラー:", error)
        await ctx.send(f"ブログ取得エラー: {error}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    urls = url_pattern.findall(message.content)

    for raw_url in urls:
        url = raw_url.rstrip(".,!?、。！？)]}〉》」』")

        try:
            blog = await asyncio.to_thread(get_images, url)

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

            # URLがパーサー側に無い場合も、元のURLを必ず使う。
            blog["url"] = str(
                blog.get("url")
                or url
            ).strip()

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
            print("画像処理エラー:", error)
            await message.channel.send(
                f"エラー: {error}",
                suppress_embeds=True,
            )

    await bot.process_commands(message)


bot.run(TOKEN)
