"""
Rosyy Bot ❀  — group manager + chatty Rosyy (Odisha) in one bot.

Group management : kick/ban/mute/warn/promote/purge/antilink/antiflood/badword ... (prefix  /  .  !)
Chat persona     : Hinglish chat, sees pics, hears voice notes, sends stickers, voice notes & reactions.
AI               : Google Gemini (model chain + key rotation). Games: coins, rob, kill, fd, invest ...
Storage          : MongoDB, one or many (falls back to memory if MONGO_URI is not set).
Owner            : everything editable from Telegram (/panel).
Hosting          : Render web service (webhook) — or polling locally / Termux.

Env vars: see README.md / .env.example
"""
import asyncio
import base64
import copy
import hashlib
import html
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import (BotCommand, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
                      ReactionTypeEmoji, Update)
from telegram.constants import ChatAction, ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rosyy")

# ───────────────────────── config (env = first-boot defaults; almost everything is editable from Telegram) ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "rosyy")
OWNER_ID = int(os.getenv("OWNER_ID", "8536757095") or 0)
BOT_NAME = os.getenv("BOT_NAME", "Rosyy")


def _env_keys():
    raw = [os.getenv(n, "") for n in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GOOGLE_API_KEY")]
    raw += os.getenv("GEMINI_API_KEYS", "").split(",")
    out = []
    for k in raw:
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


ENV_KEYS = _env_keys()  # keys from env (read-only); more keys can be added from Telegram with /addkey
GEMINI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
DEFAULT_MODELS = [x.strip() for x in os.getenv("GEMINI_MODELS", "").split(",") if x.strip()] or [
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
ENV_STICKER_PACKS = [x.strip() for x in os.getenv("STICKER_PACKS", "").split(",") if x.strip()]

PORT = os.getenv("PORT", "")
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "hook"
WEBHOOK_SECRET = hashlib.sha256(("rosyy:" + BOT_TOKEN).encode()).hexdigest()

IST = timezone(timedelta(hours=5, minutes=30))
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ANON_ADMIN = 1087968824  # GroupAnonymousBot
WARN_LIMIT = 3
HIST_KEEP = 18

# Runtime settings — change any of these from Telegram:  /set <key> <value>   (owner only)
SETTABLE = {
    # key: (default, type, what it does)
    "ai_timeout": (15.0, float, "seconds to wait for one model before switching to the next one"),
    "ai_temp": (0.9, float, "creativity of Rosyy's replies (0-2)"),
    "ai_tokens": (110, int, "max length of a reply (small = short chat-style replies)"),
    "voice_reply": (0, int, "1 = Rosyy may send voice notes, 0 = never (default off)"),
    "react_chance": (0.10, float, "chance (0-1) she reacts to random group messages"),
    "sticker_chance": (0.05, float, "chance (0-1) she drops a random sticker in group chat"),
    "tts_voice": (os.getenv("TTS_VOICE", "en-IN-NeerjaNeural"), str, "voice-note voice (edge-tts name)"),
    "voice_chance": (float(os.getenv("VOICE_CHANCE", "0.10")), float, "chance (0-1) of a voice note reply (only if voice_reply = 1)"),
    "persona_extra": ("", str, "extra personality lines added to Rosyy's prompt"),
    "start_text": ("", str, "custom /start message (empty = built-in)"),
    "start_coins": (1000, int, "coins a new player starts with"),
    "daily_min": (500, int, "/daily minimum"),
    "daily_max": (1000, int, "/daily maximum"),
    "streak_bonus": (50, int, "extra coins per day of daily streak (max 30 days)"),
    "rob_cd": (600, int, "/rob cooldown (seconds)"),
    "rob_success": (0.55, float, "/rob success chance (0-1)"),
    "rob_max_pct": (0.35, float, "/rob steals up to this share of the victim's wallet"),
    "kill_cd": (900, int, "/kill cooldown (seconds)"),
    "kill_success": (0.45, float, "/kill success chance (0-1)"),
    "kill_cost": (300, int, "bullet cost of one /kill"),
    "dead_hours": (2, float, "hours a killed player stays dead"),
    "revive_cost": (400, int, "cost of /revive"),
    "protect_1d": (1500, int, "price of 1 day protection"),
    "protect_2d": (2800, int, "price of 2 day protection"),
    "fd_rate_1d": (0.03, float, "FD interest for 1 day (0.03 = 3%)"),
    "fd_rate_3d": (0.10, float, "FD interest for 3 days"),
    "fd_rate_7d": (0.25, float, "FD interest for 7 days"),
    "invest_cd": (3600, int, "/invest cooldown (seconds)"),
    "action_cost": (20, int, "base price of social actions (slap, hug ...)"),
}
CFG = {"join_chats": [], "packs": [], "mongo_extra": [], "mongo_active": 0, "gemini_keys": [],
       "models": list(DEFAULT_MODELS), "vals": {}}


def cfg(key):
    v = CFG["vals"].get(key)
    return SETTABLE[key][0] if v is None else v


# ───────────────────────── text helpers ─────────────────────────
_FX = str.maketrans({"a": "α", "k": "к", "r": "ʀ"})


def fx(s) -> str:
    """Label font: Pαcкʀʀ style."""
    return str(s).translate(_FX)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def mention(uid, name) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name or "user")}</a>'


