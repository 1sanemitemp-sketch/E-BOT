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
from client import app
from jobs import JOBS, NO_CONF, Job, job_kb, run_job
from store import db
from utils import human_time, stats, sysline

TASKS = set()

OPTIONS = {
    "vcodec": [("libx264", "libx264"), ("libx265", "libx265"), ("libvpx-vp9", "libvpx-vp9"),
               ("libaom-av1", "libaom-av1"), ("copy", "copy")],
    "acodec": [("aac", "aac"), ("ac3", "ac3"), ("opus", "opus"), ("libmp3lame", "libmp3lame"),
               ("copy", "copy")],
    "crf": [(v, v) for v in ("16", "18", "20", "23", "26", "28", "30", "35")],
    "preset": [(v, v) for v in ("ultrafast", "superfast", "veryfast", "fast", "medium", "slow", "veryslow")],
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
    "<code>-n NewName.mkv</code> - output file name (single file only)\n"
    "Example: <code>/tenc https://site.com/ep1.mkv -n ANIME.mkv</code>\n\n"
    "/settings - encode settings, quality name swap, send as media/document, GDrive\n"
    "/rclone - save your rclone.conf (send the file in a private chat with /rclone as caption)\n"
    "/status - running jobs\n"
    "/cancel <code>id</code> - cancel a job\n"
)

USAGE = ("Please send a link.\n<code>/genc link</code>\n<code>/tenc link</code>\n"
         "<code>/tenc direct_link -n ANIME.mkv</code>")

GD_HELP = (
    "🔗 <b>GDrive Connect (rclone)</b>\n\n"
    "1. Install rclone on a PC or in Termux (<code>pkg install rclone</code>, or rclone.org).\n"
    "2. Run <code>rclone config</code> → New remote → type <b>drive</b> → scope <b>1</b> (full access) "
    "→ log in with the browser and pick your Gmail account.\n"
    "3. Run <code>rclone config file</code> to see where the config is stored. Send that "
    "<b>rclone.conf</b> file to this bot in a <b>private chat</b> with <code>/rclone</code> as the caption.\n"
    "4. Once connected, use <code>/genc folder_link</code> or <code>/tenc folder_link</code>.\n\n"
    "⚠️ Never send the config in a group. It works like a password for your GDrive."
)


# ------------------------------------------------------------------ gate
@app.on_message(filters.all, group=-1)
async def gate_msg(client, m):
    if not m.from_user:
        return
    rec = db.touch(m.from_user)
    if m.from_user.id == config.OWNER_ID:
        return
    if rec["status"] == "banned":
        if m.chat.type == ChatType.PRIVATE:
            await m.reply_text("🚫 You are banned from using this bot.")
        raise StopPropagation
    if rec["status"] == "muted":
        raise StopPropagation


@app.on_callback_query(group=-1)
async def gate_cb(client, cq):
    rec = db.touch(cq.from_user)
    if cq.from_user.id == config.OWNER_ID:
        return
    if rec["status"] in ("banned", "muted"):
        await cq.answer("No access.", show_alert=True)
        raise StopPropagation


# ------------------------------------------------------------------ basic
@app.on_message(filters.command("start"))
async def start_cmd(client, m):
    await m.reply_text(START_TEXT)


@app.on_message(filters.command("help"))
async def help_cmd(client, m):
    text = HELP_TEXT
    if m.from_user and m.from_user.id == config.OWNER_ID:
        text += "\n<b>Owner</b>\n/panel - user control panel\n/restart - bot restart"
    await m.reply_text(text)


