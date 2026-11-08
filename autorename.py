"""Auto Rename: build a file name from a user format, using the original file name and the real media tracks.

The parsing follows the L4SAVE reference bot, with these differences:
  {resolution} is the resolution of the output file only (no bit depth),
  {shortlang} is the language itself for one audio track, Dual for two, Multi for three or more.
"""
import html
import re
from os import path as ospath

PLACEHOLDERS = ("name", "year", "season", "episode", "quality", "resolution", "audio", "lib", "ott",
                "languages", "subtitles", "shortlang", "shortsub", "duration", "extension",
                "raw_name", "file_name", "file_caption")

LANGS = {
    "eng": "English", "en": "English", "hin": "Hindi", "hi": "Hindi", "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean", "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "cmn": "Chinese",
    "yue": "Cantonese", "tam": "Tamil", "ta": "Tamil", "tel": "Telugu", "te": "Telugu", "mal": "Malayalam",
    "ml": "Malayalam", "kan": "Kannada", "kn": "Kannada", "ben": "Bengali", "bn": "Bengali", "urd": "Urdu",
    "ur": "Urdu", "pan": "Punjabi", "pa": "Punjabi", "mar": "Marathi", "mr": "Marathi", "guj": "Gujarati",
    "gu": "Gujarati", "bho": "Bhojpuri", "ori": "Odia", "ory": "Odia", "asm": "Assamese", "nep": "Nepali",
    "sin": "Sinhala", "fre": "French", "fra": "French", "fr": "French", "ger": "German", "deu": "German",
    "de": "German", "spa": "Spanish", "es": "Spanish", "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian", "ita": "Italian", "it": "Italian", "ara": "Arabic", "ar": "Arabic",
    "tha": "Thai", "th": "Thai", "vie": "Vietnamese", "vi": "Vietnamese", "ind": "Indonesian",
    "id": "Indonesian", "may": "Malay", "msa": "Malay", "tur": "Turkish", "tr": "Turkish", "pol": "Polish",
    "pl": "Polish", "dut": "Dutch", "nld": "Dutch", "nl": "Dutch", "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "heb": "Hebrew", "per": "Persian",
    "fas": "Persian", "ukr": "Ukrainian", "gre": "Greek", "ell": "Greek", "cze": "Czech", "ces": "Czech",
    "hun": "Hungarian", "rum": "Romanian", "ron": "Romanian", "bul": "Bulgarian", "hrv": "Croatian",
    "srp": "Serbian", "fil": "Filipino", "tgl": "Tagalog", "khm": "Khmer", "mya": "Burmese",
}
_NO_LANG = {"", "und", "zxx", "mis", "mul", "unknown"}


def lang_name(code, title=""):
    """Language name of a track from its language tag (or, failing that, its title)."""
    code = (code or "").strip().lower()
    if code in LANGS:
        return LANGS[code]
    if code not in _NO_LANG:
        try:  # optional: the langcodes package knows every code
            from langcodes import Language
            return Language.get(code).display_name()
        except Exception:
            pass
    low = (title or "").lower()
    for name in sorted(set(LANGS.values()), key=len, reverse=True):
        if re.search(r"\b" + name.lower() + r"\b", low):
            return name
    return ""


# ----------------------------------------------------------------- file name parsing
_KNOWN_TAGS = re.compile(r"\b(?:HDHub4u|MoviesVerse|FilmyZilla|KatMovieHD|Worldfree4u|9xmovies|ExtraFlix|"
                         r"PrimeFix|mkvCinemas|Toonworld4all)\b", re.I)
_AUDIO = re.compile(r"\b(?:AAC|DD|DDP|Opus|AC3)\s?(?:\d{1,2}(?:\.\d+)?)?(?:\s?Atmos)?\b|\b(?:\d+CH)\b", re.I)
_QUALITIES = ["SDTV-Rip", "HDTV-Rip", "SDTV", "HDTV", "SDTVRip", "HDTVRip", "DVDScr", "PRE-HD", "WEBDL",
              "WEB-DL", "WEB DL", "WEBRip", "BluRay", "Blu-Ray", "BDRip", "HDRip", "PreDVD", "DVDRip", "YTRip",
              "HDTS", "HDTC", "CAMRip", "HQSprint", "HDCAM", "HDCAMRip", "TELESYNC", "TSQRip", "BRRip",
              "HQ SPrint", "REMUX"]
