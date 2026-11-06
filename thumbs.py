import os

import aiohttp

import config
from utils import run_proc

MAX_DOWNLOAD = 20 * 1024 * 1024


def path_for(uid):
    return os.path.join(config.THUMB_DIR, f"{uid}.jpg")


def has(uid):
    return os.path.exists(path_for(uid))


def remove(uid):
    try:
        os.remove(path_for(uid))
    except FileNotFoundError:
        pass


async def make(src, dst):
    """Convert any image to a Telegram-compatible thumbnail (JPEG, max 320px, under 200KB)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for quality in ("4", "12", "24"):
        rc, _ = await run_proc(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf",
                                "scale=320:320:force_original_aspect_ratio=decrease",
                                "-frames:v", "1", "-q:v", quality, dst])
        if rc != 0 or not os.path.exists(dst):
            return False
        if os.path.getsize(dst) <= 200 * 1024:
            return True
    return False


async def fetch(url, dest_dir):
    """Download an image link. Returns the local path or None."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "thumb_src")
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status >= 400:
                    return None
                size = 0
                with open(path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            return None
                        f.write(chunk)
        return path
    except Exception:
        return None
