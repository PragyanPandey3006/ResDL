# Copyright (C) @NotYourDeveloper
# Copyright (C) @NotYourDeveloper
# Channel: https://t.me/notyourdeveloper

import os
import shutil
import psutil
import asyncio
from time import time

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters, idle
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait, UserNotParticipant
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

from helpers.caption_manager import CaptionManager
from helpers.login_manager import LoginManager
from helpers.channel_manager import ChannelManager
from helpers import batch_state

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

# Feature managers (login / custom captions) and per-user login flow state
login_manager = LoginManager(PyroConf.API_ID, PyroConf.API_HASH)
caption_manager = CaptionManager()
channel_manager = ChannelManager()
user_states = {}

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task


async def notify(
    message: "Message | None" = None,
    text: str = "",
    *,
    level: str = "error",
    exc: "Exception | None" = None,
    to_user: bool = True,
):
    """Centralized problem reporter.

    Every problem in the bot should be routed through here so it is:
      1. Written to the log file (always).
      2. Sent to the user who triggered the action (when ``message`` is given
         and ``to_user`` is True).
      3. Sent to the central NOTIFY_CHAT_ID admin/log chat (when configured).

    It never raises: notification must not itself crash the bot.
    """
    detail = f"{text}" + (f"\n`{exc}`" if exc else "")

    # 1) Always log.
    log = LOGGER(__name__)
    if level == "warning":
        log.warning(f"{text}{f' | {exc}' if exc else ''}")
    elif level == "info":
        log.info(f"{text}{f' | {exc}' if exc else ''}")
    else:
        log.error(f"{text}{f' | {exc}' if exc else ''}")

    icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(level, "❌")
    body = f"{icon} **Bot notice**\n{detail}"

    # 2) Notify the triggering user.
    if to_user and message is not None:
        try:
            await message.reply(body)
        except Exception as e:
            log.error(f"notify(): failed to reply to user: {e}")

    # 3) Notify the central chat, if configured.
    notify_chat = getattr(PyroConf, "NOTIFY_CHAT_ID", None)
    if notify_chat:
        try:
            await bot.send_message(int(notify_chat), body)
        except Exception as e:
            log.error(f"notify(): failed to send to NOTIFY_CHAT_ID: {e}")