_OTT = ["NF", "AMZN", "HBO", "JioHotstar", "JioHS", "JHS", "DSNP", "HS", "CR", "JC", "Jio", "PF", "VOD", "BMS",
        "CRAV", "MX", "ATVP", "MAX", "PCOK", "SONYLiv", "ZEE5", "SS", "APLVTV", "HULU", "APL", "YT", "STZ",
        "STARZ", "PS", "AHA"]
_LANG_WORDS = ["Hindi", "English", "Tamil", "Telugu", "Malayalam", "Kannada", "Marathi", "Bengali", "Gujarati",
               "Bhojpuri", "Odia", "Spanish", "French", "German", "Chinese", "Japanese", "Korean", "Italian",
               "Portuguese", "Russian"]
_NAME_STOP = re.compile(r"^(.*?)(?=\s+(?:S\d+E\d+|S\d+|E\d+|Season\s*\d+|\d{4}(?!\d)|\d{3,4}p|-\s*\d{1,3}\b)"
                        r"(?![A-Za-z])|$)", re.I)


def clean_stem(stem):
    """Strip leading [group] tags, known site tags and separators from a name without extension."""
    s = re.sub(r"^(?:[\[\(][^\]\)]{1,40}[\]\)][\s._-]*)+", "", stem.strip())
    s = _KNOWN_TAGS.sub("", s)
    s = s.replace("_", " ").replace("+", " ")
    s = re.sub(r"\.+", " ", s)
    s = re.sub(r"[\[\]\(\)\{\}]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _season_episode(text):
    m = re.search(r"\bS(\d{1,2})E(\d{1,3})(?:[-._]E?(\d{1,3}))?\b", text, re.I)
    if m:
        end = f"E{int(m.group(3)):02d}" if m.group(3) else ""
        return f"S{int(m.group(1)):02d}", f"E{int(m.group(2)):02d}", end
    season = start = end = ""
    ms = re.search(r"\b(?:S|Season)\s*(\d{1,2})\b", text, re.I)
    if ms:
        season = f"S{int(ms.group(1)):02d}"
    me = re.search(r"\b(?:E|EP|Episode)\s*(\d{1,3})(?:\s*(?:to|-|T|\.|_)\s*(?:E)?(\d{1,3}))?", text, re.I)
    if me:
        start = f"E{int(me.group(1)):02d}"
        end = f"E{int(me.group(2)):02d}" if me.group(2) else ""
    else:  # anime style: "Show Name - 05"
        ma = re.search(r"\s-\s(\d{1,3})(?:v\d)?\b", text)
        if ma:
            start = f"E{int(ma.group(1)):02d}"
    return season, start, end


def parse_name(filename):
    """Pieces of the original file name: name, year, season, episode, audio, quality, ott, ..."""
    stem = ospath.splitext(filename)[0]
    part = re.search(r"\.part(\d{1,3})\b", filename, re.I)
    cleaned = clean_stem(stem)
    m = _NAME_STOP.search(cleaned)
    name = (m.group(1) if m else cleaned).strip(" .-–_")
    rest = cleaned[len(m.group(1)):] if m else ""
    dotted = re.sub(r"[\s_]+", ".", ospath.splitext(filename)[0])  # keeps DDP2.0 / 5.1 intact

    year = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", cleaned)
    season, start, end = _season_episode(cleaned)
    audio = " ".join(_AUDIO.findall(dotted))
    quality = next((q for q in _QUALITIES if re.search(r"\b" + re.escape(q) + r"\b", rest, re.I)), "")
    ott = ""
    if quality.lower() != "bluray":
        ott = next((p for p in _OTT if re.search(r"\b" + re.escape(p) + r"\b(?=\s|$)", rest, re.I)), "")
    sub = re.search(r"\b(?:HC-)?(?:[A-Za-z]{1,2}-)?[EeMm]sub\b", rest, re.I)
    languages = [lw for lw in _LANG_WORDS if re.search(r"\b" + lw + r"\b", rest, re.I)]
    return {
        "name": name, "year": year.group(1) if year else "", "season": season,
        "episode": f"{start}-{end[1:]}" if end else start, "audio": audio.strip(), "quality": quality,
        "ott": ott, "part": part.group(1) if part else "", "subtitle": sub.group(0) if sub else "",
        "languages": languages,
    }


# ----------------------------------------------------------------- values + rendering
def _duration(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600:02d}.{seconds % 3600 // 60:02d}.{seconds % 60:02d}"  # no colons in file names


def lib_from(vcodec_setting, source_codec=""):
    """Video codec name for {lib}: from the encoder that is used, or from the source when it is copied."""
    known = {"libx264": "x264", "libx265": "x265", "libvpx-vp9": "vp9", "libaom-av1": "av1"}
    if vcodec_setting in known:
        return known[vcodec_setting]
    return {"h264": "x264", "hevc": "x265", "av1": "av1", "vp9": "vp9"}.get(source_codec, source_codec or "")


def build_values(original_name, info, resolution, lib, audio_default="", keep_subs=True):
    p = parse_name(original_name)
    raw_name, ext = ospath.splitext(original_name)
    audio = (info or {}).get("audio", [])
    subs = (info or {}).get("subs", []) if keep_subs else []
    a_names = [lang_name(a.get("lang"), a.get("title")) for a in audio]
    s_names = [lang_name(s.get("lang"), s.get("title")) for s in subs]
    languages = [n for n in dict.fromkeys(a_names) if n] or p["languages"]
    subtitles = [n for n in dict.fromkeys(s_names) if n]

    if len(audio) == 0:
        short_lang = ""
    elif len(audio) == 1:
        short_lang = a_names[0] or (p["languages"][0] if p["languages"] else "")
    elif len(audio) == 2:
        short_lang = "Dual"
    else:
        short_lang = "Multi"
    if not subs:
        short_sub = ""
    elif len(subs) == 1:
        short_sub = f"{s_names[0][:1].upper()}Sub" if s_names[0] else (p["subtitle"] or "Sub")
    else:
        short_sub = "MSub"

    return {
        "name": p["name"], "year": p["year"], "season": p["season"], "episode": p["episode"],
        "quality": p["quality"], "resolution": resolution, "audio": p["audio"] or audio_default,
        "lib": lib, "ott": p["ott"], "languages": " ".join(languages), "subtitles": " ".join(subtitles),
        "shortlang": short_lang, "shortsub": short_sub, "duration": _duration((info or {}).get("duration")),
        "extension": ext, "raw_name": raw_name, "file_name": original_name, "file_caption": original_name,
        "_part": p["part"],
    }


def _tidy(text):
    text = re.sub(r"\[\s*\]|\(\s*\)|\{\s*\}", "", text)       # brackets around an empty placeholder
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([\]\)])", r"\1", text)
    text = re.sub(r"(?:\s*[-–]\s*){2,}", " - ", text)          # "- -" left by an empty placeholder
    return text.strip(" -–_.")


