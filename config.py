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

    MAX_CONCURRENT_DOWNLOADS = int(getenv("MAX_CONCURRENT_DOWNLOADS", "100"))
    BATCH_SIZE = int(getenv("BATCH_SIZE", "100"))
    FLOOD_WAIT_DELAY = int(getenv("FLOOD_WAIT_DELAY", "100"))

    FORWARD_CHAT_ID = getenv("FORWARD_CHAT_ID", "").strip() or None
