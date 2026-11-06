import asyncio
import logging
import os
import shutil

import config

config.validate()

# Create the event loop BEFORE importing the client so every Pyrogram version binds to it
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pyrogram import idle  # noqa: E402

from client import app  # noqa: E402
import handlers  # noqa: E402,F401  (registers handlers)
import access  # noqa: E402,F401
import botsettings  # noqa: E402,F401
import menu  # noqa: E402
import panel  # noqa: E402,F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

async def main():
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    os.makedirs(config.WORK_DIR, exist_ok=True)

    await app.start()
    await menu.apply_all(app)
    try:
        await app.send_message(config.OWNER_ID, "✅ Bot started.")
    except Exception as e:
        log.warning("Owner notification skipped (the owner must send /start to the bot once): %s", e)
    log.info("Bot is running.")
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop.run_until_complete(main())
