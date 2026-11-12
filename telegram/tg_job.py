# -*- coding: utf-8 -*-
"""
משימת טלגרם שרצה ב-GitHub Actions.
מקבלת את פרטי המשימה מה-Worker (לפי JOB_ID), מבצעת אותה עם החשבון האישי, ומדווחת חזרה.

מצבים (mode):
  login_start   {phone}                 -> שולח קוד התחברות
  login_finish  {code, password}        -> משלים התחברות (password = אימות דו-שלבי, אם יש)
  list_chats    {}                      -> רשימת צ'אטים/קבוצות/ערוצים
  list_files    {chat, offset_id}       -> קבצים אחרונים בצ'אט
  transfer      {links:[...]} או {items:[{chat,msg_id}]}  -> העברה לדרייב בזרימה
  logout        {}                      -> ניתוק החשבון

שום מידע רגיש לא מודפס ללוג (הלוגים של GitHub עלולים להיות ציבוריים).
"""

import asyncio
import json
import os
import re
import time

import requests
from cryptography.fernet import Fernet
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

WORKER = os.environ["WORKER_URL"].rstrip("/")
SECRET = os.environ["CALLBACK_SECRET"]
JOB_ID = os.environ["JOB_ID"]
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
FERNET = Fernet(os.environ["SESSION_KEY"].encode())

CHUNK = 8 * 1024 * 1024          # 8MB לכל שליחה לדרייב (כפולה של 256KB)
DRIVE_FOLDER_NAME = "Telegram_Files"


