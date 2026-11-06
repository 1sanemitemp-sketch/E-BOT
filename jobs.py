import asyncio
import html
import logging
import os
import re
import shutil
import time
import uuid
import zipfile

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import ddl
import encoder
import rclone
import thumbs
from store import db
from utils import (Cancelled, UserError, bar, bot_stats_text, human_size, human_time,
                   is_archive, natural_key, run_proc, safe_name, split_file, watch_dir)

log = logging.getLogger("jobs")
JOBS = {}


class Limiter:
    """Async limiter whose limit can be changed while the bot is running."""

    def __init__(self, limit):
        self.limit = max(1, int(limit))
        self.active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def set_limit(self, n):
        async with self._cond:
            self.limit = max(1, int(n))
            self._cond.notify_all()


LIMITER = Limiter(db.get_bot("max_concurrent"))

NO_CONF = ("GDrive is not connected. Open /settings and tap GDrive Connect, "
           "or send your rclone.conf in a private chat with /rclone as the caption.")


def job_kb(job_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data=f"jt|{job_id}"),
         InlineKeyboardButton("♻️ Refresh", callback_data=f"jr|{job_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"jc|{job_id}")],
    ])


class Job:
    def __init__(self, kind, user, chat_id, link, custom_name, settings,
                 thumb_url=None, extract=False, zip_out=False):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.user_id = user.id
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id)
        self.mention = f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'
        self.chat_id = chat_id
        self.link = link
        self.custom_name = custom_name
        self.settings = settings
        self.thumb_url = thumb_url
        self.extract = extract
        self.zip_out = zip_out
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
        self.notes = []
        self.dest = None
        self.dest_link = None
        self.dest_is_dir = False
        self.thumb = None
        self.zip_files = []
        self.pending_delete = []

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
    def header(self, title):
        return f"{title}\nTask By {self.mention} ( <code>#ID{self.user_id}</code> )"

    def render_block(self):
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
                  "extracting": "Extract", "encoding": "Encode", "zipping": "Zip", "splitting": "Split",
                  "uploading": "Upload"}.get(self.stage, self.stage)
        in_mode = "#GDrive" if self.src_gd else "#DDL"
        if self.kind == "genc":
            out_mode = "#GDrive"
        else:
            out_mode = "#Telegram (Document)" if self.settings["as_doc"] else "#Telegram (Media)"
        files = f"{self.file_idx}/{self.file_total}" if self.file_total else "-"
        index = f"{self.file_idx}. " if self.file_idx else ""
        name = html.escape(self.file_name[:90]) or "Preparing..."
        return (
            f"<b>{index}{name}</b>\n\n"
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
            f"└ <b>Stop</b> → /cancel_{self.id}"
        )

    def render(self):
        return f"{self.render_block()}\n\n⌬ <b>Bot Stats</b>\n{bot_stats_text()}"

    def summary(self):
        title = "✅ <b>Completed</b>" if not self.failed else "⚠️ <b>Completed with errors</b>"
        lines = [self.header(title), f"Files done: {self.ok}/{self.file_total}"]
        if self.kind == "genc" and self.dest:
            lines.append(f"GDrive folder: <code>{html.escape(self.dest)}</code>")
        if self.dest_link:
            label = "Folder link" if self.dest_is_dir else "File link"
            lines.append(f'{label}: <a href="{self.dest_link}">Link</a>')
        for n, e in self.failed[:10]:
            lines.append(f"• <code>{html.escape(n)}</code>: {html.escape(e)}")
        for note in self.notes:
            lines.append(f"ℹ️ {html.escape(note)}")
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

    ctx = {"remote": remote, "root_id": fid, "parent_id": None, "is_folder": False,
           "use_folder": False, "base_name": None, "items": []}

    if fid:
        job.set_stage("resolving", "Rclone")
        meta = await rclone.drive_meta(uid, remote, fid)
        is_folder = None
        folder_name = None
        if meta:
            is_folder = meta.get("mimeType") == "application/vnd.google-apps.folder"
            parents = meta.get("parents") or []
            ctx["parent_id"] = parents[0] if parents else None
            folder_name = meta.get("name")
        if is_folder is None:
            try:
                await rclone.list_folder(uid, remote, fid)
                is_folder = True
                folder_name = "Folder"
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
            ctx["items"] = [{"name": f["Name"], "size": f.get("Size", 0), "path": f["Path"],
                             "id": f.get("ID")} for f in files]
            if not ctx["items"]:
                raise UserError("No video files found in this folder.")
            ctx["is_folder"] = True
            ctx["use_folder"] = True
            ctx["base_name"] = job.custom_name or folder_name
        else:
            if not meta:
                raise UserError("Cannot access this file. Check the link and its permissions.")
            ctx["items"] = [{"name": meta["name"], "size": int(meta.get("size") or 0),
                             "gid": fid, "id": fid}]
    elif job.link.lower().startswith(("http://", "https://")):
        ctx["items"] = [{"url": job.link}]
    else:
        raise UserError("Please send a valid GDrive link or a direct download link.")
    return ctx


