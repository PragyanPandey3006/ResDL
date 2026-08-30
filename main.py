# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
import psutil
import asyncio
from time import time

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import (
    processMediaGroup,
    progressArgs,
    send_media
)

from helpers.forward import check_forward_permission, resolve_forward_chat_id

from helpers.files import (
    get_download_path,
    fileSizeLimit,
    get_readable_file_size,
    get_readable_time,
    cleanup_download,
    cleanup_downloads_root
)

from helpers.msg import (
    getChatMsgID,
    getStoryChatMsgID,
    is_story_link,
    get_file_name,
    get_story_file_name,
    get_raw_text
)

from config import PyroConf
from logger import LOGGER

# Initialize the bot client
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=100,
    parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=1, # ✅ SAFE DEFAULT
    sleep_threshold=30,
)

# Client for user session
user = Client(
    "user_session",
    workers=100,
    session_string=PyroConf.SESSION_STRING,
    max_concurrent_transmissions=1, # ✅ SAFE DEFAULT
    sleep_threshold=30,
)

RUNNING_TASKS = set()
download_semaphore = None
forward_chat_id = None

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task


@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can grab photos, videos, audio, and documents from any Telegram post,\n"
        "and now also **download restricted stories** (photo or video).\n"
        "Just send me a link (paste it directly or use `/dl <link>` for posts /\n"
        "`/dls <link>` for stories).\n\n"
        "ℹ️ Use `/help` to view all commands and examples.\n"
        "🔒 Make sure the user client is part of the chat / follows the user.\n\n"
        "Ready? Send me a Telegram post or story link!"
    )

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)


@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Download Media**\n"
        "   – Send `/dl <post_URL>` **or** just paste a Telegram post link to fetch photos, videos, audio, or documents.\n\n"
        "➤ **Batch Download**\n"
        "   – Send `/bdl start_link end_link` to grab a series of posts in one go.\n"
        "     💡 Example: `/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`\n"
        "**It will download all posts from ID 100 to 120.**\n\n"
        "➤ **Download Story**\n"
        "   – Send `/dls <story_URL>` **or** just paste a Telegram story link to fetch a restricted story (photo or video).\n"
        "     💡 Example: `/dls https://t.me/username/s/12`\n\n"
        "➤ **Batch Story Download**\n"
        "   – Send `/bdls start_link end_link` to grab a range of stories from the same user/channel.\n"
        "     💡 Example: `/bdls https://t.me/username/s/10 https://t.me/username/s/25`\n\n"
        "➤ **Requirements**\n"
        "   – Make sure the user client is part of the chat (or follows the user for stories).\n\n"
        "➤ **If the bot hangs**\n"
        "   – Send `/killall` to cancel any pending downloads.\n\n"
        "➤ **Logs**\n"
        "   – Send `/logs` to download the bot’s logs file.\n\n"
        "➤ **Cleanup**\n"
        "   – Send `/cleanup` to remove temporary downloaded files from disk.\n\n"
        "➤ **Stats**\n"
        "   – Send `/stats` to view current status"
    )
    
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)


@bot.on_message(filters.command("cleanup") & filters.private)
async def cleanup_storage(_, message: Message):
    try:
        files_removed, bytes_freed = cleanup_downloads_root()
        if files_removed == 0:
            return await message.reply("🧹 **Cleanup complete:** no local downloads found.")
        return await message.reply(
            f"🧹 **Cleanup complete:** removed `{files_removed}` file(s), "
            f"freed `{get_readable_file_size(bytes_freed)}`."
        )
    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed: {e}")
        return await message.reply("❌ **Cleanup failed.** Check logs for details.")


