import os, asyncio, sqlite3, subprocess, shutil, re
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (MessageNotModifiedError, UserNotParticipantError,
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PasswordHashInvalidError, UserAlreadyParticipantError, InviteHashExpiredError,
    InviteHashInvalidError, ChatAdminRequiredError, ChannelPrivateError,
    FloodWaitError, ChannelsTooMuchError, PhoneNumberInvalidError)
from telethon.utils import get_peer_id
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream, GroupCallConfig, AudioQuality, VideoQuality
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
    c.execute("CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, account_id INTEGER, phone TEXT, first_name TEXT, username TEXT, session_file TEXT, added_at TEXT, UNIQUE(user_id, account_id))")
    c.execute("CREATE TABLE IF NOT EXISTS videos(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_path TEXT, file_name TEXT, file_size INTEGER, added_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS chats(user_id INTEGER PRIMARY KEY, chat_id INTEGER, chat_title TEXT, invite_link TEXT, set_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS active_streams(user_id INTEGER PRIMARY KEY, account_id INTEGER, chat_id INTEGER, chat_title TEXT, video_id INTEGER, started_at TEXT)")
    try:
        c.execute("ALTER TABLE chats ADD COLUMN invite_link TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE subscribers ADD COLUMN expires_at TEXT")
    except Exception:
        pass
    c.commit()
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

# ============================================================
# المشتركون
# ============================================================
def is_subscriber(uid):
    if uid == DEV_ID: return True
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

def remove_all_subscribers():
    c = db()
    try:
        r = c.execute("DELETE FROM subscribers")
        c.commit()
        return r.rowcount
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

# ============================================================
# الحسابات
# ============================================================
def count_accounts(uid):
    c = db()
    try:
        n = c.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (uid,)).fetchone()[0]
        return n
    finally:
        c.close()

def max_accounts_for(uid):
    return MAX_ACCOUNTS_SUB if is_subscriber(uid) else MAX_ACCOUNTS_FREE

