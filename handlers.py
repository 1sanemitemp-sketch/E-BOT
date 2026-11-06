import asyncio
import html
import os
import re
import sys
import time

from pyrogram import StopPropagation, filters
from pyrogram.enums import ChatType
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
import rclone
import thumbs
from access import access_view
from client import app
from jobs import JOBS, NO_CONF, Job, job_kb, run_job
from store import DEFAULTS, db
from utils import bot_stats_text, stats

TASKS = set()
DEFAULT_TAG = DEFAULTS["tag"]
AWAIT = {}  # user id -> (what we wait for: thumb/tag/rclone, time, id of the triggering message)

OPTIONS = {
    "vcodec": [("libx264", "libx264"), ("libx265", "libx265"), ("libvpx-vp9", "libvpx-vp9"),
               ("libaom-av1", "libaom-av1"), ("copy", "copy")],
    "acodec": [("aac", "aac"), ("ac3", "ac3"), ("opus", "opus"), ("libmp3lame", "libmp3lame"),
               ("copy", "copy")],
    "crf": [(f"{x / 2:g}", f"{x / 2:g}") for x in range(32, 57)],  # 16 to 28 in 0.5 steps
    "preset": [(v, v) for v in ("ultrafast", "superfast", "veryfast", "fast", "medium", "slow",
                                "slower", "veryslow", "placebo")],
    "abitrate": [(v, v) for v in ("64k", "96k", "112k", "128k", "160k", "192k", "224k", "256k", "320k")],
    "res": [("Original / Auto", "-"), ("1920:1080", "1920:1080"), ("1280:720", "1280:720"),
            ("854:480", "854:480"), ("640:360", "640:360"), ("3840:2160", "3840:2160")],
    "fps": [("Original / Auto", "-"), ("24", "24"), ("25", "25"), ("30", "30"), ("60", "60")],
    "ext": [(v, v) for v in ("mkv", "mp4", "avi", "webm", "ts", "mov")],
}

START_TEXT = (
    "👋 <b>Encode Bot</b>\n\n"
    "I download videos from a GDrive folder/file or a direct link, encode them, "
    "and upload the result to GDrive or Telegram.\n\n"
    "/settings - encode settings and GDrive connect\n"
    "/help - all commands"
)

HELP_TEXT = (
    "<b>Commands</b>\n\n"
    "/genc <code>link</code> - encode and upload to GDrive\n"
    "/tenc <code>link</code> - encode and upload to Telegram\n"
    "<code>link</code> = GDrive folder/file link or a direct download link\n"
    "A link to an archive (zip, rar, 7z) without -e is not encoded, it is uploaded as it is. "
    "Files above 2GB are uploaded in parts.\n\n"
    "<b>Arguments</b>\n"
    "<code>-n NewName.mkv</code> - output file name (with -e: the archive name)\n"
    "<code>-e</code> - extract the archive and encode every video inside\n"
    "<code>-z</code> - zip the encoded files and upload one zip\n"
    "<code>-t image_link</code> - use this thumbnail for this task (Telegram uploads)\n"
    "Example: <code>/genc https://site.com/pack.zip -e -z -n Pack.zip</code>\n\n"
    "/settings - encode settings, tag, quality name swap, send as media/document, thumbnail, GDrive\n"
    "/rclone - connect GDrive: send /rclone, then send your rclone.conf file\n"
    "/status - running tasks\n"
    "/cancel <code>id</code> - cancel a task\n"
)

ADMIN_HELP = (
    "\n<b>Admin</b>\n"
    "/a <code>user_id</code> - authorize a user\n"
    "/ua <code>user_id</code> - unauthorize a user\n"
    "/alist - authorized users\n"
    "/ualist - unauthorized users\n"
)

OWNER_HELP = (
    "\n<b>Owner</b>\n"
    "/admin <code>user_id</code> - make an admin\n"
    "/unadmin <code>user_id</code> - remove an admin\n"
    "/panel - user control panel\n"
    "/bot_settings - global bot settings\n"
    "/restart - restart the bot (cancels running tasks)\n"
)

