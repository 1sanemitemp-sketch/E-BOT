import html

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
import menu
from client import app
from store import db


def user_line(u):
    uname = f" (@{u['username']})" if u.get("username") else ""
    return f"{html.escape(u['name'])}{uname} — <code>{u['id']}</code>"


def access_view(rec):
    """The screen an unauthorized user sees. Returns (text, keyboard or None)."""
    auth = db.auth_of(rec)
    head = ("🔒 <b>Access required</b>\n\n"
            "You need approval from an admin to use this bot.\n"
            f"Your ID: <code>{rec['id']}</code>\n\n")
    if auth == "pending":
        return head + "⏳ Your request is pending. You will get a message here when an admin responds.", None
    if auth == "rejected":
        return head + f"❌ Your request was declined. You can contact {config.ADMIN_CONTACTS}.", None
    text = (head + f"Copy your ID and send it to {config.ADMIN_CONTACTS}, "
            "or tap the button below to send them a request.")
    return text, Markup([[Btn("📨 Request Approval", callback_data="rq|req")]])


def request_text(rec):
    uname = f"@{rec['username']}" if rec.get("username") else "-"
    return ("🔔 <b>Approval request</b>\n\n"
            f"Name: {html.escape(rec['name'])}\n"
            f"Username: {uname}\n"
            f"ID: <code>{rec['id']}</code>")


def request_kb(uid):
    return Markup([
        [Btn("✅ Approve", callback_data=f"ap|approve|{uid}"),
         Btn("❌ Reject", callback_data=f"ap|reject|{uid}")],
        [Btn("⏳ Later", callback_data=f"ap|later|{uid}")],
    ])


async def set_auth_state(client, uid, state):
    """Authorize ('approved') or unauthorize ('rejected') a user and tell them."""
    db.set_auth(uid, state)
    text = ("✅ You have been approved. Send /help to see what I can do."
            if state == "approved" else
            "❌ Your access request was declined, or your access was removed.")
    try:
        await client.send_message(uid, text)
    except Exception:
        pass  # the user may never have started the bot


# ---------------------------------------------------------------- user side
@app.on_callback_query(filters.regex(r"^rq\|"))
async def request_cb(client, cq):
    rec = db.touch(cq.from_user)
    uid = cq.from_user.id
    if db.is_authorized(uid):
        return await cq.answer("You already have access.", show_alert=True)
    auth = db.auth_of(rec)
    if auth == "rejected":
        return await cq.answer("Your request was declined.", show_alert=True)
    if auth == "new":
        db.set_auth(uid, "pending")
        rec = db.get_user(uid)
        for rid in db.admin_ids():
            try:
                await client.send_message(rid, request_text(rec), reply_markup=request_kb(uid))
            except Exception:
                pass
    text, kb = access_view(rec)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer("Request sent.")


# ---------------------------------------------------------------- admin side
@app.on_callback_query(filters.regex(r"^ap\|"))
async def approval_cb(client, cq):
    if not db.is_admin(cq.from_user.id):
        return await cq.answer("Admins only.", show_alert=True)
    _, action, uid = cq.data.split("|")
    uid = int(uid)
    rec = db.get_user(uid)
    if not rec:
        return await cq.answer("User not found.", show_alert=True)
    if action == "later":
        return await cq.answer("Left pending. You can find it in /panel under Pending.", show_alert=True)
    if db.auth_of(rec) != "pending":
        return await cq.answer(f"Already handled ({db.auth_of(rec)}).", show_alert=True)
    state = "approved" if action == "approve" else "rejected"
    await set_auth_state(client, uid, state)
    who = html.escape(cq.from_user.first_name or str(cq.from_user.id))
    mark = "✅ Approved" if state == "approved" else "❌ Rejected"
    try:
        await cq.message.edit_text(f"{request_text(rec)}\n\n{mark} by {who}")
    except MessageNotModified:
        pass
    await cq.answer(mark)


def _ids(m):
    return [int(x) for x in m.command[1:] if x.isdigit()]


def _is_admin_msg(m):
    return bool(m.from_user) and db.is_admin(m.from_user.id)


@app.on_message(filters.command("a"))
async def authorize_cmd(client, m):
    if not _is_admin_msg(m):
        return
    ids = _ids(m)
    if not ids:
        return await m.reply_text("Usage: <code>/a user_id</code>")
    for uid in ids:
        db.ensure_user(uid)
        await set_auth_state(client, uid, "approved")
    await m.reply_text("✅ Authorized: " + ", ".join(f"<code>{i}</code>" for i in ids))


@app.on_message(filters.command("ua"))
async def unauthorize_cmd(client, m):
    if not _is_admin_msg(m):
        return
    ids = _ids(m)
    if not ids:
        return await m.reply_text("Usage: <code>/ua user_id</code>")
    done = []
    for uid in ids:
        if db.is_admin(uid):
            await m.reply_text(f"<code>{uid}</code> is an admin or the owner. Use /unadmin first.")
            continue
        db.ensure_user(uid)
        await set_auth_state(client, uid, "rejected")
        done.append(uid)
    if done:
        await m.reply_text("🚫 Unauthorized: " + ", ".join(f"<code>{i}</code>" for i in done))


async def _send_list(m, title, users):
    if not users:
        return await m.reply_text(f"{title}\n\nNo users.")
    chunk = f"{title} ({len(users)})\n"
    for i, u in enumerate(users, 1):
        line = f"\n{i}. {user_line(u)}"
        if len(chunk) + len(line) > 3800:
            await m.reply_text(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await m.reply_text(chunk)


@app.on_message(filters.command("alist"))
async def alist_cmd(client, m):
    if _is_admin_msg(m):
        await _send_list(m, "✅ <b>Authorized users</b>", db.list_users("approved"))


@app.on_message(filters.command("ualist"))
async def ualist_cmd(client, m):
    if not _is_admin_msg(m):
        return
    users = db.list_users("unauthorized")
    tag = {"new": "not requested", "pending": "pending", "rejected": "rejected"}

    shown = []
    for u in users:
        v = dict(u)
        v["name"] = f"{u['name']} [{tag.get(db.auth_of(u), db.auth_of(u))}]"
        shown.append(v)
    await _send_list(m, "🚫 <b>Unauthorized users</b>", shown)


@app.on_message(filters.command("admin"))
async def admin_cmd(client, m):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    ids = _ids(m)
    if not ids:
        return await m.reply_text("Usage: <code>/admin user_id</code>")
    for uid in ids:
        db.set_admin(uid, True)
        await menu.apply_menu(client, uid)
        try:
            await client.send_message(uid, "⭐ You are now an admin of this bot. Send /help to see your commands.")
        except Exception:
            pass
    await m.reply_text("⭐ Admin added: " + ", ".join(f"<code>{i}</code>" for i in ids))


@app.on_message(filters.command("unadmin"))
async def unadmin_cmd(client, m):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    ids = [i for i in _ids(m) if i != config.OWNER_ID]
    if not ids:
        return await m.reply_text("Usage: <code>/unadmin user_id</code>")
    for uid in ids:
        db.set_admin(uid, False)
        await menu.apply_menu(client, uid)
    await m.reply_text("Admin removed: " + ", ".join(f"<code>{i}</code>" for i in ids))
