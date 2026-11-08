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
from pyrogram.enums import ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import autorename
import client as botclient
import config
import ddl
import encoder
import logchannel
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
        self.limit = max(0, int(limit))  # 0 = unlimited
        self.active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.limit == 0 or self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def set_limit(self, n):
        async with self._cond:
            self.limit = max(0, int(n))
            self._cond.notify_all()


LIMITER = Limiter(db.get_bot("max_concurrent"))

NO_CONF = ("GDrive is not connected. In a private chat send /rclone and then your rclone.conf file, "
           "or open /settings and tap GDrive Connect.")


class Job:
    def __init__(self, kind, user, chat_id, link, custom_name, settings,
                 thumb_url=None, extract=False, zip_out=False, plain=False, origin=None, cmd=""):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.cmd = cmd
        self.user_id = user.id
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id)
        self.mention = f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'
        self.task_by = f"@{user.username}" if getattr(user, "username", None) else self.mention
        self.user_label = logchannel.who(user)
        self.chat_id = chat_id  # where the files and the result go (the user's PM for a group task)
        self.origin = origin  # the chat the command was sent in (a group, or the user's PM)
        self.origin_id = origin.id if origin is not None else chat_id
        self.from_group = origin is not None and origin.type != ChatType.PRIVATE
        self.out_bytes = 0
        self.link = link
        self.custom_name = custom_name
        self.settings = settings
        self.thumb_url = thumb_url
        self.extract = extract
        self.zip_out = zip_out
        self.plain = plain  # upload without encoding (/gup, /tup)
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
        self.used_names = set()
        self.external_speed = False  # the speed comes from the tool (ffmpeg), not from our own samples
        self.deleted_files = 0
        self.deleted_folder = None

    # ---- state helpers ----
    def set_stage(self, stage, engine, unit="bytes"):
        self.stage, self.engine, self.unit = stage, engine, unit
        self.done = self.total = 0
        self.speed = 0.0
        self._lt = 0.0
        self._ld = 0
        self.external_speed = False
        self.stage_started = time.time()

    def set_progress(self, done, total=0):
        now = time.monotonic()
        self.done = done
        if total:
            self.total = total
        if self.external_speed:
            return
        if self._lt == 0.0:
            self._lt, self._ld = now, done
            return
        dt = now - self._lt
        if dt >= 1.0:
            inst = max(0.0, (done - self._ld) / dt)
            self.speed = inst if not self.speed else self.speed * 0.6 + inst * 0.4
            self._lt, self._ld = now, done

    def add_out(self, size):
        """Count the bytes that were delivered (shown on the group card and in the log)."""
        self.out_bytes += int(size or 0)

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

    def render_block(self, number=None):
        pct = min(100.0, (self.done / self.total * 100) if self.total else 0.0)
        if self.unit == "time":
            proc = f"{human_time(self.done)} of {human_time(self.total)}"
            spd = f"{self.speed:.2f}x"
        else:
            proc = f"{human_size(self.done)} of {human_size(self.total)}"
            spd = f"{human_size(self.speed)}/s"
        elapsed = time.time() - self.stage_started
        time_line = human_time(elapsed)
        if self.speed >= 0.01 and self.total:
            eta = max(0.0, (self.total - self.done) / self.speed)
            if eta < 99 * 3600:  # an absurd ETA means the speed is not known yet
                time_line = f"{human_time(elapsed)} of {human_time(elapsed + eta)} ( {human_time(eta)} )"
        status = {"queued": "Queued", "resolving": "Resolving", "downloading": "Download",
                  "extracting": "Extract", "encoding": "Encode", "zipping": "Zip", "splitting": "Split",
                  "uploading": "Upload"}.get(self.stage, self.stage)
        in_mode, out_mode = self.modes()
        files = f"{self.file_idx}/{self.file_total}" if self.file_total else "-"
        index = f"{number}. " if number else ""
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
        number = list(JOBS).index(self.id) + 1 if self.id in JOBS else 1
        return f"{self.render_block(number)}\n\n⌬ <b>Bot Stats</b>\n{bot_stats_text()}"

    def modes(self):
        in_mode = "#GDrive" if self.src_gd else "#DDL"
        if self.kind == "genc":
            out_mode = "#GDrive"
        else:
            out_mode = "#Telegram (Document)" if self.settings["as_doc"] else "#Telegram (Media)"
        return in_mode, out_mode

    def group_card(self):
        """The message a group sees when a task is done: the files went to the user's PM."""
        in_mode, out_mode = self.modes()
        title = html.escape((self.dest or self.file_name or "Task")[:100])
        what = "Result link has" if self.kind == "genc" else "File(s) have"
        return (
            f"<b>{title}</b>\n│\n"
            f"┟ <b>Task Size</b> → {human_size(self.out_bytes)}\n"
            f"┠ <b>Time Taken</b> → {human_time(time.time() - self.started)}\n"
            f"┠ <b>In Mode</b> → {in_mode}\n"
            f"┠ <b>Out Mode</b> → {out_mode}\n"
            f"┠ <b>Total Files</b> → {self.ok}\n"
            f"┖ <b>Task By</b> → {self.task_by}\n\n"
            f"〶 <b>Action Performed :</b>\n"
            f"⋗ {what} been sent to User PM"
        )

    def result_markup(self):
        """A button with the folder/file link, shown under the completion message."""
        if not self.dest_link:
            return None
        label = "📁 Folder Link" if self.dest_is_dir else "📄 File Link"
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=self.dest_link)]])

    def summary(self):
        title = "✅ <b>Completed</b>" if not self.failed else "⚠️ <b>Completed with errors</b>"
        lines = [self.header(title), f"Files done: {self.ok}/{self.file_total}"]
        if self.kind == "genc" and self.dest:
            lines.append(f"GDrive folder: <code>{html.escape(self.dest)}</code>")
        if self.deleted_folder:
            lines.append(f"🗑 Source folder deleted: <code>{html.escape(self.deleted_folder)}</code>")
        if self.deleted_files:
            lines.append(f"🗑 Source files deleted: {self.deleted_files}")
        for n, e in self.failed[:10]:
            lines.append(f"• <code>{html.escape(n)}</code>: {html.escape(e)}")
        for note in self.notes:
            lines.append(f"ℹ️ {html.escape(note)}")
        lines.append(f"Time: {human_time(time.time() - self.started)}")
        return "\n".join(lines)


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
            if job.plain:
                files = [f for f in files if not is_junk(f["Name"])]
            else:
                files = [f for f in files
                         if os.path.splitext(f["Name"])[1].lower() in encoder.VIDEO_EXT]
            files.sort(key=lambda f: natural_key(f["Path"]))
            ctx["items"] = [{"name": f["Name"], "size": f.get("Size", 0), "path": f["Path"],
                             "id": f.get("ID")} for f in files]
            if not ctx["items"]:
                raise UserError("No files found in this folder." if job.plain
                                else "No video files found in this folder.")
            ctx["source_name"] = folder_name
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