USAGE = ("Please send a link.\n<code>/genc link</code>\n<code>/tenc link</code>\n"
         "<code>/tenc direct_link -n ANIME.mkv</code>\n"
         "<code>/genc zip_link -e -n Pack.zip</code>")

GD_HELP = (
    "🔗 <b>GDrive Connect (rclone)</b>\n\n"
    "1. Install rclone on a PC or in Termux (<code>pkg install rclone</code>, or rclone.org).\n"
    "2. Run <code>rclone config</code> → New remote → type <b>drive</b> → scope <b>1</b> (full access) "
    "→ log in with the browser and pick your Gmail account.\n"
    "3. Run <code>rclone config file</code> to see where the config is stored.\n"
    "4. Here, in a <b>private chat</b>, send <code>/rclone</code> and then send that "
    "<b>rclone.conf</b> file. No caption is needed.\n"
    "5. Once connected, use <code>/genc folder_link</code> or <code>/tenc folder_link</code>.\n\n"
    "⚠️ Never send the config in a group. It works like a password for your GDrive."
)


# ------------------------------------------------------------------ gate
@app.on_message(filters.all, group=-1)
async def gate_msg(client, m):
    if not m.from_user:
        return
    rec = db.touch(m.from_user)
    uid = m.from_user.id
    if uid == config.OWNER_ID:
        return
    private = m.chat.type == ChatType.PRIVATE
    if rec["status"] == "banned":
        if private:
            await m.reply_text("🚫 You are banned from using this bot.")
        raise StopPropagation
    if rec["status"] == "muted":
        raise StopPropagation
    if not db.is_authorized(uid):
        text = (m.text or "").strip()
        if text.startswith("/start"):
            return
        if text.startswith("/"):
            if private:
                view, kb = access_view(rec)
                await m.reply_text(view, reply_markup=kb)
            else:
                await m.reply_text("You are not authorized. Open the bot in a private chat and send /start.")
        raise StopPropagation


@app.on_callback_query(group=-1)
async def gate_cb(client, cq):
    rec = db.touch(cq.from_user)
    uid = cq.from_user.id
    if uid == config.OWNER_ID:
        return
    if rec["status"] in ("banned", "muted"):
        await cq.answer("No access.", show_alert=True)
        raise StopPropagation
    if not db.is_authorized(uid) and not (cq.data or "").startswith("rq|"):
        await cq.answer("You are not authorized to use this bot.", show_alert=True)
        raise StopPropagation


# ------------------------------------------------------------------ basic
@app.on_message(filters.command("start"))
async def start_cmd(client, m):
    if not m.from_user:
        return
    if not db.is_authorized(m.from_user.id):
        text, kb = access_view(db.get_user(m.from_user.id))
        return await m.reply_text(text, reply_markup=kb)
    await m.reply_text(START_TEXT)


@app.on_message(filters.command("help"))
async def help_cmd(client, m):
    text = HELP_TEXT
    if m.from_user:
        if db.is_admin(m.from_user.id):
            text += ADMIN_HELP
        if m.from_user.id == config.OWNER_ID:
            text += OWNER_HELP
    await m.reply_text(text)


@app.on_message(filters.command("restart"))
async def restart_cmd(client, m):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    jobs = list(JOBS.values())
    await m.reply_text(f"🔄 Restarting... cancelling {len(jobs)} running task(s).")
    for j in jobs:
        j.cancel()
    for _ in range(30):  # give the tasks a moment to report that they were cancelled
        if not JOBS:
            break
        await asyncio.sleep(0.5)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ------------------------------------------------------------------ encode commands
FLAG_RE = re.compile(r"(?:^|\s)-([ntez])(?=\s|$)")
URL_RE = re.compile(r"https?://\S+")


