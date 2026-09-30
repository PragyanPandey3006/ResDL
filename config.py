# Copyright (C) @NotYourDeveloper
# Channel: https://t.me/notyourdeveloper

from os import getenv
from time import time
from dotenv import load_dotenv

try:
    load_dotenv("config.env.local")
    load_dotenv("config.env")
except Exception:
    pass

if not getenv("API_ID") or not getenv("API_ID").strip().isdigit():
    print("Error: API_ID must be set to a valid numeric Telegram API ID")
    exit(1)

if not getenv("API_HASH") or len(getenv("API_HASH").strip()) < 10:
    print("Error: API_HASH must be set to a valid Telegram API hash")
    exit(1)

if not getenv("BOT_TOKEN") or not getenv("BOT_TOKEN").count(":") == 1:
    print("Error: BOT_TOKEN must be in format '123456:abcdefghijklmnopqrstuvwxyz'")
    exit(1)

if (
    not getenv("SESSION_STRING")
    or getenv("SESSION_STRING") == "xxxxxxxxxxxxxxxxxxxxxxx"
    or getenv("SESSION_STRING") == "your_session_string_here"
):
    print("Error: SESSION_STRING must be set with a valid string")
    exit(1)


# Pyrogram setup
class PyroConf(object):
    API_ID = int(getenv("API_ID"))
    API_HASH = getenv("API_HASH")
    BOT_TOKEN = getenv("BOT_TOKEN")
    SESSION_STRING = getenv("SESSION_STRING")
    BOT_START_TIME = time()

    # Flood-safe defaults for large batches on a single account.
    MAX_CONCURRENT_DOWNLOADS = int(getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
    BATCH_SIZE = int(getenv("BATCH_SIZE", "5"))
    FLOOD_WAIT_DELAY = int(getenv("FLOOD_WAIT_DELAY", "8"))
    # Small delay between dispatching each individual post inside a batch, to
    # smooth request bursts. Helps avoid FloodWait on very large ranges.
    PER_POST_DELAY = float(getenv("PER_POST_DELAY", "1.5"))

    # Maximum number of seconds the bot is allowed to sleep for a single
    # Telegram FloodWait. If Telegram asks to wait longer than this, the bot
    # will NOT block for hours — it aborts the current action and notifies the
    # user instead. This is what stops the bot from "freezing". (default: 300s)
    MAX_FLOOD_WAIT = int(getenv("MAX_FLOOD_WAIT", "300"))

    # During a batch, if this many consecutive posts fail with CHANNEL_INVALID
    # (or a similar access error), the batch aborts early and notifies the
    # user, instead of spinning through thousands of doomed requests.
    MAX_CHANNEL_INVALID_STREAK = int(getenv("MAX_CHANNEL_INVALID_STREAK", "10"))

    FORWARD_CHAT_ID = getenv("FORWARD_CHAT_ID", "").strip() or None

    # Optional chat ID where the bot reports every problem/error centrally
    # (e.g. an admin/log channel). Leave empty to only notify the user + logs.
    NOTIFY_CHAT_ID = getenv("NOTIFY_CHAT_ID", "").strip() or None
