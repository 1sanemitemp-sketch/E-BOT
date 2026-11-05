import asyncio
import os
import re
import shutil
from collections import deque

import psutil

import config


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