def parse_args(m):
    """Parse '/cmd link [-n name] [-t thumb_link] [-e] [-z]'."""
    text = m.text or ""
    parts = text.split(None, 1)
    body = parts[1] if len(parts) > 1 else ""
    opts = {"name": None, "thumb": None, "extract": False, "zip": False}
    flags = list(FLAG_RE.finditer(body))
    for i, f in enumerate(flags):
        end = flags[i + 1].start() if i + 1 < len(flags) else len(body)
        value = body[f.end():end].strip()
        key = f.group(1)
        if key == "e":
            opts["extract"] = True
        elif key == "z":
            opts["zip"] = True
        elif key == "n":
            lines = value.splitlines()
            opts["name"] = lines[0].strip() if lines and lines[0].strip() else None
        elif key == "t":
            um = URL_RE.search(value)
            opts["thumb"] = um.group(0) if um else None
    urls = [u for u in URL_RE.findall(body) if u != opts["thumb"]]
    link = urls[0] if urls else None
    if not link and m.reply_to_message:
        um = URL_RE.search(m.reply_to_message.text or m.reply_to_message.caption or "")
        link = um.group(0) if um else None
    return link, opts


@app.on_message(filters.command(["genc", "tenc", "enc"]))
async def enc_cmd(client, m):
    if not m.from_user:
        return
    kind = "tenc" if m.command[0].lower() == "tenc" else "genc"
    link, opts = parse_args(m)
    if not link:
        return await m.reply_text(USAGE)
    uid = m.from_user.id
    if (kind == "genc" or rclone.gdrive_id(link)) and not rclone.has_conf(uid):
        return await m.reply_text(NO_CONF)
    if not db.is_admin(uid):
        if not db.get_bot("accept_tasks"):
            return await m.reply_text("⏸ The bot is not accepting new tasks right now. Please try again later.")
        limit = db.get_bot("user_limit")
        if limit and sum(1 for j in JOBS.values() if j.user_id == uid) >= limit:
            return await m.reply_text(f"You already have {limit} running task(s). Wait for them to finish.")
    job = Job(kind, m.from_user, m.chat.id, link, opts["name"], db.get_settings(uid),
              thumb_url=opts["thumb"], extract=opts["extract"], zip_out=opts["zip"])
    JOBS[job.id] = job
    db.bump_jobs(uid)
    task = asyncio.create_task(run_job(client, job))
    job.task = task
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


# ------------------------------------------------------------------ status / cancel
def status_view(viewer):
    admin = db.is_admin(viewer)
    jobs = [j for j in JOBS.values() if admin or j.user_id == viewer]
    kb = Markup([[Btn("📊 Stats", callback_data="jt|x"), Btn("♻️ Refresh", callback_data="sr|x")]])
    foot = f"\n\n⌬ <b>Bot Stats</b>\n{bot_stats_text()}"
    if not jobs:
        return "<b>No active tasks!</b>" + foot, kb
    blocks, size = [], 0
    for j in jobs:
        block = j.render_block()
        if size + len(block) > 3200:
            blocks.append(f"... and {len(jobs) - len(blocks)} more task(s)")
            break
        blocks.append(block)
        size += len(block)
    return "\n\n".join(blocks) + foot, kb


@app.on_message(filters.command("status"))
async def status_cmd(client, m):
    if not m.from_user:
        return
    text, kb = status_view(m.from_user.id)
    await m.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^sr\|"))
async def status_refresh_cb(client, cq):
    text, kb = status_view(cq.from_user.id)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    await cq.answer("Refreshed")


async def _cancel(m, job):
    admin = db.is_admin(m.from_user.id)
    if not job or not (admin or job.user_id == m.from_user.id):
        return await m.reply_text("Task not found, or it is not yours.")
    job.cancel()
    await m.reply_text(f"Cancelling <code>{job.id}</code>...")


@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, m):
    if not m.from_user:
        return
    uid = m.from_user.id
    admin = db.is_admin(uid)
    mine = [j for j in JOBS.values() if admin or j.user_id == uid]
    if len(m.command) > 1:
        job = JOBS.get(m.command[1])
    elif len(mine) == 1:
        job = mine[0]
    else:
        return await m.reply_text("Please provide a task id: <code>/cancel id</code> (see /status).")
    await _cancel(m, job)


@app.on_message(filters.regex(r"^/cancel_([0-9a-f]{8})"))
async def cancel_link_cmd(client, m):
    if m.from_user:
        await _cancel(m, JOBS.get(m.matches[0].group(1)))


