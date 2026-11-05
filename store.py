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
    "swap": True,        # QUALITY NAME SWAP (default ON)
    "as_doc": False,     # False = send as media, True = send as document
    "auto_delete": False,  # delete GDrive original after upload (default OFF)
}


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

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._last_save = time.time()

    # ---------- users ----------
    def touch(self, user):
        uid = str(user.id)
        now = time.time()
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or uid
        uname = user.username or ""
        rec = self.data["users"].get(uid)
        if rec is None:
            rec = {"id": user.id, "name": name, "username": uname, "first_seen": now,
                   "last_seen": now, "status": "active", "admin": False, "jobs": 0}
            self.data["users"][uid] = rec
            self.save()
            return rec
        changed = rec.get("name") != name or rec.get("username") != uname
        rec.update(name=name, username=uname, last_seen=now)
        if changed or now - self._last_save > 60:
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
        rec = self.get_user(uid)
        if rec:
            rec["admin"] = bool(flag)
            self.save()

    def bump_jobs(self, uid):
        rec = self.get_user(uid)
        if rec:
            rec["jobs"] = rec.get("jobs", 0) + 1
            self.save()

    def is_admin(self, uid):
        if uid == config.OWNER_ID:
            return True
        rec = self.get_user(uid)
        return bool(rec and rec.get("admin"))

    def list_users(self, flt="all"):
        now = time.time()
        out = []
        for u in self.data["users"].values():
            if flt == "muted" and u.get("status") != "muted":
                continue
            if flt == "banned" and u.get("status") != "banned":
                continue
            if flt == "admins" and not u.get("admin"):
                continue
            if flt == "active" and not (u.get("status") == "active"
                                        and now - u.get("last_seen", 0) < 86400):
                continue
            out.append(u)
        out.sort(key=lambda u: u.get("last_seen", 0), reverse=True)
        return out

    def counts(self):
        return {k: len(self.list_users(k)) for k in ("all", "active", "muted", "banned", "admins")}

    # ---------- settings ----------
    def get_settings(self, uid):
        s = dict(DEFAULTS)
        s.update(self.data["settings"].get(str(uid), {}))
        return s

    def set_setting(self, uid, key, value):
        self.data["settings"].setdefault(str(uid), {})[key] = value
        self.save()


db = Store(os.path.join(config.DATA_DIR, "db.json"))
