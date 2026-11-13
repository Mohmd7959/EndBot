import static_ffmpeg
static_ffmpeg.add_paths()
import os, asyncio, sqlite3, subprocess, shutil, re, time
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import User, InputPeerUser, MessageEntityBlockquote, MessageEntityCode
from telethon.utils import get_peer_id
from telethon.tl.functions.phone import DiscardCallRequest
from telethon.tl.types import InputPhoneCall, PhoneCallDiscardReasonMissed, PhoneCallDiscardReasonHangup, UpdatePhoneCall
from telethon.errors import MessageNotModifiedError, UserAlreadyParticipantError, SessionPasswordNeededError, PhoneCodeInvalidError
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from config import *

bot = TelegramClient("bot_session", API_ID, API_HASH)
user_state = {}
login_pending = {}
auto_leave_tasks = {}

def get_state(uid):
    if uid not in user_state:
        user_state[uid] = {"clients": {}, "calls": {}}
    return user_state[uid]

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers(
        user_id INTEGER PRIMARY KEY,
        added_at TEXT,
        expires_at TEXT
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, account_id INTEGER, phone TEXT, first_name TEXT, username TEXT, session_file TEXT, added_at TEXT, UNIQUE(user_id, account_id))")
    c.execute("""CREATE TABLE IF NOT EXISTS dev_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER UNIQUE,
        first_name TEXT,
        session_string TEXT,
        added_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS videos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, 
        account_id INTEGER, 
        file_path TEXT, 
        file_name TEXT, 
        file_size INTEGER, 
        added_at TEXT,
        UNIQUE(user_id, account_id)
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS chats(user_id INTEGER PRIMARY KEY, chat_id INTEGER, chat_title TEXT, invite_link TEXT, set_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS active_streams(user_id INTEGER, account_id INTEGER, chat_id INTEGER, chat_title TEXT, video_id INTEGER, started_at TEXT, PRIMARY KEY(user_id, account_id))")
    
    try:
        c.execute("ALTER TABLE chats ADD COLUMN invite_link TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE subscribers ADD COLUMN expires_at TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE accounts ADD COLUMN session_string TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE videos ADD COLUMN media_type TEXT DEFAULT 'video'")
    except Exception:
        pass
    c.commit()
    c.close()

def is_dev(uid):
    return uid == DEV_ID

def is_admin(uid):
    if uid == DEV_ID: return True
    c = db()
    try:
        r = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
        return r is not None
    finally:
        c.close()

def add_admin_db(uid):
    c = db()
    try:
        c.execute("INSERT OR IGNORE INTO admins(user_id, added_at) VALUES(?,?)", (uid, datetime.now().isoformat()))
        c.commit()
        return True
    except Exception:
        return False
    finally:
        c.close()

def remove_admin_db(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def list_admins():
    c = db()
    try:
        rows = c.execute("SELECT * FROM admins").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def add_user(uid, uname, fname):
    c = db()
    try:
        c.execute("INSERT OR IGNORE INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?)", (uid, uname, fname, datetime.now().isoformat()))
        c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (uname, fname, uid))
        c.commit()
    finally:
        c.close()

def get_user(uid):
    c = db()
    try:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def is_subscriber(uid):
    if is_admin(uid): return True
    c = db()
    try:
        r = c.execute("SELECT * FROM subscribers WHERE user_id=?", (uid,)).fetchone()
        if not r: return False
        exp = r["expires_at"]
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp)
                if datetime.now() > exp_dt:
                    c.execute("DELETE FROM subscribers WHERE user_id=?", (uid,))
                    c.commit()
                    return False
            except Exception:
                pass
        return True
    finally:
        c.close()

def add_subscriber(uid, days):
    c = db()
    try:
        now = datetime.now()
        exp = now + timedelta(days=days)
        existing = c.execute("SELECT * FROM subscribers WHERE user_id=?", (uid,)).fetchone()
        if existing and existing["expires_at"]:
            try:
                cur_exp = datetime.fromisoformat(existing["expires_at"])
                if cur_exp > now:
                    exp = cur_exp + timedelta(days=days)
            except Exception:
                pass
        c.execute("INSERT OR REPLACE INTO subscribers(user_id, added_at, expires_at) VALUES(?,?,?)", (uid, now.isoformat(), exp.isoformat()))
        c.commit()
        return exp
    finally:
        c.close()

def remove_subscriber(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM subscribers WHERE user_id=?", (uid,))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def list_subscribers():
    c = db()
    try:
        rows = c.execute("SELECT * FROM subscribers ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def days_left(uid):
    c = db()
    try:
        r = c.execute("SELECT expires_at FROM subscribers WHERE user_id=?", (uid,)).fetchone()
        if not r or not r["expires_at"]:
            return 0
        try:
            exp = datetime.fromisoformat(r["expires_at"])
            diff = exp - datetime.now()
            return max(0, diff.days)
        except Exception:
            return 0
    finally:
        c.close()

def count_accounts(uid):
    c = db()
    try:
        n = c.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (uid,)).fetchone()[0]
        return n
    finally:
        c.close()

def max_accounts_for(uid):
    return MAX_ACCOUNTS_SUB if is_subscriber(uid) else MAX_ACCOUNTS_FREE

def add_account(uid, aid, phone, fname, uname, sfile, sstr=None):
    c = db()
    try:
        existing = c.execute("SELECT id FROM accounts WHERE user_id=? AND account_id=?", (uid, aid)).fetchone()
        if existing:
            return "exists"
        c.execute("INSERT INTO accounts(user_id, account_id, phone, first_name, username, session_file, session_string, added_at) VALUES(?,?,?,?,?,?,?,?)", (uid, aid, phone, fname, uname, sfile, sstr, datetime.now().isoformat()))
        c.commit()
        return True
    except Exception:
        return False
    finally:
        c.close()

def get_accounts(uid):
    c = db()
    try:
        rows = c.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def get_account(uid, aid):
    c = db()
    try:
        r = c.execute("SELECT * FROM accounts WHERE user_id=? AND account_id=?", (uid, aid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_account(uid, aid):
    c = db()
    try:
        r = c.execute("DELETE FROM accounts WHERE user_id=? AND account_id=?", (uid, aid))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def remove_all_accounts(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM accounts WHERE user_id=?", (uid,))
        c.commit()
        return r.rowcount
    finally:
        c.close()

def add_video(uid, aid, path, name, size, media_type="video"):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO videos(user_id, account_id, file_path, file_name, file_size, added_at, media_type) VALUES(?,?,?,?,?,?,?)", (uid, aid, path, name, size, datetime.now().isoformat(), media_type))
        c.commit()
    finally:
        c.close()

def get_video(uid, aid):
    c = db()
    try:
        r = c.execute("SELECT * FROM videos WHERE user_id=? AND account_id=?", (uid, aid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_video(uid, aid):
    c = db()
    try:
        r = c.execute("DELETE FROM videos WHERE user_id=? AND account_id=?", (uid, aid))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def set_chat(uid, cid, title, invite_link=None):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO chats(user_id, chat_id, chat_title, invite_link, set_at) VALUES(?,?,?,?,?)", (uid, cid, title, invite_link, datetime.now().isoformat()))
        c.commit()
    finally:
        c.close()

def get_chat(uid):
    c = db()
    try:
        r = c.execute("SELECT * FROM chats WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def set_active_stream(uid, aid, cid, title, vid):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO active_streams(user_id, account_id, chat_id, chat_title, video_id, started_at) VALUES(?,?,?,?,?,?)", (uid, aid, cid, title, vid, datetime.now().isoformat()))
        c.commit()
    finally:
        c.close()

def get_active_stream(uid, aid):
    c = db()
    try:
        r = c.execute("SELECT * FROM active_streams WHERE user_id=? AND account_id=?", (uid, aid)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_active_stream(uid, aid):
    c = db()
    try:
        r = c.execute("DELETE FROM active_streams WHERE user_id=? AND account_id=?", (uid, aid))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def get_all_active_streams_for_user(uid):
    c = db()
    try:
        rows = c.execute("SELECT * FROM active_streams WHERE user_id=?", (uid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def get_all_active_streams():
    c = db()
    try:
        rows = c.execute("SELECT * FROM active_streams").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

def clear_media_and_chats(uid):
    c = db()
    try:
        vids = c.execute("SELECT file_path FROM videos WHERE user_id=?", (uid,)).fetchall()
        for v in vids:
            if v["file_path"] and os.path.exists(v["file_path"]):
                try:
                    os.remove(v["file_path"])
                except Exception:
                    pass
        c.execute("DELETE FROM videos WHERE user_id=?", (uid,))
        c.execute("DELETE FROM chats WHERE user_id=?", (uid,))
        c.execute("DELETE FROM active_streams WHERE user_id=?", (uid,))
        c.commit()
    finally:
        c.close()

def mask_phone(p):
    if not p: return "بدون رقم"
    p = str(p)
    return p[:8] + "*" * 4 if len(p) > 8 else p[:4] + "*" * (len(p) - 4)

def format_size(b):
    if not b: return "0 B"
    for u in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

# ═════════════ إدارة جلسات الحسابات (Telethon Session String) ═════════════
_client_locks = {}


def save_session_file(uid, aid, sstr):
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
    except Exception:
        pass
    sfile = os.path.join(SESSIONS_DIR, f"{uid}_{aid}.session")
    try:
        with open(sfile, "w", encoding="utf-8") as f:
            f.write(sstr)
    except Exception:
        pass
    return sfile

def get_session_string(uid, aid):
    """يرجع الـ Session String المحفوظ بقاعدة البيانات (أو من الملف القديم)."""
    c = db()
    try:
        r = c.execute("SELECT session_string, session_file FROM accounts WHERE user_id=? AND account_id=?", (uid, aid)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    if r["session_string"]:
        return r["session_string"]
    sfile = r["session_file"]
    if sfile and os.path.exists(sfile):
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception:
            return None
    return None

def update_account_session(uid, aid, sstr, phone=None, fname=None, uname=None):
    sfile = save_session_file(uid, aid, sstr)
    c = db()
    try:
        c.execute("UPDATE accounts SET session_string=?, session_file=?, first_name=COALESCE(?, first_name), username=COALESCE(?, username) WHERE user_id=? AND account_id=?", (sstr, sfile, fname, uname, uid, aid))
        if phone:
            c.execute("UPDATE accounts SET phone=? WHERE user_id=? AND account_id=?", (phone, uid, aid))
        c.commit()
    finally:
        c.close()

def is_dev_session_account(aid):
    c = db()
    try:
        return c.execute("SELECT 1 FROM dev_sessions WHERE account_id=?", (aid,)).fetchone() is not None
    finally:
        c.close()

def acc_display(a):
    return a["first_name"] if a.get("first_name") else mask_phone(a.get("phone"))

def cancel_auto_leave(uid, aid):
    t = auto_leave_tasks.pop((uid, aid), None)
    if t:
        t.cancel()

async def drop_client(uid, aid):
    st = get_state(uid)
    cancel_auto_leave(uid, aid)
    remove_active_stream(uid, aid)
    cli = st["clients"].pop(aid, None)
    st["calls"].pop(aid, None)
    if cli is not None:
        try:
            await cli.disconnect()
        except Exception:
            pass

async def attach_client(uid, aid, cli):
    """يربط عميل Telethon مع PyTgCalls ويحفظه بذاكرة البوت (يستبدل القديم إن وجد)."""
    st = get_state(uid)
    old = st["clients"].get(aid)
    if old is not None and old is not cli:
        await drop_client(uid, aid)
    call = PyTgCalls(cli)
    try:
        await call.start()
    except Exception as ex:
        print(f"[PyTgCalls] start failed for {aid}: {type(ex).__name__}: {ex}")
    st["clients"][aid] = cli
    st["calls"][aid] = call
    return call

async def ensure_client(uid, aid, force=False):
    """يرجع (client, call) جاهزين للحساب. يعيد الاتصال تلقائياً بنفس الـ Session String المحفوظ.
    force=True => قطع الاتصال الحالي وإعادة تشغيله من جديد (تحديث الحساب)."""
    lock = _client_locks.setdefault((uid, aid), asyncio.Lock())
    async with lock:
        st = get_state(uid)
        if force:
            await drop_client(uid, aid)
        cli = st["clients"].get(aid)
        call = st["calls"].get(aid)
        if cli and call:
            try:
                if not cli.is_connected():
                    await cli.connect()
                if await cli.is_user_authorized():
                    return cli, call
            except Exception:
                pass
            await drop_client(uid, aid)
        sstr = get_session_string(uid, aid)
        if not sstr:
            raise RuntimeError("لا توجد Session محفوظة لهذا الحساب")
        cli = TelegramClient(StringSession(sstr), API_ID, API_HASH)
        await cli.connect()
        if not await cli.is_user_authorized():
            await cli.disconnect()
            raise RuntimeError("الجلسة منتهية أو غير صالحة")
        call = await attach_client(uid, aid, cli)
        return cli, call

def schedule_auto_leave(uid, aid, call, chat_id, duration, extra=1.5):
    cancel_auto_leave(uid, aid)
    if not duration or duration <= 0:
        return

    async def _timer():
        try:
            await asyncio.sleep(duration + extra)
        except asyncio.CancelledError:
            return
        if get_active_stream(uid, aid):
            try:
                await call.leave_call(chat_id)
            except Exception:
                pass
            remove_active_stream(uid, aid)
        auto_leave_tasks.pop((uid, aid), None)

    auto_leave_tasks[(uid, aid)] = asyncio.create_task(_timer())

async def restore_all_accounts():
    """عند تشغيل البوت: يرجّع كل الحسابات المحفوظة تلقائياً بنفس الـ Session String."""
    c = db()
    try:
        rows = c.execute("SELECT user_id, account_id, session_file, session_string FROM accounts").fetchall()
        for r in rows:  # ترحيل الجلسات القديمة (ملفات) إلى قاعدة البيانات
            if not r["session_string"] and r["session_file"] and os.path.exists(r["session_file"]):
                try:
                    with open(r["session_file"], "r", encoding="utf-8") as f:
                        s = f.read().strip()
                    if s:
                        c.execute("UPDATE accounts SET session_string=? WHERE user_id=? AND account_id=?", (s, r["user_id"], r["account_id"]))
                except Exception:
                    pass
        c.execute("DELETE FROM active_streams")  # لا يوجد بث فعلي بعد إعادة التشغيل
        c.commit()
        rows = c.execute("SELECT user_id, account_id FROM accounts").fetchall()
    finally:
        c.close()

    sem = asyncio.Semaphore(3)

    async def one(r):
        async with sem:
            try:
                await ensure_client(r["user_id"], r["account_id"])
                return True
            except Exception as ex:
                print(f"[restore] {r['user_id']}/{r['account_id']}: {type(ex).__name__}: {ex}")
                return False

    results = await asyncio.gather(*(one(r) for r in rows))
    print(f"[restore] تم تشغيل {sum(1 for x in results if x)}/{len(rows)} حساب")

# ═════════════ مساعدات: عرض الجلسات / إنهاء الاتصال الخاص / روابط call ═════════════
def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2

async def send_session_msg(e, acc, sstr):
    """يرسل الـ Session داخل اقتباس مطوي (expandable) + code للنسخ بضغطة واحدة."""
    header = f"🔑 Session — {acc_display(acc)}\n"
    footer = "\n\n⚠️ اضغط على الجلسة لنسخها. لا تشاركها مع أحد."
    text = header + sstr + footer
    off = utf16_len(header)
    ln = utf16_len(sstr)
    try:
        try:
            quote = MessageEntityBlockquote(offset=off, length=ln, collapsed=True)
        except TypeError:  # نسخة Telethon قديمة بدون collapsed
            quote = MessageEntityBlockquote(offset=off, length=ln)
        await e.respond(text, formatting_entities=[quote, MessageEntityCode(offset=off, length=ln)], parse_mode=None)
    except Exception:
        await e.respond(f"{header}`{sstr}`{footer}", parse_mode="markdown")

CALL_LINK_RE = re.compile(r'(?:https?://)?(?:www\.)?(?:t|telegram)\.me/call/([A-Za-z0-9_\-]+)', re.I)

class CallLinkUnsupported(Exception):
    pass

async def play_call_link(call, link, stream):
    """نقطة الانضمام لاتصالات t.me/call/... (اتصال جماعي مشفّر E2E خارج أي كروب/قناة).
    الانضمام يحتاج دعماً من PyTgCalls/NTgCalls؛ لم أستطع التأكد من وجوده بالنسخة المثبتة عندك،
    لذلك يرجع خطأ واضح بدل المحاولة على كروب خاطئ (وهو سبب ChatAdminRequired السابق)."""
    raise CallLinkUnsupported(
        "رابط call/ محفوظ صحيح، لكن نسخة PyTgCalls المثبتة لا تدعم الانضمام للاتصالات الجماعية المشفّرة. "
        "جرّب pip install -U py-tgcalls"
    )

def _find_phone_call_ref(obj, depth=3, seen=None):
    """احتياطي فقط: يبحث داخل كائن PyTgCalls عن (id, access_hash) لو ما وصلنا أي تحديث رسمي
    من تيليجرام يعطينا نفس القيمتين مباشرة (المسار العادي أدناه يعتمد على التحديث الرسمي)."""
    if seen is None:
        seen = set()
    if depth < 0 or obj is None or id(obj) in seen:
        return None
    seen.add(id(obj))
    iid = getattr(obj, "id", None)
    ah = getattr(obj, "access_hash", None)
    if isinstance(iid, int) and isinstance(ah, int) and iid and type(obj).__name__ != "PyTgCalls":
        return (iid, ah)
    try:
        d = vars(obj)
    except Exception:
        return None
    for v in d.values():
        if isinstance(v, dict):
            for vv in v.values():
                r = _find_phone_call_ref(vv, depth - 1, seen)
                if r:
                    return r
        elif isinstance(v, (list, tuple, set)):
            for vv in v:
                r = _find_phone_call_ref(vv, depth - 1, seen)
                if r:
                    return r
        else:
            r = _find_phone_call_ref(v, depth - 1, seen)
            if r:
                return r
    return None

async def _force_hangup(cli, call, chat_id, uid, aid, ref=None, reason=None):
    """ينهي المكالمة الخاصة فعلياً من جهتنا:
    1) phone.discardCall بالمرجع (id/access_hash) الحقيقي الجاي من تحديث تيليجرام نفسه إذا متوفر،
       وإلا يحاول يلقاه داخل كائن PyTgCalls كحل احتياطي.
    2) احتياطياً يجرب leave_call/stop_stream من PyTgCalls.
    يطبع بالكونسول تفاصيل كل محاولة فاشلة."""
    if not ref:
        try:
            ref = _find_phone_call_ref(call)
        except Exception as ex:
            print(f"[private_call] خطأ أثناء البحث عن مرجع المكالمة: {type(ex).__name__}: {ex}")

    if ref and ref[0] and ref[1]:
        call_id, access_hash = ref
        try:
            await cli(DiscardCallRequest(
                peer=InputPhoneCall(id=call_id, access_hash=access_hash),
                duration=0,
                reason=reason or PhoneCallDiscardReasonHangup(),
                connection_id=0
            ))
            print(f"[private_call] ✅ تم إنهاء المكالمة فعلياً عبر phone.discardCall (id={call_id})")
        except Exception as ex:
            print(f"[private_call] ❌ فشل phone.discardCall (id={call_id}): {type(ex).__name__}: {ex}")
    else:
        print("[private_call] ⚠️ لم يُعثر على مرجع المكالمة (id/access_hash).")

    for attempt in (
        lambda: call.leave_call(chat_id),
        lambda: call.stop_stream(chat_id),
    ):
        try:
            await attempt()
            break
        except Exception:
            continue
    remove_active_stream(uid, aid)
    auto_leave_tasks.pop((uid, aid), None)

def schedule_private_hangup(uid, aid, cli, call, chat_id, duration, max_wait_to_answer=300):
    """يراقب المكالمة الخاصة اعتماداً على تحديثات تيليجرام الحقيقية (UpdatePhoneCall) بدل توقيت ثابت:
    1) ينتظر فعلياً حتى يرد الطرف الآخر (حتى لو تأخر)، بدل افتراض مهلة رنين محدّدة سلفاً.
    2) بمجرد الرد، يبدأ عدّ مدة الفيديو من لحظة الرد الحقيقية، فينهي المكالمة بالضبط لما تخلص
       (مو قبلها بسبب تأخر الرد، ومو بعدها بوقت زيادة).
    3) إذا الطرف الثاني رفض/أنهى المكالمة هو نفسه، تيليجرام ترسل تحديث بهذا فنتوقف فوراً بدون أي أمر إضافي.
    max_wait_to_answer: سقف أمان بس (لو ما وصل أي تحديث إطلاقاً لسبب غير متوقع)، مو مهلة رنين فعلية."""
    cancel_auto_leave(uid, aid)

    answered = asyncio.Event()
    discarded = asyncio.Event()
    ref_holder = {"id": None, "access_hash": None}

    async def _on_update(update):
        pc = getattr(update, "phone_call", None)
        if pc is None:
            return
        tname = type(pc).__name__
        print(f"[private_call] 📡 تحديث حالة المكالمة: {tname} | id={getattr(pc, 'id', None)} access_hash={getattr(pc, 'access_hash', None)}")
        if tname in ("PhoneCallAccepted", "PhoneCall") and not answered.is_set():
            ref_holder["id"] = getattr(pc, "id", None)
            ref_holder["access_hash"] = getattr(pc, "access_hash", None)
            answered.set()
        elif tname == "PhoneCallDiscarded":
            discarded.set()

    cli.add_event_handler(_on_update, events.Raw(types=UpdatePhoneCall))

    async def _watch():
        try:
            try:
                waiters = [asyncio.ensure_future(answered.wait()), asyncio.ensure_future(discarded.wait())]
                done, pending = await asyncio.wait_for(
                    asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED),
                    timeout=max_wait_to_answer
                )
                for p in pending:
                    p.cancel()
            except asyncio.TimeoutError:
                pass

            if discarded.is_set() and not answered.is_set():
                print("[private_call] ℹ️ الطرف الثاني رفض/تجاهل المكالمة قبل الرد، تيليجرام أنهتها من الجهتين.")
                return

            if not answered.is_set():
                print(f"[private_call] ⚠️ لم يصل أي تحديث رد خلال {max_wait_to_answer} ثانية — محاولة إنهاء احتياطية.")
                await _force_hangup(cli, call, chat_id, uid, aid, ref=None, reason=PhoneCallDiscardReasonMissed())
                return

            print(f"[private_call] ✅ تم رصد الرد (id={ref_holder['id']})، بدء عدّ مدة الفيديو ({duration} ثانية).")
            elapsed = 0
            while elapsed < duration + 1:
                await asyncio.sleep(1)
                elapsed += 1
                if not get_active_stream(uid, aid):
                    print(f"[private_call] ℹ️ أوقف يدوياً بعد {elapsed}ث من الرد.")
                    return
                if discarded.is_set():
                    print(f"[private_call] ℹ️ الطرف الثاني أنهى المكالمة بنفسه بعد {elapsed}ث من الرد.")
                    return
            print(f"[private_call] ⏱️ انتهت مدة الفيديو ({duration}ث) — جاري إنهاء المكالمة الآن.")
        except asyncio.CancelledError:
            raise
        finally:
            try:
                cli.remove_event_handler(_on_update, events.Raw)
            except Exception:
                pass
        await _force_hangup(cli, call, chat_id, uid, aid,
                             ref=(ref_holder["id"], ref_holder["access_hash"]),
                             reason=PhoneCallDiscardReasonHangup())

    auto_leave_tasks[(uid, aid)] = asyncio.create_task(_watch())

# ═════════════ تكرار الكام السريع (فتح / إغلاق / فلاش) ═════════════
FLASH_ON_SECONDS = 0.01    # مدة ظهور الكام بكل لمحة
FLASH_OFF_SECONDS = 0.01   # مدة الإغلاق بين اللمحات
RESTART_LATENCY = 0.35     # وقت إعادة تشغيل البث التقريبي: نقدّم الصوت بمقداره عشان ما يرجع/يقفز الصوت

cam_state = {}      # (uid, aid) -> True = الكام مفتوحة / False = مغلقة
cam_locks = {}      # قفل لكل حساب عشان الضغطات السريعة ما تتداخل
_asset_locks = {}   # قفل تجهيز ملف الفلاش
flash_running = set()   # الحسابات اللي فلاشها شغال حالياً
flash_stop = set()      # طلبات إيقاف التكرار
flash_pending = set()   # مستخدمين جاري انضمام حساباتهم قبل بدء الفلاش
stop_requested = set()  # مستخدمين ضغطوا إيقاف أثناء الانضمام

def _cam_lock(uid, aid):
    key = (uid, aid)
    if key not in cam_locks:
        cam_locks[key] = asyncio.Lock()
    return cam_locks[key]

def flash_asset_path(vid):
    return vid["file_path"] + ".flash.mp4"

async def ensure_flash_asset(vid):
    """يجهّز (مرة وحدة) ملف فيه صوت الملف الأصلي + فيديو أسود صغير جداً. يشتغل مع ملفات الصوت والفيديو."""
    out = flash_asset_path(vid)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    lock = _asset_locks.setdefault(out, asyncio.Lock())
    async with lock:
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        tmp = out + ".tmp.mp4"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:r=2",
                "-i", vid["file_path"],
                "-map", "0:v", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-pix_fmt", "yuv420p", "-g", "2",
                "-c:a", "aac", "-b:a", "192k", "-shortest", tmp,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(tmp):
                print(f"[cam] ❌ فشل تجهيز ملف الفلاش: {(err or b'').decode(errors='ignore')[:300]}")
                return None
            os.replace(tmp, out)
            return out
        except Exception as ex:
            print(f"[cam] ❌ فشل تجهيز ملف الفلاش: {type(ex).__name__}: {ex}")
            return None
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

async def prewarm_flash_assets(uid):
    """يجهّز ملفات الفلاش بالخلفية أول ما يبدأ البث عشان أول ضغطة تكون فورية."""
    for s in get_all_active_streams_for_user(uid):
        vid = get_video(uid, s["account_id"])
        if vid and os.path.exists(vid["file_path"]):
            try:
                await ensure_flash_asset(vid)
            except Exception:
                pass

def cam_menu_buttons(uid, back_data=b"stream_menu"):
    rows = []
    streams = get_all_active_streams_for_user(uid)
    if streams:
        any_run = any((uid, s["account_id"]) in flash_running for s in streams)
        rows.append([Button.inline("⏹ إيقاف Flash" if any_run else "⚡ Flash الجميع",
                                   b"cam_flash_all", style='danger' if any_run else 'success')])
    for s in streams:
        aid = s["account_id"]
        on = cam_state.get((uid, aid), False)
        acc = get_account(uid, aid)
        name = (acc["first_name"] if acc and acc.get("first_name") else str(aid))
        lbl = f"📷 {name}: {'🟢 مفتوحة' if on else '🔴 مغلقة'}"
        rows.append([
            Button.inline(lbl, f"cam_toggle_{aid}".encode(), style='danger' if on else 'success'),
            Button.inline("⏹ إيقاف الفلاش" if (uid, aid) in flash_running else "⚡ فلاش متكرر",
                          f"cam_flash_{aid}".encode(),
                          style='danger' if (uid, aid) in flash_running else 'primary'),
        ])
    if streams:
        rows.append([Button.inline("📷 فتح / إغلاق الكل", b"cam_toggle_all", style='primary')])
    rows.append([Button.inline("رجوع", back_data, style='danger')])
    return rows

async def _cam_play(uid, aid, mode, silent_lock=False, already_on=False):
    """mode: off = بدون كام | full = كام كاملة | flash = كام صغيرة ثم تنغلق. يرجع (ok, msg)."""
    s = get_active_stream(uid, aid)
    if not s:
        return False, "لا يوجد بث شغال"
    call = get_state(uid)["calls"].get(aid)
    if not call:
        return False, "الحساب غير متصل"
    chat_id = s["chat_id"]
    if chat_id == 0:
        return False, "رابط call ما يدعم التبديل"
    vid = get_video(uid, aid)
    if not vid or not os.path.exists(vid["file_path"]):
        return False, "ملف الفيديو غير موجود"
    is_audio = vid.get("media_type") == "audio"

    def _offset():
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(s["started_at"])).total_seconds())
        except Exception:
            return 0.0

    async def _play(path, mode_kind):
        if not get_active_stream(uid, aid):
            return None   # البث انتهى/اتوقف: لا نشغّل شي عشان ما يرجع للاتصال
        kwargs = {"audio_parameters": AudioQuality.STUDIO}
        if mode_kind == "off":
            kwargs["video_flags"] = MediaStream.Flags.IGNORE
        elif mode_kind == "flash":
            kwargs["video_parameters"] = VideoQuality.SD_360p
        else:
            kwargs["video_parameters"] = VideoQuality.HD_720p
        off = _offset() + RESTART_LATENCY
        last = None
        for use_off in ((True, False) if off > 0.5 else (False,)):
            try:
                kw = dict(kwargs)
                if use_off:
                    kw["ffmpeg_parameters"] = f"-ss {off:.3f}"
                await call.play(chat_id, MediaStream(path, **kw))
                return None
            except Exception as ex:
                last = ex
                print(f"[cam] ❌ فشل ({mode_kind}) لحساب {aid}: {type(ex).__name__}: {ex}")
        return last

    async with _cam_lock(uid, aid):
        if mode == "flash":
            asset = await ensure_flash_asset(vid)
            if not asset:
                return False, "فشل تجهيز ملف الكام"
            key = (uid, aid)
            flash_stop.discard(key)
            err = None
            skip_on = already_on   # الحساب توه صاعد بكام الفلاش، فنبدأ بالإغلاق بدون إعادة تشغيل
            while True:  # تكرار تلقائي لين تضغط إيقاف أو ينتهي البث
                if key in flash_stop or not get_active_stream(uid, aid):
                    break
                if not skip_on:
                    err = await _play(asset, "flash")
                    if err:
                        break
                skip_on = False
                await asyncio.sleep(FLASH_ON_SECONDS)
                err = await _play(asset, "off")   # نفس الملف الخفيف: إعادة التشغيل أسرع والصوت أقل تقطيعاً
                if err:
                    break
                if key in flash_stop:
                    break
                await asyncio.sleep(FLASH_OFF_SECONDS)
            flash_stop.discard(key)
            cam_state[key] = False
            return (err is None), ("تم" if err is None else type(err).__name__)

        if mode == "full":
            if is_audio:
                path = await ensure_flash_asset(vid)
                if not path:
                    return False, "فشل تجهيز ملف الكام"
                err = await _play(path, "flash")
            else:
                err = await _play(vid["file_path"], "full")
            if err:
                return False, type(err).__name__
            cam_state[(uid, aid)] = True
            return True, "تم"

        err = await _play(vid["file_path"], "off")
        if err:
            return False, type(err).__name__
        cam_state[(uid, aid)] = False
        return True, "تم"

flash_pending_n = {}

def _pend_add(uid, n):
    flash_pending_n[uid] = flash_pending_n.get(uid, 0) + n
    flash_pending.add(uid)

def _pend_done(uid):
    n = flash_pending_n.get(uid, 1) - 1
    if n <= 0:
        flash_pending_n.pop(uid, None)
        flash_pending.discard(uid)
    else:
        flash_pending_n[uid] = n

async def flash_join_account(uid, aid, ch):
    """انضمام سريع ومستقل للفلاش: بدون بوابة تزامن وبدون انتظار باقي الحسابات.
    يدخل الاتصال مباشرة بفيديو الفلاش الصغير (ما يشغّل الفيديو الأصلي أولاً)."""
    chat_id = ch["chat_id"]
    title = ch["chat_title"]
    saved_link = ch.get("invite_link")
    if chat_id == 0:
        return False, "الفلاش ما يدعم رابط call"
    acc = get_account(uid, aid)
    if not acc:
        return False, "الحساب غير موجود"
    vid = get_video(uid, aid)
    if not vid or not os.path.exists(vid["file_path"]):
        return False, "لا يوجد فيديو مرفق للحساب"

    asset_task = asyncio.create_task(ensure_flash_asset(vid))   # يتجهز أثناء الانضمام
    try:
        cli, call = await ensure_client(uid, aid)
    except Exception as ex:
        asset_task.cancel()
        return False, f"فشل تشغيل عميل تيليجرام: {ex}"

    joined = False
    try:
        await cli(GetParticipantRequest(channel=chat_id, participant=aid))
        joined = True
    except Exception:
        joined = False

    join_err = None
    if not joined and saved_link:
        try:
            if "t.me/+" in saved_link or "joinchat" in saved_link or len(saved_link) > 25:
                m = re.search(r't\.me/(?:joinchat/)?\+?([A-Za-z0-9_\-]+)', saved_link)
                if m:
                    try:
                        await cli(ImportChatInviteRequest(m.group(1)))
                    except UserAlreadyParticipantError:
                        pass
                    joined = True
            else:
                m = re.search(r't\.me/([A-Za-z0-9_]+)', saved_link)
                if m:
                    ent = await cli.get_entity("@" + m.group(1))
                    try:
                        await cli(JoinChannelRequest(ent))
                    except UserAlreadyParticipantError:
                        pass
                    joined = True
        except Exception as ex:
            join_err = ex
    if not joined:
        asset_task.cancel()
        if join_err:
            return False, f"تعذر الانضمام للمجموعة: {type(join_err).__name__}"
        return False, "الحساب ليس عضواً في المجموعة"

    asset = await asset_task
    if not asset:
        return False, "فشل تجهيز ملف الكام"

    last_err = None
    for attempt in range(2):
        try:
            await call.play(chat_id, MediaStream(asset, audio_parameters=AudioQuality.STUDIO,
                                                 video_parameters=VideoQuality.SD_360p))
            set_active_stream(uid, aid, chat_id, title, vid["id"])
            schedule_auto_leave(uid, aid, call, chat_id, get_video_duration(vid["file_path"]))
            cam_state[(uid, aid)] = True
            return True, "تم"
        except Exception as ex:
            last_err = ex
            print(f"[flash_join] ❌ محاولة {attempt + 1} فشلت لحساب {aid}: {type(ex).__name__}: {ex}")
            if attempt == 0:
                await asyncio.sleep(0.5)
    return False, f"خطأ الانضمام: {type(last_err).__name__}"

async def hard_stop_account(uid, aid, call, chat_id):
    """ينزّل الحساب نهائياً حتى لو الفلاش/الكام شغالين، وما يخليه يرجع للاتصال."""
    key = (uid, aid)
    remove_active_stream(uid, aid)   # أي تشغيل جديد يتوقف فوراً (الحارس)
    cancel_auto_leave(uid, aid)
    flash_stop.add(key)

    async def _leave():
        if call is None:
            return
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass

    await _leave()                    # نزول فوري
    lock = _cam_lock(uid, aid)
    got = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=4)   # ننتظر نهاية أي تشغيل جاري
        got = True
    except Exception:
        pass
    try:
        await _leave()                # نزول ثاني يلتقط أي رجوع متأخر
        await asyncio.sleep(0.3)
        await _leave()
    finally:
        if got:
            lock.release()
        flash_stop.discard(key)
        flash_running.discard(key)
        cam_state.pop(key, None)

async def toggle_camera(uid, aid):
    """يبدّل الكام (فتح/إغلاق). يرجع (ok, is_on_now, msg)."""
    turn_on = not cam_state.get((uid, aid), False)
    ok, msg = await _cam_play(uid, aid, "full" if turn_on else "off")
    return ok, (turn_on if ok else not turn_on), msg

async def flash_camera(uid, aid, already_on=False):
    """فلاش متكرر (فتح/إغلاق عدة مرات). إذا كان شغال وضغطت مرة ثانية يوقفه."""
    key = (uid, aid)
    if key in flash_running:
        flash_stop.add(key)
        return True, "تم إيقاف التكرار"
    flash_running.add(key)
    try:
        return await _cam_play(uid, aid, "flash", already_on=already_on)
    finally:
        flash_running.discard(key)

async def respond(event, text, buttons=None, **kw):
    try:
        return await event.respond(text, buttons=buttons, reply_to=event.message.id, **kw)
    except Exception:
        return await event.respond(text, buttons=buttons, **kw)

async def edit(event, text, buttons=None, **kw):
    try:
        await event.edit(text, buttons=buttons, **kw)
    except MessageNotModifiedError:
        pass

async def reject_if_not_subscribed(event):
    if is_subscriber(event.sender_id):
        return False
    btns = [[Button.url("المطور", DEV_LINK, style='primary')]]
    await respond(event, "لا يمكنك استخدام البوت. أنت ما مشترك!", buttons=btns)
    return True

def delete_video_file(uid, aid):
    vid = get_video(uid, aid)
    if vid and vid.get("file_path") and os.path.exists(vid["file_path"]):
        try:
            os.remove(vid["file_path"])
        except Exception:
            pass
        try:
            os.remove(vid["file_path"] + ".flash.mp4")
        except Exception:
            pass
    remove_video(uid, aid)

def get_video_duration(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0

def main_menu():
    return [
        [Button.inline("اضف حساب", b"add_account", style='success')],
        [Button.inline("فحص حساباتي", b"check_accounts", style='primary')],
        [Button.inline("حذف حساب", b"del_account", style='danger')],
        [Button.inline("🔑 جلسات الحسابات (استخراج / تحديث)", b"sessions_menu", style='primary')],
        [Button.inline("📂 ملف موحد لكل الحسابات", b"add_unified_video", style='primary')],
        [Button.inline("🎥 فيديو مخصص لكل حساب", b"add_video_menu", style='primary')],
        [Button.inline("رابط الاتصال (كروب / قناة / call)", b"set_chat", style='primary')],
        [Button.inline("📞 الاتصال الخاص (يوزر / ID)", b"private_call_menu", style='success')],
        [Button.inline("🧹 مسح الفيديوهات والروابط", b"clear_media", style='danger')],
        [Button.inline("قائمة البث والتحكم", b"stream_menu", style='success')],
    ]

def back_button():
    return [[Button.inline("رجوع", b"back_main", style='danger')]]

MAIN_TEXT = "اهلا بك في بوت كام - إدارة البث المتعدد"

async def resolve_chat(uid, link):
    link = link.strip()
    # رابط اتصال جماعي t.me/call/... (ليس كروب ولا قناة) — لا يُعامل كيوزر "call"
    m_call = CALL_LINK_RE.search(link)
    if m_call:
        return 0, "اتصال جماعي (رابط call)", f"https://t.me/call/{m_call.group(1)}"
    st = get_state(uid)
    cli = None
    for c_ in list(st["clients"].values()):
        try:
            if c_.is_connected():
                cli = c_
                break
        except Exception:
            pass
    if not cli:
        accs = get_accounts(uid)
        if not accs:
            return None, "اضف حساب اولاً", None
        try:
            cli, _ = await ensure_client(uid, accs[0]["account_id"])
        except Exception as ex:
            return None, f"تعذر تشغيل الحساب: {ex}", None

    if not cli:
        return None, "اضف حساب اولاً", None

    try:
        cid = int(link)
        return cid, str(cid), None
    except ValueError:
        pass

    if link.startswith("@"):
        try:
            ent = await cli.get_entity(link)
            try:
                await cli(JoinChannelRequest(ent))
            except Exception:
                pass
            return get_peer_id(ent), getattr(ent, 'title', None) or link, None
        except Exception as ex:
            return None, f"تعذر: {type(ex).__name__}", None

    if "t.me/" in link or "telegram.me/" in link:
        if "t.me/+" in link or "telegram.me/+" in link or "joinchat" in link:
            m = re.search(r't\.me/(?:joinchat/)?\+?([A-Za-z0-9_\-]+)', link)
            if not m:
                return None, "رابط دعوة غير صحيح", None
            invite_code = m.group(1)
            try:
                try:
                    await cli(ImportChatInviteRequest(invite_code))
                except UserAlreadyParticipantError:
                    pass
                except Exception:
                    pass

                info = await cli(CheckChatInviteRequest(invite_code))
                chat = getattr(info, 'chat', None)
                if chat:
                    return get_peer_id(chat), getattr(chat, 'title', None) or str(chat.id), link
                return None, "تم الانضمام لكن تعذر استخراج ID الكروب", None
            except Exception as ex:
                return None, f"تعذر الانضمام بالرابط: {type(ex).__name__}", None

        m = re.search(r't\.me/([A-Za-z0-9_]+)', link)
        if m:
            code = m.group(1)
            try:
                ent = await cli.get_entity("@" + code)
                try:
                    await cli(JoinChannelRequest(ent))
                except UserAlreadyParticipantError:
                    pass
                except Exception:
                    pass
                return get_peer_id(ent), getattr(ent, 'title', None) or code, link
            except Exception as ex:
                return None, f"تعذر: {type(ex).__name__}", None

    return None, "صيغة غير معروفة", None

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def cmd_start(e):
    if not e.is_private:
        return
    sender = await e.get_sender()
    add_user(e.sender_id, getattr(sender, 'username', None) or "", sender.first_name or "")
    if await reject_if_not_subscribed(e):
        return
    await respond(e, MAIN_TEXT, buttons=main_menu())

@bot.on(events.CallbackQuery(data=b"back_main"))
async def cb_back(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    try:
        await e.edit(MAIN_TEXT, buttons=main_menu())
    except MessageNotModifiedError:
        pass
    await e.answer()

@bot.on(events.CallbackQuery(data=b"clear_media"))
async def cb_clear_media(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    clear_media_and_chats(e.sender_id)
    await edit(e, "🧹 تم مسح كافة الفيديوهات والروابط المخزنة بنجاح!", buttons=back_button())
    await e.answer("تم المسح")

@bot.on(events.CallbackQuery(data=b"add_account"))
async def cb_add_account(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    n = count_accounts(e.sender_id)
    mx = max_accounts_for(e.sender_id)
    if n >= mx:
        return await e.answer(f"وصلت للحد الأقصى ({mx})", alert=True)
    btns = [
        [Button.inline("برقم الهاتف", b"acc_phone", style='success')],
        [Button.inline("Session String", b"acc_session", style='primary')],
        [Button.inline("رجوع", b"back_main", style='danger')],
    ]
    await edit(e, f"اضف حساب\nلديك {n}/{mx}", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(data=b"acc_phone"))
async def cb_acc_phone(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "phone"}
    await edit(e, "ارسل رقم الهاتف مع رمز الدولة\nمثال: +9647701234567", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"acc_session"))
async def cb_acc_session(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "session"}
    await edit(e, "ارسل Session String الخاص بالحساب", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"check_accounts"))
async def cb_check(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(e.sender_id)
    if not accs:
        return await edit(e, "لا يوجد حسابات مضافة", buttons=back_button())
    txt = "حساباتك المضافة:\n\n"
    for i, a in enumerate(accs, 1):
        cli = get_state(e.sender_id)["clients"].get(a["account_id"])
        status = "متصل وعملي" if cli and cli.is_connected() else "غير متصل"
        vid = get_video(e.sender_id, a["account_id"])
        vid_status = f"فيديو مرفق ({vid['file_name']})" if vid else "لا يوجد فيديو مرفق"
        uname = f"@{a['username']}" if a['username'] else "-"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        txt += f"{i}. {acc_name}\n   الرقم: {mask_phone(a['phone'])}\n   اليوزر: {uname}\n   الحالة: {status}\n   {vid_status}\n\n"
    await edit(e, txt, buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"del_account"))
async def cb_del(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(e.sender_id)
    if not accs:
        return await edit(e, "لا يوجد حسابات", buttons=back_button())
    btns = []
    for a in accs:
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        lbl = f"{acc_name}"
        btns.append([Button.inline(lbl, f"del_acc_{a['account_id']}".encode(), style='danger')])
    btns.append([Button.inline("حذف الكل", b"del_all_acc", style='danger')])
    btns.append([Button.inline("رجوع", b"back_main", style='primary')])
    await edit(e, "اختر الحساب المراد حذفه:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^del_acc_(\d+)$"))
async def cb_del_one(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    uid = e.sender_id
    st = get_state(uid)
    if aid in st["clients"]:
        try:
            await st["clients"][aid].disconnect()
        except Exception:
            pass
        st["clients"].pop(aid, None)
        st["calls"].pop(aid, None)
    
    delete_video_file(uid, aid)
    acc = get_account(uid, aid)
    if acc and acc.get("session_file") and os.path.exists(acc["session_file"]):
        try:
            os.remove(acc["session_file"])
        except Exception:
            pass
    remove_account(uid, aid)
    await edit(e, "تم حذف الحساب وفيديو المرتبط به بنجاح", buttons=back_button())
    await e.answer("تم الحذف")

@bot.on(events.CallbackQuery(data=b"del_all_acc"))
async def cb_del_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    st = get_state(uid)
    for aid, cli in list(st["clients"].items()):
        try:
            await cli.disconnect()
        except Exception:
            pass
        delete_video_file(uid, aid)
    st["clients"].clear()
    st["calls"].clear()
    for a in get_accounts(uid):
        if a.get("session_file") and os.path.exists(a["session_file"]):
            try:
                os.remove(a["session_file"])
            except Exception:
                pass
    remove_all_accounts(uid)
    await edit(e, "تم حذف جميع الحسابات والفيديوهات", buttons=back_button())
    await e.answer("تم")

@bot.on(events.CallbackQuery(data=b"add_unified_video"))
async def cb_add_unified_video(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "يجب إضافة حسابات أولاً لكي تتمكن من رفع الملف الموحد!", buttons=back_button())
    login_pending[uid] = {"step": "unified_video"}
    await edit(e, "📂 أرسل الفيديو أو الملف الصوتي ليعمل بشكل موحد لكل الحسابات:", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"add_video_menu"))
async def cb_add_video_menu(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "يجب إضافة حسابات أولاً لكي تتمكن من رفع فيديو لكل حساب!", buttons=back_button())
    
    btns = []
    for a in accs:
        vid = get_video(uid, a["account_id"])
        has_v = "✅ (مرفق فيديو)" if vid else "❌ (بدون فيديو)"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        lbl = f"{acc_name} {has_v}"
        btns.append([Button.inline(lbl, f"select_vid_acc_{a['account_id']}".encode(), style='primary')])
    btns.append([Button.inline("رجوع", b"back_main", style='danger')])
    await edit(e, "اختر الحساب الذي تريد رفع فيديو خاص به:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^select_vid_acc_(\d+)$"))
async def cb_select_vid_acc(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    login_pending[e.sender_id] = {"step": "video", "target_account_id": aid}
    await edit(e, "أرسل الآن الفيديو (MP4) المخصص لهذا الحساب:", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"set_chat"))
async def cb_set_chat(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "chat"}
    await edit(e, "🔗 **أرسل رابط الكروب أو القناة (أو رابط الدعوة مثل t.me/+...) أو رابط اتصال جماعي t.me/call/...:**", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"private_call_menu"))
async def cb_private_call_menu(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(e.sender_id)
    if not accs:
        return await e.answer("لا توجد حسابات مضافة!", alert=True)
    btns = []
    for a in accs:
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        btns.append([Button.inline(f"الاتصال عبر: {acc_name}", f"priv_call_acc_{a['account_id']}".encode(), style='success')])
    btns.append([Button.inline("رجوع", b"back_main", style='danger')])
    await edit(e, "اختر الحساب الذي تريد بدء الاتصال الخاص به:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^priv_call_acc_(\d+)$"))
async def cb_priv_call_acc(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    login_pending[e.sender_id] = {"step": "private_call_target", "target_aid": aid}
    await edit(e, "👤 أرسل الآن **يوزر المستخدم** أو **الـ ID العددي** لبدء المكالمة الصوتية وبث الفيديو فوراً في الاتصال الخاص:", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"stream_menu"))
async def cb_stream_menu(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "لا توجد حسابات مضافة!", buttons=back_button())
    
    ch = get_chat(uid)
    chat_status = f"رابط الاتصال الحالي: {ch['chat_title']}" if ch else "❌ لم يتم تعيين رابط الاتصال بعد!"

    c = db()
    try:
        active_rows = c.execute("SELECT account_id FROM active_streams WHERE user_id=?", (uid,)).fetchall()
        active_ids = {r["account_id"] for r in active_rows}
    finally:
        c.close()

    btns = []
    for a in accs:
        aid = a["account_id"]
        status_txt = "🟢 يبث حالياً" if aid in active_ids else "🔴 متوقف"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        btns.append([Button.inline(f"تحكم بالحساب: {acc_name} [{status_txt}]", f"control_acc_{aid}".encode(), style='primary')])
    
    btns.append([Button.inline("🧹 مسح الفيديوهات والروابط", b"clear_media", style='danger')])
    btns.append([Button.inline("⚡ تشغيل فوري للجميع (بالصوت الأصلي)", b"start_all_normal", style='success')])
    _any_flash = any((uid, _i) in flash_running for _i in active_ids)
    btns.append([Button.inline("⏹ إيقاف Flash" if _any_flash else "⚡ Flash الجميع", b"menu_flash_all", style='danger' if _any_flash else 'primary')])
    btns.append([Button.inline("🛑 إيقاف كل الحسابات معاً", b"stop_all_streams", style='danger')])
    btns.append([Button.inline("رجوع", b"back_main", style='danger')])
    
    await edit(e, f"التحكم في البث:\n{chat_status}\n\nاختر الحساب للتحكم المفرد أو شغل الكل:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(data=b"list_dev_sessions_user"))
async def cb_list_dev_sessions_user(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    c = db()
    try:
        dev_accs = c.execute("SELECT * FROM dev_sessions").fetchall()
    finally:
        c.close()
    
    if not dev_accs:
        return await edit(e, "⚠️ لا توجد حسابات مطور مضافة حالياً من قبل الإدارة.", buttons=[[Button.inline("رجوع", b"stream_menu", style='danger')]])
    
    btns = []
    for da in dev_accs:
        da_id = da["account_id"]
        da_name = da["first_name"] or "حساب مطور"
        btns.append([Button.inline(f"👤 {da_name} (متصل)", f"add_dev_acc_to_my_{da_id}".encode(), style='success')])
    
    btns.append([Button.inline("رجوع", b"stream_menu", style='danger')])
    await edit(e, "🔑 **حسابات المطور الجاهزة:**\nاختر الحساب الذي تريد إضافته إلى حساباتك:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^add_dev_acc_to_my_(\d+)$"))
async def cb_add_dev_acc_to_my(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    da_id = int(e.pattern_match.group(1))

    c = db()
    try:
        row = c.execute("SELECT * FROM dev_sessions WHERE account_id=?", (da_id,)).fetchone()
    finally:
        c.close()

    if not row:
        return await e.answer("الحساب غير موجود!", alert=True)
    if get_account(uid, da_id):
        return await e.answer("هذا الحساب مضاف لديك مسبقاً!", alert=True)

    sstr = row["session_string"]
    fname = row["first_name"]
    try:
        cli = TelegramClient(StringSession(sstr), API_ID, API_HASH)
        await cli.connect()
        if not await cli.is_user_authorized():
            await cli.disconnect()
            return await e.answer("جلسة حساب المطور منتهية!", alert=True)
        me = await cli.get_me()
        sfile = save_session_file(uid, da_id, sstr)
        ok = add_account(uid, da_id, "", me.first_name or fname, me.username or "", sfile, sstr)
        if ok == "exists":
            await cli.disconnect()
            return await e.answer("هذا الحساب مضاف لديك مسبقاً!", alert=True)
        await attach_client(uid, da_id, cli)
        await e.answer("✅ تم إضافة الحساب بنجاح!", alert=True)
        await edit(e, f"✅ تم إضافة حساب المطور ({me.first_name}) إلى حساباتك بنجاح وبصورة طبيعية!", buttons=back_button())
    except Exception as ex:
        await e.answer(f"فشل ربط الحساب: {type(ex).__name__}", alert=True)

@bot.on(events.CallbackQuery(pattern=rb"^control_acc_(\d+)$"))
async def cb_control_acc(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    acc = get_account(uid, aid)
    if not acc:
        return await e.answer("الحساب غير موجود", alert=True)
    
    active = get_active_stream(uid, aid)
    vid = get_video(uid, aid)
    
    status_txt = f"البث الحالي: يبث في ({active['chat_title']})" if active else "الحالة: متوقف"
    vid_txt = f"الفيديو: {vid['file_name']}" if vid else "⚠️ تنبيه: لم ترفع فيديو لهذا الحساب بعد!"
    acc_name = acc['first_name'] if acc['first_name'] else mask_phone(acc['phone'])

    btns = [
        [Button.inline("▶️ بدء البث", f"start_one_norm_{aid}".encode(), style='success')],
        [Button.inline("⏹️ إيقاف البث لهذا الحساب", f"stop_one_{aid}".encode(), style='danger')],
        [Button.inline("🔄 تحديث الحساب (بنفس الـ Session)", f"sess_ref_{aid}".encode(), style='primary')],
        [Button.inline("رجوع للقائمة", b"stream_menu", style='primary')]
    ]
    await edit(e, f"حساب: {acc_name}\nالرقم: {mask_phone(acc['phone'])}\n{vid_txt}\n{status_txt}", buttons=btns)
    await e.answer()

# ═════════════ قائمة جلسات الحسابات ═════════════
def sess_back():
    return [[Button.inline("رجوع", b"sess_menu_only", style='danger')]]

def sess_menu_buttons():
    return [
        [Button.inline("📤 إعادة إرسال جلساتي", b"sessions_menu", style='success')],
        [Button.inline("🔄 تحديث حساب (بنفس الـ Session)", b"sess_refresh_list", style='primary')],
        [Button.inline("🔄 تحديث كل الحسابات", b"sess_refresh_all", style='primary')],
        [Button.inline("رجوع", b"back_main", style='danger')],
    ]

SESS_MENU_TEXT = "🔑 جلسات الحسابات\n\nجلساتك محفوظة داخل البوت وترجع تلقائياً بعد أي إعادة تشغيل.\nإذا صارت مشكلة بحساب اضغط تحديث ويتصل بنفس الـ Session."

@bot.on(events.CallbackQuery(data=b"sess_menu_only"))
async def cb_sess_menu_only(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    await edit(e, SESS_MENU_TEXT, buttons=sess_menu_buttons())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"sessions_menu"))
async def cb_sessions_menu(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    await e.answer()
    accs = get_accounts(uid)
    sent = 0
    for a in accs:
        # جلسات حسابات المطور لا تُعرض للمشتركين
        if not is_dev(uid) and is_dev_session_account(a["account_id"]):
            continue
        sstr = get_session_string(uid, a["account_id"])
        if not sstr:
            continue
        await send_session_msg(e, a, sstr)
        sent += 1
    if not sent:
        await e.respond("لا توجد جلسات متاحة للعرض (أضف حساباً أولاً).")
    await e.respond(SESS_MENU_TEXT, buttons=sess_menu_buttons())
    try:
        await e.delete()
    except Exception:
        pass

@bot.on(events.CallbackQuery(data=b"sess_refresh_list"))
async def cb_sess_refresh_list(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "لا يوجد حسابات مضافة", buttons=sess_back())
    btns = [[Button.inline(f"🔄 {acc_display(a)}", f"sess_ref_{a['account_id']}".encode(), style='primary')] for a in accs]
    btns.append([Button.inline("رجوع", b"sess_menu_only", style='danger')])
    await edit(e, "اختر الحساب المراد تحديثه:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^sess_ref_(\d+)$"))
async def cb_sess_ref(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    acc = get_account(uid, aid)
    if not acc:
        return await e.answer("الحساب غير موجود", alert=True)
    await e.answer("⏳ جاري التحديث...")
    try:
        await ensure_client(uid, aid, force=True)
        txt = f"✅ تم تحديث الحساب ({acc_display(acc)}) بنفس الـ Session وهو متصل الآن."
    except Exception as ex:
        txt = (f"❌ فشل تحديث ({acc_display(acc)}): {ex}\n\n"
               "إذا كانت الجلسة منتهية: أضف الحساب من جديد (برقم الهاتف أو Session String) وسيتحدث تلقائياً بدون تكرار.")
    await edit(e, txt, buttons=sess_back())

@bot.on(events.CallbackQuery(data=b"sess_refresh_all"))
async def cb_sess_refresh_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "لا يوجد حسابات مضافة", buttons=sess_back())
    await e.answer("⏳ جاري تحديث كل الحسابات...")

    async def one(a):
        try:
            await ensure_client(uid, a["account_id"], force=True)
            return True, ""
        except Exception as ex:
            return False, str(ex)

    outs = await asyncio.gather(*(one(a) for a in accs))
    lines = []
    for a, (ok, err) in zip(accs, outs):
        lines.append(f"{acc_display(a)}: " + ("🟢 متصل" if ok else f"❌ {err}"))
    await edit(e, "🔄 نتيجة التحديث:\n\n" + "\n".join(lines), buttons=sess_back())

@bot.on(events.NewMessage())
async def on_message(e):
    if not e.is_private or e.out:
        return
    if e.text and e.text.startswith("/"):
        return

    uid = e.sender_id
    if not is_subscriber(uid):
        return
    lp = login_pending.get(uid)
    if not lp:
        return
    step = lp.get("step")

    if step == "phone":
        phone = (e.text or "").strip()
        if not phone.startswith("+") or len(phone) < 8:
            return await respond(e, "رقم غير صالح. ابدأ بـ +")
        try:
            cli = TelegramClient(StringSession(), API_ID, API_HASH)
            await cli.connect()
            res = await cli.send_code_request(phone)
            lp["phone"] = phone
            lp["client"] = cli
            lp["phone_code_hash"] = res.phone_code_hash
            lp["step"] = "code"
            await respond(e, "ارسل الكود المرسل اليك من تيليجرام:")
        except Exception as ex:
            await respond(e, f"خطأ: {type(ex).__name__}")
        return

    if step == "code":
        code = (e.text or "").strip().replace(" ", "")
        cli = lp.get("client")
        phone = lp.get("phone")
        ph = lp.get("phone_code_hash")
        if not cli:
            login_pending.pop(uid, None)
            return await respond(e, "انتهت الجلسة، اعد المحاولة")
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=ph)
        except SessionPasswordNeededError:
            lp["step"] = "password"
            return await respond(e, "الحساب محمي بتحقق بخطوتين (كلمة مرور). ارسلها:")
        except PhoneCodeInvalidError:
            return await respond(e, "الكود غير صحيح، أعد إرساله:")
        except Exception as ex:
            return await respond(e, f"خطأ: {type(ex).__name__}")
        await finish_login(e, cli, phone, uid)
        return

    if step == "password":
        pwd = (e.text or "").strip()
        cli = lp.get("client")
        phone = lp.get("phone")
        try:
            await cli.sign_in(password=pwd)
        except Exception as ex:
            return await respond(e, f"كلمة المرور خاطئة أو حدث خطأ: {type(ex).__name__}")
        await finish_login(e, cli, phone, uid)
        return

    if step == "session":
        sstr = (e.text or "").strip()
        try:
            cli = TelegramClient(StringSession(sstr), API_ID, API_HASH)
            await cli.connect()
            if not await cli.is_user_authorized():
                await cli.disconnect()
                return await respond(e, "الجلسة غير صالحة")
            me = await cli.get_me()
            await finish_login_with_client(e, cli, None, me, uid, show_session=True)
        except Exception as ex:
            return await respond(e, f"خطأ: {type(ex).__name__}")
        return

    if step == "private_call_target":
        target_str = (e.text or "").strip()
        aid = lp.get("target_aid")
        login_pending.pop(uid, None)

        msg = await respond(e, f"📞 جاري الاتصال الصوتي بالخاص بـ ( {target_str} ) وبدء البث...")

        async def _drop_msg():
            try:
                await msg.delete()
            except Exception:
                pass

        vid = get_video(uid, aid)
        if not vid or not os.path.exists(vid["file_path"]):
            await _drop_msg()
            return await respond(e, "❌ تنبيه: لم ترفع فيديو لهذا الحساب بعد!")

        try:
            cli, call = await ensure_client(uid, aid)
        except Exception as ex:
            await _drop_msg()
            return await respond(e, f"❌ فشل تشغيل الحساب للاتصال: {ex}")

        try:
            entity = None
            if target_str.isdigit() or (target_str.startswith("-") and target_str[1:].isdigit()):
                entity_id = int(target_str)
                try:
                    entity = await cli.get_entity(entity_id)
                except Exception:
                    async for dialog in cli.iter_dialogs(limit=200):
                        if dialog.id == entity_id:
                            entity = dialog.entity
                            break
                    if not entity:
                        entity = await cli.get_input_entity(entity_id)
            else:
                uname = target_str if target_str.startswith("@") else "@" + target_str
                entity = await cli.get_entity(uname)

            if isinstance(entity, User):
                if entity.bot:
                    raise ValueError("لا يمكن الاتصال ببوت")
                chat_id = entity.id
            elif isinstance(entity, InputPeerUser):
                chat_id = entity.user_id
            else:
                raise ValueError("الاتصال الخاص يعمل مع المستخدمين فقط (ليس كروب أو قناة)")

            try:
                await msg.edit("⏳ جاري الاتصال... بانتظار رد الطرف الآخر")
            except Exception:
                pass

            is_audio_media = vid.get("media_type") == "audio"
            stream_kwargs = {"audio_parameters": AudioQuality.STUDIO}
            if is_audio_media:
                stream_kwargs["video_flags"] = MediaStream.Flags.IGNORE
            else:
                stream_kwargs["video_parameters"] = VideoQuality.HD_720p
            stream_media = MediaStream(vid["file_path"], **stream_kwargs)

            # PyTgCalls ينفّذ مصافحة المكالمة الخاصة كاملة (DH + g_a_hash + التشفير) تلقائياً
            await call.play(chat_id, stream_media)
            set_active_stream(uid, aid, chat_id, f"خاص: {target_str}", vid["id"])

            # إنهاء الاتصال (decline) تلقائياً فور انتهاء الفيديو
            schedule_private_hangup(uid, aid, cli, call, chat_id, get_video_duration(vid["file_path"]))

            await _drop_msg()
            await respond(e, f"✅ تم إرسال الاتصال الخاص إلى ({target_str}). يبدأ البث فور رد الطرف الآخر، ويُنهي حسابك الاتصال تلقائياً فور انتهاء الفيديو.", buttons=back_button())

        except Exception as ex:
            await _drop_msg()
            await respond(e, f"❌ فشل الاتصال بالخاص: {type(ex).__name__} - {str(ex)}")
        return

    if step in ("video", "unified_video"):
        target_aid = lp.get("target_account_id")
        if step == "video" and not target_aid:
            login_pending.pop(uid, None)
            return await respond(e, "حدث خطأ في تحديد الحساب المستهدف، أعد المحاولة.")

        media = e.video or e.document or e.audio or e.voice or e.gif
        if not media:
            return await respond(e, "ارسل فيديو أو ملف صوتي صالح")

        is_audio = bool(e.audio or e.voice)  # صوتي صراحة (مو فيديو ولا مستند)
        size = getattr(media, "size", 0) or 0
        if size > 500 * 1024 * 1024:
            return await respond(e, "حجم الملف يتجاوز 500MB")

        ext = "mp3" if is_audio else "mp4"
        temp_path = os.path.join(VIDEOS_DIR, f"temp_{uid}_{media.id}.bin")
        final_name = f"{uid}_{media.id}.{ext}"
        final_path = os.path.join(VIDEOS_DIR, final_name)

        msg = await respond(e, "⏳ جاري معالجة الملف بالجودة الأصلية...")
        try:
            await e.download_media(file=temp_path)
        except Exception as ex:
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, f"فشل التحميل: {type(ex).__name__}")

        try:
            if is_audio:
                ff_args = ["ffmpeg", "-y", "-i", temp_path, "-vn",
                           "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                           final_path]
            else:
                ff_args = ["ffmpeg", "-y", "-i", temp_path,
                           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "0",
                           "-fps_mode", "cfr", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                           "-movflags", "+faststart", final_path]
            proc = await asyncio.create_subprocess_exec(
                *ff_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=900)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
        except Exception:
            shutil.copy2(temp_path, final_path)

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, "فشل معالجة الملف")

        actual = os.path.getsize(final_path)
        media_type = "audio" if is_audio else "video"
        accs = get_accounts(uid)
        if step == "unified_video":
            for a in accs:
                acc_media_path = os.path.join(VIDEOS_DIR, f"{uid}_{a['account_id']}_{media.id}.{ext}")
                await asyncio.to_thread(shutil.copy2, final_path, acc_media_path)
                add_video(uid, a["account_id"], acc_media_path, final_name, actual, media_type)
        else:
            add_video(uid, target_aid, final_path, final_name, actual, media_type)

        login_pending.pop(uid, None)
        try:
            await msg.delete()
        except Exception:
            pass
        kind_txt = "الصوت" if is_audio else "الفيديو"
        await respond(e, f"✅ تم حفظ {kind_txt} بالجودة الأصلية بنجاح!\nالحجم: {format_size(actual)}", buttons=back_button())
        return

    if step == "chat":
        link = (e.text or "").strip()
        msg = await respond(e, "⏳ جاري التحقق من الرابط والانضمام للكروب/القناة...")
        cid, result, invite_link = await resolve_chat(uid, link)
        try:
            await msg.delete()
        except Exception:
            pass
        if cid is None:
            return await respond(e, f"❌ تعذر: {result}", buttons=back_button())
        set_chat(uid, cid, result, invite_link or link)
        login_pending.pop(uid, None)
        if cid == 0:
            await respond(e, f"✅ **تم تعيين رابط الاتصال الجماعي:**\n📌 {result}\n🔗 {invite_link}", buttons=back_button(), parse_mode="markdown")
        else:
            await respond(e, f"✅ **تم تعيين رابط الاتصال بنجاح:**\n📌 العنوان: {result}\n🆔 ID الكروب: `{cid}`", buttons=back_button(), parse_mode="markdown")
        return

    if step == "adm_add_sub_id":
        try:
            tid = int(e.text.strip())
            login_pending[e.sender_id] = {"step": "adm_add_sub_days", "target_uid": tid}
            await respond(e, "ارسل عدد الأيام للمشترك:")
        except ValueError:
            await respond(e, "ID غير صالح، ارسل أرقاماً فقط:")
        return

    if step == "adm_add_sub_days":
        try:
            days = int(e.text.strip())
            tid = lp.get("target_uid")
            exp = add_subscriber(tid, days)
            login_pending.pop(e.sender_id, None)
            await respond(e, f"تمت إضافة المشترك بنجاح!\nينتهي في: {exp.strftime('%Y-%m-%d')}")
        except ValueError:
            await respond(e, "أدخل عدد أيام صالح:")
        return

    if step == "dev_add_sub_all_input":
        login_pending.pop(e.sender_id, None)
        if not is_dev(e.sender_id):
            return
        
        text = e.text.strip()
        try:
            if "hour" in text.lower() or "ساع" in text:
                num = int(re.search(r'\d+', text).group())
                delta = timedelta(hours=num)
                duration_label = f"{num} ساعة ⏱️"
            elif "min" in text.lower() or "دقيق" in text:
                num = int(re.search(r'\d+', text).group())
                delta = timedelta(minutes=num)
                duration_label = f"{num} دقيقة ⏱️"
            elif "week" in text.lower() or "اسبوع" in text or "أسبوع" in text:
                num = int(re.search(r'\d+', text).group())
                delta = timedelta(weeks=num)
                duration_label = f"{num} أسبوع 📅"
            else:
                num = int(re.search(r'\d+', text).group())
                delta = timedelta(days=num)
                duration_label = f"{num} يوم 📅"
        except Exception:
            return await respond(e, "❌ صيغة غير صالحة. أرسل الرقم والوحدة بشكل صحيح (مثال: `2 hours` أو `1 day`).")

        c = db()
        try:
            users = c.execute("SELECT user_id FROM users").fetchall()
        finally:
            c.close()

        if not users:
            return await respond(e, "⚠️ لا توجد أي مستخدمين مسجلين في قاعدة البيانات حتى الآن!")

        msg_wait = await respond(e, "⏳ جاري تفعيل الاشتراك الجماعي وإرسال الإشعارات للمستخدمين...")
        
        now = datetime.now()
        success_count = 0

        for u in users:
            uid_target = u["user_id"]
            c = db()
            try:
                existing = c.execute("SELECT expires_at FROM subscribers WHERE user_id=?", (uid_target,)).fetchone()
                current_exp = now
                if existing and existing["expires_at"]:
                    try:
                        parsed_exp = datetime.fromisoformat(existing["expires_at"])
                        if parsed_exp > now:
                            current_exp = parsed_exp
                    except Exception:
                        pass
                
                final_exp = current_exp + delta
                c.execute("INSERT OR REPLACE INTO subscribers(user_id, added_at, expires_at) VALUES(?,?,?)", 
                          (uid_target, now.isoformat(), final_exp.isoformat()))
                c.commit()
            except Exception:
                pass
            finally:
                c.close()

            try:
                notif_text = (
                    f"🎉 **مبروك يا غالي!**\n"
                    f"✨ **تم إضافة اشتراك جماعي لك من قبل الإدارة!**\n"
                    f"⏳ **المدة المضافة:** `{duration_label}`\n"
                    f"📅 **ينتهي الاشتراك في:** `{final_exp.strftime('%Y-%m-%d %H:%M')}`\n"
                    f"🚀 استمتع بكافة مميزات البوت الآن!"
                )
                await bot.send_message(uid_target, notif_text, parse_mode="markdown")
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass

        try:
            await msg_wait.delete()
        except Exception:
            pass

        await respond(e, f"✅ **تمت عملية الاشتراك الجماعي بنجاح تام!**\n\n👥 إجمالي المستفيدين: `{success_count}` مستخدم\n⏱️ المدة المضافة: `{duration_label}`\n🎉 وتم إرسال الإشعارات للجميع بنجاح!")
        return

    if step == "dev_add_session_str":
        login_pending.pop(uid, None)
        if not is_dev(uid):
            return
        sstr = (e.text or "").strip()
        try:
            await e.delete()  # إخفاء رسالة الـ Session من المحادثة
        except Exception:
            pass
        try:
            tmp = TelegramClient(StringSession(sstr), API_ID, API_HASH)
            await tmp.connect()
            if not await tmp.is_user_authorized():
                await tmp.disconnect()
                return await respond(e, "❌ الجلسة غير صالحة", buttons=[[Button.inline("رجوع", b"dev_sessions_menu", style='danger')]])
            me = await tmp.get_me()
            await tmp.disconnect()
        except Exception as ex:
            return await respond(e, f"❌ خطأ: {type(ex).__name__}")
        c = db()
        try:
            c.execute("INSERT OR REPLACE INTO dev_sessions(account_id, first_name, session_string, added_at) VALUES(?,?,?,?)",
                      (me.id, me.first_name or "", sstr, datetime.now().isoformat()))
            c.commit()
        finally:
            c.close()
        await respond(e, f"✅ تم حفظ جلسة المطور: {me.first_name} (`{me.id}`)", buttons=[[Button.inline("رجوع", b"dev_sessions_menu", style='danger')]], parse_mode="markdown")
        return

    if step == "adm_add_admin_id":
        login_pending.pop(e.sender_id, None)
        if not is_dev(e.sender_id):
            return
        try:
            tid = int(e.text.strip())
            add_admin_db(tid)
            try:
                await bot.send_message(tid, "🎉 تم ترقيتك إلى أدمن بنجاح! اضغط أمر /admin للتحكم.")
            except Exception:
                pass
            await respond(e, f"✅ تمت إضافة المستخدم `{tid}` كمساعد أدمن بنجاح وإشعاره!", parse_mode="markdown")
        except ValueError:
            await respond(e, "❌ ID غير صالح، أرسل أرقاماً فقط.")
        return

async def finish_login(e, cli, phone, uid):
    me = await cli.get_me()
    await finish_login_with_client(e, cli, phone, me, uid, show_session=True)

async def finish_login_with_client(e, cli, phone, me, uid, show_session=False):
    aid = me.id
    sstr = StringSession.save(cli.session)
    sfile = save_session_file(uid, aid, sstr)

    ok = add_account(uid, aid, phone or "", me.first_name or "", me.username or "", sfile, sstr)
    if ok == "exists":
        # الحساب مضاف مسبقاً: نحدّث الـ Session ونعيد تشغيله (بدون تكرار)
        update_account_session(uid, aid, sstr, phone or None, me.first_name or "", me.username or "")
        await attach_client(uid, aid, cli)
        login_pending.pop(uid, None)
        await respond(e, f"🔄 هذا الحساب مضاف مسبقاً، تم تحديث جلسته واستخراج الجلسة بنجاح: {me.first_name} (@{me.username or '-'})", buttons=back_button())
        if show_session:
            await send_session_msg(e, {"first_name": me.first_name, "phone": phone}, sstr)
        return
    elif not ok:
        login_pending.pop(uid, None)
        return await respond(e, "حدث خطأ أثناء إضافة الحساب.")

    await attach_client(uid, aid, cli)
    login_pending.pop(uid, None)
    await respond(e, f"✅ تم اضافة الحساب واستخراج الجلسة بنجاح: {me.first_name} (@{me.username or '-'})", buttons=back_button())
    if show_session:
        await send_session_msg(e, {"first_name": me.first_name, "phone": phone}, sstr)

class _SyncGate:
    """يجمع كل الحسابات عند نفس اللحظة قبل تنفيذ البث، حتى يبدأ الصوت لدى الجميع بتوقيت واحد.
    حساب فشل بالانضمام لا يعطّل البقية أكثر من المهلة القصوى."""

    def __init__(self, total, timeout=10):
        self.total = max(total, 1)
        self.count = 0
        self.lock = asyncio.Lock()
        self.event = asyncio.Event()
        self.timeout = timeout

    async def wait(self):
        async with self.lock:
            self.count += 1
            if self.count >= self.total:
                self.event.set()
        try:
            await asyncio.wait_for(self.event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass

async def start_stream_for_account(uid, aid, chat_id, title, saved_link, sync_gate=None):
    st = get_state(uid)
    acc = get_account(uid, aid)
    if not acc:
        return False, "الحساب غير موجود"
    
    vid = get_video(uid, aid)
    if not vid or not os.path.exists(vid["file_path"]):
        return False, "لا يوجد فيديو مرفق للحساب"

    try:
        cli, call = await ensure_client(uid, aid)
    except Exception as ex:
        return False, f"فشل تشغيل عميل تيليجرام: {ex}"

    if chat_id == 0 and saved_link and CALL_LINK_RE.search(saved_link):
        try:
            is_audio_media = vid.get("media_type") == "audio"
            stream_kwargs = {"audio_parameters": AudioQuality.STUDIO}
            if is_audio_media:
                stream_kwargs["video_flags"] = MediaStream.Flags.IGNORE
            else:
                stream_kwargs["video_parameters"] = VideoQuality.HD_720p
            stream_media = MediaStream(vid["file_path"], **stream_kwargs)
            if sync_gate:
                await sync_gate.wait()
            await play_call_link(call, saved_link, stream_media)
            set_active_stream(uid, aid, 0, title, vid["id"])
            return True, "تم البث بنجاح"
        except CallLinkUnsupported as ex:
            return False, str(ex)
        except Exception as ex:
            return False, f"خطأ الانضمام لرابط call: {type(ex).__name__}"

    joined = False
    try:
        await cli(GetParticipantRequest(channel=chat_id, participant=aid))
        joined = True
    except Exception:
        joined = False

    join_err = None
    if not joined and saved_link:
        try:
            if "t.me/+" in saved_link or "joinchat" in saved_link or len(saved_link) > 25:
                m = re.search(r't\.me/(?:joinchat/)?\+?([A-Za-z0-9_\-]+)', saved_link)
                if m:
                    try:
                        await cli(ImportChatInviteRequest(m.group(1)))
                    except UserAlreadyParticipantError:
                        pass
                    joined = True
            else:
                m = re.search(r't\.me/([A-Za-z0-9_]+)', saved_link)
                if m:
                    ent = await cli.get_entity("@" + m.group(1))
                    try:
                        await cli(JoinChannelRequest(ent))
                    except UserAlreadyParticipantError:
                        pass
                    joined = True
        except Exception as ex:
            join_err = ex
            print(f"[group_stream] ❌ فشل انضمام حساب {aid} لكروب {chat_id}: {type(ex).__name__}: {ex}")

    if not joined:
        if join_err:
            return False, f"تعذر الانضمام للمجموعة: {type(join_err).__name__}: {join_err}"
        return False, "الحساب ليس عضواً في المجموعة"

    is_audio = vid.get("media_type") == "audio"
    stream_kwargs = {"audio_parameters": AudioQuality.STUDIO}
    if is_audio:
        stream_kwargs["video_flags"] = MediaStream.Flags.IGNORE
    else:
        stream_kwargs["video_parameters"] = VideoQuality.HD_720p
    stream_media = MediaStream(vid["file_path"], **stream_kwargs)

    if sync_gate:
        await sync_gate.wait()

    last_err = None
    for attempt in range(2):  # محاولة + إعادة محاولة وحدة إذا صار خطأ مؤقت (شائع بكروبات فيها ناس كثير)
        try:
            await call.play(chat_id, stream_media)
            set_active_stream(uid, aid, chat_id, title, vid["id"])
            schedule_auto_leave(uid, aid, call, chat_id, get_video_duration(vid["file_path"]))
            return True, "تم البث بنجاح"
        except Exception as ex:
            last_err = ex
            wait_s = getattr(ex, "seconds", None)  # FloodWaitError من تيليجرام يحدد وقت انتظار صريح
            print(f"[group_stream] ❌ محاولة {attempt + 1} فشلت لحساب {aid} بكروب {chat_id}: "
                  f"{type(ex).__name__}: {ex}")
            if attempt == 0:
                await asyncio.sleep(wait_s if wait_s else 3)
    return False, f"خطأ البث: {type(last_err).__name__}: {last_err}"

@bot.on(events.CallbackQuery(pattern=rb"^start_one_norm_(\d+)$"))
async def cb_start_one_norm(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    ch = get_chat(uid)
    if not ch:
        return await e.answer("يجب تعيين رابط الاتصال أولاً!", alert=True)
    await e.answer("جاري تشغيل البث بالفيديو والصوت...", alert=True)
    success, msg = await start_stream_for_account(uid, aid, ch["chat_id"], ch["chat_title"], ch.get("invite_link"))
    if success:
        await edit(e, f"✅ تم بدء التشغيل في: {ch['chat_title']} (سينزل تلقائياً فور انتهاء الفيديو)", buttons=back_button())
        cam_state[(uid, aid)] = not (get_video(uid, aid) or {}).get("media_type") == "audio"
        asyncio.create_task(prewarm_flash_assets(uid))
        await e.respond("📷 تحكم بالكاميرا أثناء البث:", buttons=cam_menu_buttons(uid))
    else:
        await edit(e, f"❌ فشل البث: {msg}", buttons=back_button())

@bot.on(events.CallbackQuery(pattern=rb"^stop_one_(\d+)$"))
async def cb_stop_one(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    st = get_state(uid)
    call = st["calls"].get(aid)
    active = get_active_stream(uid, aid)
    if active:
        await hard_stop_account(uid, aid, call, active["chat_id"])
    else:
        cancel_auto_leave(uid, aid)
        remove_active_stream(uid, aid)
    await edit(e, "⏹️ تم إيقاف البث ونزول الحساب فوراً.", buttons=back_button())
    await e.answer("تم النزول والإيقاف")

@bot.on(events.CallbackQuery(data=b"start_all_normal"))
async def cb_start_all_normal(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    ch = get_chat(uid)
    if not ch:
        return await e.answer("يجب تعيين رابط الاتصال أولاً!", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await e.answer("لا توجد حسابات!", alert=True)

    await e.answer("⚡ تشغيل فوري لكافة الحسابات بالصوت والصورة الأصلية...", alert=True)
    emergency_btns = [
        [Button.inline("🛑 إيقاف ونزول الكل فوراً", b"stop_all_streams", style='danger')],
        [Button.inline("القائمة الرئيسية", b"back_main", style='primary')]
    ]
    await edit(e, "⚡ جاري تجهيز جميع الحسابات، وسيبدأ البث لدى الجميع في نفس اللحظة بالضبط...", buttons=emergency_btns)

    gate = _SyncGate(len(accs))
    tasks = [
        start_stream_for_account(uid, acc["account_id"], ch["chat_id"], ch["chat_title"], ch.get("invite_link"), sync_gate=gate)
        for acc in accs
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for acc, outcome in zip(accs, outcomes):
        acc_name = acc['first_name'] if acc['first_name'] else mask_phone(acc['phone'])
        if isinstance(outcome, tuple) and outcome[0]:
            results.append(f"حساب {acc_name}: 🟢 يعمل بنجاح (صوت وفيديو أصلي)")
        else:
            err_msg = outcome[1] if isinstance(outcome, tuple) else "خطأ غير متوقع"
            results.append(f"حساب {acc_name}: ❌ فشل ({err_msg})")

    await edit(e, "⚡ نتائج التشغيل الفوري المتزامن:\n\n" + "\n".join(results), buttons=back_button())
    if any(isinstance(o, tuple) and o[0] for o in outcomes):
        for _a in accs:
            cam_state[(uid, _a["account_id"])] = not (get_video(uid, _a["account_id"]) or {}).get("media_type") == "audio"
        asyncio.create_task(prewarm_flash_assets(uid))
        await e.respond("📷 تحكم بالكاميرا أثناء البث (لكل الحسابات الشغالة):", buttons=cam_menu_buttons(uid))

@bot.on(events.CallbackQuery(data=b"stop_all_streams"))
async def cb_stop_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    
    st = get_state(uid)
    active_streams = get_all_active_streams_for_user(uid)
    
    if uid in flash_pending:
        stop_requested.add(uid)   # الحسابات لسا تنضم: ننزلهم أول ما يخلصون

    async def stop_single(active):
        aid = active["account_id"]
        await hard_stop_account(uid, aid, st["calls"].get(aid), active["chat_id"])

    if active_streams:
        await asyncio.gather(*(stop_single(s) for s in active_streams), return_exceptions=True)

    await edit(e, "🛑 تم إيقاف جميع البثوث ونزول كافة الحسابات فوراً بنجاح.", buttons=back_button())
    await e.answer("تم النزول الجماعي الفوري")

# قوائم الأدمن والمطور
def dev_menu():
    return [
        [Button.inline("الاحصائيات", b"adm_stats", style='primary')],
        [Button.inline("عرض المشتركين", b"adm_list_subs", style='primary')],
        [Button.inline("الاتصالات الجارية", b"adm_active", style='primary')],
        [Button.inline("اضف مشترك", b"adm_add_sub", style='success')],
        [Button.inline("🎁 إضافة اشتراك للجميع", b"dev_add_sub_all", style='success')],
        [Button.inline("حذف مشترك", b"adm_del_sub", style='danger')],
        [Button.inline("➕ إضافة أدمن جديد", b"dev_add_admin", style='success')],
        [Button.inline("➖ حذف أدمن", b"dev_del_admin", style='danger')],
        [Button.inline("ايقاف كل الاتصالات", b"adm_stop_all", style='danger')],
    ]

def admin_menu():
    return [
        [Button.inline("الاحصائيات", b"adm_stats", style='primary')],
        [Button.inline("عرض المشتركين", b"adm_list_subs", style='primary')],
        [Button.inline("اضف مشترك", b"adm_add_sub", style='success')],
        [Button.inline("حذف مشترك", b"adm_del_sub", style='danger')],
    ]

@bot.on(events.NewMessage(pattern=r"^/admin$"))
async def cmd_admin(e):
    if not e.is_private:
        return
    uid = e.sender_id
    if is_dev(uid):
        await respond(e, "👑 **لوحة تحكم المطور الأساسي:**", buttons=dev_menu())
    elif is_admin(uid):
        await respond(e, "🛡️ **لوحة التحكم الإدارية:**", buttons=admin_menu())

@bot.on(events.CallbackQuery(data=b"adm_stats"))
async def cb_stats(e):
    if not is_admin(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    c = db()
    try:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        a = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        v = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        act = c.execute("SELECT COUNT(*) FROM active_streams").fetchone()[0]
    finally:
        c.close()
    txt = f"الاحصائيات العامة:\n\nالمستخدمون: {u}\nالحسابات: {a}\nالمشتركون: {s}\nالفيديوهات: {v}\nالاتصالات النشطة: {act}"
    b_target = b"dev_back" if is_dev(e.sender_id) else b"adm_back"
    await edit(e, txt, buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_list_subs"))
async def cb_list_subs(e):
    if not is_admin(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    subs = list_subscribers()
    b_target = b"dev_back" if is_dev(e.sender_id) else b"adm_back"
    if not subs:
        return await edit(e, "لا يوجد مشتركون", buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    txt = "المشتركون:\n\n"
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        days = days_left(uid)
        txt += f"الاسم: {name}\nID: {uid}\nمتبقي: {days} يوم\n\n"
    await edit(e, txt[:4000], buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_add_sub"))
async def cb_addsub(e):
    if not is_admin(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "adm_add_sub_id"}
    b_target = b"dev_back" if is_dev(e.sender_id) else b"adm_back"
    await edit(e, "ارسل ID المستخدم:", buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"dev_add_sub_all"))
async def cb_dev_add_sub_all(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "dev_add_sub_all_input"}
    await edit(e, "🎁 **أرسل المدة المراد إضافتها للجميع**\n(مثال: `1 day` أو `2 hours` أو `30 minutes` أو `3` لأجل 3 أيام):", buttons=[[Button.inline("رجوع", b"dev_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_del_sub"))
async def cb_delsub(e):
    if not is_admin(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    subs = list_subscribers()
    b_target = b"dev_back" if is_dev(e.sender_id) else b"adm_back"
    if not subs:
        return await edit(e, "لا يوجد مشتركون", buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    btns = []
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        days = days_left(uid)
        lbl = f"{name} ({uid}) - {days}ي"
        btns.append([Button.inline(lbl, f"adm_del_sub_{uid}".encode(), style='danger')])
    btns.append([Button.inline("رجوع", b_target, style='primary')])
    await edit(e, "اختر مشترك للحذف:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^adm_del_sub_(\d+)$"))
async def cb_delsub_one(e):
    if not is_admin(e.sender_id):
        return
    tid = int(e.pattern_match.group(1))
    remove_subscriber(tid)
    b_target = b"dev_back" if is_dev(e.sender_id) else b"adm_back"
    await edit(e, f"تم حذف المشترك {tid}", buttons=[[Button.inline("رجوع", b_target, style='danger')]])
    await e.answer("تم")

@bot.on(events.CallbackQuery(data=b"adm_active"))
async def cb_act(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    streams = get_all_active_streams()
    txt = "الاتصالات النشطة:\n\n"
    for s in streams:
        txt += f"المستخدم: {s['user_id']} | الحساب: {s['account_id']}\nالقناة: {s['chat_title']}\n\n"
    if not streams:
        txt = "لا توجد أي اتصالات نشطة حالياً."
    await edit(e, txt[:4000], buttons=[[Button.inline("رجوع", b"dev_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_stop_all"))
async def cb_stopall(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    count = 0
    for s in get_all_active_streams():
        st = get_state(s["user_id"])
        c_obj = st["calls"].get(s["account_id"])
        if c_obj:
            count += 1
        await hard_stop_account(s["user_id"], s["account_id"], c_obj, s["chat_id"])
    await edit(e, f"تم إيقاف عدد {count} اتصال نشط عام.", buttons=[[Button.inline("رجوع", b"dev_back", style='danger')]])
    await e.answer("تم الإيقاف")

@bot.on(events.CallbackQuery(data=b"dev_add_admin"))
async def cb_dev_add_admin(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "adm_add_admin_id"}
    await edit(e, "👤 أرسل الآن **ID المستخدم** الذي تريد ترقيته ليصبح أدمن:", buttons=[[Button.inline("رجوع", b"dev_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"dev_del_admin"))
async def cb_dev_del_admin(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    admins = list_admins()
    if not admins:
        return await edit(e, "لا يوجد أدمنية مضافين حالياً.", buttons=[[Button.inline("رجوع", b"dev_back", style='danger')]])
    btns = []
    for a in admins:
        u = get_user(a["user_id"])
        name = (u["first_name"] if u and u["first_name"] else "بدون اسم")
        btns.append([Button.inline(f"🗑 {name} ({a['user_id']})", f"dev_rm_admin_{a['user_id']}".encode(), style='danger')])
    btns.append([Button.inline("رجوع", b"dev_back", style='primary')])
    await edit(e, "اختر الأدمن المراد حذفه:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^dev_rm_admin_(\d+)$"))
async def cb_dev_rm_admin(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    tid = int(e.pattern_match.group(1))
    ok = remove_admin_db(tid)
    if ok:
        try:
            await bot.send_message(tid, "ℹ️ تم سحب صلاحيات الأدمن منك.")
        except Exception:
            pass
        txt = f"✅ تم حذف الأدمن `{tid}` بنجاح."
    else:
        txt = "⚠️ هذا الأدمن غير موجود."
    await edit(e, txt, buttons=[[Button.inline("➖ حذف أدمن آخر", b"dev_del_admin", style='danger')], [Button.inline("رجوع", b"dev_back", style='primary')]], parse_mode="markdown")
    await e.answer()

@bot.on(events.CallbackQuery(data=b"dev_sessions_menu"))
async def cb_dev_sessions_menu(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    btns = [
        [Button.inline("➕ إضافة جلسة مطور", b"dev_add_session", style='success')],
        [Button.inline("📋 عرض / حذف الجلسات", b"dev_list_sessions", style='primary')],
        [Button.inline("رجوع", b"dev_back", style='danger')],
    ]
    await edit(e, "🔑 جلسات المطور (تظهر للمشتركين ويضيفونها لحساباتهم):", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(data=b"dev_add_session"))
async def cb_dev_add_session(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "dev_add_session_str"}
    await edit(e, "ارسل Session String (Telethon) لحساب المطور:", buttons=[[Button.inline("رجوع", b"dev_sessions_menu", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"dev_list_sessions"))
async def cb_dev_list_sessions(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    c = db()
    try:
        rows = c.execute("SELECT account_id, first_name FROM dev_sessions").fetchall()
    finally:
        c.close()
    if not rows:
        return await edit(e, "لا توجد جلسات مطور.", buttons=[[Button.inline("رجوع", b"dev_sessions_menu", style='danger')]])
    btns = [[Button.inline(f"🗑 {r['first_name'] or 'حساب'} ({r['account_id']})", f"dev_del_session_{r['account_id']}".encode(), style='danger')] for r in rows]
    btns.append([Button.inline("رجوع", b"dev_sessions_menu", style='primary')])
    await edit(e, "اضغط على الجلسة لحذفها:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^dev_del_session_(\d+)$"))
async def cb_dev_del_session(e):
    if not is_dev(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    c = db()
    try:
        c.execute("DELETE FROM dev_sessions WHERE account_id=?", (aid,))
        c.commit()
    finally:
        c.close()
    await edit(e, "✅ تم حذف جلسة المطور.", buttons=[[Button.inline("رجوع", b"dev_sessions_menu", style='primary')]])
    await e.answer()

async def _refresh_cam_menu(e, uid):
    try:
        await edit(e, "📷 تحكم بالكاميرا أثناء البث:", buttons=cam_menu_buttons(uid))
    except Exception:
        pass

@bot.on(events.CallbackQuery(pattern=rb"^cam_toggle_(\d+)$"))
async def cb_cam_toggle(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    ok, on, msg = await toggle_camera(uid, aid)
    if not ok:
        return await e.answer(f"❌ {msg}", alert=True)
    await e.answer("📷 مفتوحة" if on else "📷 مغلقة")
    asyncio.create_task(_refresh_cam_menu(e, uid))

async def _run_flash(e, uid, aids, refresh=None):
    refresh = refresh or _refresh_cam_menu
    job = asyncio.gather(*[flash_camera(uid, a) for a in aids], return_exceptions=True)
    await asyncio.sleep(0.4)
    await refresh(e, uid)   # يظهر زر الإيقاف
    results = await job
    fails = [r[1] for r in results if isinstance(r, tuple) and not r[0]]
    fails += [type(r).__name__ for r in results if not isinstance(r, tuple)]
    if fails:
        try:
            await e.respond(f"❌ فشل الفلاش: {fails[0]}")
        except Exception:
            pass
    await refresh(e, uid)

@bot.on(events.CallbackQuery(pattern=rb"^cam_flash_(\d+)$"))
async def cb_cam_flash(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    if not get_active_stream(uid, aid):
        return await e.answer("لا يوجد بث شغال", alert=True)
    stopping = (uid, aid) in flash_running
    await e.answer("⏹ جاري الإيقاف" if stopping else "⚡")
    asyncio.create_task(_run_flash(e, uid, [aid]))

@bot.on(events.CallbackQuery(data=b"cam_toggle_all"))
async def cb_cam_toggle_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    streams = get_all_active_streams_for_user(uid)
    if not streams:
        return await e.answer("لا يوجد بث شغال حالياً", alert=True)
    results = await asyncio.gather(*[toggle_camera(uid, s["account_id"]) for s in streams], return_exceptions=True)
    done = sum(1 for r in results if isinstance(r, tuple) and r[0])
    await e.answer(f"تم تبديل الكام لـ {done} حساب")
    asyncio.create_task(_refresh_cam_menu(e, uid))

@bot.on(events.CallbackQuery(data=b"cam_flash_all"))
async def cb_cam_flash_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    streams = get_all_active_streams_for_user(uid)
    if not streams:
        return await e.answer("لا يوجد بث شغال حالياً", alert=True)
    aids = [s["account_id"] for s in streams]
    running = [a for a in aids if (uid, a) in flash_running]
    if running:
        await e.answer("⏹ جاري الإيقاف")
        asyncio.create_task(_run_flash(e, uid, running))   # الضغط على الشغال يوقفه
        return
    await e.answer(f"⚡ {len(aids)}")
    asyncio.create_task(_run_flash(e, uid, aids))

async def _refresh_stream_menu(e, uid):
    try:
        await cb_stream_menu(e)
    except Exception:
        pass

async def _flash_join_and_run(e, uid, acc, ch):
    """حساب واحد: ينضم ويبدأ الفلاش فوراً أول ما يصعد، بدون انتظار باقي الحسابات."""
    aid = acc["account_id"]
    name = acc["first_name"] if acc["first_name"] else mask_phone(acc["phone"])
    try:
        ok, msg = await flash_join_account(uid, aid, ch)
    except Exception as ex:
        ok, msg = False, type(ex).__name__
    finally:
        _pend_done(uid)
    if not ok:
        try:
            await e.respond(f"❌ {name}: {msg}")
        except Exception:
            pass
        return
    if uid in stop_requested:   # ضغط إيقاف أثناء الانضمام
        s_ = get_active_stream(uid, aid)
        await hard_stop_account(uid, aid, get_state(uid)["calls"].get(aid), s_["chat_id"] if s_ else ch["chat_id"])
        return
    asyncio.create_task(_refresh_stream_menu(e, uid))
    ok2, msg2 = await flash_camera(uid, aid, already_on=True)   # يبدأ بإغلاق ثم فتح... تكرار
    if not ok2:
        try:
            await e.respond(f"❌ {name}: فشل الفلاش ({msg2})")
        except Exception:
            pass
    await _refresh_stream_menu(e, uid)

async def _flash_all_pipeline(e, uid, ch, accs):
    stop_requested.discard(uid)
    active = {s["account_id"] for s in get_all_active_streams_for_user(uid)}
    live = [a["account_id"] for a in accs if a["account_id"] in active]
    missing = [a for a in accs if a["account_id"] not in active]
    tasks = []
    if live:
        tasks.append(_run_flash(e, uid, live, _refresh_stream_menu))
    if missing:
        _pend_add(uid, len(missing))
        try:
            await edit(e, "⚡ جاري الانضمام... كل حساب يبدأ فلاش أول ما يصعد",
                       buttons=[[Button.inline("🛑 إيقاف ونزول الكل فوراً", b"stop_all_streams", style='danger')]])
        except Exception:
            pass
        for acc in missing:
            tasks.append(_flash_join_and_run(e, uid, acc, ch))
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop_requested.discard(uid)

@bot.on(events.CallbackQuery(data=b"menu_flash_all"))
async def cb_menu_flash_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    streams = get_all_active_streams_for_user(uid)
    aids = [s["account_id"] for s in streams]
    running = [a for a in aids if (uid, a) in flash_running]
    if running:
        await e.answer("⏹ جاري الإيقاف")
        asyncio.create_task(_run_flash(e, uid, running, _refresh_stream_menu))
        return
    if uid in flash_pending:
        return await e.answer("الحسابات جاري انضمامها، انتظر لحظة...", alert=True)
    accs = get_accounts(uid)
    if not accs:
        return await e.answer("لا توجد حسابات!", alert=True)
    ch = get_chat(uid)
    if len(aids) < len(accs) and not ch:
        return await e.answer("يجب تعيين رابط الاتصال أولاً!", alert=True)
    await e.answer(f"⚡ {len(accs)}")
    asyncio.create_task(_flash_all_pipeline(e, uid, ch, accs))

@bot.on(events.CallbackQuery(data=b"dev_back"))
async def cb_dev_back(e):
    if not is_dev(e.sender_id):
        return
    await edit(e, "👑 **لوحة تحكم المطور الأساسي:**", buttons=dev_menu())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_back"))
async def cb_adm_back(e):
    if not is_admin(e.sender_id):
        return
    await edit(e, "🛡️ **لوحة التحكم الإدارية:**", buttons=admin_menu())
    await e.answer()

async def main():
    init_db()
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print("=" * 40)
    print(f"Bot: @{me.username}")
    print("=" * 40)
    restore_task = asyncio.create_task(restore_all_accounts())  # noqa: F841
    await idle()

if __name__ == "__main__":
    asyncio.run(main())