@app.on_callback_query(filters.regex(r"^jc\|"))
async def cancel_cb(client, cq):
    job = JOBS.get(cq.data.split("|")[1])
    if not job:
        return await cq.answer("This task has already finished.", show_alert=True)
    if not (db.is_admin(cq.from_user.id) or job.user_id == cq.from_user.id):
        return await cq.answer("This is not your task.", show_alert=True)
    job.cancel()
    await cq.answer("Cancelling...")


@app.on_callback_query(filters.regex(r"^jr\|"))
async def refresh_cb(client, cq):
    job = JOBS.get(cq.data.split("|")[1])
    if not job:
        return await cq.answer("This task has already finished.", show_alert=True)
    try:
        await cq.message.edit_text(job.render(), reply_markup=job_kb(job.id))
    except MessageNotModified:
        pass
    await cq.answer("Refreshed")


@app.on_callback_query(filters.regex(r"^jt\|"))
async def stats_cb(client, cq):
    st = stats()
    await cq.answer(
        f"CPU: {st['cpu']:.1f}%\nRAM: {st['ram']:.1f}%\n"
        f"Free disk: {st['free']} [{st['free_pct']:.1f}%]\nUptime: {st['up']}\n"
        f"Running tasks: {len(JOBS)}",
        show_alert=True,
    )


# ------------------------------------------------------------------ rclone.conf
async def _save_rclone(client, m, data):
    uid = m.from_user.id
    remote = rclone.save_conf(uid, data)
    if not remote:
        return await m.reply_text("❌ No <b>drive</b> remote found in this config.")
    ok, err = await rclone.verify(uid, remote)
    if not ok:
        rclone.remove_conf(uid)
        return await m.reply_text(f"❌ Could not connect to GDrive:\n<code>{html.escape(err)}</code>")
    try:
        await m.delete()  # remove the file (it contains tokens) from the chat
    except Exception:
        pass
    await client.send_message(m.chat.id, f"✅ GDrive connected (remote: <code>{html.escape(remote)}</code>)")


async def _read_conf_document(client, doc, uid):
    if doc.file_size and doc.file_size > 200 * 1024:
        return None
    os.makedirs(config.WORK_DIR, exist_ok=True)
    path = await client.download_media(doc, file_name=os.path.join(config.WORK_DIR, f"rc_{uid}.conf"))
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data


def _rc_filter(_, __, m):
    return (m.caption or m.text or "").strip().lower().startswith("/rclone")


@app.on_message(filters.create(_rc_filter))
async def rclone_cmd(client, m):
    if not m.from_user:
        return
    if m.chat.type != ChatType.PRIVATE:
        return await m.reply_text("⚠️ Use /rclone in a private chat only.")
    uid = m.from_user.id
    AWAIT.pop(uid, None)
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if not doc:
        AWAIT[uid] = ("rclone", time.time(), m.id)
        return await m.reply_text(GD_HELP + "\n\n📎 <b>Send your rclone.conf file now.</b>")
    data = await _read_conf_document(client, doc, uid)
    if data is None:
        return await m.reply_text("This file is too large to be an rclone.conf.")
    await _save_rclone(client, m, data)


# ------------------------------------------------------------------ settings UI
def _on(v):
    return "ON" if v else "OFF"


def main_text(uid):
    s = db.get_settings(uid)
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"<b>Encode:</b> {s['vcodec']} / {s['acodec']} {s['abitrate']} / CRF {s['crf']} / "
        f"{s['preset']} / {s['res'] or 'Auto'} / {s['ext']}\n"
        f"<b>Send as:</b> {'Document' if s['as_doc'] else 'Media'}\n"
        f"<b>Quality Name Swap:</b> {_on(s['swap'])}\n"
        f"<b>Tag:</b> <code>{html.escape(s['tag'])}</code>\n"
        f"<b>Auto Delete Source:</b> {_on(s['auto_delete'])} (permanent)\n"
        f"<b>Thumbnail:</b> {'Set ✅' if thumbs.has(uid) else 'Not set'}\n"
        f"<b>GDrive:</b> {'Connected ✅' if rclone.has_conf(uid) else 'Not connected ❌'}"
    )