async def handle_download(bot: Client, message: Message, post_url: str):
    global forward_chat_id
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

        try:
            effective_forward_chat_id = None
            if forward_chat_id:
                ok, err_msg = await check_forward_permission(bot, forward_chat_id)
                if not ok:
                    await message.reply(
                        f"⚠️ **Forward chat misconfigured:** {err_msg}\n\n"
                        "The file will be sent to you only."
                    )
                else:
                    effective_forward_chat_id = forward_chat_id

            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)

            LOGGER(__name__).info(f"Downloading media from URL: {post_url}")

            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size
                    if chat_message.document
                    else chat_message.video.file_size
                    if chat_message.video
                    else chat_message.audio.file_size
                )

                if not await fileSizeLimit(
                    file_size, message, "download", user.me.is_premium
                ):
                    return

            raw_caption, raw_caption_entities = get_raw_text(
                chat_message.caption, chat_message.caption_entities
            )
            raw_text, raw_text_entities = get_raw_text(
                chat_message.text, chat_message.entities
            )

            if chat_message.media_group_id:
                if not await processMediaGroup(chat_message, bot, message, forward_chat_id=effective_forward_chat_id):
                    await message.reply(
                        "**Could not extract any valid media from the media group.**"
                    )
                return

            has_downloadable_media = (
                chat_message.photo
                or chat_message.video
                or chat_message.audio
                or chat_message.document
                or chat_message.voice
                or chat_message.video_note
                or chat_message.animation
                or chat_message.sticker
            )

            if has_downloadable_media:
                start_time = time()
                progress_message = await message.reply("**📥 Downloading Progress...**")

                filename = get_file_name(message_id, chat_message)
                download_path = get_download_path(message.id, filename)

                media_path = None
                for attempt in range(2):
                    try:
                        media_path = await chat_message.download(
                            file_name=download_path,
                            progress=Leaves.progress_for_pyrogram,
                            progress_args=progressArgs(
                                "📥 Downloading Progress", progress_message, start_time
                            ),
                        )
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while downloading media: {wait_s}s")
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise

                if not media_path or not os.path.exists(media_path):
                    await progress_message.edit("**❌ Download failed: File not saved properly**")
                    return

                file_size = os.path.getsize(media_path)
                if file_size == 0:
                    await progress_message.edit("**❌ Download failed: File is empty**")
                    cleanup_download(media_path)
                    return

                LOGGER(__name__).info(f"Downloaded media: {media_path} (Size: {file_size} bytes)")

                media_type = (
                    "photo"
                    if chat_message.photo
                    else "video"
                    if chat_message.video
                    else "audio"
                    if chat_message.audio
                    else "document"
                )
                await send_media(
                    bot,
                    message,
                    media_path,
                    media_type,
                    raw_caption,
                    raw_caption_entities,
                    progress_message,
                    start_time,
                    forward_chat_id=effective_forward_chat_id,
                )

                cleanup_download(media_path)
                await progress_message.delete()

            elif chat_message.poll:
                await message.reply("**This post contains a poll which cannot be downloaded.**")

            elif chat_message.text or chat_message.caption:
                txt = raw_text or raw_caption
                ents = raw_text_entities if raw_text else raw_caption_entities
                sent_text = None
                try:
                    sent_text = await message.reply(txt, entities=ents or None)
                except BadRequest as e:
                    if "ENTITY_TEXT_INVALID" in str(e):
                        LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in text reply, retrying without entities: {e}")
                        sent_text = await message.reply(txt)
                    else:
                        raise
                if effective_forward_chat_id and sent_text:
                    try:
                        await bot.copy_message(
                            chat_id=effective_forward_chat_id,
                            from_chat_id=sent_text.chat.id,
                            message_id=sent_text.id,
                        )
                        LOGGER(__name__).info(f"Copied text message to chat: {effective_forward_chat_id}")
                    except Exception as e:
                        LOGGER(__name__).error(f"Failed to copy text message to {effective_forward_chat_id}: {e}")
            else:
                await message.reply("**No media or text found in the post URL.**")

        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait in handle_download: {wait_s}s")
            if wait_s > 0:
                await asyncio.sleep(wait_s + 1)
            return
        except PeerIdInvalid as e:
            LOGGER(__name__).error(f"PeerIdInvalid for {post_url}: {e}")
            await message.reply(
                "**❌ Access Denied**\n\n"
                "The user client cannot access this chat.\n"
                "Make sure the user account has joined the channel/group.\n\n"
                f"**Details:** `{e}`"
            )
        except BadRequest as e:
            LOGGER(__name__).error(f"BadRequest for {post_url}: {e}")
            await message.reply(
                "**❌ Bad Request**\n\n"
                f"Telegram returned an error: `{e}`\n\n"
                "This may happen if the message ID is invalid or the chat is inaccessible."
            )
        except KeyError as e:
            LOGGER(__name__).error(f"KeyError for {post_url}: {e}")
            await message.reply(f"**❌ Invalid URL format:** `{e}`")
        except Exception as e:
            LOGGER(__name__).error(f"Unexpected error for {post_url}: {e}")
            await message.reply("**❌ An unexpected error occurred.** Check /logs for details.")