def unique_name(job, name, scope=""):
    """Avoid two output files with the same name (archives often repeat names in sub folders)."""
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while f"{scope}/{candidate}".lower() in job.used_names:
        n += 1
        candidate = f"{base} ({n}){ext}"
    job.used_names.add(f"{scope}/{candidate}".lower())
    return candidate


def is_junk(name):
    return name.startswith(".") or name.lower() in ("thumbs.db", "desktop.ini")


def find_files(root):
    found = []
    for base, _, files in os.walk(root):
        if "__MACOSX" in base:
            continue
        found += [os.path.join(base, f) for f in files if not is_junk(f)]
    found.sort(key=lambda p: natural_key(os.path.relpath(p, root)))
    return found


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
    job.add_out(size)
    if job.cancelled:
        raise Cancelled()


async def deliver_file(client, job, ctx, path, thumb, rel_dir=""):
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
            dest = "/".join(x for x in (ctx.get("dest_dir"), rel_dir) if x) or None
            await rclone.upload(job, ctx["remote"], part, dest, ctx["parent_id"])
            job.add_out(job.total)
            ctx.setdefault("uploaded", []).append(name)
        else:
            await send_telegram(client, job, part, name, thumb, None, force_doc=True)
        os.remove(part)


async def deliver_plain(client, job, ctx, path, rel_dir=""):
    """Upload any file without encoding. Videos go to Telegram as media unless documents are chosen."""
    size = os.path.getsize(path)
    if (job.kind == "tenc" and not job.settings["as_doc"] and size <= config.TG_LIMIT
            and os.path.splitext(path)[1].lower() in encoder.VIDEO_EXT):
        info = await encoder.probe(path)
        if info and info["duration"] > 0:
            thumb = job.thumb or await encoder.thumbnail(path, path + ".jpg", info["duration"])
            await send_telegram(client, job, path, os.path.basename(path), thumb, info)
            os.remove(path)
            return
    await deliver_file(client, job, ctx, path, job.thumb, rel_dir)


