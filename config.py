import os


def _int(name, default=0):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")

TAG_NAME = os.getenv("TAG_NAME", "Blaze Ash")
MAX_CONCURRENT = max(1, _int("MAX_CONCURRENT", 1))
STATUS_INTERVAL = max(3, _int("STATUS_INTERVAL", 5))

DATA_DIR = os.getenv("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.access(DATA_DIR, os.W_OK):
        raise PermissionError
except Exception:
    DATA_DIR = os.path.abspath("./data")
    os.makedirs(DATA_DIR, exist_ok=True)

RCLONE_DIR = os.path.join(DATA_DIR, "rclone")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
ADMIN_CONTACTS = os.getenv("ADMIN_CONTACTS", "@sanemi111 @abbacalling420")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
APPROVAL_CONTACTS = [x.strip() for x in os.getenv("APPROVAL_CONTACTS", "@sanemi111,@abbacalling420").split(",") if x.strip()]
WORK_DIR = os.getenv("WORK_DIR", "/tmp/encbot")
TG_LIMIT = 2000 * 1024 * 1024  # Telegram bot upload limit


def validate():
    missing = [k for k, v in (("API_ID", API_ID), ("API_HASH", API_HASH),
                              ("BOT_TOKEN", BOT_TOKEN), ("OWNER_ID", OWNER_ID)) if not v]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
