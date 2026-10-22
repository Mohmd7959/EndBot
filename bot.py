# -*- coding: utf-8 -*-
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
    FloodWaitError, ChannelsTooMuchError, PhoneNumberInvalidError, QueryIdInvalidError)
from telethon.utils import get_peer_id
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream, GroupCallConfig, AudioQuality, VideoQuality
from config import *

bot = TelegramClient("bot_session", API_ID, API_HASH)
user_state = {}
login_pending = {}

# إيموجيات بريميوم للنصوص والرسائل (تليجرام يدعمها في النص حصراً وليس في الأزرار)
EMOJI = {
    "star": '<tg-emoji id="5431448834079361001">⭐</tg-emoji>',
    "crown": '<tg-emoji id="5431448834079361002">👑</tg-emoji>',
    "video": '<tg-emoji id="5431448834079361003">🎥</tg-emoji>',
    "link": '<tg-emoji id="5431448834079361004">🔗</tg-emoji>',
    "phone": '<tg-emoji id="5431448834079361005">📱</tg-emoji>',
    "check": '<tg-emoji id="5431448834079361006">✅</tg-emoji>',
    "cross": '<tg-emoji id="5431448834079361007">❌</tg-emoji>',
    "zap": '<tg-emoji id="5431448834079361008">⚡</tg-emoji>',
    "fire": '<tg-emoji id="5431448834079361009">🔥</tg-emoji>',
    "gear": '<tg-emoji id="5431448834079361010">⚙️</tg-emoji>',
}

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

    c.execute("""CREATE TABLE IF NOT EXISTS global_videos(
        user_id INTEGER PRIMARY KEY, 
        file_path TEXT, 
        file_name TEXT, 
        file_size INTEGER, 
        added_at TEXT
    )""")
    
    c.execute("CREATE TABLE IF NOT EXISTS chats(user_id INTEGER PRIMARY KEY, chat_id INTEGER, chat_title TEXT, invite_link TEXT, set_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS active_streams(user_id INTEGER, account_id INTEGER, chat_id INTEGER, chat_title TEXT, video_file TEXT, started_at TEXT, PRIMARY KEY(user_id, account_id))")
    
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

def add_video(uid, aid, path, name, size):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO videos(user_id, account_id, file_path, file_name, file_size, added_at) VALUES(?,?,?,?,?,?)", (uid, aid, path, name, size, datetime.now().isoformat()))
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

def set_global_video(uid, path, name, size):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO global_videos(user_id, file_path, file_name, file_size, added_at) VALUES(?,?,?,?,?)", (uid, path, name, size, datetime.now().isoformat()))
        c.commit()
    finally:
        c.close()

