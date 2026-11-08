from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
import jobs
import logchannel
from client import app
from store import db

CHOICES = {
    "max_concurrent": ("Max tasks at once (0 = unlimited)", list(range(0, 11))),
    "user_limit": ("Tasks per user (0 = unlimited)", list(range(0, 11))),
}


def _fmt(value):
    return "Unlimited" if not value else str(value)


TOGGLES = {
    "approval_required": "Approval required for new users",
    "split_gdrive": "Split zips above 2GB for GDrive",
    "accept_tasks": "Accept new tasks",
}


def _is_staff(_, __, u):
    return bool(u.from_user) and db.is_admin(u.from_user.id)


owner = filters.create(_is_staff)  # the owner and every admin (the name is kept for the decorators below)


def _on(v):
    return "ON" if v else "OFF"


def main_view():
    max_tasks, user_limit = _fmt(db.get_bot("max_concurrent")), _fmt(db.get_bot("user_limit"))
    text = (
        "🛠 <b>Bot Settings</b>\n\n"
        f"Max tasks at once: <b>{max_tasks}</b>\n"
        f"Tasks per user: <b>{user_limit}</b>\n"
        f"Approval required: <b>{_on(db.get_bot('approval_required'))}</b>\n"
        f"Split zips above 2GB for GDrive: <b>{_on(db.get_bot('split_gdrive'))}</b>\n"
        f"Accept new tasks: <b>{_on(db.get_bot('accept_tasks'))}</b>\n"
        f"Log channel: <b>{('<code>' + str(db.log_chat()) + '</code>') if db.log_chat() else 'Not set'}</b>"
        " (/setlog id, /unsetlog)"
    )
    rows = [
        [Btn(f"⚙️ Max tasks at once: {max_tasks}", callback_data="bs|opt|max_concurrent")],
        [Btn(f"👤 Tasks per user: {user_limit}", callback_data="bs|opt|user_limit")],
    ]
    for key, label in TOGGLES.items():
        rows.append([Btn(f"{label}: {_on(db.get_bot(key))}", callback_data=f"bs|tog|{key}")])
    rows.append([Btn("Close", callback_data="bs|close")])
    return text, Markup(rows)


def option_view(key):
    title, values = CHOICES[key]
    text = f"<b>{title}</b>\nCurrent: <code>{_fmt(db.get_bot(key))}</code>"
    btns = [Btn("0 ∞" if v == 0 else str(v), callback_data=f"bs|set|{key}|{v}") for v in values]
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([Btn("« Back", callback_data="bs|main")])
    return text, Markup(rows)


@app.on_message(filters.command("bot_settings") & owner)
async def bot_settings_cmd(client, m):
    text, kb = main_view()
    await m.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^bs\|") & owner)
async def bot_settings_cb(client, cq):
    p = cq.data.split("|")
    act = p[1]
    if act == "close":
        await cq.message.delete()
        return await cq.answer()
    if act == "opt":
        text, kb = option_view(p[2])
    else:
        if act == "set" and p[2] in CHOICES:
            value = int(p[3])
            db.set_bot(p[2], value)
            if p[2] == "max_concurrent":
                await jobs.LIMITER.set_limit(value)
        elif act == "tog" and p[2] in TOGGLES:
            db.set_bot(p[2], not db.get_bot(p[2]))
        text, kb = main_view()
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer()


@app.on_message(filters.command("setlog") & owner)
async def setlog_cmd(client, m):
    arg = m.command[1] if len(m.command) > 1 else ""
    if not arg.lstrip("-").isdigit():
        cur = db.log_chat()
        return await m.reply_text(
            "Usage: <code>/setlog -100xxxxxxxxxx</code>\n"
            "Make the bot an admin of the channel first (it must be able to post messages).\n"
            f"Current: {('<code>' + str(cur) + '</code>') if cur else 'Not set'}")
    chat = int(arg)
    ok, err = await logchannel.test(client, chat)
    if not ok:
        return await m.reply_text("❌ I could not write to that chat. Add the bot as an admin there and try again.\n"
                                  f"<code>{err[:300]}</code>")
    db.set_bot("log_chat", chat)
    await m.reply_text(f"✅ Log channel saved: <code>{chat}</code>")


@app.on_message(filters.command("unsetlog") & owner)
async def unsetlog_cmd(client, m):
    db.set_bot("log_chat", 0)
    left = config.LOG_CHANNEL
    await m.reply_text("🗑 Log channel removed from the bot settings."
                       + (f" The LOG_CHANNEL variable (<code>{left}</code>) is still used." if left else ""))