# ---------------- תקשורת עם ה-Worker ----------------
def cb(op, payload=None):
    r = requests.post(
        f"{WORKER}/tg-callback",
        json={"op": op, "job_id": JOB_ID, **(payload or {})},
        headers={"X-Callback-Secret": SECRET},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def report(status, data=None, progress=None):
    try:
        cb("report", {"status": status, "data": data, "progress": progress})
    except Exception as e:
        print("report failed:", type(e).__name__)


def kv_get(key):
    v = cb("kv_get", {"key": key}).get("value")
    return FERNET.decrypt(v.encode()).decode() if v else None


def kv_put(key, value):
    enc = FERNET.encrypt(value.encode()).decode() if value is not None else None
    cb("kv_put", {"key": key, "value": enc})


# ---------------- דרייב ----------------
class Drive:
    def __init__(self, token, folder_name=DRIVE_FOLDER_NAME):
        self.folder_name = folder_name
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.folder = None

    def folder_id(self):
        if self.folder:
            return self.folder
        q = (f"name='{self.folder_name}' and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
        r = self.s.get("https://www.googleapis.com/drive/v3/files", params={"q": q})
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self.folder = files[0]["id"]
        else:
            r = self.s.post("https://www.googleapis.com/drive/v3/files",
                            json={"name": self.folder_name,
                                  "mimeType": "application/vnd.google-apps.folder"})
            r.raise_for_status()
            self.folder = r.json()["id"]
        return self.folder

    def start(self, name, size, mime):
        r = self.s.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
            json={"name": name, "parents": [self.folder_id()]},
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
        )
        if r.status_code == 401:
            raise RuntimeError("פג תוקף ההתחברות לגוגל - התחבר מחדש באתר ונסה שוב")
        r.raise_for_status()
        return r.headers["Location"]

    def put(self, url, data, start, total):
        end = start + len(data) - 1
        last = None
        for attempt in range(6):
            try:
                r = self.s.put(url, data=data,
                               headers={"Content-Range": f"bytes {start}-{end}/{total}"},
                               timeout=300)
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code == 308:
                    return None
                last = f"HTTP {r.status_code}"
                if r.status_code == 401:
                    raise RuntimeError("פג תוקף ההתחברות לגוגל באמצע ההעלאה")
            except requests.RequestException as e:
                last = type(e).__name__
            time.sleep(2 ** attempt)
        raise RuntimeError(f"העלאה לדרייב נכשלה ({last})")


# ---------------- טלגרם ----------------
def make_client(session_str=""):
    return TelegramClient(StringSession(session_str), API_ID, API_HASH,
                          device_model="DRIV", app_version="1.0")


async def logged_in_client():
    s = kv_get("session")
    if not s:
        raise RuntimeError("החשבון לא מחובר - צריך להתחבר קודם")
    client = make_client(s)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("ההתחברות לטלגרם פגה - צריך להתחבר מחדש")
    return client


_dialogs_loaded = False


async def resolve(client, chat):
    """chat יכול להיות מספר (מזהה) או שם משתמש."""
    global _dialogs_loaded
    if isinstance(chat, str) and re.fullmatch(r"-?\d+", chat):
        chat = int(chat)
    try:
        return await client.get_entity(chat)
    except (ValueError, TypeError):
        if not _dialogs_loaded:
            await client.get_dialogs()     # טוען את המטמון של הצ'אטים
            _dialogs_loaded = True
        return await client.get_entity(chat)


def parse_link(link):
    link = link.strip()
    m = re.search(r"t\.me/c/(\d+)/(?:\d+/)?(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,})/(?:\d+/)?(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None


def file_info(msg):
    f = msg.file
    name = f.name or f"tg_{msg.chat_id}_{msg.id}{f.ext or ''}"
    return {
        "id": msg.id,
        "name": name,
        "size": f.size or 0,
        "mime": f.mime_type or "application/octet-stream",
        "date": msg.date.strftime("%d/%m/%Y %H:%M") if msg.date else "",
        "text": (msg.message or "")[:120],
    }


# ---------------- המצבים ----------------
async def do_login_start(args):
    phone = (args.get("phone") or "").replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+972" + phone[1:]
    client = make_client()
    await client.connect()
    sent = await client.send_code_request(phone)
    kv_put("login_tmp", json.dumps({
        "session": client.session.save(), "phone": phone,
        "hash": sent.phone_code_hash, "code_ok": False,
    }))
    await client.disconnect()
    return {"msg": "קוד נשלח. הוא מגיע בהודעה בטלגרם (באפליקציה המחוברת) או ב-SMS."}


async def do_login_finish(args):
    raw = kv_get("login_tmp")
    if not raw:
        raise RuntimeError("לא נמצא תהליך התחברות פתוח - לחץ קודם על 'שלח קוד'")
    tmp = json.loads(raw)
    code = (args.get("code") or "").strip()
    password = args.get("password") or ""
    client = make_client(tmp["session"])
    await client.connect()
    try:
        if not tmp.get("code_ok"):
            try:
                await client.sign_in(tmp["phone"], code, phone_code_hash=tmp["hash"])
            except errors.SessionPasswordNeededError:
                tmp["code_ok"] = True
                tmp["session"] = client.session.save()
                kv_put("login_tmp", json.dumps(tmp))
                if not password:
                    raise RuntimeError("לחשבון יש אימות דו-שלבי - הכנס גם את הסיסמה ולחץ שוב 'התחבר'")
                await client.sign_in(password=password)
        else:
            if not password:
                raise RuntimeError("צריך את סיסמת האימות הדו-שלבי")
            await client.sign_in(password=password)
    except errors.PhoneCodeInvalidError:
        raise RuntimeError("הקוד שגוי")
    except errors.PhoneCodeExpiredError:
        raise RuntimeError("הקוד פג תוקף - שלח קוד חדש")
    except errors.PasswordHashInvalidError:
        raise RuntimeError("סיסמת האימות הדו-שלבי שגויה")

    kv_put("session", client.session.save())
    kv_put("login_tmp", None)
    me = await client.get_me()
    await client.disconnect()
    return {"msg": f"מחובר בתור {me.first_name or ''} {me.last_name or ''}".strip()}


async def do_logout(args):
    try:
        client = await logged_in_client()
        await client.log_out()
    except Exception:
        pass
    kv_put("session", None)
    return {"msg": "החשבון נותק"}


async def do_list_chats(args):
    client = await logged_in_client()
    out = []
    async for d in client.iter_dialogs(limit=200):
        if d.is_channel and not d.is_group:
            kind = "ערוץ"
        elif d.is_group:
            kind = "קבוצה"
        else:
            kind = "פרטי"
        out.append({"id": str(d.id), "name": d.name or "(ללא שם)", "type": kind,
                    "unread": d.unread_count})
    await client.disconnect()
    return {"chats": out}


async def do_list_files(args):
    client = await logged_in_client()
    entity = await resolve(client, args["chat"])
    offset = int(args.get("offset_id") or 0)
    files, last_id, scanned = [], None, 0
    async for msg in client.iter_messages(entity, limit=400, offset_id=offset):
        scanned += 1
        last_id = msg.id
        if msg.file and not msg.sticker:
            files.append(file_info(msg))
            if len(files) >= 40:
                break
    await client.disconnect()
    return {"files": files, "next_offset": last_id if scanned else None}


async def transfer_one(client, drive, entity, msg_id, label, idx, total_items):
    msg = await client.get_messages(entity, ids=msg_id)
    if not msg:
        raise RuntimeError("ההודעה לא נמצאה")
    if not msg.file:
        raise RuntimeError("אין קובץ בהודעה הזו")
    info = file_info(msg)
    size = info["size"]
    if not size:
        raise RuntimeError("גודל הקובץ לא ידוע")
    url = drive.start(info["name"], size, info["mime"])
    buf = bytearray()
    sent = 0
    result = None
    last_report = 0
    async for part in client.iter_download(msg.media, request_size=512 * 1024):
        buf += part
        while len(buf) >= CHUNK:
            result = drive.put(url, bytes(buf[:CHUNK]), sent, size)
            sent += CHUNK
            del buf[:CHUNK]
            if time.time() - last_report > 5:
                last_report = time.time()
                report("running", progress=f"קובץ {idx}/{total_items}: {info['name']} - {sent * 100 // size}%")
    if buf:
        result = drive.put(url, bytes(buf), sent, size)
    if not result or "id" not in result:
        raise RuntimeError("דרייב לא אישר את סיום ההעלאה")
    return {"name": info["name"], "ok": True, "fileId": result["id"]}


async def do_transfer(args, drive_token):
    if not drive_token:
        raise RuntimeError("צריך להתחבר לגוגל באתר לפני העברה")
    items = []
    for link in args.get("links") or []:
        if not link.strip():
            continue
        p = parse_link(link)
        items.append({"chat": p[0], "msg_id": p[1], "label": link} if p
                     else {"bad": True, "label": link})
    for it in args.get("items") or []:
        items.append({"chat": it["chat"], "msg_id": int(it["msg_id"]),
                      "label": it.get("name") or str(it["msg_id"])})
    if not items:
        raise RuntimeError("לא נבחרו קבצים")

    client = await logged_in_client()
    drive = Drive(drive_token, "DRIV_Temp" if args.get("temp") else DRIVE_FOLDER_NAME)
    results = []
    for i, it in enumerate(items, 1):
        if it.get("bad"):
            results.append({"name": it["label"], "ok": False, "error": "קישור לא מזוהה"})
            continue
        report("running", progress=f"קובץ {i}/{len(items)}: מתחיל...")
        try:
            entity = await resolve(client, it["chat"])
            results.append(await transfer_one(client, drive, entity, it["msg_id"],
                                              it["label"], i, len(items)))
        except errors.FloodWaitError as e:
            results.append({"name": it["label"], "ok": False,
                            "error": f"טלגרם ביקש להמתין {e.seconds} שניות"})
        except Exception as e:
            results.append({"name": it["label"], "ok": False, "error": str(e)})
    await client.disconnect()
    return {"results": results}


async def main():
    job = cb("get_job").get("job")
    if not job:
        print("job not found")
        return
    mode, args = job.get("mode"), job.get("args") or {}
    report("running", progress="מתחבר לטלגרם...")
    try:
        if mode == "login_start":
            data = await do_login_start(args)
        elif mode == "login_finish":
            data = await do_login_finish(args)
        elif mode == "logout":
            data = await do_logout(args)
        elif mode == "list_chats":
            data = await do_list_chats(args)
        elif mode == "list_files":
            data = await do_list_files(args)
        elif mode == "transfer":
            data = await do_transfer(args, job.get("drive_token"))
        else:
            raise RuntimeError("מצב לא מוכר")
        report("done", data=data)
        print("done:", mode)
    except errors.FloodWaitError as e:
        report("error", data=f"טלגרם ביקש להמתין {e.seconds} שניות לפני ניסיון נוסף")
    except Exception as e:
        report("error", data=str(e) or type(e).__name__)
        print("error in", mode, type(e).__name__)


if __name__ == "__main__":
    asyncio.run(main())