def get_global_video(uid):
    c = db()
    try:
        r = c.execute("SELECT * FROM global_videos WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()

def remove_global_video(uid):
    c = db()
    try:
        r = c.execute("DELETE FROM global_videos WHERE user_id=?", (uid,))
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

def set_active_stream(uid, aid, cid, title, vfile):
    c = db()
    try:
        c.execute("INSERT OR REPLACE INTO active_streams(user_id, account_id, chat_id, chat_title, video_file, started_at) VALUES(?,?,?,?,?,?)", (uid, aid, cid, title, vfile, datetime.now().isoformat()))
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

async def safe_answer(e, text=None, alert=False):
    try:
        await e.answer(text, alert=alert)
    except (QueryIdInvalidError, Exception):
        pass

async def respond(event, text, buttons=None, **kw):
    try:
        return await event.respond(text, buttons=buttons, reply_to=event.message.id, parse_mode="html", **kw)
    except Exception:
        return await event.respond(text, buttons=buttons, parse_mode="html", **kw)

async def edit(event, text, buttons=None, **kw):
    try:
        await event.edit(text, buttons=buttons, parse_mode="html", **kw)
    except MessageNotModifiedError:
        pass

async def reject_if_not_subscribed(event):
    if is_subscriber(event.sender_id):
        return False
    btns = [[Button.url("المطور 👑", DEV_LINK, style='primary')]]
    await respond(event, f"{EMOJI['cross']} <b>لا يمكنك استخدام البوت. أنت غير مشترك!</b>", buttons=btns)
    return True

def main_menu():
    return [
        [Button.inline("➕ اضف حساب", b"add_account", style='success')],
        [Button.inline("🛡️ فحص حساباتي", b"check_accounts", style='primary')],
        [Button.inline("🗑️ حذف حساب", b"del_account", style='danger')],
        [Button.inline("🎥 فيديو لكل حساب", b"add_video_menu", style='primary')],
        [Button.inline("🌐 فيديو موحد لكل الحسابات", b"set_global_video", style='primary')],
        [Button.inline("🔗 رابط الاتصال (المجموعة)", b"set_chat", style='primary')],
        [Button.inline("⚡ قائمة البث والتحكم", b"stream_menu", style='success')],
    ]

def back_button():
    return [[Button.inline("🔙 رجوع", b"back_main", style='danger')]]

MAIN_TEXT = f"{EMOJI['crown']} <b>اهلا بك في بوت كام - إدارة البث المتعدد</b>\n\nقم بإدارة حساباتك وبثوثك بكل احترافية وسرعة فائقة {EMOJI['zap']}"

async def resolve_chat(uid, link):
    link = link.strip()
    st = get_state(uid)
    if not st["clients"]:
        accs = get_accounts(uid)
        if not accs:
            return None, "اضف حساب اولاً", None
        acc = accs[0]
        try:
            cli = TelegramClient(acc["session_file"], API_ID, API_HASH)
            await cli.connect()
            st["clients"][acc["account_id"]] = cli
        except Exception:
            return None, "فشل الاتصال بأول حساب لتفقد الرابط", None

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
                except Exception:
                    pass

                from telethon.tl.functions.messages import CheckChatInviteRequest
                info = await cli(CheckChatInviteRequest(code))
                chat = getattr(info, 'chat', None)
                if chat:
                    return get_peer_id(chat), getattr(chat, 'title', None) or str(chat.id), link
                return None, "تم الانضمام لكن تعذر استخراج ID", None
            except Exception as ex:
                return None, f"تعذر: {type(ex).__name__}", None

        try:
            ent = await cli.get_entity("@" + code)
            try:
                await cli(JoinChannelRequest(ent))
            except Exception:
                pass
            return get_peer_id(ent), getattr(ent, 'title', None) or code, link
        except Exception as ex:
            return None, f"تعذر: {type(ex).__name__}", None

    return None, "صيغة غير معروفة", None

async def auto_leave_when_done(uid, aid, chat_id, duration):
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
    remove_active_stream(uid, aid)

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
        await safe_answer(e, "غير مصرح", alert=True)
        return
    try:
        await e.edit(MAIN_TEXT, buttons=main_menu(), parse_mode="html")
    except MessageNotModifiedError:
        pass
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"add_account"))
async def cb_add_account(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    n = count_accounts(e.sender_id)
    mx = max_accounts_for(e.sender_id)
    if n >= mx:
        await safe_answer(e, f"وصلت للحد الأقصى ({mx})", alert=True)
        return
    btns = [
        [Button.inline("📱 برقم الهاتف", b"acc_phone", style='success')],
        [Button.inline("🔑 Session String", b"acc_session", style='primary')],
        [Button.inline("🔙 رجوع", b"back_main", style='danger')],
    ]
    await edit(e, f"{EMOJI['phone']} <b>اضف حساب جديد</b>\nلديك حالياً {n}/{mx} حساب مضاف", buttons=btns)
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"acc_phone"))
async def cb_acc_phone(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    login_pending[e.sender_id] = {"step": "phone"}
    await edit(e, f"{EMOJI['phone']} <b>ارسل رقم الهاتف مع رمز الدولة</b>\nمثال: <code>+9647701234567</code>", buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"acc_session"))
async def cb_acc_session(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    login_pending[e.sender_id] = {"step": "session"}
    await edit(e, f"{EMOJI['star']} <b>ارسل Session String الخاص بالحساب:</b>", buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"check_accounts"))
async def cb_check(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    accs = get_accounts(e.sender_id)
    if not accs:
        await edit(e, f"{EMOJI['cross']} <b>لا يوجد حسابات مضافة حالياً.</b>", buttons=back_button())
        await safe_answer(e)
        return
    mx = max_accounts_for(e.sender_id)
    txt = f"{EMOJI['crown']} <b>حساباتك المضافة ({len(accs)}/{mx}):</b>\n\n"
    for i, a in enumerate(accs, 1):
        cli = get_state(e.sender_id)["clients"].get(a["account_id"])
        status = "🟢 متصل" if cli and cli.is_connected() else "🔴 غير متصل"
        vid = get_video(e.sender_id, a["account_id"])
        
        v_name = vid['file_name'] if vid else None
        v_status = f"{EMOJI['check']} فيديو خاص ({v_name})" if vid else f"{EMOJI['cross']} بدون فيديو خاص"
        
        uname = f"@{a['username']}" if a['username'] else "-"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        txt += f"✨ {i}. <b>{acc_name}</b>\n   📞 الرقم: <code>{mask_phone(a['phone'])}</code>\n   🌐 اليوزر: {uname}\n   ⚙️ الحالة: {status}\n   {v_status}\n\n"
    await edit(e, txt, buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"del_account"))