@app.on_message(filters.command("restart"))
async def restart_cmd(client, m):
    if not m.from_user or m.from_user.id != config.OWNER_ID:
        return
    await m.reply_text("🔄 Restarting...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ------------------------------------------------------------------ encode commands
def parse_args(m):
    text = m.text or ""
    parts = text.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    if not rest.strip() and m.reply_to_message:
        rest = m.reply_to_message.text or m.reply_to_message.caption or ""
    name = None
    mm = re.search(r"(?:^|\s)-n\s+(.+)$", rest, re.S)
    if mm:
        lines = mm.group(1).strip().splitlines()
        name = lines[0].strip() if lines else None
        rest = rest[:mm.start()]
    lm = re.search(r"https?://\S+", rest)
    return (lm.group(0) if lm else None), (name or None)


@app.on_message(filters.command(["genc", "tenc", "enc"]))
async def enc_cmd(client, m):
    if not m.from_user:
        return
    kind = "tenc" if m.command[0].lower() == "tenc" else "genc"
    link, name = parse_args(m)
    if not link:
        return await m.reply_text(USAGE)
    uid = m.from_user.id
    if (kind == "genc" or rclone.gdrive_id(link)) and not rclone.has_conf(uid):
        return await m.reply_text(NO_CONF)
    job = Job(kind, m.from_user, m.chat.id, link, name, db.get_settings(uid))
    JOBS[job.id] = job
    db.bump_jobs(uid)
    task = asyncio.create_task(run_job(client, job))
    job.task = task
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


@app.on_message(filters.command("status"))
async def status_cmd(client, m):
    if not m.from_user:
        return
    uid = m.from_user.id
    admin = db.is_admin(uid)
    jobs = [j for j in JOBS.values() if admin or j.user_id == uid]
    if not jobs:
        return await m.reply_text("No active jobs.\n\n<code>" + sysline() + "</code>")
    lines = []
    for j in jobs:
        pct = (j.done / j.total * 100) if j.total else 0
        lines.append(f"• <code>{j.id}</code> #{j.kind} {j.stage} {pct:.0f}% "
                     f"[{j.file_idx}/{j.file_total or '-'}] {human_time(time.time() - j.started)}")
    await m.reply_text("<b>Active jobs</b>\n" + "\n".join(lines) +
                       "\n\n<code>" + sysline() + "</code>")


async def _cancel(m, job):
    admin = db.is_admin(m.from_user.id)
    if not job or not (admin or job.user_id == m.from_user.id):
        return await m.reply_text("Job not found.")
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
        return await m.reply_text("Please provide a job id: <code>/cancel id</code> (see /status).")
    await _cancel(m, job)


@app.on_message(filters.regex(r"^/cancel_([0-9a-f]{8})"))
async def cancel_link_cmd(client, m):
    if m.from_user:
        await _cancel(m, JOBS.get(m.matches[0].group(1)))


@app.on_callback_query(filters.regex(r"^jc\|"))
async def cancel_cb(client, cq):
    job = JOBS.get(cq.data.split("|")[1])
    if not job:
        return await cq.answer("This job has already finished.", show_alert=True)
    if not (db.is_admin(cq.from_user.id) or job.user_id == cq.from_user.id):
        return await cq.answer("This is not your job.", show_alert=True)
    job.cancel()
    await cq.answer("Cancelling...")


@app.on_callback_query(filters.regex(r"^jr\|"))
async def refresh_cb(client, cq):
    job = JOBS.get(cq.data.split("|")[1])
    if not job:
        return await cq.answer("This job has already finished.", show_alert=True)
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
        f"Running jobs: {len(JOBS)}",
        show_alert=True,
    )


# ------------------------------------------------------------------ rclone.conf
def _rc_filter(_, __, m):
    return (m.caption or m.text or "").strip().lower().startswith("/rclone")


@app.on_message(filters.create(_rc_filter))
async def rclone_cmd(client, m):
    if not m.from_user:
        return
    if m.chat.type != ChatType.PRIVATE:
        return await m.reply_text("⚠️ Use /rclone in a private chat only.")
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if not doc:
        return await m.reply_text(GD_HELP)
    if doc.file_size and doc.file_size > 200 * 1024:
        return await m.reply_text("This file is too large to be an rclone.conf.")
    os.makedirs(config.WORK_DIR, exist_ok=True)
    tmp = os.path.join(config.WORK_DIR, f"rc_{m.from_user.id}.conf")
    path = await client.download_media(doc, file_name=tmp)
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
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


# ------------------------------------------------------------------ settings UI
def _on(v):
    return "ON" if v else "OFF"


def main_text(uid):
    s = db.get_settings(uid)
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"<b>Encode:</b> {s['vcodec']} / {s['acodec']} / CRF {s['crf']} / {s['preset']} / "
        f"{s['res'] or 'Auto'} / {s['ext']}\n"
        f"<b>Send as:</b> {'Document' if s['as_doc'] else 'Media'}\n"
        f"<b>Quality Name Swap:</b> {_on(s['swap'])}\n"
        f"<b>Auto Delete Source:</b> {_on(s['auto_delete'])}\n"
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
        [b("🗑 Auto Delete Source: " + _on(s["auto_delete"]), "autodel")],
        [b("🔗 GDrive Connect", "gd")],
    ]
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
        [b("Ext", "opt|ext"), b("Done", "menu")],
        [b("«", "menu"), b("Cancel", "close")],
    ])


def opt_text(uid, key):
    return f"<b>VT_{key.upper()}</b>\nCurrent: <code>{db.get_settings(uid)[key]}</code>"


def opt_kb(uid, key):
    btns = [Btn(label, callback_data=f"st|{uid}|set|{key}|{val}") for label, val in OPTIONS[key]]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
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
    elif act == "gd":
        await _show(cq, GD_HELP, Markup([[Btn("«", callback_data=f"st|{uid}|menu")]]))
    elif act == "rmgd":
        rclone.remove_conf(uid)
        await _show(cq, main_text(uid), main_kb(uid))
    await cq.answer()
