"""One shared status message per chat that lists every running task (like the Mixology status board)."""
import asyncio
import logging
import math

from pyrogram import filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
from client import app
from jobs import JOBS
from utils import bot_stats_text

log = logging.getLogger("board")
PER_PAGE = 4
BOARDS = {}  # chat id -> Board
_LOCKS = {}


class Board:
    def __init__(self, chat_id, msg_id, auto):
        self.chat_id, self.msg_id, self.auto = chat_id, msg_id, auto
        self.page = 0
        self.task = None


def jobs_for(chat_id):
    """A group shows the tasks started in it. A private chat shows the tasks started in private chats,
    plus the viewer's own group tasks."""
    if chat_id < 0:
        return [j for j in JOBS.values() if j.origin_id == chat_id]
    return [j for j in JOBS.values() if j.origin_id > 0 or j.user_id == chat_id]


def render(page=0, chat_id=0):
    """Returns (text, keyboard, page) for the given page of the task list."""
    jobs = jobs_for(chat_id)
    foot = f"\n\n⌬ <b>Bot Stats</b>\n{bot_stats_text()}"
    if not jobs:
        kb = Markup([[Btn("📊 Stats", callback_data="jt|x"), Btn("♻️ Refresh", callback_data="sr|0")]])
        return "<b>No active tasks!</b>" + foot, kb, 0
    pages = math.ceil(len(jobs) / PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PER_PAGE
    blocks = [j.render_block(n) for n, j in enumerate(jobs[start:start + PER_PAGE], start + 1)]
    rows = []
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("⬅️", callback_data=f"sr|{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", callback_data=f"sr|{page}"))
        if page < pages - 1:
            nav.append(Btn("➡️", callback_data=f"sr|{page + 1}"))
        rows.append(nav)
    rows.append([Btn("📊 Stats", callback_data="jt|x"), Btn("♻️ Refresh", callback_data=f"sr|{page}")])
    return "\n\n".join(blocks) + f"\n\nTasks: {len(jobs)}" + foot, Markup(rows), page


async def _edit(client, b):
    text, kb, b.page = render(b.page, b.chat_id)
    try:
        await client.edit_message_text(b.chat_id, b.msg_id, text, reply_markup=kb)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep((getattr(e, "value", None) or getattr(e, "x", 5)) + 1)
    except Exception:
        log.debug("board edit failed", exc_info=True)


async def _loop(client, b):
    while True:
        await asyncio.sleep(config.STATUS_INTERVAL)
        if BOARDS.get(b.chat_id) is not b:
            return  # replaced by a newer board
        if not jobs_for(b.chat_id):
            break
        await _edit(client, b)
    BOARDS.pop(b.chat_id, None)
    try:
        if b.auto:
            await client.delete_messages(b.chat_id, b.msg_id)  # nothing left to show
        else:
            await _edit(client, b)  # a board opened with /status stays and says "No active tasks!"
    except Exception:
        pass


async def ensure(client, chat_id, auto=True, fresh=False):
    """Make sure this chat has a live board. fresh=True replaces the old one (used by /status)."""
    async with _LOCKS.setdefault(chat_id, asyncio.Lock()):
        b = BOARDS.get(chat_id)
        if b and not fresh:
            return b
        if b:
            if b.task:
                b.task.cancel()
            BOARDS.pop(chat_id, None)
            try:
                await client.delete_messages(chat_id, b.msg_id)
            except Exception:
                pass
        text, kb, _ = render(0, chat_id)
        msg = await client.send_message(chat_id, text, reply_markup=kb)
        b = Board(chat_id, msg.id, auto)
        BOARDS[chat_id] = b
        b.task = asyncio.create_task(_loop(client, b))
        return b


@app.on_callback_query(filters.regex(r"^sr\|"))
async def board_cb(client, cq):
    text, kb, page = render(int(cq.data.split("|")[1]), cq.message.chat.id)
    b = BOARDS.get(cq.message.chat.id)
    if b and b.msg_id == cq.message.id:
        b.page = page
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer("Refreshed")
