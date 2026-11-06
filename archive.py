import asyncio
import os
import re
import zipfile

import encoder
from rclone import _watch_dir
from utils import Cancelled, UserError, natural_key, run_proc

ARCHIVE_EXT = {".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}


def stem(name):
    root = os.path.splitext(name)[0]
    if root.lower().endswith(".tar"):
        root = os.path.splitext(root)[0]
    return root


async def _total_size(path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "7z", "l", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
        found = re.findall(r"^\s*(?:\d{4}-\d\d-\d\d\s+\d\d:\d\d:\d\d\s+)?(\d+)\s+(?:\d+\s+)?\d+ files?",
                           out.decode(errors="ignore"), re.M)
        return int(found[-1]) if found else 0
    except Exception:
        return 0


def _walk(root):
    for base, _, names in os.walk(root):
        for n in names:
            yield os.path.join(base, n)


async def extract(job, path, out_dir):
    """Extract an archive and return the sorted list of video files inside it."""
    if os.path.splitext(path)[1].lower() not in ARCHIVE_EXT:
        raise UserError("Not a supported archive (zip, 7z, tar, gz, bz2, xz).")
    os.makedirs(out_dir, exist_ok=True)
    target = path
    for _ in range(2):  # a second pass handles .tar.gz style archives
        job.set_stage("extracting", "7-Zip")
        job.total = await _total_size(target)
        watcher = asyncio.create_task(_watch_dir(out_dir, job.total, job))
        try:
            rc, err = await run_proc(
                ["7z", "x", "-y", "-bd", "-bso0", "-bsp0", f"-o{out_dir}", target], job)
        finally:
            watcher.cancel()
        if job.cancelled:
            raise Cancelled()
        if rc != 0:
            raise UserError(f"Extraction failed: {err[-200:]}")
        files = [f for f in _walk(out_dir)
                 if os.path.splitext(f)[1].lower() in encoder.VIDEO_EXT]
        if files:
            return sorted(files, key=natural_key)
        tars = [f for f in _walk(out_dir) if f.lower().endswith(".tar")]
        if len(tars) != 1:
            break
        target = tars[0]
    raise UserError("No video files found inside the archive.")


def make_zip(files, dest, job):
    """Store all files in one zip archive (no compression, videos are already compressed)."""
    done = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for p in files:
            info = zipfile.ZipInfo.from_file(p, arcname=os.path.basename(p))
            info.compress_type = zipfile.ZIP_STORED
            with open(p, "rb") as src, zf.open(info, "w", force_zip64=True) as out:
                while True:
                    if job.cancelled:
                        raise Cancelled()
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    job.set_progress(done, job.total)
