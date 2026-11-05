import asyncio
import html
import logging
import os
import shutil
import time
import uuid

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import ddl
import encoder
import rclone
from store import db
from utils import (Cancelled, UserError, bar, bot_stats_text, human_size, human_time,
                   natural_key, safe_name)

log = logging.getLogger("jobs")
JOBS = {}
SEM = asyncio.Semaphore(config.MAX_CONCURRENT)

NO_CONF = ("GDrive is not connected. Open /settings and tap GDrive Connect, "
           "or send your rclone.conf in a private chat with /rclone as the caption.")


def job_kb(job_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data=f"jt|{job_id}"),
         InlineKeyboardButton("♻️ Refresh", callback_data=f"jr|{job_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"jc|{job_id}")],
    ])


class Job:
    def __init__(self, kind, user, chat_id, link, custom_name, settings):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.user_id = user.id
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id)
        self.mention = f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'
        self.chat_id = chat_id
        self.link = link
        self.custom_name = custom_name
        self.settings = settings
        self.workdir = os.path.join(config.WORK_DIR, self.id)

        self.stage = "queued"
        self.engine = "-"
        self.unit = "bytes"
        self.done = 0
        self.total = 0
        self.speed = 0.0
        self._lt = 0.0
        self._ld = 0
        self.file_name = ""
        self.file_idx = 0
        self.file_total = 0
        self.started = time.time()
        self.stage_started = time.time()
        self.src_gd = bool(rclone.gdrive_id(link))

        self.cancelled = False
        self.proc = None
        self.task = None
        self.msg = None
        self.ok = 0
        self.failed = []
        self.dest = None

    # ---- state helpers ----
    def set_stage(self, stage, engine, unit="bytes"):
        self.stage, self.engine, self.unit = stage, engine, unit
        self.done = self.total = 0
        self.speed = 0.0
        self._lt = 0.0
        self._ld = 0
        self.stage_started = time.time()

    def set_progress(self, done, total=0):
        now = time.monotonic()
        self.done = done
        if total:
            self.total = total
        if self._lt == 0.0:
            self._lt, self._ld = now, done
            return
        dt = now - self._lt
        if dt >= 1.0:
            inst = max(0.0, (done - self._ld) / dt)
            self.speed = inst if not self.speed else self.speed * 0.6 + inst * 0.4
            self._lt, self._ld = now, done

    def cancel(self):
        self.cancelled = True
        if self.proc:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if self.stage == "queued" and self.task:
            self.task.cancel()

    # ---- rendering ----
    def render(self):
        pct = min(100.0, (self.done / self.total * 100) if self.total else 0.0)
        if self.unit == "time":
            proc = f"{human_time(self.done)} of {human_time(self.total)}"
            spd = f"{self.speed:.2f}x"
        else:
            proc = f"{human_size(self.done)} of {human_size(self.total)}"
            spd = f"{human_size(self.speed)}/s"
        elapsed = time.time() - self.stage_started
        if self.speed > 0 and self.total:
            eta = max(0.0, (self.total - self.done) / self.speed)
            time_line = f"{human_time(elapsed)} of {human_time(elapsed + eta)} ( {human_time(eta)} )"
        else:
            time_line = human_time(elapsed)
        status = {"queued": "Queued", "resolving": "Resolving", "downloading": "Download",
                  "encoding": "Encode", "uploading": "Upload"}.get(self.stage, self.stage)
        in_mode = "#GDrive" if self.src_gd else "#DDL"
        if self.kind == "genc":
            out_mode = "#GDrive"
        else:
            out_mode = "#Telegram (Document)" if self.settings["as_doc"] else "#Telegram (Media)"
        files = f"{self.file_idx}/{self.file_total}" if self.file_total else "-"
        index = f"{self.file_idx}. " if self.file_idx else ""
        return (
            f"<b>{index}{html.escape(self.file_name[:90]) or 'Preparing...'}</b>\n\n"
            f"Task By {self.mention} ( <code>#ID{self.user_id}</code> )\n"
            f"┌ [{bar(pct)}] {pct:.2f}%\n"
            f"├ <b>Processed</b> → {proc}\n"
            f"├ <b>Status</b> → {status}\n"
            f"├ <b>Speed</b> → {spd}\n"
            f"├ <b>Time</b> → {time_line}\n"
            f"├ <b>Engine</b> → {self.engine}\n"
            f"├ <b>File</b> → {files}\n"
            f"├ <b>In Mode</b> → {in_mode}\n"
            f"├ <b>Out Mode</b> → {out_mode}\n"
            f"└ <b>Stop</b> → /cancel_{self.id}\n\n"
            f"⌬ <b>Bot Stats</b>\n{bot_stats_text()}"
        )

    def summary(self):
        lines = ["✅ <b>Completed</b>" if not self.failed else "⚠️ <b>Completed with errors</b>",
                 f"Files done: {self.ok}/{self.file_total}"]
        if self.dest:
            lines.append(f"GDrive folder: <code>{html.escape(self.dest)}</code>")
        for n, e in self.failed[:10]:
            lines.append(f"• <code>{html.escape(n)}</code>: {html.escape(e)}")
        lines.append(f"Time: {human_time(time.time() - self.started)}")
        return "\n".join(lines)


