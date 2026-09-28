# Copyright (C) @NotYourDeveloper
# Channel: https://t.me/notyourdeveloper

import os
import asyncio
from time import time
from uuid import uuid4
from PIL import Image
from logger import LOGGER
from typing import Optional
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec, create_subprocess_shell, wait_for

from pyleaves import Leaves
from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Voice,
)

from helpers.files import (
    fileSizeLimit,
    cleanup_download
)

from helpers.msg import (
    get_raw_text
)

# Progress bar template
PROGRESS_BAR = """
Percentage: {percentage:.2f}% | {current}/{total}
Speed: {speed}/s
Estimated Time Left: {est_time} seconds
"""

async def cmd_exec(cmd, shell=False):
    if shell:
        proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    else:
        proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
    except Exception:
        stdout = "Unable to decode the response!"
    try:
        stderr = stderr.decode().strip()
    except Exception:
        stderr = "Unable to decode the error!"
    return stdout, stderr, proc.returncode


async def get_media_info(path):
    try:
        result = await cmd_exec([
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams", path,
        ])
    except Exception as e:
        LOGGER(__name__).error(f"Get Media Info: {e}. File: {path}")
        return 0, None, None, None, None

    if result[0] and result[2] == 0:
        try:
            import json
            data = json.loads(result[0])

            fields = data.get("format", {})
            duration = round(float(fields.get("duration", 0)))

            tags = fields.get("tags", {})
            artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
            title = tags.get("title") or tags.get("TITLE") or tags.get("Title")

            width = None
            height = None
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = stream.get("width")
                    height = stream.get("height")
                    break

            return duration, artist, title, width, height
        except Exception as e:
            LOGGER(__name__).error(f"Error parsing media info: {e}")
            return 0, None, None, None, None
    return 0, None, None, None, None


async def get_video_thumbnail(video_file, duration):
    os.makedirs("Assets", exist_ok=True)
    # Unique thumbnail per invocation. A shared "video_thumb.jpg" was being
    # overwritten/deleted by other concurrent uploads during batch downloads,
    # so videos got a wrong or missing thumbnail (and races on cleanup).
    output = os.path.join("Assets", f"thumb_{uuid4().hex}.jpg")

    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if not duration:
        duration = 3
    duration //= 2

    if os.path.exists(output):
        try:
            os.remove(output)
        except:
            pass

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(duration), "-i", video_file,
        "-vframes", "1", "-q:v", "2",
        "-y", output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not os.path.exists(output):
            LOGGER(__name__).warning(f"Thumbnail generation failed: {err}")
            return None
    except Exception as e:
        LOGGER(__name__).warning(f"Thumbnail generation error: {e}")
        return None
    return output


# Generate progress bar for downloading/uploading
def progressArgs(action: str, progress_message, start_time):
    return (action, progress_message, start_time, PROGRESS_BAR, "▓", "░")


def _normalize_targets(forward_chat_ids, fallback_chat_id):
    """Build a deduplicated, order-preserving list of destination chat IDs.

    ``forward_chat_ids`` may be a single id, a list/tuple of ids, or None.
    When it resolves to no usable targets we fall back to ``fallback_chat_id``
    (the user's DM) so the download is never silently dropped.
    """
    if forward_chat_ids is None:
        candidates = []
    elif isinstance(forward_chat_ids, (list, tuple, set)):
        candidates = list(forward_chat_ids)
    else:
        candidates = [forward_chat_ids]

    seen = set()
    targets = []
    for cid in candidates:
        if cid is None:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        targets.append(cid)

    if not targets and fallback_chat_id is not None:
        targets.append(fallback_chat_id)

    return targets


