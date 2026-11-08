"""Log channel: every command, task, approval and setting change is posted there (like WZML-X).

The channel comes from /setlog (saved in the bot settings) or from the LOG_CHANNEL variable.
Messages go through a queue, so logging never slows a task down and a FloodWait is waited out.
"""
import asyncio
import html
import logging
import time

from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait

from store import db

log = logging.getLogger("logchannel")
_Q = None
_worker = None
_last_error = ""


def _fmt_time():
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def who(user):
    """'Name (@username) [id]' of a pyrogram user."""
    if not user:
        return "Unknown"
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id)
    uname = f" (@{user.username})" if getattr(user, "username", None) else ""
    return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>{uname} [<code>{user.id}</code>]'


def who_id(uid):
    rec = db.get_user(uid)
    if not rec:
        return f"<code>{uid}</code>"
    uname = f" (@{rec['username']})" if rec.get("username") else ""
    return f'<a href="tg://user?id={uid}">{html.escape(rec["name"])}</a>{uname} [<code>{uid}</code>]'


def where(chat):
    if chat is None:
        return "-"
    if chat.type == ChatType.PRIVATE:
        return "Private chat"
    title = html.escape(getattr(chat, "title", "") or "")
    return f'{chat.type.name.title()} "{title}" [<code>{chat.id}</code>]'


def event(tag, *lines):
    """Queue one log message: #Tag, a line per item and the time. Safe to call from anywhere."""
    if not db.log_chat():
        return
    text = f"#{tag}\n" + "\n".join(str(x) for x in lines if x) + f"\n🕒 {_fmt_time()}"
    _enqueue(text[:4000])


def _enqueue(text):
    global _Q, _worker
    if _Q is None:
        _Q = asyncio.Queue(maxsize=1000)
    if _worker is None or _worker.done():
        _worker = asyncio.get_event_loop().create_task(_run())
    try:
        _Q.put_nowait(text)
    except asyncio.QueueFull:
        pass


async def _run():
    from client import app  # imported here: client must exist first
    global _last_error
    while True:
        text = await _Q.get()
        chat = db.log_chat()
        if not chat:
            continue
        for _ in range(3):
            try:
                await app.send_message(chat, text, disable_web_page_preview=True)
                _last_error = ""
                break
            except FloodWait as e:
                await asyncio.sleep((getattr(e, "value", None) or getattr(e, "x", 5)) + 1)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != _last_error:  # report each distinct problem only once
                    log.warning("could not write to the log channel %s: %s", chat, msg)
                    _last_error = msg
                break
        await asyncio.sleep(0.4)  # stay below Telegram's per chat rate limit


async def test(client, chat):
    """Send a test message. Returns (True, '') or (False, error)."""
    try:
        await client.send_message(chat, "✅ <b>Log channel connected.</b>\nAll bot activity will be logged here.")
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