async def passthrough(client, job, ctx, src, out_dir, use_custom, rel_dir=""):
    """Upload a file as it is (plain mode, or an archive without -e). Nothing is encoded."""
    name = os.path.basename(src)  # with -n and Auto Rename on, the file was downloaded under the -n name
    renamed = False
    if (job.settings.get("auto_rename") and not is_archive(src)
            and os.path.splitext(name)[1].lower() in encoder.VIDEO_EXT):
        info = await encoder.probe(src)  # Auto Rename also names plain uploads of video files
        if info:
            name = autorename.render(job.settings["auto_rename"], name, info, resolution=f"{info['height']}p",
                                     ext=os.path.splitext(name)[1],
                                     lib=autorename.lib_from("copy", info.get("vcodec")))
            renamed = True
    if not renamed and use_custom and job.custom_name:
        ext = os.path.splitext(name)[1]
        name = job.custom_name if os.path.splitext(job.custom_name)[1] else job.custom_name + ext
    name = unique_name(job, safe_name(name), rel_dir if job.kind == "genc" else "")
    ensure_dest(job, ctx, "", name)
    if job.zip_out and not is_archive(src):  # -z: collect the file for the zip
        keep = os.path.join(job.workdir, "zipout", str(len(job.zip_files)))
        os.makedirs(keep, exist_ok=True)
        dst = os.path.join(keep, name)
        shutil.move(src, dst)
        job.zip_files.append((dst, "/".join(x for x in (rel_dir, name) if x)))
        return
    dst = os.path.join(out_dir, name)
    os.replace(src, dst)
    job.file_name = name
    await deliver_plain(client, job, ctx, dst, rel_dir)


async def encode_one(client, job, ctx, src, out_dir, use_custom):
    """Encode one local video and deliver the result (upload, or queue it for -z)."""
    s = job.settings
    src_name = os.path.basename(src)
    info = await encoder.probe(src)
    if not info or info["duration"] <= 0:
        raise UserError("The video file is invalid or corrupt.")
    label = encoder.quality_label(s, info["height"])
    # With Auto Rename on, -n only names the download; the format then builds the output name from it
    custom = job.custom_name if (use_custom and not s.get("auto_rename")) else None
    out_name, ext = encoder.output_name(src_name, label, s["swap"], s["ext"], custom)
    if s.get("auto_rename") and not custom:
        out_name = autorename.render(
            s["auto_rename"], src_name, info, resolution=label, ext=ext,
            lib=autorename.lib_from(s["vcodec"], info.get("vcodec")),
            audio_default={"aac": "AAC", "ac3": "AC3", "opus": "Opus", "libmp3lame": "MP3"}.get(s["acodec"], ""),
            keep_subs=ext in ("mkv", "mp4", "mov"))
    out_name = unique_name(job, safe_name(out_name))
    out_path = os.path.join(out_dir, out_name)
    job.file_name = out_name

    job.set_stage("encoding", "FFmpeg", unit="time")
    job.total = info["duration"]
    await encoder.encode(job, src, out_path, s, ext, info["duration"], info)
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
        job.add_out(job.total)
        ctx.setdefault("uploaded", []).append(out_name)
    else:
        out_info = await encoder.probe(out_path) or info
        # A thumbnail only comes with the file when the user chose one (-t or /settings), or when the
        # file is sent as a video: a document without a chosen thumbnail gets none.
        thumb = job.thumb
        if not thumb and not s["as_doc"]:
            thumb = await encoder.thumbnail(out_path, os.path.join(out_dir, "thumb.jpg"),
                                            out_info["duration"])
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
        if await rclone.delete_id(job.user_id, ctx["remote"], item["id"]):
            job.deleted_files += 1
        else:
            job.notes.append(f"Could not delete the source: {item.get('name', item['id'])}")