def render(template, original_name, info, *, resolution, ext, lib, audio_default="", keep_subs=True):
    """Build the new file name (with extension) from the user's format."""
    ext = ext if ext.startswith(".") or not ext else "." + ext
    values = build_values(original_name, info, resolution, lib, audio_default, keep_subs)
    values["extension"] = ext
    template = re.sub(r"<[^>]*>", "", template)  # file names cannot carry HTML
    body = re.sub(r"\{extension\}", "", template, flags=re.I)
    text = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1).lower(), m.group(0))), body)
    text = _tidy(text)
    if values["_part"]:
        text = f"{text} Part {values['_part']}".strip()
    text = re.sub(r'[<>:"|?*\x00-\x1f/\\]', "", text).strip(" .")
    if not text:
        text = values["raw_name"]
    return text[:240].rstrip(" .") + ext


def validate_format(fmt):
    """Returns (True, cleaned format) or (False, error message)."""
    fmt = re.sub(r"<[^>]*>", "", fmt or "").strip()
    if not fmt:
        return False, "The format is empty."
    if len(fmt) > 200:
        return False, "The format is too long (200 characters at most)."
    found = re.findall(r"\{(\w+)\}", fmt)
    if not found:
        return False, "Add at least one placeholder, for example {name}. Tap VIEW ALL AVAILABLE FORMATS."
    unknown = sorted({f for f in found if f.lower() not in PLACEHOLDERS})
    if unknown:
        return False, "Unknown placeholder: " + ", ".join("{" + u + "}" for u in unknown)
    return True, fmt