def add_account(uid, aid, phone, fname, uname, sfile):
    c = db()
    try:
        c.execute("INSERT INTO accounts(user_id, account_id, phone, first_name, username, session_file, added_at) VALUES(?,?,?,?,?,?,?)", (uid, aid, phone, fname, uname, sfile, datetime.now().isoformat()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
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

# ============================================================
# الفيديوهات
# ============================================================
def add_video(uid, path, name, size):
    c = db()
    try:
        cur = c.execute("INSERT INTO videos(user_id, file_path, file_name, file_size, added_at) VALUES(?,?,?,?,?)", (uid, path, name, size, datetime.now().isoformat()))
        vid = cur.lastrowid
        c.commit()
        return vid
    finally:
        c.close()

def get_video(uid):
    c = db()
    try:
        r = c.execute("SELECT * FROM videos WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_video(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM videos WHERE user_id=?", (uid,))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

# ============================================================
# المحادثات
# ============================================================
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

# ============================================================
# البث النشط
# ============================================================
def set_active_stream(uid, aid, cid, title, vid):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO active_streams(user_id, account_id, chat_id, chat_title, video_id, started_at) VALUES(?,?,?,?,?,?)", (uid, aid, cid, title, vid, datetime.now().isoformat()))
        c.commit()
    finally:
        c.close()

def get_active_stream(uid):
    c = db()
    try:
        r = c.execute("SELECT * FROM active_streams WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_active_stream(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM active_streams WHERE user_id=?", (uid,))
        c.commit()
        return r.rowcount > 0
    finally:
        c.close()

def get_all_active_streams():
    c = db()
    try:
        rows = c.execute("SELECT * FROM active_streams").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()

# ============================================================
# أدوات
# ============================================================
def mask_phone(p):
    if not p: return "unknown"
    p = str(p)
    return p[:8] + "*" * 4 if len(p) > 8 else p[:4] + "*" * (len(p) - 4)

def format_size(b):
    if not b: return "0 B"
    for u in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

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

def delete_video_file(uid):
    vid = get_video(uid)
    if vid and vid.get("file_path") and os.path.exists(vid["file_path"]):
        try:
            os.remove(vid["file_path"])
        except Exception:
            pass
    remove_video(uid)

def main_menu():
    return [
        [Button.inline("اضف حساب", b"add_account", style='success')],
        [Button.inline("فحص حساباتي", b"check_accounts", style='primary')],
        [Button.inline("حذف حساب", b"del_account", style='danger')],
        [Button.inline("اضف فيديو", b"add_video", style='primary')],
        [Button.inline("رابط الاتصال", b"set_chat", style='primary')],
        [Button.inline("بدء البث", b"start_stream", style='success')],
        [Button.inline("ايقاف", b"stop_stream", style='danger')],
    ]

def back_button():
    return [[Button.inline("رجوع", b"back_main", style='danger')]]

MAIN_TEXT = "اهلا بك في بوت كام"


# ============================================================
# resolve_chat
# ============================================================
async def resolve_chat(uid, link):
    link = link.strip()
    st = get_state(uid)
    if not st["clients"]:
        return None, "اضف حساب اولاً", None
    cli = list(st["clients"].values())[0]

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
            except UserAlreadyParticipantError:
                pass
            except Exception:
                pass
            return get_peer_id(ent), getattr(ent, 'title', None) or link, None
        except Exception as ex:
            return None, f"تعذر: {type(ex).__name__}", None

    if "t.me/" in link or "telegram.me/" in link:
        m = re.search(r't\.me/(?:joinchat/)?(\+?)([A-Za-z0-9_\-]+)', link)
        if not m:
            return None, "رابط غير صحيح", None
        plus = m.group(1)
        code = m.group(2)

        if plus == "+" or "joinchat" in link:
            try:
                try:
                    await cli(ImportChatInviteRequest(code))
                except UserAlreadyParticipantError:
                    pass
                except InviteHashExpiredError:
                    return None, "الرابط منتهي الصلاحية", None
                except InviteHashInvalidError:
                    return None, "رابط غير صالح", None
                except ChannelsTooMuchError:
                    return None, "الحساب انضم لكثير من المجموعات", None

                from telethon.tl.functions.messages import CheckChatInviteRequest
                info = await cli(CheckChatInviteRequest(code))
                chat = getattr(info, 'chat', None)
                if chat:
                    return get_peer_id(chat), getattr(chat, 'title', None) or str(chat.id), link

                await asyncio.sleep(1)
                try:
                    async for d in cli.iter_dialogs(limit=30):
                        if d.is_group or d.is_channel:
                            return get_peer_id(d.entity), d.title or str(d.entity.id), link
                except Exception:
                    pass

                return None, "تم الانضمام لكن تعذر استخراج ID", None
            except Exception as ex:
                return None, f"تعذر: {type(ex).__name__}", None

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


# ============================================================
# auto_leave_when_done
# ============================================================
async def auto_leave_when_done(uid, aid, chat_id, duration, video_path):
    try:
        await asyncio.sleep(duration + 3)
    except asyncio.CancelledError:
        return
    st = get_state(uid)
    call = st["calls"].get(aid)
    if call:
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
    try:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
    except Exception:
        pass
    remove_active_stream(uid)
    delete_video_file(uid)


# ============================================================
# /start
# ============================================================
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

# ============================================================
# اضافة حساب
# ============================================================
@bot.on(events.CallbackQuery(data=b"add_account"))
async def cb_add_account(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    n = count_accounts(e.sender_id)
    mx = max_accounts_for(e.sender_id)
    if n >= mx:
        return await e.answer(f"وصلت للحد ({mx})", alert=True)
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
    await edit(e, "ارسل Session String", buttons=back_button())
    await e.answer()

# ============================================================
# فحص حساباتي
# ============================================================
@bot.on(events.CallbackQuery(data=b"check_accounts"))
async def cb_check(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(e.sender_id)
    if not accs:
        return await edit(e, "لا يوجد حسابات", buttons=back_button())
    txt = "حساباتي:\n\n"
    for i, a in enumerate(accs, 1):
        cli = get_state(e.sender_id)["clients"].get(a["account_id"])
        status = "متصل" if cli and cli.is_connected() else "غير متصل"
        uname = f"@{a['username']}" if a['username'] else "-"
        txt += f"{i}. {mask_phone(a['phone'])}\n   الاسم: {a['first_name'] or '-'}\n   اليوزر: {uname}\n   الحالة: {status}\n\n"
    await edit(e, txt, buttons=back_button())
    await e.answer()

# ============================================================
# حذف حساب
# ============================================================
@bot.on(events.CallbackQuery(data=b"del_account"))
async def cb_del(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    accs = get_accounts(e.sender_id)
    if not accs:
        return await edit(e, "لا يوجد حسابات", buttons=back_button())
    btns = []
    for a in accs:
        lbl = f"{mask_phone(a['phone'])} - {a['first_name'] or 'unknown'}"
        btns.append([Button.inline(lbl, f"del_acc_{a['account_id']}".encode(), style='danger')])
    btns.append([Button.inline("حذف الكل", b"del_all_acc", style='danger')])
    btns.append([Button.inline("رجوع", b"back_main", style='primary')])
    await edit(e, "اختر الحساب للحذف:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^del_acc_(\d+)$"))
async def cb_del_one(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    aid = int(e.pattern_match.group(1))
    st = get_state(e.sender_id)
    if aid in st["clients"]:
        try:
            await st["clients"][aid].disconnect()
        except Exception:
            pass
        st["clients"].pop(aid, None)
        st["calls"].pop(aid, None)
    acc = get_account(e.sender_id, aid)
    if acc and acc.get("session_file") and os.path.exists(acc["session_file"]):
        try:
            os.remove(acc["session_file"])
        except Exception:
            pass
    remove_account(e.sender_id, aid)
    await edit(e, "تم حذف الحساب", buttons=back_button())
    await e.answer("تم الحذف")

@bot.on(events.CallbackQuery(data=b"del_all_acc"))
async def cb_del_all(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    st = get_state(e.sender_id)
    for aid, cli in list(st["clients"].items()):
        try:
            await cli.disconnect()
        except Exception:
            pass
    st["clients"].clear()
    st["calls"].clear()
    for a in get_accounts(e.sender_id):
        if a.get("session_file") and os.path.exists(a["session_file"]):
            try:
                os.remove(a["session_file"])
            except Exception:
                pass
    n = remove_all_accounts(e.sender_id)
    await edit(e, f"تم حذف {n} حساب", buttons=back_button())
    await e.answer("تم")

# ============================================================
# اضافة فيديو + رابط
# ============================================================
@bot.on(events.CallbackQuery(data=b"add_video"))
async def cb_add_video(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "video"}
    await edit(e, "ارسل الفيديو (MP4)", buttons=back_button())
    await e.answer()

@bot.on(events.CallbackQuery(data=b"set_chat"))
async def cb_set_chat(e):
    if not is_subscriber(e.sender_id):
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "chat"}
    await edit(e, "ارسل رابط المجموعة او القناة", buttons=back_button())
    await e.answer()

# ============================================================
# Message handler
# ============================================================
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
            await respond(e, "ارسل الكود المرسل اليك")
        except PhoneNumberInvalidError:
            await respond(e, "رقم غير صالح")
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
            return await respond(e, "انتهت الجلسة")
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=ph)
        except SessionPasswordNeededError:
            lp["step"] = "password"
            return await respond(e, "الحساب محمي بكلمة مرور. ارسلها")
        except PhoneCodeInvalidError:
            return await respond(e, "الكود خطأ")
        except PhoneCodeExpiredError:
            login_pending.pop(uid, None)
            return await respond(e, "انتهى الكود")
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
        except PasswordHashInvalidError:
            return await respond(e, "كلمة المرور خطأ")
        except Exception as ex:
            return await respond(e, f"خطأ: {type(ex).__name__}")
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
            await finish_login_with_client(e, cli, None, me, uid)
        except Exception as ex:
            return await respond(e, f"خطأ: {type(ex).__name__}")
        return

    if step == "video":
        media = e.video or e.document or e.gif
        if not media:
            return await respond(e, "ارسل فيديو او ملف MP4")
        size = getattr(media, "size", 0) or 0
        if size > 500 * 1024 * 1024:
            return await respond(e, "الحجم يتجاوز 500MB")

        old = get_video(uid)
        if old and old.get("file_path") and os.path.exists(old["file_path"]):
            try:
                os.remove(old["file_path"])
            except Exception:
                pass
        remove_video(uid)

        temp_path = os.path.join(VIDEOS_DIR, f"temp_{uid}_{media.id}.mp4")
        final_name = f"{uid}_{media.id}.mp4"
        final_path = os.path.join(VIDEOS_DIR, final_name)

        msg = await respond(e, "جاري تحميل ومعالجة الفيديو...")
        try:
            await e.download_media(file=temp_path)
        except Exception as ex:
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, f"فشل التحميل: {type(ex).__name__}")

        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, "الملف فارغ")

        # معالجة وتحويل الفيديو بطريقة مباشرة وآمنة عبر FFmpeg
        converted = False
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", temp_path,
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-movflags", "+faststart", final_path
                ],
                capture_output=True, text=True, timeout=300
            )
            if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                converted = True
        except Exception:
            converted = False

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        if not converted or not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, "فشل تجهيز الفيديو، تأكد من وجود FFmpeg على الخادم وصلاحية الملف")

        actual = os.path.getsize(final_path)
        add_video(uid, final_path, final_name, actual)
        login_pending.pop(uid, None)
        try:
            await msg.delete()
        except Exception:
            pass
        await respond(e, f"تم حفظ الفيديو بنجاح\nالاسم: {final_name}\nالحجم: {format_size(actual)}", buttons=back_button())
        return

    if step == "chat":
        link = (e.text or "").strip()
        msg = await respond(e, "جاري الانضمام والتحقق...")
        cid, result, invite_link = await resolve_chat(uid, link)
        try:
            await msg.delete()
        except Exception:
            pass
        if cid is None:
            return await respond(e, f"تعذر: {result}", buttons=back_button())
        set_chat(uid, cid, result, invite_link or link)
        login_pending.pop(uid, None)
        await respond(e, f"تم تعيين: {result}\nID: {cid}", buttons=back_button())
        return

    # ===== ADMIN INPUT =====
    if uid != DEV_ID:
        return

    if step == "adm_add_sub_id":
        try:
            tid = int((e.text or "").strip())
        except Exception:
            return await respond(e, "ID خطأ", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        login_pending[uid] = {"step": "adm_add_sub_days", "target": tid}
        await respond(e, f"ID: {tid}\nارسل عدد الأيام:", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        return

    if step == "adm_add_sub_days":
        try:
            days = int((e.text or "").strip())
            if days < 1: raise ValueError()
        except Exception:
            return await respond(e, "عدد أيام خطأ", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        tid = lp.get("target")
        exp = add_subscriber(tid, days)
        login_pending.pop(uid, None)
        await respond(e, f"تم اضافة {tid} لمدة {days} يوم\nينتهي: {exp.strftime('%Y-%m-%d')}", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        return

    if step == "adm_del_sub_id":
        try:
            tid = int((e.text or "").strip())
        except Exception:
            return await respond(e, "ID خطأ", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        remove_subscriber(tid)
        login_pending.pop(uid, None)
        await respond(e, f"تم حذف {tid}", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
        return


async def finish_login(e, cli, phone, uid):
    me = await cli.get_me()
    await finish_login_with_client(e, cli, phone, me, uid)


async def finish_login_with_client(e, cli, phone, me, uid):
    aid = me.id
    sfile = os.path.join(SESSIONS_DIR, f"{uid}_{aid}.session")
    sstr = StringSession.save(cli.session)
    with open(sfile, "w", encoding="utf-8") as f:
        f.write(sstr)
    ok = add_account(uid, aid, phone or "", me.first_name or "", me.username or "", sfile)
    if not ok:
        login_pending.pop(uid, None)
        return await respond(e, "الحساب مضاف مسبقاً")
    call = PyTgCalls(cli)
    try:
        await call.start()
    except Exception:
        pass
    st = get_state(uid)
    st["clients"][aid] = cli
    st["calls"][aid] = call
    login_pending.pop(uid, None)
    await respond(e, f"تم اضافة: {me.first_name} (@{me.username or '-'})", buttons=back_button())


# ============================================================
# بدء البث
# ============================================================
@bot.on(events.CallbackQuery(data=b"start_stream"))
async def cb_start_stream(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    await e.answer("جاري...")

    accs = get_accounts(uid)
    if not accs:
        return await edit(e, "لا يوجد حسابات", buttons=back_button())
    vid = get_video(uid)
    if not vid:
        return await edit(e, "لا يوجد فيديو", buttons=back_button())
    ch = get_chat(uid)
    if not ch:
        return await edit(e, "لا يوجد رابط اتصال", buttons=back_button())
    if not os.path.exists(vid["file_path"]):
        return await edit(e, f"الملف مفقود", buttons=back_button())

    await edit(e, "جاري بدء البث...")
    st = get_state(uid)
    chat_id = ch["chat_id"]
    title = ch["chat_title"]
    saved_link = ch.get("invite_link")
    errors = []
    done = False

    for acc in accs:
        aid = acc["account_id"]
        cli = st["clients"].get(aid)
        call = st["calls"].get(aid)
        if not cli or not call:
            try:
                with open(acc["session_file"], "r", encoding="utf-8") as f:
                    sstr = f.read().strip()
                cli = TelegramClient(StringSession(sstr), API_ID, API_HASH)
                await cli.connect()
                call = PyTgCalls(cli)
                await call.start()
                st["clients"][aid] = cli
                st["calls"][aid] = call
            except Exception as ex:
                errors.append(f"{acc['first_name']}: ربط فشل")
                continue

        joined = False
        try:
            await cli(GetParticipantRequest(channel=chat_id, participant=aid))
            joined = True
        except UserNotParticipantError:
            joined = False
        except Exception:
            joined = False

        if not joined:
            if saved_link:
                if "t.me/+" in saved_link or "joinchat" in saved_link:
                    m = re.search(r't\.me/(?:joinchat/)?\+([A-Za-z0-9_\-]+)', saved_link)
                    if m:
                        try:
                            await cli(ImportChatInviteRequest(m.group(1)))
                            joined = True
                        except UserAlreadyParticipantError:
                            joined = True
                        except Exception:
                            errors.append(f"{acc['first_name']}: رابط فشل")
                            continue
                else:
                    m = re.search(r't\.me/([A-Za-z0-9_]+)', saved_link)
                    if m:
                        try:
                            ent = await cli.get_entity("@" + m.group(1))
                            try:
                                await cli(JoinChannelRequest(ent))
                            except UserAlreadyParticipantError:
                                pass
                            joined = True
                        except Exception:
                            errors.append(f"{acc['first_name']}: فشل الانضمام")
                            continue
            else:
                errors.append(f"{acc['first_name']}: ليس عضو")
                continue

        try:
            await call.play(chat_id, MediaStream(vid["file_path"],
                audio_parameters=AudioQuality.HIGH,
                video_parameters=VideoQuality.HD_720p),
                config=GroupCallConfig(auto_start=False))
            set_active_stream(uid, aid, chat_id, title, vid["id"])

            duration = 0
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", vid["file_path"]],
                    capture_output=True, text=True, timeout=15
                )
                duration = int(float(probe.stdout.strip()))
            except Exception:
                duration = 0

            old_task = auto_leave_tasks.pop(uid, None)
            if old_task:
                old_task.cancel()

            if duration > 0:
                task = asyncio.create_task(
                    auto_leave_when_done(uid, aid, chat_id, duration, vid["file_path"])
                )
                auto_leave_tasks[uid] = task

            await edit(e, f"تم بدء البث في:\n{title}\n\nالحساب: {acc['first_name']}", buttons=back_button())
            done = True
            break
        except Exception as ex:
            err = type(ex).__name__
            if "NoActive" in err or "GroupCallNotFound" in err:
                errors.append(f"{acc['first_name']}: لا توجد مكالمة")
            elif "ChatAdminRequired" in err or "ADMIN" in err:
                errors.append(f"{acc['first_name']}: يحتاج صلاحية")
            else:
                errors.append(f"{acc['first_name']}: {err}")
            continue

    if not done:
        await edit(e, "فشل البث:\n\n" + "\n".join(errors[:5]), buttons=back_button())


# ============================================================
# ايقاف
# ============================================================
@bot.on(events.CallbackQuery(data=b"stop_stream"))
async def cb_stop(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        return await e.answer("غير مصرح", alert=True)
    task = auto_leave_tasks.pop(uid, None)
    if task:
        task.cancel()
    st = get_state(uid)
    active = get_active_stream(uid)
    if active:
        call = st["calls"].get(active["account_id"])
        if call:
            try:
                await call.leave_call(active["chat_id"])
            except Exception:
                pass
    remove_active_stream(uid)
    delete_video_file(uid)
    await edit(e, "تم الايقاف وحذف الفيديو", buttons=back_button())
    await e.answer("تم")


# ============================================================
# /admin
# ============================================================
def admin_menu():
    return [
        [Button.inline("الاحصائيات", b"adm_stats", style='primary')],
        [Button.inline("عرض المشتركين", b"adm_list_subs", style='primary')],
        [Button.inline("الاتصالات الجارية", b"adm_active", style='primary')],
        [Button.inline("اضف مشترك", b"adm_add_sub", style='success')],
        [Button.inline("حذف مشترك", b"adm_del_sub", style='danger')],
        [Button.inline("ايقاف كل الاتصالات", b"adm_stop_all", style='danger')],
    ]

@bot.on(events.NewMessage(pattern=r"^/admin$"))
async def cmd_admin(e):
    if not e.is_private or e.sender_id != DEV_ID:
        return
    await respond(e, "لوحة المطور", buttons=admin_menu())

@bot.on(events.CallbackQuery(data=b"adm_stats"))
async def cb_stats(e):
    if e.sender_id != DEV_ID:
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
    txt = f"الاحصائيات\n\nالمستخدمون: {u}\nالحسابات: {a}\nالمشتركون: {s}\nالفيديوهات: {v}\nالاتصالات: {act}"
    await edit(e, txt, buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_list_subs"))
async def cb_list_subs(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    subs = list_subscribers()
    if not subs:
        return await edit(e, "لا يوجد مشتركون", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    txt = "المشتركون:\n\n"
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        uname = f"@{u['username']}" if u and u.get("username") else "-"
        days = days_left(uid)
        txt += f"الاسم: {name}\nاليوزر: {uname}\nID: {uid}\nمتبقي: {days} يوم\n\n"
    await edit(e, txt[:4000], buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_add_sub"))
async def cb_addsub(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    login_pending[e.sender_id] = {"step": "adm_add_sub_id"}
    await edit(e, "ارسل ID المستخدم", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_del_sub"))
async def cb_delsub(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    subs = list_subscribers()
    if not subs:
        return await edit(e, "لا يوجد مشتركون", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    btns = []
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        days = days_left(uid)
        lbl = f"{name} ({uid}) - {days}ي"
        btns.append([Button.inline(lbl, f"adm_del_sub_{uid}".encode(), style='danger')])
    btns.append([Button.inline("حذف الكل", b"adm_del_all_subs", style='danger')])
    btns.append([Button.inline("رجوع", b"adm_back", style='primary')])
    await edit(e, "اختر مشترك للحذف:", buttons=btns)
    await e.answer()

@bot.on(events.CallbackQuery(pattern=rb"^adm_del_sub_(\d+)$"))
async def cb_delsub_one(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    tid = int(e.pattern_match.group(1))
    remove_subscriber(tid)
    await edit(e, f"تم حذف {tid}", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer("تم")

@bot.on(events.CallbackQuery(data=b"adm_del_all_subs"))
async def cb_delallsubs(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    n = remove_all_subscribers()
    await edit(e, f"تم حذف {n} مشترك", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer("تم")

@bot.on(events.CallbackQuery(data=b"adm_active"))
async def cb_act(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    streams = get_all_active_streams()
    txt = "الاتصالات النشطة:\n\n"
    for s in streams:
        txt += f"المستخدم: {s['user_id']}\nالغرفة: {s['chat_title']}\n\n"
    if not streams:
        txt = "لا يوجد"
    await edit(e, txt[:4000], buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer()

@bot.on(events.CallbackQuery(data=b"adm_stop_all"))
async def cb_stopall(e):
    if e.sender_id != DEV_ID:
        return await e.answer("غير مصرح", alert=True)
    count = 0
    for s in get_all_active_streams():
        st = get_state(s["user_id"])
        call = st["calls"].get(s["account_id"])
        if call:
            try:
                await call.leave_call(s["chat_id"])
                count += 1
            except Exception:
                pass
        task = auto_leave_tasks.pop(s["user_id"], None)
        if task:
            task.cancel()
        remove_active_stream(s["user_id"])
        delete_video_file(s["user_id"])
    await edit(e, f"تم ايقاف {count} اتصال", buttons=[[Button.inline("رجوع", b"adm_back", style='danger')]])
    await e.answer("تم")

@bot.on(events.CallbackQuery(data=b"adm_back"))
async def cb_adm_back(e):
    if e.sender_id != DEV_ID:
        return
    await edit(e, "لوحة المطور", buttons=admin_menu())
    await e.answer()


# ============================================================
# Main
# ============================================================
async def main():
    init_db()
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print("=" * 40)
    print(f"Bot: @{me.username}")
    print("=" * 40)
    await idle()

if __name__ == "__main__":
    asyncio.run(main())