async def handle_local(client, job, ctx, src, out_dir, use_custom, rel_dir=""):
    """Encode one downloaded file, or upload it as it is (plain mode, or an archive)."""
    if job.plain or is_archive(src):
        await passthrough(client, job, ctx, src, out_dir, use_custom, rel_dir)
    else:
        await encode_one(client, job, ctx, src, out_dir, use_custom)


# ---------------------------------------------------------------- pipelines
async def process_item(client, job, ctx, item, single):
    src_dir = os.path.join(job.workdir, "in")
    out_dir = os.path.join(job.workdir, "out")
    for d in (src_dir, out_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    use_custom = single and not job.zip_out
    save_as = None
    if use_custom and job.custom_name and job.settings.get("auto_rename"):
        save_as = job.custom_name  # Auto Rename reads this name, exactly like WZML-X
        if not os.path.splitext(save_as)[1] and item.get("name"):
            save_as += os.path.splitext(item["name"])[1]
    src = await download_item(job, ctx, item, src_dir, save_as=save_as)
    job.file_name = job.custom_name if (use_custom and job.custom_name) else os.path.basename(src)
    rel_dir = os.path.dirname(item["path"]) if (job.plain and item.get("path")) else ""
    await handle_local(client, job, ctx, src, out_dir, use_custom, rel_dir)
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

    if not is_archive(archive):
        # -e on a file that is not an archive: handle it like a normal single file
        await handle_local(client, job, ctx, archive, out_dir, not job.zip_out)
        job.ok += 1
        if job.settings.get("auto_delete"):
            job.pending_delete.append(item)
        return

    await extract_archive(job, archive, ex_dir)
    os.remove(archive)
    files = find_files(ex_dir) if job.plain else find_videos(ex_dir)
    if not files:
        raise UserError("No files were found inside the archive." if job.plain
                        else "No video files were found inside the archive.")
    ctx["base_name"] = os.path.splitext(os.path.basename(archive))[0]
    ctx["use_folder"] = len(files) > 1 and not job.zip_out
    job.file_total = len(files)
    for i, path in enumerate(files, 1):
        if job.cancelled:
            raise Cancelled()
        job.file_idx = i
        job.file_name = os.path.basename(path)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir)
        rel_dir = os.path.dirname(os.path.relpath(path, ex_dir)) if job.plain else ""
        try:
            await handle_local(client, job, ctx, path, out_dir, False, rel_dir)
            job.ok += 1
        except Cancelled:
            raise
        except Exception as e:
            if len(files) == 1:
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

    if job.zip_out and job.zip_files:
        await finalize_zip(client, job, ctx)
    if job.pending_delete:
        await delete_sources(job, ctx, job.pending_delete)
    if (job.settings.get("auto_delete") and ctx.get("is_folder") and not job.failed
            and job.ok == len(items)):
        # every file of the source folder was handled, so the folder itself is removed too
        if await rclone.delete_id(job.user_id, ctx["remote"], ctx["root_id"]):
            job.deleted_folder = ctx.get("source_name") or "Folder"
        else:
            job.notes.append("Could not delete the source folder.")
    if job.kind == "genc":
        target = ctx.get("dest_dir")
        if not target:
            uploaded = ctx.get("uploaded", [])
            target = uploaded[0] if len(uploaded) == 1 else None
        if target:
            job.dest_link, job.dest_is_dir = await rclone.path_link(
                job.user_id, ctx["remote"], target, ctx["parent_id"])


