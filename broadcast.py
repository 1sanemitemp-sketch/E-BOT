import asyncio
import logging

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from client import app
from store import db

log = logging.getLogger("broadcast")
TASKS = set()

USAGE = ("Usage:\n<code>/broadcast your message</code> (or /bc)\n"
         "Or reply to any message, sticker or photo with /broadcast to send that message to everyone.")


def _is_staff(_, __, m):
    return bool(m.from_user) and db.is_admin(m.from_user.id)


staff = filters.create(_is_staff)


def recipients():
    """Everyone who started the bot, except banned users."""
    return [u["id"] for u in db.data["users"].values()
            if u.get("last_seen", 0) > 0 and u.get("status") != "banned"]


async def _send_one(client, uid, source, text):
    """Send the broadcast to one user. Returns True on success."""
    for _ in range(3):
        try:
            if source is not None:  # a replied message is copied as it is (text, photo, sticker, ...)
                await client.copy_message(uid, source.chat.id, source.id)
            else:
                try:
                    await client.send_message(uid, text)
                except FloodWait:
                    raise
                except Exception:  # broken HTML in the text: send it as plain text instead
                    await client.send_message(uid, text, parse_mode=ParseMode.DISABLED)
            return True
        except FloodWait as e:
            await asyncio.sleep((getattr(e, "value", None) or getattr(e, "x", 5)) + 1)
        except Exception:  # blocked the bot, deleted account, ...
            return False
    return False


async def _run(client, status, users, source, text):
    ok = failed = 0
    for uid in users:
        if await _send_one(client, uid, source, text):
            ok += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # stay well below Telegram's bulk limits
    try:
        await status.edit_text(f"✅ <b>Broadcast finished</b>\nSent: {ok}\nFailed: {failed}")
    except Exception:
        log.info("broadcast finished: sent=%s failed=%s", ok, failed)


@app.on_message(filters.command(["broadcast", "bc"]) & staff)
async def broadcast_cmd(client, m):
    parts = (m.text or "").split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    source = m.reply_to_message  # a reply wins over typed text
    if source is None and not text:
        return await m.reply_text(USAGE)
    users = recipients()
    status = await m.reply_text(f"📣 Broadcasting to {len(users)} user(s)...")
    task = asyncio.create_task(_run(client, status, users, source, text))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
