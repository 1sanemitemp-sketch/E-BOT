import os

from pyrogram import Client
from pyrogram.enums import ParseMode

import config

app = Client(
    "encbot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DATA_DIR,
    parse_mode=ParseMode.HTML,
    max_concurrent_transmissions=int(os.getenv("TG_TRANSMISSIONS", "20")),
)

BOT_USERNAME = ""  # filled in main.py after the bot has started