def _open_pm_markup():
    if not botclient.BOT_USERNAME:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📩 Open PM", url=f"https://t.me/{botclient.BOT_USERNAME}")]])


async def _finish(client, job, final, success):
    """Send the result to the user (PM for a group task). A group only gets a short card."""
    pm_ok = True
    try:
        await client.send_message(job.chat_id, final, reply_markup=job.result_markup())
    except Exception:
        pm_ok = False
        log.warning("could not send the final message", exc_info=True)
    if not job.from_group:
        return
    try:
        if not pm_ok:
            text = (job.header("⚠️ <b>Task finished, but I could not message you.</b>")
                    + "\nOpen the bot in PM, press Start, and run the task again.")
            await client.send_message(job.origin_id, text, reply_markup=_open_pm_markup())
        elif success:
            await client.send_message(job.origin_id, job.group_card(), reply_markup=_open_pm_markup())
        else:
            await client.send_message(job.origin_id, final)
    except Exception:
        log.warning("could not send the group message", exc_info=True)


def _log_started(job):
    flags = " ".join(x for x in ("-e" if job.extract else "", "-z" if job.zip_out else "",
                                 f"-n {job.custom_name}" if job.custom_name else "",
                                 "-t" if job.thumb_url else "") if x)
    logchannel.event(
        "TaskStarted",
        f"Task: <code>{job.id}</code> · /{job.cmd or job.kind}",
        f"User: {job.user_label}",
        f"Chat: {logchannel.where(job.origin)}",
        f"Link: <code>{html.escape(job.link[:300])}</code>",
        f"Flags: <code>{html.escape(flags)}</code>" if flags else "",
        f"Encode: {job.settings['vcodec']} / CRF {job.settings['crf']} / {job.settings['preset']}"
        if not job.plain else "Mode: plain upload (no encode)")


def _log_finished(job, state, error=""):
    logchannel.event(
        f"Task{state}",
        f"Task: <code>{job.id}</code> · /{job.cmd or job.kind}",
        f"User: {job.user_label}",
        f"Chat: {logchannel.where(job.origin)}",
        f"Name: <code>{html.escape((job.dest or job.file_name or '-')[:150])}</code>",
        f"Files: {job.ok}/{job.file_total} · Size: {human_size(job.out_bytes)} · "
        f"Time: {human_time(time.time() - job.started)}",
        f"Link: {job.dest_link}" if job.dest_link else "",
        f"Error: <code>{html.escape(error[:400])}</code>" if error else "")


async def run_job(client, job):
    import board  # imported here because board itself needs jobs.JOBS
    try:
        await board.ensure(client, job.origin_id)  # every chat has one live status message
    except Exception:
        log.warning("could not open the status board", exc_info=True)
    _log_started(job)
    final = job.header("❌ <b>Stopped.</b>")
    success, state, error = False, "Stopped", ""
    try:
        if db.is_admin(job.user_id):  # admins have no limit: they never wait in the queue
            if job.cancelled:
                raise Cancelled()
            await pipeline(client, job)
        else:
            async with LIMITER:
                if job.cancelled:
                    raise Cancelled()
                await pipeline(client, job)
        final = job.summary()
        success = job.ok > 0
        state = "Completed" if not job.failed else "CompletedWithErrors"
        if job.failed:
            error = "; ".join(f"{n}: {e}" for n, e in job.failed[:3])
    except (Cancelled, asyncio.CancelledError):
        final = job.header("❌ <b>Cancelled.</b>")
        state = "Cancelled"
    except UserError as e:
        final = job.header("❌ <b>Failed</b>") + f"\n{html.escape(str(e))}"
        state, error = "Failed", str(e)
    except Exception as e:
        log.exception("job failed")
        final = job.header("❌ <b>Error</b>") + f"\n<code>{html.escape(str(e)[:500])}</code>"
        state, error = "Error", str(e)
    finally:
        JOBS.pop(job.id, None)
        shutil.rmtree(job.workdir, ignore_errors=True)
    _log_finished(job, state, error)
    await _finish(client, job, final, success)