async def handle_story_download(bot: Client, message: Message, story_url: str):
    global forward_chat_id
    async with download_semaphore:
        if "?" in story_url:
            story_url = story_url.split("?", 1)[0]

        try:
            effective_forward_chat_id = None
            if forward_chat_id:
                ok, err_msg = await check_forward_permission(bot, forward_chat_id)
                if not ok:
                    await message.reply(
                        f"⚠️ **Forward chat misconfigured:** {err_msg}\n\n"
                        "The file will be sent to you only."
                    )
                else:
                    effective_forward_chat_id = forward_chat_id

            chat_username, story_id = getStoryChatMsgID(story_url)

            story = None
            for attempt in range(2):
                try:
                    story = await user.get_stories(
                        chat_id=chat_username, story_ids=story_id
                    )
                    break
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 0)
                    LOGGER(__name__).warning(
                        f"FloodWait while fetching story: {wait_s}s"
                    )
                    if wait_s > 0 and attempt == 0:
                        await asyncio.sleep(wait_s + 1)
                        continue
                    raise

            if not story:
                await message.reply(
                    "**❌ Story not found.**\n\n"
                    "It may have expired (stories are only visible for 24h unless pinned), "
                    "or the user session does not have access to view it."
                )
                return

            LOGGER(__name__).info(f"Downloading story from URL: {story_url}")

            if story.video:
                if not await fileSizeLimit(
                    story.video.file_size, message, "download", user.me.is_premium
                ):
                    return

            if not (story.photo or story.video):
                await message.reply(
                    "**This story has no downloadable media.**"
                )
                return

            raw_caption, raw_caption_entities = get_raw_text(
                story.caption, story.caption_entities
            )

            start_time = time()
            progress_message = await message.reply("**📥 Downloading Story...**")

            filename = get_story_file_name(story_id, story, chat_username)
            download_path = get_download_path(message.id, filename)

            media_path = None
            for attempt in range(2):
                try:
                    media_path = await story.download(
                        file_name=download_path,
                        progress=Leaves.progress_for_pyrogram,
                        progress_args=progressArgs(
                            "📥 Downloading Progress", progress_message, start_time
                        ),
                    )
                    break
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 0)
                    LOGGER(__name__).warning(
                        f"FloodWait while downloading story: {wait_s}s"
                    )
                    if wait_s > 0 and attempt == 0:
                        await asyncio.sleep(wait_s + 1)
                        continue
                    raise

            if not media_path or not os.path.exists(media_path):
                await progress_message.edit(
                    "**❌ Download failed: File not saved properly**"
                )
                return

            file_size = os.path.getsize(media_path)
            if file_size == 0:
                await progress_message.edit("**❌ Download failed: File is empty**")
                cleanup_download(media_path)
                return

            LOGGER(__name__).info(
                f"Downloaded story: {media_path} (Size: {file_size} bytes)"
            )

            media_type = "video" if story.video else "photo"
            await send_media(
                bot,
                message,
                media_path,
                media_type,
                raw_caption,
                raw_caption_entities,
                progress_message,
                start_time,
                forward_chat_id=effective_forward_chat_id,
            )

            cleanup_download(media_path)
            await progress_message.delete()

        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait in handle_story_download: {wait_s}s")
            if wait_s > 0:
                await asyncio.sleep(wait_s + 1)
            return
        except PeerIdInvalid as e:
            LOGGER(__name__).error(f"PeerIdInvalid for story {story_url}: {e}")
            await message.reply(
                "**❌ Access Denied**\n\n"
                "The user client cannot resolve this user/channel.\n"
                "Make sure the user account follows or has access to it.\n\n"
                f"**Details:** `{e}`"
            )
        except BadRequest as e:
            LOGGER(__name__).error(f"BadRequest for story {story_url}: {e}")
            await message.reply(
                "**❌ Bad Request**\n\n"
                f"Telegram returned an error: `{e}`\n\n"
                "The story may have expired, been deleted, or the ID is invalid."
            )
        except ValueError as e:
            await message.reply(f"**❌ Invalid story URL:** `{e}`")
        except Exception as e:
            LOGGER(__name__).error(f"Unexpected error for story {story_url}: {e}")
            await message.reply("**❌ An unexpected error occurred.** Check /logs for details.")


