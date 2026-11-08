import json
import os
import threading
import time

import config

DEFAULTS = {
    "vcodec": "libx264",
    "acodec": "aac",
    "crf": "23",
    "preset": "ultrafast",
    "res": "",
    "fps": "",
    "ext": "mkv",
    "swap": True,          # QUALITY NAME SWAP (default ON)
    "as_doc": False,       # False = send as media, True = send as document
    "auto_delete": False,  # permanently delete the GDrive source after upload (default OFF)
    "abitrate": "128k",
    "colorfix": True,         # write explicit colour tags into the encode (see encoder.color_flags)
    "tag": "",             # prefix of output folder / zip names, set by each user (empty = no prefix)
    "auto_rename": "",     # Auto Rename format, empty = off
}

BOT_DEFAULTS = {
    "max_concurrent": config.MAX_CONCURRENT,
    "user_limit": 0,            # tasks per user at the same time, 0 = unlimited
    "approval_required": True,
    "split_gdrive": False,      # also split files above 2GB into parts for GDrive (off: one big file)
    "accept_tasks": True,
    "log_chat": 0,              # log channel id, 0 = use the LOG_CHANNEL variable (or none)
}

ENCODE_KEYS = ("vcodec", "acodec", "abitrate", "crf", "preset", "res", "fps", "ext", "colorfix")
LIST_FILTERS = ("all", "active", "pending", "approved", "rejected", "muted", "banned", "admins")


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._last_save = 0.0
        self.data = {"users": {}, "settings": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                os.replace(path, path + ".bak")
        self.data.setdefault("users", {})
        self.data.setdefault("settings", {})
        self.data.setdefault("bot", {})
        self.data.setdefault("groups", {})
        self.fresh = set()  # users seen for the first time (the log channel is told once)

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._last_save = time.time()

    # ---------- users ----------
    def _new_record(self, uid, name, uname, now):
        return {"id": uid, "name": name, "username": uname, "first_seen": now,
                "last_seen": now, "status": "active", "admin": False, "jobs": 0, "auth": "new"}

    def touch(self, user):
        uid = str(user.id)
        now = time.time()
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or uid
        uname = user.username or ""
        rec = self.data["users"].get(uid)
        if rec is None:
            rec = self._new_record(user.id, name, uname, now)
            self.data["users"][uid] = rec
            self.fresh.add(user.id)
            self.save()
            return rec
        changed = rec.get("name") != name or rec.get("username") != uname
        rec.update(name=name, username=uname, last_seen=now)
        if changed or now - self._last_save > 60:
            self.save()
        return rec

    def ensure_user(self, uid):
        rec = self.get_user(uid)
        if rec:
            return rec
        rec = self._new_record(uid, str(uid), "", time.time())
        rec["last_seen"] = 0
        self.data["users"][str(uid)] = rec
        self.save()
        return rec

    def get_user(self, uid):
        return self.data["users"].get(str(uid))

    def set_status(self, uid, status):
        rec = self.get_user(uid)
        if rec:
            rec["status"] = status
            self.save()

    def set_admin(self, uid, flag):
        rec = self.ensure_user(uid)
        rec["admin"] = bool(flag)
        if flag:
            rec["auth"] = "approved"
        self.save()

    def bump_jobs(self, uid):
        rec = self.get_user(uid)
        if rec:
            rec["jobs"] = rec.get("jobs", 0) + 1
            self.save()

    # ---------- authorization ----------
    @staticmethod
    def auth_of(rec):
        # records created before the approval system existed are treated as approved
        return rec.get("auth", "approved")

    def set_auth(self, uid, state):
        rec = self.ensure_user(uid)
        rec["auth"] = state
        rec["auth_time"] = time.time()
        self.save()

    def is_admin(self, uid):
        if uid == config.OWNER_ID:
            return True
        rec = self.get_user(uid)
        return bool(rec and rec.get("admin"))

    def is_authorized(self, uid):
        if not self.get_bot("approval_required"):
            return True
        if self.is_admin(uid):
            return True
        rec = self.get_user(uid)
        return bool(rec and self.auth_of(rec) == "approved")

    # ---------- groups ----------
    def is_group_authorized(self, chat_id):
        return str(chat_id) in self.data["groups"]

    def set_group(self, chat_id, title="", flag=True, by=0):
        key = str(chat_id)
        if flag:
            self.data["groups"][key] = {"id": chat_id, "title": title or key,
                                        "since": time.time(), "by": by}
        else:
            self.data["groups"].pop(key, None)
        self.save()

    def list_groups(self):
        return sorted(self.data["groups"].values(), key=lambda g: g.get("since", 0))

    def can_use(self, uid, chat_id, private):
        """A user may use the bot when authorized, or inside a group that was authorized."""
        return self.is_authorized(uid) or (not private and self.is_group_authorized(chat_id))

    # ---------- bulk authorization ----------
    def authorize_all(self):
        """Approve every user who started the bot (admins are untouched, banned users stay banned)
        and unmute the muted ones. Returns how many users changed."""
        n = 0
        for rec in self.data["users"].values():
            if rec["id"] == config.OWNER_ID or rec.get("admin") or rec.get("status") == "banned":
                continue
            if rec.get("last_seen", 0) <= 0:
                continue
            if self.auth_of(rec) != "approved" or rec.get("status") == "muted":
                rec["auth"] = "approved"
                rec["auth_time"] = time.time()
                if rec.get("status") == "muted":
                    rec["status"] = "active"
                n += 1
        self.save()
        return n

    def unauthorize_all(self):
        """Unauthorize every user except the owner and the admins. Returns how many changed."""
        n = 0
        for rec in self.data["users"].values():
            if rec["id"] == config.OWNER_ID or rec.get("admin"):
                continue
            if self.auth_of(rec) != "rejected":
                rec["auth"] = "rejected"
                rec["auth_time"] = time.time()
                n += 1
        self.save()
        return n

    def log_chat(self):
        return self.get_bot("log_chat") or config.LOG_CHANNEL

    def admin_ids(self):
        ids = {config.OWNER_ID}
        ids.update(u["id"] for u in self.data["users"].values() if u.get("admin"))
        return ids

    # ---------- lists ----------
    def list_users(self, flt="all"):
        now = time.time()
        out = []
        for u in self.data["users"].values():
            auth = self.auth_of(u)
            staff = bool(u.get("admin")) or u["id"] == config.OWNER_ID
            if flt == "muted" and u.get("status") != "muted":
                continue
            if flt == "banned" and u.get("status") != "banned":
                continue
            if flt == "admins" and not u.get("admin"):
                continue
            if flt == "pending" and not (auth == "pending" and not staff):
                continue
            if flt == "approved" and not (auth == "approved" and not staff):
                continue
            if flt == "rejected" and not (auth == "rejected" and not staff):
                continue
            if flt == "unauthorized" and (staff or auth == "approved"):
                continue
            if flt == "active" and not (u.get("status") == "active"
                                        and now - u.get("last_seen", 0) < 86400):
                continue
            out.append(u)
        out.sort(key=lambda u: u.get("last_seen", 0), reverse=True)
        return out

    def counts(self):
        return {k: len(self.list_users(k)) for k in LIST_FILTERS}

    # ---------- global bot settings ----------
    def get_bot(self, key):
        return self.data["bot"].get(key, BOT_DEFAULTS[key])

    def set_bot(self, key, value):
        self.data["bot"][key] = value
        self.save()

    # ---------- settings ----------
    def get_settings(self, uid):
        s = dict(DEFAULTS)
        s.update(self.data["settings"].get(str(uid), {}))
        return s

    def reset_settings(self, uid, keys=None):
        """Reset the given setting keys to their defaults, or all of them when keys is None."""
        cur = self.data["settings"].get(str(uid))
        if cur is None:
            return
        if keys is None:
            self.data["settings"].pop(str(uid), None)
        else:
            for k in keys:
                cur.pop(k, None)
        self.save()

    def set_setting(self, uid, key, value):
        self.data["settings"].setdefault(str(uid), {})[key] = value
        self.save()


db = Store(os.path.join(config.DATA_DIR, "db.json"))