def main_kb(uid):
    s = db.get_settings(uid)

    def b(t, a):
        return Btn(t, callback_data=f"st|{uid}|{a}")

    rows = [
        [b("🎛 Encode Settings", "enc")],
        [b(("🟢 " if s["swap"] else "⚪ ") + f"QUALITY NAME SWAP : {_on(s['swap'])}", "swap")],
        [b("📤 Send as: " + ("Document" if s["as_doc"] else "Media"), "asdoc")],
        [b("🏷 Tag: " + s["tag"][:30], "tag")],
        [b("🗑 Auto Delete Source: " + _on(s["auto_delete"]), "autodel")],
        [b("🖼 Set Thumbnail", "thumb")],
    ]
    if thumbs.has(uid):
        rows.append([b("❌ Remove Thumbnail", "rmthumb")])
    rows.append([b("🔗 GDrive Connect", "gd")])
    if rclone.has_conf(uid):
        rows.append([b("❌ Remove GDrive", "rmgd")])
    rows.append([b("Close", "close")])
    return Markup(rows)


def enc_text(uid):
    s = db.get_settings(uid)
    return (
        "<b>Encode</b>\n"
        f"Vcodec: <code>{s['vcodec']}</code>\n"
        f"Acodec: <code>{s['acodec']}</code>\n"
        f"Abitrate: <code>{s['abitrate']}</code>\n"
        f"Crf: <code>{s['crf']}</code>\n"
        f"Preset: <code>{s['preset']}</code>\n"
        f"Res: <code>{s['res']}</code>\n"
        f"Fps: <code>{s['fps']}</code>\n"
        f"Ext: <code>{s['ext']}</code>"
    )


def enc_kb(uid):
    def b(t, a):
        return Btn(t, callback_data=f"st|{uid}|{a}")

    return Markup([
        [b("Vcodec", "opt|vcodec"), b("Acodec", "opt|acodec")],
        [b("Crf", "opt|crf"), b("Preset", "opt|preset")],
        [b("Res", "opt|res"), b("Fps", "opt|fps")],
        [b("Ext", "opt|ext"), b("Abitrate", "opt|abitrate")],
        [b("«", "menu"), b("Done", "menu"), b("Cancel", "close")],
    ])


def opt_text(uid, key):
    return f"<b>VT_{key.upper()}</b>\nCurrent: <code>{db.get_settings(uid)[key]}</code>"


def opt_kb(uid, key):
    width = 4 if key == "crf" else 2
    btns = [Btn(label, callback_data=f"st|{uid}|set|{key}|{val}") for label, val in OPTIONS[key]]
    rows = [btns[i:i + width] for i in range(0, len(btns), width)]
    rows.append([Btn("«", callback_data=f"st|{uid}|enc"), Btn("Cancel", callback_data=f"st|{uid}|close")])
    return Markup(rows)


@app.on_message(filters.command("settings"))
async def settings_cmd(client, m):
    if not m.from_user:
        return
    await m.reply_text(main_text(m.from_user.id), reply_markup=main_kb(m.from_user.id))


async def _show(cq, text, kb):
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