@bot.on_message(filters.command("dl") & filters.private)
async def download_media(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after the /dl command.**")
        return

    post_url = message.command[1]
    await track_task(handle_download(bot, message, post_url))


@bot.on_message(filters.command("dls") & filters.private)
async def download_story(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply(
            "**Provide a story URL after the /dls command.**\n"
            "💡 Example: `/dls https://t.me/username/s/12`"
        )
        return

    story_url = message.command[1]
    if not is_story_link(story_url):
        await message.reply(
            "**❌ Not a valid story URL.**\n"
            "Expected format: `https://t.me/<username>/s/<story_id>`"
        )
        return

    await track_task(handle_story_download(bot, message, story_url))


@bot.on_message(filters.command("bdls") & filters.private)
async def download_story_range(bot: Client, message: Message):
    args = message.text.split()

    if len(args) != 3 or not all(is_story_link(arg) for arg in args[1:]):
        await message.reply(
            "🚀 **Batch Story Download**\n"
            "`/bdls start_link end_link`\n\n"
            "💡 **Example:**\n"
            "`/bdls https://t.me/username/s/10 https://t.me/username/s/25`"
        )
        return

    try:
        start_chat, start_id = getStoryChatMsgID(args[1])
        end_chat,   end_id   = getStoryChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"**❌ Error parsing links:\n{e}**")

    if start_chat.lower() != end_chat.lower():
        return await message.reply(
            "**❌ Both links must be from the same user/channel.**"
        )
    if start_id > end_id:
        return await message.reply(
            "**❌ Invalid range: start ID cannot exceed end ID.**"
        )

    prefix = f"https://t.me/{start_chat}/s"
    loading = await message.reply(
        f"📥 **Downloading stories {start_id}–{end_id}…**"
    )

    downloaded = failed = 0
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE

    for sid in range(start_id, end_id + 1):
        url = f"{prefix}/{sid}"
        task = track_task(handle_story_download(bot, message, url))
        batch_tasks.append(task)

        if len(batch_tasks) >= BATCH_SIZE:
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    await loading.delete()
                    return await message.reply(
                        f"**❌ Batch canceled** after downloading `{downloaded}` stories."
                    )
                elif isinstance(result, Exception):
                    failed += 1
                    LOGGER(__name__).error(f"Error: {result}")
                else:
                    downloaded += 1

            batch_tasks.clear()
            await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1

    await loading.delete()
    await message.reply(
        "**✅ Batch Story Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded** : `{downloaded}` story(s)\n"
        f"❌ **Failed**     : `{failed}` error(s)"
    )


@bot.on_message(filters.command("bdl") & filters.private)
async def download_range(bot: Client, message: Message):
    args = message.text.split()

    if len(args) != 3 or not all(arg.startswith("https://t.me/") for arg in args[1:]):
        await message.reply(
            "🚀 **Batch Download Process**\n"
            "`/bdl start_link end_link`\n\n"
            "💡 **Example:**\n"
            "`/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`"
        )
        return

    try:
        start_chat, start_id = getChatMsgID(args[1])
        end_chat,   end_id   = getChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"**❌ Error parsing links:\n{e}**")

    if start_chat != end_chat:
        return await message.reply("**❌ Both links must be from the same channel.**")
    if start_id > end_id:
        return await message.reply("**❌ Invalid range: start ID cannot exceed end ID.**")

    try:
        await user.get_chat(start_chat)
    except Exception:
        pass

    prefix = args[1].rsplit("/", 1)[0]
    loading = await message.reply(f"📥 **Downloading posts {start_id}–{end_id}…**")

    downloaded = skipped = failed = 0
    processed_media_groups = set()
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE

    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            chat_msg = await user.get_messages(chat_id=start_chat, message_ids=msg_id)
            if not chat_msg:
                skipped += 1
                continue

            if chat_msg.media_group_id:
                if chat_msg.media_group_id in processed_media_groups:
                    skipped += 1
                    continue
                processed_media_groups.add(chat_msg.media_group_id)

            has_media = bool(chat_msg.media_group_id or chat_msg.media)
            has_text  = bool(chat_msg.text or chat_msg.caption)
            if not (has_media or has_text):
                skipped += 1
                continue

            task = track_task(handle_download(bot, message, url))
            batch_tasks.append(task)

            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        await loading.delete()
                        return await message.reply(
                            f"**❌ Batch canceled** after downloading `{downloaded}` posts."
                        )
                    elif isinstance(result, Exception):
                        failed += 1
                        LOGGER(__name__).error(f"Error: {result}")
                    else:
                        downloaded += 1

                batch_tasks.clear()
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error at {url}: {e}")

    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1

    await loading.delete()
    await message.reply(
        "**✅ Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded** : `{downloaded}` post(s)\n"
        f"⏭️ **Skipped**    : `{skipped}` (no content)\n"
        f"❌ **Failed**     : `{failed}` error(s)"
    )