EXAMPLE_FILE = "Spider Noir S01E03 2040 1080p TG WEB-DL Hindi Bhojpuri 8CH x265 ESub.mkv"
_EXAMPLE_INFO = {"duration": 2712, "audio": [{"lang": "hin"}, {"lang": "bho"}], "subs": [{"lang": "eng"}]}


def preview(fmt):
    """What the format gives for the example file."""
    return render(fmt, EXAMPLE_FILE, _EXAMPLE_INFO, resolution="1080p", ext=".mkv", lib="x265")


GUIDE_HTML = (
    "<blockquote><b>Auto Rename Format Guide</b>\n\n"
    f"Example file: <code>{html.escape(EXAMPLE_FILE)}</code>\n\n"
    "<b>Available Placeholders:</b>\n\n"
    "{name} : The title only. Like: Spider Noir\n\n"
    "{year} : Year from the file name. Like: 2040\n\n"
    "{season} : Season of the file. Like: S01\n\n"
    "{episode} : Episode of the file. Like: E03\n\n"
    "{quality} : Quality tag from the file name. Like: WEB-DL, BluRay, HDRip\n\n"
    "{resolution} : Resolution of the output video. Like: 1080p, 720p\n\n"
    "{audio} : Audio tag from the file name. Like: 8CH, AAC, DDP5.1\n\n"
    "{lib} : Video codec of the output. Like: x264, x265\n\n"
    "{ott} : OTT platform tag if present. Like: NF, AMZN\n\n"
    "{languages} : Audio languages, detected from the file's actual audio tracks. Like: Hindi Bhojpuri\n\n"
    "{subtitles} : Subtitle languages, detected from the file's actual subtitle tracks. Like: English\n\n"
    "{shortlang} : One audio track: its language (Hindi). Two tracks: Dual. Three or more: Multi.\n\n"
    "{shortsub} : One subtitle track: ESub (first letter of its language + Sub). Two or more: MSub.\n\n"
    "{duration} : Duration of the file as HH.MM.SS (colons are not allowed in file names). Like: 00.45.12\n\n"
    "{extension} : Extension of the file. Like: .mkv, .mp4\n\n"
    "{raw_name} : Original file name without the extension.\n\n"
    "{file_name} : Original full file name with extension.\n\n"
    "{file_caption} : Same as {file_name}.\n\n"
    "<b>Auto Behaviour:</b>\n\n"
    "If the original file name has .partN in it (split file), \"Part N\" is added at the end automatically.\n\n"
    "HTML tags do not work in Auto Rename, since this builds the real file name, not a caption. "
    "Any HTML tags are removed automatically.\n\n"
    "If some info is not found in the file, that placeholder just stays empty and the rest of the name "
    "does not break. Empty brackets like [] are removed too.\n\n"
    "If {extension} is missing from the format, the extension is added at the end automatically.\n\n"
    "With -n the file is first downloaded under your -n name, then this format builds the final name "
    "from it (zip/extract names work the same way). Auto Rename applies to encoded files and to the video "
    "files uploaded with /gup and /tup.\n\n"
    "<b>Example Format:</b>\n"
    "<code>{name} {season}{episode} {year} {resolution} {ott} {audio} {lib}{extension}</code>\n\n"
    "<b>Result:</b>\n"
    "<code>Spider Noir S01E03 2040 1080p 8CH x265.mkv</code></blockquote>"
)