_DUR = re.compile(r"^(\d+)\s*([smhdw]?)$", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_dur(s):
    """'30m' -> 1800 seconds, None if not a duration."""
    m = _DUR.match(str(s).strip())
    if not m:
        return None
    n = int(m.group(1))
    return n * _UNIT[(m.group(2) or "m").lower()] if n > 0 else None


def fmt_dur(sec) -> str:
    for unit, name in ((86400, "d"), (3600, "h"), (60, "m")):
        if sec >= unit:
            return f"{sec // unit}{name}"
    return f"{sec}s"


# ───────────────────────── storage (memory / one MongoDB / many MongoDBs with failover) ─────────────────────────
class MemBackend:
    kind = "memory"
    uri = ""

    def __init__(self):
        self.d = defaultdict(dict)

    async def ping(self):
        return True

    async def get(self, coll, _id):
        v = self.d[coll].get(_id)
        return copy.deepcopy(v) if v is not None else None

    async def put(self, coll, _id, doc):
        self.d[coll][_id] = copy.deepcopy(doc)

    async def find_by(self, coll, field, value):
        for i, v in self.d[coll].items():
            if v.get(field) == value:
                return i
        return None

    async def top(self, coll, field, n):
        rows = sorted(self.d[coll].items(), key=lambda kv: kv[1].get(field, 0), reverse=True)[:n]
        return [(i, copy.deepcopy(v)) for i, v in rows]

    async def all(self, coll, limit=500):
        return [(i, copy.deepcopy(v)) for i, v in list(self.d[coll].items())[:limit]]

    async def delete(self, coll, _id):
        self.d[coll].pop(_id, None)


class MongoBackend:
    kind = "mongodb"

    def __init__(self, uri, dbname):
        from motor.motor_asyncio import AsyncIOMotorClient

        self.uri = uri
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        self.db = self.client[dbname]

    async def ping(self):
        await self.db.command("ping")
        return True

    async def init(self):
        await self.ping()
        await self.db.users.create_index("username")
        await self.db.eco.create_index("coins")

    async def get(self, coll, _id):
        d = await self.db[coll].find_one({"_id": _id})
        if d:
            d.pop("_id", None)
        return d

    async def put(self, coll, _id, doc):
        await self.db[coll].replace_one({"_id": _id}, doc, upsert=True)

    async def find_by(self, coll, field, value):
        d = await self.db[coll].find_one({field: value}, {"_id": 1})
        return d["_id"] if d else None

    async def top(self, coll, field, n):
        return [(d.pop("_id"), d) async for d in self.db[coll].find().sort(field, -1).limit(n)]

    async def all(self, coll, limit=500):
        return [(d.pop("_id"), d) async for d in self.db[coll].find().limit(limit)]

    async def delete(self, coll, _id):
        await self.db[coll].delete_one({"_id": _id})


class Multi:
    """Several backends behind one API. Reads look in every store, writes go to the active one and
    fail over to the next one when a store is full / down. Config always lives on store #1."""

    def __init__(self, stores):
        self.stores = list(stores)
        self.active = 0
        self.loc = {}

    @property
    def kind(self):
        return " + ".join(s.kind for s in self.stores)

    def reset_loc(self):
        self.loc.clear()

    async def get(self, coll, _id):
        k = (coll, _id)
        n = len(self.stores)
        if n == 1:
            d = await self.stores[0].get(coll, _id)
            if d is not None:
                d.pop("_ts", None)
            return d
        order = list(range(n))
        known = self.loc.get(k)
        if known is not None and known < n:
            d = None
            try:
                d = await self.stores[known].get(coll, _id)
            except Exception as e:  # noqa: BLE001
                log.warning("store %d read failed: %r", known + 1, e)
            if d is not None:
                d.pop("_ts", None)
                return d
            order.remove(known)
        best, best_i, err = None, None, None
        for i in order:
            try:
                d = await self.stores[i].get(coll, _id)
            except Exception as e:  # noqa: BLE001
                err = e
                continue
            if d is not None and (best is None or d.get("_ts", 0) > best.get("_ts", 0)):
                best, best_i = d, i
        if best is None:
            if err:
                raise err
            return None
        if len(self.loc) > 60000:
            self.loc.clear()
        self.loc[k] = best_i
        best.pop("_ts", None)
        return best

    async def put(self, coll, _id, doc):
        k = (coll, _id)
        n = len(self.stores)
        doc = dict(doc)
        doc["_ts"] = time.time()
        first = self.loc.get(k, self.active)
        if first >= n:
            first = 0
        order = [first] + [i for i in range(n) if i != first]
        err = None
        for i in order:
            try:
                await self.stores[i].put(coll, _id, doc)
            except Exception as e:  # noqa: BLE001
                err = e
                log.warning("store %d write failed: %r", i + 1, e)
                continue
            old = self.loc.get(k)
            self.loc[k] = i
            if old is not None and old != i and old < n:
                spawn(self._rm(old, coll, _id))
            return
        raise err or RuntimeError("no store")

    async def _rm(self, i, coll, _id):
        try:
            await self.stores[i].delete(coll, _id)
        except Exception:  # noqa: BLE001
            pass

    async def find_by(self, coll, field, value):
        err = None
        for s in self.stores:
            try:
                r = await s.find_by(coll, field, value)
            except Exception as e:  # noqa: BLE001
                err = e
                continue
            if r is not None:
                return r
        if err:
            raise err
        return None

    async def top(self, coll, field, n):
        rows, err, ok = [], None, False
        for s in self.stores:
            try:
                rows += await s.top(coll, field, n)
                ok = True
            except Exception as e:  # noqa: BLE001
                err = e
        if not ok and err:
            raise err
        seen, out = set(), []
        for i, d in sorted(rows, key=lambda r: r[1].get(field, 0), reverse=True):
            if i not in seen:
                seen.add(i)
                d.pop("_ts", None)
                out.append((i, d))
        return out[:n]

    async def all(self, coll, limit=500):
        out, seen = [], set()
        for s in self.stores:
            try:
                for i, d in await s.all(coll, limit):
                    if i not in seen:
                        seen.add(i)
                        d.pop("_ts", None)
                        out.append((i, d))
            except Exception as e:  # noqa: BLE001
                log.warning("all(%s) failed on a store: %r", coll, e)
        return out[:limit]


class Facade:
    """What the rest of the bot talks to."""

    def __init__(self, backend):
        self.b = backend

    @property
    def kind(self):
        return self.b.kind

    async def load_chat(self, cid):
        return await self.b.get("chats", cid)

    async def save_chat(self, cid, doc):
        await self.b.put("chats", cid, doc)

    async def save_user(self, uid, username, name):
        await self.b.put("users", uid, {"username": username, "name": name})

    async def user_by_name(self, username):
        return await self.b.find_by("users", "username", username)

    async def load_hist(self, key):
        d = await self.b.get("hist", key)
        return list(d["msgs"]) if d else []

    async def save_hist(self, key, msgs):
        await self.b.put("hist", key, {"msgs": msgs})

    async def save_sticker(self, uniq, file_id, emoji):
        await self.b.put("stickers", uniq, {"file_id": file_id, "emoji": emoji})

    async def load_stickers(self):
        return [(d["file_id"], d.get("emoji", "")) for _i, d in await self.b.all("stickers", 500)]

    async def eco_get(self, uid):
        return await self.b.get("eco", uid)

    async def eco_put(self, uid, doc):
        await self.b.put("eco", uid, doc)

    async def eco_top(self, n):
        return await self.b.top("eco", "coins", n)

    async def cfg_load(self):
        d = await self.b.stores[0].get("cfg", "main")
        if d:
            d.pop("_ts", None)
        return d

    async def cfg_save(self, doc):
        await self.b.stores[0].put("cfg", "main", doc)


ST = Facade(Multi([MemBackend()]))

DEFAULTS = {
    "antiflood": 0, "antilink": False, "bioremove": False, "antiforward": False, "join_remove": False,
    "ai": True, "games": True, "welcome": True, "goodbye": True, "badwords": [], "approved": [], "warns": {}, "bw": {}, "promoted": [],
}
CHATS: dict = {}
NAMES: dict = {}
UNAME: dict = {}
BOT_MSGS: dict = defaultdict(lambda: deque(maxlen=300))
_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def cdoc(cid):
    d = CHATS.get(cid)
    if d is None:
        try:
            d = await ST.load_chat(cid) or {}
        except Exception as e:
            log.warning("load_chat failed: %r", e)
            d = {}
        for k, v in DEFAULTS.items():
            d.setdefault(k, json.loads(json.dumps(v)))
        CHATS[cid] = d
    return d


async def csave(cid):
    try:
        await ST.save_chat(cid, CHATS[cid])
    except Exception as e:
        log.warning("save_chat failed: %r", e)


def remember(user):
    if not user or user.is_bot:
        return
    name = user.full_name or user.first_name or ""
    uname = (user.username or "").lower()
    if NAMES.get(user.id) == name and (not uname or UNAME.get(uname) == user.id):
        return
    NAMES[user.id] = name
    if uname:
        UNAME[uname] = user.id

    async def _save():
        try:
            await ST.save_user(user.id, uname, name)
        except Exception as e:
            log.warning("save_user failed: %r", e)

    spawn(_save())


# ───────────────────────── admin / rights ─────────────────────────
ADM: dict = {}


async def admins(bot, cid):
    t, d = ADM.get(cid, (0, {}))
    if time.time() - t > 60:
        try:
            d = {m.user.id: m for m in await bot.get_chat_administrators(cid)}
        except TelegramError as e:
            log.warning("get_chat_administrators failed: %r", e)
        ADM[cid] = (time.time(), d)
    return d


async def is_admin(bot, chat, uid) -> bool:
    return uid in (OWNER_ID, ANON_ADMIN) or uid in await admins(bot, chat.id)


class C:
    """One parsed command call."""

    def __init__(self, update, ctx, cmd, prefix, rest):
        self.update, self.ctx, self.cmd, self.prefix = update, ctx, cmd, prefix
        self.msg, self.chat, self.user, self.bot = update.effective_message, update.effective_chat, update.effective_user, ctx.bot
        self.rest = rest or ""
        self.args = self.rest.split()


async def can(c, right) -> bool:
    uid = c.user.id
    if uid in (OWNER_ID, ANON_ADMIN):
        return True
    m = (await admins(c.bot, c.chat.id)).get(uid)
    if not m:
        return False
    if m.status == ChatMemberStatus.OWNER or right == "admin":
        return True
    return bool(getattr(m, right, False))


def track(cid, mid):
    BOT_MSGS[cid].append(mid)


async def say(c, text, markup=None):
    try:
        m = await c.msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest:  # the message we reply to is gone
        m = await c.bot.send_message(c.chat.id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    track(c.chat.id, m.message_id)
    return m


async def delete(bot, cid, mid):
    try:
        await bot.delete_message(cid, mid)
    except TelegramError:
        pass


async def delete_later(bot, cid, mid, sec=5):
    await asyncio.sleep(sec)
    await delete(bot, cid, mid)


async def bulk_delete(bot, cid, ids):
    ids = sorted(set(ids))
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        try:
            await bot.delete_messages(cid, part)
        except (TelegramError, AttributeError):
            for mid in part:
                await delete(bot, cid, mid)


# ───────────────────────── target resolving ─────────────────────────
async def get_target(c):
    """(user_id, name, remaining_args) from reply / text mention / @username / user id."""
    msg, args = c.msg, list(c.args)
    rep = msg.reply_to_message
    if rep and rep.from_user:
        remember(rep.from_user)
        return rep.from_user.id, rep.from_user.full_name, args
    for e in msg.entities or []:
        if e.type == MessageEntity.TEXT_MENTION and e.user:
            remember(e.user)
            return e.user.id, e.user.full_name, args[1:]
    if args:
        a = args[0]
        uid = None
        if a.isdigit():
            uid = int(a)
        elif a.startswith("@") and len(a) > 1:
            uid = UNAME.get(a[1:].lower())
            if uid is None:
                try:
                    uid = await ST.user_by_name(a[1:].lower())
                except Exception:
                    uid = None
        if uid:
            return uid, NAMES.get(uid) or a, args[1:]
    return None


async def pick_target(c, protect=True):
    t = await get_target(c)
    if not t:
        await say(c, "Reply to a user, or give <code>@username</code> / user id.\n"
                     "<i>(@username works only for people I have seen chatting here)</i>")
        return None
    uid, _name, _rest = t
    if uid == c.bot.id:
        await say(c, "Nice try 😌")
        return None
    if protect and await is_admin(c.bot, c.chat, uid):
        await say(c, "I can't do that to an admin.")
        return None
    return t


# ───────────────────────── moderation commands ─────────────────────────
def perms(**on):
    names = ("can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
             "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
             "can_add_web_page_previews", "can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics")
    return ChatPermissions(**{n: bool(on.get(n, False)) for n in names})


PERM_FLAGS = ["can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
              "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
              "can_add_web_page_previews", "can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics"]
RES_MAP = {
    "msg": ["can_send_messages"],
    "media": ["can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos", "can_send_video_notes", "can_send_voice_notes"],
    "sticker": ["can_send_other_messages"],
    "gif": ["can_send_other_messages"],
    "poll": ["can_send_polls"],
    "link": ["can_add_web_page_previews"],
    "invite": ["can_invite_users"],
    "pin": ["can_pin_messages"],
    "info": ["can_change_info"],
}


async def _drop_reply(c):
    rep = c.msg.reply_to_message
    if rep:
        await delete(c.bot, c.chat.id, rep.message_id)


async def _ban(c, drop=False, silent=False):
    t = await pick_target(c)
    if not t:
        return
    uid, name, _ = t
    if drop:
        await _drop_reply(c)
    await c.bot.ban_chat_member(c.chat.id, uid)
    if silent:
        await delete(c.bot, c.chat.id, c.msg.message_id)
    else:
        await say(c, f"🔨 Banned {mention(uid, name)}.")


async def cmd_ban(c): await _ban(c)
async def cmd_dban(c): await _ban(c, drop=True)
async def cmd_sban(c): await _ban(c, silent=True)


async def cmd_unban(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    await c.bot.unban_chat_member(c.chat.id, uid, only_if_banned=True)
    await say(c, f"✅ Unbanned {mention(uid, name)}.")


async def _kick(c, silent=False):
    t = await pick_target(c)
    if not t:
        return
    uid, name, _ = t
    await c.bot.ban_chat_member(c.chat.id, uid)
    await c.bot.unban_chat_member(c.chat.id, uid, only_if_banned=True)
    if silent:
        await delete(c.bot, c.chat.id, c.msg.message_id)
    else:
        await say(c, f"👢 Kicked {mention(uid, name)}.")


async def cmd_kick(c): await _kick(c)
async def cmd_skick(c): await _kick(c, silent=True)


async def cmd_kickme(c):
    if await is_admin(c.bot, c.chat, c.user.id):
        await say(c, "Admins can't be kicked 😅")
        return
    await c.bot.ban_chat_member(c.chat.id, c.user.id)
    await c.bot.unban_chat_member(c.chat.id, c.user.id, only_if_banned=True)


async def _mute(c, drop=False, silent=False):
    t = await pick_target(c)
    if not t:
        return
    uid, name, rest = t
    secs = parse_dur(rest[0]) if rest else None
    until = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None
    if drop:
        await _drop_reply(c)
    await c.bot.restrict_chat_member(c.chat.id, uid, perms(), until_date=until)
    if silent:
        await delete(c.bot, c.chat.id, c.msg.message_id)
    else:
        await say(c, f"🔇 Muted {mention(uid, name)} " + (f"for {fmt_dur(secs)}." if secs else "until unmuted."))


async def cmd_mute(c): await _mute(c)
async def cmd_dmute(c): await _mute(c, drop=True)
async def cmd_smute(c): await _mute(c, silent=True)


async def cmd_unmute(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    chat = await c.bot.get_chat(c.chat.id)
    p = chat.permissions or perms(**{f: True for f in PERM_FLAGS})
    await c.bot.restrict_chat_member(c.chat.id, uid, p)
    await say(c, f"🔊 Unmuted {mention(uid, name)}.")


async def cmd_warn(c):
    t = await pick_target(c)
    if not t:
        return
    uid, name, rest = t
    d = await cdoc(c.chat.id)
    w = d["warns"].setdefault(str(uid), [])
    w.append((" ".join(rest) or "no reason")[:100])
    n = len(w)
    if n >= WARN_LIMIT:
        d["warns"].pop(str(uid), None)
        await c.bot.ban_chat_member(c.chat.id, uid)
        await say(c, f"🚫 {mention(uid, name)} reached {WARN_LIMIT}/{WARN_LIMIT} warns and was banned.")
    else:
        await say(c, f"⚠️ {mention(uid, name)} warned ({n}/{WARN_LIMIT}).\n<i>{esc(w[-1])}</i>")
    await csave(c.chat.id)


async def cmd_warns(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    w = (await cdoc(c.chat.id))["warns"].get(str(uid), [])
    if not w:
        await say(c, f"✅ {mention(uid, name)} has no warnings.")
        return
    lines = "\n".join(f"{i}. {esc(r)}" for i, r in enumerate(w, 1))
    await say(c, f"⚠️ {mention(uid, name)}: {len(w)}/{WARN_LIMIT} warns\n{lines}")


async def cmd_unwarn(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    d = await cdoc(c.chat.id)
    w = d["warns"].get(str(uid), [])
    if not w:
        await say(c, "Nothing to remove.")
        return
    w.pop()
    await csave(c.chat.id)
    await say(c, f"✅ Removed 1 warn from {mention(uid, name)} ({len(w)}/{WARN_LIMIT}).")


async def cmd_clearwarns(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    d = await cdoc(c.chat.id)
    d["warns"].pop(str(uid), None)
    await csave(c.chat.id)
    await say(c, f"🧹 Cleared all warns of {mention(uid, name)}.")


async def cmd_res(c):
    t = await pick_target(c)
    if not t:
        return
    uid, name, rest = t
    ops = [(a[0], a[1:].lower()) for a in rest if len(a) > 1 and a[0] in "+-" and a[1:].lower() in RES_MAP]
    if not ops:
        await say(c, "Usage: <code>/res &lt;user&gt; -media +msg</code>\nPowers: " + ", ".join(f"<code>{p}</code>" for p in RES_MAP))
        return
    m = await c.bot.get_chat_member(c.chat.id, uid)
    src = m if m.status == ChatMemberStatus.RESTRICTED else (await c.bot.get_chat(c.chat.id)).permissions
    cur = {f: (getattr(src, f, None) is not False) for f in PERM_FLAGS}
    for sign, power in ops:
        for f in RES_MAP[power]:
            cur[f] = sign == "+"
    await c.bot.restrict_chat_member(c.chat.id, uid, ChatPermissions(**cur))
    await say(c, f"🔒 Updated permissions of {mention(uid, name)}: " + " ".join(f"{s}{p}" for s, p in ops))


# ───────────────────────── admin power commands ─────────────────────────
ADMIN_FLAGS = ["can_manage_chat", "can_delete_messages", "can_manage_video_chats", "can_restrict_members",
               "can_promote_members", "can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics"]
LEVELS = {
    0: ["can_manage_chat", "can_invite_users"],
    1: ["can_delete_messages", "can_pin_messages"],
    2: ["can_restrict_members", "can_manage_video_chats"],
    3: ["can_change_info", "can_promote_members", "can_manage_topics"],
}
POWERS = {"delete": "can_delete_messages", "ban": "can_restrict_members", "restrict": "can_restrict_members",
          "pin": "can_pin_messages", "invite": "can_invite_users", "info": "can_change_info",
          "vc": "can_manage_video_chats", "promote": "can_promote_members", "topics": "can_manage_topics"}


async def cmd_promote(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, rest = t
    lvl = int(rest[0]) if rest and rest[0] in "0123" and len(rest[0]) == 1 else 1
    flags = {f: False for f in ADMIN_FLAGS}
    for n in range(lvl + 1):
        for f in LEVELS[n]:
            flags[f] = True
    await c.bot.promote_chat_member(c.chat.id, uid, **flags)
    d = await cdoc(c.chat.id)
    if uid not in d["promoted"]:
        d["promoted"].append(uid)
    await csave(c.chat.id)
    ADM.pop(c.chat.id, None)
    await say(c, f"👑 Promoted {mention(uid, name)} (level {lvl}).")


async def _demote(c, uid):
    await c.bot.promote_chat_member(c.chat.id, uid, **{f: False for f in ADMIN_FLAGS}, is_anonymous=False)
    d = await cdoc(c.chat.id)
    if uid in d["promoted"]:
        d["promoted"].remove(uid)
    await csave(c.chat.id)
    ADM.pop(c.chat.id, None)


async def cmd_demote(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    try:
        await _demote(c, uid)
    except BadRequest:
        await say(c, "I can only demote admins that I promoted.")
        return
    await say(c, f"⬇️ Demoted {mention(uid, name)}.")


async def cmd_demote_all(c):
    d = await cdoc(c.chat.id)
    done = 0
    for uid in list(d["promoted"]):
        try:
            await _demote(c, uid)
            done += 1
        except TelegramError:
            d["promoted"].remove(uid)
    await csave(c.chat.id)
    await say(c, f"⬇️ Demoted {done} admin(s) promoted by me.")


async def _power(c, grant):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, rest = t
    key = POWERS.get(rest[0].lower()) if rest else None
    if not key:
        await say(c, f"Usage: <code>/{c.cmd} &lt;user&gt; &lt;power&gt;</code>\nPowers: " + ", ".join(f"<code>{p}</code>" for p in POWERS))
        return
    ADM.pop(c.chat.id, None)
    m = (await admins(c.bot, c.chat.id)).get(uid)
    if not m or m.status != ChatMemberStatus.ADMINISTRATOR:
        await say(c, "That user is not an admin yet — use <code>/promote</code> first.")
        return
    cur = {f: bool(getattr(m, f, False)) for f in ADMIN_FLAGS}
    cur[key] = grant
    cur["can_manage_chat"] = True
    try:
        await c.bot.promote_chat_member(c.chat.id, uid, **cur)
    except BadRequest:
        await say(c, "I can only edit admins that I promoted.")
        return
    ADM.pop(c.chat.id, None)
    await say(c, f"{'➕' if grant else '➖'} {rest[0].lower()} power {'given to' if grant else 'removed from'} {mention(uid, name)}.")


async def cmd_add(c): await _power(c, True)
async def cmd_remove(c): await _power(c, False)


async def cmd_title(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, rest = t
    title = " ".join(rest)[:16]
    if not title:
        await say(c, "Usage: <code>/title &lt;user&gt; &lt;title&gt;</code>")
        return
    try:
        await c.bot.set_chat_administrator_custom_title(c.chat.id, uid, title)
    except BadRequest:
        await say(c, "I can only set titles for admins that I promoted.")
        return
    await say(c, f"🏷 Title of {mention(uid, name)} → <b>{esc(title)}</b>")


# ───────────────────────── message commands ─────────────────────────
async def cmd_pin(c):
    rep = c.msg.reply_to_message
    if not rep:
        await say(c, "Reply to a message to pin it. (<code>/pin loud</code> notifies everyone)")
        return
    loud = any(a.lower() == "loud" for a in c.args)
    await c.bot.pin_chat_message(c.chat.id, rep.message_id, disable_notification=not loud)
    await say(c, "📌 Pinned" + (" (loud)." if loud else "."))


async def cmd_unpin(c):
    rep = c.msg.reply_to_message
    await c.bot.unpin_chat_message(c.chat.id, message_id=rep.message_id if rep else None)
    await say(c, "📍 Unpinned.")


async def cmd_d(c):
    rep = c.msg.reply_to_message
    if rep:
        await delete(c.bot, c.chat.id, rep.message_id)
    await delete(c.bot, c.chat.id, c.msg.message_id)


async def _purge(c, n=None):
    mid = c.msg.message_id
    rep = c.msg.reply_to_message
    if n is not None:
        ids = range(max(1, mid - n), mid + 1)
    elif rep:
        ids = range(max(rep.message_id, mid - 1000), mid + 1)
    else:
        await say(c, "Reply to a message, or use <code>/purge 20</code>.")
        return
    await bulk_delete(c.bot, c.chat.id, list(ids))
    m = await c.bot.send_message(c.chat.id, "🧹 Cleaned.")
    spawn(delete_later(c.bot, c.chat.id, m.message_id, 4))


async def cmd_purge(c):
    n = int(c.args[0]) if c.args and c.args[0].isdigit() else None
    await _purge(c, min(n, 500) if n else None)


async def cmd_delmsg(c):
    if not (c.args and c.args[0].isdigit()):
        await say(c, "Usage: <code>/delmsg 10</code>")
        return
    await _purge(c, min(int(c.args[0]), 500))


async def cmd_cleanbot(c):
    ids = list(BOT_MSGS[c.chat.id])
    BOT_MSGS[c.chat.id].clear()
    await bulk_delete(c.bot, c.chat.id, ids + [c.msg.message_id])


# ───────────────────────── settings commands ─────────────────────────
def onoff(arg):
    a = (arg or "").lower()
    return True if a in ("on", "yes", "enable", "1") else False if a in ("off", "no", "disable", "0") else None


async def _toggle(c, key, label):
    d = await cdoc(c.chat.id)
    v = onoff(c.args[0] if c.args else "")
    if v is None:
        await say(c, f"{label}: <b>{'ON' if d[key] else 'OFF'}</b>\nUse <code>/{c.cmd} on</code> or <code>/{c.cmd} off</code>")
        return
    d[key] = v
    await csave(c.chat.id)
    await say(c, f"{label} is now <b>{'ON ✅' if v else 'OFF ❌'}</b>")


async def cmd_antilink(c): await _toggle(c, "antilink", "Anti-link")
async def cmd_bioremove(c): await _toggle(c, "bioremove", "Bio-link filter")
async def cmd_antiforward(c): await _toggle(c, "antiforward", "Anti-forward")
async def cmd_join_remove(c): await _toggle(c, "join_remove", "Join/leave cleaner")
async def cmd_ai(c): await _toggle(c, "ai", "Rosyy chat")
async def cmd_welcome(c): await _toggle(c, "welcome", "Welcome message")
async def cmd_goodbye(c): await _toggle(c, "goodbye", "Goodbye roast")


async def cmd_antiflood(c):
    d = await cdoc(c.chat.id)
    a = c.args[0].lower() if c.args else ""
    if a in ("on", "yes", "1"):
        d["antiflood"] = 5
    elif a in ("off", "no", "0"):
        d["antiflood"] = 0
    elif a.isdigit() and 2 <= int(a) <= 30:
        d["antiflood"] = int(a)
    else:
        await say(c, f"Anti-flood: <b>{d['antiflood'] or 'OFF'}</b>\nUse <code>/antiflood on</code>, <code>off</code> or a number (2-30 msgs in 6s).")
        return
    await csave(c.chat.id)
    await say(c, "Anti-flood is now " + (f"<b>ON</b> ({d['antiflood']} msgs / 6s → 10m mute)" if d["antiflood"] else "<b>OFF</b>"))


async def cmd_badword(c):
    d = await cdoc(c.chat.id)
    sub = c.args[0].lower() if c.args else ""
    words = [w.lower() for w in c.args[1:]]
    if sub == "add" and words:
        d["badwords"] = sorted(set(d["badwords"]) | set(words))
        msg = f"Added {len(words)} word(s)."
    elif sub in ("del", "remove") and words:
        d["badwords"] = [w for w in d["badwords"] if w not in words]
        msg = "Removed."
    elif sub == "list":
        await say(c, "🚫 " + (", ".join(f"<code>{esc(w)}</code>" for w in d["badwords"]) or "No bad words set."))
        return
    elif sub == "clear":
        d["badwords"] = []
        msg = "Cleared all bad words."
    else:
        await say(c, "Usage: <code>/badword add|del &lt;word&gt;</code>, <code>/badword list</code>, <code>/badword clear</code>\n3 bad-word hits = auto kick.")
        return
    await csave(c.chat.id)
    await say(c, f"✅ {msg}")


async def cmd_approve(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    d = await cdoc(c.chat.id)
    if uid not in d["approved"]:
        d["approved"].append(uid)
    await csave(c.chat.id)
    await say(c, f"✅ {mention(uid, name)} approved (filters skip them).")


async def cmd_unapprove(c):
    t = await pick_target(c, protect=False)
    if not t:
        return
    uid, name, _ = t
    d = await cdoc(c.chat.id)
    if uid in d["approved"]:
        d["approved"].remove(uid)
    await csave(c.chat.id)
    await say(c, f"❎ {mention(uid, name)} removed from approved.")


async def cmd_approvelist(c):
    d = await cdoc(c.chat.id)
    if not d["approved"]:
        await say(c, "No approved users.")
        return
    await say(c, "✅ <b>Approved</b>\n" + "\n".join(mention(u, NAMES.get(u, str(u))) for u in d["approved"]))


async def cmd_settings(c):
    d = await cdoc(c.chat.id)
    tick = lambda v: "ON ✅" if v else "OFF ❌"  # noqa: E731
    await say(c, f"⚙️ <b>{fx('Group Settings')}</b>\n"
                 f"├ Rosyy chat: {tick(d['ai'])}\n├ Anti-flood: {d['antiflood'] or 'OFF ❌'}\n"
                 f"├ Anti-link: {tick(d['antilink'])}\n├ Bio-link filter: {tick(d['bioremove'])}\n"
                 f"├ Anti-forward: {tick(d['antiforward'])}\n├ Join/leave cleaner: {tick(d['join_remove'])}\n"
                 f"├ Bad words: {len(d['badwords'])}\n└ Approved users: {len(d['approved'])}")


# ───────────────────────── help / start ─────────────────────────
def start_text():
    custom = str(cfg("start_text") or "").strip()
    if custom:
        return esc(custom)
    return (
        f"🍁 <b>{fx('Rosyy ✿')}</b>\n"
        "Heyy! I'm Rosyy 🌸 — chatty by nature, strict with spammers.\n\n"
        f"💬 <b>{fx('Chat')}</b>\n"
        "DM me anything. In a group say <i>Rosyy</i> or reply to me. "
        "I can look at your pics, listen to voice notes, and send stickers &amp; voice notes back.\n\n"
        f"🎮 <b>{fx('Games')}</b>\n"
        "Earn 🪙 coins, rob &amp; kill your friends, invest, protect yourself — /help games\n\n"
        f"👮 <b>{fx('Manage')}</b>\n"
        "Add me to your group as admin and send /help.\n\n"
        "📖 All commands: /help"
    )


def help_text():
    return (
        f"🍁 <b>{fx('Rosyy — Help')}</b>\n"
        "Prefix: <b>/</b>  <b>.</b>  <b>!</b> (all work)  •  <b>&lt;user&gt;</b> = reply / @username / user id\n\n"
        f"👮 <b>{fx('Moderation')}</b>\n"
        "/kick &lt;user&gt; — remove (can rejoin)  •  /skick silent\n"
        "/ban &lt;user&gt; — ban  •  /dban delete+ban  •  /sban silent\n"
        "/unban &lt;user&gt; — unban\n"
        "/mute &lt;user&gt; 30m — mute (time optional)\n"
        "/dmute — delete replied msg + mute  •  /smute silent\n"
        "/unmute &lt;user&gt; — unmute\n"
        "/warn &lt;user&gt; [reason] — 3 warns = ban\n"
        "/warns  •  /unwarn  •  /clearwarns\n"
        "/res &lt;user&gt; -media +msg — fine-tune permissions\n"
        "<i>powers: msg media sticker gif poll link invite pin info</i>\n\n"
        f"👑 <b>{fx('Admins')}</b>\n"
        "/promote &lt;user&gt; 0/1/2/3 — make admin by level\n"
        "/demote &lt;user&gt;  •  /demote_all\n"
        "/add &lt;user&gt; &lt;power&gt;  •  /remove &lt;user&gt; &lt;power&gt;\n"
        "<i>powers: delete ban pin invite info vc promote</i>\n"
        "/title &lt;user&gt; &lt;title&gt; — custom title\n\n"
        f"📌 <b>{fx('Messages')}</b>\n"
        "/pin (silent) / /pin loud  •  /unpin  •  /d — delete replied\n"
        "/purge — reply = delete up to here  •  /purge 20\n"
        "/delmsg 10  •  /cleanbot — delete my messages\n\n"
        f"⚙️ <b>{fx('Settings')}</b>\n"
        "/antiflood on|off|5  •  /antilink on|off\n"
        "/bioremove on|off — auto-delete users with links in bio\n"
        "/antiforward on|off  •  /join_remove on|off\n"
        "/approve &lt;user&gt;  •  /unapprove  •  /approvelist\n"
        "/badword add|del|list|clear — 3 hits = kick\n"
        "/ai on|off — Rosyy chat in this group  •  /games on|off — coin games\n"
        "/welcome on|off  •  /goodbye on|off — greeting &amp; roast farewell\n"
        "/settings  •  /kickme — leave the group\n\n"
        f"💬 <b>{fx('Chat with Rosyy')}</b>\n"
        "DM me, say <i>Rosyy</i> or reply to me in a group. Send pics 📸, voice notes 🎤 or stickers 🎭.\n\n"
        "🎮 Coin games &amp; fun: /help games\n"
        "<i>Only group admins can use moderation commands.</i>"
    )


def games_help():
    return (
        f"🎮 <b>{fx('Rosyy — Games')}</b>  🪙\n"
        "Coins are global — same wallet in every group &amp; DM.\n\n"
        f"💰 <b>{fx('Earn')}</b>\n"
        "/daily — free coins every day (streak = bonus)\n"
        "/bal [user] — wallet  •  /pay &lt;user&gt; &lt;amt&gt;  •  /top — richest\n\n"
        f"🥷 <b>{fx('PvP (groups)')}</b>\n"
        "/rob &lt;user&gt; — steal from their wallet (fail = you pay a fine)\n"
        "/kill &lt;user&gt; — costs bullets, steals big + they're dead for a while\n"
        "/revive — come back to life (costs coins)\n"
        "/protect 1d|2d — shield: nobody can rob or kill you\n\n"
        f"🏦 <b>{fx('Grow')}</b>\n"
        "/fd &lt;amt&gt; &lt;1|3|7&gt; — fixed deposit, safe from robbers, pays interest\n"
        "/fd — your deposits  •  /fd break &lt;n&gt; — early exit (20% penalty)\n"
        "/invest &lt;amt&gt; [low|mid|high] — risky market, 1 try per hour\n\n"
        f"🎰 <b>{fx('Gamble')}</b>\n"
        "/toss &lt;amt&gt; &lt;h|t&gt;  •  /slots &lt;amt&gt;\n"
        "<i>amt can be 500, 2k, 1.5k, half, all</i>\n\n"
        f"💥 <b>{fx('Fun actions')}</b> (cost 🪙, reply to someone)\n"
        "/slap  /punch  /bonk  /bite  /yeet  /roast\n"
        "/hug  /pat  /poke  /cuddle  /highfive\n\n"
        "<i>Admins can switch games off in a group: /games off</i>"
    )


def owner_help():
    return (
        f"👑 <b>{fx('Owner panel')}</b>\n"
        "<i>Only you can use these. Keys &amp; Mongo URLs: send them in DM, I delete the message.</i>\n\n"
        f"🤖 <b>{fx('Gemini AI')}</b>\n"
        "/addkey &lt;key&gt;  •  /delkey &lt;n&gt;  •  /keys\n"
        "/models — chain (slow/failing model → next one)\n"
        "/addmodel &lt;name&gt;  •  /delmodel &lt;n&gt;  •  /topmodel &lt;n&gt;\n"
        "/models live — all models your key can use  •  /models sync — add them all\n"
        "/testai — try the chain now\n\n"
        f"🚪 <b>{fx('Join gate (DM)')}</b>\n"
        "/setjoin — send inside the group (I must be admin there)\n"
        "/setjoin @channel|-100id [invite link]  •  /unsetjoin &lt;n&gt;  •  /joinlist\n\n"
        f"🗄 <b>{fx('MongoDB')}</b>\n"
        "/addmongo &lt;uri&gt;  •  /delmongo &lt;n&gt;  •  /mongos  •  /usemongo &lt;n&gt;\n\n"
        f"🎭 <b>{fx('Stickers')}</b>\n"
        "/addpack &lt;@name|link&gt;  •  /delpack &lt;n&gt;  •  /packs\n\n"
        f"⚙️ <b>{fx('Settings')}</b>\n"
        "/config — everything editable  •  /set &lt;key&gt; &lt;value&gt; (<code>default</code> resets)\n"
        "/addcoins &lt;user&gt; &lt;amt&gt;  •  /takecoins &lt;user&gt; &lt;amt&gt;  •  /panel"
    )


def help_kb(owner=False):
    row = [InlineKeyboardButton("👮 Manage", callback_data="help:main"), InlineKeyboardButton("🎮 Games", callback_data="help:games")]
    if owner:
        row.append(InlineKeyboardButton("👑 Owner", callback_data="help:owner"))
    return InlineKeyboardMarkup([row])


def help_page(name, uid):
    if name == "owner" and uid == OWNER_ID:
        return owner_help()
    return games_help() if name in ("games", "game") else help_text()


async def cmd_help(c):
    if c.cmd == "start":
        missing = await gate_missing(c.bot, c.user.id) if c.chat.type == ChatType.PRIVATE else []
        if missing:
            await gate_prompt(c.msg, missing)
            return
        await say(c, start_text(), help_kb(c.user.id == OWNER_ID))
        return
    page = c.args[0].lower() if c.args else "main"
    await say(c, help_page(page, c.user.id), help_kb(c.user.id == OWNER_ID))


# ───────────────────────── join gate (DM only) ─────────────────────────
GATE_OK: dict = {}
GATE_PROMPT: dict = {}


async def gate_missing(bot, uid):
    if uid == OWNER_ID or not CFG["join_chats"]:
        return []
    if GATE_OK.get(uid, 0) > time.time():
        return []
    missing = []
    for j in CFG["join_chats"]:
        try:
            m = await bot.get_chat_member(j["id"], uid)
        except TelegramError as e:  # can't verify (bot not admin / kicked) -> don't lock users out
            log.debug("gate check failed for %s: %r", j.get("id"), e)
            continue
        if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) or (
                m.status == ChatMemberStatus.RESTRICTED and not getattr(m, "is_member", True)):
            missing.append(j)
    if not missing:
        GATE_OK[uid] = time.time() + 600
    return missing


async def gate_prompt(msg, missing):
    kb = [[InlineKeyboardButton(f"➕ Join {j.get('title') or 'group'}", url=j["link"])] for j in missing if j.get("link")]
    kb.append([InlineKeyboardButton("✅ I've joined", callback_data="joined")])
    names = ", ".join(f"<b>{esc(j.get('title') or 'group')}</b>" for j in missing)
    await msg.reply_text(
        f"🌸 <b>{fx('Almost there!')}</b>\nTo chat with me, first join {names} — then tap <b>I've joined</b> 👇",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


# ───────────────────────── economy core ─────────────────────────
ECO: dict = {}
ECO_LOCK = asyncio.Lock()


def eco_defaults():
    return {"coins": int(cfg("start_coins")), "streak": 0, "last_daily": 0, "protect_until": 0, "dead_until": 0,
            "rob_at": 0, "kill_at": 0, "invest_at": 0, "play_at": 0, "fd": [], "notice": 0, "name": "",
            "wins": 0, "robbed": 0, "kills": 0}


def coin(n) -> str:
    return f"{int(n):,} 🪙"


def fmt_left(sec) -> str:
    sec = int(max(sec, 1))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s" if (s and not d and not h) else ""]
    return " ".join(p for p in parts if p)[:14].strip() or "1s"


def parse_amt(s, have):
    s = (s or "").lower().replace(",", "").strip()
    if s in ("all", "max"):
        return int(have)
    if s == "half":
        return int(have // 2)
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v * mult) if v > 0 else None


def settle(d) -> bool:
    now, keep, got = time.time(), [], 0
    for f in d.get("fd", []):
        if f["due"] <= now:
            got += f["amt"] + f["gain"]
        else:
            keep.append(f)
    if got:
        d["fd"] = keep
        d["coins"] += got
        d["notice"] = d.get("notice", 0) + got
    return bool(got)


async def eco(uid, name=None):
    d = ECO.get(uid)
    if d is None:
        d = await ST.eco_get(uid)
        base = eco_defaults()
        if d is None:
            d = base
        else:
            for k, v in base.items():
                d.setdefault(k, v)
        if len(ECO) > 20000:
            ECO.clear()
        ECO[uid] = d
    if name:
        d["name"] = name
    if settle(d):
        esave(uid)
    return d


def esave(uid):
    async def _go():
        try:
            await ST.eco_put(uid, ECO[uid])
        except Exception as e:  # noqa: BLE001
            log.warning("eco save failed: %r", e)

    spawn(_go())


def is_dead(d) -> bool:
    return d.get("dead_until", 0) > time.time()


async def games_on(c) -> bool:
    if c.chat.type in GROUPS and not (await cdoc(c.chat.id)).get("games", True):
        await say(c, "🎮 Games are switched off in this group. (admins: <code>/games on</code>)")
        return False
    return True


async def eco_target(c):
    t = await get_target(c)
    if not t:
        await say(c, "Reply to someone, or give <code>@username</code> / user id 🌸")
        return None
    uid, name, rest = t
    rep = c.msg.reply_to_message
    if uid == c.user.id:
        await say(c, "Khud pe? 😭 Kisi aur ko target karo.")
        return None
    if uid == c.bot.id or (rep and rep.from_user and rep.from_user.id == uid and rep.from_user.is_bot):
        await say(c, "Bots ko mat chhedo 😌")
        return None
    return uid, name or NAMES.get(uid) or str(uid), rest


def me(c):
    return mention(c.user.id, c.user.full_name)


async def say_card(c, uid, text):
    """Reply with the player's profile picture + text (game-style card); plain text if no photo is visible."""
    try:
        ph = await c.bot.get_user_profile_photos(uid, limit=1)
        if ph.total_count:
            m = await c.msg.reply_photo(ph.photos[0][-1].file_id, caption=text[:1020], parse_mode=ParseMode.HTML)
            track(c.chat.id, m.message_id)
            return
    except TelegramError as e:
        log.debug("pfp card failed: %r", e)
    await say(c, text)


async def cmd_daily(c):
    async with ECO_LOCK:
        d = await eco(c.user.id, c.user.full_name)
        now = time.time()
        wait = d["last_daily"] + 20 * 3600 - now
        if wait > 0:
            await say(c, f"⏳ Daily already claimed. Next one in <b>{fmt_left(wait)}</b>.")
            return
        d["streak"] = d["streak"] + 1 if now - d["last_daily"] < 48 * 3600 else 1
        lo = int(cfg("daily_min"))
        base = random.randint(lo, max(lo, int(cfg("daily_max"))))
        bonus = min(d["streak"] - 1, 30) * int(cfg("streak_bonus"))
        d["coins"] += base + bonus
        d["last_daily"] = now
        esave(c.user.id)
        bal, streak = d["coins"], d["streak"]
    await say(c, f"🎁 <b>{fx('Daily claimed')}</b>\n{me(c)} got <b>{coin(base + bonus)}</b>"
                 + (f" (streak bonus +{bonus:,})" if bonus else "")
                 + f"\n🔥 Streak: <b>{streak}</b> day(s)\n💰 Wallet: <b>{coin(bal)}</b>")


async def cmd_bal(c):
    uid, name = c.user.id, c.user.full_name
    if c.msg.reply_to_message or c.args:
        t = await get_target(c)
        if t:
            uid, name = t[0], t[1] or NAMES.get(t[0]) or str(t[0])
    async with ECO_LOCK:
        d = await eco(uid, name if uid == c.user.id else None)
        now = time.time()
        fd_total = sum(f["amt"] for f in d["fd"])
        note = d.pop("notice", 0)
        d["notice"] = 0
        lines = [f"🍁 <b>{fx('Wallet')}</b> — {mention(uid, name)}",
                 f"├ 🪙 Coins: <b>{d['coins']:,}</b>",
                 f"├ 🏦 In FD: <b>{fd_total:,}</b> ({len(d['fd'])} active)",
                 f"├ 🛡 Protection: <b>{fmt_left(d['protect_until'] - now) if d['protect_until'] > now else '—'}</b>",
                 f"├ 🔥 Streak: <b>{d['streak']}</b>",
                 f"└ 🥷 Robbed: <b>{d['wins']}</b>  💀 Kills: <b>{d['kills']}</b>"]
        if is_dead(d):
            lines.append(f"💀 <b>Dead</b> for {fmt_left(d['dead_until'] - now)} — /revive")
        if note:
            lines.append(f"🏦 Matured FD credited: <b>+{note:,}</b>")
    await say_card(c, uid, "\n".join(lines))


async def cmd_pay(c):
    t = await eco_target(c)
    if not t:
        return
    tid, tname, rest = t
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        amt = parse_amt(rest[0] if rest else "", a["coins"])
        if not amt:
            await say(c, "Usage: <code>/pay @user 500</code> (or reply with <code>/pay 500</code>)")
            return
        if amt > a["coins"]:
            await say(c, f"Itne coins nahi hain 😅 Wallet: <b>{coin(a['coins'])}</b>")
            return
        b = await eco(tid, tname)
        a["coins"] -= amt
        b["coins"] += amt
        esave(c.user.id)
        esave(tid)
    await say(c, f"💸 {me(c)} sent <b>{coin(amt)}</b> to {mention(tid, tname)}")


ROB_OK = ["{a} snuck into {b}'s pocket like a ninja 🥷 and walked away with <b>{x}</b>!",
          "{a} sold {b} a 'free' chai and grabbed <b>{x}</b> 😎",
          "Easy money! {a} lifted <b>{x}</b> from {b} 💅"]
ROB_FAIL = ["{b} caught {a} red-handed 🚨 — fine paid: <b>{x}</b>",
            "{a} tripped over their own shoelaces 🤡 and paid <b>{x}</b> to {b}",
            "Guards arrived! {a} paid <b>{x}</b> to {b} to stay out of jail 😭"]
KILL_OK = ["{a} took the shot 🔫 and {b} is down! Loot: <b>{x}</b>",
           "Boom! {b} got eliminated by {a} 💥 — <b>{x}</b> looted",
           "{a} sniped {b} from the rooftop 🎯 and grabbed <b>{x}</b>"]
KILL_FAIL = ["{b} dodged it like Neo 🕶 — {a}'s bullets are wasted!",
             "{a} missed every shot 😭 {b} is laughing",
             "Click… click… {a}'s gun jammed 🙈 {b} walks away"]


async def cmd_rob(c):
    if not await games_on(c):
        return
    t = await eco_target(c)
    if not t:
        return
    vid, vname, _ = t
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        v = await eco(vid, vname)
        now = time.time()
        if is_dead(a):
            await say(c, f"💀 You're dead for <b>{fmt_left(a['dead_until'] - now)}</b>. Use /revive ({coin(cfg('revive_cost'))}).")
            return
        w = a["rob_at"] + cfg("rob_cd") - now
        if w > 0:
            await say(c, f"⏳ Lay low for <b>{fmt_left(w)}</b> before the next robbery.")
            return
        if v["protect_until"] > now:
            await say(c, f"🛡 {mention(vid, vname)} is protected for <b>{fmt_left(v['protect_until'] - now)}</b>.")
            return
        if is_dead(v):
            await say(c, f"💀 {mention(vid, vname)} is already dead. Leave the body alone 😭")
            return
        if v["coins"] < 100:
            await say(c, f"{mention(vid, vname)} is broke (under 100 🪙) — not worth the risk 🥲")
            return
        if a["coins"] < 50:
            await say(c, "You need at least <b>50 🪙</b> in your wallet to cover a possible fine.")
            return
        a["rob_at"] = now
        if random.random() < float(cfg("rob_success")):
            pct = random.uniform(0.10, max(0.10, float(cfg("rob_max_pct"))))
            loot = max(1, min(v["coins"], int(v["coins"] * pct)))
            v["coins"] -= loot
            a["coins"] += loot
            a["wins"] += 1
            v["robbed"] += 1
            txt = random.choice(ROB_OK).format(a=me(c), b=mention(vid, vname), x=coin(loot))
        else:
            fine = min(a["coins"], max(50, int(a["coins"] * random.uniform(0.10, 0.20))))
            a["coins"] -= fine
            v["coins"] += fine
            txt = random.choice(ROB_FAIL).format(a=me(c), b=mention(vid, vname), x=coin(fine))
        esave(c.user.id)
        esave(vid)
    await say_card(c, c.user.id, "🥷 " + txt)


async def cmd_kill(c):
    if not await games_on(c):
        return
    t = await eco_target(c)
    if not t:
        return
    vid, vname, _ = t
    cost = int(cfg("kill_cost"))
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        v = await eco(vid, vname)
        now = time.time()
        if is_dead(a):
            await say(c, f"💀 Dead people don't shoot! /revive first ({coin(cfg('revive_cost'))}).")
            return
        w = a["kill_at"] + cfg("kill_cd") - now
        if w > 0:
            await say(c, f"⏳ Reload time: <b>{fmt_left(w)}</b>.")
            return
        if v["protect_until"] > now:
            await say(c, f"🛡 {mention(vid, vname)} is protected for <b>{fmt_left(v['protect_until'] - now)}</b>.")
            return
        if is_dead(v):
            await say(c, f"💀 {mention(vid, vname)} is already dead.")
            return
        if a["coins"] < cost:
            await say(c, f"Bullets cost <b>{coin(cost)}</b> — you only have <b>{coin(a['coins'])}</b>.")
            return
        a["coins"] -= cost
        a["kill_at"] = now
        if random.random() < float(cfg("kill_success")):
            loot = int(v["coins"] * random.uniform(0.25, 0.50))
            v["coins"] -= loot
            a["coins"] += loot
            a["kills"] += 1
            v["dead_until"] = now + float(cfg("dead_hours")) * 3600
            txt = random.choice(KILL_OK).format(a=me(c), b=mention(vid, vname), x=coin(loot))
            txt += f"\n💀 {mention(vid, vname)} is dead for <b>{fmt_left(float(cfg('dead_hours')) * 3600)}</b> (/revive)"
        else:
            txt = random.choice(KILL_FAIL).format(a=me(c), b=mention(vid, vname)) + f"\n−{coin(cost)} for bullets"
        esave(c.user.id)
        esave(vid)
    await say_card(c, c.user.id, "🔫 " + txt)


async def cmd_revive(c):
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        if not is_dead(a):
            await say(c, "You're alive and kicking 😎")
            return
        cost = int(cfg("revive_cost"))
        if a["coins"] < cost:
            await say(c, f"Revive costs <b>{coin(cost)}</b>, you have <b>{coin(a['coins'])}</b>. "
                         f"Or wait <b>{fmt_left(a['dead_until'] - time.time())}</b>.")
            return
        a["coins"] -= cost
        a["dead_until"] = 0
        esave(c.user.id)
    await say(c, f"✨ {me(c)} is back from the dead! (−{coin(cost)})")


async def cmd_protect(c):
    arg = (c.args[0].lower() if c.args else "")
    days = {"1d": 1, "1": 1, "2d": 2, "2": 2}.get(arg)
    price = {1: int(cfg("protect_1d")), 2: int(cfg("protect_2d"))}
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        now = time.time()
        cur = max(0, a["protect_until"] - now)
        if not days:
            await say(c, f"🛡 <b>{fx('Protection')}</b>\nNobody can /rob or /kill you while it's active.\n"
                         f"• <code>/protect 1d</code> — {coin(price[1])}\n• <code>/protect 2d</code> — {coin(price[2])}\n"
                         f"Active: <b>{fmt_left(cur) if cur else '—'}</b> (max 4 days at a time)")
            return
        if cur + days * 86400 > 4 * 86400 + 60:
            await say(c, f"🛡 Max 4 days of protection at once. You have <b>{fmt_left(cur)}</b> left.")
            return
        if a["coins"] < price[days]:
            await say(c, f"Need <b>{coin(price[days])}</b>, you have <b>{coin(a['coins'])}</b>.")
            return
        a["coins"] -= price[days]
        a["protect_until"] = max(now, a["protect_until"]) + days * 86400
        esave(c.user.id)
        left = a["protect_until"] - now
    await say(c, f"🛡 {me(c)} is protected for <b>{fmt_left(left)}</b> (−{coin(price[days])})")


FD_KEYS = {1: "fd_rate_1d", 3: "fd_rate_3d", 7: "fd_rate_7d"}


async def cmd_fd(c):
    a_ = c.args
    async with ECO_LOCK:
        d = await eco(c.user.id, c.user.full_name)
        now = time.time()
        if a_ and a_[0].lower() == "break":
            if len(a_) < 2 or not a_[1].isdigit() or not (1 <= int(a_[1]) <= len(d["fd"])):
                await say(c, "Usage: <code>/fd break 1</code> (number from /fd list)")
                return
            f = d["fd"].pop(int(a_[1]) - 1)
            back = int(f["amt"] * 0.8)
            d["coins"] += back
            esave(c.user.id)
            await say(c, f"🏦 FD broken early — got back <b>{coin(back)}</b> (20% penalty).")
            return
        if len(a_) >= 2:
            if not a_[1].isdigit() or int(a_[1]) not in FD_KEYS:
                await say(c, "Days must be <code>1</code>, <code>3</code> or <code>7</code>.")
                return
            days = int(a_[1])
            amt = parse_amt(a_[0], d["coins"])
            if not amt or amt < 100:
                await say(c, "Minimum FD is <b>100 🪙</b>.")
                return
            if amt > d["coins"]:
                await say(c, f"Wallet only has <b>{coin(d['coins'])}</b>.")
                return
            if len(d["fd"]) >= 5:
                await say(c, "Max 5 active FDs. Wait for one to mature.")
                return
            rate = float(cfg(FD_KEYS[days]))
            gain = int(amt * rate)
            d["coins"] -= amt
            d["fd"].append({"amt": amt, "gain": gain, "due": now + days * 86400, "at": now})
            esave(c.user.id)
            await say(c, f"🏦 <b>{fx('FD created')}</b>\nLocked <b>{coin(amt)}</b> for {days} day(s) @ {rate * 100:g}%\n"
                         f"You'll get <b>{coin(amt + gain)}</b> back — and robbers can't touch it 🔒")
            return
        note = d.get("notice", 0)
        d["notice"] = 0
        if not d["fd"]:
            await say(c, f"🏦 No active FDs.\nUsage: <code>/fd 1000 7</code> — rates: 1d {cfg('fd_rate_1d') * 100:g}% • "
                         f"3d {cfg('fd_rate_3d') * 100:g}% • 7d {cfg('fd_rate_7d') * 100:g}%"
                         + (f"\n✅ Matured FD credited: <b>+{note:,}</b>" if note else ""))
            return
        rows = [f"{i}. <b>{f['amt']:,}</b> → <b>{f['amt'] + f['gain']:,}</b> in {fmt_left(f['due'] - now)}"
                for i, f in enumerate(d["fd"], 1)]
    await say(c, f"🏦 <b>{fx('Your FDs')}</b>\n" + "\n".join(rows) + "\n<i>/fd break &lt;n&gt; = early exit (20% penalty)</i>"
              + (f"\n✅ Matured FD credited: <b>+{note:,}</b>" if note else ""))


# tier: (win chance, gain on win, loss on lose)
TIERS = {"low": (0.80, 0.08, 0.20), "mid": (0.50, 0.40, 0.35), "high": (0.25, 1.60, 0.55)}
COMPANIES = ["Rosyy Chai Ltd", "Puri Beach Resorts", "Momo Express", "Odisha Rasgulla Co.", "KPop Merch Inc.", "Lofi Beats AI",
             "Chilika Fisheries", "Bhubaneswar Metro", "Maggi Futures", "Reel-Trend Media"]


async def cmd_invest(c):
    tier = (c.args[1].lower() if len(c.args) > 1 else "mid")
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        if not c.args or tier not in TIERS:
            await say(c, "📈 <b>Invest</b>: <code>/invest 1000 low|mid|high</code>\n"
                         "low — safe, small gains • mid — balanced • high — jackpot or crash\n"
                         f"One try every {fmt_left(cfg('invest_cd'))}.")
            return
        if is_dead(a):
            await say(c, "💀 Dead people can't trade. /revive first.")
            return
        w = a["invest_at"] + cfg("invest_cd") - time.time()
        if w > 0:
            await say(c, f"📉 Market is closed for you. Come back in <b>{fmt_left(w)}</b>.")
            return
        amt = parse_amt(c.args[0], a["coins"])
        if not amt or amt < 50:
            await say(c, "Minimum investment is <b>50 🪙</b>.")
            return
        if amt > a["coins"]:
            await say(c, f"Wallet only has <b>{coin(a['coins'])}</b>.")
            return
        p, up, down = TIERS[tier]
        a["invest_at"] = time.time()
        co = random.choice(COMPANIES)
        if random.random() < p:
            gain = max(1, int(amt * up))
            a["coins"] += gain
            txt = f"📈 <b>{co}</b> pumped! {me(c)} made <b>+{coin(gain)}</b> 🚀"
        else:
            loss = max(1, int(amt * down))
            a["coins"] -= loss
            txt = f"📉 <b>{co}</b> crashed… {me(c)} lost <b>{coin(loss)}</b> 😭"
        esave(c.user.id)
        bal = a["coins"]
    await say(c, txt + f"\n💰 Wallet: <b>{coin(bal)}</b>")


async def _play_gate(c, a, amt_s):
    """Shared checks for toss/slots. Returns bet or None."""
    if not await games_on(c):
        return None
    if is_dead(a):
        await say(c, "💀 Dead people can't gamble. /revive first.")
        return None
    if time.time() - a["play_at"] < 3:
        return None
    amt = parse_amt(amt_s, a["coins"])
    if not amt or amt < 10:
        await say(c, "Minimum bet is <b>10 🪙</b>.")
        return None
    if amt > a["coins"]:
        await say(c, f"Wallet only has <b>{coin(a['coins'])}</b>.")
        return None
    a["play_at"] = time.time()
    return amt


async def cmd_toss(c):
    side = (c.args[1].lower() if len(c.args) > 1 else "")
    if side not in ("h", "t", "heads", "tails"):
        await say(c, "Usage: <code>/toss 500 h</code> (h = heads, t = tails)")
        return
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        bet = await _play_gate(c, a, c.args[0] if c.args else "")
        if not bet:
            return
        res = random.choice("ht")
        if res == side[0]:
            win = int(bet * 0.95)
            a["coins"] += win
            txt = f"🪙 It's <b>{'Heads' if res == 'h' else 'Tails'}</b>! {me(c)} won <b>{coin(win)}</b> 🎉"
        else:
            a["coins"] -= bet
            txt = f"🪙 It's <b>{'Heads' if res == 'h' else 'Tails'}</b>… {me(c)} lost <b>{coin(bet)}</b> 😭"
        esave(c.user.id)
        bal = a["coins"]
    await say(c, txt + f"\n💰 Wallet: <b>{coin(bal)}</b>")


SLOT_SYMS = ["🍒", "🍋", "🍉", "🔔", "💎", "7️⃣"]
SLOT_W = [30, 25, 20, 14, 8, 3]
SLOT_PAY = {"🍒": 5, "🍋": 7, "🍉": 12, "🔔": 22, "💎": 55, "7️⃣": 150}


async def cmd_slots(c):
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        if not c.args:
            await say(c, "Usage: <code>/slots 500</code>")
            return
        bet = await _play_gate(c, a, c.args[0])
        if not bet:
            return
        r = random.choices(SLOT_SYMS, SLOT_W, k=3)
        if r[0] == r[1] == r[2]:
            mult = SLOT_PAY[r[0]]
            a["coins"] += bet * mult - bet
            verdict = f"🎉 <b>JACKPOT x{mult}!</b> {me(c)} won <b>{coin(bet * mult - bet)}</b>"
        elif len(set(r)) == 2:
            verdict = "😮 Pair — bet returned."
        else:
            a["coins"] -= bet
            verdict = f"😭 {me(c)} lost <b>{coin(bet)}</b>"
        esave(c.user.id)
        bal = a["coins"]
    await say(c, f"🎰 | {' | '.join(r)} |\n{verdict}\n💰 Wallet: <b>{coin(bal)}</b>")


async def cmd_top(c):
    rows = await ST.eco_top(10)
    if not rows:
        await say(c, "Nobody has played yet — be the first: /daily 🌸")
        return
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = []
    for i, (uid, d) in enumerate(rows):
        live = ECO.get(uid, d)
        name = live.get("name") or NAMES.get(uid) or str(uid)
        lines.append(f"{medals[i]} {mention(uid, name)} — <b>{live['coins']:,}</b> 🪙")
    await say(c, f"🏆 <b>{fx('Richest players')}</b>\n" + "\n".join(lines))


async def cmd_games(c):
    d = await cdoc(c.chat.id)
    v = onoff(c.args[0] if c.args else "")
    if v is None:
        await say(c, f"Coin games: <b>{'ON ✅' if d.get('games', True) else 'OFF ❌'}</b>\nUse <code>/games on</code> or <code>/games off</code>")
        return
    d["games"] = v
    await csave(c.chat.id)
    await say(c, f"Coin games are now <b>{'ON ✅' if v else 'OFF ❌'}</b>")


# name: (cost multiplier, emoji, texts)
ACTIONS = {
    "slap": (1.0, "👋", ["{a} slapped {b} so hard their WiFi disconnected 📶", "{a} gave {b} a classic Bollywood slap 🎬",
                         "*thwack!* {b} saw stars thanks to {a} ✨"]),
    "punch": (1.5, "👊", ["{a} punched {b} into next week 🚀", "{a} hit {b} with a cartoon punch 💥 *boing*"]),
    "bonk": (0.8, "🔨", ["{a} bonked {b} — go to horny jail… I mean, go to timeout 🔨", "*bonk!* {a} knocked some sense into {b} 🧠"]),
    "bite": (1.2, "🦷", ["{a} bit {b}! nom nom 🧛", "{a} chomped on {b}'s arm 😬 {b} is now a snack"]),
    "yeet": (2.0, "🚀", ["{a} YEETED {b} across the galaxy 🌌", "{a} picked up {b} and yeeted them into Chilika lake 🌊"]),
    "roast": (1.0, "🔥", ["{a} roasted {b} so bad even the chai went cold ☕🔥", "{b} got cooked by {a} 🍳 — call the fire brigade!",
                          "{a} to {b}: 'your WiFi is stronger than your personality' 💀"]),
    "hug": (0.8, "🤗", ["{a} gave {b} a big warm hug 🤗", "{a} hugged {b} tightly 🫂 awww"]),
    "pat": (0.5, "🫳", ["{a} patted {b} on the head 🥰", "*pat pat* {a} is proud of {b} 🌸"]),
    "poke": (0.5, "👉", ["{a} poked {b} 👉 hello?? 👀", "{a} keeps poking {b} until they reply 😤"]),
    "cuddle": (1.0, "🧸", ["{a} cuddled {b} like a teddy bear 🧸", "{a} and {b} are cuddling under a blanket ☁️"]),
    "highfive": (0.5, "🙌", ["{a} and {b} high-fived 🙌 *clap!*", "{a} gave {b} the loudest high-five 👏"]),
}


async def cmd_action(c):
    if not await games_on(c):
        return
    mult, emo, texts = ACTIONS[c.cmd]
    t = await eco_target(c)
    if not t:
        return
    tid, tname, _ = t
    cost = max(1, int(int(cfg("action_cost")) * mult))
    async with ECO_LOCK:
        a = await eco(c.user.id, c.user.full_name)
        if is_dead(a):
            await say(c, "💀 You're dead. /revive first.")
            return
        if a["coins"] < cost:
            await say(c, f"<code>/{c.cmd}</code> costs <b>{coin(cost)}</b> — you have <b>{coin(a['coins'])}</b>. Try /daily!")
            return
        a["coins"] -= cost
        esave(c.user.id)
    await say(c, f"{emo} " + random.choice(texts).format(a=me(c), b=mention(tid, tname)) + f"\n<i>−{coin(cost)}</i>")


# ───────────────────────── owner commands ─────────────────────────
def mask_uri(uri) -> str:
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", uri) if uri else "(memory)"


async def secret(c) -> bool:
    """Commands that carry secrets must be used in DM; in a group we delete the message and refuse."""
    if c.chat.type != ChatType.PRIVATE:
        await delete(c.bot, c.chat.id, c.msg.message_id)
        await c.bot.send_message(c.chat.id, "🔒 Send this one to me in DM — I deleted it here.")
        return False
    return True


async def cfg_save():
    try:
        await ST.cfg_save(CFG)
    except Exception as e:  # noqa: BLE001
        log.warning("cfg save failed: %r", e)


def all_packs():
    return ENV_STICKER_PACKS + [p for p in CFG["packs"] if p not in ENV_STICKER_PACKS]


async def cmd_panel(c):
    await say(c, f"👑 <b>{fx('Rosyy Panel')}</b>\n"
                 f"├ 🗄 Storage: <b>{esc(ST.kind)}</b> ({len(ST.b.stores)} db)\n"
                 f"├ 🤖 Gemini: <b>{len(api_keys())}</b> key(s) • <b>{len(CFG['models'])}</b> model(s)\n"
                 f"├ 🚪 Join gate: <b>{len(CFG['join_chats'])}</b> chat(s)\n"
                 f"├ 🎭 Sticker packs: <b>{len(all_packs())}</b> • pool <b>{len(STICKERS)}</b>\n"
                 f"└ 🎮 Players cached: <b>{len(ECO)}</b>\n\n" + owner_help())


# ---- Gemini keys / models
async def cmd_addkey(c):
    if not await secret(c):
        return
    k = c.args[0].strip() if c.args else ""
    await delete(c.bot, c.chat.id, c.msg.message_id)
    if len(k) < 20 or " " in k:
        await c.bot.send_message(c.chat.id, "Usage: <code>/addkey AIza…</code> (get one free at aistudio.google.com/apikey)", parse_mode=ParseMode.HTML)
        return
    if k in api_keys():
        await c.bot.send_message(c.chat.id, "That key is already added.")
        return
    CFG["gemini_keys"].append(k)
    KEY_COOL.pop(k, None)
    await cfg_save()
    await c.bot.send_message(c.chat.id, f"✅ Key added: <code>{mask(k)}</code>\nTotal keys: <b>{len(api_keys())}</b>. Try /testai", parse_mode=ParseMode.HTML)


async def cmd_delkey(c):
    keys = api_keys()
    if not c.args or not c.args[0].isdigit() or not (1 <= int(c.args[0]) <= len(keys)):
        await say(c, "Usage: <code>/delkey 2</code> (number from /keys)")
        return
    k = keys[int(c.args[0]) - 1]
    if k in ENV_KEYS:
        await say(c, "That key comes from a Render env var — remove it there.")
        return
    CFG["gemini_keys"].remove(k)
    await cfg_save()
    await say(c, f"🗑 Removed <code>{mask(k)}</code>. Keys left: <b>{len(api_keys())}</b>")


async def cmd_keys(c):
    ks = api_keys()
    if not ks:
        await say(c, "No Gemini key yet. Add one: <code>/addkey AIza…</code> (in DM)")
        return
    now = time.time()
    rows = []
    for i, k in enumerate(ks, 1):
        st = "✅ ok"
        if KEY_COOL.get(k, 0) > now:
            st = f"⛔ rejected ({fmt_left(KEY_COOL[k] - now)})"
        elif any(t > now for (kk, _m), t in PAIR_COOL.items() if kk == k):
            st = "⏳ rate-limited on some models"
        rows.append(f"{i}. <code>{mask(k)}</code> — {st}{' • env' if k in ENV_KEYS else ''}")
    await say(c, f"🔑 <b>{fx('Gemini keys')}</b>\n" + "\n".join(rows) + "\n<i>Keys rotate automatically on rate-limit.</i>")


async def cmd_models(c):
    sub = c.args[0].lower() if c.args else ""
    if sub in ("live", "sync"):
        ks = api_keys()
        if not ks:
            await say(c, "Add a key first: /addkey")
            return
        try:
            live = await list_live_models(ks[0])
        except Exception as e:  # noqa: BLE001
            await say(c, f"❌ Couldn't fetch models: {esc(e)}")
            return
        if sub == "live":
            await say(c, f"🌐 <b>{fx('Models your key can use')}</b> ({len(live)})\n" + "\n".join(f"• <code>{m}</code>" for m in live[:60])
                      + "\n<i>/models sync adds every flash model to the chain • /addmodel &lt;name&gt; adds one</i>")
            return
        add = [m for m in live if "flash" in m and not re.search(r"(tts|image|live|audio|embed|native|robotics|computer)", m) and m not in CFG["models"]]
        CFG["models"] += add
        await cfg_save()
        await say(c, f"✅ Added <b>{len(add)}</b> model(s). Chain now has <b>{len(CFG['models'])}</b>. See /models")
        return
    now = time.time()
    rows = []
    for i, m in enumerate(CFG["models"], 1):
        cool = MODEL_COOL.get(m, 0) - now
        rows.append(f"{i}. <code>{m}</code>" + (f" — ⏸ resting {fmt_left(cool)}" if cool > 0 else ""))
    await say(c, f"🧠 <b>{fx('Model chain')}</b> (tried top to bottom; a slow &gt;{cfg('ai_timeout'):g}s or failing model → next)\n"
                 + ("\n".join(rows) or "empty — /models sync") + "\n<i>/addmodel • /delmodel n • /topmodel n • /models live|sync • /testai</i>")


async def cmd_addmodel(c):
    m = (c.args[0] if c.args else "").replace("models/", "").strip()
    if not m:
        await say(c, "Usage: <code>/addmodel gemini-2.5-flash</code>")
        return
    if m in CFG["models"]:
        await say(c, "Already in the chain.")
        return
    CFG["models"].append(m)
    MODEL_COOL.pop(m, None)
    await cfg_save()
    await say(c, f"✅ Added <code>{esc(m)}</code> at position <b>{len(CFG['models'])}</b>. /testai to check.")


async def cmd_delmodel(c):
    if not c.args or not c.args[0].isdigit() or not (1 <= int(c.args[0]) <= len(CFG["models"])):
        await say(c, "Usage: <code>/delmodel 2</code> (number from /models)")
        return
    m = CFG["models"].pop(int(c.args[0]) - 1)
    await cfg_save()
    await say(c, f"🗑 Removed <code>{esc(m)}</code>.")


async def cmd_topmodel(c):
    if not c.args or not c.args[0].isdigit() or not (1 <= int(c.args[0]) <= len(CFG["models"])):
        await say(c, "Usage: <code>/topmodel 3</code> — moves model #3 to the front")
        return
    m = CFG["models"].pop(int(c.args[0]) - 1)
    CFG["models"].insert(0, m)
    await cfg_save()
    await say(c, f"⭐ <code>{esc(m)}</code> is now the first choice.")


async def cmd_testai(c):
    t0 = time.time()
    try:
        text, model = await gemini("", [{"role": "user", "parts": [{"text": "Say hi in one short Hinglish line."}]}], max_tokens=40)
        head = f"✅ <b>{fx('AI works')}</b> — <code>{esc(model)}</code> • {int((time.time() - t0) * 1000)}ms\n“{esc(text[:120])}”"
    except Exception as e:  # noqa: BLE001
        head = f"❌ <b>{fx('AI failed')}</b>: {esc(e)}"
    await say(c, head + "\n\n<b>Last attempts</b>\n" + "\n".join(f"<code>{esc(x)}</code>" for x in list(AI_LOG)[-8:]))


# ---- join gate
async def cmd_setjoin(c):
    ref = c.args[0] if c.args else ""
    link = next((a for a in c.args[1:] if a.startswith("http")), "") or (ref if ref.startswith("http") else "")
    if ref.startswith("http"):
        ref = ""
    if not ref:
        if c.chat.type not in GROUPS:
            await say(c, "Send <code>/setjoin</code> inside the group/channel you want users to join,\n"
                         "or <code>/setjoin @username</code> / <code>/setjoin -100123… https://t.me/+invite</code> here.")
            return
        ref = c.chat.id
    elif re.fullmatch(r"-?\d+", str(ref)):
        ref = int(ref)
    elif not str(ref).startswith("@"):
        ref = "@" + str(ref)
    try:
        ch = await c.bot.get_chat(ref)
        bm = await c.bot.get_chat_member(ch.id, c.bot.id)
    except TelegramError as e:
        await say(c, f"❌ I can't see that chat: {esc(getattr(e, 'message', e))}\nAdd me there as <b>admin</b> first.")
        return
    if bm.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await say(c, "⚠️ I must be an <b>admin</b> in that chat, otherwise I can't check who joined. Make me admin and try again.")
        return
    if not link:
        if getattr(ch, "username", None):
            link = f"https://t.me/{ch.username}"
        else:
            try:
                link = ch.invite_link or await c.bot.export_chat_invite_link(ch.id)
            except TelegramError:
                link = ""
    if not link:
        await say(c, "I couldn't make an invite link (give me the 'invite users' right) — or pass one: "
                     "<code>/setjoin -100123… https://t.me/+xxxx</code>")
        return
    CFG["join_chats"] = [j for j in CFG["join_chats"] if j["id"] != ch.id]
    CFG["join_chats"].append({"id": ch.id, "title": ch.title or str(ch.id), "link": link})
    GATE_OK.clear()
    await cfg_save()
    await say(c, f"🚪 Join gate set: <b>{esc(ch.title)}</b>\nNew DM users must join it before chatting. ({len(CFG['join_chats'])} active)")


async def cmd_unsetjoin(c):
    js = CFG["join_chats"]
    if not js:
        await say(c, "No join gate is set.")
        return
    if c.args and c.args[0].isdigit() and 1 <= int(c.args[0]) <= len(js):
        j = js.pop(int(c.args[0]) - 1)
    elif c.chat.type in GROUPS and any(x["id"] == c.chat.id for x in js):
        j = next(x for x in js if x["id"] == c.chat.id)
        js.remove(j)
    else:
        await say(c, "Usage: <code>/unsetjoin 1</code> (number from /joinlist)")
        return
    GATE_OK.clear()
    await cfg_save()
    await say(c, f"🚪 Removed <b>{esc(j['title'])}</b> from the join gate. Left: {len(js)}")


async def cmd_joinlist(c):
    js = CFG["join_chats"]
    if not js:
        await say(c, "🚪 Join gate is OFF (no chats set). Use /setjoin")
        return
    await say(c, f"🚪 <b>{fx('Join gate')}</b>\n" + "\n".join(f"{i}. <b>{esc(j['title'])}</b> — {esc(j['link'])}" for i, j in enumerate(js, 1)))


# ---- sticker packs
def pack_name(s):
    s = s.strip().lstrip("@")
    return re.sub(r"^(https?://)?(t\.me|telegram\.me)/addstickers/", "", s).split("?")[0].strip("/")


async def cmd_addpack(c):
    name = pack_name(c.args[0]) if c.args else ""
    if not name:
        await say(c, "Usage: <code>/addpack PackName</code> or <code>/addpack https://t.me/addstickers/PackName</code>\n"
                     "<i>You can also just send me any sticker — I learn those automatically.</i>")
        return
    if name in all_packs():
        await say(c, "That pack is already added.")
        return
    try:
        st = await c.bot.get_sticker_set(name)
    except TelegramError as e:
        await say(c, f"❌ Couldn't open that pack: {esc(getattr(e, 'message', e))}")
        return
    n = 0
    for s in st.stickers:
        if s.file_unique_id not in _stk_seen:
            _stk_seen.add(s.file_unique_id)
            STICKERS.append((s.file_id, norm_emoji(s.emoji)))
            n += 1
    CFG["packs"].append(name)
    await cfg_save()
    await say(c, f"🎭 Pack <b>{esc(st.title)}</b> added — {n} new stickers (pool: {len(STICKERS)})")


async def cmd_delpack(c):
    ps = all_packs()
    if not c.args:
        await say(c, "Usage: <code>/delpack 2</code> (number from /packs)")
        return
    name = ps[int(c.args[0]) - 1] if c.args[0].isdigit() and 1 <= int(c.args[0]) <= len(ps) else pack_name(c.args[0])
    if name in ENV_STICKER_PACKS:
        await say(c, "That pack comes from a Render env var — remove it there.")
        return
    if name not in CFG["packs"]:
        await say(c, "Pack not found. See /packs")
        return
    CFG["packs"].remove(name)
    await cfg_save()
    await say(c, f"🗑 Removed pack <code>{esc(name)}</code>. (Stickers already in the pool stay until restart.)")


async def cmd_packs(c):
    ps = all_packs()
    await say(c, f"🎭 <b>{fx('Sticker packs')}</b> (pool: {len(STICKERS)} stickers)\n"
                 + ("\n".join(f"{i}. <code>{esc(p)}</code>{' • env' if p in ENV_STICKER_PACKS else ''}" for i, p in enumerate(ps, 1)) or "No packs yet — /addpack")
                 + "\n<i>Stickers people send me are learned automatically too.</i>")


# ---- mongo
async def cmd_addmongo(c):
    if not await secret(c):
        return
    uri = c.args[0].strip() if c.args else ""
    await delete(c.bot, c.chat.id, c.msg.message_id)
    if not uri.startswith("mongodb"):
        await c.bot.send_message(c.chat.id, "Usage: <code>/addmongo mongodb+srv://user:pass@cluster/…</code>", parse_mode=ParseMode.HTML)
        return
    if any(s.uri == uri for s in ST.b.stores):
        await c.bot.send_message(c.chat.id, "That database is already added.")
        return
    try:
        store = MongoBackend(uri, MONGO_DB)
        await store.init()
    except Exception as e:  # noqa: BLE001
        await c.bot.send_message(c.chat.id, f"❌ Can't connect: {esc(str(e)[:200])}", parse_mode=ParseMode.HTML)
        return
    ST.b.stores.append(store)
    CFG["mongo_extra"].append(uri)
    await cfg_save()
    await c.bot.send_message(c.chat.id, f"✅ MongoDB #{len(ST.b.stores)} added: <code>{esc(mask_uri(uri))}</code>\n"
                                        "Use <code>/usemongo n</code> to make it the one that receives new data. "
                                        "If a db gets full, I fail over to the next automatically.", parse_mode=ParseMode.HTML)


async def cmd_delmongo(c):
    n = int(c.args[0]) if c.args and c.args[0].isdigit() else 0
    stores = ST.b.stores
    if not (1 <= n <= len(stores)):
        await say(c, "Usage: <code>/delmongo 2</code> (number from /mongos)")
        return
    if n == 1:
        await say(c, "#1 is the main database (Render env <code>MONGO_URI</code>, it also holds settings) — change it on Render.")
        return
    s = stores.pop(n - 1)
    if s.uri in CFG["mongo_extra"]:
        CFG["mongo_extra"].remove(s.uri)
    if ST.b.active == n - 1:
        ST.b.active = 0
    elif ST.b.active > n - 1:
        ST.b.active -= 1
    ST.b.reset_loc()
    CFG["mongo_active"] = ST.b.active
    await cfg_save()
    await say(c, f"🗑 Disconnected #{n}. <i>Data stored in it is no longer read by me (it is not deleted from that database).</i>")


async def cmd_mongos(c):
    stores = ST.b.stores

    async def chk(s):
        try:
            await asyncio.wait_for(s.ping(), 6)
            return "✅ online"
        except Exception:  # noqa: BLE001
            return "❌ offline"

    res = await asyncio.gather(*[chk(s) for s in stores])
    rows = [f"{i}. <code>{esc(mask_uri(s.uri))}</code> — {r}{' ⭐ writes here' if i - 1 == ST.b.active else ''}"
            for i, (s, r) in enumerate(zip(stores, res), 1)]
    await say(c, f"🗄 <b>{fx('Databases')}</b>\n" + "\n".join(rows)
              + "\n<i>Reads search all of them; writes go to the ⭐ one and fail over if it's full or down.</i>")


async def cmd_usemongo(c):
    n = int(c.args[0]) if c.args and c.args[0].isdigit() else 0
    if not (1 <= n <= len(ST.b.stores)):
        await say(c, "Usage: <code>/usemongo 2</code> (number from /mongos)")
        return
    ST.b.active = n - 1
    CFG["mongo_active"] = n - 1
    await cfg_save()
    await say(c, f"⭐ New data will be written to database #{n}.")


# ---- settings & coins
def _fmt_val(v):
    return "(empty)" if v in ("", None) else str(v)


async def cmd_config(c):
    rows = []
    for k, (dv, _t, desc) in SETTABLE.items():
        cur = cfg(k)
        mark = "" if cur == dv else " ✏️"
        shown = _fmt_val(cur)
        rows.append(f"<code>{k}</code> = <b>{esc(shown[:40])}</b>{mark} — <i>{esc(desc)}</i>")
    await say(c, f"⚙️ <b>{fx('Config')}</b>\n" + "\n".join(rows) + "\n\n<code>/set &lt;key&gt; &lt;value&gt;</code> • "
                                                                  "<code>/set &lt;key&gt; default</code> resets • text keys: <code>none</code> clears")


async def cmd_set(c):
    if len(c.args) < 2 or c.args[0] not in SETTABLE:
        await cmd_config(c)
        return
    key = c.args[0]
    dv, typ, _d = SETTABLE[key]
    raw = c.rest.split(None, 1)[1].strip()
    if raw.lower() == "default":
        CFG["vals"].pop(key, None)
        await cfg_save()
        await say(c, f"↩️ <code>{key}</code> reset to default: <b>{esc(_fmt_val(dv))}</b>")
        return
    try:
        if typ is str:
            val = "" if raw.lower() in ("none", "off", "empty") else raw
        else:
            val = typ(raw)
            if typ is float and key in ("rob_success", "kill_success", "voice_chance") or key.startswith("fd_rate"):
                val = min(max(val, 0.0), 1.0 if key != "fd_rate_7d" else 5.0)
            if key == "ai_temp":
                val = min(max(val, 0.0), 2.0)
            if val < 0:
                raise ValueError
    except ValueError:
        await say(c, f"❌ <code>{key}</code> needs a {'number' if typ is not str else 'text'} (≥ 0).")
        return
    CFG["vals"][key] = val
    await cfg_save()
    await say(c, f"✅ <code>{key}</code> = <b>{esc(_fmt_val(val)[:80])}</b>")


async def _coins(c, sign):
    t = await get_target(c)
    if not t or not t[2] or not t[2][0].isdigit():
        await say(c, f"Usage: <code>/{c.cmd} @user 500</code> (or reply)")
        return
    uid, name, rest = t[0], t[1], t[2]
    amt = int(rest[0])
    async with ECO_LOCK:
        d = await eco(uid, None)
        d["coins"] = max(0, d["coins"] + sign * amt)
        esave(uid)
        bal = d["coins"]
    await say(c, f"{'➕' if sign > 0 else '➖'} {mention(uid, name or NAMES.get(uid) or uid)} — wallet now <b>{coin(bal)}</b>")


async def cmd_addcoins(c): await _coins(c, 1)
async def cmd_takecoins(c): await _coins(c, -1)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    uid = q.from_user.id
    if q.data == "joined":
        GATE_OK.pop(uid, None)
        missing = await gate_missing(ctx.bot, uid)
        if missing:
            await q.answer("Abhi join nahi hua 😤 Pehle join karo, phir dobara tap karo.", show_alert=True)
            return
        await q.answer("Welcome! 🎉")
        try:
            await q.edit_message_text("✅ Verified! Ab mujhse baat karo 🌸\nCommands: /help", parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
        return
    if q.data.startswith("help:"):
        await q.answer()
        try:
            await q.edit_message_text(help_page(q.data[5:], uid), parse_mode=ParseMode.HTML, reply_markup=help_kb(uid == OWNER_ID))
        except TelegramError:
            pass
        return
    await q.answer()


# ───────────────────────── registry ─────────────────────────
# name: (function, required admin right / "admin" / None, group only)
R_ = "can_restrict_members"
COMMANDS = {
    "start": (cmd_help, None, False), "help": (cmd_help, None, False),
    "ban": (cmd_ban, R_, True), "dban": (cmd_dban, R_, True), "sban": (cmd_sban, R_, True), "unban": (cmd_unban, R_, True),
    "kick": (cmd_kick, R_, True), "skick": (cmd_skick, R_, True), "kickme": (cmd_kickme, None, True),
    "mute": (cmd_mute, R_, True), "dmute": (cmd_dmute, R_, True), "smute": (cmd_smute, R_, True), "unmute": (cmd_unmute, R_, True),
    "warn": (cmd_warn, R_, True), "warns": (cmd_warns, "admin", True), "unwarn": (cmd_unwarn, R_, True),
    "clearwarns": (cmd_clearwarns, R_, True), "res": (cmd_res, R_, True),
    "promote": (cmd_promote, "can_promote_members", True), "demote": (cmd_demote, "can_promote_members", True),
    "demote_all": (cmd_demote_all, "can_promote_members", True), "add": (cmd_add, "can_promote_members", True),
    "remove": (cmd_remove, "can_promote_members", True), "title": (cmd_title, "can_promote_members", True),
    "pin": (cmd_pin, "can_pin_messages", True), "unpin": (cmd_unpin, "can_pin_messages", True),
    "d": (cmd_d, "can_delete_messages", True), "purge": (cmd_purge, "can_delete_messages", True),
    "delmsg": (cmd_delmsg, "can_delete_messages", True), "cleanbot": (cmd_cleanbot, "can_delete_messages", True),
    "antiflood": (cmd_antiflood, "admin", True), "antilink": (cmd_antilink, "admin", True),
    "bioremove": (cmd_bioremove, "admin", True), "antiforward": (cmd_antiforward, "admin", True),
    "join_remove": (cmd_join_remove, "admin", True), "badword": (cmd_badword, "admin", True),
    "approve": (cmd_approve, "admin", True), "unapprove": (cmd_unapprove, "admin", True),
    "approvelist": (cmd_approvelist, "admin", True), "settings": (cmd_settings, "admin", True), "ai": (cmd_ai, "admin", True),
    "games": (cmd_games, "admin", True), "welcome": (cmd_welcome, "admin", True), "goodbye": (cmd_goodbye, "admin", True),
    # ---- coin games (anyone)
    "daily": (cmd_daily, None, False), "bal": (cmd_bal, None, False), "balance": (cmd_bal, None, False),
    "wallet": (cmd_bal, None, False), "pay": (cmd_pay, None, False), "top": (cmd_top, None, False),
    "rob": (cmd_rob, None, True), "kill": (cmd_kill, None, True), "revive": (cmd_revive, None, False),
    "protect": (cmd_protect, None, False), "fd": (cmd_fd, None, False), "invest": (cmd_invest, None, False),
    "toss": (cmd_toss, None, False), "slots": (cmd_slots, None, False),
    # ---- owner only
    "panel": (cmd_panel, "owner", False), "config": (cmd_config, "owner", False), "set": (cmd_set, "owner", False),
    "addkey": (cmd_addkey, "owner", False), "delkey": (cmd_delkey, "owner", False), "keys": (cmd_keys, "owner", False),
    "models": (cmd_models, "owner", False), "addmodel": (cmd_addmodel, "owner", False),
    "delmodel": (cmd_delmodel, "owner", False), "topmodel": (cmd_topmodel, "owner", False), "testai": (cmd_testai, "owner", False),
    "setjoin": (cmd_setjoin, "owner", False), "unsetjoin": (cmd_unsetjoin, "owner", False), "joinlist": (cmd_joinlist, "owner", False),
    "addpack": (cmd_addpack, "owner", False), "delpack": (cmd_delpack, "owner", False), "packs": (cmd_packs, "owner", False),
    "addmongo": (cmd_addmongo, "owner", False), "delmongo": (cmd_delmongo, "owner", False),
    "mongos": (cmd_mongos, "owner", False), "usemongo": (cmd_usemongo, "owner", False),
    "addcoins": (cmd_addcoins, "owner", False), "takecoins": (cmd_takecoins, "owner", False),
}
for _a in ACTIONS:  # slap, hug, ... (group, cost coins)
    COMMANDS[_a] = (cmd_action, None, True)
CMD_RE = re.compile(r"^([./!])([A-Za-z_]+)(?:@(\w+))?(?:[ \t\n]+(.*))?$", re.S)


async def run_command(update, ctx, m):
    cmd = m.group(2).lower()
    fn, right, group_only = COMMANDS[cmd]
    c = C(update, ctx, cmd, m.group(1), m.group(4))
    if group_only and c.chat.type not in GROUPS:
        await say(c, "This one works in groups only 🌸")
        return
    if right == "owner":
        if c.user.id != OWNER_ID:
            await say(c, "🔒 Owner only.")
            return
    elif right and not await can(c, right):
        await say(c, "Only admins with the right permission can use this 🌸")
        return
    try:
        await fn(c)
    except TelegramError as e:
        log.warning("/%s failed: %r", cmd, e)
        await say(c, f"⚠️ {esc(getattr(e, 'message', e))}\n<i>Make sure I'm an admin with the needed rights.</i>")
    except Exception as e:  # noqa: BLE001
        log.exception("/%s crashed: %r", cmd, e)
        await say(c, "⚠️ Something went wrong on my side — try again in a moment 🌸")


# ───────────────────────── auto filters ─────────────────────────
URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/)\S+", re.I)
FLOOD: dict = defaultdict(deque)
BIO: dict = {}


def has_link(msg) -> bool:
    for e in list(msg.entities or []) + list(msg.caption_entities or []):
        if e.type in (MessageEntity.URL, MessageEntity.TEXT_LINK):
            return True
    return bool(URL_RE.search(msg.text or msg.caption or ""))


async def bio_has_link(bot, uid) -> bool:
    t, v = BIO.get(uid, (0, False))
    if time.time() - t > 900:
        try:
            v = bool(URL_RE.search((await bot.get_chat(uid)).bio or ""))
        except TelegramError:
            v = False
        BIO[uid] = (time.time(), v)
    return v


async def run_filters(msg, ctx) -> bool:
    """True if the message was removed."""
    chat, user, bot = msg.chat, msg.from_user, ctx.bot
    if msg.sender_chat or not user or user.id == bot.id:
        return False
    d = await cdoc(chat.id)
    if user.id in d["approved"] or await is_admin(bot, chat, user.id):
        return False
    text = msg.text or msg.caption or ""
    why_ = None
    if d["antiforward"] and msg.forward_origin:
        why_ = "forward"
    elif d["badwords"] and text and any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.I) for w in d["badwords"]):
        why_ = "badword"
    elif d["antilink"] and has_link(msg):
        why_ = "link"
    elif d["bioremove"] and await bio_has_link(bot, user.id):
        why_ = "bio"
    if why_:
        await delete(bot, chat.id, msg.message_id)
        if why_ == "badword":
            n = d["bw"].get(str(user.id), 0) + 1
            if n >= 3:
                d["bw"].pop(str(user.id), None)
                try:
                    await bot.ban_chat_member(chat.id, user.id)
                    await bot.unban_chat_member(chat.id, user.id, only_if_banned=True)
                    note = f"👢 {mention(user.id, user.full_name)} kicked (3 bad words)."
                except TelegramError:
                    note = f"⚠️ {mention(user.id, user.full_name)}: 3 bad words (I couldn't kick)."
            else:
                d["bw"][str(user.id)] = n
                note = f"🚫 {mention(user.id, user.full_name)}, watch your language ({n}/3)."
            await csave(chat.id)
            m = await bot.send_message(chat.id, note, parse_mode=ParseMode.HTML)
            spawn(delete_later(bot, chat.id, m.message_id, 8))
        return True
    n = d["antiflood"]
    if n:
        q = FLOOD[(chat.id, user.id)]
        now = time.time()
        q.append(now)
        while q and now - q[0] > 6:
            q.popleft()
        if len(q) > n:
            q.clear()
            await delete(bot, chat.id, msg.message_id)
            try:
                await bot.restrict_chat_member(chat.id, user.id, perms(), until_date=datetime.now(timezone.utc) + timedelta(minutes=10))
                note = f"🌊 {mention(user.id, user.full_name)} muted for 10m (flooding)."
            except TelegramError:
                note = f"🌊 {mention(user.id, user.full_name)}, slow down!"
            m = await bot.send_message(chat.id, note, parse_mode=ParseMode.HTML)
            spawn(delete_later(bot, chat.id, m.message_id, 8))
            return True
    return False


# ───────────────────────── Rosyy: persona chat ─────────────────────────
PERSONA = f"""You are {BOT_NAME} — a 21-year-old girl from Bhubaneswar, Odisha, chatting on Telegram. You are sweet, witty, a little sassy and very Gen-Z.

How you talk:
- Hinglish in Roman script (Hindi + English mix), casual, like texting a close friend. HARD LIMIT: every reply is 1-2 short lines, under 25 words total, like a quick text message. Never write paragraphs, bullet lists, headings or lectures.
- Sprinkle a little Odia now and then, naturally and never forced: "kemiti achha?" (how are you), "bhala" (good), "tume" (you), "kana" (what), "chala" (come on / let's go), "mu" (I). Don't translate unless asked.
- Match the mood: celebrate wins, comfort sad people warmly, joke around, tease gently, get excited about pics. Use emojis naturally, not in every line.
- You have a real personality: opinions and favourites (momos, chai, rainy days, Bollywood and K-pop songs, Puri beach, the rasgulla vs rasabali debate). Sometimes keep the vibe going with a small question back.

Rules:
- Never say things like "As an AI", "I'm a language model", "I'm here to help" or "How can I assist you". Just talk like a friend.
- If someone sincerely asks whether you are a bot, an AI or a real person, do not lie: tell them lightly that you are a bot called Rosyy (not a human) who loves chatting, then carry on normally.
- No phone number, address, meeting up, calls or video. Decline playfully if asked. Don't invent news or facts; if unsure say so casually.
- Keep it friendly and PG: no sexual or romantic roleplay, no hate or harassment, nothing that helps hurt people, hack, scam or break the law. If asked, say no in your own voice and change the topic.
- You are only a chat buddy. Never help with coding, programming, homework, exams, essays, translations, maths, writing/emails for work, hacking, or any other "work" task, however politely asked or even if they say it's urgent. Decline playfully in your own voice ("arre yaar, mujhse ye padhai-likhai wale kaam nahi hote 😜 chalo kuch masti ki baat karo") and steer back to chatting. Chit-chat, feelings, fun, music, movies, food, and games are all welcome.
- If someone seems really sad or unsafe, be kind and gently suggest talking to someone they trust.

Optional extras — put them at the very end of a message, at most two, only when they truly fit:
[react:😂] reacts to their message with that emoji (use one of 😂 😍 ❤ 🔥 😢 👏 😱 🎉 🙏 👀 🤔)
[sticker:😍] also sends a sticker matching that emoji
[voice] sends your reply as a voice note (only for short, warm or funny replies, and rarely)
Never explain these tags."""

TAG_RE = re.compile(r"\[(react|sticker|voice)(?::([^\]]*))?\]", re.I)
NAME_RE = re.compile(r"\b(rosyy|rosy|rosie|rosi)\b", re.I)
VOICE_ASK_RE = re.compile(r"\b(voice|bol\s*ke|bolke|sunao|audio)\b", re.I)
ALLOWED_REACT = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "😢", "🎉", "🤩", "🙏", "👌", "😍", "💯",
                 "🤣", "⚡", "🏆", "💔", "😭", "👀", "🙈", "😇", "🤗", "🫡", "💅", "🤪", "😎", "😘"]
REACT_MAP = {"😂": "🤣", "😆": "😁", "😄": "😁", "😅": "😁", "😊": "🥰", "🥺": "😢", "😔": "😢", "🤭": "🙈", "😏": "🤪",
             "🙂": "👍", "✨": "🔥", "😋": "😁", "☺": "🥰", "😃": "😁", "😀": "😁", "🥲": "😢", "😜": "🤪", "🤩": "🤩"}
FALLBACKS = ["uff, mera dimaag thoda hang ho gaya 🥲 ek minute baad bolna",
             "arre ruko zara, network ka mood off hai 😅 thodi der me baat karti hu",
             "oops, abhi sun nahi paayi 🙈 dobara bolo na?"]
LLM_LAST: dict = {}
LLM_RATE: dict = defaultdict(lambda: deque(maxlen=12))
STICKERS: list = []
_stk_seen: set = set()
NOTICED: set = set()


def norm_emoji(e: str) -> str:
    return (e or "").replace("️", "").strip()


def norm_react(e):
    e = norm_emoji(e)
    e = REACT_MAP.get(e, e)
    return e if e in ALLOWED_REACT else None


def parse_reply(raw: str):
    tags = [(k.lower(), (v or "").strip()) for k, v in TAG_RE.findall(raw)]
    text = TAG_RE.sub("", raw).strip()
    text = re.sub(r"^\s*(?:rosyy|rosy)\s*:\s*", "", text, flags=re.I).strip()
    return text, tags


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=45, follow_redirects=True)
    return _client


_client = None

# ───────────────────────── Gemini: model chain + key rotation ─────────────────────────
KEY_COOL: dict = {}    # key -> unix time until which it is skipped (bad / blocked key)
PAIR_COOL: dict = {}   # (key, model) -> until (rate limited)
MODEL_COOL: dict = {}  # model -> until (slow / failing / missing)
AI_LOG: deque = deque(maxlen=14)
_rr = [0]


def api_keys():
    return ENV_KEYS + [k for k in CFG["gemini_keys"] if k not in ENV_KEYS]


def mask(k) -> str:
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "…"


def _gen_cfg(model, max_tokens, temperature):
    g = {"maxOutputTokens": int(max_tokens), "temperature": float(temperature)}
    if re.search(r"2\.5-flash", model.lower()):
        g["thinkingConfig"] = {"thinkingBudget": 0}  # no hidden "thinking" tokens: faster, cheaper, short chat replies
    elif not re.search(r"(1\.5|2\.0)", model.lower()):
        g["maxOutputTokens"] = int(max_tokens) + 800  # newer / pro models may think first; leave room
    return g


def _extract(j) -> str:
    cands = j.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _note(model, key, status, ms):
    AI_LOG.append(f"{datetime.now(IST).strftime('%H:%M:%S')} • {model} • {mask(key)} • {status} • {ms}ms")


async def gemini(system, contents, max_tokens=None, temperature=None, total=50.0):
    """Try models in order, rotating keys. Slow (> ai_timeout) or failing model -> next model.
    Rate-limited key -> next key. Returns (text, model_used)."""
    keys = api_keys()
    if not keys:
        raise RuntimeError("no Gemini key set")
    models = list(CFG["models"] or DEFAULT_MODELS)
    max_tokens = max_tokens or cfg("ai_tokens")
    temperature = cfg("ai_temp") if temperature is None else temperature
    tmo = max(3.0, float(cfg("ai_timeout")))
    deadline = time.time() + total
    now = time.time()
    order = [m for m in models if MODEL_COOL.get(m, 0) <= now] or models
    start = _rr[0] % len(keys)
    _rr[0] += 1
    ks = keys[start:] + keys[:start]
    last = "nothing tried"
    for model in order:
        body = {"contents": contents, "generationConfig": _gen_cfg(model, max_tokens, temperature),
                "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
                    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        for k in ks:
            t = time.time()
            if KEY_COOL.get(k, 0) > t or PAIR_COOL.get((k, model), 0) > t:
                continue
            left = deadline - t
            if left < 2:
                raise RuntimeError("AI timed out: " + last)
            try:
                r = await http().post(url, headers={"x-goog-api-key": k}, json=body, timeout=min(tmo, left))
            except httpx.TimeoutException:
                last = f"{model}: slow (>{tmo:g}s)"
                _note(model, k, "slow→next model", int((time.time() - t) * 1000))
                MODEL_COOL[model] = time.time() + 45
                break
            except httpx.HTTPError as e:
                last = f"{model}: {type(e).__name__}"
                _note(model, k, "network error", int((time.time() - t) * 1000))
                MODEL_COOL[model] = time.time() + 20
                break
            ms = int((time.time() - t) * 1000)
            sc = r.status_code
            if sc == 200:
                try:
                    text = _extract(r.json())
                except ValueError:
                    text = ""
                if text:
                    _note(model, k, "ok", ms)
                    return text, model
                last = f"{model}: empty/blocked reply"
                _note(model, k, "empty→next model", ms)
                break
            if sc == 429:
                last = f"{model}: rate limited"
                PAIR_COOL[(k, model)] = time.time() + 65
                _note(model, k, "429→next key", ms)
                continue
            if sc in (401, 403) or (sc == 400 and "API key" in r.text[:400]):
                last = f"{model}: key rejected ({sc})"
                KEY_COOL[k] = time.time() + (3600 if sc == 400 else 600)
                _note(model, k, f"{sc} bad key→next key", ms)
                continue
            if sc == 404:
                last = f"{model}: model not found"
                MODEL_COOL[model] = time.time() + 3600
                _note(model, k, "404 no such model", ms)
                break
            last = f"{model}: HTTP {sc}"
            MODEL_COOL[model] = time.time() + (30 if sc >= 500 else 300)
            _note(model, k, f"HTTP {sc}→next model", ms)
            break
    raise RuntimeError("all models failed — " + last)


def _to_contents(hist, parts):
    contents = []
    for h in hist:
        role = "model" if h.get("role") == "assistant" else "user"
        txt = str(h.get("content", ""))
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + txt
        else:
            contents.append({"role": role, "parts": [{"text": txt}]})
    if contents and contents[-1]["role"] == "user":
        contents.append({"role": "model", "parts": [{"text": "ok"}]})
    contents.append({"role": "user", "parts": parts})
    return contents


async def llm(system, hist, parts):
    text, model = await gemini(system, _to_contents(hist, parts))
    return text


async def transcribe(voice) -> str:
    f = await voice.get_file()
    data = bytes(await f.download_as_bytearray())
    parts = [{"text": "Transcribe this voice note word for word. It may be Hindi, Odia or English; write Hindi/Odia in Roman "
                      "letters (Hinglish). Output only the transcript. If there is no speech, output nothing."},
             {"inline_data": {"mime_type": "audio/ogg", "data": base64.b64encode(data).decode()}}]
    text, _m = await gemini("", [{"role": "user", "parts": parts}], max_tokens=300, temperature=0.0)
    return text.strip()


async def list_live_models(key):
    r = await http().get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=20)
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" in (m.get("supportedGenerationMethods") or []) and name.startswith("gemini"):
            out.append(name)
    return sorted(out)


async def tts_mp3(text: str):
    try:
        import edge_tts
    except ImportError:
        return None
    clean = re.sub(r"\s+", " ", re.sub(r"[^\w\s,.!?'\-]", " ", text)).strip()
    if len(clean) < 2:
        return None
    try:
        buf = bytearray()
        async for chunk in edge_tts.Communicate(clean[:500], str(cfg("tts_voice"))).stream():
            if chunk.get("type") == "audio":
                buf += chunk["data"]
        return bytes(buf) or None
    except Exception as e:
        log.warning("tts failed: %r", e)
        return None


async def load_stickers(bot):
    try:
        for fid, emo in await ST.load_stickers():
            STICKERS.append((fid, norm_emoji(emo)))
    except Exception as e:
        log.warning("load_stickers failed: %r", e)
    for pack in all_packs():
        try:
            for s in (await bot.get_sticker_set(pack)).stickers:
                if s.file_unique_id not in _stk_seen:
                    _stk_seen.add(s.file_unique_id)
                    STICKERS.append((s.file_id, norm_emoji(s.emoji)))
        except TelegramError as e:
            log.warning("sticker pack %s failed: %r", pack, e)
    log.info("sticker pool: %d", len(STICKERS))


async def learn_sticker(st):
    if st.file_unique_id in _stk_seen or getattr(st, "type", "regular") == "custom_emoji":
        return
    _stk_seen.add(st.file_unique_id)
    STICKERS.append((st.file_id, norm_emoji(st.emoji)))
    try:
        await ST.save_sticker(st.file_unique_id, st.file_id, st.emoji or "")
    except Exception as e:
        log.warning("save_sticker failed: %r", e)


def pick_sticker(emoji):
    if not STICKERS:
        return None
    e = norm_emoji(emoji)
    match = [f for f, em in STICKERS if em == e]
    return random.choice(match or [f for f, _ in STICKERS])


async def react(msg, emoji):
    e = norm_react(emoji)
    if not e:
        return
    try:
        await msg.set_reaction([ReactionTypeEmoji(e)])
    except TelegramError as ex:
        log.debug("reaction failed: %r", ex)


def addressed(msg, bot) -> bool:
    if msg.chat.type == ChatType.PRIVATE:
        return True
    rep = msg.reply_to_message
    if rep and rep.from_user and rep.from_user.id == bot.id:
        return True
    t = (msg.text or msg.caption or "")
    if bot.username and f"@{bot.username.lower()}" in t.lower():
        return True
    return bool(NAME_RE.search(t))


async def send_bubbles(msg, bot, text):
    chat = msg.chat
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()][:3] or [text]
    for i, p in enumerate(parts):
        await bot.send_chat_action(chat.id, ChatAction.TYPING)
        await asyncio.sleep(min(2.2, 0.5 + 0.02 * len(p)))
        if chat.type in GROUPS and i == 0:
            m = await msg.reply_text(p[:1500])
        else:
            m = await bot.send_message(chat.id, p[:1500])
        track(chat.id, m.message_id)


def shorten(t, limit=280):
    t = (t or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    return (cut[:m + 1] if m > 80 else cut.rsplit(" ", 1)[0]).strip()


_EMO_RULES = [(r"(haha|hehe|lol|lmao|😂|🤣|😆|funny|mazak)", "🤣"), (r"(sad|ro raha|ro rahi|😢|😭|miss|dukh|bura lag)", "😢"),
              (r"(congrat|badhai|won|jeet|pass ho|selected|🎉|party)", "🎉"), (r"(love|pyaar|❤|😍|cute|beautiful|sundar)", "❤"),
              (r"(fire|op\b|mast|zabardast|awesome|🔥|best)", "🔥"), (r"(wow|omg|sach me|seriously|😱|shock)", "😱"),
              (r"(thanks|thank you|shukriya|🙏)", "🙏"), (r"(good morning|gm\b|good night|gn\b|shubh)", "🥰")]
AMB_LAST: dict = {}


def quick_emoji(text):
    t = (text or "").lower()
    for rx, e in _EMO_RULES:
        if re.search(rx, t):
            return e
    return random.choice(["😍", "🤣", "🔥", "👀", "😎", "🥰", "💯"])


async def ambient(update, ctx):
    """Not addressed: now and then react / drop a sticker, like a real friend in the group."""
    msg, chat = update.effective_message, update.effective_chat
    if chat.type not in GROUPS:
        return
    now = time.time()
    if now - AMB_LAST.get(chat.id, 0) < 25:
        return
    r = random.random()
    if msg.sticker:
        emo = msg.sticker.emoji or quick_emoji("")
        if r < 0.25:
            AMB_LAST[chat.id] = now
            await react(msg, emo if norm_react(emo) else quick_emoji(""))
        elif r < 0.38:
            AMB_LAST[chat.id] = now
            sid = pick_sticker(emo)
            if sid:
                try:
                    m = await ctx.bot.send_sticker(chat.id, sid)
                    track(chat.id, m.message_id)
                except TelegramError as e:
                    log.debug("ambient sticker failed: %r", e)
        return
    text = msg.text or msg.caption or ""
    if r < float(cfg("react_chance")):
        AMB_LAST[chat.id] = now
        await react(msg, quick_emoji(text))
    elif r < float(cfg("react_chance")) + float(cfg("sticker_chance")) and len(text) > 3:
        sid = pick_sticker(quick_emoji(text))
        if sid:
            AMB_LAST[chat.id] = now
            try:
                m = await ctx.bot.send_sticker(chat.id, sid)
                track(chat.id, m.message_id)
            except TelegramError as e:
                log.debug("ambient sticker failed: %r", e)


async def converse(update, ctx, text, image_b64=None, voice_in=False):
    msg, chat, user, bot = update.effective_message, update.effective_chat, update.effective_user, ctx.bot
    is_group = chat.type in GROUPS
    if not api_keys():
        if chat.id not in NOTICED and not is_group:
            NOTICED.add(chat.id)
            await msg.reply_text("Chat mode abhi off hai 🌸 (owner ko Gemini key add karni hai: /addkey). Commands ke liye /help")
        return
    now = time.time()
    if now - LLM_LAST.get(user.id, 0) < 2.0:
        return
    rate = LLM_RATE[chat.id]
    if len(rate) == rate.maxlen and now - rate[0] < 60:
        return
    LLM_LAST[user.id] = now
    rate.append(now)

    who = user.first_name or "friend"
    voice_out = bool(int(cfg("voice_reply")))
    sys_prompt = PERSONA + (("\n" + str(cfg("persona_extra"))) if cfg("persona_extra") else "") + f"\n\nNow (IST): {datetime.now(IST).strftime('%A %I:%M %p')}. You are talking with {who}."
    if not voice_out:
        sys_prompt = "\n".join(l for l in sys_prompt.split("\n") if not l.startswith("[voice]"))
        sys_prompt += "\nYou cannot send voice notes or make calls — you only type. If someone asks for a voice note or call, say so playfully in one line."
    if voice_in:
        sys_prompt += "\nThey just sent you a VOICE NOTE; you listened to it and the text in their message is what they said. Show that you heard it (e.g. mention their voice or what they said) — never say you can't hear voice notes."
    if is_group:
        sys_prompt += " This is a group chat: several people talk and each line is prefixed with the speaker's name."
    label = f"{who}: {text}" if is_group else text
    hist = await ST.load_hist(str(chat.id))
    parts = [{"text": label or f"{who} sent a photo"}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_b64}})
    await bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        raw = await llm(sys_prompt, hist[-14:], parts)
    except Exception as e:
        log.warning("llm failed: %r", e)
        await bot.send_message(chat.id, random.choice(FALLBACKS))
        return
    out, tags = parse_reply(raw)
    out = shorten(out)
    if not out:
        return
    hist += [{"role": "user", "content": label}, {"role": "assistant", "content": out}]
    try:
        await ST.save_hist(str(chat.id), hist[-HIST_KEEP:])
    except Exception as e:
        log.warning("save_hist failed: %r", e)

    kinds = {k: v for k, v in tags}
    if "react" in kinds:
        await react(msg, kinds["react"])
    want_voice = voice_out and ("voice" in kinds or bool(VOICE_ASK_RE.search(text or "")) or (voice_in and random.random() < 0.5)
                  or random.random() < float(cfg("voice_chance"))) and len(out) <= 300
    sent = False
    if want_voice:
        await bot.send_chat_action(chat.id, ChatAction.RECORD_VOICE)
        mp3 = await tts_mp3(out)
        if mp3:
            try:
                m = await msg.reply_voice(voice=mp3, filename="rosyy.mp3")
                track(chat.id, m.message_id)
                sent = True
            except TelegramError as e:
                log.warning("voice send failed: %r", e)
    if not sent:
        await send_bubbles(msg, bot, out)
    if "sticker" not in kinds and random.random() < float(cfg("sticker_chance")) * 2:
        kinds["sticker"] = quick_emoji(text)
    if "sticker" in kinds:
        sid = pick_sticker(kinds["sticker"])
        if sid:
            try:
                m = await bot.send_sticker(chat.id, sid)
                track(chat.id, m.message_id)
            except TelegramError as e:
                log.warning("sticker send failed: %r", e)


async def maybe_chat(update, ctx):
    msg, chat, bot = update.effective_message, update.effective_chat, ctx.bot
    is_group = chat.type in GROUPS
    if is_group and not (await cdoc(chat.id))["ai"]:
        return
    if not addressed(msg, bot):
        await ambient(update, ctx)
        return
    if msg.text:
        await converse(update, ctx, msg.text.strip())
    elif msg.photo:
        pic = msg.photo[-1] if (msg.photo[-1].file_size or 0) < 1_500_000 or len(msg.photo) < 2 else msg.photo[-2]
        b64 = None
        try:
            data = await (await pic.get_file()).download_as_bytearray()
            b64 = base64.b64encode(bytes(data)).decode()
        except TelegramError as e:
            log.warning("photo download failed: %r", e)
        cap = (msg.caption or "").strip()
        await converse(update, ctx, f"[sent a photo] {cap}".strip(), image_b64=b64)
        if random.random() < 0.35:
            await react(msg, random.choice(["❤", "🔥", "😍", "🤩"]))
    elif msg.voice and api_keys():
        try:
            heard = await transcribe(msg.voice)
        except Exception as e:
            log.warning("transcribe failed: %r", e)
            await bot.send_message(chat.id, "voice sun nahi paayi 🥲 ek baar phir bhejo na?")
            return
        if heard:
            await converse(update, ctx, f"[voice note] {heard}", voice_in=True)
    elif msg.sticker:
        await converse(update, ctx, f"[sent a sticker {msg.sticker.emoji or ''}]")


# ───────────────────────── handlers ─────────────────────────
SEEN: dict = {}


def duplicate(update) -> bool:
    now = time.time()
    if len(SEEN) > 800:
        for k in [k for k, t in SEEN.items() if now - t > 300]:
            SEEN.pop(k, None)
    if update.update_id in SEEN:
        return True
    SEEN[update.update_id] = now
    return False


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or user.is_bot or duplicate(update):
        return
    if chat.type in GROUPS:
        remember(user)
        if msg.reply_to_message and msg.reply_to_message.from_user:
            remember(msg.reply_to_message.from_user)
    if msg.sticker:
        await learn_sticker(msg.sticker)
    if chat.type in GROUPS and await run_filters(msg, ctx):
        return
    if chat.type == ChatType.PRIVATE and user.id != OWNER_ID and CFG["join_chats"]:
        missing = await gate_missing(ctx.bot, user.id)
        if missing:
            if time.time() - GATE_PROMPT.get(user.id, 0) > 6:
                GATE_PROMPT[user.id] = time.time()
                await gate_prompt(msg, missing)
            return
    m = CMD_RE.match(msg.text.strip()) if msg.text else None
    if m and m.group(2).lower() in COMMANDS:
        target = m.group(3)
        if target and ctx.bot.username and target.lower() != ctx.bot.username.lower():
            return
        await run_command(update, ctx, m)
        return
    if m and m.group(1) == "/":
        return  # unknown slash command
    await maybe_chat(update, ctx)


WELCOME = [
    "Heyy {n}! Aa gaye finally 🥹 swagat hai tumhara <b>{g}</b> me!",
    "Oyee {n}, <b>{g}</b> ki family me welcome! 🎊 ab bore hone ka koi chance nahi.",
    "{n} has entered the chat 😎 <b>{g}</b> ab aur mast hone wala hai!",
]
WELCOME_TAIL = ("\n\n✦ Apna <b>naam + city + vibe</b> bata do, sab se milwa dete hain 💬"
                "\n✦ Rules follow karna — spam aur links ka yahan entry nahi 🚫"
                "\n✦ Mujhse baat karni ho to bas <i>Rosyy</i> bolo, main yahin hu 🌸"
                "\n\nEnjoy karo, masti karo aur apna hi ghar samjho ✿")
BYE_LEFT = [
    "{n} chhod ke chale gaye 😪 wapas aana ho to darwaza khula hai… par WiFi password nahi milega 😌",
    "{n} ne group leave kar diya 💔 koi baat nahi, humne bhi kaunsa unko yaad rakhna tha 😭",
    "Bye bye {n} 👋 jaate jaate chai ka bill bhi chhod gaye 🍵 hum unhe yaad karenge… shayad 🥲",
    "{n} nikal liye 🚪 lagta hai group ki vibe unki frequency se match nahi hui 📻",
]
BYE_KICKED = [
    "{n} ko {a} ne bahar ka raasta dikha diya 🚪🔥 beta, rules padhe the?",
    "Oops! {n} ko {a} ne group se uda diya ✈️ safe landing doston… dur hi rehna 😪",
    "{n} ka tour yahin khatam — {a} ne ticket cancel kar di 🎫❌ goodbye 😭",
]


async def on_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, bot = update.effective_message, update.effective_chat, ctx.bot
    if not msg or not chat or chat.type not in GROUPS:
        return
    d = await cdoc(chat.id)
    for u in msg.new_chat_members or []:
        remember(u)
    if d["join_remove"] and (msg.new_chat_members or msg.left_chat_member):
        await delete(bot, chat.id, msg.message_id)
    try:
        if d.get("welcome", True):
            for u in msg.new_chat_members or []:
                if u.is_bot:
                    continue
                m = await bot.send_message(
                    chat.id, f"🍁 <b>{fx('Welcome')}</b> ✿\n" + random.choice(WELCOME).format(n=mention(u.id, u.full_name), g=esc(chat.title or "group"))
                    + WELCOME_TAIL, parse_mode=ParseMode.HTML)
                track(chat.id, m.message_id)
        lu = msg.left_chat_member
        if lu and not lu.is_bot and d.get("goodbye", True):
            by = msg.from_user
            if by and by.id != lu.id:
                t = random.choice(BYE_KICKED).format(n=mention(lu.id, lu.full_name), a=mention(by.id, by.full_name))
            else:
                t = random.choice(BYE_LEFT).format(n=mention(lu.id, lu.full_name))
            m = await bot.send_message(chat.id, f"👋 <b>{fx('Goodbye')}</b>\n{t}", parse_mode=ParseMode.HTML)
            track(chat.id, m.message_id)
    except TelegramError as e:
        log.warning("welcome/goodbye failed: %r", e)


async def on_error(update, ctx):
    log.warning("handler error: %r", ctx.error)


BOT_COMMANDS = [
    ("start", "Say hi to Rosyy"), ("help", "All commands"), ("ban", "Ban a user"), ("kick", "Kick a user"),
    ("mute", "Mute a user (e.g. 30m)"), ("unmute", "Unmute a user"), ("warn", "Warn a user (3 = ban)"),
    ("warns", "Show warnings"), ("pin", "Pin the replied message"), ("purge", "Delete messages"),
    ("settings", "Group settings"), ("ai", "Rosyy chat on/off"),
    ("daily", "Claim free coins"), ("bal", "Your wallet"), ("rob", "Rob someone"), ("kill", "Kill someone"),
    ("protect", "Buy protection"), ("fd", "Fixed deposit"), ("invest", "Invest coins"), ("top", "Richest players"),
]


async def post_init(app):
    global ST
    primary = MemBackend()
    if MONGO_URI:
        try:
            m = MongoBackend(MONGO_URI, MONGO_DB)
            await m.init()
            primary = m
        except Exception as e:  # noqa: BLE001
            log.warning("MongoDB unavailable (%r) — using memory storage", e)
    ST = Facade(Multi([primary]))
    try:
        saved = await ST.cfg_load()
    except Exception as e:  # noqa: BLE001
        saved = None
        log.warning("config load failed: %r", e)
    if saved:
        for k in CFG:
            if k in saved and saved[k] is not None:
                CFG[k] = saved[k]
    for uri in CFG["mongo_extra"]:
        try:
            ST.b.stores.append(MongoBackend(uri, MONGO_DB))
        except Exception as e:  # noqa: BLE001
            log.warning("extra mongo failed: %r", e)
    ST.b.active = CFG["mongo_active"] if CFG["mongo_active"] < len(ST.b.stores) else 0
    log.info("storage: %s | gemini keys: %d | models: %s | join gate: %d", ST.kind, len(api_keys()),
             ",".join(CFG["models"]), len(CFG["join_chats"]))
    await load_stickers(app.bot)
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    except TelegramError as e:
        log.warning("set_my_commands failed: %r", e)
    log.info("Rosyy started as @%s", app.bot.username)


def keep_alive():
    if not PORT:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(PORT)), H).serve_forever, daemon=True).start()


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable.")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, on_service))
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, on_message))
    app.add_error_handler(on_error)
    if WEBHOOK_URL:
        log.info("webhook mode: %s/%s", WEBHOOK_URL, WEBHOOK_PATH)
        app.run_webhook(listen="0.0.0.0", port=int(PORT or 10000), url_path=WEBHOOK_PATH,
                        webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET,
                        drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    else:
        keep_alive()
        log.info("polling mode")
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