async def cb_del(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    accs = get_accounts(e.sender_id)
    if not accs:
        await edit(e, f"{EMOJI['cross']} <b>لا يوجد حسابات للحذف.</b>", buttons=back_button())
        await safe_answer(e)
        return
    btns = []
    for a in accs:
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        btns.append([Button.inline(f"🗑️ {acc_name}", f"del_acc_{a['account_id']}".encode(), style='danger')])
    btns.append([Button.inline("⚠️ حذف كل الحسابات", b"del_all_acc", style='danger')])
    btns.append([Button.inline("🔙 رجوع", b"back_main", style='primary')])
    await edit(e, f"{EMOJI['cross']} <b>اختر الحساب المراد حذفه:</b>", buttons=btns)
    await safe_answer(e)

@bot.on(events.CallbackQuery(pattern=rb"^del_acc_(\d+)$"))
async def cb_del_one(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
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
    
    vid = get_video(uid, aid)
    if vid and vid.get("file_path") and os.path.exists(vid["file_path"]):
        try:
            os.remove(vid["file_path"])
        except Exception:
            pass
    remove_video(uid, aid)

    acc = get_account(uid, aid)
    if acc and acc.get("session_file") and os.path.exists(acc["session_file"]):
        try:
            os.remove(acc["session_file"])
        except Exception:
            pass
    remove_account(uid, aid)
    await edit(e, f"{EMOJI['check']} <b>تم حذف الحساب وفيديو المرتبط به بنجاح.</b>", buttons=back_button())
    await safe_answer(e, "تم الحذف")

@bot.on(events.CallbackQuery(data=b"del_all_acc"))
async def cb_del_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    st = get_state(uid)
    for aid, cli in list(st["clients"].items()):
        try:
            await cli.disconnect()
        except Exception:
            pass
        vid = get_video(uid, aid)
        if vid and vid.get("file_path") and os.path.exists(vid["file_path"]):
            try:
                os.remove(vid["file_path"])
            except Exception:
                pass
        remove_video(uid, aid)

    st["clients"].clear()
    st["calls"].clear()
    for a in get_accounts(uid):
        if a.get("session_file") and os.path.exists(a["session_file"]):
            try:
                os.remove(a["session_file"])
            except Exception:
                pass
    remove_all_accounts(uid)
    await edit(e, f"{EMOJI['check']} <b>تم حذف جميع الحسابات والفيديوهات بنجاح.</b>", buttons=back_button())
    await safe_answer(e, "تم حذف الكل")

@bot.on(events.CallbackQuery(data=b"add_video_menu"))
async def cb_add_video_menu(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    accs = get_accounts(uid)
    if not accs:
        await edit(e, f"{EMOJI['cross']} <b>يجب إضافة حسابات أولاً لكي تتمكن من رفع فيديو لكل حساب!</b>", buttons=back_button())
        await safe_answer(e)
        return
    
    btns = []
    for a in accs:
        vid = get_video(uid, a["account_id"])
        has_v = "✅ (مرفق)" if vid else "❌ (بدون فيديو)"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        btns.append([Button.inline(f"🎥 {acc_name} {has_v}", f"select_vid_acc_{a['account_id']}".encode(), style='primary')])
    btns.append([Button.inline("🔙 رجوع", b"back_main", style='danger')])
    await edit(e, f"{EMOJI['video']} <b>اختر الحساب الذي تريد رفع فيديو خاص به:</b>", buttons=btns)
    await safe_answer(e)

@bot.on(events.CallbackQuery(pattern=rb"^select_vid_acc_(\d+)$"))
async def cb_select_vid_acc(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    aid = int(e.pattern_match.group(1))
    login_pending[e.sender_id] = {"step": "video", "target_account_id": aid}
    await edit(e, f"{EMOJI['video']} <b>أرسل الآن الفيديو (MP4) المخصص لهذا الحساب:</b>", buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"set_global_video"))
async def cb_set_global_video(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    login_pending[uid] = {"step": "global_video"}
    vid = get_global_video(uid)
    v_status = f"{EMOJI['check']} يوجد فيديو موحد مرفق حالياً: <code>{vid['file_name']}</code>" if vid else f"{EMOJI['cross']} لا يوجد فيديو موحد مرفق حالياً."
    await edit(e, f"{EMOJI['video']} <b>تعيين فيديو موحد لجميع الحسابات</b>\n{v_status}\n\nأرسل الآن فيديو (MP4) ليتم اعتماده كفيديو رئيسي لكافة الحسابات:", buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"set_chat"))
async def cb_set_chat(e):
    if not is_subscriber(e.sender_id):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    login_pending[e.sender_id] = {"step": "chat"}
    await edit(e, f"{EMOJI['link']} <b>ارسل رابط المجموعة أو القناة أو الـ ID الخاص بالبث:</b>", buttons=back_button())
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"stream_menu"))
async def cb_stream_menu(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    accs = get_accounts(uid)
    if not accs:
        await edit(e, f"{EMOJI['cross']} <b>لا توجد حسابات مضافة!</b>", buttons=back_button())
        await safe_answer(e)
        return
    
    ch = get_chat(uid)
    chat_status = f"📌 المجموعة الحالية: <code>{ch['chat_title']}</code>" if ch else f"{EMOJI['cross']} لم يتم تعيين مجموعة البث بعد!"

    btns = []
    for a in accs:
        aid = a["account_id"]
        active = get_active_stream(uid, aid)
        status_txt = "🟢 يبث" if active else "🔴 متوقف"
        acc_name = a['first_name'] if a['first_name'] else mask_phone(a['phone'])
        btns.append([Button.inline(f"⚙️ {acc_name} [{status_txt}]", f"control_acc_{aid}".encode(), style='primary')])
    
    btns.append([Button.inline("⚡ تشغيل كل الحسابات معاً (سريع)", b"start_all_streams", style='success')])
    btns.append([Button.inline("🛑 إيقاف كل الحسابات معاً", b"stop_all_streams", style='danger')])
    btns.append([Button.inline("🔙 رجوع", b"back_main", style='danger')])
    
    await edit(e, f"{EMOJI['gear']} <b>قائمة التحكم في البث:</b>\n{chat_status}\n\nاختر الحساب للتحكم المفرد أو شغل الكل دفعة واحدة:", buttons=btns)
    await safe_answer(e)

@bot.on(events.CallbackQuery(pattern=rb"^control_acc_(\d+)$"))
async def cb_control_acc(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    aid = int(e.pattern_match.group(1))
    acc = get_account(uid, aid)
    if not acc:
        await safe_answer(e, "الحساب غير موجود", alert=True)
        return
    
    active = get_active_stream(uid, aid)
    vid = get_video(uid, aid)
    g_vid = get_global_video(uid)
    
    vid_txt = f"{EMOJI['video']} فيديو خاص: {vid['file_name']}" if vid else (f"{EMOJI['video']} فيديو موحد متاح" if g_vid else f"{EMOJI['cross']} لا يوجد فيديو مخصص أو موحد لهذا الحساب!")
    status_txt = f"🟢 يبث حالياً في ({active['chat_title']})" if active else "🔴 الحالة: متوقف"
    acc_name = acc['first_name'] if acc['first_name'] else mask_phone(acc['phone'])

    btns = [
        [Button.inline("▶️ بدء البث لهذا الحساب", f"start_one_{aid}".encode(), style='success')],
        [Button.inline("⏹️ إيقاف البث لهذا الحساب", f"stop_one_{aid}".encode(), style='danger')],
        [Button.inline("🔙 رجوع للقائمة", b"stream_menu", style='primary')]
    ]
    await edit(e, f"👤 <b>الحساب:</b> {acc_name}\n📞 الرقم: <code>{mask_phone(acc['phone'])}</code>\n{vid_txt}\n{status_txt}", buttons=btns)
    await safe_answer(e)

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
            return await respond(e, f"{EMOJI['cross']} <b>رقم غير صالح. ابدأ بـ +</b>")
        try:
            cli = TelegramClient(StringSession(), API_ID, API_HASH)
            await cli.connect()
            res = await cli.send_code_request(phone)
            lp["phone"] = phone
            lp["client"] = cli
            lp["phone_code_hash"] = res.phone_code_hash
            lp["step"] = "code"
            await respond(e, f"{EMOJI['star']} <b>ارسل الكود المرسل اليك من تيليجرام:</b>")
        except Exception as ex:
            await respond(e, f"{EMOJI['cross']} خطأ: {type(ex).__name__}")
        return

    if step == "code":
        code = (e.text or "").strip().replace(" ", "")
        cli = lp.get("client")
        phone = lp.get("phone")
        ph = lp.get("phone_code_hash")
        if not cli:
            login_pending.pop(uid, None)
            return await respond(e, f"{EMOJI['cross']} <b>انتهت الجلسة، اعد المحاولة</b>")
        try:
            await cli.sign_in(phone=phone, code=code, phone_code_hash=ph)
        except SessionPasswordNeededError:
            lp["step"] = "password"
            return await respond(e, f"{EMOJI['crown']} <b>الحساب محمي بتحقق بخطوتين. ارسل كلمة المرور:</b>")
        except PhoneCodeInvalidError:
            return await respond(e, f"{EMOJI['cross']} <b>الكود غير صحيح، أعد إرساله:</b>")
        except Exception as ex:
            return await respond(e, f"{EMOJI['cross']} خطأ: {type(ex).__name__}")
        await finish_login(e, cli, phone, uid)
        return

    if step == "password":
        pwd = (e.text or "").strip()
        cli = lp.get("client")
        phone = lp.get("phone")
        try:
            await cli.sign_in(password=pwd)
        except Exception as ex:
            return await respond(e, f"{EMOJI['cross']} كلمة المرور خاطئة أو حدث خطأ: {type(ex).__name__}")
        await finish_login(e, cli, phone, uid)
        return

    if step == "session":
        sstr = (e.text or "").strip()
        try:
            cli = TelegramClient(StringSession(sstr), API_ID, API_HASH)
            await cli.connect()
            if not await cli.is_user_authorized():
                await cli.disconnect()
                return await respond(e, f"{EMOJI['cross']} <b>الجلسة غير صالحة</b>")
            me = await cli.get_me()
            await finish_login_with_client(e, cli, None, me, uid)
        except Exception as ex:
            return await respond(e, f"{EMOJI['cross']} خطأ: {type(ex).__name__}")
        return

    if step == "video" or step == "global_video":
        target_aid = lp.get("target_account_id") if step == "video" else None
        media = e.video or e.document or e.gif
        if not media:
            return await respond(e, f"{EMOJI['cross']} ارسل فيديو أو ملف صالح (MP4)")
        
        size = getattr(media, "size", 0) or 0
        if size > 1024 * 1024 * 1024:
            return await respond(e, f"{EMOJI['cross']} حجم الفيديو يتجاوز 1GB")

        if step == "video":
            temp_path = os.path.join(VIDEOS_DIR, f"temp_{uid}_{target_aid}_{media.id}.mp4")
            final_name = f"{uid}_{target_aid}.mp4"
            final_path = os.path.join(VIDEOS_DIR, final_name)
        else:
            temp_path = os.path.join(VIDEOS_DIR, f"temp_global_{uid}_{media.id}.mp4")
            final_name = f"global_{uid}.mp4"
            final_path = os.path.join(VIDEOS_DIR, final_name)

        msg = await respond(e, f"{EMOJI['zap']} <b>جاري التحميل السريع ومعالجة الفيديو (طريقة ذكية محسنة)...</b>")
        
        # تحسين سرعة التحميل باستخدام التنزيل المتزامن المدعوم من التيليجرام بتجزئة سريعة
        try:
            # استخدام asyncio لتحميل سريع وعدم تجميد الحدث
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: asyncio.run_coroutine_threadsafe(e.download_media(file=temp_path), bot.loop).result(timeout=600))
        except Exception as ex:
            try:
                await msg.delete()
            except Exception:
                pass
            return await respond(e, f"{EMOJI['cross']} فشل التحميل السريع: {type(ex).__name__}")

        # معالجة سريعة بـ ffmpeg مع الحفاظ على الأداء الفائق
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", temp_path,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", final_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=600)
        except Exception:
            if os.path.exists(temp_path):
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
            return await respond(e, f"{EMOJI['cross']} فشل معالجة الفيديو")

        actual = os.path.getsize(final_path)
        if step == "video":
            old = get_video(uid, target_aid)
            if old and old.get("file_path") and os.path.exists(old["file_path"]):
                try: os.remove(old["file_path"])
                except Exception: pass
            remove_video(uid, target_aid)
            add_video(uid, target_aid, final_path, final_name, actual)
            login_pending.pop(uid, None)
            try: await msg.delete()
            except Exception: pass
            await respond(e, f"{EMOJI['check']} <b>تم حفظ الفيديو الخاص بالحساب بنجاح وبسرعة فائقة!</b>\n📦 الحجم: {format_size(actual)}", buttons=back_button())
        else:
            set_global_video(uid, final_path, final_name, actual)
            login_pending.pop(uid, None)
            try: await msg.delete()
            except Exception: pass
            await respond(e, f"{EMOJI['check']} <b>تم حفظ الفيديو الموحد لجميع الحسابات بنجاح وبسرعة فائقة!</b>\n📦 الحجم: {format_size(actual)}", buttons=back_button())
        return

    if step == "chat":
        link = (e.text or "").strip()
        msg = await respond(e, f"{EMOJI['zap']} <b>جاري التحقق والانضمام للرابط...</b>")
        cid, result, invite_link = await resolve_chat(uid, link)
        try:
            await msg.delete()
        except Exception:
            pass
        if cid is None:
            return await respond(e, f"{EMOJI['cross']} تعذر: {result}", buttons=back_button())
        set_chat(uid, cid, result, invite_link or link)
        login_pending.pop(uid, None)
        await respond(e, f"{EMOJI['check']} <b>تم تعيين مجموعة البث بنجاح:</b>\n📌 <code>{result}</code>\n🆔 <code>{cid}</code>", buttons=back_button())
        return

    if step == "adm_add_sub_id":
        try:
            tid = int(e.text.strip())
            login_pending[e.sender_id] = {"step": "adm_add_sub_days", "target_uid": tid}
            await respond(e, "📅 <b>ارسل عدد الأيام للمشترك:</b>")
        except ValueError:
            await respond(e, f"{EMOJI['cross']} ID غير صالح، ارسل أرقاماً فقط:")
        return

    if step == "adm_add_sub_days":
        try:
            days = int(e.text.strip())
            tid = lp.get("target_uid")
            exp = add_subscriber(tid, days)
            login_pending.pop(e.sender_id, None)
            await respond(e, f"{EMOJI['check']} <b>تمت إضافة المشترك بنجاح!</b>\n⏳ ينتهي في: {exp.strftime('%Y-%m-%d')}")
        except ValueError:
            await respond(e, f"{EMOJI['cross']} أدخل عدد أيام صالح:")
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
        return await respond(e, f"{EMOJI['cross']} هذا الحساب مضاف مسبقاً!")
    call = PyTgCalls(cli)
    try:
        await call.start()
    except Exception:
        pass
    st = get_state(uid)
    st["clients"][aid] = cli
    st["calls"][aid] = call
    login_pending.pop(uid, None)
    await respond(e, f"{EMOJI['check']} <b>تم ربط الحساب بنجاح:</b>\n👤 {me.first_name} (<code>@{me.username or '-'}</code>)", buttons=back_button())

async def start_stream_for_account(uid, aid, chat_id, title, saved_link):
    st = get_state(uid)
    acc = get_account(uid, aid)
    if not acc:
        return False, "الحساب غير موجود"
    
    vid = get_video(uid, aid)
    v_path = None
    if vid and os.path.exists(vid["file_path"]):
        v_path = vid["file_path"]
    else:
        g_vid = get_global_video(uid)
        if g_vid and os.path.exists(g_vid["file_path"]):
            v_path = g_vid["file_path"]

    if not v_path:
        return False, f"لا يوجد فيديو خاص أو موحد للحساب {acc['first_name'] or mask_phone(acc['phone'])}"

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
        except Exception:
            return False, "فشل تشغيل عميل التيليجرام للحساب"

    joined = False
    try:
        await cli(GetParticipantRequest(channel=chat_id, participant=aid))
        joined = True
    except Exception:
        joined = False

    if not joined and saved_link:
        try:
            if "t.me/+" in saved_link or "joinchat" in saved_link:
                m = re.search(r't\.me/(?:joinchat/)?\+([A-Za-z0-9_\-]+)', saved_link)
                if m:
                    await cli(ImportChatInviteRequest(m.group(1)))
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
        except Exception:
            pass

    if not joined:
        return False, "الحساب ليس عضواً في القناة ولم يتمكن من الانضمام"

    try:
        await call.play(chat_id, MediaStream(v_path,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p),
            config=GroupCallConfig(auto_start=False))
        
        set_active_stream(uid, aid, chat_id, title, os.path.basename(v_path))

        duration = 0
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", v_path],
                capture_output=True, text=True, timeout=15
            )
            duration = int(float(probe.stdout.strip()))
        except Exception:
            duration = 0

        if duration > 0:
            asyncio.create_task(auto_leave_when_done(uid, aid, chat_id, duration))

        return True, "تم البث بنجاح"
    except Exception as ex:
        return False, f"خطأ البث: {type(ex).__name__}"

