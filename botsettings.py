from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
import jobs
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


def _is_owner(_, __, u):
    return bool(u.from_user) and u.from_user.id == config.OWNER_ID


owner = filters.create(_is_owner)


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
        f"Accept new tasks: <b>{_on(db.get_bot('accept_tasks'))}</b>"
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