@app.on_callback_query(filters.regex(r"^st\|"))
async def settings_cb(client, cq):
    p = cq.data.split("|")
    uid = int(p[1])
    if cq.from_user.id != uid:
        return await cq.answer("These are not your settings. Send /settings.", show_alert=True)
    act = p[2]
    AWAIT.pop(uid, None)
    back = Markup([[Btn("« Back", callback_data=f"st|{uid}|menu")]])

    if act == "close":
        await cq.message.delete()
        return await cq.answer()
    if act == "menu":
        await _show(cq, main_text(uid), main_kb(uid))
    elif act == "enc":
        await _show(cq, enc_text(uid), enc_kb(uid))
    elif act == "opt":
        await _show(cq, opt_text(uid, p[3]), opt_kb(uid, p[3]))
    elif act == "set":
        key, val = p[3], p[4]
        if key in OPTIONS:
            db.set_setting(uid, key, "" if val == "-" else val)
        await _show(cq, enc_text(uid), enc_kb(uid))
    elif act in ("swap", "asdoc", "autodel"):
        key = {"swap": "swap", "asdoc": "as_doc", "autodel": "auto_delete"}[act]
        db.set_setting(uid, key, not db.get_settings(uid)[key])
        await _show(cq, main_text(uid), main_kb(uid))
    elif act == "tag":
        AWAIT[uid] = ("tag", time.time(), 0)
        cur = html.escape(db.get_settings(uid)["tag"])
        await _show(cq, "🏷 <b>Tag</b>\n\nCurrent: <code>" + cur + "</code>\n\n"
                        "Send the new tag as a message. It is put in front of the output folder and "
                        "zip names, for example <code>" + cur + " NAME S01 720p</code>.",
                    Markup([[Btn("Reset to default", callback_data=f"st|{uid}|tagreset")],
                            [Btn("« Back", callback_data=f"st|{uid}|menu")]]))
    elif act == "tagreset":
        db.set_setting(uid, "tag", DEFAULT_TAG)
        await _show(cq, main_text(uid), main_kb(uid))
    elif act == "thumb":
        AWAIT[uid] = ("thumb", time.time(), 0)
        await _show(cq, "🖼 <b>Set Thumbnail</b>\n\nSend a photo, an image file, or a direct image link "
                        "within 2 minutes.", back)
    elif act == "rmthumb":
        thumbs.remove(uid)
        await _show(cq, main_text(uid), main_kb(uid))
    elif act == "gd":
        rows = [[Btn("📎 Send rclone.conf now", callback_data=f"st|{uid}|rcw")],
                [Btn("« Back", callback_data=f"st|{uid}|menu")]]
        await _show(cq, GD_HELP, Markup(rows))
    elif act == "rcw":
        if cq.message.chat.type != ChatType.PRIVATE:
            return await cq.answer("Open the bot in a private chat to send your config.", show_alert=True)
        AWAIT[uid] = ("rclone", time.time(), 0)
        await _show(cq, "📎 <b>Send your rclone.conf file now.</b>\nNo caption is needed.", back)
    elif act == "rmgd":
        rclone.remove_conf(uid)
        await _show(cq, main_text(uid), main_kb(uid))
    await cq.answer()


@app.on_message(filters.photo | filters.document | filters.text, group=1)
async def await_input(client, m):
    """Receives the next message after a prompt (thumbnail, tag or rclone.conf)."""
    if not m.from_user or m.from_user.id not in AWAIT:
        return
    uid = m.from_user.id
    kind, started, trigger = AWAIT[uid]
    if m.id == trigger:
        return  # the command message that opened the prompt
    if time.time() - started > 300 or (kind != "rclone" and time.time() - started > 120):
        AWAIT.pop(uid, None)
        return
    text = (m.text or "").strip()
    if text.startswith("/"):
        AWAIT.pop(uid, None)
        return

    if kind == "tag":
        tag = re.sub(r"[\x00-\x1f/\\]", "", text)[:50].strip()
        if not tag:
            return await m.reply_text("Please send the tag as text.")
        db.set_setting(uid, "tag", tag)
        AWAIT.pop(uid, None)
        return await m.reply_text(f"✅ Tag set to <code>{html.escape(tag)}</code>.")

    if kind == "rclone":
        data = None
        if m.document:
            data = await _read_conf_document(client, m.document, uid)
        elif "type" in text and "drive" in text and "[" in text:
            data = text.encode()
        if not data:
            return await m.reply_text("Please send the rclone.conf file (or paste its text).")
        AWAIT.pop(uid, None)
        return await _save_rclone(client, m, data)

    # thumbnail
    os.makedirs(config.WORK_DIR, exist_ok=True)
    base = os.path.join(config.WORK_DIR, f"th_{uid}")
    src = None
    if m.photo:
        src = await client.download_media(m.photo, file_name=base + ".img")
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        src = await client.download_media(m.document, file_name=base + ".img")
    elif text:
        found = re.search(r"https?://\S+", text)
        if found:
            src = await thumbs.fetch(found.group(0), config.WORK_DIR)
    if not src:
        return await m.reply_text("Please send a photo, an image file, or a direct image link.")
    ok = await thumbs.make(src, thumbs.path_for(uid))
    try:
        os.remove(src)
    except OSError:
        pass
    if not ok:
        return await m.reply_text("❌ Could not use that image. Try another one.")
    AWAIT.pop(uid, None)
    await m.reply_text("✅ Thumbnail saved. It will be used for your Telegram uploads.")