def safe_handler(func):
    """Decorator: make a command/message handler crash-proof.

    Any exception raised inside a handler is caught here so it can NEVER
    propagate up and disturb the event loop or stop the bot. Every failure is
    routed through ``notify`` so it is logged, replied to the triggering user,
    and forwarded to the central NOTIFY_CHAT_ID — satisfying "notify every
    error". FloodWait is handled specially so we never freeze for hours.

    Apply this to every ``@bot.on_message`` handler.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        try:
            return await func(client, message, *args, **kwargs)
        except FloodWait as e:
            # Never block the loop for a long flood wait inside a handler.
            wait_s = int(getattr(e, "value", 0) or 0)
            await notify(
                message,
                f"Action postponed: Telegram asked to wait `{wait_s}s`.",
                level="warning",
                exc=e if wait_s == 0 else None,
            )
        except asyncio.CancelledError:
            # Respect cancellation (e.g. /cancel or shutdown) — re-raise so the
            # task actually stops instead of being swallowed.
            raise
        except Exception as e:
            # Catch-all: report through the central notifier and keep running.
            try:
                await notify(
                    message,
                    f"**Unexpected error in `{getattr(func, '__name__', 'handler')}`.** "
                    "The bot is still running. Check /logs for details.",
                    exc=e,
                )
            except Exception as notify_err:
                # notify() should never raise, but guard anyway so the handler
                # can't take the bot down under any circumstances.
                LOGGER(__name__).error(
                    f"safe_handler(): failed to notify about error in "
                    f"{getattr(func, '__name__', 'handler')}: {notify_err} "
                    f"(original error: {e})"
                )

    return wrapper


async def capped_flood_sleep(exc: "FloodWait", context: str = "") -> bool:
    """Sleep off a FloodWait, but never for longer than MAX_FLOOD_WAIT.

    Returns True if the wait was within the cap and we slept it off (caller may
    retry). Returns False if the wait exceeds the cap — in that case the caller
    should abort the current action instead of freezing for hours.
    """
    wait_s = int(getattr(exc, "value", 0) or 0)
    cap = getattr(PyroConf, "MAX_FLOOD_WAIT", 300)
    if wait_s > cap:
        LOGGER(__name__).warning(
            f"FloodWait {wait_s}s exceeds cap {cap}s ({context}); aborting instead of freezing."
        )
        return False
    if wait_s > 0:
        LOGGER(__name__).warning(f"FloodWait {wait_s}s ({context}); sleeping within cap.")
        await asyncio.sleep(wait_s + 1)
    return True


def get_user_client(message: Message) -> Client:
    """Return the Telegram user client to use for fetching/downloading content.

    If the requesting user has logged in via /login, use their own session so
    they can access chats/channels their account is a member of. This is what
    fixes CHANNEL_INVALID errors: the global SESSION_STRING account may not be
    a member of the target channel, but the logged-in account is.

    Falls back to the global user client (SESSION_STRING) when the user has no
    active personal session.
    """
    if message.from_user:
        user_client = login_manager.get_user_session(message.from_user.id)
        if user_client is not None:
            return user_client
    return user


@bot.on_message(filters.command("start") & filters.private)
@safe_handler
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
        [[InlineKeyboardButton("Update Channel", url="https://t.me/notyourdeveloper")]]
    )
    await message.reply(welcome_text, reply_markup=markup)


@bot.on_message(filters.command("help") & filters.private)
@safe_handler
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Login (recommended)**\n"
        "   – Send `/login +<country_code><number>` to sign in with your own account so you can download from chats/channels **your account** is a member of.\n"
        "     💡 Example: `/login +14155552671`\n"
        "   – You'll be asked for the OTP code (and 2FA password if enabled).\n"
        "   – Send `/logout` to sign out and remove your session.\n\n"
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
        "➤ **Grab Whole Channel (media-type only)**\n"
        "   – `/gc <video|photo|both> <channel> [start_id] [end_id]` – grab only videos, only photos, or **both** from a whole channel, with **captions removed**.\n"
        "     💡 Examples: `/gc video @mychannel` · `/gc photo @mychannel 1 500` · `/gc both @mychannel`\n"
        "   – Output goes to your `/setchannel` target if set, otherwise your DM.\n\n"
        "➤ **Forward Channel**\n"
        "   – `/setchannel <channel_id_or_@username>` – auto-send your downloads to a channel (bot must be admin there).\n"
        "   – `/resetchannel` – stop forwarding and receive files in DM only.\n\n"
        "➤ **Custom Caption**\n"
        "   – `/setcaption <text>` – set a custom caption for your downloads.\n"
        "   – `/resetcaption` – remove your custom caption.\n\n"
        "➤ **Requirements**\n"
        "   – Make sure your logged-in account (or the bot's session) is part of the chat (or follows the user for stories).\n\n"
        "➤ **If the bot hangs**\n"
        "   – Send `/killall` to cancel any pending downloads.\n\n"
        "➤ **Utilities**\n"
        "   – `/stats` – view current status (uptime, disk, memory, CPU, etc.).\n"
        "   – `/logs` – download the bot's logs file.\n"
        "   – `/cleanup` – remove temporary downloaded files from disk."
    )
    
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/notyourdeveloper")]]
    )
    await message.reply(help_text, reply_markup=markup)


@bot.on_message(filters.command("cleanup") & filters.private)
@safe_handler
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


async def handle_download(bot: Client, message: Message, post_url: str,
                          media_filter: str = None, strip_caption: bool = False):
    global forward_chat_id
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

        # Pick the per-user logged-in session when available, else the global
        # SESSION_STRING client. This is the key fix for CHANNEL_INVALID.
        user_client = get_user_client(message)

        try:
            effective_forward_chat_ids = []

            # Build the list of forward destinations:
            #   1. The per-user /setchannel target (if the user set one).
            #   2. The global dump channel (FORWARD_CHAT_ID) — ALWAYS included so
            #      every download from every user is mirrored to the dump.
            # Both are permission-checked; a misconfigured one is skipped (with a
            # warning) instead of blocking the others. Duplicates are removed.
            user_channel_id = None
            if message.from_user:
                user_channel_id = channel_manager.get_channel(message.from_user.id)

            seen_targets = set()
            for candidate in (user_channel_id, forward_chat_id):
                if not candidate or candidate in seen_targets:
                    continue
                seen_targets.add(candidate)
                ok, err_msg = await check_forward_permission(bot, candidate)
                if not ok:
                    await message.reply(
                        f"⚠️ **Forward chat `{candidate}` misconfigured:** {err_msg}\n\n"
                        "That destination will be skipped."
                    )
                    continue
                effective_forward_chat_ids.append(candidate)

            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user_client.get_messages(chat_id=chat_id, message_ids=message_id)

            LOGGER(__name__).info(f"Downloading media from URL: {post_url}")

            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size
                    if chat_message.document
                    else chat_message.video.file_size
                    if chat_message.video
                    else chat_message.audio.file_size
                )

                is_premium = bool(getattr(user_client.me, "is_premium", False)) if user_client.me else False
                if not await fileSizeLimit(
                    file_size, message, "download", is_premium
                ):
                    return

            raw_caption, raw_caption_entities = get_raw_text(
                chat_message.caption, chat_message.caption_entities
            )
            raw_text, raw_text_entities = get_raw_text(
                chat_message.text, chat_message.entities
            )

            if chat_message.media_group_id:
                if not await processMediaGroup(
                    chat_message, bot, message,
                    forward_chat_ids=effective_forward_chat_ids,
                    media_filter=media_filter,
                    strip_caption=strip_caption,
                ):
                    # When filtering by media type, an empty group is expected
                    # (no matching media) — stay quiet in that case.
                    if not media_filter:
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

            # When a media-type filter is active (from /gc), skip anything that
            # doesn't match so only the requested type reaches the destination.
            if media_filter == "photo" and not chat_message.photo:
                return
            if media_filter == "video" and not chat_message.video:
                return
            if media_filter == "both" and not (chat_message.photo or chat_message.video):
                return

            if has_downloadable_media:
                start_time = time()
                progress_message = await message.reply("**📥 Downloading Progress...**")

                filename = get_file_name(message_id, chat_message)
                # Use a folder unique to THIS post (chat + message id) so that
                # concurrent batch downloads never share the same file / .temp
                # path. Previously every post in a /bdl batch used message.id
                # (the command message id), so posts whose server-side filename
                # collided (e.g. "video_...-.mp4") clobbered each other's
                # .temp files mid-download -> "moov atom not found" / "No such
                # file or directory: ...temp".
                unique_folder = f"{message.id}_{chat_id}_{message_id}"
                download_path = get_download_path(unique_folder, filename)

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
                        # Never freeze for hours: only retry if within the cap.
                        if attempt == 0 and await capped_flood_sleep(e, "download media"):
                            continue
                        await notify(
                            message,
                            f"Skipped a download: Telegram FloodWait of `{wait_s}s` "
                            "exceeds the safe limit.",
                            level="warning",
                        )
                        return

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
                    forward_chat_ids=effective_forward_chat_ids,
                    strip_caption=strip_caption,
                )

                cleanup_download(media_path)
                await progress_message.delete()

            elif chat_message.poll:
                if media_filter:
                    return
                await message.reply("**This post contains a poll which cannot be downloaded.**")

            elif chat_message.text or chat_message.caption:
                if media_filter:
                    return
                txt = raw_text or raw_caption
                ents = raw_text_entities if raw_text else raw_caption_entities
                # Send text to every forward destination (channel(s) + dump),
                # or the user's DM when none are configured.
                text_targets = effective_forward_chat_ids or [message.chat.id]
                for text_target in text_targets:
                    try:
                        await bot.send_message(text_target, txt, entities=ents or None)
                    except BadRequest as e:
                        if "ENTITY_TEXT_INVALID" in str(e):
                            LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in text reply, retrying without entities: {e}")
                            await bot.send_message(text_target, txt)
                        else:
                            LOGGER(__name__).error(f"Failed to send text to {text_target}: {e}")
                    except Exception as e:
                        LOGGER(__name__).error(f"Failed to send text to {text_target}: {e}")
                if effective_forward_chat_ids:
                    LOGGER(__name__).info(f"Sent text message to chats: {effective_forward_chat_ids}")
            else:
                await message.reply("**No media or text found in the post URL.**")

        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            # Cap the sleep so the bot never freezes for hours. If the wait is
            # too long, report and return instead of blocking.
            if not await capped_flood_sleep(e, "handle_download"):
                await notify(
                    message,
                    f"Download postponed: Telegram asked to wait `{wait_s}s`, "
                    "which exceeds the safe limit. Try again later.",
                    level="warning",
                )
            return
        except PeerIdInvalid as e:
            await notify(
                message,
                "**Access Denied** — the user client cannot access this chat. "
                "Make sure the account has joined the channel/group.",
                exc=e,
            )
        except BadRequest as e:
            await notify(
                message,
                "**Bad Request** — Telegram rejected this. The message ID may be "
                "invalid or the chat inaccessible.",
                exc=e,
            )
        except KeyError as e:
            await notify(message, "**Invalid URL format.**", exc=e)
        except Exception as e:
            await notify(message, "**An unexpected error occurred.** Check /logs for details.", exc=e)


async def handle_story_download(bot: Client, message: Message, story_url: str):
    global forward_chat_id
    async with download_semaphore:
        if "?" in story_url:
            story_url = story_url.split("?", 1)[0]

        # Use the per-user logged-in session when available, else the global one.
        user_client = get_user_client(message)

        try:
            effective_forward_chat_ids = []

            # Same multi-destination logic as handle_download: per-user
            # /setchannel target plus the global dump channel, deduplicated and
            # permission-checked.
            user_channel_id = None
            if message.from_user:
                user_channel_id = channel_manager.get_channel(message.from_user.id)

            seen_targets = set()
            for candidate in (user_channel_id, forward_chat_id):
                if not candidate or candidate in seen_targets:
                    continue
                seen_targets.add(candidate)
                ok, err_msg = await check_forward_permission(bot, candidate)
                if not ok:
                    await message.reply(
                        f"⚠️ **Forward chat `{candidate}` misconfigured:** {err_msg}\n\n"
                        "That destination will be skipped."
                    )
                    continue
                effective_forward_chat_ids.append(candidate)

            chat_username, story_id = getStoryChatMsgID(story_url)

            story = None
            for attempt in range(2):
                try:
                    story = await user_client.get_stories(
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
                is_premium = bool(getattr(user_client.me, "is_premium", False)) if user_client.me else False
                if not await fileSizeLimit(
                    story.video.file_size, message, "download", is_premium
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
            unique_folder = f"{message.id}_{chat_username}_{story_id}"
            download_path = get_download_path(unique_folder, filename)

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
                forward_chat_ids=effective_forward_chat_ids,
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
@safe_handler
async def download_media(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after the /dl command.**")
        return

    post_url = message.command[1]
    await track_task(handle_download(bot, message, post_url))


@bot.on_message(filters.command("dls") & filters.private)
@safe_handler
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
@safe_handler
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
    base_delay = PyroConf.FLOOD_WAIT_DELAY
    current_delay = base_delay

    for sid in range(start_id, end_id + 1):
        url = f"{prefix}/{sid}"
        task = track_task(handle_story_download(bot, message, url))
        batch_tasks.append(task)

        # Gentle inter-post spacing to smooth request bursts.
        await asyncio.sleep(PyroConf.PER_POST_DELAY)

        if len(batch_tasks) >= BATCH_SIZE:
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            hit_flood = False
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    await loading.delete()
                    return await message.reply(
                        f"**❌ Batch canceled** after downloading `{downloaded}` stories."
                    )
                elif isinstance(result, FloodWait):
                    hit_flood = True
                    failed += 1
                    LOGGER(__name__).error(f"Error: {result}")
                elif isinstance(result, Exception):
                    failed += 1
                    LOGGER(__name__).error(f"Error: {result}")
                else:
                    downloaded += 1

            batch_tasks.clear()

            if hit_flood:
                current_delay = min(current_delay * 2, 300)
                LOGGER(__name__).warning(
                    f"FloodWait during story batch; increasing inter-batch delay to {current_delay}s"
                )
            else:
                current_delay = max(base_delay, current_delay - 1)

            await asyncio.sleep(current_delay)

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
@safe_handler
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

    prefix = args[1].rsplit("/", 1)[0]

    # If an unfinished batch for this same user+chat+range exists, resume it
    # instead of starting over. This covers the case where /bdl is re-issued
    # after a crash before the auto-resume kicked in.
    resume_from = start_id
    downloaded = skipped = failed = 0
    if message.from_user:
        cp = batch_state.load_checkpoint(message.from_user.id, start_chat)
        if (
            cp
            and cp.get("start_id") == start_id
            and cp.get("end_id") == end_id
            and cp.get("next_id", start_id) > start_id
        ):
            resume_from = cp["next_id"]
            downloaded = cp.get("downloaded", 0)
            skipped = cp.get("skipped", 0)
            failed = cp.get("failed", 0)
            await message.reply(
                f"↩️ **Resuming previous batch** from post `{resume_from}` "
                f"(of `{start_id}`–`{end_id}`)."
            )

    await _run_batch_download(
        bot,
        message,
        start_chat=start_chat,
        prefix=prefix,
        start_id=start_id,
        end_id=end_id,
        resume_from=resume_from,
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
    )


async def _run_batch_download(
    bot: Client,
    message: Message,
    start_chat,
    prefix: str,
    start_id: int,
    end_id: int,
    resume_from: int = None,
    downloaded: int = 0,
    skipped: int = 0,
    failed: int = 0,
):
    """Core batch-download loop with checkpointing.

    Processes posts ``resume_from``..``end_id`` (inclusive). After each post is
    scheduled, a checkpoint recording the *next* unprocessed id is written so
    the batch can resume after a restart. The checkpoint is cleared when the
    batch completes or is cancelled.
    """
    if resume_from is None:
        resume_from = start_id

    user_id = message.from_user.id if message.from_user else None
    origin_chat_id = message.chat.id if message.chat else None
    command_message_id = message.id

    # Use the per-user logged-in session when available for the preview fetches.
    user_client = get_user_client(message)

    try:
        await user_client.get_chat(start_chat)
    except Exception:
        pass

    loading = await message.reply(f"📥 **Downloading posts {resume_from}–{end_id}…**")

    processed_media_groups = set()
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE
    # Adaptive pacing: start at the configured delay and grow it if we hit
    # FloodWait, so a 1000-2000 post run slows itself down instead of getting
    # rate-limited/banned. It relaxes back down after clean batches.
    base_delay = PyroConf.FLOOD_WAIT_DELAY
    current_delay = base_delay
    # Consecutive CHANNEL_INVALID / access failures. Crossing the configured
    # threshold aborts the batch instead of spinning through doomed requests.
    channel_invalid_streak = 0
    MAX_CI_STREAK = PyroConf.MAX_CHANNEL_INVALID_STREAK

    def _persist(next_id):
        if user_id is None:
            return
        batch_state.save_checkpoint(
            user_id=user_id,
            origin_chat_id=origin_chat_id,
            command_message_id=command_message_id,
            chat_id=start_chat,
            prefix=prefix,
            start_id=start_id,
            end_id=end_id,
            next_id=next_id,
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
        )

    def _clear():
        if user_id is not None:
            batch_state.clear_checkpoint(user_id, start_chat)

    for msg_id in range(resume_from, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            chat_msg = None
            for attempt in range(2):
                try:
                    chat_msg = await user_client.get_messages(chat_id=start_chat, message_ids=msg_id)
                    break
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 0)
                    LOGGER(__name__).warning(f"FloodWait fetching {url}: {wait_s}s")
                    # Cap: never sleep longer than MAX_FLOOD_WAIT. If Telegram
                    # asks for more, abort the whole batch cleanly.
                    if not await capped_flood_sleep(e, f"fetch {url}"):
                        _persist(msg_id)
                        await loading.delete()
                        await notify(
                            message,
                            f"**Batch paused.** Telegram asked to wait `{wait_s}s` "
                            f"(over the `{PyroConf.MAX_FLOOD_WAIT}s` limit). "
                            f"Re-send the `/bdl` command later to resume from post `{msg_id}`.",
                            level="warning",
                        )
                        return
                    current_delay = min(current_delay + wait_s, 300)
                    if attempt == 1:
                        raise
            if not chat_msg:
                skipped += 1
                # This id is fully handled; next unprocessed id is msg_id + 1.
                _persist(msg_id + 1)
                continue

            if chat_msg.media_group_id:
                if chat_msg.media_group_id in processed_media_groups:
                    skipped += 1
                    _persist(msg_id + 1)
                    continue
                processed_media_groups.add(chat_msg.media_group_id)

            has_media = bool(chat_msg.media_group_id or chat_msg.media)
            has_text  = bool(chat_msg.text or chat_msg.caption)
            if not (has_media or has_text):
                skipped += 1
                _persist(msg_id + 1)
                continue

            task = track_task(handle_download(bot, message, url))
            batch_tasks.append(task)

            # Gentle inter-post spacing to smooth request bursts.
            await asyncio.sleep(PyroConf.PER_POST_DELAY)

            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                hit_flood = False
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        # Persist so a resume picks up right after this batch.
                        _persist(msg_id + 1)
                        await loading.delete()
                        return await message.reply(
                            f"**❌ Batch canceled** after downloading `{downloaded}` posts.\n"
                            f"Send the same `/bdl` command to resume from post `{msg_id + 1}`."
                        )
                    elif isinstance(result, FloodWait):
                        hit_flood = True
                        failed += 1
                        LOGGER(__name__).error(f"Error: {result}")
                    elif isinstance(result, Exception):
                        failed += 1
                        if "CHANNEL_INVALID" in str(result) or "CHANNEL_PRIVATE" in str(result):
                            channel_invalid_streak += 1
                        LOGGER(__name__).error(f"Error: {result}")
                    else:
                        downloaded += 1
                        channel_invalid_streak = 0

                batch_tasks.clear()
                # Checkpoint after each completed batch of downloads.
                _persist(msg_id + 1)

                # If access failures piled up across this batch, abort.
                if channel_invalid_streak >= MAX_CI_STREAK:
                    await loading.delete()
                    await notify(
                        message,
                        f"**Batch aborted.** `{channel_invalid_streak}` channel-access "
                        "errors (CHANNEL_INVALID) in a row. The session account likely "
                        "isn't a member of this channel. Join it and re-send the command.",
                    )
                    return

                # Adapt the delay: back off on flood, relax on clean batches.
                if hit_flood:
                    current_delay = min(current_delay * 2, 300)
                    LOGGER(__name__).warning(
                        f"FloodWait during batch; increasing inter-batch delay to {current_delay}s"
                    )
                else:
                    current_delay = max(base_delay, current_delay - 1)

                await asyncio.sleep(current_delay)

        except Exception as e:
            failed += 1
            _persist(msg_id + 1)
            err = str(e)
            if "CHANNEL_INVALID" in err or "CHANNEL_PRIVATE" in err or "PEER_ID_INVALID" in err.upper():
                channel_invalid_streak += 1
                LOGGER(__name__).error(
                    f"Access error at {url} (streak {channel_invalid_streak}/{MAX_CI_STREAK}): {e}"
                )
                if channel_invalid_streak >= MAX_CI_STREAK:
                    await loading.delete()
                    await notify(
                        message,
                        f"**Batch aborted.** `{channel_invalid_streak}` posts in a row "
                        "failed with a channel-access error (CHANNEL_INVALID).\n\n"
                        "Your session account likely **isn't a member** of this channel, "
                        "or the session lost access. Join the channel with the logged-in "
                        "account (or /login) and re-send the command.",
                    )
                    return
            else:
                # Any non-access error breaks the streak.
                channel_invalid_streak = 0
                LOGGER(__name__).error(f"Error at {url}: {e}")

    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1

    # Batch finished successfully — remove the checkpoint.
    _clear()

    await loading.delete()
    await message.reply(
        "**✅ Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded** : `{downloaded}` post(s)\n"
        f"⏭️ **Skipped**    : `{skipped}` (no content)\n"
        f"❌ **Failed**     : `{failed}` error(s)"
    )


@bot.on_message(filters.command("gc") & filters.private)
@safe_handler
async def grab_channel(bot: Client, message: Message):
    """Grab a whole channel filtered by media type (video/photo only).

    Usage:
        /gc <video|photo> <channel> [start_id] [end_id]

    Captions are stripped. Output goes to the channel set via /setchannel
    (or the global FORWARD_CHAT_ID), otherwise to the user's DM.
    """
    args = message.text.split()

    valid_types = {"video": "video", "photo": "photo", "both": "both", "all": "both", "pv": "both"}
    requested = args[1].lower() if len(args) >= 2 else ""

    if len(args) < 3 or requested not in valid_types:
        await message.reply(
            "🎯 **Grab Channel by Media Type**\n"
            "`/gc <video|photo|both> <channel> [start_id] [end_id]`\n\n"
            "💡 **Examples:**\n"
            "`/gc video @mychannel`\n"
            "`/gc photo @mychannel 1 500`\n"
            "`/gc both @mychannel`\n"
            "`/gc video https://t.me/mychannel/1 https://t.me/mychannel/2000`\n\n"
            "• Captions are removed.\n"
            "• `video`/`photo` send only that type; `both` sends photos **and** videos.\n"
            "• Goes to your `/setchannel` target if set, else your DM."
        )
        return

    media_filter = valid_types[requested]

    # Resolve channel + range. Accept either a bare @username / id and optional
    # numeric start/end, OR two full post links.
    start_id = end_id = None
    try:
        if args[2].startswith("https://t.me/"):
            start_chat, start_id = getChatMsgID(args[2])
            if len(args) >= 4 and args[3].startswith("https://t.me/"):
                end_chat, end_id = getChatMsgID(args[3])
                if end_chat != start_chat:
                    return await message.reply("**❌ Both links must be from the same channel.**")
            else:
                end_id = None
        else:
            start_chat = args[2]
            if len(args) >= 4 and args[3].isdigit():
                start_id = int(args[3])
            if len(args) >= 5 and args[4].isdigit():
                end_id = int(args[4])
    except Exception as e:
        return await message.reply(f"**❌ Error parsing arguments:\n{e}**")

    user_client = get_user_client(message)

    # Resolve the channel and auto-detect the latest message id when no end
    # was provided (so /gc video @channel grabs the whole channel).
    try:
        chat = await user_client.get_chat(start_chat)
        start_chat = chat.id
    except Exception as e:
        return await message.reply(
            f"**❌ Cannot access channel `{args[2]}`.**\n"
            f"Make sure your session is a member.\n\n**Details:** `{e}`"
        )

    if start_id is None:
        start_id = 1
    if end_id is None:
        try:
            latest_id = 1
            async for m in user_client.get_chat_history(start_chat, limit=1):
                latest_id = m.id
                break
            end_id = latest_id
        except Exception as e:
            return await message.reply(
                f"**❌ Could not determine the latest message id:** `{e}`\n"
                "Provide an explicit range: `/gc video @channel 1 500`"
            )

    if start_id > end_id:
        return await message.reply("**❌ Invalid range: start ID cannot exceed end ID.**")

    type_label = "photos & videos" if media_filter == "both" else f"{media_filter}s"
    loading = await message.reply(
        f"🎯 **Grabbing {type_label} from `{args[2]}` "
        f"({start_id}–{end_id})…**\nCaptions will be removed."
    )

    downloaded = skipped = failed = 0
    processed_media_groups = set()
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE
    base_delay = PyroConf.FLOOD_WAIT_DELAY
    current_delay = base_delay

    def _matches(m):
        if media_filter == "video":
            return bool(m.video)
        if media_filter == "photo":
            return bool(m.photo)
        # "both": accept photos and videos
        return bool(m.video) or bool(m.photo)

    for msg_id in range(start_id, end_id + 1):
        try:
            chat_msg = None
            for attempt in range(2):
                try:
                    chat_msg = await user_client.get_messages(chat_id=start_chat, message_ids=msg_id)
                    break
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 0)
                    LOGGER(__name__).warning(f"FloodWait fetching {msg_id}: {wait_s}s")
                    # Cap: never freeze for hours on a single FloodWait.
                    if not await capped_flood_sleep(e, f"gc fetch {msg_id}"):
                        await loading.delete()
                        await notify(
                            message,
                            f"**Grab paused.** Telegram asked to wait `{wait_s}s` "
                            f"(over the `{PyroConf.MAX_FLOOD_WAIT}s` limit). Try again later.",
                            level="warning",
                        )
                        return
                    current_delay = min(current_delay + wait_s, 300)
                    if attempt == 1:
                        raise
            if not chat_msg:
                skipped += 1
                continue

            # For media groups, only dispatch once per group; the group handler
            # applies the media filter internally.
            if chat_msg.media_group_id:
                if chat_msg.media_group_id in processed_media_groups:
                    skipped += 1
                    continue
                processed_media_groups.add(chat_msg.media_group_id)
            elif not _matches(chat_msg):
                skipped += 1
                continue

            url = f"https://t.me/c/{str(start_chat).replace('-100', '')}/{msg_id}"
            task = track_task(
                handle_download(bot, message, url, media_filter=media_filter, strip_caption=True)
            )
            batch_tasks.append(task)

            await asyncio.sleep(PyroConf.PER_POST_DELAY)

            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                hit_flood = False
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        await loading.delete()
                        return await message.reply(
                            f"**❌ Grab canceled** after `{downloaded}` items."
                        )
                    elif isinstance(result, FloodWait):
                        hit_flood = True
                        failed += 1
                    elif isinstance(result, Exception):
                        failed += 1
                        LOGGER(__name__).error(f"Error: {result}")
                    else:
                        downloaded += 1

                batch_tasks.clear()
                if hit_flood:
                    current_delay = min(current_delay * 2, 300)
                    LOGGER(__name__).warning(
                        f"FloodWait during /gc; increasing inter-batch delay to {current_delay}s"
                    )
                else:
                    current_delay = max(base_delay, current_delay - 1)
                await asyncio.sleep(current_delay)

        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error at msg {msg_id}: {e}")

    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1

    await loading.delete()
    await message.reply(
        "**✅ Channel Grab Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **Type**       : `{media_filter}`\n"
        f"📥 **Dispatched** : `{downloaded}` item(s)\n"
        f"⏭️ **Skipped**    : `{skipped}` (no match)\n"
        f"❌ **Failed**     : `{failed}` error(s)"
    )


@bot.on_message(filters.command("setchannel") & filters.private)
@safe_handler
async def set_channel_command(_, message: Message):
    user_id = message.from_user.id

    if len(message.command) < 2:
        current_channel = channel_manager.get_channel(user_id)
        if current_channel:
            await message.reply(
                f"📢 **Current Channel:** `{current_channel}`\n\n"
                "To change it, use: `/setchannel <channel_id_or_username>`\n"
                "To reset it, use: `/resetchannel`"
            )
        else:
            await message.reply(
                "📢 **Set Extraction Channel**\n\n"
                "Usage: `/setchannel <channel_id>` or `/setchannel <@username>`\n\n"
                "**Examples:**\n"
                "• `/setchannel -1001234567890`\n"
                "• `/setchannel @mychannel`\n\n"
                "**Note:** Make sure to add the bot to the channel as admin!"
            )
        return

    channel_input = message.command[1]

    try:
        # Try to get channel info
        if channel_input.startswith("@"):
            # Username provided
            try:
                channel_info = await bot.get_chat(channel_input)
                channel_id = channel_info.id
                channel_name = channel_info.title or channel_input
            except Exception:
                await message.reply(f"❌ **Channel not found:** {channel_input}")
                return
        else:
            # Channel ID provided
            try:
                channel_id = int(channel_input)
                try:
                    channel_info = await bot.get_chat(channel_id)
                    channel_name = channel_info.title or str(channel_id)
                except Exception:
                    channel_name = str(channel_id)
            except ValueError:
                await message.reply("❌ **Invalid channel ID format.**")
                return

        # Check if bot is admin in the channel
        try:
            bot_member = await bot.get_chat_member(channel_id, bot.me.id)
            if not bot_member.privileges or not (
                bot_member.privileges.can_post_messages
                or bot_member.privileges.can_edit_messages
            ):
                await message.reply(
                    f"❌ **Bot is not admin in {channel_name}**\n\n"
                    "Please add the bot to the channel as admin with post messages permission."
                )
                return
        except UserNotParticipant:
            await message.reply(
                f"❌ **Bot is not a member of {channel_name}**\n\n"
                "Please add the bot to the channel first."
            )
            return
        except Exception as e:
            await message.reply(f"❌ **Error checking channel permissions:** {str(e)}")
            return

        # Set channel
        if channel_manager.set_channel(user_id, channel_id):
            await message.reply(
                f"✅ **Channel Set Successfully!**\n\n"
                f"📢 **Channel:** {channel_name}\n"
                f"🆔 **ID:** `{channel_id}`\n\n"
                "Now all your downloaded files will be sent to this channel.\n\n"
                "Use `/resetchannel` to reset back to DM."
            )
        else:
            await message.reply("❌ **Failed to set channel.**")

    except Exception as e:
        await message.reply(f"❌ **Error:** {str(e)}")


@bot.on_message(filters.command("resetchannel") & filters.private)
@safe_handler
async def reset_channel_command(_, message: Message):
    user_id = message.from_user.id

    current_channel = channel_manager.get_channel(user_id)

    if not current_channel:
        await message.reply("❌ **No channel is currently set.**")
        return

    if channel_manager.reset_channel(user_id):
        await message.reply(
            f"✅ **Channel Reset Successfully!**\n\n"
            f"📢 **Previous Channel:** `{current_channel}`\n\n"
            "Your downloaded files will now be sent to DM only."
        )
    else:
        await message.reply("❌ **Failed to reset channel.**")


@bot.on_message(filters.command("setcaption") & filters.private)
@safe_handler
async def set_caption_command(_, message: Message):
    user_id = message.from_user.id

    if len(message.command) < 2:
        current_caption = caption_manager.get_caption(user_id)
        if current_caption:
            await message.reply(
                f"📝 **Current Caption:** `{current_caption}`\n\n"
                "To change it, use: `/setcaption <new_caption>`\n"
                "To reset it, use: `/resetcaption`"
            )
        else:
            await message.reply(
                "📝 **Set Custom Caption**\n\n"
                "Usage: `/setcaption <your_caption>`\n\n"
                "**Example:** `/setcaption My Downloaded File`\n\n"
                "This caption will be saved for your downloads."
            )
        return

    # Get caption from command (everything after /setcaption)
    caption = message.text.split(maxsplit=1)[1]

    if len(caption) > 200:
        await message.reply("❌ **Caption too long!** Maximum 200 characters allowed.")
        return

    if caption_manager.set_caption(user_id, caption):
        await message.reply(
            f"✅ **Caption Set Successfully!**\n\n"
            f"📝 **Caption:** `{caption}`\n\n"
            "Use `/resetcaption` to reset the caption."
        )
    else:
        await message.reply("❌ **Failed to set caption.** Please try again.")


@bot.on_message(filters.command("resetcaption") & filters.private)
@safe_handler
async def reset_caption_command(_, message: Message):
    user_id = message.from_user.id

    current_caption = caption_manager.get_caption(user_id)

    if not current_caption:
        await message.reply("❌ **No caption is currently set.**")
        return

    if caption_manager.remove_caption(user_id):
        await message.reply(
            f"✅ **Caption Reset Successfully!**\n\n"
            f"📝 **Previous Caption:** `{current_caption}`\n\n"
            "Your downloaded files will no longer have custom captions."
        )
    else:
        await message.reply("❌ **Failed to reset caption.** Please try again.")


@bot.on_message(filters.command("login") & filters.private)
@safe_handler
async def login_command(_, message: Message):
    user_id = message.from_user.id

    if len(message.command) < 2:
        await message.reply(
            "📱 **Login to Your Telegram Account**\n\n"
            "To login, send your phone number with country code:\n"
            "**Example:** `/login +1234567890`\n\n"
            "**Supported formats:**\n"
            "• `/login +1234567890`\n"
            "• `/login 1234567890`\n"
            "• `/login 001234567890`\n\n"
            "⚠️ **Note:** Make sure your phone number is correct as you'll receive an OTP."
        )
        return

    phone_number = message.command[1]

    # Show processing message
    processing_msg = await message.reply(
        "🔄 **Starting login process...**\n\n"
        "Please wait while we connect to Telegram servers."
    )

    try:
        success, response = await login_manager.start_login_process(user_id, phone_number)

        if success:
            user_states[user_id] = "waiting_for_code"
            await processing_msg.edit(
                f"✅ {response}\n\n"
                "💡 **Tip:** Send the code exactly as you receive it (with or without spaces)."
            )
        else:
            await processing_msg.edit(
                f"{response}\n\n"
                "💡 **Need help?** Make sure:\n"
                "• Phone number includes country code\n"
                "• You have access to this phone number\n"
                "• Try again in a few minutes if you see app update errors"
            )
    except Exception as e:
        LOGGER(__name__).error(f"Login command error: {e}")
        await processing_msg.edit(
            "❌ **Unexpected error occurred**\n\n"
            "Please try again in a few minutes."
        )


@bot.on_message(filters.command("logout") & filters.private)
@safe_handler
async def logout_command(_, message: Message):
    user_id = message.from_user.id

    if login_manager.get_user_session(user_id) is None and not os.path.exists(
        login_manager.get_user_data_path(user_id)
    ):
        await message.reply(
            "ℹ️ **You are not logged in.**\n\n"
            "Use `/login +<country_code><number>` to log in with your account."
        )
        return

    processing_msg = await message.reply("🔄 **Logging out...**")
    try:
        success, response = await login_manager.logout_user(user_id)
        # Also clear any in-progress login flow state for this user.
        user_states.pop(user_id, None)
        await processing_msg.edit(
            f"{response}\n\n"
            "Your downloads will now use the bot's default session again.\n"
            "Use `/login` to sign back in."
        )
    except Exception as e:
        LOGGER(__name__).error(f"Logout command error: {e}")
        await processing_msg.edit("❌ **Logout failed.** Check /logs for details.")


@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "bdl", "dls", "bdls", "gc", "stats", "logs", "killall", "cleanup", "login", "logout", "setcaption", "resetcaption", "setchannel", "resetchannel"]))
@safe_handler
async def handle_any_message(bot: Client, message: Message):
    user_id = message.from_user.id

    # Handle in-progress login flow (OTP code / 2FA password)
    if user_id in user_states and message.text:
        if user_states[user_id] == "waiting_for_code":
            success, response = await login_manager.verify_code(user_id, message.text)
            if success:
                del user_states[user_id]
            elif "password" in response.lower():
                user_states[user_id] = "waiting_for_password"
            await message.reply(response)
            return
        elif user_states[user_id] == "waiting_for_password":
            success, response = await login_manager.verify_password(user_id, message.text)
            if success:
                del user_states[user_id]
            await message.reply(response)
            return

    if message.text and not message.text.startswith("/"):
        text = message.text.strip()
        if is_story_link(text):
            await track_task(handle_story_download(bot, message, text))
        else:
            await track_task(handle_download(bot, message, text))


@bot.on_message(filters.command("stats") & filters.private)
@safe_handler
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


# Only this username is authorized to use /logs
LOGS_AUTHORIZED_USERNAME = "fakepra"


@bot.on_message(filters.command("logs") & filters.private)
@safe_handler
async def logs(_, message: Message):
    username = (message.from_user.username or "").lower()
    if username != LOGS_AUTHORIZED_USERNAME.lower():
        await message.reply("🚫 **You are not authorized to use this command.**")
        return

    if os.path.exists("logs.txt"):
        await message.reply_document(document="logs.txt", caption="**Logs**")
    else:
        await message.reply("**Not exists**")


@bot.on_message(filters.command("killall") & filters.private)
@safe_handler
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

    # Restore any user sessions created via /login so they survive restarts.
    try:
        await login_manager.load_existing_sessions()
        LOGGER(__name__).info(
            f"Restored {len(login_manager.user_sessions)} logged-in user session(s)."
        )
    except Exception as e:
        LOGGER(__name__).error(f"Failed to load existing user sessions: {e}")


async def resume_pending_batches():
    """Re-run any unfinished /bdl batches from their last checkpoint.

    For each pending checkpoint we re-fetch the original command message (so we
    have a real Message with from_user / chat / reply) and continue the batch
    from the stored ``next_id``.
    """
    pending = batch_state.list_pending_checkpoints()
    if not pending:
        return

    LOGGER(__name__).info(f"Found {len(pending)} interrupted batch(es) to resume.")
    for cp in pending:
        user_id = cp.get("user_id")
        origin_chat_id = cp.get("origin_chat_id")
        command_message_id = cp.get("command_message_id")
        start_chat = cp.get("chat_id")
        prefix = cp.get("prefix")
        start_id = cp.get("start_id")
        end_id = cp.get("end_id")
        next_id = cp.get("next_id", start_id)

        if next_id > end_id:
            # Nothing left; just clear it.
            if user_id is not None:
                batch_state.clear_checkpoint(user_id, start_chat)
            continue

        try:
            trigger = await bot.get_messages(origin_chat_id, command_message_id)
        except Exception as e:
            LOGGER(__name__).error(
                f"Could not fetch trigger message for resume (user {user_id}): {e}. "
                "Notifying user instead."
            )
            trigger = None

        if trigger is None:
            # Fall back to notifying the user so they can re-issue /bdl.
            try:
                await bot.send_message(
                    origin_chat_id,
                    "↩️ **A previous batch was interrupted by a restart.**\n"
                    f"Re-send `/bdl {prefix}/{start_id} {prefix}/{end_id}` to resume "
                    f"from post `{next_id}`.",
                )
            except Exception as notify_err:
                LOGGER(__name__).error(f"Failed to notify user {user_id} about resume: {notify_err}")
            continue

        LOGGER(__name__).info(
            f"Resuming batch for user {user_id}: {prefix}/{next_id}..{end_id}"
        )
        track_task(
            _run_batch_download(
                bot,
                trigger,
                start_chat=start_chat,
                prefix=prefix,
                start_id=start_id,
                end_id=end_id,
                resume_from=next_id,
                downloaded=cp.get("downloaded", 0),
                skipped=cp.get("skipped", 0),
                failed=cp.get("failed", 0),
            )
        )

async def _notify_chat_only(text: str, level: str = "error"):
    """Send a notice to NOTIFY_CHAT_ID (and log it). Used where there is no
    triggering user message (startup, shutdown, background crashes)."""
    icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(level, "❌")
    log = LOGGER(__name__)
    (log.error if level == "error" else log.warning if level == "warning" else log.info)(text)
    notify_chat = getattr(PyroConf, "NOTIFY_CHAT_ID", None)
    if notify_chat:
        try:
            await bot.send_message(int(notify_chat), f"{icon} **Bot notice**\n{text}")
        except Exception as e:
            log.error(f"_notify_chat_only(): failed to send: {e}")


def _loop_exception_handler(loop, context):
    """Catch-all for exceptions in background tasks so a stray error is always
    reported instead of silently killing a task."""
    msg = context.get("exception", context.get("message"))
    LOGGER(__name__).error(f"Unhandled exception in event loop: {msg}")
    try:
        loop.create_task(
            _notify_chat_only(f"Unhandled background error: `{msg}`", level="error")
        )
    except Exception:
        pass


async def _startup():
    """Start both clients, run initialization, then resume interrupted batches,
    and block on idle() until a stop signal arrives.

    Everything runs inside the same running event loop so ``bot.get_messages``
    (used by the resume step) and background tasks work correctly.
    """
    await bot.start()
    await user.start()
    await initialize()

    # Install a catch-all handler so no background task dies silently.
    try:
        asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    except Exception as e:
        LOGGER(__name__).error(f"Could not set loop exception handler: {e}")

    # Announce readiness to the central chat.
    await _notify_chat_only("Bot started and ready. ✅", level="info")

    # Auto-resume any /bdl batches interrupted by a restart. Runs after the
    # clients are started so get_messages / task scheduling work.
    try:
        await resume_pending_batches()
    except Exception as e:
        await _notify_chat_only(f"Failed to resume pending batches: `{e}`", level="error")

    # Block here handling updates until SIGINT/SIGTERM (kurigram idle is async).
    await idle()

    await _shutdown()


async def _shutdown():
    try:
        await _notify_chat_only("Bot is shutting down.", level="warning")
    except Exception:
        pass
    try:
        await bot.stop()
    except Exception:
        pass
    try:
        await user.stop()
    except Exception:
        pass


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        LOGGER(__name__).info("Bot Started!")
        loop.run_until_complete(_startup())
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
        # Best-effort: report the fatal error to the central chat before exit.
        try:
            loop.run_until_complete(
                _notify_chat_only(f"Bot crashed with a fatal error: `{err}`", level="error")
            )
        except Exception:
            pass
    finally:
        LOGGER(__name__).info("Bot Stopped")
