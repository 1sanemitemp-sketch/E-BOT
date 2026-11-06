import asyncio
import json
import os
import re

from utils import Cancelled, UserError, cpu_limit, run_proc

EXTS = {"mkv", "mp4", "avi", "webm", "ts", "mov"}
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".webm", ".ts", ".mov", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"}
QUAL_RE = re.compile(r"(?<![A-Za-z0-9])(?:2160|1440|1080|720|576|540|480|360|240)p(?![A-Za-z0-9])", re.I)


async def probe(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        d = json.loads(out)
    except Exception:
        return None
    vs = [s for s in d.get("streams", [])
          if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not vs:
        return None
    v = vs[0]
    try:
        dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": v.get("width") or 0, "height": v.get("height") or 0}


def res_height(res):
    if not res:
        return None
    try:
        return int(str(res).split(":")[1])
    except (IndexError, ValueError):
        return None


def quality_label(settings, src_height):
    h = res_height(settings.get("res")) or src_height
    return f"{h}p"


def swap_quality(text, label, swap):
    """Replace the first quality token (e.g. 1080p) in text with label when swap is on."""
    if swap and QUAL_RE.search(text):
        return QUAL_RE.sub(label, text, count=1)
    return text


def output_name(src_name, label, swap, ext, custom=None):
    """Returns (final file name, container extension)."""
    if custom:
        cext = os.path.splitext(custom)[1].lstrip(".").lower()
        if cext in EXTS:
            root, dot_ext = os.path.splitext(custom)
            out_ext = cext
        else:
            root, dot_ext = custom, f".{ext}"
            out_ext = ext
        if swap and QUAL_RE.search(root):
            root = QUAL_RE.sub(label, root, count=1)
        return root + dot_ext, out_ext
    stem = os.path.splitext(src_name)[0]
    if swap:
        if QUAL_RE.search(stem):
            new = QUAL_RE.sub(label, stem, count=1)
        else:
            new = f"{stem} {label}"
    else:
        new = f"{label}_{stem}"
    return f"{new}.{ext}", ext


def folder_name(base, label, swap, tag):
    """Output folder/zip name: {tag} {base}, with the quality token swapped when swap is on."""
    if swap and label and QUAL_RE.search(base):
        base = QUAL_RE.sub(label, base, count=1)
    return f"{tag} {base}"


def build_cmd(src, dst, s, ext, with_subs):
    v, a = s["vcodec"], s["acodec"]
    threads = str(max(1, min(cpu_limit(), 16)))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
           "-progress", "pipe:1", "-threads", threads, "-i", src,
           "-threads", threads, "-map", "0:v:0", "-map", "0:a?"]
    if with_subs and ext == "mkv":
        cmd += ["-map", "0:s?", "-map", "0:t?", "-c:s", "copy", "-c:t", "copy"]
    elif with_subs and ext in ("mp4", "mov"):
        cmd += ["-map", "0:s?", "-c:s", "mov_text"]

    if v == "copy":
        cmd += ["-c:v", "copy"]
    else:
        crf = str(s["crf"])
        cmd += ["-c:v", v]
        if v in ("libx264", "libx265"):
            cmd += ["-crf", crf, "-preset", s["preset"], "-pix_fmt", "yuv420p"]
            if v == "libx265" and ext in ("mp4", "mov"):
                cmd += ["-tag:v", "hvc1"]
        elif v == "libvpx-vp9":
            cmd += ["-crf", str(round(float(crf))), "-b:v", "0", "-row-mt", "1", "-cpu-used", "4", "-pix_fmt", "yuv420p"]
        elif v == "libaom-av1":
            cmd += ["-crf", str(round(float(crf))), "-b:v", "0", "-cpu-used", "6", "-pix_fmt", "yuv420p"]
        h = res_height(s.get("res"))
        if h:
            cmd += ["-vf", f"scale=-2:{h}"]
        if s.get("fps"):
            cmd += ["-r", str(s["fps"])]

    if a == "copy":
        cmd += ["-c:a", "copy"]
    else:
        enc = "libopus" if a == "opus" else a
        cmd += ["-c:a", enc, "-b:a", str(s.get("abitrate") or "128k")]

    cmd += ["-max_muxing_queue_size", "9999", dst]
    return cmd


async def encode(job, src, dst, s, ext, duration):
    attempts = [True, False] if ext in ("mkv", "mp4", "mov") else [False]
    last = ""
    for with_subs in attempts:
        cmd = build_cmd(src, dst, s, ext, with_subs)

        def on_out(line):
            if line.startswith(("out_time_us=", "out_time_ms=")):
                v = line.split("=", 1)[1].strip()
                if v.lstrip("-").isdigit():
                    secs = int(v) / 1_000_000
                    if secs >= 0:
                        job.set_progress(min(secs, duration), duration)

        rc, err = await run_proc(cmd, job, on_stdout=on_out)
        if job.cancelled:
            raise Cancelled()
        if rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return
        last = err
        if os.path.exists(dst):
            os.remove(dst)
    raise UserError(f"FFmpeg error: {last[-400:]}")


async def thumbnail(path, out, duration):
    t = int(max(1, (duration or 0) * 0.1))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", path,
           "-frames:v", "1", "-vf", "scale=320:-2", out]
    rc, _ = await run_proc(cmd)
    return out if rc == 0 and os.path.exists(out) else None