async def prepare_thumb(job):
    job.thumb = None
    if job.kind != "tenc":
        return
    if job.thumb_url:
        raw = await thumbs.fetch(job.thumb_url, job.workdir)
        dst = os.path.join(job.workdir, "thumb.jpg")
        if not raw or not await thumbs.make(raw, dst):
            raise UserError("Could not use the thumbnail link. Send a direct link to an image.")
        job.thumb = dst
    elif thumbs.has(job.user_id):
        job.thumb = thumbs.path_for(job.user_id)


# ---------------------------------------------------------------- steps
async def download_item(job, ctx, item, dest_dir, save_as=None):
    if item.get("url"):
        job.set_stage("downloading", "Aiohttp")
        path = await ddl.download(item["url"], dest_dir, job, save_as)
    else:
        job.set_stage("downloading", "Rclone")
        job.total = item.get("size", 0)
        path = os.path.join(dest_dir, safe_name(save_as or item["name"]))
        if item.get("path"):
            await rclone.download_path(job, ctx["remote"], ctx["root_id"], item["path"], path, job.total)
        else:
            await rclone.download_id(job, ctx["remote"], item["gid"], path, job.total)
    if not os.path.exists(path):
        raise UserError("Download failed.")
    return path


async def _archive_size(path):
    proc = await asyncio.create_subprocess_exec(
        "7z", "l", "-slt", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return sum(int(x) for x in re.findall(r"^Size = (\d+)$", out.decode(errors="ignore"), re.M))


async def extract_archive(job, archive, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    job.set_stage("extracting", "7-Zip")
    job.total = await _archive_size(archive)
    watcher = asyncio.create_task(watch_dir(out_dir, job.total, job))
    try:
        rc, err = await run_proc(["7z", "x", "-y", f"-o{out_dir}", archive], job)
    finally:
        watcher.cancel()
    if job.cancelled:
        raise Cancelled()
    if rc != 0:
        raise UserError(f"Extraction failed: {err[-200:] or 'unsupported or corrupt archive'}")


def find_videos(root):
    found = []
    for base, _, files in os.walk(root):
        if "__MACOSX" in base:
            continue
        for f in files:
            if os.path.splitext(f)[1].lower() in encoder.VIDEO_EXT:
                found.append(os.path.join(base, f))
    found.sort(key=lambda p: natural_key(os.path.relpath(p, root)))
    return found


def ensure_dest(job, ctx, label, out_name):
    """Remember the encode label and, only when a folder is used, pick its name once."""
    ctx.setdefault("label", label)
    ctx.setdefault("first_out", out_name)
    if not ctx.get("use_folder") or "dest_dir" in ctx:
        return
    base = ctx.get("base_name") or os.path.splitext(out_name)[0]
    ctx["dest_dir"] = safe_name(
        encoder.folder_name(base, label, job.settings["swap"], job.settings["tag"]))
    job.dest = ctx["dest_dir"]


def zip_title(job, ctx):
    base = ctx.get("base_name") or os.path.splitext(ctx.get("first_out", "Encodes"))[0]
    return safe_name(encoder.folder_name(base, ctx.get("label", ""), job.settings["swap"],
                                         job.settings["tag"]))


async def send_telegram(client, job, path, name, thumb, info, force_doc=False):
    size = os.path.getsize(path)
    if size > config.TG_LIMIT:
        raise UserError("The file is larger than 2GB, so a bot cannot upload it to Telegram. "
                        "Add -z to upload it as split zip parts, or use /genc.")
    job.set_stage("uploading", "Pyrogram")
    job.total = size

    def prog(cur, tot):
        if job.cancelled:
            client.stop_transmission()
        job.set_progress(cur, tot)

    cap = f"<b>{html.escape(name)}</b>"
    if force_doc or job.settings["as_doc"] or info is None:
        await client.send_document(job.chat_id, path, thumb=thumb, caption=cap, progress=prog)
    else:
        await client.send_video(job.chat_id, path, caption=cap,
                                duration=int(info["duration"]), width=info["width"],
                                height=info["height"], thumb=thumb,
                                supports_streaming=True, progress=prog)
    if job.cancelled:
        raise Cancelled()


async def deliver_file(client, job, ctx, path, thumb):
    """Upload a file as-is. Files above 2GB are split into numbered parts when needed."""
    size = os.path.getsize(path)
    split = size > config.TG_LIMIT and (job.kind == "tenc" or db.get_bot("split_gdrive"))
    files = [path]
    if split:
        job.set_stage("splitting", "Split")
        job.file_name = os.path.basename(path)
        job.total = size
        files = await asyncio.to_thread(split_file, job, path, config.TG_LIMIT)
        os.remove(path)
        job.notes.append(f"The file was split into {len(files)} parts of up to 2GB. "
                         "Join them with 7-Zip, or with cat on Linux/macOS.")
    for part in files:
        name = os.path.basename(part)
        job.file_name = name
        if job.kind == "genc":
            job.set_stage("uploading", "Rclone")
            job.total = os.path.getsize(part)
            await rclone.upload(job, ctx["remote"], part, ctx.get("dest_dir"), ctx["parent_id"])
            ctx.setdefault("uploaded", []).append(name)
        else:
            await send_telegram(client, job, part, name, thumb, None, force_doc=True)
        os.remove(part)


async def passthrough(client, job, ctx, src, out_dir, use_custom):
    """An archive without -e is not encoded, it is uploaded as it is."""
    name = os.path.basename(src)
    if use_custom and job.custom_name:
        ext = os.path.splitext(name)[1]
        name = job.custom_name if os.path.splitext(job.custom_name)[1] else job.custom_name + ext
    name = safe_name(name)
    dst = os.path.join(out_dir, name)
    os.replace(src, dst)
    job.file_name = name
    await deliver_file(client, job, ctx, dst, job.thumb)


async def encode_one(client, job, ctx, src, out_dir, use_custom):
    """Encode one local video and deliver the result (upload, or queue it for -z)."""
    s = job.settings
    src_name = os.path.basename(src)
    info = await encoder.probe(src)
    if not info or info["duration"] <= 0:
        raise UserError("The video file is invalid or corrupt.")
    label = encoder.quality_label(s, info["height"])
    out_name, ext = encoder.output_name(src_name, label, s["swap"], s["ext"],
                                        job.custom_name if use_custom else None)
    out_name = safe_name(out_name)
    out_path = os.path.join(out_dir, out_name)
    job.file_name = out_name

    job.set_stage("encoding", "FFmpeg", unit="time")
    job.total = info["duration"]
    await encoder.encode(job, src, out_path, s, ext, info["duration"])
    os.remove(src)
    ensure_dest(job, ctx, label, out_name)

    if job.zip_out:
        keep = os.path.join(job.workdir, "zipout")
        os.makedirs(keep, exist_ok=True)
        dst = os.path.join(keep, out_name)
        shutil.move(out_path, dst)
        job.zip_files.append((dst, out_name))
    elif job.kind == "genc":
        job.set_stage("uploading", "Rclone")
        job.total = os.path.getsize(out_path)
        await rclone.upload(job, ctx["remote"], out_path, ctx.get("dest_dir"), ctx["parent_id"])
        ctx.setdefault("uploaded", []).append(out_name)
    else:
        out_info = await encoder.probe(out_path) or info
        thumb = job.thumb or await encoder.thumbnail(
            out_path, os.path.join(out_dir, "thumb.jpg"), out_info["duration"])
        await send_telegram(client, job, out_path, out_name, thumb, out_info)


def _zip_files(job, files, zip_path, total):
    done = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, name in files:
            zi = zipfile.ZipInfo.from_file(path, arcname=name)
            zi.compress_type = zipfile.ZIP_STORED
            with open(path, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                while True:
                    if job.cancelled:
                        raise Cancelled()
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    job.set_progress(done, total)


async def finalize_zip(client, job, ctx):
    if not job.zip_files:
        raise UserError("There is nothing to zip.")
    zip_name = safe_name(zip_title(job, ctx) + ".zip")
    zip_path = os.path.join(job.workdir, zip_name)
    job.file_name = zip_name
    job.set_stage("zipping", "Zip")
    total = sum(os.path.getsize(p) for p, _ in job.zip_files)
    job.total = total
    await asyncio.to_thread(_zip_files, job, job.zip_files, zip_path, total)
    shutil.rmtree(os.path.join(job.workdir, "zipout"), ignore_errors=True)
    await deliver_file(client, job, ctx, zip_path, job.thumb)


async def delete_sources(job, ctx, items):
    for item in items:
        if not item.get("id") or not ctx.get("remote"):
            continue
        if not await rclone.delete_id(job.user_id, ctx["remote"], item["id"]):
            job.notes.append(f"Could not delete the source: {item.get('name', item['id'])}")


# ---------------------------------------------------------------- pipelines
async def process_item(client, job, ctx, item, single):
    src_dir = os.path.join(job.workdir, "in")
    out_dir = os.path.join(job.workdir, "out")
    for d in (src_dir, out_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    use_custom = single and not job.zip_out
    src = await download_item(job, ctx, item, src_dir)
    job.file_name = job.custom_name if (use_custom and job.custom_name) else os.path.basename(src)
    if not job.extract and is_archive(src):
        job.zip_out = False  # nothing to zip: the archive itself is uploaded
        await passthrough(client, job, ctx, src, out_dir, single)
    else:
        await encode_one(client, job, ctx, src, out_dir, use_custom)
    shutil.rmtree(out_dir, ignore_errors=True)
    if job.settings.get("auto_delete"):
        if job.zip_out:
            job.pending_delete.append(item)  # delete only after the zip is uploaded
        else:
            await delete_sources(job, ctx, [item])


async def run_extract(client, job, ctx):
    item = ctx["items"][0]
    src_dir = os.path.join(job.workdir, "in")
    ex_dir = os.path.join(job.workdir, "ex")
    out_dir = os.path.join(job.workdir, "out")
    for d in (src_dir, ex_dir, out_dir):
        os.makedirs(d, exist_ok=True)
    job.file_total, job.file_idx = 1, 1
    archive = await download_item(job, ctx, item, src_dir, save_as=job.custom_name)
    job.file_name = os.path.basename(archive)
    await extract_archive(job, archive, ex_dir)
    os.remove(archive)

    videos = find_videos(ex_dir)
    if not videos:
        raise UserError("No video files were found inside the archive.")
    ctx["base_name"] = os.path.splitext(os.path.basename(archive))[0]
    ctx["use_folder"] = len(videos) > 1 and not job.zip_out
    job.file_total = len(videos)
    for i, video in enumerate(videos, 1):
        if job.cancelled:
            raise Cancelled()
        job.file_idx = i
        job.file_name = os.path.basename(video)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir)
        try:
            await encode_one(client, job, ctx, video, out_dir, False)
            job.ok += 1
        except Cancelled:
            raise
        except Exception as e:
            if len(videos) == 1:
                raise
            log.warning("item failed: %s", job.file_name, exc_info=True)
            job.failed.append((job.file_name, str(e)[:200]))
    if job.settings.get("auto_delete") and not job.failed:
        job.pending_delete.append(item)


async def pipeline(client, job):
    os.makedirs(job.workdir, exist_ok=True)
    await prepare_thumb(job)
    ctx = await resolve_source(job)
    items = ctx["items"]
    job.file_total = len(items)
    single = len(items) == 1
    if job.zip_out:
        ctx["use_folder"] = False  # the zip is uploaded directly, without a wrapper folder

    if job.extract:
        if ctx["is_folder"] or not single:
            raise UserError("-e works with a single archive link, not a folder.")
        await run_extract(client, job, ctx)
    else:
        if job.zip_out and single and job.custom_name:
            ctx["base_name"] = os.path.splitext(job.custom_name)[0]
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

    if job.zip_out:
        await finalize_zip(client, job, ctx)
    if job.pending_delete:
        await delete_sources(job, ctx, job.pending_delete)
    if job.kind == "genc":
        target = ctx.get("dest_dir")
        if not target:
            uploaded = ctx.get("uploaded", [])
            target = uploaded[0] if len(uploaded) == 1 else None
        if target:
            job.dest_link, job.dest_is_dir = await rclone.path_link(
                job.user_id, ctx["remote"], target, ctx["parent_id"])


async def _finish(client, job, final):
    """Replace the live status message with a fresh message so the mention notifies the user."""
    try:
        await client.delete_messages(job.chat_id, job.msg.id)
    except Exception:
        pass
    try:
        await client.send_message(job.chat_id, final)
    except Exception:
        log.warning("could not send the final message", exc_info=True)


async def run_job(client, job):
    job.msg = await client.send_message(job.chat_id, job.render(), reply_markup=job_kb(job.id))
    updater = asyncio.create_task(status_loop(client, job))
    final = job.header("❌ <b>Stopped.</b>")
    try:
        async with LIMITER:
            if job.cancelled:
                raise Cancelled()
            await pipeline(client, job)
        final = job.summary()
    except (Cancelled, asyncio.CancelledError):
        final = job.header("❌ <b>Cancelled.</b>")
    except UserError as e:
        final = job.header("❌ <b>Failed</b>") + f"\n{html.escape(str(e))}"
    except Exception as e:
        log.exception("job failed")
        final = job.header("❌ <b>Error</b>") + f"\n<code>{html.escape(str(e)[:500])}</code>"
    finally:
        updater.cancel()
        JOBS.pop(job.id, None)
        shutil.rmtree(job.workdir, ignore_errors=True)
    await _finish(client, job, final)
