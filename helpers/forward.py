# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

from logger import LOGGER
from pyrogram import Client
from pyrogram.errors import (
    ChatAdminRequired,
    ChatWriteForbidden,
    UserNotParticipant,
    PeerIdInvalid,
    ChannelPrivate,
    ChatForbidden,
)


async def resolve_forward_chat_id(raw: str):
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


async def check_forward_permission(bot: Client, chat_id) -> tuple[bool, str]:
    try:
        chat = await bot.get_chat(chat_id)
    except (PeerIdInvalid, ChannelPrivate, ChatForbidden, ValueError):
        return False, "Bot is not a member of the configured forward chat or the chat ID is invalid."
    except Exception as e:
        return False, f"Could not resolve forward chat: {e}"

    try:
        member = await bot.get_chat_member(chat_id, "me")
    except UserNotParticipant:
        return False, "Bot is not a member of the configured forward chat."
    except (ChatAdminRequired, ChatWriteForbidden):
        return False, "Bot does not have permission to send messages in the configured forward chat."
    except Exception as e:
        return False, f"Could not check bot permissions in forward chat: {e}"

    from pyrogram.enums import ChatMemberStatus, ChatType

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        status = member.status
        if status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            return False, "Bot is banned or not present in the configured forward chat."

        if chat.type == ChatType.CHANNEL:
            if not (member.privileges and member.privileges.can_post_messages):
                return False, "Bot does not have 'Post Messages' permission in the configured forward channel."
        else:
            if status == ChatMemberStatus.RESTRICTED:
                if member.permissions and not member.permissions.can_send_media_messages:
                    return False, "Bot is restricted from sending media in the configured forward chat."

    return True, ""