async def status_loop(client, job):
    last = None
    while True:
        await asyncio.sleep(config.STATUS_INTERVAL)
        text = job.render()
        if text == last or job.msg is None:
            continue
        try:
            await client.edit_message_text(job.chat_id, job.msg.id, text,
                                           reply_markup=job_kb(job.id))
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep((getattr(e, "value", None) or getattr(e, "x", 5)) + 1)
        except Exception:
            log.debug("status edit failed", exc_info=True)
        last = text


# ---------------------------------------------------------------- source
async def resolve_source(job):
    uid = job.user_id
    fid = rclone.gdrive_id(job.link)
    remote = None
    if job.kind == "genc" or fid:
        if not rclone.has_conf(uid):
            raise UserError(NO_CONF)
        remote = rclone.first_drive_remote(rclone.conf_path(uid))
        if not remote:
            raise UserError("No drive remote found in your rclone.conf.")

    ctx = {"remote": remote, "root_id": fid, "parent_id": None, "folder_name": None, "items": []}

    if fid:
        job.set_stage("resolving", "Rclone")
        meta = await rclone.drive_meta(uid, remote, fid)
        is_folder = None
        if meta:
            is_folder = meta.get("mimeType") == "application/vnd.google-apps.folder"
            parents = meta.get("parents") or []
            ctx["parent_id"] = parents[0] if parents else None
            ctx["folder_name"] = meta.get("name")
        if is_folder is None:
            try:
                await rclone.list_folder(uid, remote, fid)
                is_folder = True
                ctx["folder_name"] = "Folder"
            except rclone.RcloneError:
                is_folder = False

        if is_folder:
            try:
                files = await rclone.list_folder(uid, remote, fid)
            except rclone.RcloneError as e:
                raise UserError(f"Folder access error: {e}")
            files = [f for f in files
                     if os.path.splitext(f["Name"])[1].lower() in encoder.VIDEO_EXT]
            files.sort(key=lambda f: natural_key(f["Path"]))
            ctx["items"] = [{"name": f["Name"], "size": f.get("Size", 0), "path": f["Path"]}
                            for f in files]
            if not ctx["items"]:
                raise UserError("No video files found in this folder.")
        else:
            if not meta:
                raise UserError("Cannot access this file. Check the link and its permissions.")
            ctx["folder_name"] = None
            ctx["items"] = [{"name": meta["name"], "size": int(meta.get("size") or 0), "gid": fid}]
    elif job.link.lower().startswith(("http://", "https://")):
        ctx["items"] = [{"url": job.link}]
    else:
        raise UserError("Please send a valid GDrive link or a direct download link.")
    return ctx


# ---------------------------------------------------------------- upload
async def send_telegram(client, job, path, name, thumb, info):
    size = os.path.getsize(path)
    if size > config.TG_LIMIT:
        raise UserError("The file is larger than 2GB, so a bot cannot upload it to Telegram. "
                        "Use /genc to upload it to GDrive instead.")
    job.set_stage("uploading", "Pyrogram")
    job.total = size

    def prog(cur, tot):
        if job.cancelled:
            client.stop_transmission()
        job.set_progress(cur, tot)

    cap = f"<b>{html.escape(name)}</b>"
    if job.settings["as_doc"]:
        await client.send_document(job.chat_id, path, thumb=thumb, caption=cap, progress=prog)
    else:
        await client.send_video(job.chat_id, path, caption=cap,
                                duration=int(info["duration"]), width=info["width"],
                                height=info["height"], thumb=thumb,
                                supports_streaming=True, progress=prog)
    if job.cancelled:
        raise Cancelled()