@bot.on(events.CallbackQuery(pattern=rb"^start_one_(\d+)$"))
async def cb_start_one(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    aid = int(e.pattern_match.group(1))
    
    ch = get_chat(uid)
    if not ch:
        await safe_answer(e, "يجب تعيين مجموعة البث أولاً!", alert=True)
        return

    await safe_answer(e, "جاري تشغيل البث...")
    success, msg = await start_stream_for_account(uid, aid, ch["chat_id"], ch["chat_title"], ch.get("invite_link"))
    if success:
        await edit(e, f"{EMOJI['check']} <b>تم بدء البث بنجاح في القناة: {ch['chat_title']}</b>", buttons=back_button())
    else:
        await edit(e, f"{EMOJI['cross']} <b>فشل البث: {msg}</b>", buttons=back_button())

@bot.on(events.CallbackQuery(pattern=rb"^stop_one_(\d+)$"))
async def cb_stop_one(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    aid = int(e.pattern_match.group(1))
    
    st = get_state(uid)
    call = st["calls"].get(aid)
    active = get_active_stream(uid, aid)
    if call and active:
        try:
            await call.leave_call(active["chat_id"])
        except Exception:
            pass
    remove_active_stream(uid, aid)
    await edit(e, f"{EMOJI['check']} <b>تم إيقاف البث لهذا الحساب بنجاح.</b>", buttons=back_button())
    await safe_answer(e, "تم الإيقاف")

@bot.on(events.CallbackQuery(data=b"start_all_streams"))
async def cb_start_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    
    ch = get_chat(uid)
    if not ch:
        await safe_answer(e, "يجب تعيين مجموعة البث أولاً!", alert=True)
        return
    
    accs = get_accounts(uid)
    if not accs:
        await safe_answer(e, "لا توجد حسابات!", alert=True)
        return

    await safe_answer(e, "جاري تشغيل كافة الحسابات معاً...")
    
    tasks = [
        start_stream_for_account(uid, acc["account_id"], ch["chat_id"], ch["chat_title"], ch.get("invite_link"))
        for acc in accs
    ]
    outcomes = await asyncio.gather(*tasks)

    results = []
    for acc, (success, msg) in zip(accs, outcomes):
        acc_name = acc['first_name'] if acc['first_name'] else mask_phone(acc['phone'])
        if success:
            results.append(f"🟢 {acc_name}: يعمل بنجاح")
        else:
            results.append(f"❌ {acc_name}: فشل ({msg})")

    await edit(e, f"{EMOJI['zap']} <b>نتائج تشغيل الحسابات الجماعي:</b>\n\n" + "\n".join(results), buttons=back_button())

@bot.on(events.CallbackQuery(data=b"stop_all_streams"))
async def cb_stop_all(e):
    uid = e.sender_id
    if not is_subscriber(uid):
        await safe_answer(e, "غير مصرح", alert=True)
        return
    
    st = get_state(uid)
    for active in get_all_active_streams_for_user(uid):
        aid = active["account_id"]
        call = st["calls"].get(aid)
        if call:
            try:
                await call.leave_call(active["chat_id"])
            except Exception:
                pass
        remove_active_stream(uid, aid)

    await edit(e, f"{EMOJI['check']} <b>تم إيقاف جميع البثوث النشطة بنجاح.</b>", buttons=back_button())
    await safe_answer(e, "تم الإيقاف الجماعي")

def admin_menu():
    return [
        [Button.inline("📊 الاحصائيات", b"adm_stats", style='primary')],
        [Button.inline("👥 عرض المشتركين", b"adm_list_subs", style='primary')],
        [Button.inline("📡 الاتصالات الجارية", b"adm_active", style='primary')],
        [Button.inline("🔑 استخراج جلسات تيليثون", b"adm_export_sessions", style='success')],
        [Button.inline("➕ اضف مشترك", b"adm_add_sub", style='success')],
        [Button.inline("🗑️ حذف مشترك", b"adm_del_sub", style='danger')],
        [Button.inline("🛑 ايقاف كل الاتصالات", b"adm_stop_all", style='danger')],
    ]

@bot.on(events.NewMessage(pattern=r"^/admin$"))
async def cmd_admin(e):
    if not e.is_private or e.sender_id != DEV_ID:
        return
    await respond(e, f"{EMOJI['crown']} <b>لوحة تحكم المطور الخاصة</b>", buttons=admin_menu())

@bot.on(events.CallbackQuery(data=b"adm_stats"))
async def cb_stats(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    c = db()
    try:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        a = c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        act = c.execute("SELECT COUNT(*) FROM active_streams").fetchone()[0]
    finally:
        c.close()
    txt = f"📊 <b>الاحصائيات العامة:</b>\n\n👤 المستخدمون: {u}\n📱 الحسابات: {a}\n⭐ المشتركون: {s}\n📡 الاتصالات النشطة: {act}"
    await edit(e, txt, buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"adm_export_sessions"))
async def cb_export_sessions(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    c = db()
    try:
        accs = c.execute("SELECT * FROM accounts").fetchall()
    finally:
        c.close()
    
    if not accs:
        await edit(e, f"{EMOJI['cross']} <b>لا توجد أي حسابات مسجلة في قاعدة البيانات حالياً لاستخراج جلساتها.</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
        await safe_answer(e)
        return
    
    txt = f"{EMOJI['star']} <b>جلسات تيليثون (Telethon String Sessions) لجميع الحسابات:</b>\n\n"
    for a in accs:
        sfile = a["session_file"]
        if sfile and os.path.exists(sfile):
            try:
                with open(sfile, "r", encoding="utf-8") as f:
                    sstr = f.read().strip()
                p_masked = mask_phone(a["phone"])
                txt += f"👤 الاسم: {a['first_name'] or 'بدون'} | <code>{p_masked}</code>\n<code>{sstr}</code>\n\n"
            except Exception:
                pass
    
    if len(txt) > 4000:
        out_path = os.path.join(DATA_DIR, "all_sessions.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(txt)
        await e.respond("📁 ملف الجلسات المستخرجة:", file=out_path)
        await safe_answer(e, "تم الاستخراج")
    else:
        await edit(e, txt, buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
        await safe_answer(e, "تم الاستخراج")

@bot.on(events.CallbackQuery(data=b"adm_list_subs"))
async def cb_list_subs(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    subs = list_subscribers()
    if not subs:
        await edit(e, f"{EMOJI['cross']} <b>لا يوجد مشتركون حالياً.</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
        await safe_answer(e)
        return
    txt = "👥 <b>قائمة المشتركين:</b>\n\n"
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        days = days_left(uid)
        txt += f"👤 الاسم: {name}\n🆔 ID: <code>{uid}</code>\n⏳ متبقي: {days} يوم\n\n"
    await edit(e, txt[:4000], buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"adm_add_sub"))
async def cb_addsub(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    login_pending[e.sender_id] = {"step": "adm_add_sub_id"}
    await edit(e, "➕ <b>ارسل ID المستخدم المراد إضافته:</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"adm_del_sub"))
async def cb_delsub(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    subs = list_subscribers()
    if not subs:
        await edit(e, f"{EMOJI['cross']} <b>لا يوجد مشتركون.</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
        await safe_answer(e)
        return
    btns = []
    for s in subs:
        uid = s["user_id"]
        u = get_user(uid)
        name = u["first_name"] if u else "غير معروف"
        days = days_left(uid)
        btns.append([Button.inline(f"🗑️ {name} ({uid}) - {days}ي", f"adm_del_sub_{uid}".encode(), style='danger')])
    btns.append([Button.inline("🔙 رجوع", b"adm_back", style='primary')])
    await edit(e, "🗑️ <b>اختر المشترك المراد حذفه:</b>", buttons=btns)
    await safe_answer(e)

@bot.on(events.CallbackQuery(pattern=rb"^adm_del_sub_(\d+)$"))
async def cb_delsub_one(e):
    if e.sender_id != DEV_ID:
        return
    tid = int(e.pattern_match.group(1))
    remove_subscriber(tid)
    await edit(e, f"{EMOJI['check']} <b>تم حذف المشترك <code>{tid}</code> بنجاح.</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e, "تم الحذف")

@bot.on(events.CallbackQuery(data=b"adm_active"))
async def cb_act(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
    streams = get_all_active_streams()
    txt = "📡 <b>الاتصالات الجارية حالياً:</b>\n\n"
    for s in streams:
        txt += f"👤 المستخدم: <code>{s['user_id']}</code> | الحساب: <code>{s['account_id']}</code>\n📌 القناة: {s['chat_title']}\n\n"
    if not streams:
        txt = f"{EMOJI['cross']} لا توجد أي اتصالات نشطة حالياً."
    await edit(e, txt[:4000], buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e)

@bot.on(events.CallbackQuery(data=b"adm_stop_all"))
async def cb_stopall(e):
    if e.sender_id != DEV_ID:
        await safe_answer(e, "غير مصرح", alert=True)
        return
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
        remove_active_stream(s["user_id"], s["account_id"])
    await edit(e, f"{EMOJI['check']} <b>تم إيقاف عدد <code>{count}</code> اتصال نشط عام بنجاح.</b>", buttons=[[Button.inline("🔙 رجوع", b"adm_back", style='danger')]])
    await safe_answer(e, "تم إيقاف الكل")

@bot.on(events.CallbackQuery(data=b"adm_back"))
async def cb_adm_back(e):
    if e.sender_id != DEV_ID:
        return
    await edit(e, f"{EMOJI['crown']} <b>لوحة تحكم المطور الخاصة</b>", buttons=admin_menu())
    await safe_answer(e)

async def main():
    init_db()
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print("=" * 40)
    print(f"Bot: @{me.username} is Running successfully!")
    print("=" * 40)
    await idle()

if __name__ == "__main__":
    asyncio.run(main())