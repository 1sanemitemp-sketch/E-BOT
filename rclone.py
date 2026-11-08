import asyncio
import configparser
import json
import os
import re

import aiohttp

import config
from utils import Cancelled, run_proc, watch_dir


class RcloneError(Exception):
    pass


GD_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com\S*?(?:/folders/|/file/d/|[?&]id=)([\w-]{10,})"
)


def gdrive_id(text):
    m = GD_RE.search(text or "")
    return m.group(1) if m else None


def conf_path(uid):
    return os.path.join(config.RCLONE_DIR, f"{uid}.conf")


def has_conf(uid):
    return os.path.exists(conf_path(uid))


def _parse(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    cp.read(path, encoding="utf-8")
    return cp


def first_drive_remote(path):
    try:
        cp = _parse(path)
    except Exception:
        return None
    for sec in cp.sections():
        if cp[sec].get("type", "").strip() == "drive":
            return sec
    return None


def save_conf(uid, data: bytes):
    """Save user's rclone.conf. Returns the drive remote name, or None if invalid."""
    os.makedirs(config.RCLONE_DIR, exist_ok=True)
    path = conf_path(uid)
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    remote = first_drive_remote(path)
    if not remote:
        os.remove(path)
    return remote


def remove_conf(uid):
    try:
        os.remove(conf_path(uid))
    except FileNotFoundError:
        pass


def _argv(uid, args):
    return ["rclone", "--config", conf_path(uid), "--retries", "3",
            "--low-level-retries", "10", *args]


async def _simple(uid, args, timeout=180):
    proc = await asyncio.create_subprocess_exec(
        *_argv(uid, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RcloneError("rclone timeout")
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def verify(uid, remote):
    rc, _, err = await _simple(uid, ["about", f"{remote}:"], 90)
    return rc == 0, err.strip()[-300:]


def _access_token(uid, remote):
    cp = _parse(conf_path(uid))
    tok = json.loads(cp[remote]["token"])
    return tok["access_token"]


async def drive_meta(uid, remote, fid):
    """Get name/mimeType/parents/size of a Drive ID using the user's own token."""
    try:
        await _simple(uid, ["about", f"{remote}:"], 90)  # makes rclone refresh the token
        token = _access_token(uid, remote)
    except Exception:
        return None
    url = f"https://www.googleapis.com/drive/v3/files/{fid}"
    params = {"fields": "id,name,mimeType,parents,size", "supportsAllDrives": "true"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params,
                             headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None


async def list_folder(uid, remote, fid):
    rc, out, err = await _simple(
        uid, ["lsjson", f"{remote}:", "--drive-root-folder-id", fid, "-R", "--files-only"], 300
    )
    if rc != 0:
        raise RcloneError(err.strip()[-300:])
    return [d for d in json.loads(out or "[]") if not d.get("IsDir")]


STATS_FLAGS = ["--stats", "2s", "--use-json-log", "--stats-log-level", "NOTICE"]


def _make_cb(job):
    errs = []

    def cb(line):
        try:
            o = json.loads(line)
        except Exception:
            errs.append(line[-200:])
            return
        st = o.get("stats")
        if st:
            job.set_progress(st.get("bytes", 0), st.get("totalBytes") or job.total)
        elif o.get("level") in ("error", "critical"):
            errs.append(str(o.get("msg"))[-200:])

    return cb, errs


async def _run(job, args, on_err=None, errs=None, watch=None):
    watcher = asyncio.create_task(watch_dir(*watch)) if watch else None
    try:
        rc, tail = await run_proc(_argv(job.user_id, args), job, on_stderr=on_err)
    finally:
        if watcher:
            watcher.cancel()
    if job.cancelled:
        raise Cancelled()
    if rc != 0:
        raise RcloneError((errs[-1] if errs else tail[-300:]) or "rclone failed")


async def download_path(job, remote, root_id, rel_path, dst, total):
    cb, errs = _make_cb(job)
    args = ["copyto", f"{remote}:{rel_path}", dst, "--drive-root-folder-id", root_id,
            "--drive-acknowledge-abuse", "--multi-thread-streams", "8",
            "--multi-thread-cutoff", "32M", "--buffer-size", "64M",
            "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200", *STATS_FLAGS]
    await _run(job, args, on_err=cb, errs=errs)


async def download_id(job, remote, gid, dst, total):
    args = ["backend", "copyid", f"{remote}:", gid, dst, "--drive-acknowledge-abuse"]
    await _run(job, args, watch=(os.path.dirname(dst), total, job))


async def upload(job, remote, local, dest_dir, parent_id):
    name = os.path.basename(local)
    cb, errs = _make_cb(job)
    target = f"{remote}:{dest_dir}/{name}" if dest_dir else f"{remote}:{name}"
    args = ["copyto", local, target, *STATS_FLAGS,
            "--drive-chunk-size", "128M", "--buffer-size", "64M",
            "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200"]
    if parent_id:
        args += ["--drive-root-folder-id", parent_id]
    await _run(job, args, on_err=cb, errs=errs)


async def delete_id(uid, remote, fid):
    """Permanently delete a Drive file by ID (skips the trash). Returns True on success."""
    try:
        await _simple(uid, ["about", f"{remote}:"], 90)  # refresh the token
        token = _access_token(uid, remote)
        async with aiohttp.ClientSession() as s:
            async with s.delete(f"https://www.googleapis.com/drive/v3/files/{fid}",
                                params={"supportsAllDrives": "true"},
                                headers={"Authorization": f"Bearer {token}"}) as resp:
                return resp.status in (200, 204)
    except Exception:
        return False


async def path_link(uid, remote, path, parent_id):
    """Return (Drive URL, is_folder) for an uploaded file or folder, or (None, False)."""
    root = ["--drive-root-folder-id", parent_id] if parent_id else []
    try:
        rc, out, _ = await _simple(uid, ["lsjson", "--stat", f"{remote}:{path}", *root], 120)
        if rc == 0:
            d = json.loads(out)
            fid, is_dir = d.get("ID"), bool(d.get("IsDir"))
            if fid:
                if is_dir:
                    return f"https://drive.google.com/drive/folders/{fid}", True
                return f"https://drive.google.com/file/d/{fid}/view", False
        rc, out, _ = await _simple(uid, ["lsjson", "--dirs-only", f"{remote}:", *root], 180)
        if rc == 0:
            for d in json.loads(out or "[]"):
                if d.get("Name") == path and d.get("ID"):
                    return f"https://drive.google.com/drive/folders/{d['ID']}", True
    except Exception:
        pass
    return None, False
