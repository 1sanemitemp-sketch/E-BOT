import os
from urllib.parse import unquote, urlparse

import aiohttp

from utils import Cancelled, UserError, safe_name


async def download(url, dest_dir, job):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status >= 400:
                raise UserError(f"Link error: HTTP {r.status}")
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise UserError("Eta direct download link na (HTML page ashche).")
            name = None
            cd = r.content_disposition
            if cd and cd.filename:
                name = cd.filename
            if not name:
                name = os.path.basename(unquote(urlparse(str(r.url)).path)) or "video.mkv"
            name = safe_name(name)
            if not os.path.splitext(name)[1]:
                name += ".mkv"
            path = os.path.join(dest_dir, name)
            total = r.content_length or 0
            job.file_name = name
            job.total = total
            done = 0
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    if job.cancelled:
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    job.set_progress(done, total)
    return path
