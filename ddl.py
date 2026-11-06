import asyncio
import os
import re
from urllib.parse import unquote, urlparse

import aiohttp

from utils import Cancelled, UserError, safe_name

CONNECTIONS = 8
PART_SIZE = 8 * 1024 * 1024
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _pick_name(r):
    cd = r.content_disposition
    name = cd.filename if cd and cd.filename else None
    if not name:
        name = os.path.basename(unquote(urlparse(str(r.url)).path)) or "video.mkv"
    name = safe_name(name)
    if not os.path.splitext(name)[1]:
        name += ".mkv"
    return name


async def _probe(s, url):
    """Returns (file name, total size, server supports ranges)."""
    async with s.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as r:
        if r.status >= 400 and r.status != 416:
            raise UserError(f"Link error: HTTP {r.status}")
        if "text/html" in (r.headers.get("Content-Type") or "").lower():
            raise UserError("This is not a direct download link (got an HTML page).")
        name = _pick_name(r)
        total, ranged = 0, False
        if r.status == 206:
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            if m:
                total = int(m.group(1))
                ranged = total > 0
        else:
            total = r.content_length or 0
        return name, total, ranged


async def _stream(s, url, path, total, job):
    async with s.get(url, allow_redirects=True) as r:
        if r.status >= 400:
            raise UserError(f"Link error: HTTP {r.status}")
        done = 0
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                if job.cancelled:
                    raise Cancelled()
                f.write(chunk)
                done += len(chunk)
                job.set_progress(done, total)


async def _fetch(s, url, f, start, end, job, state, total):
    last = None
    for attempt in range(4):
        got = 0
        try:
            async with s.get(url, headers={"Range": f"bytes={start}-{end}"}) as r:
                if r.status != 206:
                    raise RuntimeError(f"HTTP {r.status}")
                async for chunk in r.content.iter_chunked(1 << 18):
                    if job.cancelled:
                        raise Cancelled()
                    f.seek(start + got)
                    f.write(chunk)
                    got += len(chunk)
                    state["done"] += len(chunk)
                    job.set_progress(state["done"], total)
            if got != end - start + 1:
                raise RuntimeError("short read")
            return
        except Cancelled:
            raise
        except Exception as e:  # retry this part
            state["done"] -= got
            last = e
            await asyncio.sleep(1 + attempt)
    raise UserError(f"Download failed: {last}")


async def _parallel(s, url, path, total, job):
    with open(path, "wb") as f:
        f.truncate(total)
    parts = asyncio.Queue()
    for start in range(0, total, PART_SIZE):
        parts.put_nowait((start, min(start + PART_SIZE, total) - 1))
    state = {"done": 0}
    with open(path, "r+b") as f:
        async def worker():
            while True:
                try:
                    start, end = parts.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await _fetch(s, url, f, start, end, job, state, total)

        tasks = [asyncio.create_task(worker()) for _ in range(CONNECTIONS)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


async def download(url, dest_dir, job, local_name=None):
    """Download a direct link. Uses several parallel connections when the server supports ranges."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                     headers={"User-Agent": USER_AGENT}) as s:
        name, total, ranged = await _probe(s, url)
        if local_name:
            if not os.path.splitext(local_name)[1]:
                local_name += os.path.splitext(name)[1]
            name = safe_name(local_name)
        job.file_name = name if local_name else (job.custom_name or name)
        job.total = total
        path = os.path.join(dest_dir, name)
        if ranged and total >= PART_SIZE:
            await _parallel(s, url, path, total, job)
        else:
            await _stream(s, url, path, total, job)
    return path
