import html
import math
import time

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
from client import app
from jobs import JOBS
from store import db

PER_PAGE = 8
TITLES = {"all": "All Users", "active": "Active (24h)", "muted": "Muted",
          "banned": "Banned", "admins": "Admins"}


def _is_owner(_, __, u):
    return bool(u.from_user) and u.from_user.id == config.OWNER_ID


owner = filters.create(_is_owner)


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "-"


def main_view():
    c = db.counts()
    text = (
        "👑 <b>Admin Panel</b>\n\n"
        f"Total users: <b>{c['all']}</b>\n"
        f"Active (24h): <b>{c['active']}</b>\n"
        f"Muted: <b>{c['muted']}</b>\n"
        f"Banned: <b>{c['banned']}</b>\n"
        f"Admins: <b>{c['admins']}</b>\n"
        f"Running jobs: <b>{len(JOBS)}</b>"
    )
    rows = [[Btn(f"👥 {TITLES[k]} ({c[k]})", callback_data=f"pn|list|{k}|0")]
            for k in ("all", "active", "muted", "banned", "admins")]
    rows.append([Btn("Close", callback_data="pn|close")])
    return text, Markup(rows)


def _label(u):
    icon = "🚫" if u["status"] == "banned" else "🔇" if u["status"] == "muted" else "👤"
    star = "⭐" if u.get("admin") else ""
    uname = f" @{u['username']}" if u.get("username") else ""
    return f"{icon}{star} {u['name'][:16]}{uname} · {u['id']}"


def list_view(flt, page):
    users = db.list_users(flt)
    pages = max(1, math.ceil(len(users) / PER_PAGE))
    page = max(0, min(page, pages - 1))
    text = f"👥 <b>{TITLES[flt]}</b> — {len(users)} user(s)"
    if not users:
        text += "\n\nNo users."
    rows = [[Btn(_label(u), callback_data=f"pn|user|{u['id']}|{flt}|{page}")]
            for u in users[page * PER_PAGE:(page + 1) * PER_PAGE]]
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"pn|list|{flt}|{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="pn|noop"))
    if page < pages - 1:
        nav.append(Btn("➡️", callback_data=f"pn|list|{flt}|{page + 1}"))
    rows.append(nav)
    rows.append([Btn("« Back", callback_data="pn|main")])
    return text, Markup(rows)


def user_view(uid, flt="all", page=0):
    u = db.get_user(uid)
    if not u:
        return "User not found.", Markup([[Btn("« Back", callback_data="pn|main")]])
    status = {"active": "Active", "muted": "Muted 🔇", "banned": "Banned 🚫"}[u["status"]]
    text = (
        f"👤 <b>{html.escape(u['name'])}</b>\n"
        f"ID: <code>{u['id']}</code>\n"
        f"Username: {'@' + u['username'] if u.get('username') else '-'}\n"
        f"Status: {status}\n"
        f"Role: {'Admin ⭐' if u.get('admin') else 'User'}\n"
        f"First seen: {_fmt(u.get('first_seen'))}\n"
        f"Last seen: {_fmt(u.get('last_seen'))}\n"
        f"Jobs run: {u.get('jobs', 0)}"
    )
    rows = []
    if u["id"] != config.OWNER_ID:
        def a(t, act):
            return Btn(t, callback_data=f"pn|act|{act}|{u['id']}|{flt}|{page}")

        rows.append([
            a("🔊 Unmute" if u["status"] == "muted" else "🔇 Mute",
              "unmute" if u["status"] == "muted" else "mute"),
            a("✅ Unban" if u["status"] == "banned" else "🚫 Ban",
              "unban" if u["status"] == "banned" else "ban"),
        ])
        rows.append([a("➖ Remove Admin" if u.get("admin") else "⭐ Make Admin",
                       "unadmin" if u.get("admin") else "admin")])
    rows.append([Btn("« Back", callback_data=f"pn|list|{flt}|{page}")])
    return text, Markup(rows)


@app.on_message(filters.command("panel") & owner)
async def panel_cmd(client, m):
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.isdigit() and db.get_user(int(arg)):
        text, kb = user_view(int(arg))
    else:
        text, kb = main_view()
    await m.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^pn\|") & owner)
async def panel_cb(client, cq):
    p = cq.data.split("|")
    act = p[1]
    if act == "noop":
        return await cq.answer()
    if act == "close":
        await cq.message.delete()
        return await cq.answer()

    if act == "act":
        _, _, action, uid, flt, page = p
        uid = int(uid)
        if uid != config.OWNER_ID:
            if action == "mute":
                db.set_status(uid, "muted")
            elif action == "ban":
                db.set_status(uid, "banned")
            elif action in ("unmute", "unban"):
                db.set_status(uid, "active")
            elif action == "admin":
                db.set_admin(uid, True)
            elif action == "unadmin":
                db.set_admin(uid, False)
        text, kb = user_view(uid, flt, int(page))
    elif act == "user":
        text, kb = user_view(int(p[2]), p[3], int(p[4]))
    elif act == "list":
        text, kb = list_view(p[2], int(p[3]))
    else:
        text, kb = main_view()
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer()
