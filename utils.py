import asyncio
import os
import re
import shutil
import time
from collections import deque

import psutil

import config


START_TIME = time.time()


class Cancelled(Exception):
    pass


class UserError(Exception):
    pass


_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name, limit=180):
    name = _BAD.sub("_", name or "").strip().strip(".")
    if len(name) > limit:
        root, ext = os.path.splitext(name)
        name = root[: limit - len(ext)] + ext
    return name or "file"


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)}B" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024


def human_time(sec):
    sec = int(max(0, sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def bar(pct, n=12):
    filled = int(max(0, min(100, pct)) / 100 * n)
    return "●" * filled + "○" * (n - filled)


def sysline():
    try:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        path = config.WORK_DIR if os.path.exists(config.WORK_DIR) else "/"
        free = shutil.disk_usage(path).free
        return f"CPU: {cpu:.1f}% | RAM: {ram:.1f}% | FREE: {human_size(free)}"
    except Exception:
        return ""


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


async def run_proc(args, job=None, on_stdout=None, on_stderr=None):
    """Run a subprocess, stream its lines to callbacks, return (returncode, stderr_tail)."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if job is not None:
        job.proc = proc
    tail = deque(maxlen=30)

    async def reader(stream, cb, keep):
        while True:
            line = await stream.readline()
            if not line:
                break
            s = line.decode(errors="ignore").rstrip()
            if keep:
                tail.append(s)
            if cb:
                try:
                    cb(s)
                except Exception:
                    pass

    try:
        await asyncio.gather(
            reader(proc.stdout, on_stdout, False),
            reader(proc.stderr, on_stderr, True),
        )
        rc = await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    finally:
        if job is not None:
            job.proc = None
    return rc, "\n".join(tail)


def stats():
    """Host/bot statistics used by the status message."""
    path = config.WORK_DIR if os.path.exists(config.WORK_DIR) else "/"
    try:
        du = shutil.disk_usage(path)
        free, free_pct = du.free, du.free / du.total * 100 if du.total else 0
    except Exception:
        free, free_pct = 0, 0
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "free": human_size(free),
        "free_pct": free_pct,
        "up": human_time(time.time() - START_TIME),
    }


def bot_stats_text():
    st = stats()
    return (f"┌ CPU → {st['cpu']:.1f}% | F → {st['free']} [{st['free_pct']:.1f}%]\n"
            f"└ RAM → {st['ram']:.1f}% | UP → {st['up']}")


def cpu_limit():
    """CPU count available to this container (cgroup quota aware)."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
        if quota != "max":
            return max(1, round(int(quota) / int(period)))
    except Exception:
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
            quota = int(f.read())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read())
        if quota > 0:
            return max(1, round(quota / period))
    except Exception:
        pass
    return os.cpu_count() or 2


async def watch_dir(path, total, job):
    """Report the size of a directory as job progress (used while rclone/7z write files)."""
    while True:
        await asyncio.sleep(2)
        size = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        job.set_progress(size, total)


ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".001"}
ARCHIVE_MAGIC = (b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf\x27\x1c")


def is_archive(path):
    if os.path.splitext(path)[1].lower() in ARCHIVE_EXT:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head.startswith(ARCHIVE_MAGIC)


def split_file(job, path, part_size):
    """Split a file into numbered parts (name.001, name.002, ...). Blocking, run it in a thread."""
    total = os.path.getsize(path)
    done = 0
    parts = []
    with open(path, "rb") as src:
        index = 0
        while True:
            index += 1
            part = f"{path}.{index:03d}"
            written = 0
            with open(part, "wb") as dst:
                while written < part_size:
                    if job.cancelled:
                        raise Cancelled()
                    chunk = src.read(min(1 << 20, part_size - written))
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
                    done += len(chunk)
                    job.set_progress(done, total)
            if written == 0:
                os.remove(part)
                break
            parts.append(part)
            if written < part_size:
                break
    return parts