# ---------------------------------------------------------------- pipeline
async def process_item(client, job, ctx, item, single):
    s = job.settings
    src_dir = os.path.join(job.workdir, "in")
    out_dir = os.path.join(job.workdir, "out")
    for d in (src_dir, out_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)

    # 1. download
    if item.get("url"):
        job.set_stage("downloading", "Aiohttp")
        src = await ddl.download(item["url"], src_dir, job)
    else:
        job.set_stage("downloading", "Rclone")
        job.total = item.get("size", 0)
        src = os.path.join(src_dir, safe_name(item["name"]))
        if item.get("path"):
            await rclone.download_path(job, ctx["remote"], ctx["root_id"], item["path"], src, job.total)
        else:
            await rclone.download_id(job, ctx["remote"], item["gid"], src, job.total)
    if not os.path.exists(src):
        raise UserError("Download failed.")
    src_name = os.path.basename(src)
    job.file_name = job.custom_name if (single and job.custom_name) else src_name

    # 2. probe + naming
    info = await encoder.probe(src)
    if not info or info["duration"] <= 0:
        raise UserError("The video file is invalid or corrupt.")
    label = encoder.quality_label(s, info["height"])
    out_name, ext = encoder.output_name(src_name, label, s["swap"], s["ext"],
                                        job.custom_name if single else None)
    out_name = safe_name(out_name)
    out_path = os.path.join(out_dir, out_name)
    job.file_name = out_name

    # 3. encode
    job.set_stage("encoding", "FFmpeg", unit="time")
    job.total = info["duration"]
    await encoder.encode(job, src, out_path, s, ext, info["duration"])
    os.remove(src)

    # 4. upload
    if job.kind == "genc":
        if "dest_dir" not in ctx:
            if ctx.get("folder_name"):
                base = job.custom_name if (job.custom_name and not single) else ctx["folder_name"]
                ctx["dest_dir"] = f"{label} {base} [{config.TAG_NAME}]"
            else:
                ctx["dest_dir"] = f"{label} Encodes [{config.TAG_NAME}]"
            ctx["dest_dir"] = safe_name(ctx["dest_dir"])
            job.dest = ctx["dest_dir"]
        job.set_stage("uploading", "Rclone")
        job.total = os.path.getsize(out_path)
        await rclone.upload(job, ctx["remote"], out_path, ctx["dest_dir"], ctx["parent_id"])
    else:
        out_info = await encoder.probe(out_path) or info
        thumb = await encoder.thumbnail(out_path, os.path.join(out_dir, "thumb.jpg"),
                                        out_info["duration"])
        await send_telegram(client, job, out_path, out_name, thumb, out_info)

    # 5. cleanup + optional delete of GDrive original
    shutil.rmtree(out_dir, ignore_errors=True)
    if s.get("auto_delete") and item.get("path") and ctx.get("remote"):
        try:
            await rclone.delete_path(job.user_id, ctx["remote"], ctx["root_id"], item["path"])
        except Exception:
            log.warning("auto delete failed", exc_info=True)


async def pipeline(client, job):
    os.makedirs(job.workdir, exist_ok=True)
    ctx = await resolve_source(job)
    items = ctx["items"]
    job.file_total = len(items)
    single = len(items) == 1
    for idx, item in enumerate(items, 1):
        if job.cancelled:
            raise Cancelled()
        job.file_idx = idx
        job.file_name = item.get("name") or item.get("url", "")
        try:
            await process_item(client, job, ctx, item, single)
            job.ok += 1
        except Cancelled:
            raise
        except Exception as e:
            if single:
                raise
            log.warning("item failed: %s", job.file_name, exc_info=True)
            job.failed.append((job.file_name, str(e)[:200]))


async def run_job(client, job):
    job.msg = await client.send_message(job.chat_id, job.render(), reply_markup=job_kb(job.id))
    updater = asyncio.create_task(status_loop(client, job))
    final = "❌ <b>Stopped.</b>"
    try:
        async with SEM:
            if job.cancelled:
                raise Cancelled()
            await pipeline(client, job)
        final = job.summary()
    except (Cancelled, asyncio.CancelledError):
        final = "❌ <b>Cancelled.</b>"
    except UserError as e:
        final = f"❌ <b>Failed:</b> {html.escape(str(e))}"
    except Exception as e:
        log.exception("job failed")
        final = f"❌ <b>Error:</b> <code>{html.escape(str(e)[:500])}</code>"
    finally:
        updater.cancel()
        JOBS.pop(job.id, None)
        shutil.rmtree(job.workdir, ignore_errors=True)
    try:
        await client.edit_message_text(job.chat_id, job.msg.id, final)
    except Exception:
        pass