async def send_media(
    bot, message, media_path, media_type, caption, caption_entities,
    progress_message, start_time, forward_chat_ids=None, strip_caption=False,
    forward_chat_id=None,
):
    file_size = os.path.getsize(media_path)

    if not await fileSizeLimit(file_size, message, "upload"):
        return

    progress_args = progressArgs("📥 Uploading Progress", progress_message, start_time)
    LOGGER(__name__).info(f"Uploading media: {media_path} ({media_type})")

    # Accept either the new list-based ``forward_chat_ids`` or the legacy single
    # ``forward_chat_id`` for backward compatibility. The media is uploaded once
    # per destination (Telegram has no "send to many chats" primitive), reusing
    # the same local file so the download only happens once.
    if forward_chat_ids is None and forward_chat_id is not None:
        forward_chat_ids = forward_chat_id
    target_chat_ids = _normalize_targets(forward_chat_ids, message.chat.id)

    # Optionally drop the caption/entities entirely (used by media-type-only
    # whole-channel grabs where the user wants clean media).
    if strip_caption:
        caption = ""
        caption_entities = []

    last_sent_message = None

    async def _send_once(target_chat_id, cap, ents):
        if media_type == "photo":
            return await bot.send_photo(
                target_chat_id,
                media_path,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
        if media_type == "video":
            duration, _, _, width, height = await get_media_info(media_path)

            if not duration or duration == 0:
                duration = 0
                LOGGER(__name__).warning(f"Could not extract duration for {media_path}")

            if not width or not height:
                width = 640
                height = 480

            thumb = await get_video_thumbnail(media_path, duration)

            sent = await bot.send_video(
                target_chat_id,
                media_path,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                caption=cap,
                caption_entities=ents or None,
                supports_streaming=True,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            if thumb:
                cleanup_download(thumb)
            return sent
        if media_type == "audio":
            duration, artist, title, _, _ = await get_media_info(media_path)
            return await bot.send_audio(
                target_chat_id,
                media_path,
                duration=duration,
                performer=artist,
                title=title,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
        if media_type == "document":
            return await bot.send_document(
                target_chat_id,
                media_path,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
        return None

    # Send to every destination independently. A failure to deliver to one
    # target (e.g. the bot was removed from a channel) must not stop delivery
    # to the others.
    for target_chat_id in target_chat_ids:
        cur_cap = caption or ""
        cur_ents = caption_entities or []
        for attempt in range(2):
            try:
                sent = await _send_once(target_chat_id, cur_cap, cur_ents)
                if sent is not None:
                    last_sent_message = sent
                break
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 0) or 0)
                LOGGER(__name__).warning(f"FloodWait while uploading media: {wait_s}s")
                if wait_s > 0 and attempt == 0:
                    await asyncio.sleep(wait_s + 1)
                    continue
                raise
            except BadRequest as e:
                if "ENTITY_TEXT_INVALID" in str(e) and attempt == 0:
                    LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in caption entities, retrying without entities: {e}")
                    cur_ents = []
                    continue
                LOGGER(__name__).error(f"Failed to send media to {target_chat_id}: {e}")
                break
            except Exception as e:
                LOGGER(__name__).error(f"Failed to send media to {target_chat_id}: {e}")
                break

    return last_sent_message


async def download_single_media(msg, progress_message, start_time):
    for attempt in range(2):
        try:
            media_path = await msg.download(
                progress=Leaves.progress_for_pyrogram,
                progress_args=progressArgs(
                    "📥 Downloading Progress", progress_message, start_time
                ),
            )

            raw_cap, raw_ents = get_raw_text(msg.caption, msg.caption_entities)

            if msg.photo:
                return ("success", media_path, InputMediaPhoto(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
            if msg.video:
                return ("success", media_path, InputMediaVideo(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
            if msg.document:
                return ("success", media_path, InputMediaDocument(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
            if msg.audio:
                return ("success", media_path, InputMediaAudio(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))

        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait while downloading media: {wait_s}s")
            if wait_s > 0 and attempt == 0:
                await asyncio.sleep(wait_s + 1)
                continue
            return ("error", None, None)
        except Exception as e:
            LOGGER(__name__).info(f"Error downloading media: {e}")
            return ("error", None, None)

    return ("skip", None, None)


async def processMediaGroup(chat_message, bot, message, forward_chat_ids=None,
                            media_filter=None, strip_caption=False,
                            forward_chat_id=None):
    media_group_messages = await chat_message.get_media_group()
    valid_media = []
    temp_paths = []
    invalid_paths = []

    # Destinations: per-user /setchannel target and/or the global dump channel.
    # Falls back to the user's DM when none are configured. Accepts the legacy
    # single ``forward_chat_id`` for backward compatibility.
    if forward_chat_ids is None and forward_chat_id is not None:
        forward_chat_ids = forward_chat_id
    target_chat_ids = _normalize_targets(forward_chat_ids, message.chat.id)

    start_time = time()
    progress_message = await message.reply("📥 Downloading media group...")
    LOGGER(__name__).info(
        f"Downloading media group with {len(media_group_messages)} items..."
    )

    def _passes_filter(m):
        if media_filter == "photo":
            return bool(m.photo)
        if media_filter == "video":
            return bool(m.video)
        if media_filter == "both":
            return bool(m.photo) or bool(m.video)
        return True

    download_tasks = []
    for msg in media_group_messages:
        if not _passes_filter(msg):
            continue
        if msg.photo or msg.video or msg.document or msg.audio:
            download_tasks.append(download_single_media(msg, progress_message, start_time))

    results = await asyncio.gather(*download_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            LOGGER(__name__).error(f"Download task failed: {result}")
            continue

        status, media_path, media_obj = result
        if status == "success" and media_path and media_obj:
            temp_paths.append(media_path)
            valid_media.append(media_obj)
        elif status == "error" and media_path:
            invalid_paths.append(media_path)

    LOGGER(__name__).info(f"Valid media count: {len(valid_media)}")

    if strip_caption:
        for m in valid_media:
            m.caption = None
            m.caption_entities = None

    if valid_media:
        async def _send_group_to(target_chat_id):
            """Send the whole media group to one destination, with a per-item
            fallback if the grouped send fails. Returns the sent messages."""
            sent_messages = []
            # send_media_group mutates nothing, but caption_entities may be
            # stripped on ENTITY_TEXT_INVALID; work on a fresh reference each try.
            try:
                for attempt in range(3):
                    try:
                        sent_messages = await bot.send_media_group(chat_id=target_chat_id, media=valid_media)
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while sending media group: {wait_s}s")
                        if wait_s > 0 and attempt < 2:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise
                    except BadRequest as e:
                        if "ENTITY_TEXT_INVALID" in str(e) and attempt == 0:
                            LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in media group, retrying without caption entities: {e}")
                            for m in valid_media:
                                m.caption_entities = None
                            continue
                        raise
            except Exception:
                await message.reply(
                    "**❌ Failed to send media group, trying individual uploads**"
                )
                for media in valid_media:
                    try:
                        sent = None
                        if isinstance(media, InputMediaPhoto):
                            sent = await bot.send_photo(
                                chat_id=target_chat_id,
                                photo=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaVideo):
                            sent = await bot.send_video(
                                chat_id=target_chat_id,
                                video=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaDocument):
                            sent = await bot.send_document(
                                chat_id=target_chat_id,
                                document=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaAudio):
                            sent = await bot.send_audio(
                                chat_id=target_chat_id,
                                audio=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, Voice):
                            sent = await bot.send_voice(
                                chat_id=target_chat_id,
                                voice=media.media,
                                caption=media.caption,
                            )
                        if sent:
                            sent_messages.append(sent)
                    except Exception as individual_e:
                        await message.reply(
                            f"Failed to upload individual media: {individual_e}"
                        )
            return sent_messages

        # Deliver to every destination. One target failing (e.g. bot removed
        # from a channel) must not block the others.
        for target_chat_id in target_chat_ids:
            try:
                await _send_group_to(target_chat_id)
            except Exception as e:
                LOGGER(__name__).error(f"Failed to send media group to {target_chat_id}: {e}")

        await progress_message.delete()

        # Media group was sent to every target above (channel(s) + dump, or the
        # user's DM when none configured), so no additional copy is required.

        for path in temp_paths + invalid_paths:
            cleanup_download(path)
        return True

    await progress_message.delete()
    await message.reply("❌ No valid media found in the media group.")
    for path in invalid_paths:
        cleanup_download(path)
    return False