@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "bdl", "dls", "bdls", "stats", "logs", "killall", "cleanup"]))
async def handle_any_message(bot: Client, message: Message):
    if message.text and not message.text.startswith("/"):
        text = message.text.strip()
        if is_story_link(text):
            await track_task(handle_story_download(bot, message, text))
        else:
            await track_task(handle_download(bot, message, text))


@bot.on_message(filters.command("stats") & filters.private)
async def stats(_, message: Message):
    currentTime = get_readable_time(time() - PyroConf.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    total = get_readable_file_size(total)
    used = get_readable_file_size(used)
    free = get_readable_file_size(free)
    sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
    recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
    cpuUsage = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    process = psutil.Process(os.getpid())

    stats = (
        "**≧◉◡◉≦ Bot is Up and Running successfully.**\n\n"
        f"**➜ Bot Uptime:** `{currentTime}`\n"
        f"**➜ Total Disk Space:** `{total}`\n"
        f"**➜ Used:** `{used}`\n"
        f"**➜ Free:** `{free}`\n"
        f"**➜ Memory Usage:** `{round(process.memory_info()[0] / 1024**2)} MiB`\n\n"
        f"**➜ Upload:** `{sent}`\n"
        f"**➜ Download:** `{recv}`\n\n"
        f"**➜ CPU:** `{cpuUsage}%` | "
        f"**➜ RAM:** `{memory}%` | "
        f"**➜ DISK:** `{disk}%`"
    )
    await message.reply(stats)


@bot.on_message(filters.command("logs") & filters.private)
async def logs(_, message: Message):
    if os.path.exists("logs.txt"):
        await message.reply_document(document="logs.txt", caption="**Logs**")
    else:
        await message.reply("**Not exists**")


@bot.on_message(filters.command("killall") & filters.private)
async def cancel_all_tasks(_, message: Message):
    cancelled = 0
    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    await message.reply(f"**Cancelled {cancelled} running task(s).**")


async def initialize():
    global download_semaphore, forward_chat_id
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)

    if PyroConf.FORWARD_CHAT_ID:
        forward_chat_id = await resolve_forward_chat_id(PyroConf.FORWARD_CHAT_ID)
        LOGGER(__name__).info(f"Auto-forward enabled. Target chat: {forward_chat_id}")

if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Bot Started!")
        asyncio.get_event_loop().run_until_complete(initialize())
        user.start()
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")
