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
)
