import logging
import os
import shutil

import config

config.validate()

from pyrogram import idle  # noqa: E402
from pyrogram.types import BotCommand, BotCommandScopeChat  # noqa: E402

from client import app  # noqa: E402
import handlers  # noqa: E402,F401  (registers handlers)
import panel  # noqa: E402,F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

USER_COMMANDS = [
    BotCommand("start", "Bot start"),
    BotCommand("help", "Shob command"),
    BotCommand("settings", "Encode, GDrive, upload settings"),
    BotCommand("genc", "Encode kore GDrive e upload"),
    BotCommand("tenc", "Encode kore Telegram e upload"),
    BotCommand("rclone", "rclone.conf save koro"),
    BotCommand("status", "Running jobs"),
    BotCommand("cancel", "Job cancel"),
]
OWNER_COMMANDS = USER_COMMANDS + [
    BotCommand("panel", "User control panel"),
    BotCommand("restart", "Bot restart"),
]


async def main():
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    os.makedirs(config.WORK_DIR, exist_ok=True)

    await app.start()
    await app.set_bot_commands(USER_COMMANDS)
    try:
        await app.set_bot_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=config.OWNER_ID))
        await app.send_message(config.OWNER_ID, "✅ Bot started.")
    except Exception as e:
        log.warning("Owner setup skipped (owner must /start the bot once): %s", e)
    log.info("Bot is running.")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
