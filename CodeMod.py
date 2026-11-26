import asyncio
import glob
import importlib
import logging
import subprocess
import sys
import time
import io
import os
import json
import math
import re
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple
from functools import wraps


def _discover_site_packages() -> None:
    """Auto-discover venv / .local site-packages in container environments."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    patterns = [
        os.path.join(base_dir, "venv", "lib", "python*", "site-packages"),
        os.path.join(base_dir, ".venv", "lib", "python*", "site-packages"),
        os.path.join(os.path.expanduser("~"), ".local", "lib", "python*", "site-packages"),
        "/home/container/.local/lib/python*/site-packages",
        "/home/container/venv/lib/python*/site-packages",
        "/home/container/.venv/lib/python*/site-packages",
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)


def _ensure_package(import_name: str, pip_name: Optional[str] = None, optional: bool = False):
    """Import a package, attempting a quiet pip install if missing."""
    try:
        return importlib.import_module(import_name)
    except ImportError:
        pkg = pip_name or import_name
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", pkg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            importlib.invalidate_caches()
            return importlib.import_module(import_name)
        except Exception:
            if optional:
                return None
            raise


_discover_site_packages()
_ensure_package("aiohttp")
_ensure_package("aiogram")
qrcode = _ensure_package("qrcode", "qrcode[pil]", optional=True)
if qrcode is not None:
    _ensure_package("PIL", "Pillow", optional=True)

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, LinkPreviewOptions,
    LabeledPrice, PreCheckoutQuery, BotCommand,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)

# ====================================================================================================
# [ CONFIGURATION ]
# ====================================================================================================

BOT_TOKEN = ""
OWNER_ID  = 

KHQR_API_KEY    = ""
KHQR_CREATE_URL        = "https://khqrpay.site/api/v1/aba/create"
KHQR_BAKONG_CREATE_URL = "https://khqrpay.site/api/v1/bakong/create"
KHQR_CHECK_URL         = "https://khqrpay.site/api/v1/payment"

DB_JSON_PATH = "reach.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger("BSLOUN")

if qrcode is None:
    logger.warning("⚠️ Package 'qrcode' is not available; KHQR will fall back to text/link mode if needed.")

from contextvars import ContextVar

_main_bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

_cv_bot: ContextVar[Bot] = ContextVar("cv_bot", default=_main_bot)
_cv_db_path: ContextVar[str] = ContextVar("cv_db_path", default=DB_JSON_PATH)
_cv_owner_id: ContextVar[int] = ContextVar("cv_owner_id", default=OWNER_ID)
_cv_is_clone: ContextVar[bool] = ContextVar("cv_is_clone", default=False)


class _BotProxy:
    def __getattr__(self, name):
        return getattr(_cv_bot.get(_main_bot), name)


bot = _BotProxy()
dp = Dispatcher()

# Clone runtime registries
_bot_runtime_meta: Dict[str, dict] = {}
_clone_tasks: Dict[str, asyncio.Task] = {}
_clone_bot_objs: Dict[str, Bot] = {}


def get_current_db_path() -> str:
    return _cv_db_path.get(DB_JSON_PATH)


def get_current_owner_id() -> int:
    return _cv_owner_id.get(OWNER_ID)


def is_clone_bot() -> bool:
    return _cv_is_clone.get(False)


@dp.update.outer_middleware()
async def _multi_bot_context_middleware(handler, event, data):
    b = data.get("bot") or _main_bot
    meta = _bot_runtime_meta.get(b.token)
    t_bot = _cv_bot.set(b)
    if meta:
        t_db = _cv_db_path.set(meta["db_path"])
        t_own = _cv_owner_id.set(meta["owner_id"])
        t_cln = _cv_is_clone.set(True)
    else:
        t_db = _cv_db_path.set(DB_JSON_PATH)
        t_own = _cv_owner_id.set(OWNER_ID)
        t_cln = _cv_is_clone.set(False)
    try:
        return await handler(event, data)
    finally:
        _cv_bot.reset(t_bot)
        _cv_db_path.reset(t_db)
        _cv_owner_id.reset(t_own)
        _cv_is_clone.reset(t_cln)

background_tasks = set()

def run_bg_task(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


# ====================================================================================================
# [ ⚡ SMOOTH CALLBACK HELPERS ]
# ====================================================================================================

async def quick_answer(call: CallbackQuery, text: Optional[str] = None, show_alert: bool = False):
    """Answer callback ភ្លាមៗ — ធ្វើឱ្យ button ចុចរលូន គ្មាន spinner យឺត"""
    try:
        await bot.answer_callback_query(
            callback_query_id=call.id,
            text=text,
            show_alert=show_alert,
        )
    except TelegramBadRequest:
        pass
    except Exception:
        pass


def auto_answer(text: Optional[str] = None, alert: bool = False):
    """Decorator: answer callback ភ្លាមៗមុន handler"""
    def decorator(handler):
        @wraps(handler)
        async def wrapper(call: CallbackQuery, *args, **kwargs):
            await quick_answer(call, text, alert)
            return await handler(call, *args, **kwargs)
        return wrapper
    return decorator


async def safe_edit_or_send(
    call: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    disable_preview: bool = True
):
    """Edit message បើអាច — បើមិនបាន delete+send (smooth)"""
    try:
        await call.message.edit_text(
            text,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=disable_preview)
        )
        return
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return
        if "message to edit not found" in err or "message can't be edited" in err:
            pass
    except Exception:
        pass

    try:
        await call.message.delete()
    except Exception:
        pass
    try:
        await bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=disable_preview)
        )
    except Exception as e:
        logger.error(f"safe_edit_or_send failed: {e}")


fast_answer = quick_answer

cancelled_payments = set()
_processed_payments: Dict[str, float] = {}

# 📌 តាមដាន QR Message សកម្មតាម User (uid -> (chat_id, msg_id, payment_id))
_active_user_qrs: Dict[int, Tuple[int, int, str]] = {}
_pending_manual_orders: Dict[str, dict] = {}
_user_start_msgs: Dict[int, List[Tuple[int, int]]] = {}
_user_last_ui_msg: Dict[int, Tuple[int, int]] = {}


async def _delete_user_start_msgs(uid: int):
    msgs = _user_start_msgs.pop(uid, [])
    for cid, mid in msgs:
        try:
            await bot.delete_message(cid, mid)
        except Exception:
            pass


async def _delete_last_ui_msg(uid: int):
    info = _user_last_ui_msg.pop(uid, None)
    if info:
        cid, mid = info
        try:
            await bot.delete_message(cid, mid)
        except Exception:
            pass

# ✅ Premium Icon IDs
ICON_CREATE_PRODUCT = "5904258298764334001"
ICON_DELETE_PRODUCT = "6039404727542747508"
ICON_ADD_PIC_VIDEO  = "6039451237743595514"
ICON_DEL_PIC_VIDEO  = "6039779802741739617"
ICON_ADD_PLAN       = "5944753741512052670"
ICON_DEL_PLAN       = "6028205772117118673"
ICON_ADD_FILE_LINK  = "6039522349517115015"
ICON_DEL_FILE_LINK  = "5983580310292402968"
ICON_BACK           = "5447506720316225765"  # ✅ Back Button Icon
ICON_BUY_NOW        = "5312361253610475399"
ICON_ADMIN_PANEL    = "5445372439397691719"
ICON_HELLO          = "6129863473305230077"
ICON_STORE          = "6039451237743595514"

# 👑 Admin Icons
ICON_ADD_ADMIN      = "5985596818912712352"
ICON_DEL_ADMIN      = "5985346521103604145"
ICON_VIEW_ADMINS    = "5985346521103604145"

# 🛠️ Maintenance Icons
ICON_MAINT_TITLE    = "6021401276904905698"
ICON_MAINT_STATUS   = "6195074326832619538"
ICON_MAINT_OPEN     = "5215685881989442149"
ICON_MAINT_CLOSE    = "5215313353706057331"

# 📦 Stock Plan Icons
ICON_STOCK_PLAN     = "6024106569430472546"
ICON_STK_PRODUCT    = "6017174676898321263"
ICON_STK_PLAN       = "5447187153274567373"
ICON_STK_PRICE      = "6030352469786105758"
ICON_STK_RESELLER   = "5886763041541853781"
ICON_STK_STOCK      = "5931472654660800739"

# 👥 Reseller List Icons
ICON_RES_TITLE       = "6021690418398239007"
ICON_RES_ID          = "5884366771913233289"
ICON_RES_USER        = "6008118472066732010"
ICON_RES_ORDERS      = "6024106569430472546"
ICON_RES_PRICE       = "6030352469786105758"
ICON_RES_TOTAL_MONEY = "5444960062407732826"
ICON_REFRESH         = "5447282724886839705"
ICON_EDIT_RES_NORM   = "6194811955870442525"
ICON_ADD_RES_VIP     = "6194804005885979475"
ICON_DEL_RES_VIP     = "6194754974539326139"
ICON_EDIT_RES_VIP    = "6201468188811602056"
ICON_PAY_STAR        = "6203769737231475220"

# 🎉 Delivery & Status Emojis
ICON_CREATING_KHQR    = "5386367538735104399"
ICON_BUY_SUCCESS      = "5312361253610475399"
ICON_KEY_LIST         = "6284816251143331422"
ICON_THANK_YOU        = "5285070644864628879"
ICON_INSTALL_LINK     = "5274099962655816924"
ICON_INSTALL_BTN      = "6147890838320979470"
ICON_PAY_CANCELLED    = "5445092669522996408"
ICON_STOCK_OUT        = "5445005936953424165"
ICON_QR_EXPIRED       = "5445350406215465190"

# 📢 Order Group Notification Emojis
ICON_GP_ORDER_NEW   = "5444860552310457690"
ICON_GP_PRODUCT     = "5447319442562251569"
ICON_GP_TOTAL_KEY   = "5447579253723918909"
ICON_GP_USERNAME    = "5447311106030726740"
ICON_GP_TIME_BUY    = "5445358884480916784"

# 🛒 Confirm Order / Quantity Emojis
ICON_CONFIRM_TITLE      = "5444860552310457690"
ICON_CONFIRM_PRODUCT    = "5447319442562251569"
ICON_CONFIRM_PLAN       = "5445033158456145975"
ICON_CONFIRM_UNITPRICE  = "5447579253723918909"
ICON_CONFIRM_STOCK      = "5445137435967126342"
ICON_CONFIRM_QTY        = "5447387942995655277"
ICON_CONFIRM_TOTAL      = "5447298551841322535"

# ⚙️ Setup Group Icons
ICON_SETUP_GROUP    = "5445059250382469069"
ICON_ADD_GROUP      = "5445372439397691719"
ICON_DEL_GROUP      = "5445092669522996408"
ICON_VIEW_GROUP     = "5444869180899752137"

# 💳 Top Up / Balance / How To Buy / New Admin Content Icons
ICON_TOPUP              = "5447579253723918909"
ICON_TOPUP_BTN          = "6323531378407252820"
ICON_SWITCH_BAKONG      = "5447602197439218445"
ICON_MANUAL_KHQR        = "6026337676091726218"
ICON_CURRENT_BAL        = "5444960062407732826"
ICON_ENTER_AMOUNT       = "5445128296276718145"
ICON_CUSTOM_AMT         = "5258215635996908355"
ICON_AMT_PROMPT         = "5258331647358540449"
ICON_BACK_NEW           = "5805509901048356965"
ICON_CREATING_QR_TOPUP  = "5307905813451397794"
ICON_HOW_TO_BUY         = "6194880074051757359"
ICON_PAY_BALANCE        = "5447421246172069841"
ICON_MANAGER_SETTING    = "6021622729713652937"
ICON_BOT_CONTENT        = "6021788373717358680"
ICON_CONTENT_EDIT       = "6021517988346206310"
ICON_SET_START_MEDIA    = "6024008227564296298"

ICON_MAIN_MENU          = "6023896773162967617"

# 👋 Start Message Icons
ICON_START_HELLO        = "6196975549940769899"
ICON_START_WAVE         = "5413694143601842851"
ICON_START_WELCOME      = "5444965061749644170"
ICON_START_SHOP         = "5447319442562251569"
ICON_START_TOPUP        = "5447579253723918909"

# ====================================================================================================
# [ HTTP SESSION ]
# ====================================================================================================

KHQR_TIMEOUT = aiohttp.ClientTimeout(total=40, connect=15, sock_read=25)
_http_session: Optional[aiohttp.ClientSession] = None


def get_http() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            ttl_dns_cache=300,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
        )
        _http_session = aiohttp.ClientSession(
            timeout=KHQR_TIMEOUT, connector=connector
        )
    return _http_session


async def close_http():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None

# ====================================================================================================
# [ JSON DATABASE ]
# ====================================================================================================

DEFAULT_DB = {
    "users": {},
    "categories": [],
    "items": [],
    "stock": [],
    "orders": [],
    "clones": [],
    "settings": {
        "contact_admin": "@Vbenstore",
        "bot_name": "REACH MOD VIP",
        "maintenance": "0",
        "order_group_id": "",
        "reseller_group_id": "",
        "start_message": "",
        "how_to_buy_media": "",
        "aba_enabled": "1",
        "payment_mode": "aba",
        "khqr_api_key": KHQR_API_KEY,
        "bakong_api_key": KHQR_API_KEY,
        "manual_qr_media": "",
        "contact_admin_id": "",
        "fn_buy_now": "1",
        "fn_top_up": "1",
        "fn_how_to_buy": "1"
    },
    "counters": {"categories": 0, "items": 0, "stock": 0, "orders": 0}
}

_db_caches: Dict[str, dict] = {}
_item_indexes: Dict[str, Dict[int, dict]] = {}
_cat_indexes: Dict[str, Dict[int, dict]] = {}
_stock_by_items: Dict[str, Dict[int, List[dict]]] = {}


def _write_db_file(data: dict, db_path: Optional[str] = None):
    p = db_path or get_current_db_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def _rebuild_indexes(data: dict, db_path: str):
    ii = _item_indexes.setdefault(db_path, {})
    ci = _cat_indexes.setdefault(db_path, {})
    sbi = _stock_by_items.setdefault(db_path, {})
    ii.clear()
    ci.clear()
    sbi.clear()
    for c in data.get("categories", []):
        ci[c["id"]] = c
    for i in data.get("items", []):
        ii[i["id"]] = i
        sbi[i["id"]] = []
    for s in data.get("stock", []):
        if s["item_id"] in sbi:
            sbi[s["item_id"]].append(s)


def _load_db(db_path: Optional[str] = None) -> dict:
    p = db_path or get_current_db_path()
    if p in _db_caches:
        return _db_caches[p]

    if not os.path.exists(p):
        logger.info(f"📁 Creating new JSON DB: {p}")
        data = json.loads(json.dumps(DEFAULT_DB))
        _write_db_file(data, p)
    else:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"❌ JSON DB corrupted ({p}): {e} — recreating")
            data = json.loads(json.dumps(DEFAULT_DB))
            _write_db_file(data, p)

    for k, v in DEFAULT_DB.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v))

    for sk, sv in DEFAULT_DB["settings"].items():
        if sk not in data["settings"]:
            data["settings"][sk] = sv

    for u in data.get("users", {}).values():
        if "balance" not in u:
            u["balance"] = 0.0
        if "is_reseller_vip" not in u:
            u["is_reseller_vip"] = 0

    for item in data.get("items", []):
        if "reseller_price" not in item:
            item["reseller_price"] = float(item.get("price", 0.0))
        if "reseller_vip_price" not in item:
            item["reseller_vip_price"] = float(item.get("reseller_price", item.get("price", 0.0)))

    _db_caches[p] = data
    _rebuild_indexes(data, p)
    return data


def _save_db(data: dict, db_path: Optional[str] = None):
    p = db_path or get_current_db_path()
    _db_caches[p] = data
    _rebuild_indexes(data, p)
    try:
        _write_db_file(data, p)
    except Exception as e:
        logger.error(f"❌ DB save failed ({p}): {e}")


def _next_id(data: dict, table: str) -> int:
    data["counters"][table] = data["counters"].get(table, 0) + 1
    return data["counters"][table]


def init_db(db_path: Optional[str] = None, owner_id: Optional[int] = None):
    p = db_path or get_current_db_path()
    oid = owner_id if owner_id is not None else get_current_owner_id()
    data = _load_db(p)
    owner_key = str(oid)
    if owner_key not in data["users"]:
        data["users"][owner_key] = {
            "is_admin": 1, "is_reseller": 0,
            "first_name": "Owner", "username": "",
            "total_orders": 0, "total_spent": 0.0,
            "balance": 0.0
        }
    else:
        data["users"][owner_key]["is_admin"] = 1
        data["users"][owner_key].setdefault("balance", 0.0)
    _save_db(data, p)
    logger.info(f"✅ JSON DB ready: {p} (items={len(data['items'])}, stock={len(data['stock'])})")


def db_get_user(uid: int) -> dict:
    return _load_db()["users"].get(str(uid), {})


def db_upsert_user(uid: int, **fields):
    data = _load_db()
    key = str(uid)
    if key not in data["users"]:
        data["users"][key] = {
            "is_admin": 0, "is_reseller": 0, "is_reseller_vip": 0,
            "first_name": "", "username": "",
            "total_orders": 0, "total_spent": 0.0,
            "balance": 0.0
        }
    data["users"][key].update(fields)
    _save_db(data)


def get_user_balance(uid: int) -> float:
    u = db_get_user(uid)
    try:
        return round(float(u.get("balance", 0.0)), 2)
    except (TypeError, ValueError):
        return 0.0


def add_user_balance(uid: int, amount: float) -> float:
    data = _load_db()
    key = str(uid)
    if key not in data["users"]:
        data["users"][key] = {
            "is_admin": 0, "is_reseller": 0, "is_reseller_vip": 0,
            "first_name": "", "username": "",
            "total_orders": 0, "total_spent": 0.0,
            "balance": 0.0
        }
    curr = float(data["users"][key].get("balance", 0.0))
    new_bal = round(curr + float(amount), 2)
    data["users"][key]["balance"] = new_bal
    _save_db(data)
    return new_bal


def deduct_user_balance(uid: int, amount: float) -> Tuple[bool, float]:
    data = _load_db()
    key = str(uid)
    if key not in data["users"]:
        return False, 0.0
    curr = round(float(data["users"][key].get("balance", 0.0)), 2)
    amt = round(float(amount), 2)
    if curr < amt:
        return False, curr
    new_bal = round(curr - amt, 2)
    data["users"][key]["balance"] = new_bal
    _save_db(data)
    return True, new_bal


def db_all_users():
    return list(_load_db()["users"].keys())


def is_user_reseller(uid: int) -> bool:
    u = db_get_user(uid)
    val = u.get("is_reseller", 0)
    return bool(val) or str(val).strip().lower() in ("1", "true")


def is_user_reseller_vip(uid: int) -> bool:
    u = db_get_user(uid)
    val = u.get("is_reseller_vip", 0)
    return bool(val) or str(val).strip().lower() in ("1", "true")


def get_item_price(item: dict, is_reseller: bool, is_vip: bool = False) -> float:
    if not item:
        return 0.0
    if is_vip:
        vip_p = item.get("reseller_vip_price")
        if vip_p is not None:
            try:
                return float(vip_p)
            except (ValueError, TypeError):
                pass
    if is_reseller or is_vip:
        res_p = item.get("reseller_price")
        if res_p is not None:
            try:
                return float(res_p)
            except (ValueError, TypeError):
                pass
    return float(item.get("price", 0.0))


def get_user_item_price(item: dict, uid: int) -> float:
    return get_item_price(item, is_user_reseller(uid), is_user_reseller_vip(uid))


def db_get_resellers():
    data = _load_db()
    resellers = []
    for uid, u in data["users"].items():
        uid_int = int(uid)
        is_vip = is_user_reseller_vip(uid_int)
        is_norm = is_user_reseller(uid_int)
        if is_vip or is_norm:
            resellers.append({
                "id": uid,
                "first_name": u.get("first_name", "Unknown"),
                "username": u.get("username", ""),
                "total_orders": u.get("total_orders", 0),
                "total_spent": u.get("total_spent", 0.0),
                "role": "Reseller VIP" if is_vip else "Reseller Normal",
            })
    resellers.sort(key=lambda x: x["total_spent"], reverse=True)
    return resellers


def db_get_admins():
    data = _load_db()
    admins = []
    for uid, u in data.get("users", {}).items():
        if int(uid) == OWNER_ID or bool(u.get("is_admin", 0)):
            admins.append({
                "id": uid,
                "first_name": u.get("first_name", "Unknown"),
                "username": u.get("username", ""),
                "is_owner": int(uid) == OWNER_ID
            })
    return admins


def db_set_setting(key: str, value: str):
    data = _load_db()
    data["settings"][key] = value
    _save_db(data)


def get_setting(key, default=None):
    return _load_db()["settings"].get(key, default)


def add_category(name: str, icon: str) -> int:
    data = _load_db()
    cid = _next_id(data, "categories")
    cat = {"id": cid, "name": name, "icon": icon, "media_data": None}
    data["categories"].append(cat)
    _save_db(data)
    return cid


def get_categories():
    return [(c["id"], c["name"], c.get("icon")) for c in _load_db()["categories"]]


def del_category(cid: int):
    data = _load_db()
    data["categories"] = [c for c in data["categories"] if c["id"] != cid]
    item_ids = [i["id"] for i in data["items"] if i["cat_id"] == cid]
    data["items"] = [i for i in data["items"] if i["cat_id"] != cid]
    data["stock"] = [s for s in data["stock"] if s["item_id"] not in item_ids]
    _save_db(data)


def set_category_media(cid: int, media):
    data = _load_db()
    for c in data["categories"]:
        if c["id"] == cid:
            c["media_data"] = media
            break
    _save_db(data)


def get_category(cid: int):
    return _cat_indexes.get(get_current_db_path(), {}).get(cid)


def add_item(cat_id, name, price, reseller_price, icon, reseller_vip_price=None) -> int:
    data = _load_db()
    iid = _next_id(data, "items")
    vip_p = float(reseller_vip_price) if reseller_vip_price is not None else float(reseller_price)
    item = {
        "id": iid, "cat_id": cat_id, "name": name,
        "price": float(price), "reseller_price": float(reseller_price),
        "reseller_vip_price": vip_p,
        "icon": icon, "post_media": None
    }
    data["items"].append(item)
    _save_db(data)
    return iid


def update_item_prices(item_id: int, price: float, reseller_price: float, reseller_vip_price: Optional[float] = None):
    data = _load_db()
    for i in data.get("items", []):
        if i["id"] == item_id:
            i["price"] = float(price)
            i["reseller_price"] = float(reseller_price)
            if reseller_vip_price is not None:
                i["reseller_vip_price"] = float(reseller_vip_price)
            break
    _save_db(data)


def update_item_single_price(item_id: int, field: str, value: float):
    data = _load_db()
    for i in data.get("items", []):
        if i["id"] == item_id:
            i[field] = float(value)
            break
    _save_db(data)


def get_items(cat_id: int):
    return [i for i in _load_db()["items"] if i["cat_id"] == cat_id]


def get_all_items():
    return _load_db()["items"]


def get_item(item_id: int):
    return _item_indexes.get(get_current_db_path(), {}).get(item_id)


def del_item(item_id: int):
    data = _load_db()
    data["items"] = [i for i in data["items"] if i["id"] != item_id]
    data["stock"] = [s for s in data["stock"] if s["item_id"] != item_id]
    _save_db(data)


def set_item_post_media(item_id: int, media):
    data = _load_db()
    for i in data["items"]:
        if i["id"] == item_id:
            i["post_media"] = media
            break
    _save_db(data)


def add_stock_row(item_id: int, key_data: str, stype: str) -> int:
    return add_stock_rows_bulk(item_id, [(key_data, stype)])


def add_stock_rows_bulk(item_id: int, rows: List[Tuple[str, str]]) -> int:
    data = _load_db()
    n = 0
    for key_data, stype in rows:
        sid = _next_id(data, "stock")
        data["stock"].append({
            "id": sid, "item_id": item_id, "key_data": key_data,
            "is_sold": 0, "type": stype, "sold_to": None, "sold_at": None
        })
        n += 1
    if n:
        _save_db(data)
    return n


def count_stock(item_id: int) -> int:
    bucket = _stock_by_items.get(get_current_db_path(), {}).get(item_id)
    if not bucket:
        return 0
    return sum(1 for s in bucket if s["is_sold"] == 0)


def get_stock_list(item_id: int):
    bucket = _stock_by_items.get(get_current_db_path(), {}).get(item_id) or []
    rows = [s for s in bucket if s["is_sold"] == 0]
    rows.sort(key=lambda x: x["id"])
    return [(s["id"], s["key_data"], s["type"]) for s in rows]


def del_stock_row(stock_id: int):
    data = _load_db()
    target = None
    for s in data["stock"]:
        if s["id"] == stock_id and s["is_sold"] == 0:
            target = s
            break
    if target is None:
        return
    data["stock"] = [s for s in data["stock"] if s["id"] != stock_id]
    _save_db(data)


def reserve_stock(item_id: int, qty: int):
    data = _load_db()
    bucket = _stock_by_items.get(get_current_db_path(), {}).get(item_id) or []
    available = [s for s in bucket if s["is_sold"] == 0]
    available.sort(key=lambda x: x["id"])
    if len(available) < qty:
        return []
    picked = available[:qty]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for s in picked:
        s["is_sold"] = 1
        s["sold_at"] = now
    _save_db(data)
    return [(s["id"], s["key_data"], s["type"]) for s in picked]


def record_order(uid, item_id, item_name, qty, total, payment_id="", keys: Optional[List[str]] = None):
    data = _load_db()
    oid = _next_id(data, "orders")
    now_kh = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")
    data["orders"].append({
        "id": oid, "user_id": uid, "item_id": item_id,
        "item_name": item_name, "qty": qty, "total": total,
        "payment_id": payment_id,
        "keys": keys or [],
        "created_at": now_kh
    })
    key = str(uid)
    if key in data["users"]:
        data["users"][key]["total_orders"] = data["users"][key].get("total_orders", 0) + 1
        data["users"][key]["total_spent"] = data["users"][key].get("total_spent", 0.0) + total
    _save_db(data)
    return oid


def get_user_orders(uid: int, limit: int = 20):
    rows = [o for o in _load_db()["orders"] if o["user_id"] == uid]
    rows.sort(key=lambda x: x["id"], reverse=True)
    return rows[:limit]


init_db()

# ====================================================================================================
# [ UI HELPERS ]
# ====================================================================================================

def _is_premium_id(s) -> bool:
    try:
        s2 = str(s).strip()
        return s2.isdigit() and len(s2) >= 15
    except Exception:
        return False


_IKB_FIELDS = None

def _ikb_has(field: str) -> bool:
    global _IKB_FIELDS
    if _IKB_FIELDS is None:
        try:
            _IKB_FIELDS = set(InlineKeyboardButton.model_fields.keys())
        except Exception:
            _IKB_FIELDS = set()
    return field in _IKB_FIELDS


def IBtn(text, callback_data=None, style=None, icon_custom_emoji_id=None, url=None):
    display_text = text
    real_icon = None

    if icon_custom_emoji_id:
        s = str(icon_custom_emoji_id).strip()
        if _is_premium_id(s):
            real_icon = s
        elif s:
            display_text = f"{s} {text}".strip()

    kwargs = {"text": display_text}
    if url:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = callback_data

    if real_icon and _ikb_has("icon_custom_emoji_id"):
        kwargs["icon_custom_emoji_id"] = real_icon
    if style and _ikb_has("style"):
        kwargs["style"] = style

    return InlineKeyboardButton(**kwargs)


def RBtn(text, style=None, icon_custom_emoji_id=None):
    display_text = text
    real_icon = None

    if icon_custom_emoji_id:
        s = str(icon_custom_emoji_id).strip()
        if _is_premium_id(s):
            real_icon = s
        elif s:
            display_text = f"{s} {text}".strip()

    btn = KeyboardButton(text=display_text)
    if style:
        try:
            object.__setattr__(btn, 'style', style)
        except Exception:
            pass
    if real_icon:
        try:
            object.__setattr__(btn, 'icon_custom_emoji_id', real_icon)
        except Exception:
            pass
    return btn


def cancel_inline():
    return InlineKeyboardMarkup(inline_keyboard=[[
        IBtn(text="Cancel", callback_data="cancel_action", style="danger")
    ]])


_cached_bot_names: Dict[str, str] = {}
_cached_bot_usernames: Dict[str, str] = {}


async def get_real_bot_username() -> str:
    tok = bot.token
    if tok in _cached_bot_usernames:
        return _cached_bot_usernames[tok]
    try:
        me = await bot.get_me()
        _cached_bot_usernames[tok] = me.username or ""
    except Exception:
        _cached_bot_usernames[tok] = ""
    return _cached_bot_usernames[tok]


async def get_real_bot_name() -> str:
    tok = bot.token
    if tok in _cached_bot_names:
        return _cached_bot_names[tok]
    try:
        me = await bot.get_me()
        _cached_bot_names[tok] = me.first_name or me.username or get_setting('bot_name', 'REACH MOD VIP')
    except Exception:
        _cached_bot_names[tok] = get_setting('bot_name', 'REACH MOD VIP')
    return _cached_bot_names[tok]


def get_main_reply_kb(uid: int) -> ReplyKeyboardMarkup:
    kb = []
    row1 = []
    if get_setting("fn_buy_now", "1") == "1":
        row1.append(RBtn(text="BUY NOW", style="primary", icon_custom_emoji_id=ICON_BUY_NOW))
    if get_setting("fn_top_up", "1") == "1":
        row1.append(RBtn(text="Top Up", style="success", icon_custom_emoji_id=ICON_TOPUP_BTN))
    if row1:
        kb.append(row1)
    if get_setting("fn_how_to_buy", "1") == "1":
        kb.append([RBtn(text="How to buy", style="danger", icon_custom_emoji_id=ICON_HOW_TO_BUY)])
    if is_admin_user(uid):
        kb.append([RBtn(text="Admin Panel", style="danger", icon_custom_emoji_id=ICON_ADMIN_PANEL)])
    if not kb:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, is_persistent=True)


async def cancel_and_delete_user_qr(uid: int):
    info = _active_user_qrs.pop(uid, None)
    if info:
        chat_id, msg_id, payment_id = info
        cancelled_payments.add(payment_id)
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


async def alert_to_channel(text):
    chan_id = get_setting('order_group_id') or get_setting('channel_id')
    if not chan_id:
        return
    try:
        await bot.send_message(int(chan_id), text)
    except Exception as e:
        logger.warning(f"alert_to_channel failed: {e}")


def is_admin_user(uid: int) -> bool:
    if uid == OWNER_ID or uid == get_current_owner_id():
        return True
    return bool(db_get_user(uid).get("is_admin", 0))


def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=([
        [IBtn(text="Manage Products", callback_data="adm_manage_products", style="primary",
              icon_custom_emoji_id=ICON_CREATE_PRODUCT)],
        [IBtn(text="View Stock", callback_data="adm_view_stock", style="primary",
              icon_custom_emoji_id=ICON_DELETE_PRODUCT),
         IBtn(text="Manage Stock", callback_data="adm_manage_stock", style="primary",
              icon_custom_emoji_id=ICON_ADD_PIC_VIDEO)],
        [IBtn(text="Broadcast", callback_data="adm_broadcast", style="primary",
              icon_custom_emoji_id=ICON_ADD_PLAN),
         IBtn(text="Manage Reseller", callback_data="adm_reseller", style="primary",
              icon_custom_emoji_id=ICON_DEL_PLAN)],
        [IBtn(text="Manage Admins", callback_data="adm_manage_admins", style="primary",
              icon_custom_emoji_id=ICON_ADD_ADMIN),
         IBtn(text="Manager Setting", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_MANAGER_SETTING)],
        [IBtn(text="Setup Group", callback_data="adm_setup_group", style="primary",
              icon_custom_emoji_id=ICON_SETUP_GROUP),
         IBtn(text="Maintenance", callback_data="adm_maintenance", style="primary",
              icon_custom_emoji_id=ICON_MAINT_OPEN)],
    ] + (
        [[IBtn(text="Clone Bot Customer", callback_data="adm_clone_menu", style="primary",
               icon_custom_emoji_id="6325393233845167901")]]
        if not is_clone_bot() else []
    )))


def manager_setting_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="+ User Deposit", callback_data="adm_user_deposit", style="success",
                 icon_custom_emoji_id=ICON_TOPUP),
            IBtn(text="- Withdraw User Money", callback_data="adm_user_withdraw", style="danger",
                 icon_custom_emoji_id=ICON_TOPUP),
        ],
        [
            IBtn(text="Bot Content Setting", callback_data="adm_bot_content", style="primary",
                 icon_custom_emoji_id=ICON_BOT_CONTENT),
            IBtn(text="Edit Contact Admin", callback_data="adm_edit_contact_admin", style="primary",
                 icon_custom_emoji_id="5258215635996908355"),
        ],
        [
            IBtn(text="Payment ABA", callback_data="adm_payment_aba", style="primary",
                 icon_custom_emoji_id="6021741116192201252"),
            IBtn(text="Close Product", callback_data="adm_close_product", style="danger",
                 icon_custom_emoji_id="6021440013214948027"),
        ],
        [
            IBtn(text="Disable function", callback_data="adm_disable_func", style="danger",
                 icon_custom_emoji_id="6021440013214948027"),
            IBtn(text="Enable the function", callback_data="adm_enable_func", style="success",
                 icon_custom_emoji_id="5445333299360723379"),
        ],
        [
            IBtn(text="Back", callback_data="back_admin", style="primary",
                 icon_custom_emoji_id=ICON_BACK_NEW),
        ]
    ])


def bot_content_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Edit Notification message (start)", callback_data="adm_edit_start_msg", style="primary",
              icon_custom_emoji_id=ICON_CONTENT_EDIT)],
        [IBtn(text="Video settings (how to buy)", callback_data="adm_set_howtobuy", style="primary",
              icon_custom_emoji_id=ICON_CONTENT_EDIT)],
        [IBtn(text="❌ Remove restrictions (how to buy)", callback_data="adm_del_howtobuy", style="danger")],
        [IBtn(text="Set Video / GIFs / File / Photo ( Start )", callback_data="adm_start_media", style="primary",
              icon_custom_emoji_id=ICON_SET_START_MEDIA)],
        [IBtn(text="❌ Remove Video / GIFs / File / Photo ( start )", callback_data="adm_del_start_media", style="danger")],
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])


def setup_group_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Add Group", callback_data="adm_add_group", style="primary",
              icon_custom_emoji_id=ICON_ADD_GROUP),
         IBtn(text="Del Grop", callback_data="adm_del_group", style="danger",
              icon_custom_emoji_id=ICON_DEL_GROUP)],
        [IBtn(text="View Gp Id", callback_data="adm_view_group", style="success",
              icon_custom_emoji_id=ICON_VIEW_GROUP)],
        [IBtn(text="Back", callback_data="back_admin", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])


def manage_products_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Create Product", callback_data="adm_add_cat", style="primary",
              icon_custom_emoji_id=ICON_CREATE_PRODUCT),
         IBtn(text="Delete Product", callback_data="adm_del_cat", style="primary",
              icon_custom_emoji_id=ICON_DELETE_PRODUCT)],
        [IBtn(text="Add Picture/Video", callback_data="adm_add_cat_media", style="primary",
              icon_custom_emoji_id=ICON_ADD_PIC_VIDEO),
         IBtn(text="Delete Picture/Video", callback_data="adm_del_cat_media", style="primary",
              icon_custom_emoji_id=ICON_DEL_PIC_VIDEO)],
        [IBtn(text="Add Plan", callback_data="adm_add_item", style="primary",
              icon_custom_emoji_id=ICON_ADD_PLAN),
         IBtn(text="Delete Plan", callback_data="adm_del_item", style="primary",
              icon_custom_emoji_id=ICON_DEL_PLAN)],
        [IBtn(text="Edit Price Plan", callback_data="adm_edit_item_price", style="primary",
              icon_custom_emoji_id=ICON_ADD_PLAN)],
        [IBtn(text="Add File/Link", callback_data="adm_add_post_media", style="primary",
              icon_custom_emoji_id=ICON_ADD_FILE_LINK),
         IBtn(text="Delete File/Link", callback_data="adm_del_post_media", style="primary",
              icon_custom_emoji_id=ICON_DEL_FILE_LINK)],
        [IBtn(text="Back", callback_data="back_admin", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])


def manage_admins_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="View Admins", callback_data="adm_view_admins", style="success",
              icon_custom_emoji_id=ICON_VIEW_ADMINS)],
        [IBtn(text="Add Admin", callback_data="adm_add_admin", style="primary",
              icon_custom_emoji_id=ICON_ADD_ADMIN),
         IBtn(text="Del Admin", callback_data="adm_del_admin", style="danger",
              icon_custom_emoji_id=ICON_DEL_ADMIN)],
        [IBtn(text="Back", callback_data="back_admin", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])


# ✅ កែសម្រួល៖ BOT MAINTENANCE ជាមួយនឹង Custom Emoji ID
def maintenance_kb():
    is_closed = get_setting("maintenance", "0") == "1"
    status_label = (
        f"<tg-emoji emoji-id=\"{ICON_MAINT_CLOSE}\">🔴</tg-emoji> <b>CLOSED (បិទ)</b>"
        if is_closed else
        f"<tg-emoji emoji-id=\"{ICON_MAINT_OPEN}\">🟢</tg-emoji> <b>OPEN (បើកដំណើរការ)</b>"
    )

    text = (
        f"<tg-emoji emoji-id=\"{ICON_MAINT_TITLE}\">🛠️</tg-emoji> <b>BOT MAINTENANCE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id=\"{ICON_MAINT_STATUS}\">🚦</tg-emoji> ស្ថានភាពបច្ចុប្បន្ន៖ {status_label}\n\n"
        f"<tg-emoji emoji-id=\"{ICON_MAINT_OPEN}\">🟢</tg-emoji> • <b>Open</b> : បើកឱ្យ User ទិញទំនិញធម្មតា\n"
        f"<tg-emoji emoji-id=\"{ICON_MAINT_CLOSE}\">🔴</tg-emoji> • <b>Close</b> : បិទ Bot (ពេល User ចុច Buy Now នឹងលោត \"Bot Update សូមរងចាំ\")\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Open", callback_data="adm_maint_open", style="success",
              icon_custom_emoji_id=ICON_MAINT_OPEN),
         IBtn(text="Close", callback_data="adm_maint_close", style="danger",
              icon_custom_emoji_id=ICON_MAINT_CLOSE)],
        [IBtn(text="Back", callback_data="back_admin", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    return text, kb


MANAGE_PRODUCTS_TEXT = "<b>Manage Products & Items</b>"
ADMIN_PANEL_TEXT = "<b>ADMIN PANEL</b>"

# ====================================================================================================
# [ QR CODE GENERATION ]
# ====================================================================================================

def extract_khqr_merchant_name(qr_string: str) -> str:
    """Parse EMVCo KHQR TLV tag 59 (Merchant Name) directly from qr_string."""
    if not qr_string:
        return ""
    try:
        idx = 0
        n = len(qr_string)
        while idx + 4 <= n:
            tag = qr_string[idx:idx + 2]
            length = int(qr_string[idx + 2:idx + 4])
            val_start = idx + 4
            val_end = val_start + length
            if val_end > n:
                break
            if tag == "59":
                return qr_string[val_start:val_end].strip()
            idx = val_end
    except Exception:
        pass
    return ""


def _load_pil_font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def render_khqr_png(qr_string: str, amount: float = 0.0, merchant_name: str = "") -> Optional[io.BytesIO]:
    if not qr_string or qrcode is None:
        return None
    try:
        from PIL import Image, ImageDraw

        m_name = (merchant_name or extract_khqr_merchant_name(qr_string) or "ABA MERCHANT").strip()

        # Portrait ABA PAY KHQR Card (420 x 720)
        W, H = 420, 720
        bg_color = (248, 249, 251)
        img = Image.new("RGB", (W, H), bg_color)
        draw = ImageDraw.Draw(img)

        # Top "ABA' PAY" branding
        f_brand = _load_pil_font(42, bold=True)
        aba_x, aba_y = 66, 22
        draw.text((aba_x, aba_y), "ABA", fill=(0, 95, 127), font=f_brand)
        aba_bbox = draw.textbbox((aba_x, aba_y), "ABA", font=f_brand)
        # Small red vertical accent bar between ABA and PAY
        bar_x = aba_bbox[2] + 6
        draw.rectangle([bar_x, aba_y + 4, bar_x + 6, aba_y + 18], fill=(227, 30, 36))
        draw.text((bar_x + 14, aba_y), "PAY", fill=(0, 188, 228), font=f_brand)

        # Card shadow + rounded white card
        cx0, cy0, cx1, cy1 = 28, 92, W - 28, H - 72
        draw.rounded_rectangle([cx0 + 3, cy0 + 5, cx1 + 3, cy1 + 5], radius=24, fill=(226, 230, 236))
        draw.rounded_rectangle([cx0, cy0, cx1, cy1], radius=24, fill=(255, 255, 255), outline=(235, 238, 242), width=1)

        # Red KHQR header bar with rounded top + bottom-right diagonal notch
        hdr_h = 64
        red = (227, 30, 36)
        draw.rounded_rectangle([cx0, cy0, cx1, cy0 + hdr_h], radius=24, fill=red)
        draw.rectangle([cx0, cy0 + 24, cx1, cy0 + hdr_h], fill=red)
        # Diagonal notch on bottom-right below header
        draw.polygon([(cx1 - 24, cy0 + hdr_h), (cx1, cy0 + hdr_h), (cx1, cy0 + hdr_h + 22)], fill=red)

        # "KHQR" centered in white
        f_khqr = _load_pil_font(26, bold=True)
        kw = draw.textlength("KHQR", font=f_khqr)
        draw.text(((W - kw) / 2, cy0 + 18), "KHQR", fill=(255, 255, 255), font=f_khqr)

        # Merchant Name & Amount
        f_merch = _load_pil_font(19, bold=False)
        f_amt = _load_pil_font(34, bold=True)
        f_usd = _load_pil_font(18, bold=False)

        tx = cx0 + 34
        draw.text((tx, cy0 + hdr_h + 26), m_name[:28], fill=(35, 38, 45), font=f_merch)

        amt_str = f"{amount:.2f}"
        amt_y = cy0 + hdr_h + 56
        draw.text((tx, amt_y), amt_str, fill=(20, 22, 26), font=f_amt)
        amt_bbox = draw.textbbox((tx, amt_y), amt_str, font=f_amt)
        draw.text((amt_bbox[2] + 8, amt_y + 13), "USD", fill=(45, 48, 55), font=f_usd)

        # Dashed separator line
        dash_y = cy0 + hdr_h + 116
        for dx in range(cx0 + 6, cx1 - 6, 14):
            draw.line([(dx, dash_y), (min(dx + 7, cx1 - 6), dash_y)], fill=(215, 219, 226), width=2)

        # Generate QR code with High error correction for center emblem
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=1,
        )
        qr.add_data(qr_string)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = 286
        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)

        # Center red KHQR emblem on QR
        em_r = 24
        qcx, qcy = qr_size // 2, qr_size // 2
        qdraw = ImageDraw.Draw(qr_img)
        qdraw.ellipse([qcx - em_r - 4, qcy - em_r - 4, qcx + em_r + 4, qcy + em_r + 4], fill=(255, 255, 255))
        qdraw.ellipse([qcx - em_r, qcy - em_r, qcx + em_r, qcy + em_r], fill=red)
        # Inner white geometric octagon/symbol
        qdraw.rectangle([qcx - 9, qcy - 9, qcx + 9, qcy + 9], outline=(255, 255, 255), width=2)
        qdraw.polygon([(qcx, qcy - 13), (qcx + 13, qcy), (qcx, qcy + 13), (qcx - 13, qcy)], outline=(255, 255, 255), width=2)

        qx = (W - qr_size) // 2
        qy = dash_y + 24
        img.paste(qr_img, (qx, qy))

        # Bottom footer text
        f_foot = _load_pil_font(15, bold=False)
        line1 = "Scan with ABA Mobile or any KHQR"
        line2 = "supported banking app"
        w1 = draw.textlength(line1, font=f_foot)
        w2 = draw.textlength(line2, font=f_foot)
        draw.text(((W - w1) / 2, H - 56), line1, fill=(135, 140, 148), font=f_foot)
        draw.text(((W - w2) / 2, H - 34), line2, fill=(135, 140, 148), font=f_foot)

        # Quantize compression (~16-24KB)
        quantized = img.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
        buf = io.BytesIO()
        quantized.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"QRCode card render failed: {e}")
        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=2,
            )
            qr.add_data(qr_string)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        except Exception:
            return None

# ====================================================================================================
# [ GLOBAL CANCEL ]
# ====================================================================================================

@dp.callback_query(F.data == "cancel_action")
@auto_answer()
async def cancel_action_handler(call: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    state_data = await state.get_data()
    await state.clear()
    await cancel_and_delete_user_qr(call.from_user.id)
    try:
        await call.message.delete()
    except Exception:
        pass

    if current_state == OrderFSM.custom_qty.state:
        item_id = state_data.get("item_id")
        if item_id:
            await render_checkout(call, item_id, 1, is_edit=False)
        else:
            await send_categories(call)
        return

    if is_admin_user(call.from_user.id):
        await bot.send_message(call.message.chat.id, ADMIN_PANEL_TEXT,
                               reply_markup=admin_main_kb())
    else:
        await send_categories(call)


@dp.callback_query(F.data.startswith("cancelpay_"))
async def cancel_payment_handler(call: CallbackQuery, state: FSMContext):
    payment_id = call.data.split("_", 1)[1]
    final_status = await khqr_check_payment(payment_id)
    if final_status and final_status.lower() in PAID_STATUSES:
        return await quick_answer(call, "ការទូទាត់បានជោគជ័យរួចហើយ! សូមរង់ចាំទំនិញ...", show_alert=True)

    cancelled_payments.add(payment_id)
    _active_user_qrs.pop(call.from_user.id, None)
    await quick_answer(call)
    await state.clear()

    try:
        await call.message.delete()
    except Exception:
        pass

    rk = get_main_reply_kb(call.from_user.id)
    await bot.send_message(
        call.message.chat.id,
        f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Payment cancelled.</b>",
        reply_markup=rk
    )


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await cancel_and_delete_user_qr(msg.from_user.id)
    rk = get_main_reply_kb(msg.from_user.id)
    await msg.answer(
        f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Payment cancelled.</b>",
        reply_markup=rk
    )

# ====================================================================================================
# [ KHQR API ]
# ====================================================================================================

PAID_STATUSES   = {"paid", "success", "completed", "approved", "successful"}
FAILED_STATUSES = {"expired", "failed", "underpaid", "cancelled", "canceled", "rejected"}


def _get_active_khqr_creds(provider: Optional[str] = None) -> Tuple[str, str, str]:
    mode = (provider or get_setting("payment_mode", "aba") or "aba").strip().lower()
    if mode == "bakong":
        api_key = get_setting("bakong_api_key", "") or get_setting("khqr_api_key", KHQR_API_KEY) or KHQR_API_KEY
        return "bakong", KHQR_BAKONG_CREATE_URL, api_key
    api_key = get_setting("khqr_api_key", KHQR_API_KEY) or KHQR_API_KEY
    return "aba", KHQR_CREATE_URL, api_key


async def khqr_create_payment(amount: float, provider: Optional[str] = None) -> Dict[str, Any]:
    mode, create_url, api_key = _get_active_khqr_creds(provider)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"ok": False, "error_text": "Invalid amount"}

    if amt < 0.01:
        return {"ok": False, "error_text": "Amount too small (min $0.01)"}

    payload = {"amount": amt}

    try:
        async with get_http().post(create_url, json=payload, headers=headers) as resp:
            raw_text = await resp.text()
            try:
                data = json.loads(raw_text)
            except Exception:
                logger.warning(f"KHQR ({mode}) create non-JSON [{resp.status}]: {raw_text[:200]}")
                return {"ok": False, "error_text": f"HTTP {resp.status}: invalid JSON response"}

            if not isinstance(data, dict):
                return {"ok": False, "error_text": "Malformed API response"}

            if "ok" not in data:
                data["ok"] = bool(data.get("id") or data.get("qr"))

            data.setdefault("provider", mode)
            logger.info(f"KHQR ({mode}) create [{resp.status}]: ok={data.get('ok')}, id={data.get('id')}, amount=${amt}")

            if not data.get("ok"):
                data.setdefault("error_text", f"HTTP {resp.status}: {data.get('error') or data.get('msg') or raw_text[:150]}")
            if data.get("ok") and not data.get("qr"):
                return {"ok": False, "error_text": "API returned ok but missing QR string"}
            return data

    except asyncio.TimeoutError:
        return {"ok": False, "error_text": "Gateway timeout"}
    except aiohttp.ClientError as e:
        return {"ok": False, "error_text": f"Network error: {e}"}
    except Exception as e:
        logger.error(f"KHQR ({mode}) Create Error: {e}", exc_info=True)
        return {"ok": False, "error_text": str(e)}


async def khqr_check_payment(payment_id: str, provider: Optional[str] = None) -> str:
    if not payment_id:
        return ""
    _, _, api_key = _get_active_khqr_creds(provider)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with get_http().get(KHQR_CHECK_URL, headers=headers,
                                  params={"id": payment_id}) as resp:
            if resp.status != 200:
                logger.warning(f"KHQR check HTTP {resp.status} for {payment_id}")
                return ""
            try:
                data = await resp.json(content_type=None)
            except Exception:
                return ""
            if not isinstance(data, dict):
                return ""

            if data.get("error"):
                logger.warning(f"KHQR check API error for {payment_id}: {data.get('error')}")
            payment_obj = data.get("payment") if isinstance(data.get("payment"), dict) else data
            status = str(
                payment_obj.get("status")
                or payment_obj.get("webhook_status")
                or ""
            ).strip().lower()
            return status
    except asyncio.TimeoutError:
        return ""
    except Exception as e:
        logger.error(f"KHQR Check Error: {e}")
        return ""


def _mark_processed(payment_id: str) -> bool:
    if not payment_id:
        return True
    now = time.time()
    stale = [k for k, t in _processed_payments.items() if now - t > 1800]
    for k in stale:
        _processed_payments.pop(k, None)
    if payment_id in _processed_payments:
        return False
    _processed_payments[payment_id] = now
    return True

# ====================================================================================================
# [ FSM STATES ]
# ====================================================================================================

class OrderFSM(StatesGroup):
    custom_qty = State()

class TopUpFSM(StatesGroup):
    custom_amount = State()

class AdminAddCat(StatesGroup):
    name = State(); icon = State()

class AdminAddItem(StatesGroup):
    cat_id = State(); name = State(); price = State()
    reseller_price = State(); reseller_vip_price = State(); icon = State()

class AdminEditItemPrice(StatesGroup):
    item_id = State()
    price = State()
    reseller_price = State()
    reseller_vip_price = State()

class AdminEditResellerPrice(StatesGroup):
    amount = State()

class AdminAddStock(StatesGroup):
    item_id = State(); keys = State()

class AdminBroadcast(StatesGroup):
    msg = State()

class AdminReseller(StatesGroup):
    add_id = State(); del_id = State()
    add_vip_id = State(); del_vip_id = State()

class AdminAdmin(StatesGroup):
    add_id = State(); del_id = State()

class AdminGroup(StatesGroup):
    add_id = State()

class AdminStartMedia(StatesGroup):
    media = State()

class AdminStartMessage(StatesGroup):
    text = State()

class AdminHowToBuyMedia(StatesGroup):
    media = State()

class AdminDepositFSM(StatesGroup):
    user_id = State()
    amount = State()

class AdminWithdrawFSM(StatesGroup):
    user_id = State()
    amount = State()

class AdminContactFSM(StatesGroup):
    username = State()

class AdminABAApiFSM(StatesGroup):
    api_key = State()

class AdminManualQRFSM(StatesGroup):
    photo = State()

class ManualPayFSM(StatesGroup):
    receipt = State()

class AdminCatMedia(StatesGroup):
    cat_id = State(); media = State()

class AdminPostMedia(StatesGroup):
    item_id = State(); media = State()

# ====================================================================================================
# [ START ]
# ====================================================================================================

async def send_start_message(chat_id: int, uid: int, fname: str, uname: str):
    await _delete_user_start_msgs(uid)
    await _delete_last_ui_msg(uid)

    is_new_user = not bool(db_get_user(uid))
    db_upsert_user(uid, first_name=fname, username=uname)

    if is_new_user:
        user_gp = get_setting("order_group_id")
        if user_gp:
            u_disp = f"@{uname}" if uname else fname
            bot_uname = await get_real_bot_username()
            bot_tag = f"@{bot_uname}" if bot_uname else await get_real_bot_name()
            now_kh = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %I:%M:%S %p")
            run_bg_task(bot.send_message(
                int(user_gp),
                f'<tg-emoji emoji-id="5904630315946611415">👤</tg-emoji> <b>New customers, please click here!</b>\n'
                f'<tg-emoji emoji-id="5904630315946611415">👤</tg-emoji> <b>User</b> {u_disp}\n'
                f'<tg-emoji emoji-id="5888781182249738113">🆔</tg-emoji> <b>User ID</b> <code>{uid}</code>\n'
                f'<tg-emoji emoji-id="6034973034257848185">⏰</tg-emoji> <b>Enter</b> {now_kh}\n'
                f'<tg-emoji emoji-id="6147853111328249787">🤖</tg-emoji> Please click on our Bot to purchase Key {bot_tag} '
                f'<tg-emoji emoji-id="6323608722178318625">✨</tg-emoji>'
            ))

    bot_name = await get_real_bot_name()
    rk = get_main_reply_kb(uid)

    custom_start = get_setting('start_message', '')
    if custom_start:
        text = (
            custom_start
            .replace("{name}", fname)
            .replace("{first_name}", fname)
            .replace("{bot_name}", bot_name)
        )
    else:
        text = (
            f"<tg-emoji emoji-id='6196975549940769899'>👋</tg-emoji> <b>សួរស្ដី {fname} !</b> "
            f"<tg-emoji emoji-id='5413694143601842851'>✨</tg-emoji>\n"
            f"<tg-emoji emoji-id='5444965061749644170'>🏪</tg-emoji> ស្វារគមន៍បងមកកាន់ប្រព័ន្វ <b>{bot_name}</b> . "
            f"លក់ Mod Key ដោយស្វ័យប្រវត្តិ ។ សូមបងចុចបូតុង "
            f"<tg-emoji emoji-id='5447319442562251569'>🛒</tg-emoji> <b>BUY NOW</b> ខាងក្រោមដើម្បីទិញ \n"
            f"ឬ បងចុចបូតុង "
            f"<tg-emoji emoji-id='5447579253723918909'>💵</tg-emoji> <b>Top Up</b> ដើម្បីដាក់ប្រាក់ចូល Bot ទិញទំនិញក៏បាន៖"
        )

    sent = None
    start_media = get_setting('start_media')
    if start_media:
        try:
            m_type, m_id = start_media.split(':', 1)
            if m_type == 'photo':
                sent = await bot.send_photo(chat_id, m_id, caption=text, reply_markup=rk)
            elif m_type == 'video':
                sent = await bot.send_video(chat_id, m_id, caption=text, reply_markup=rk)
            elif m_type == 'animation':
                sent = await bot.send_animation(chat_id, m_id, caption=text, reply_markup=rk)
            elif m_type == 'document':
                sent = await bot.send_document(chat_id, m_id, caption=text, reply_markup=rk)
        except Exception as e:
            logger.warning(f"start_media send failed: {e}")
            sent = None

    if sent is None:
        sent = await bot.send_message(chat_id, text, reply_markup=rk)

    if sent:
        _user_start_msgs.setdefault(uid, []).append((chat_id, sent.message_id))


@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await cancel_and_delete_user_qr(msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass
    fname = msg.from_user.first_name or "Unknown"
    uname = msg.from_user.username or ""
    await send_start_message(msg.chat.id, msg.from_user.id, fname, uname)

# ====================================================================================================
# [ TOP UP BALANCE FLOW ]
# ====================================================================================================

async def send_topup_menu(event, is_edit: bool = False):
    uid = event.from_user.id
    bal = get_user_balance(uid)
    text = (
        f"<tg-emoji emoji-id=\"{ICON_TOPUP}\">💵</tg-emoji> <b>Top Up Balance៖</b>\n"
        f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${bal:.2f}</b>\n\n"
        f"<tg-emoji emoji-id=\"{ICON_ENTER_AMOUNT}\">💳</tg-emoji> Please enter the amount of money you want to deposit into the account or select the amount you want to deposit ."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="1.00", callback_data="topup_amt_1.00", style="primary", icon_custom_emoji_id=ICON_TOPUP),
            IBtn(text="5.00", callback_data="topup_amt_5.00", style="primary", icon_custom_emoji_id=ICON_TOPUP),
            IBtn(text="10.00", callback_data="topup_amt_10.00", style="primary", icon_custom_emoji_id=ICON_TOPUP),
        ],
        [
            IBtn(text="15.00", callback_data="topup_amt_15.00", style="primary", icon_custom_emoji_id=ICON_TOPUP),
            IBtn(text="20.00", callback_data="topup_amt_20.00", style="primary", icon_custom_emoji_id=ICON_TOPUP),
        ],
        [
            IBtn(text="ចំនួនផ្ទាល់ខ្លួន", callback_data="topup_custom", style="success", icon_custom_emoji_id=ICON_CUSTOM_AMT),
        ],
        [
            IBtn(text="Main Menu", callback_data="topup_main_menu", style="danger", icon_custom_emoji_id=ICON_MAIN_MENU),
        ]
    ])
    if is_edit and isinstance(event, CallbackQuery):
        await safe_edit_or_send(event, text, kb)
    elif isinstance(event, Message):
        await _delete_last_ui_msg(uid)
        sent = await event.answer(text, reply_markup=kb)
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)
    else:
        await _delete_last_ui_msg(uid)
        sent = await bot.send_message(event.message.chat.id, text, reply_markup=kb)
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)


@dp.message(F.text == "Top Up")
async def topup_entry(msg: Message, state: FSMContext):
    if get_setting("fn_top_up", "1") != "1":
        return
    if get_setting("maintenance", "0") == "1" and not is_admin_user(msg.from_user.id):
        return await msg.answer("Bot Update សូមរងចាំ")
    await state.clear()
    await cancel_and_delete_user_qr(msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass
    await send_topup_menu(msg, is_edit=False)


@dp.callback_query(F.data == "topup_main_menu")
@auto_answer()
async def topup_main_menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cancel_and_delete_user_qr(call.from_user.id)
    try:
        await call.message.delete()
    except Exception:
        pass
    fname = call.from_user.first_name or "Unknown"
    uname = call.from_user.username or ""
    await send_start_message(call.message.chat.id, call.from_user.id, fname, uname)


@dp.callback_query(F.data == "topup_menu")
@auto_answer()
async def topup_menu_cb(call: CallbackQuery, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)
    await state.clear()
    await send_topup_menu(call, is_edit=True)


@dp.callback_query(F.data == "topup_back")
@auto_answer()
async def topup_back_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_topup_menu(call, is_edit=True)


@dp.callback_query(F.data == "topup_custom")
@auto_answer()
async def topup_custom_prompt(call: CallbackQuery, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)
    text = (
        f"<tg-emoji emoji-id=\"{ICON_AMT_PROMPT}\">🔢</tg-emoji> "
        f"Please enter the amount of money in numbers e.g. 1, 0.50"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="topup_back", style="danger", icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    await safe_edit_or_send(call, text, kb)
    await state.update_data(prompt_msg_id=call.message.message_id)
    await state.set_state(TopUpFSM.custom_amount)


@dp.message(TopUpFSM.custom_amount)
async def topup_custom_process(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace("$", "").replace(",", "")
    kb_back = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="topup_back", style="danger", icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    try:
        amt = round(float(raw), 2)
        if amt < 0.01:
            raise ValueError
    except Exception:
        return await msg.answer(
            f"<tg-emoji emoji-id=\"{ICON_AMT_PROMPT}\">🔢</tg-emoji> "
            f"Please enter the amount of money in numbers e.g. 1, 0.50",
            reply_markup=kb_back
        )

    data = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id")
    await state.clear()

    if prompt_msg_id:
        try:
            await bot.delete_message(msg.chat.id, prompt_msg_id)
        except Exception:
            pass
    try:
        await msg.delete()
    except Exception:
        pass

    await start_topup_qr(
        chat_id=msg.chat.id,
        uid=msg.from_user.id,
        amount=amt,
        first_name=msg.from_user.first_name or "",
        username=msg.from_user.username or "",
        state=state
    )


@dp.callback_query(F.data.startswith("topup_amt_"))
@auto_answer()
async def topup_preset_amt(call: CallbackQuery, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)
    await state.clear()
    try:
        amt = round(float(call.data.split("_")[2]), 2)
    except (ValueError, IndexError):
        return
    try:
        await call.message.delete()
    except Exception:
        pass

    await start_topup_qr(
        chat_id=call.message.chat.id,
        uid=call.from_user.id,
        amount=amt,
        first_name=call.from_user.first_name or "",
        username=call.from_user.username or "",
        state=state
    )


class ManualTopUpFSM(StatesGroup):
    receipt = State()


async def start_topup_qr(chat_id: int, uid: int, amount: float, first_name: str, username: str, state: Optional[FSMContext] = None):
    await cancel_and_delete_user_qr(uid)
    amt = round(float(amount), 2)

    if get_setting("aba_enabled", "1") != "1":
        rk = get_main_reply_kb(uid)
        return await bot.send_message(chat_id, "⚠️ Payment QR ត្រូវបានបិទបណ្ដោះអាសន្ន!", reply_markup=rk)

    if get_setting("payment_mode", "aba") == "manual_qr":
        qr_media = get_setting("manual_qr_media", "")
        if not qr_media:
            rk = get_main_reply_kb(uid)
            return await bot.send_message(chat_id, "⚠️ Admin មិនទាន់បានដាក់រូបភាព KHQR នៅឡើយទេ!", reply_markup=rk)
        cap = (
            f'<tg-emoji emoji-id="6026337676091726218">🏦</tg-emoji> <b>Top Up Balance (KHQR)</b>\n'
            f'━━━━━━━━━━━━━━━━━━━━\n'
            f'<tg-emoji emoji-id="5447155993286832278">💵</tg-emoji> <b>ចំនួនដាក់ប្រាក់៖</b> <b>${amt:.2f}</b>\n'
            f'━━━━━━━━━━━━━━━━━━━━\n'
            f'<tg-emoji emoji-id="6201496295077584895">📸</tg-emoji> <b>សូមស្កេន QR ខាងលើដើម្បីបង់ប្រាក់</b>\n'
            f'<tg-emoji emoji-id="6021405408663445899">📩</tg-emoji> បន្ទាប់ពីបង់រួច <b>សូមផ្ញើរូបភាពវិក្កយប័ត្រ (Screenshot)</b> មកទីនេះដើម្បីឱ្យ Admin បញ្ជាក់!'
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [IBtn(text="Cancel / បោះបង់", callback_data="cancel_action", style="danger",
                  icon_custom_emoji_id="6030602393933060595")]
        ])
        if state:
            await state.update_data(topup_amount=amt)
            await state.set_state(ManualTopUpFSM.receipt)
        try:
            m_type, m_id = qr_media.split(":", 1)
            if m_type == "photo":
                sent = await bot.send_photo(chat_id, m_id, caption=cap, reply_markup=kb)
            elif m_type == "document":
                sent = await bot.send_document(chat_id, m_id, caption=cap, reply_markup=kb)
            else:
                sent = await bot.send_message(chat_id, cap, reply_markup=kb)
            if state:
                await state.update_data(qr_msg_id=sent.message_id)
        except Exception as e:
            logger.error(f"topup manual qr failed: {e}")
            if state:
                await state.clear()
        return

    try:
        loading_msg = await bot.send_message(
            chat_id,
            f"<tg-emoji emoji-id=\"{ICON_CREATING_QR_TOPUP}\">⏳</tg-emoji> <b>I'm about to create a QR for you:</b>",
            reply_markup=ReplyKeyboardRemove()
        )
    except Exception:
        loading_msg = None

    res = await khqr_create_payment(amt)

    if loading_msg:
        try:
            await loading_msg.delete()
        except Exception:
            pass

    if not res or not res.get("ok"):
        err = (
            (res or {}).get("msg")
            or (res or {}).get("error_text")
            or (res or {}).get("error")
            or "Unknown error"
        )
        rk = get_main_reply_kb(uid)
        return await bot.send_message(
            chat_id,
            f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Failed to create QR!</b>\n<code>{err}</code>",
            reply_markup=rk
        )

    payment_id = str(res.get("id"))
    qr_string  = res.get("qr")
    expires_at = res.get("expires_at")
    aba_deep   = res.get("aba_deeplink")

    expires_ts = _parse_expires_at(expires_at)
    if expires_ts is None or expires_ts <= time.time():
        expires_ts = time.time() + 600

    p_mode = res.get("provider") or get_setting("payment_mode", "aba")
    qr_title = "BAKONG PAY (KHQR)" if p_mode == "bakong" else "ABA PAY WAY (KHQR)"
    cap_template = (
        f"<tg-emoji emoji-id=\"6158997026747983036\">🏦</tg-emoji> <b>{qr_title}</b>\n"
        f"- <tg-emoji emoji-id=\"{ICON_TOPUP}\">💵</tg-emoji> ប្រភេទ : <b>Top Up Balance</b>\n"
        f"- <tg-emoji emoji-id=\"5197434882321567830\">💵</tg-emoji> ចំនួនដាក់ប្រាក់ : <b>${amt:.2f}</b>\n"
        f"- <tg-emoji emoji-id=\"5444856076954520455\">🧾</tg-emoji> Ref : <code>{payment_id}</code>\n"
        f"- <tg-emoji emoji-id=\"5386367538735104399\">⏱️</tg-emoji> <b>QR Expires in :</b> <b>{{COUNTDOWN}}</b>\n"
        f"- <tg-emoji emoji-id=\"6129570565125577536\">⚡️</tg-emoji> ពេលផេររួច ទឹកប្រាក់នឹងចូលគណនីដោយស្វ័យប្រវត្តិ"
    )

    def build_cap(remaining: int) -> str:
        mm, ss = divmod(max(0, remaining), 60)
        return cap_template.replace("{COUNTDOWN}", f"{mm:02d}:{ss:02d}")

    initial_cap = build_cap(int(expires_ts - time.time()))

    kb_arr = []
    if aba_deep:
        kb_arr.append([IBtn(text="Open ABA Mobile", url=aba_deep, style="primary",
                            icon_custom_emoji_id="6316599954026535998")])

    kb_arr.append([IBtn(text="Cancel QR / បោះបង់", callback_data=f"cancelpay_{payment_id}",
                        style="danger", icon_custom_emoji_id="6102914615107133393")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_arr)

    merch_name = str(res.get("merchant_name") or res.get("account_name") or "").strip()
    png_buf = await asyncio.to_thread(render_khqr_png, qr_string, amt, merch_name)
    is_photo_msg = bool(png_buf)

    try:
        if png_buf:
            sent_msg = await bot.send_photo(
                chat_id,
                photo=BufferedInputFile(png_buf.getvalue(), filename="topup_khqr.png"),
                caption=initial_cap, reply_markup=kb)
        else:
            sent_msg = await bot.send_message(
                chat_id, initial_cap, reply_markup=kb,
                link_preview_options=LinkPreviewOptions(is_disabled=False))
    except Exception as e:
        logger.error(f"send TopUp QR failed: {e}")
        rk = get_main_reply_kb(uid)
        return await bot.send_message(
            chat_id,
            f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Failed to send QR. Try again.</b>",
            reply_markup=rk
        )

    _active_user_qrs[uid] = (chat_id, sent_msg.message_id, payment_id)

    run_bg_task(_live_qr_countdown(
        chat_id=chat_id,
        msg_id=sent_msg.message_id,
        payment_id=payment_id,
        expires_ts=expires_ts,
        caption_builder=build_cap,
        reply_markup=kb,
        is_photo=is_photo_msg,
    ))

    run_bg_task(poll_topup_payment(
        uid=uid,
        chat_id=chat_id,
        payment_id=payment_id,
        msg_id=sent_msg.message_id,
        amount=amt
    ))


async def poll_topup_payment(uid: int, chat_id: int, payment_id: str, msg_id: int, amount: float):
    timeout = time.time() + 600
    consecutive_errors = 0
    start_time = time.time()

    while time.time() < timeout:
        elapsed = time.time() - start_time
        if elapsed < 60:
            interval = 0.5
        elif elapsed < 180:
            interval = 0.2
        else:
            interval = 0.1

        if payment_id in cancelled_payments:
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            return

        if payment_id in _processed_payments:
            _active_user_qrs.pop(uid, None)
            return

        status = await khqr_check_payment(payment_id)

        if not status:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                await asyncio.sleep(min(2.0, interval * 4))
                continue
        else:
            consecutive_errors = 0

        if status in PAID_STATUSES:
            if not _mark_processed(payment_id):
                return
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            new_bal = add_user_balance(uid, amount)
            rk = get_main_reply_kb(uid)
            await bot.send_message(
                chat_id,
                f"<tg-emoji emoji-id=\"{ICON_BUY_SUCCESS}\">✅</tg-emoji> <b>Top Up Balance Successful!</b>\n\n"
                f"<tg-emoji emoji-id=\"{ICON_TOPUP}\">💵</tg-emoji> ចំនួនបានដាក់៖ <b>+${amount:.2f}</b>\n"
                f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${new_bal:.2f}</b>",
                reply_markup=rk
            )
            return

        if status in FAILED_STATUSES:
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            rk = get_main_reply_kb(uid)
            if status in ("cancelled", "canceled"):
                await bot.send_message(
                    chat_id,
                    f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Payment cancelled.</b>",
                    reply_markup=rk
                )
            else:
                await bot.send_message(
                    chat_id,
                    f"<tg-emoji emoji-id=\"{ICON_QR_EXPIRED}\">⌛</tg-emoji> <b>QR payment expired.</b>",
                    reply_markup=rk
                )
            return

        await asyncio.sleep(interval)

    cancelled_payments.discard(payment_id)
    _active_user_qrs.pop(uid, None)
    if msg_id:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    rk = get_main_reply_kb(uid)
    await bot.send_message(
        chat_id,
        f"<tg-emoji emoji-id=\"{ICON_QR_EXPIRED}\">⌛</tg-emoji> <b>QR payment expired.</b>",
        reply_markup=rk
    )

# ====================================================================================================
# [ HOW TO BUY ]
# ====================================================================================================

@dp.message(ManualTopUpFSM.receipt)
async def handle_manual_topup_receipt(msg: Message, state: FSMContext):
    if not msg.photo and not msg.document:
        return await msg.answer(
            '<tg-emoji emoji-id="6201496295077584895">📸</tg-emoji> សូមផ្ញើជារូបភាពវិក្កយប័ត្រ (Photo/Screenshot)!',
            reply_markup=cancel_inline()
        )
    data = await state.get_data()
    await state.clear()
    qr_msg_id = data.get("qr_msg_id")
    if qr_msg_id:
        try:
            await bot.delete_message(msg.chat.id, qr_msg_id)
        except Exception:
            pass

    uid = msg.from_user.id
    amt = float(data.get("topup_amount", 0.0))
    req_id = f"top_{uid}_{int(time.time())}"
    _pending_manual_orders[req_id] = {
        "uid": uid,
        "chat_id": msg.chat.id,
        "amount": amt,
    }
    rk = get_main_reply_kb(uid)
    await msg.answer(
        f'<tg-emoji emoji-id="6201623099692032873">⏳</tg-emoji> <b>វិក្កយប័ត្រ Top Up របស់អ្នកត្រូវបានផ្ញើទៅ Admin ហើយ!</b>\n'
        f'<tg-emoji emoji-id="6323531378407252820">💳</tg-emoji> ចំនួន៖ <b>${amt:.2f}</b>\n'
        f'សូមរង់ចាំ Admin បញ្ជាក់បន្តិច...',
        reply_markup=rk
    )

    u_disp = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.first_name or str(uid))
    adm_cap = (
        f'<tg-emoji emoji-id="6026337676091726218">🧾</tg-emoji> <b>សំណើ Top Up KHQR ថ្មី!</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="6019102674832595118">👤</tg-emoji> <b>User:</b> {u_disp} (<code>{uid}</code>)\n'
        f'<tg-emoji emoji-id="5447155993286832278">💵</tg-emoji> <b>Amount:</b> <b>${amt:.2f}</b>'
    )
    adm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IBtn(text="បញ្ជាក់", callback_data=f"mtconfirm_{req_id}", style="success",
             icon_custom_emoji_id="6021536113108196448"),
        IBtn(text="បោះបង់", callback_data=f"mtreject_{req_id}", style="danger",
             icon_custom_emoji_id="6030602393933060595"),
    ]])
    admins = db_get_admins()
    admin_ids = {int(a["id"]) for a in admins} | {OWNER_ID, get_current_owner_id()}
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    for aid in admin_ids:
        try:
            if msg.photo:
                await bot.send_photo(aid, file_id, caption=adm_cap, reply_markup=adm_kb)
            else:
                await bot.send_document(aid, file_id, caption=adm_cap, reply_markup=adm_kb)
        except Exception as e:
            logger.warning(f"notify admin {aid} topup receipt failed: {e}")


@dp.callback_query(F.data.startswith("mtconfirm_"))
async def admin_manual_topup_confirm(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Admin only!", show_alert=True)
    req_id = call.data.split("_", 1)[1]
    info = _pending_manual_orders.pop(req_id, None)
    if not info:
        return await quick_answer(call, "⚠️ សំណើនេះត្រូវបានដោះស្រាយរួចហើយ!", show_alert=True)
    await quick_answer(call, "✅ Confirmed!", show_alert=True)
    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n✅ <b>CONFIRMED BY ADMIN</b>",
            reply_markup=None
        )
    except Exception:
        pass
    new_bal = add_user_balance(info["uid"], info["amount"])
    try:
        rk = get_main_reply_kb(info["uid"])
        await bot.send_message(
            info["chat_id"],
            f'<tg-emoji emoji-id="6021536113108196448">✅</tg-emoji> <b>Top Up Balance Successful!</b>\n\n'
            f'<tg-emoji emoji-id="5447155993286832278">💵</tg-emoji> ចំនួនបានដាក់៖ <b>+${info["amount"]:.2f}</b>\n'
            f'<tg-emoji emoji-id="6323531378407252820">💰</tg-emoji> Current balance: <b>${new_bal:.2f}</b>',
            reply_markup=rk
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("mtreject_"))
async def admin_manual_topup_reject(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Admin only!", show_alert=True)
    req_id = call.data.split("_", 1)[1]
    info = _pending_manual_orders.pop(req_id, None)
    if not info:
        return await quick_answer(call, "⚠️ សំណើនេះត្រូវបានដោះស្រាយរួចហើយ!", show_alert=True)
    await quick_answer(call, "❌ Rejected!", show_alert=True)
    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n❌ <b>REJECTED BY ADMIN</b>",
            reply_markup=None
        )
    except Exception:
        pass
    try:
        rk = get_main_reply_kb(info["uid"])
        await bot.send_message(
            info["chat_id"],
            '<tg-emoji emoji-id="6030602393933060595">❌</tg-emoji> <b>សំណើ Top Up KHQR របស់អ្នកត្រូវបានបដិសេធដោយ Admin!</b>',
            reply_markup=rk
        )
    except Exception:
        pass


@dp.message(F.text == "How to buy")
async def how_to_buy_handler(msg: Message, state: FSMContext):
    if get_setting("fn_how_to_buy", "1") != "1":
        return
    uid = msg.from_user.id
    await state.clear()
    await cancel_and_delete_user_qr(uid)
    await _delete_last_ui_msg(uid)
    try:
        await msg.delete()
    except Exception:
        pass
    media = get_setting("how_to_buy_media", "")
    if not media:
        sent = await msg.answer(
            f"<tg-emoji emoji-id=\"{ICON_HOW_TO_BUY}\">🎬</tg-emoji> <b>How to buy</b>\n\n"
            f"មិនទាន់មានវីដេអូ ឬការណែនាំសម្រាប់ How to buy នៅឡើយទេ!"
        )
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)
        return
    try:
        m_type, content = media.split(":", 1)
        cap = f"<tg-emoji emoji-id=\"{ICON_HOW_TO_BUY}\">🎬</tg-emoji> <b>How to buy</b>"
        if m_type == "video":
            sent = await msg.answer_video(content, caption=cap)
        elif m_type == "photo":
            sent = await msg.answer_photo(content, caption=cap)
        elif m_type == "document":
            sent = await msg.answer_document(content, caption=cap)
        elif m_type == "animation":
            sent = await msg.answer_animation(content, caption=cap)
        else:
            sent = await msg.answer(content)
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)
    except Exception as e:
        logger.warning(f"how_to_buy_media send failed: {e}")
        sent = await msg.answer(
            f"<tg-emoji emoji-id=\"{ICON_HOW_TO_BUY}\">🎬</tg-emoji> <b>How to buy</b>\n\n"
            f"មិនទាន់មានវីដេអូ ឬការណែនាំសម្រាប់ How to buy នៅឡើយទេ!"
        )
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)

# ====================================================================================================
# [ SHOP FLOW ]
# ====================================================================================================

def render_my_orders_png(username_label: str, orders: List[dict]) -> Optional[io.BytesIO]:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    f_title = _load_pil_font(28, bold=True)
    f_sub = _load_pil_font(15, bold=False)
    f_hdr = _load_pil_font(14, bold=True)
    f_cell = _load_pil_font(13, bold=False)

    W = 1120
    margin_x = 36
    table_w = W - margin_x * 2
    # Columns: Order ID | Product | Amount | Status | Key(s) | Date
    col_widths = [165, 165, 115, 130, 325, 148]
    headers = ["Order ID", "Product", "Amount", "Status", "Key(s)", "Date"]

    # Prepare wrapped rows
    prepared_rows = []
    for o in orders:
        oid_str = f"VBEN-{o.get('id', 0)} (x{o.get('qty', 1)})"
        prod_str = str(o.get("item_name", "Item"))
        amt_str = f"${float(o.get('total', 0.0)):.2f}"
        status_str = "COMPLETED"
        date_str = str(o.get("created_at", ""))[:16]

        keys_list = o.get("keys") or []
        if keys_list:
            keys_joined = ", ".join([str(k) for k in keys_list])
        else:
            keys_joined = "[Delivered]"

        wrapped_keys = textwrap.wrap(keys_joined, width=38) or ["-"]
        wrapped_prod = textwrap.wrap(prod_str, width=20) or ["-"]
        max_lines = max(len(wrapped_keys), len(wrapped_prod), 1)
        row_h = max(44, 18 + max_lines * 18)
        prepared_rows.append((oid_str, wrapped_prod, amt_str, status_str, wrapped_keys, date_str, row_h))

    header_h = 34
    top_area_h = 104
    bottom_pad = 36
    total_rows_h = sum(r[6] for r in prepared_rows) if prepared_rows else 50
    H = max(340, top_area_h + header_h + total_rows_h + bottom_pad)

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Title & Generated subtitle
    draw.text((margin_x, 30), f"My Orders — {username_label}", fill=(15, 18, 22), font=f_title)
    now_str = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d-%m-%Y %I:%M %p")
    draw.text((margin_x, 68), f"Generated: {now_str} · {len(orders)} order(s)", fill=(120, 125, 132), font=f_sub)

    # Table Header (Green)
    y = top_area_h
    hdr_fill = (51, 122, 44)
    border_col = (140, 145, 150)
    x = margin_x
    for cw, htxt in zip(col_widths, headers):
        draw.rectangle([x, y, x + cw, y + header_h], fill=hdr_fill, outline=border_col, width=1)
        draw.text((x + 10, y + 9), htxt, fill=(255, 255, 255), font=f_hdr)
        x += cw
    y += header_h

    if not prepared_rows:
        draw.rectangle([margin_x, y, margin_x + table_w, y + 50], fill=(255, 255, 255), outline=border_col, width=1)
        draw.text((margin_x + 14, y + 16), "No orders found.", fill=(90, 95, 102), font=f_cell)
    else:
        for oid_str, w_prod, amt_str, status_str, w_keys, date_str, rh in prepared_rows:
            x = margin_x
            cells = [
                [oid_str],
                w_prod,
                [amt_str],
                [status_str],
                w_keys,
                [date_str],
            ]
            for cw, lines in zip(col_widths, cells):
                draw.rectangle([x, y, x + cw, y + rh], fill=(255, 255, 255), outline=border_col, width=1)
                ly = y + 10
                for line in lines:
                    draw.text((x + 10, ly), line, fill=(25, 28, 32), font=f_cell)
                    ly += 18
                x += cw
            y += rh

    quantized = img.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    buf = io.BytesIO()
    quantized.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


@dp.message(Command("buy_now"))
async def cmd_buy_now(msg: Message, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(msg.from_user.id):
        return await msg.answer("Bot Update សូមរងចាំ")
    await state.clear()
    await cancel_and_delete_user_qr(msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass
    await send_categories(msg)


@dp.message(Command("my_orders"))
async def cmd_my_orders(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    await state.clear()
    await cancel_and_delete_user_qr(uid)
    await _delete_last_ui_msg(uid)
    try:
        await msg.delete()
    except Exception:
        pass

    orders = get_user_orders(uid, limit=15)
    uname = msg.from_user.username
    label = f"@{uname}" if uname else (msg.from_user.first_name or f"ID {uid}")
    png_buf = await asyncio.to_thread(render_my_orders_png, label, orders)
    rk = get_main_reply_kb(uid)

    if png_buf:
        sent = await bot.send_photo(
            msg.chat.id,
            photo=BufferedInputFile(png_buf.getvalue(), filename="my_orders.png"),
            caption=f"📋 <b>My Orders — {label}</b>",
            reply_markup=rk
        )
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)
    else:
        if not orders:
            sent = await msg.answer("📋 <b>My Orders</b>\n\nមិនទាន់មានការបញ្ជាទិញនៅឡើយទេ។", reply_markup=rk)
        else:
            lines = [f"📋 <b>My Orders — {label}</b>"]
            for o in orders[:10]:
                lines.append(f"• <code>VBEN-{o['id']}</code> | {o['item_name']} x{o['qty']} | ${float(o['total']):.2f}")
            sent = await msg.answer("\n".join(lines), reply_markup=rk)
        _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)


@dp.message(F.text == "BUY NOW")
async def shop_categories(msg: Message, state: FSMContext):
    if get_setting("fn_buy_now", "1") != "1":
        return
    if get_setting("maintenance", "0") == "1" and not is_admin_user(msg.from_user.id):
        return await msg.answer("Bot Update សូមរងចាំ")

    await state.clear()
    await cancel_and_delete_user_qr(msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass
    await send_categories(msg)


async def send_categories(event):
    cats = get_categories()
    if not cats:
        txt = "❌ not have product !"
        if isinstance(event, CallbackQuery):
            return await safe_edit_or_send(event, txt)
        await _delete_last_ui_msg(event.from_user.id)
        sent = await event.answer(txt)
        _user_last_ui_msg[event.from_user.id] = (sent.chat.id, sent.message_id)
        return

    kb = [[IBtn(text=c[1], callback_data=f"sel_cat_{c[0]}",
                style="primary", icon_custom_emoji_id=c[2])] for c in cats]
    contact_raw = (get_setting("contact_admin", "@Vbenstore") or "@Vbenstore").strip()
    contact_clean = contact_raw.lstrip("@").replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").strip("/")
    contact_uid = str(get_setting("contact_admin_id", "") or "").strip()

    if contact_clean.isdigit():
        contact_uid = contact_clean
    elif not contact_uid:
        for u_id_str, u_obj in _load_db().get("users", {}).items():
            if str(u_obj.get("username", "")).strip().lstrip("@").lower() == contact_clean.lower():
                contact_uid = u_id_str
                db_set_setting("contact_admin_id", contact_uid)
                break
        if not contact_uid:
            try:
                chat_info = await bot.get_chat(f"@{contact_clean}")
                if chat_info and chat_info.id:
                    contact_uid = str(chat_info.id)
                    db_set_setting("contact_admin_id", contact_uid)
            except Exception:
                pass

    if contact_uid and contact_uid.isdigit():
        contact_url = f"tg://user?id={contact_uid}"
    else:
        contact_url = f"tg://user?id={OWNER_ID}" if contact_clean.lower() == "vbenstore" else f"tg://resolve?domain={contact_clean}"

    kb.append([IBtn(text="Contact Admin", url=contact_url, style="success",
                    icon_custom_emoji_id="6204269216158198672")])
    text = '<tg-emoji emoji-id="5312361253610475399">🛒</tg-emoji> <b>Categories all ៖</b>'
    mk = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event, text, mk)
    else:
        await _delete_last_ui_msg(event.from_user.id)
        sent = await event.answer(text, reply_markup=mk)
        _user_last_ui_msg[event.from_user.id] = (sent.chat.id, sent.message_id)


@dp.callback_query(F.data.startswith("sel_cat_"))
@auto_answer()
async def shop_items(call: CallbackQuery):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)

    try:
        cat_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return
    cat = get_category(cat_id)
    if not cat:
        return
    if cat.get("is_closed"):
        kb_closed = InlineKeyboardMarkup(inline_keyboard=[
            [IBtn(text="Back ( Category )", callback_data="back_to_cat",
                  style="danger", icon_custom_emoji_id="5352759161945867747")]
        ])
        closed_txt = '<tg-emoji emoji-id="6197472048160186777">🛠️</tg-emoji> <b>ម៉ូតកំពង់ Updates សូមបងទៅទិញម៉ូតផ្សេងបានណា៖</b>'
        return await safe_edit_or_send(call, closed_txt, kb_closed)
    items = get_items(cat_id)

    uid = call.from_user.id
    is_res = is_user_reseller(uid)
    is_vip = is_user_reseller_vip(uid)

    kb = []
    for i in items:
        stock_count = count_stock(i["id"])
        effective_price = get_item_price(i, is_res, is_vip)

        if is_vip:
            suffix = f"${effective_price:.2f} VIP" + (f" ({stock_count})" if stock_count > 0 else " (Out)")
        elif is_res:
            suffix = f"${effective_price:.2f} reseller" + (f" ({stock_count})" if stock_count > 0 else " (Out)")
        else:
            suffix = f"${effective_price:.2f}" + (f" ({stock_count})" if stock_count > 0 else " (Out)")

        btn_style = "success" if stock_count > 0 else "danger"
        kb.append([IBtn(text=f"{i['name']} - {suffix}", callback_data=f"sel_item_{i['id']}",
                        style=btn_style, icon_custom_emoji_id=i.get("icon"))])

    kb.append([IBtn(text="Back ( Category )", callback_data="back_to_cat",
                    style="danger", icon_custom_emoji_id="5352759161945867747")])
    mk = InlineKeyboardMarkup(inline_keyboard=kb)

    # 🔹 ចាប់យក Icon ID ពី Category (Custom Emoji ឬ Regular Emoji)
    cat_icon = str(cat.get("icon") or "").strip()
    if cat_icon.isdigit() and len(cat_icon) >= 15:
        icon_display = f'<tg-emoji emoji-id="{cat_icon}">📦</tg-emoji> '
    elif cat_icon:
        icon_display = f"{cat_icon} "
    else:
        icon_display = ""

    # ✅ ចេញ Icon នៅពីមុខឈ្មោះ Category (ឧ. [AmongUs] bdk)
    text = f"{icon_display}<b>{cat['name']}</b>"

    if cat.get("media_data"):
        try:
            await call.message.delete()
        except Exception:
            pass
        try:
            m_type, m_id = cat["media_data"].split(':', 1)
            if m_type == 'photo':
                await bot.send_photo(call.message.chat.id, m_id, caption=text, reply_markup=mk)
            elif m_type == 'video':
                await bot.send_video(call.message.chat.id, m_id, caption=text, reply_markup=mk)
            else:
                raise ValueError
        except Exception:
            await bot.send_message(call.message.chat.id, text, reply_markup=mk)
        return

    await safe_edit_or_send(call, text, mk)




@dp.callback_query(F.data == "back_to_cat")
@auto_answer()
async def back_to_cat_handler(call: CallbackQuery):
    await send_categories(call)


@dp.callback_query(F.data.startswith("sel_item_"))
@auto_answer()
async def pre_checkout_item(call: CallbackQuery, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)

    try:
        item_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return
    _it = get_item(item_id)
    _ct = get_category(_it.get("cat_id")) if _it else None
    if _ct and _ct.get("is_closed"):
        kb_closed = InlineKeyboardMarkup(inline_keyboard=[
            [IBtn(text="Back ( Category )", callback_data="back_to_cat",
                  style="danger", icon_custom_emoji_id="5352759161945867747")]
        ])
        return await safe_edit_or_send(
            call,
            '<tg-emoji emoji-id="6197472048160186777">🛠️</tg-emoji> <b>ម៉ូតកំពង់ Updates សូមបងទៅទិញម៉ូតផ្សេងបានណា៖</b>',
            kb_closed
        )
    if count_stock(item_id) <= 0:
        return await bot.send_message(
            call.message.chat.id,
            f"<tg-emoji emoji-id=\"{ICON_STOCK_OUT}\">⚠️</tg-emoji> <b>Stock Out! Please try again later.</b>"
        )
    await render_checkout(call, item_id, 1, is_edit=True)


@dp.callback_query(F.data.startswith("setqty_"))
@auto_answer()
async def ask_custom_qty(call: CallbackQuery, state: FSMContext):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)

    try:
        item_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    await state.update_data(item_id=item_id, msg_id=call.message.message_id)
    try:
        await call.message.edit_text("សូមវាយបញ្ចូលចំនួនដែលអ្នកចង់ទិញ (ជាលេខ):",
                                      reply_markup=cancel_inline())
    except Exception:
        pass
    await state.set_state(OrderFSM.custom_qty)


@dp.message(OrderFSM.custom_qty)
async def process_custom_qty(msg: Message, state: FSMContext):
    data = await state.get_data()
    try:
        qty = int((msg.text or "").strip())
        if qty <= 0:
            raise ValueError
    except Exception:
        return await msg.answer("❌ សូមបញ្ចូលចំនួនជាលេខត្រឹមត្រូវ!", reply_markup=cancel_inline())
    try:
        await msg.delete()
    except Exception:
        pass
    await render_checkout(msg, data['item_id'], qty, is_edit=False,
                          old_msg_id=data.get('msg_id'))
    await state.clear()


async def render_checkout(event, item_id, qty, is_edit=True, old_msg_id=None):
    uid = event.from_user.id
    chat_id = (
        event.message.chat.id
        if isinstance(event, CallbackQuery) and event.message
        else (event.chat.id if isinstance(event, Message) else event.from_user.id)
    )

    item = get_item(item_id)
    if not item:
        return

    cat = get_category(item.get("cat_id"))
    cat_name = cat["name"] if cat else "ទំនិញ"

    u_price = get_user_item_price(item, uid)
    total = u_price * qty
    stock_count = count_stock(item_id)
    bal = get_user_balance(uid)

    pay_enabled = stock_count >= qty and stock_count > 0

    text = (
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_TITLE}\">🛒</tg-emoji> <b>កំណត់ចំនួនទំនិញដែលចង់ទិញ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_PRODUCT}\">📦</tg-emoji> • <b>ទំនិញ៖</b> {cat_name}\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_PLAN}\">🏷</tg-emoji> • <b>កញ្ចប់៖</b> {item['name']}\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_UNITPRICE}\">💵</tg-emoji> • <b>តម្លៃរាយ៖</b> ${u_price:.2f} / មួយ\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_STOCK}\">📊</tg-emoji> • <b>ស្តុកនៅសល់៖</b> {stock_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_QTY}\">🔢</tg-emoji> • <b>ចំនួនជ្រើសរើស៖</b> {qty}\n"
        f"<tg-emoji emoji-id=\"{ICON_CONFIRM_TOTAL}\">💰</tg-emoji> • <b>តម្លៃសរុប៖</b> ${total:.2f}\n"
        f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💳</tg-emoji> • <b>សមតុល្យបច្ចុប្បន្ន៖</b> ${bal:.2f}\n\n"
        f"ចុច 'បញ្ចូលចំនួនផ្ទាល់ខ្លួន' ដើម្បីកែប្រែចំនួន ឬចុច 'បង់ប្រាក់' ដើម្បីបន្ត៖"
    )

    stars_amt = max(1, math.ceil(total / 0.02))
    kb_rows = [[IBtn(text="បញ្ចូលចំនួនផ្ទាល់ខ្លួន", callback_data=f"setqty_{item_id}",
                     style="primary", icon_custom_emoji_id="5258215635996908355")]]

    if pay_enabled:
        p_mode = get_setting("payment_mode", "aba")
        bal_enabled = get_setting("fn_top_up", "1") == "1"
        row_pay = []
        if get_setting("aba_enabled", "1") == "1":
            if p_mode == "manual_qr":
                row_pay.append(IBtn(text="បង់ KHQR", callback_data=f"manualqr_{item_id}_{qty}",
                                    style="success", icon_custom_emoji_id=ICON_MANUAL_KHQR))
            else:
                qr_label = "Bakong (KHQR)" if p_mode == "bakong" else "ABA (KHQR)"
                row_pay.append(IBtn(text=qr_label, callback_data=f"abapay_{item_id}_{qty}",
                                    style="success", icon_custom_emoji_id="6316599954026535998"))
        if bal_enabled:
            row_pay.append(IBtn(text=f"Buy with Balance • ${total:.2f}", callback_data=f"paybal_{item_id}_{qty}",
                                style="primary", icon_custom_emoji_id="6323531378407252820"))
        if row_pay:
            kb_rows.append(row_pay)
        kb_rows.append([
            IBtn(text=f"បង់តាម Star • {stars_amt}", callback_data=f"paystar_{item_id}_{qty}",
                 style="primary", icon_custom_emoji_id=ICON_PAY_STAR),
        ])
    else:
        kb_rows.append([IBtn(text="បង់ប្រាក់ (Out of Stock)", callback_data="out_of_stock",
                             style="danger", icon_custom_emoji_id="6316599954026535998")])

    kb_rows.append([IBtn(text="Back", callback_data=f"sel_cat_{item['cat_id']}",
                         style="danger", icon_custom_emoji_id="5352759161945867747")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass

    if is_edit and isinstance(event, CallbackQuery) and event.message:
        await safe_edit_or_send(event, text, kb)
        return

    await bot.send_message(chat_id, text, reply_markup=kb)


@dp.callback_query(F.data == "out_of_stock")
@auto_answer("Stock Out! Please try again later.", alert=True)
async def out_of_stock_handler(call: CallbackQuery):
    pass


@dp.callback_query(F.data.startswith("paybal_"))
async def handle_pay_balance(call: CallbackQuery):
    uid = call.from_user.id

    if get_setting("maintenance", "0") == "1" and not is_admin_user(uid):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)

    parts = call.data.split("_")
    try:
        item_id, qty = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return await quick_answer(call, "❌ Invalid request.", show_alert=True)

    stock_qty = count_stock(item_id)
    if qty > stock_qty or stock_qty <= 0:
        return await quick_answer(call, f"⚠️ Stock Out! (មាន {stock_qty})", show_alert=True)

    item = get_item(item_id)
    if not item:
        return await quick_answer(call, "Item not found.", show_alert=True)

    u_price = get_user_item_price(item, uid)
    total = round(u_price * qty, 2)
    bal = get_user_balance(uid)

    if bal < total:
        await quick_answer(
            call,
            f"⚠️ សមតុល្យមិនគ្រប់គ្រាន់! ត្រូវការ ${total:.2f} (មាន ${bal:.2f})",
            show_alert=True
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [IBtn(text="Top Up", callback_data="topup_menu", style="success", icon_custom_emoji_id=ICON_TOPUP)],
            [IBtn(text="Back", callback_data=f"sel_item_{item_id}", style="danger", icon_custom_emoji_id=ICON_BACK_NEW)]
        ])
        await safe_edit_or_send(
            call,
            f"<tg-emoji emoji-id=\"{ICON_PAY_BALANCE}\">💳</tg-emoji> <b>សមតុល្យមិនគ្រប់គ្រាន់!</b>\n\n"
            f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${bal:.2f}</b>\n"
            f"<tg-emoji emoji-id=\"{ICON_CONFIRM_TOTAL}\">💰</tg-emoji> តម្លៃសរុប៖ <b>${total:.2f}</b>\n\n"
            f"សូមចុច <b>Top Up</b> ដើម្បីដាក់ប្រាក់បន្ថែម៖",
            reply_markup=kb
        )
        return

    await quick_answer(call)
    await cancel_and_delete_user_qr(uid)

    payment_id = f"BAL-{int(time.time())}-{uid}"
    await deliver_item(
        uid=uid,
        user_snapshot={
            "id": uid,
            "first_name": call.from_user.first_name,
            "username": call.from_user.username
        },
        chat_id=call.message.chat.id,
        item_id=item_id,
        qty=qty,
        msg_id=call.message.message_id,
        total_paid=total,
        item_name=item["name"],
        payment_id=payment_id,
        deduct_bal=True
    )


@dp.callback_query(F.data.startswith("paystar_"))
async def handle_pay_star(call: CallbackQuery):
    uid = call.from_user.id

    if get_setting("maintenance", "0") == "1" and not is_admin_user(uid):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)

    parts = call.data.split("_")
    try:
        item_id, qty = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return await quick_answer(call, "❌ Invalid request.", show_alert=True)

    stock_qty = count_stock(item_id)
    if qty > stock_qty or stock_qty <= 0:
        return await quick_answer(call, f"⚠️ Stock Out! (មាន {stock_qty})", show_alert=True)

    item = get_item(item_id)
    if not item:
        return await quick_answer(call, "Item not found.", show_alert=True)

    cat = get_category(item.get("cat_id"))
    cat_name = cat["name"] if cat else "Product"

    u_price = get_user_item_price(item, uid)
    total = round(u_price * qty, 2)
    stars_amt = max(1, math.ceil(total / 0.02))

    await quick_answer(call)
    await cancel_and_delete_user_qr(uid)
    try:
        await call.message.delete()
    except Exception:
        pass

    payload = f"star_{item_id}_{qty}_{uid}_{int(time.time())}"
    sent = await bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"{item['name']} x{qty}",
        description=f"{cat_name} | {item['name']} x{qty} (${total:.2f})",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{item['name']} x{qty}", amount=stars_amt)],
    )
    _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)


@dp.pre_checkout_query()
async def star_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    payload = pre_checkout_q.invoice_payload or ""
    if payload.startswith("star_"):
        parts = payload.split("_")
        try:
            item_id, qty = int(parts[1]), int(parts[2])
            if count_stock(item_id) < qty:
                return await pre_checkout_q.answer(
                    ok=False,
                    error_message="Stock Out! Please try again later."
                )
        except Exception:
            return await pre_checkout_q.answer(ok=False, error_message="Invalid order.")
    await pre_checkout_q.answer(ok=True)


@dp.message(F.successful_payment)
async def star_successful_payment(msg: Message):
    sp = msg.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("star_"):
        return
    parts = payload.split("_")
    try:
        item_id, qty = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return

    uid = msg.from_user.id
    item = get_item(item_id)
    if not item:
        return

    u_price = get_user_item_price(item, uid)
    total = round(u_price * qty, 2)
    charge_id = sp.telegram_payment_charge_id or str(int(time.time()))

    await _delete_last_ui_msg(uid)
    await deliver_item(
        uid=uid,
        user_snapshot={
            "id": uid,
            "first_name": msg.from_user.first_name,
            "username": msg.from_user.username
        },
        chat_id=msg.chat.id,
        item_id=item_id,
        qty=qty,
        msg_id=0,
        total_paid=total,
        item_name=item["name"],
        payment_id=f"STAR-{charge_id}",
        deduct_bal=False
    )

# ====================================================================================================
# [ ABA KHQR CHECKOUT ]
# ====================================================================================================

def _parse_expires_at(expires_at) -> Optional[float]:
    if expires_at is None:
        return None

    if isinstance(expires_at, (int, float)):
        v = float(expires_at)
        return v / 1000 if v > 1e12 else v

    s = str(expires_at).strip()
    if not s:
        return None

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    return None


async def _live_qr_countdown(chat_id: int, msg_id: int, payment_id: str,
                              expires_ts: float, caption_builder,
                              reply_markup: Optional[InlineKeyboardMarkup] = None,
                              is_photo: bool = True):
    """⚡ Smooth countdown — update 200ms ជិត expiry, 500ms ធម្មតា"""
    last_cap = None
    last_edit_time = 0

    while True:
        if payment_id in cancelled_payments:
            return

        remaining = int(expires_ts - time.time())
        if remaining < 0:
            remaining = 0

        cap = caption_builder(remaining)
        now = time.time()

        if cap != last_cap and (now - last_edit_time >= 0.9 or remaining <= 5):
            try:
                if is_photo:
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=msg_id,
                        caption=cap,
                        reply_markup=reply_markup,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=cap,
                        reply_markup=reply_markup,
                        link_preview_options=LinkPreviewOptions(is_disabled=False),
                    )
                last_cap = cap
                last_edit_time = now
            except TelegramBadRequest as e:
                low = str(e).lower()
                if "message to edit not found" in low or "message can't be edited" in low:
                    return
                if "message is not modified" in low:
                    last_cap = cap
                elif "flood" in low or "retry" in low:
                    await asyncio.sleep(1.5)
            except Exception:
                pass

        if remaining <= 0:
            return

        if remaining > 10:
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(0.2)


@dp.callback_query(F.data.startswith("manualqr_"))
async def handle_manual_qr_pay(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if get_setting("maintenance", "0") == "1" and not is_admin_user(uid):
        return await quick_answer(call, "Bot Update សូមរងចាំ", show_alert=True)
    if get_setting("aba_enabled", "1") != "1":
        return await quick_answer(call, "⚠️ Payment ត្រូវបានបិទបណ្ដោះអាសន្ន!", show_alert=True)

    qr_media = get_setting("manual_qr_media", "")
    if not qr_media:
        return await quick_answer(call, "⚠️ Admin មិនទាន់បានដាក់រូបភាព KHQR នៅឡើយទេ!", show_alert=True)

    parts = call.data.split("_")
    try:
        item_id, qty = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return await quick_answer(call, "❌ Invalid request.", show_alert=True)

    stock_qty = count_stock(item_id)
    if qty > stock_qty or stock_qty <= 0:
        return await quick_answer(call, f"⚠️ Stock Out! (មាន {stock_qty})", show_alert=True)

    item = get_item(item_id)
    if not item:
        return await quick_answer(call, "Item not found.", show_alert=True)

    cat = get_category(item.get("cat_id"))
    cat_name = cat["name"] if cat else "Product"
    u_price = get_user_item_price(item, uid)
    total = round(u_price * qty, 2)

    await quick_answer(call)
    try:
        await call.message.delete()
    except Exception:
        pass

    cap = (
        f'<tg-emoji emoji-id="6026337676091726218">🏦</tg-emoji> <b>បង់ប្រាក់តាម KHQR</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="6019102674832595118">📦</tg-emoji> <b>ទំនិញ៖</b> {cat_name}\n'
        f'<tg-emoji emoji-id="6323398779881924106">⏳</tg-emoji> <b>កញ្ចប់៖</b> {item["name"]} (x{qty})\n'
        f'<tg-emoji emoji-id="5447155993286832278">💵</tg-emoji> <b>ចំនួនត្រូវបង់៖</b> <b>${total:.2f}</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="6201496295077584895">📸</tg-emoji> <b>សូមស្កេន QR ខាងលើដើម្បីបង់ប្រាក់</b>\n'
        f'<tg-emoji emoji-id="6021405408663445899">📩</tg-emoji> បន្ទាប់ពីបង់រួច <b>សូមផ្ញើរូបភាពវិក្កយប័ត្រ (Screenshot)</b> មកទីនេះដើម្បីឱ្យ Admin បញ្ជាក់!'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Cancel / បោះបង់", callback_data="cancel_action", style="danger",
              icon_custom_emoji_id="6030602393933060595")]
    ])

    await state.update_data(item_id=item_id, qty=qty, total=total, item_name=item["name"], cat_name=cat_name)
    await state.set_state(ManualPayFSM.receipt)

    try:
        m_type, m_id = qr_media.split(":", 1)
        if m_type == "photo":
            sent = await bot.send_photo(call.message.chat.id, m_id, caption=cap, reply_markup=kb)
        elif m_type == "document":
            sent = await bot.send_document(call.message.chat.id, m_id, caption=cap, reply_markup=kb)
        else:
            sent = await bot.send_message(call.message.chat.id, cap, reply_markup=kb)
        await state.update_data(qr_msg_id=sent.message_id)
    except Exception as e:
        logger.error(f"send manual QR failed: {e}")
        await state.clear()
        await bot.send_message(call.message.chat.id, "❌ មិនអាចបង្ហាញរូបភាព QR បានទេ!")


@dp.message(ManualPayFSM.receipt)
async def handle_manual_receipt(msg: Message, state: FSMContext):
    if not msg.photo and not msg.document:
        return await msg.answer(
            '<tg-emoji emoji-id="6201496295077584895">📸</tg-emoji> សូមផ្ញើជារូបភាពវិក្កយប័ត្រ (Photo/Screenshot)!',
            reply_markup=cancel_inline()
        )

    data = await state.get_data()
    await state.clear()

    qr_msg_id = data.get("qr_msg_id")
    if qr_msg_id:
        try:
            await bot.delete_message(msg.chat.id, qr_msg_id)
        except Exception:
            pass

    uid = msg.from_user.id
    req_id = f"{uid}_{int(time.time())}"
    _pending_manual_orders[req_id] = {
        "uid": uid,
        "chat_id": msg.chat.id,
        "first_name": msg.from_user.first_name or "User",
        "username": msg.from_user.username or "",
        "item_id": data["item_id"],
        "qty": data["qty"],
        "total": data["total"],
        "item_name": data["item_name"],
        "cat_name": data.get("cat_name", "Product"),
    }

    rk = get_main_reply_kb(uid)
    await msg.answer(
        f'<tg-emoji emoji-id="6201623099692032873">⏳</tg-emoji> <b>វិក្កយប័ត្ររបស់អ្នកត្រូវបានផ្ញើទៅ Admin ហើយ!</b>\n'
        f'<tg-emoji emoji-id="6323531378407252820">💳</tg-emoji> ចំនួន៖ <b>${data["total"]:.2f}</b>\n'
        f'សូមរង់ចាំ Admin ពិនិត្យ និងបញ្ជាក់បន្តិច...',
        reply_markup=rk
    )

    u_disp = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.first_name or str(uid))
    adm_cap = (
        f'<tg-emoji emoji-id="6026337676091726218">🧾</tg-emoji> <b>សំណើបង់ប្រាក់ KHQR ថ្មី!</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="6019102674832595118">👤</tg-emoji> <b>User:</b> {u_disp} (<code>{uid}</code>)\n'
        f'<tg-emoji emoji-id="6323398779881924106">📦</tg-emoji> <b>Product:</b> {data.get("cat_name")} | {data["item_name"]} (x{data["qty"]})\n'
        f'<tg-emoji emoji-id="5447155993286832278">💵</tg-emoji> <b>Amount:</b> <b>${data["total"]:.2f}</b>'
    )
    adm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        IBtn(text="បញ្ជាក់", callback_data=f"mconfirm_{req_id}", style="success",
             icon_custom_emoji_id="6021536113108196448"),
        IBtn(text="បោះបង់", callback_data=f"mreject_{req_id}", style="danger",
             icon_custom_emoji_id="6030602393933060595"),
    ]])

    admins = db_get_admins()
    admin_ids = {int(a["id"]) for a in admins} | {OWNER_ID}
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    for aid in admin_ids:
        try:
            if msg.photo:
                await bot.send_photo(aid, file_id, caption=adm_cap, reply_markup=adm_kb)
            else:
                await bot.send_document(aid, file_id, caption=adm_cap, reply_markup=adm_kb)
        except Exception as e:
            logger.warning(f"notify admin {aid} manual receipt failed: {e}")


@dp.callback_query(F.data.startswith("mconfirm_"))
async def admin_manual_confirm(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Admin only!", show_alert=True)
    req_id = call.data.split("_", 1)[1]
    info = _pending_manual_orders.pop(req_id, None)
    if not info:
        return await quick_answer(call, "⚠️ សំណើនេះត្រូវបានដោះស្រាយរួចហើយ!", show_alert=True)

    await quick_answer(call, "✅ បានបញ្ជាក់ការទូទាត់!", alert=True) if False else await quick_answer(call, "✅ Confirmed!", show_alert=True)
    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n✅ <b>CONFIRMED BY ADMIN</b>",
            reply_markup=None
        )
    except Exception:
        pass

    await deliver_item(
        uid=info["uid"],
        user_snapshot={
            "id": info["uid"],
            "first_name": info["first_name"],
            "username": info["username"]
        },
        chat_id=info["chat_id"],
        item_id=info["item_id"],
        qty=info["qty"],
        msg_id=0,
        total_paid=info["total"],
        item_name=info["item_name"],
        payment_id=f"MQR-{req_id}",
        deduct_bal=False
    )


@dp.callback_query(F.data.startswith("mreject_"))
async def admin_manual_reject(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return await quick_answer(call, "Admin only!", show_alert=True)
    req_id = call.data.split("_", 1)[1]
    info = _pending_manual_orders.pop(req_id, None)
    if not info:
        return await quick_answer(call, "⚠️ សំណើនេះត្រូវបានដោះស្រាយរួចហើយ!", show_alert=True)

    await quick_answer(call, "❌ Rejected!", show_alert=True)
    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n❌ <b>REJECTED BY ADMIN</b>",
            reply_markup=None
        )
    except Exception:
        pass

    try:
        rk = get_main_reply_kb(info["uid"])
        await bot.send_message(
            info["chat_id"],
            '<tg-emoji emoji-id="6030602393933060595">❌</tg-emoji> <b>ការទូទាត់ KHQR របស់អ្នកត្រូវបានបដិសេធដោយ Admin!</b>',
            reply_markup=rk
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("abapay_"))
@auto_answer()
async def handle_aba_pay(call: CallbackQuery):
    uid = call.from_user.id

    if get_setting("maintenance", "0") == "1" and not is_admin_user(uid):
        return await bot.send_message(call.message.chat.id, "Bot Update សូមរងចាំ")

    if get_setting("aba_enabled", "1") != "1":
        return await bot.send_message(call.message.chat.id, "⚠️ ABA Payment ត្រូវបានបិទបណ្ដោះអាសន្ន!")

    parts = call.data.split("_")
    try:
        item_id, qty = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return await bot.send_message(call.message.chat.id, "❌ Invalid request.")

    stock_qty = count_stock(item_id)
    if qty > stock_qty:
        return await bot.send_message(
            call.message.chat.id,
            f"<tg-emoji emoji-id=\"{ICON_STOCK_OUT}\">⚠️</tg-emoji> <b>Stock Out! Please try again later.</b> (មាន {stock_qty})")

    item = get_item(item_id)
    if not item:
        return await bot.send_message(call.message.chat.id, "Item not found.")

    u_price = get_user_item_price(item, uid)
    total = round(u_price * qty, 2)

    await cancel_and_delete_user_qr(uid)

    try:
        await call.message.delete()
    except Exception:
        pass

    try:
        loading_msg = await bot.send_message(
            call.message.chat.id,
            f'<tg-emoji emoji-id="{ICON_CREATING_KHQR}">⏱️</tg-emoji> '
            f'<b>Creating your secure KHQR…</b>',
            reply_markup=ReplyKeyboardRemove()
        )
    except Exception:
        loading_msg = None

    res = await khqr_create_payment(total)

    if not res or not res.get("ok"):
        err = (
            (res or {}).get("msg")
            or (res or {}).get("error_text")
            or (res or {}).get("error")
            or "Unknown error"
        )
        if loading_msg:
            try:
                await loading_msg.delete()
            except Exception:
                pass
        rk = get_main_reply_kb(uid)
        return await bot.send_message(
            call.message.chat.id, f"❌ Failed to create QR!\n<code>{err}</code>",
            reply_markup=rk)

    payment_id   = str(res.get("id"))
    qr_string    = res.get("qr")
    expires_at   = res.get("expires_at")
    aba_deep     = res.get("aba_deeplink")

    expires_ts = _parse_expires_at(expires_at)
    if expires_ts is None or expires_ts <= time.time():
        expires_ts = time.time() + 600

    p_mode = res.get("provider") or get_setting("payment_mode", "aba")
    qr_title = "BAKONG PAY (KHQR)" if p_mode == "bakong" else "ABA PAY WAY (KHQR)"
    cap_template = (
        f"<tg-emoji emoji-id=\"6158997026747983036\">🏦</tg-emoji> <b>{qr_title}</b>\n"
        f"- <tg-emoji emoji-id=\"5197434882321567830\">💵</tg-emoji> ចំនួនត្រូវ pay : <b>${total:.2f}</b>\n"
        f"- <tg-emoji emoji-id=\"5345987742276797245\">📦</tg-emoji> ទំនិញ : <b>{item['name']}</b> x{qty}\n"
        f"- <tg-emoji emoji-id=\"5444856076954520455\">🧾</tg-emoji> Ref : <code>{payment_id}</code>\n"
        f"- <tg-emoji emoji-id=\"5386367538735104399\">⏱️</tg-emoji> <b>QR Expires in :</b> <b>{{COUNTDOWN}}</b>\n"
        f"- <tg-emoji emoji-id=\"6129570565125577536\">⚡️</tg-emoji> ពេលផេររួច ទំនិញនឹងលូតដោយស្វ័យប្រវត្តិ"
    )

    def build_cap(remaining: int) -> str:
        mm, ss = divmod(max(0, remaining), 60)
        return cap_template.replace("{COUNTDOWN}", f"{mm:02d}:{ss:02d}")

    initial_cap = build_cap(int(expires_ts - time.time()))

    kb_arr = []
    if aba_deep:
        kb_arr.append([IBtn(text="Open ABA Mobile", url=aba_deep, style="primary",
                            icon_custom_emoji_id="6316599954026535998")])

    kb_arr.append([IBtn(text="Cancel QR / បោះបង់", callback_data=f"cancelpay_{payment_id}",
                        style="danger", icon_custom_emoji_id="6102914615107133393")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_arr)

    if loading_msg:
        try:
            await loading_msg.delete()
        except Exception:
            pass

    merch_name = str(res.get("merchant_name") or res.get("account_name") or "").strip()
    png_buf = await asyncio.to_thread(render_khqr_png, qr_string, total, merch_name)
    is_photo_msg = bool(png_buf)

    try:
        if png_buf:
            sent_msg = await bot.send_photo(
                call.message.chat.id,
                photo=BufferedInputFile(png_buf.getvalue(), filename="aba_khqr.png"),
                caption=initial_cap, reply_markup=kb)
        else:
            sent_msg = await bot.send_message(
                call.message.chat.id, initial_cap, reply_markup=kb,
                link_preview_options=LinkPreviewOptions(is_disabled=False))
    except Exception as e:
        logger.error(f"send QR failed: {e}")
        rk = get_main_reply_kb(uid)
        return await bot.send_message(call.message.chat.id, "❌ Failed to send QR. Try again.", reply_markup=rk)

    _active_user_qrs[uid] = (call.message.chat.id, sent_msg.message_id, payment_id)

    run_bg_task(_live_qr_countdown(
        chat_id=call.message.chat.id,
        msg_id=sent_msg.message_id,
        payment_id=payment_id,
        expires_ts=expires_ts,
        caption_builder=build_cap,
        reply_markup=kb,
        is_photo=is_photo_msg,
    ))

    run_bg_task(poll_payment(
        uid=uid,
        user_snapshot={"id": uid,
                       "first_name": call.from_user.first_name,
                       "username": call.from_user.username},
        chat_id=call.message.chat.id,
        payment_id=payment_id, item_id=item_id, qty=qty,
        msg_id=sent_msg.message_id, total_paid=total,
        item_name=item["name"]))


async def poll_payment(uid, user_snapshot, chat_id, payment_id, item_id, qty, msg_id,
                       total_paid, item_name):
    """⚡ Adaptive polling: 500ms → 200ms → 100ms (smooth + safe API)"""
    timeout = time.time() + 600
    consecutive_errors = 0
    start_time = time.time()

    while time.time() < timeout:
        elapsed = time.time() - start_time

        if elapsed < 60:
            interval = 0.5
        elif elapsed < 180:
            interval = 0.2
        else:
            interval = 0.1

        if payment_id in cancelled_payments:
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            return

        if payment_id in _processed_payments:
            _active_user_qrs.pop(uid, None)
            return

        status = await khqr_check_payment(payment_id)

        if not status:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                logger.warning(f"Payment {payment_id}: 10 consecutive check failures")
                await asyncio.sleep(min(2.0, interval * 4))
                continue
        else:
            consecutive_errors = 0

        if status in PAID_STATUSES:
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            await deliver_item(uid, user_snapshot, chat_id, item_id, qty, msg_id,
                               total_paid, item_name, payment_id=payment_id)
            return

        if status in FAILED_STATUSES:
            cancelled_payments.discard(payment_id)
            _active_user_qrs.pop(uid, None)
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            rk = get_main_reply_kb(uid)
            if status == "underpaid":
                await bot.send_message(chat_id,
                    "⚠️ Payment received but amount did not match. Please contact admin.",
                    reply_markup=rk)
            elif status in ("cancelled", "canceled"):
                await bot.send_message(
                    chat_id,
                    f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Payment cancelled.</b>",
                    reply_markup=rk
                )
            elif status == "expired":
                await bot.send_message(
                    chat_id,
                    f"<tg-emoji emoji-id=\"{ICON_QR_EXPIRED}\">⌛</tg-emoji> <b>QR payment expired.</b>",
                    reply_markup=rk
                )
            else:
                await bot.send_message(chat_id, f"⌛ Payment {status}.", reply_markup=rk)
            return

        await asyncio.sleep(interval)

    cancelled_payments.discard(payment_id)
    _active_user_qrs.pop(uid, None)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass
    rk = get_main_reply_kb(uid)
    await bot.send_message(
        chat_id,
        f"<tg-emoji emoji-id=\"{ICON_QR_EXPIRED}\">⌛</tg-emoji> <b>QR payment expired.</b>",
        reply_markup=rk
    )


# ====================================================================================================
# [ DELIVERY & KEY / LINK FORMATTING ]
# ====================================================================================================

async def deliver_item(uid, user_snapshot, chat_id, item_id, qty, msg_id,
                       total_paid, item_name, payment_id="", deduct_bal: bool = False):
    if payment_id and not _mark_processed(payment_id):
        logger.warning(f"deliver_item: payment {payment_id} already processed, skipping.")
        return

    _active_user_qrs.pop(uid, None)

    if deduct_bal and get_user_balance(uid) < round(float(total_paid), 2):
        rk = get_main_reply_kb(uid)
        return await bot.send_message(
            chat_id,
            f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>សមតុល្យមិនគ្រប់គ្រាន់!</b>",
            reply_markup=rk
        )

    stocks = reserve_stock(item_id, qty)
    rk = get_main_reply_kb(uid)
    if not stocks:
        return await bot.send_message(
            chat_id,
            f"<tg-emoji emoji-id=\"{ICON_STOCK_OUT}\">⚠️</tg-emoji> <b>Stock Out! Please try again later.</b>",
            reply_markup=rk
        )

    if deduct_bal:
        deduct_user_balance(uid, total_paid)

    file_tasks = []
    key_lines = []
    raw_keys_for_group = []
    extracted_urls: List[str] = []

    for s in stocks:
        if s[2] == 'text':
            k_val = str(s[1]).strip()
            found_urls = re.findall(r'https?://[^\s<>"]+|t\.me/[^\s<>"]+', k_val)
            for u in found_urls:
                u_full = u if u.startswith("http") else f"https://{u}"
                if u_full not in extracted_urls:
                    extracted_urls.append(u_full)

            key_lines.append(f"- <code>{k_val}</code>")
            raw_keys_for_group.append(k_val)
        else:
            raw_keys_for_group.append("[File]")
            m_type, m_id = s[1].split(':', 1)
            coro = None
            if m_type == 'photo':
                coro = bot.send_photo(chat_id, m_id, caption="នេះគឺជាឯកសាររបស់អ្នក!")
            elif m_type == 'video':
                coro = bot.send_video(chat_id, m_id, caption="នេះគឺជាឯកសាររបស់អ្នក!")
            elif m_type == 'document':
                coro = bot.send_document(chat_id, m_id, caption="នេះគឺជាឯកសាររបស់អ្នក!")
            if coro is not None:
                file_tasks.append(coro)

    order_id = record_order(
        uid, item_id, item_name, qty, total_paid,
        payment_id=payment_id, keys=raw_keys_for_group
    )

    if file_tasks:
        results = await asyncio.gather(*file_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"send file failed: {r}")

    item = get_item(item_id)
    post_media_text = ""
    if item and item.get("post_media"):
        try:
            m_type, content = item["post_media"].split(':', 1)
            senders = {
                'photo': bot.send_photo, 'video': bot.send_video,
                'document': bot.send_document, 'audio': bot.send_audio,
                'voice': bot.send_voice,
            }
            if m_type == 'text':
                post_media_text = content.strip()
                found_urls = re.findall(r'https?://[^\s<>"]+|t\.me/[^\s<>"]+', post_media_text)
                for u in found_urls:
                    u_full = u if u.startswith("http") else f"https://{u}"
                    if u_full not in extracted_urls:
                        extracted_urls.append(u_full)
            elif m_type in senders:
                await senders[m_type](chat_id, content)
        except Exception as e:
            logger.warning(f"post_media failed: {e}")

    link_display = ""
    if extracted_urls:
        link_display = "\n".join([f"- 👉 <a href=\"{u}\">{u}</a>" for u in extracted_urls])
    elif post_media_text:
        link_display = f"- {post_media_text}"
    else:
        link_display = "- <i>(គ្មានលីងបន្ថែមទេ)</i>"

    keys_formatted = "\n".join(key_lines) if key_lines else "- <code>[ឯកសារ File ត្រូវបានផ្ញើជូន]</code>"

    msg = (
        f"<tg-emoji emoji-id=\"{ICON_BUY_SUCCESS}\">✅</tg-emoji> <b>ទំនិញបានជោគជ័យ</b>\n\n"
        f"<tg-emoji emoji-id=\"{ICON_KEY_LIST}\">🔑</tg-emoji> <b>បញ្ជី Key របស់អ្នក [ សរុប {len(stocks)} Key ]</b>\n"
        f"{keys_formatted}\n\n"
        f"<tg-emoji emoji-id=\"{ICON_THANK_YOU}\">🙏</tg-emoji> <b>អរគុណសម្រាប់ការទិញទំនិញ</b> <tg-emoji emoji-id=\"{ICON_THANK_YOU}\">🙏</tg-emoji>\n\n"
        f"<tg-emoji emoji-id=\"{ICON_INSTALL_LINK}\">📲</tg-emoji> <b>ចុចលីងខាងក្រោមដើម្បីដំឡើង</b> <tg-emoji emoji-id=\"{ICON_INSTALL_LINK}\">📲</tg-emoji>\n"
        f"{link_display}"
    )

    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

    link_buttons = []
    if extracted_urls:
        for idx, u in enumerate(extracted_urls, 1):
            btn_title = "ចុចទីនេះដើម្បីដំឡើង" if len(extracted_urls) == 1 else f"ចុចទីនេះដើម្បីដំឡើង {idx}"
            link_buttons.append([IBtn(text=btn_title, url=u, icon_custom_emoji_id=ICON_INSTALL_BTN)])

    continue_shopping_text = (
        f"<tg-emoji emoji-id=\"{ICON_BUY_NOW}\">🛒</tg-emoji> "
        f"<b>Please choose BUY NOW to continue shopping</b>"
    )

    try:
        if link_buttons:
            await bot.send_message(
                chat_id,
                msg,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=link_buttons),
                link_preview_options=LinkPreviewOptions(is_disabled=False)
            )
            await bot.send_message(chat_id, continue_shopping_text, reply_markup=rk)
        else:
            await bot.send_message(
                chat_id,
                msg,
                link_preview_options=LinkPreviewOptions(is_disabled=False)
            )
            await bot.send_message(chat_id, continue_shopping_text, reply_markup=rk)
    except Exception as e:
        logger.error(f"deliver_item send failed: {e}")

    run_bg_task(_send_order_notifications(
        uid, user_snapshot, item_name, total_paid, qty, item_id,
        order_id=order_id, raw_keys=raw_keys_for_group))


def _fmt_premium_icon(icon_val) -> str:
    s = str(icon_val or "").strip()
    if _is_premium_id(s):
        return f'<tg-emoji emoji-id="{s}">🎮</tg-emoji> '
    if s:
        return f"{s} "
    return ""


async def _send_order_notifications(uid, user_snapshot, item_name, total_paid, qty, item_id,
                                    order_id: int = 0, raw_keys: Optional[List[str]] = None):
    if user_snapshot.get("username"):
        u_user = f"@{user_snapshot['username']}"
    else:
        u_user = user_snapshot.get("first_name") or f"User_{uid}"

    date_str = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %I:%M:%S %p")

    item = get_item(item_id)
    cat = get_category(item.get("cat_id")) if item else None
    cat_name = cat.get("name", "Product") if cat else "Product"
    cat_icon = _fmt_premium_icon(cat.get("icon") if cat else None)
    plan_icon = _fmt_premium_icon(item.get("icon") if item else None)

    is_vip = is_user_reseller_vip(uid)
    is_res = is_user_reseller(uid)
    res_gp = get_setting("reseller_group_id")
    user_gp = get_setting("order_group_id") or get_setting("channel_id")

    if is_vip:
        role_label = "Reseller VIP"
    elif is_res:
        role_label = "Reseller Normal"
    else:
        role_label = "Customer"

    msg_body = (
        f'<tg-emoji emoji-id="5830344527235263447">✅</tg-emoji> <b>Payment verified!</b>\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>Order ID:</b> <code>VBEN-{order_id}</code>\n'
        f'<tg-emoji emoji-id="5904630315946611415">👤</tg-emoji> <b>User:</b> {u_user}\n'
        f'<tg-emoji emoji-id="5888781182249738113">🆔</tg-emoji> <b>User ID:</b> <code>{uid}</code>\n'
        f'<tg-emoji emoji-id="5298877105000439431">🏷️</tg-emoji> <b>Role:</b> {role_label}\n'
        f'<tg-emoji emoji-id="6235459831302460476">💵</tg-emoji> <b>Price Plan:</b> ${total_paid:.2f}\n'
        f'<tg-emoji emoji-id="5258049708525380488">🎮</tg-emoji> <b>Game:</b> {cat_icon}{cat_name}\n'
        f'<tg-emoji emoji-id="5431897022456145283">⏳</tg-emoji> <b>Duration:</b> {plan_icon}{item_name}\n'
        f'<tg-emoji emoji-id="5413395871008058098">🔑</tg-emoji> <b>Key(s):</b> {qty}\n'
        f'<tg-emoji emoji-id="6242308461598610637">⏰</tg-emoji> <b>Time:</b> {date_str}\n'
        f'<tg-emoji emoji-id="5445195276291693508">✅</tg-emoji> <b>Status:</b> Completed (Auto)'
    )

    if (is_res or is_vip) and res_gp:
        try:
            await bot.send_message(int(res_gp), msg_body)
        except Exception as e:
            logger.warning(f"send reseller group failed: {e}")
    elif user_gp:
        try:
            await bot.send_message(int(user_gp), msg_body)
        except Exception as e:
            logger.warning(f"send user group failed: {e}")

    rem_stock = count_stock(item_id)
    if rem_stock <= 3:
        title_txt = "ស្តុកបានលក់អស់ហើយ!" if rem_stock == 0 else "ស្តុកជិតអស់ហើយ!"
        alert = (
            f'<tg-emoji emoji-id="6201692991694839983">🚨</tg-emoji> • <b>{title_txt}</b>\n'
            f'<tg-emoji emoji-id="5409125874651903587">📦</tg-emoji> • <b>ទំនិញ</b> {cat_name} {plan_icon}{item_name}\n'
            f'<tg-emoji emoji-id="6269366244462301331">📊</tg-emoji> • <b>ស្តុកនៅសល់តែ {rem_stock} ប៉ុណ្ណោះ</b>\n'
            f'<tg-emoji emoji-id="6102563003314479651">👉</tg-emoji> ចុច '
            f'<tg-emoji emoji-id="6284847737548578337">🛒</tg-emoji> <b>BUY NOW</b> ដើម្បីទិញបង្ហើយ'
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[IBtn(
            text="BUY NOW", callback_data=f"sel_item_{item_id}",
            style="primary", icon_custom_emoji_id="5312361253610475399")]])
        await broadcast_users(alert, kb)


async def broadcast_users(text: str, kb: Optional[InlineKeyboardMarkup] = None):
    users = db_all_users()
    if not users:
        return
    for uid_str in users:
        try:
            await bot.send_message(int(uid_str), text, reply_markup=kb)
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            pass
        except Exception:
            pass


@dp.callback_query(F.data == "buy_now_direct")
@auto_answer()
async def buy_now_direct_btn(call: CallbackQuery):
    if get_setting("maintenance", "0") == "1" and not is_admin_user(call.from_user.id):
        return await bot.send_message(call.message.chat.id, "Bot Update សូមរងចាំ")

    await cancel_and_delete_user_qr(call.from_user.id)
    await send_categories(call)

# ====================================================================================================
# [ ADMIN PANEL ]
# ====================================================================================================

@dp.message(F.text == "Admin Panel")
async def open_admin(msg: Message):
    uid = msg.from_user.id
    if not is_admin_user(uid):
        return
    await cancel_and_delete_user_qr(uid)
    await _delete_last_ui_msg(uid)
    try:
        await msg.delete()
    except Exception:
        pass
    sent = await msg.answer(ADMIN_PANEL_TEXT, reply_markup=admin_main_kb())
    _user_last_ui_msg[uid] = (sent.chat.id, sent.message_id)


@dp.callback_query(F.data == "back_admin")
@auto_answer()
async def back_to_admin(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cancel_and_delete_user_qr(call.from_user.id)
    await safe_edit_or_send(call, ADMIN_PANEL_TEXT, admin_main_kb())

# ====================================================================================================
# [ 💳 MANAGER SETTING (USER DEPOSIT / WITHDRAW) ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_manager_setting")
@auto_answer()
async def adm_manager_setting_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    await state.clear()
    text = (
        f"<tg-emoji emoji-id=\"{ICON_MANAGER_SETTING}\">⚙️</tg-emoji> <b>Manager Setting</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"សូមជ្រើសរើសមុខងារខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, manager_setting_kb())


@dp.callback_query(F.data == "adm_user_deposit")
@auto_answer()
async def adm_user_deposit_start(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_MANAGER_SETTING}\">⚙️</tg-emoji> "
        f"+ Please go and get the ID of the person you want to deposit money to, e.g. 12345678"
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminDepositFSM.user_id)


@dp.message(AdminDepositFSM.user_id)
async def adm_user_deposit_get_id(msg: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    try:
        target_id = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ Invalid User ID! e.g. 12345678", reply_markup=kb)

    curr_bal = get_user_balance(target_id)
    await state.update_data(target_id=target_id)
    await msg.answer(
        f"<tg-emoji emoji-id=\"{ICON_TOPUP}\">💵</tg-emoji> User ID: <code>{target_id}</code>\n"
        f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${curr_bal:.2f}</b>\n\n"
        f"<tg-emoji emoji-id=\"{ICON_AMT_PROMPT}\">🔢</tg-emoji> Please enter deposit amount (e.g. 10 or 5.50):",
        reply_markup=kb
    )
    await state.set_state(AdminDepositFSM.amount)


@dp.message(AdminDepositFSM.amount)
async def adm_user_deposit_save(msg: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    raw = (msg.text or "").strip().replace("$", "").replace(",", "")
    try:
        amt = round(float(raw), 2)
        if amt <= 0:
            raise ValueError
    except Exception:
        return await msg.answer("❌ Invalid amount! e.g. 10 or 5.50", reply_markup=kb)

    data = await state.get_data()
    target_id = data["target_id"]
    await state.clear()
    new_bal = add_user_balance(target_id, amt)

    await msg.answer(
        f"<tg-emoji emoji-id=\"{ICON_BUY_SUCCESS}\">✅</tg-emoji> <b>Deposit Successful!</b>\n\n"
        f"User ID: <code>{target_id}</code>\n"
        f"Deposited: <b>+${amt:.2f}</b>\n"
        f"New Balance: <b>${new_bal:.2f}</b>",
        reply_markup=manager_setting_kb()
    )
    try:
        await bot.send_message(
            target_id,
            f"<tg-emoji emoji-id=\"{ICON_BUY_SUCCESS}\">✅</tg-emoji> <b>Admin deposited +${amt:.2f} to your account!</b>\n"
            f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${new_bal:.2f}</b>"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "adm_user_withdraw")
@auto_answer()
async def adm_user_withdraw_start(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_MANAGER_SETTING}\">⚙️</tg-emoji> "
        f"Please go and get the ID of the person you want to withdraw money from, e.g. 12345678"
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminWithdrawFSM.user_id)


@dp.message(AdminWithdrawFSM.user_id)
async def adm_user_withdraw_get_id(msg: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    try:
        target_id = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ Invalid User ID! e.g. 12345678", reply_markup=kb)

    curr_bal = get_user_balance(target_id)
    await state.update_data(target_id=target_id)
    await msg.answer(
        f"<tg-emoji emoji-id=\"{ICON_TOPUP}\">💵</tg-emoji> User ID: <code>{target_id}</code>\n"
        f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${curr_bal:.2f}</b>\n\n"
        f"<tg-emoji emoji-id=\"{ICON_AMT_PROMPT}\">🔢</tg-emoji> Please enter withdraw amount (e.g. 10 or 5.50):",
        reply_markup=kb
    )
    await state.set_state(AdminWithdrawFSM.amount)


@dp.message(AdminWithdrawFSM.amount)
async def adm_user_withdraw_save(msg: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    raw = (msg.text or "").strip().replace("$", "").replace(",", "")
    try:
        amt = round(float(raw), 2)
        if amt <= 0:
            raise ValueError
    except Exception:
        return await msg.answer("❌ Invalid amount! e.g. 10 or 5.50", reply_markup=kb)

    data = await state.get_data()
    target_id = data["target_id"]
    ok, new_bal = deduct_user_balance(target_id, amt)
    if not ok:
        return await msg.answer(
            f"❌ Insufficient user balance! Current balance: <b>${new_bal:.2f}</b>",
            reply_markup=kb
        )

    await state.clear()
    await msg.answer(
        f"<tg-emoji emoji-id=\"{ICON_BUY_SUCCESS}\">✅</tg-emoji> <b>Withdraw Successful!</b>\n\n"
        f"User ID: <code>{target_id}</code>\n"
        f"Withdrawn: <b>-${amt:.2f}</b>\n"
        f"New Balance: <b>${new_bal:.2f}</b>",
        reply_markup=manager_setting_kb()
    )
    try:
        await bot.send_message(
            target_id,
            f"<tg-emoji emoji-id=\"{ICON_PAY_CANCELLED}\">❌</tg-emoji> <b>Admin withdrew -${amt:.2f} from your account.</b>\n"
            f"<tg-emoji emoji-id=\"{ICON_CURRENT_BAL}\">💰</tg-emoji> Current balance: <b>${new_bal:.2f}</b>"
        )
    except Exception:
        pass

@dp.callback_query(F.data == "adm_edit_contact_admin")
@auto_answer()
async def adm_edit_contact_admin_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    curr = get_setting("contact_admin", "@Vbenstore")
    text = (
        f'<tg-emoji emoji-id="5258215635996908355">✏️</tg-emoji> '
        f'Please enter your new username here. You can enter it without having to provide a link. '
        f'For example. Your @myusername៖\n\n'
        f'<i>Current:</i> <code>{curr}</code>'
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminContactFSM.username)


@dp.message(AdminContactFSM.username)
async def adm_edit_contact_admin_save(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    uname = raw.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").strip().lstrip("@").strip("/")
    if not uname or " " in uname:
        return await msg.answer("❌ Invalid username or ID! Example: <code>@Vbenstore</code> or <code>12345678</code>")
    resolved_id = ""
    if uname.isdigit():
        resolved_id = uname
        norm = uname
    else:
        norm = f"@{uname}"
        for u_id_str, u_obj in _load_db().get("users", {}).items():
            if str(u_obj.get("username", "")).strip().lstrip("@").lower() == uname.lower():
                resolved_id = u_id_str
                break
        if not resolved_id:
            try:
                chat_info = await bot.get_chat(norm)
                if chat_info and chat_info.id:
                    resolved_id = str(chat_info.id)
            except Exception:
                pass
    db_set_setting("contact_admin", norm)
    db_set_setting("contact_admin_id", resolved_id)
    await state.clear()
    await msg.answer(f"✅ Contact Admin updated to: <b>{norm}</b>", reply_markup=manager_setting_kb())


def payment_aba_kb():
    p_mode = get_setting("payment_mode", "aba")
    rows = [
        [
            IBtn(text="Open ABA", callback_data="adm_aba_open", style="success",
                 icon_custom_emoji_id="6197110630957196320"),
            IBtn(text="Close ABA", callback_data="adm_aba_close", style="danger",
                 icon_custom_emoji_id="6197110630957196320"),
        ],
    ]
    if p_mode == "bakong":
        rows.append([
            IBtn(text="Chenge API", callback_data="adm_aba_change_api", style="primary",
                 icon_custom_emoji_id="6194804005885979475"),
            IBtn(text="Switch to ABA", callback_data="adm_switch_aba", style="primary",
                 icon_custom_emoji_id=ICON_SWITCH_BAKONG),
        ])
    elif p_mode == "manual_qr":
        rows.append([
            IBtn(text="Switch to ABA", callback_data="adm_switch_aba", style="primary",
                 icon_custom_emoji_id=ICON_SWITCH_BAKONG),
            IBtn(text="Switch to Bakong", callback_data="adm_switch_bakong", style="primary",
                 icon_custom_emoji_id=ICON_SWITCH_BAKONG),
        ])
    else:
        rows.append([
            IBtn(text="Chenge API", callback_data="adm_aba_change_api", style="primary",
                 icon_custom_emoji_id="6194804005885979475"),
            IBtn(text="Switch to Bakong", callback_data="adm_switch_bakong", style="primary",
                 icon_custom_emoji_id=ICON_SWITCH_BAKONG),
        ])

    rows.append([
        IBtn(text="Switch to QR code", callback_data="adm_switch_manual_qr", style="primary",
             icon_custom_emoji_id=ICON_MANUAL_KHQR),
        IBtn(text="Set QR Photo", callback_data="adm_set_manual_qr", style="success",
             icon_custom_emoji_id=ICON_SET_START_MEDIA),
    ])
    rows.append([
        IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
             icon_custom_emoji_id="6195202819369215906"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_aba_text() -> str:
    is_open = get_setting("aba_enabled", "1") == "1"
    status_txt = "OPEN (បើក)" if is_open else "CLOSED (បិទ)"
    p_mode = get_setting("payment_mode", "aba")
    if p_mode == "bakong":
        mode_label = "Bakong (API)"
        api_label = "API Key Bakong"
        api_val = get_setting("bakong_api_key", "") or get_setting("khqr_api_key", KHQR_API_KEY) or KHQR_API_KEY
        masked = f"{api_val[:12]}...{api_val[-6:]}" if len(api_val) > 18 else api_val
        extra_line = f'<tg-emoji emoji-id="6195019956841620797">🔑</tg-emoji> <b>{api_label}:</b> <code>{masked}</code>'
    elif p_mode == "manual_qr":
        mode_label = "QR Code (Manual)"
        has_qr = "រួចរាល់ ✅" if get_setting("manual_qr_media", "") else "មិនទាន់មាន ❌"
        extra_line = f'<tg-emoji emoji-id="6026337676091726218">🖼️</tg-emoji> <b>QR Image:</b> {has_qr}'
    else:
        mode_label = "ABA (API)"
        api_val = get_setting("khqr_api_key", KHQR_API_KEY) or KHQR_API_KEY
        masked = f"{api_val[:12]}...{api_val[-6:]}" if len(api_val) > 18 else api_val
        extra_line = f'<tg-emoji emoji-id="6195019956841620797">🔑</tg-emoji> <b>API Key ABA:</b> <code>{masked}</code>'

    return (
        f'<tg-emoji emoji-id="6021741116192201252">🏦</tg-emoji> <b>Payment ABA / KHQR</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="5447602197439218445">🔄</tg-emoji> <b>Mode:</b> <b>{mode_label}</b>\n'
        f'{extra_line}\n'
        f'📊 <b>Status:</b> <b>{status_txt}</b>'
    )


@dp.callback_query(F.data == "adm_payment_aba")
@auto_answer()
async def adm_payment_aba_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    await state.clear()
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_aba_open")
@auto_answer("✅ ABA Payment ត្រូវបានបើក!", alert=True)
async def adm_aba_open_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("aba_enabled", "1")
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_aba_close")
@auto_answer("🔴 ABA Payment ត្រូវបានបិទ!", alert=True)
async def adm_aba_close_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("aba_enabled", "0")
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_switch_aba")
@auto_answer("✅ បានប្ដូរទៅ ABA (API)!", alert=True)
async def adm_switch_aba_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("payment_mode", "aba")
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_switch_bakong")
@auto_answer("✅ បានប្ដូរទៅ Bakong (API)!", alert=True)
async def adm_switch_bakong_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("payment_mode", "bakong")
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_switch_manual_qr")
@auto_answer("✅ បានប្ដូរទៅ QR Code (Manual)!", alert=True)
async def adm_switch_manual_qr_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("payment_mode", "manual_qr")
    await safe_edit_or_send(call, payment_aba_text(), payment_aba_kb())


@dp.callback_query(F.data == "adm_set_manual_qr")
@auto_answer()
async def adm_set_manual_qr_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_payment_aba", style="primary",
              icon_custom_emoji_id="6195202819369215906")]
    ])
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="6026337676091726218">🖼️</tg-emoji> <b>Set QR Photo</b>\n\nសូមផ្ញើរូបភាព <b>QR Code</b> របស់អ្នក៖',
        kb
    )
    await state.set_state(AdminManualQRFSM.photo)


@dp.message(AdminManualQRFSM.photo)
async def adm_set_manual_qr_save(msg: Message, state: FSMContext):
    if msg.photo:
        val = f"photo:{msg.photo[-1].file_id}"
    elif msg.document:
        val = f"document:{msg.document.file_id}"
    else:
        return await msg.answer("❌ សូមផ្ញើជារូបភាព (Photo)!")
    db_set_setting("manual_qr_media", val)
    await state.clear()
    await msg.answer("✅ បានរក្សាទុករូបភាព QR Code ដោយជោគជ័យ!", reply_markup=payment_aba_kb())


@dp.callback_query(F.data == "adm_aba_change_api")
@auto_answer()
async def adm_aba_change_api_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    p_mode = get_setting("payment_mode", "aba")
    api_name = "API Key Bakong" if p_mode == "bakong" else "API Key ABA"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_payment_aba", style="primary",
              icon_custom_emoji_id="6195202819369215906")]
    ])
    await safe_edit_or_send(
        call,
        f'<tg-emoji emoji-id="6194804005885979475">🔑</tg-emoji> <b>Chenge API</b>\n\nសូមផ្ញើ <b>{api_name}</b> ថ្មីរបស់អ្នក៖',
        kb
    )
    await state.set_state(AdminABAApiFSM.api_key)


@dp.message(AdminABAApiFSM.api_key)
async def adm_aba_change_api_save(msg: Message, state: FSMContext):
    new_key = (msg.text or "").strip()
    if not new_key or len(new_key) < 8:
        return await msg.answer("❌ API Key មិនត្រឹមត្រូវទេ!")
    p_mode = get_setting("payment_mode", "aba")
    setting_key = "bakong_api_key" if p_mode == "bakong" else "khqr_api_key"
    api_name = "Bakong" if p_mode == "bakong" else "ABA"
    db_set_setting(setting_key, new_key)
    await state.clear()
    await msg.answer(f"✅ បានប្ដូរ API Key {api_name} ដោយជោគជ័យ!", reply_markup=payment_aba_kb())


# ====================================================================================================
# [ 📝 BOT CONTENT SETTING ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_bot_content")
@auto_answer()
async def adm_bot_content_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    await state.clear()
    text = (
        f"<tg-emoji emoji-id=\"{ICON_BOT_CONTENT}\">📝</tg-emoji> <b>Bot Content Setting</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"សូមជ្រើសរើសមុខងារកែសម្រួលខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, bot_content_kb())


@dp.callback_query(F.data == "adm_edit_start_msg")
@auto_answer()
async def adm_edit_start_msg_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_bot_content", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_CONTENT_EDIT}\">✏️</tg-emoji> <b>Edit Notification message (start)</b>\n\n"
        f"សូមផ្ញើអក្សរថ្មីសម្រាប់ /start (អាចប្រើ <code>{{name}}</code> និង <code>{{bot_name}}</code>)\n"
        f"ឬវាយ <code>default</code> ដើម្បីត្រឡប់ទៅអក្សរដើមវិញ៖"
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminStartMessage.text)


@dp.message(AdminStartMessage.text)
async def adm_edit_start_msg_save(msg: Message, state: FSMContext):
    val = (msg.html_text or msg.text or "").strip()
    if not val:
        return await msg.answer("❌ Please send text message.")
    if val.lower() == "default":
        db_set_setting("start_message", "")
        await state.clear()
        return await msg.answer("✅ Start message reset to default!", reply_markup=bot_content_kb())
    db_set_setting("start_message", val)
    await state.clear()
    await msg.answer("✅ Start message updated successfully!", reply_markup=bot_content_kb())


@dp.callback_query(F.data == "adm_set_howtobuy")
@auto_answer()
async def adm_set_howtobuy_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_bot_content", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_CONTENT_EDIT}\">✏️</tg-emoji> <b>Video settings (how to buy)</b>\n\n"
        f"សូមផ្ញើ Video / Photo / File / GIF ឬ Text សម្រាប់បូតុង <b>How to buy</b>៖"
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminHowToBuyMedia.media)


@dp.message(AdminHowToBuyMedia.media)
async def adm_set_howtobuy_save(msg: Message, state: FSMContext):
    if msg.video:
        val = f"video:{msg.video.file_id}"
    elif msg.photo:
        val = f"photo:{msg.photo[-1].file_id}"
    elif msg.animation:
        val = f"animation:{msg.animation.file_id}"
    elif msg.document:
        val = f"document:{msg.document.file_id}"
    elif msg.text:
        val = f"text:{msg.html_text or msg.text}"
    else:
        return await msg.answer("❌ Unsupported format! Send Video, Photo, GIF, File, or Text.")
    db_set_setting("how_to_buy_media", val)
    await state.clear()
    await msg.answer("✅ Saved How to buy content!", reply_markup=bot_content_kb())


@dp.callback_query(F.data == "adm_del_howtobuy")
@auto_answer("✅ បានដកមាតិកា How to buy ចេញរួចរាល់!", alert=True)
async def adm_del_howtobuy_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    db_set_setting("how_to_buy_media", "")
    text = (
        f"<tg-emoji emoji-id=\"{ICON_BOT_CONTENT}\">📝</tg-emoji> <b>Bot Content Setting</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"សូមជ្រើសរើសមុខងារកែសម្រួលខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, bot_content_kb())


@dp.callback_query(F.data == "adm_del_start_media")
@auto_answer("✅ បានដក Start Media ចេញរួចរាល់!", alert=True)
async def adm_del_start_media_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    data = _load_db()
    data["settings"].pop("start_media", None)
    _save_db(data)
    text = (
        f"<tg-emoji emoji-id=\"{ICON_BOT_CONTENT}\">📝</tg-emoji> <b>Bot Content Setting</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"សូមជ្រើសរើសមុខងារកែសម្រួលខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, bot_content_kb())

# ====================================================================================================
# [ ⚙️ SETUP GROUP SYSTEM ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_setup_group")
@auto_answer()
async def adm_setup_group_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    res_gp = get_setting("reseller_group_id")
    usr_gp = get_setting("order_group_id")
    res_disp = f"<code>{res_gp}</code>" if res_gp else "<i>(មិនទាន់កំណត់)</i>"
    usr_disp = f"<code>{usr_gp}</code>" if usr_gp else "<i>(មិនទាន់កំណត់)</i>"
    text = (
        f"<tg-emoji emoji-id=\"{ICON_SETUP_GROUP}\">⚙️</tg-emoji> <b>SETUP GROUP NOTIFICATION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Order Resellers:</b> {res_disp}\n"
        f"📬 <b>Orders User:</b> {usr_disp}\n\n"
        f"សូមជ្រើសរើសជម្រើសខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, setup_group_kb())


@dp.callback_query(F.data == "adm_add_group")
@auto_answer()
async def adm_add_group_choose(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="📦 Order Resellers", callback_data="adm_add_gp_reseller", style="primary"),
            IBtn(text="📬 Orders User", callback_data="adm_add_gp_user", style="success"),
        ],
        [IBtn(text="Back", callback_data="adm_setup_group", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_ADD_GROUP}\">➕</tg-emoji> <b>Add Group</b>\n\n"
        f"សូមជ្រើសរើសប្រភេទ Group ដែលអ្នកចង់កំណត់៖"
    )
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data.in_({"adm_add_gp_reseller", "adm_add_gp_user"}))
@auto_answer()
async def adm_add_group_prompt(call: CallbackQuery, state: FSMContext):
    gtype = "reseller" if call.data == "adm_add_gp_reseller" else "user"
    label = "📦 Order Resellers" if gtype == "reseller" else "📬 Orders User"
    await state.update_data(group_type=gtype)
    text = (
        f"<tg-emoji emoji-id=\"{ICON_ADD_GROUP}\">➕</tg-emoji> <b>Add Group ({label}):</b>\n\n"
        f"👉 សូមផ្ញើ <b>Group ID</b> ឬ <b>Channel ID</b> (ឧ. <code>-1002345678901</code>)៖"
    )
    await safe_edit_or_send(call, text, cancel_inline())
    await state.set_state(AdminGroup.add_id)


@dp.message(AdminGroup.add_id)
async def adm_add_group_save(msg: Message, state: FSMContext):
    val = (msg.text or "").strip()

    if not val.startswith("-100"):
        if val.startswith("-"):
            val = f"-100{val[1:]}"
        else:
            val = f"-100{val}"

    try:
        gid = int(val)
        data = await state.get_data()
        gtype = data.get("group_type", "user")
        setting_key = "reseller_group_id" if gtype == "reseller" else "order_group_id"
        label = "📦 Order Resellers" if gtype == "reseller" else "📬 Orders User"
        db_set_setting(setting_key, str(gid))
        await state.clear()
        await msg.answer(
            f"✅ បានកំណត់ Group ID (<b>{label}</b>): <code>{gid}</code> ដោយជោគជ័យ!",
            reply_markup=setup_group_kb()
        )
    except ValueError:
        await msg.answer("❌ Invalid Group ID! សូមបញ្ចូលលេខសម្គាល់ Group ជាលេខត្រឹមត្រូវ (ឧ. -100xxxxxxxxxx)៖", reply_markup=cancel_inline())


@dp.callback_query(F.data == "adm_del_group")
@auto_answer()
async def adm_del_group_choose(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="📦 Del Order Resellers", callback_data="adm_del_gp_reseller", style="danger"),
            IBtn(text="📬 Del Orders User", callback_data="adm_del_gp_user", style="danger"),
        ],
        [IBtn(text="Back", callback_data="adm_setup_group", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    text = (
        f"<tg-emoji emoji-id=\"{ICON_DEL_GROUP}\">🗑️</tg-emoji> <b>Delete Group</b>\n\n"
        f"សូមជ្រើសរើស Group ដែលអ្នកចង់លុបចេញ៖"
    )
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data.in_({"adm_del_gp_reseller", "adm_del_gp_user"}))
@auto_answer("✅ បានលុប Group ID រួចរាល់!", alert=True)
async def adm_del_group_handler(call: CallbackQuery):
    if call.data == "adm_del_gp_reseller":
        db_set_setting("reseller_group_id", "")
    else:
        db_set_setting("order_group_id", "")
    await adm_setup_group_menu(call, FSMContext) if False else None
    res_gp = get_setting("reseller_group_id")
    usr_gp = get_setting("order_group_id")
    res_disp = f"<code>{res_gp}</code>" if res_gp else "<i>(មិនទាន់កំណត់)</i>"
    usr_disp = f"<code>{usr_gp}</code>" if usr_gp else "<i>(មិនទាន់កំណត់)</i>"
    text = (
        f"<tg-emoji emoji-id=\"{ICON_SETUP_GROUP}\">⚙️</tg-emoji> <b>SETUP GROUP NOTIFICATION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Order Resellers:</b> {res_disp}\n"
        f"📬 <b>Orders User:</b> {usr_disp}\n\n"
        f"សូមជ្រើសរើសជម្រើសខាងក្រោម៖"
    )
    await safe_edit_or_send(call, text, setup_group_kb())


@dp.callback_query(F.data == "adm_view_group")
@auto_answer()
async def adm_view_group_handler(call: CallbackQuery):
    res_gp = get_setting("reseller_group_id")
    usr_gp = get_setting("order_group_id")
    res_disp = f"<code>{res_gp}</code>" if res_gp else "<i>(មិនទាន់មាន Group ID នៅឡើយទេ)</i>"
    usr_disp = f"<code>{usr_gp}</code>" if usr_gp else "<i>(មិនទាន់មាន Group ID នៅឡើយទេ)</i>"
    text = (
        f"<tg-emoji emoji-id=\"{ICON_VIEW_GROUP}\">🆔</tg-emoji> <b>CURRENT GROUP IDs</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Order Resellers:</b> {res_disp}\n"
        f"📬 <b>Orders User:</b> {usr_disp}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_setup_group", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(call, text, kb)

# ----- VIEW STOCK -----

@dp.callback_query(F.data == "adm_view_stock")
@auto_answer()
async def view_stock_cmd(call: CallbackQuery):
    cats = get_categories()
    if not cats:
        return await bot.send_message(call.message.chat.id, "No products yet!")

    kb_rows = []
    for cat in cats:
        for i in get_items(cat[0]):
            cnt = count_stock(i["id"])
            p_normal = float(i.get("price", 0.0))
            p_res = float(i.get("reseller_price", p_normal))
            kb_rows.append([IBtn(
                text=f"{cat[1]} | {i['name']} (${p_normal:.2f}/R:${p_res:.2f}) : {cnt}",
                callback_data=f"viewkey_{i['id']}",
                style="danger",
                icon_custom_emoji_id=ICON_ADD_PLAN)])

    kb_rows.append([IBtn(text="Back", callback_data="back_admin",
                         style="primary", icon_custom_emoji_id=ICON_BACK)])

    text = "📊 <b>VIEW STOCK</b>\n━━━━━━━━━━━━━━━━━━━\n<i>Tap a plan below to see its keys</i>"
    mk = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await safe_edit_or_send(call, text, mk)

# ----- MANAGE PRODUCTS -----

@dp.callback_query(F.data == "adm_manage_products")
@auto_answer()
async def adm_products(call: CallbackQuery):
    await safe_edit_or_send(call, MANAGE_PRODUCTS_TEXT, manage_products_kb())


def extract_emoji(msg: Message):
    if msg.entities:
        for ent in msg.entities:
            if ent.type == "custom_emoji":
                return ent.custom_emoji_id
    text = (msg.text or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 5:
        return text
    if len(text) <= 20:
        return text
    return None


@dp.callback_query(F.data == "adm_add_cat")
@auto_answer()
async def adm_add_cat_1(call: CallbackQuery, state: FSMContext):
    await safe_edit_or_send(call, "📝 Enter Product Name (Category):", cancel_inline())
    await state.set_state(AdminAddCat.name)


@dp.message(AdminAddCat.name)
async def adm_add_cat_2(msg: Message, state: FSMContext):
    if not msg.text or not msg.text.strip():
        return await msg.answer("❌ Please send text name.", reply_markup=cancel_inline())
    await state.update_data(name=msg.text.strip())
    await msg.answer("✨ Send Premium Emoji, Regular Emoji, or ID for this product:",
                     reply_markup=cancel_inline())
    await state.set_state(AdminAddCat.icon)


@dp.message(AdminAddCat.icon)
async def adm_add_cat_3(msg: Message, state: FSMContext):
    icon_id = extract_emoji(msg)
    if not icon_id:
        return await msg.answer(
            "❌ Invalid! Send a Premium Emoji, Regular Emoji, or its ID number (max 20 chars):",
            reply_markup=cancel_inline())
    data = await state.get_data()
    add_category(data['name'], icon_id)
    await msg.answer("✅ Product created successfully!")
    await state.clear()


@dp.callback_query(F.data == "adm_del_cat")
@auto_answer()
async def adm_del_cat(call: CallbackQuery):
    cats = get_categories()
    kb = [[IBtn(text=c[1], callback_data=f"docdelcat_{c[0]}", style="primary")] for c in cats]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select product to delete:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("docdelcat_"))
@auto_answer("✅ Deleted!", alert=True)
async def doc_del_cat(call: CallbackQuery):
    try:
        del_category(int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, MANAGE_PRODUCTS_TEXT,
                           reply_markup=manage_products_kb())


@dp.callback_query(F.data == "adm_add_item")
@auto_answer()
async def adm_add_item_1(call: CallbackQuery, state: FSMContext):
    cats = get_categories()
    if not cats:
        return await bot.send_message(call.message.chat.id, "Create a Product first!")
    kb = [[IBtn(text=c[1], callback_data=f"setcat_{c[0]}", style="primary")] for c in cats]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "📂 Select Product:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("setcat_"))
@auto_answer()
async def adm_add_item_2(call: CallbackQuery, state: FSMContext):
    try:
        await state.update_data(cat_id=int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        return
    await safe_edit_or_send(call, "📝 Enter Plan Name:", cancel_inline())
    await state.set_state(AdminAddItem.name)


@dp.message(AdminAddItem.name)
async def adm_add_item_3(msg: Message, state: FSMContext):
    if not msg.text or not msg.text.strip():
        return await msg.answer("❌ Please send text name.", reply_markup=cancel_inline())
    await state.update_data(name=msg.text.strip())
    await msg.answer("💵 Enter regular price (e.g. 5.5):", reply_markup=cancel_inline())
    await state.set_state(AdminAddItem.price)


@dp.message(AdminAddItem.price)
async def adm_add_item_4(msg: Message, state: FSMContext):
    try:
        await state.update_data(price=float((msg.text or "").strip().replace("$", "")))
        await msg.answer("🏷 Enter Reseller price (e.g. 4.0):", reply_markup=cancel_inline())
        await state.set_state(AdminAddItem.reseller_price)
    except Exception:
        await msg.answer("❌ Invalid number! Enter a valid price (e.g. 5.5):", reply_markup=cancel_inline())


@dp.message(AdminAddItem.reseller_price)
async def adm_add_item_5(msg: Message, state: FSMContext):
    try:
        await state.update_data(reseller_price=float((msg.text or "").strip().replace("$", "")))
        await msg.answer("👑 Enter Reseller VIP price (e.g. 3.0):", reply_markup=cancel_inline())
        await state.set_state(AdminAddItem.reseller_vip_price)
    except Exception:
        await msg.answer("❌ Invalid number! Enter a valid reseller price (e.g. 4.0):", reply_markup=cancel_inline())


@dp.message(AdminAddItem.reseller_vip_price)
async def adm_add_item_vip(msg: Message, state: FSMContext):
    try:
        await state.update_data(reseller_vip_price=float((msg.text or "").strip().replace("$", "")))
        await msg.answer("✨ Send Premium Emoji, Regular Emoji, or ID for this Plan:",
                         reply_markup=cancel_inline())
        await state.set_state(AdminAddItem.icon)
    except Exception:
        await msg.answer("❌ Invalid number! Enter a valid Reseller VIP price (e.g. 3.0):", reply_markup=cancel_inline())


@dp.message(AdminAddItem.icon)
async def adm_add_item_6(msg: Message, state: FSMContext):
    icon_id = extract_emoji(msg)
    if not icon_id:
        return await msg.answer(
            "❌ Invalid! Send a Premium Emoji, Regular Emoji, or its ID number (max 20 chars):",
            reply_markup=cancel_inline())
    d = await state.get_data()
    add_item(d['cat_id'], d['name'], d['price'], d['reseller_price'], icon_id, reseller_vip_price=d.get('reseller_vip_price'))
    await msg.answer("✅ Plan added successfully!")
    await state.clear()

# ====================================================================================================
# [ EDIT PRICE PLAN FLOW ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_edit_item_price")
@auto_answer()
async def adm_edit_price_pick(call: CallbackQuery):
    items = get_all_items()
    if not items:
        return await bot.send_message(call.message.chat.id, "No plans yet!")
    kb = []
    for i in items:
        p_norm = float(i.get("price", 0.0))
        p_res = float(i.get("reseller_price", p_norm))
        kb.append([IBtn(text=f"{i['name']} (${p_norm:.2f} / R:${p_res:.2f})",
                        callback_data=f"edtp_{i['id']}", style="primary",
                        icon_custom_emoji_id=ICON_ADD_PLAN)])
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "✏️ <b>ជ្រើសរើស Plan ដែលអ្នកចង់កែប្រែតម្លៃ៖</b>",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("edtp_"))
@auto_answer()
async def adm_edit_price_enter_norm(call: CallbackQuery, state: FSMContext):
    try:
        item_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    item = get_item(item_id)
    if not item:
        return
    await state.update_data(item_id=item_id)
    p_norm = float(item.get("price", 0.0))
    p_res = float(item.get("reseller_price", p_norm))
    text = (
        f"📌 Plan: <b>{item['name']}</b>\n"
        f"💵 តម្លៃធម្មតាបច្ចុប្បន្ន: <b>${p_norm:.2f}</b>\n"
        f"🏷 តម្លៃ Reseller បច្ចុប្បន្ន: <b>${p_res:.2f}</b>\n\n"
        f"👉 <b>សូមបញ្ចូលតម្លៃធម្មតាថ្មី ($) (ឧ. 5.5):</b>"
    )
    await safe_edit_or_send(call, text, cancel_inline())
    await state.set_state(AdminEditItemPrice.price)


@dp.message(AdminEditItemPrice.price)
async def adm_edit_price_enter_res(msg: Message, state: FSMContext):
    try:
        val = float((msg.text or "").strip().replace("$", ""))
        if val < 0:
            raise ValueError
        await state.update_data(price=val)
        await msg.answer("🏷 <b>សូមបញ្ចូលតម្លៃ Reseller ថ្មី ($) (ឧ. 4.0):</b>", reply_markup=cancel_inline())
        await state.set_state(AdminEditItemPrice.reseller_price)
    except Exception:
        await msg.answer("❌ សូមបញ្ចូលតម្លៃជាលេខត្រឹមត្រូវ (ឧ. 5.5):", reply_markup=cancel_inline())


@dp.message(AdminEditItemPrice.reseller_price)
async def adm_edit_price_save(msg: Message, state: FSMContext):
    try:
        res_val = float((msg.text or "").strip().replace("$", ""))
        if res_val < 0:
            raise ValueError
        data = await state.get_data()
        item_id = data["item_id"]
        norm_val = data["price"]
        update_item_prices(item_id, norm_val, res_val)
        item = get_item(item_id)
        plan_name = item["name"] if item else f"ID {item_id}"
        await state.clear()
        await msg.answer(
            f"✅ <b>កែប្រែតម្លៃជោគជ័យ!</b>\n\n"
            f"📌 Plan: <b>{plan_name}</b>\n"
            f"💵 តម្លៃធម្មតាថ្មី: <b>${norm_val:.2f}</b>\n"
            f"🏷 តម្លៃ Reseller ថ្មី: <b>${res_val:.2f}</b>"
        )
    except Exception:
        await msg.answer("❌ សូមបញ្ចូលតម្លៃជាលេខត្រឹមត្រូវ (ឧ. 4.0):", reply_markup=cancel_inline())


@dp.callback_query(F.data == "adm_del_item")
@auto_answer()
async def adm_del_item(call: CallbackQuery):
    items = get_all_items()
    kb = [[IBtn(text=i["name"], callback_data=f"docdelitem_{i['id']}", style="primary")] for i in items]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select plan to delete:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("docdelitem_"))
@auto_answer("✅ Deleted!", alert=True)
async def doc_del_item(call: CallbackQuery):
    try:
        del_item(int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, MANAGE_PRODUCTS_TEXT,
                           reply_markup=manage_products_kb())


@dp.callback_query(F.data == "adm_add_cat_media")
@auto_answer()
async def set_cat_media(call: CallbackQuery):
    cats = get_categories()
    kb = [[IBtn(text=c[1], callback_data=f"setcatmd_{c[0]}", style="primary")] for c in cats]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select Product to add Picture/Video:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("setcatmd_"))
@auto_answer()
async def set_cat_media2(call: CallbackQuery, state: FSMContext):
    try:
        await state.update_data(cat_id=int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        return
    await safe_edit_or_send(call, "Send a Picture or Video:", cancel_inline())
    await state.set_state(AdminCatMedia.media)


@dp.message(AdminCatMedia.media)
async def set_cat_media3(msg: Message, state: FSMContext):
    cat_id = (await state.get_data()).get('cat_id')
    if msg.photo:
        val = f"photo:{msg.photo[-1].file_id}"
    elif msg.video:
        val = f"video:{msg.video.file_id}"
    else:
        return await msg.answer("❌ Only Picture or Video allowed!", reply_markup=cancel_inline())
    set_category_media(cat_id, val)
    await msg.answer("✅ Saved!")
    await state.clear()


@dp.callback_query(F.data == "adm_del_cat_media")
@auto_answer()
async def del_cat_media(call: CallbackQuery):
    cats = get_categories()
    kb = [[IBtn(text=c[1], callback_data=f"delcatmd_{c[0]}", style="primary")] for c in cats]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select Product to delete Picture/Video:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("delcatmd_"))
@auto_answer("✅ Deleted!", alert=True)
async def del_cat_media2(call: CallbackQuery):
    try:
        set_category_media(int(call.data.split("_")[1]), None)
    except (ValueError, IndexError):
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, MANAGE_PRODUCTS_TEXT,
                           reply_markup=manage_products_kb())


@dp.callback_query(F.data == "adm_add_post_media")
@auto_answer()
async def set_post_media(call: CallbackQuery):
    items = get_all_items()
    kb = [[IBtn(text=i["name"], callback_data=f"setpostmd_{i['id']}", style="primary")] for i in items]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select Plan to add File/Link (sent after purchase):",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("setpostmd_"))
@auto_answer()
async def set_post_media2(call: CallbackQuery, state: FSMContext):
    try:
        await state.update_data(item_id=int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        return
    await safe_edit_or_send(call, "Send Video/Picture/File/Text/Voice:", cancel_inline())
    await state.set_state(AdminPostMedia.media)


@dp.message(AdminPostMedia.media)
async def set_post_media3(msg: Message, state: FSMContext):
    item_id = (await state.get_data()).get('item_id')
    if msg.photo:
        val = f"photo:{msg.photo[-1].file_id}"
    elif msg.video:
        val = f"video:{msg.video.file_id}"
    elif msg.document:
        val = f"document:{msg.document.file_id}"
    elif msg.audio:
        val = f"audio:{msg.audio.file_id}"
    elif msg.voice:
        val = f"voice:{msg.voice.file_id}"
    else:
        val = f"text:{msg.html_text or ''}"
    set_item_post_media(item_id, val)
    await msg.answer("✅ Saved!")
    await state.clear()


@dp.callback_query(F.data == "adm_del_post_media")
@auto_answer()
async def del_post_media(call: CallbackQuery):
    items = get_all_items()
    kb = [[IBtn(text=i["name"], callback_data=f"delpostmd_{i['id']}", style="primary")] for i in items]
    kb.append([IBtn(text="Back", callback_data="adm_manage_products",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, "Select Plan to delete File/Link:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("delpostmd_"))
@auto_answer("✅ Deleted!", alert=True)
async def del_post_media2(call: CallbackQuery):
    try:
        set_item_post_media(int(call.data.split("_")[1]), None)
    except (ValueError, IndexError):
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, MANAGE_PRODUCTS_TEXT,
                           reply_markup=manage_products_kb())

# ====================================================================================================
# [ STOCK SYSTEM ]
# ====================================================================================================

# ✅ កែសម្រួល៖ STOCK PLAN (Product Selection)
@dp.callback_query(F.data == "adm_manage_stock")
@auto_answer()
async def adm_manage_stock(call: CallbackQuery):
    cats = get_categories()
    if not cats:
        return await bot.send_message(call.message.chat.id, "No products yet!")

    kb = []
    for c in cats:
        items = get_items(c[0])
        plan_names = ", ".join([i["name"] for i in items]) if items else "No plans"
        kb.append([IBtn(text=f"{c[1]}: {plan_names}", callback_data=f"stkcat_{c[0]}",
                        style="primary", icon_custom_emoji_id=ICON_ADD_PLAN)])

    kb.append([IBtn(text="Back", callback_data="back_admin",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    
    text = (
        f"<tg-emoji emoji-id=\"{ICON_STOCK_PLAN}\">📦</tg-emoji> <b>STOCK PLAN</b>\n\n"
        f"Select a product :"
    )
    await safe_edit_or_send(call, text, InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("stkcat_"))
@auto_answer()
async def stk_plan_pick(call: CallbackQuery):
    try:
        cat_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    cat = get_category(cat_id)
    if not cat:
        return
    items = get_items(cat_id)
    if not items:
        return await bot.send_message(call.message.chat.id, "No plans in this product!")

    kb = [[IBtn(text=i["name"], callback_data=f"stkopt_{i['id']}",
                style="primary", icon_custom_emoji_id=ICON_ADD_PLAN)] for i in items]
    kb.append([IBtn(text="Back", callback_data="adm_manage_stock",
                    style="primary", icon_custom_emoji_id=ICON_BACK)])
    await safe_edit_or_send(call, f"📂 <b>{cat['name']}</b>\n\nSelect a Plan:",
                             InlineKeyboardMarkup(inline_keyboard=kb))


# ✅ កែសម្រួល៖ STOCK PLAN (Plan Details)
@dp.callback_query(F.data.startswith("stkopt_"))
@auto_answer()
async def stk_options(call: CallbackQuery):
    try:
        item_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    item = get_item(item_id)
    if not item:
        return
    cat = get_category(item["cat_id"])
    cat_name = cat["name"] if cat else "?"
    current_stock = count_stock(item_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Add Stock", callback_data=f"addstk_{item_id}", style="primary",
              icon_custom_emoji_id=ICON_ADD_FILE_LINK),
         IBtn(text="View Key", callback_data=f"viewkey_{item_id}", style="primary",
              icon_custom_emoji_id=ICON_ADD_PLAN)],
        [IBtn(text="Back", callback_data=f"stkcat_{item['cat_id']}",
              style="primary", icon_custom_emoji_id=ICON_BACK)]
    ])

    p_norm = float(item.get("price", 0.0))
    p_res = float(item.get("reseller_price", p_norm))

    text = (
        f"<tg-emoji emoji-id=\"{ICON_STOCK_PLAN}\">📦</tg-emoji><b>STOCK PLAN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<tg-emoji emoji-id=\"{ICON_STK_PRODUCT}\">🏷</tg-emoji> Product: <b>{cat_name}</b>\n"
        f"<tg-emoji emoji-id=\"{ICON_STK_PLAN}\">📌</tg-emoji> Plan: <b>{item['name']}</b>\n"
        f"<tg-emoji emoji-id=\"{ICON_STK_PRICE}\">💵</tg-emoji> Price: <b>${p_norm:.2f}</b> | "
        f"<tg-emoji emoji-id=\"{ICON_STK_RESELLER}\">🏷</tg-emoji> Reseller: <b>${p_res:.2f}</b>\n"
        f"<tg-emoji emoji-id=\"{ICON_STK_STOCK}\">📊</tg-emoji> Available Stock: <b>{current_stock}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("addstk_"))
@auto_answer()
async def add_stock_start(call: CallbackQuery, state: FSMContext):
    try:
        item_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    await state.update_data(item_id=item_id)
    text = ("📥 <b>Please send stock now</b>\n"
            "(as files / video / picture / link / text)\n\n"
            "<i>If text: one key per line</i>")
    await safe_edit_or_send(call, text, cancel_inline())
    await state.set_state(AdminAddStock.keys)


@dp.message(AdminAddStock.keys)
async def add_stock_save(msg: Message, state: FSMContext):
    item_id = (await state.get_data()).get('item_id')
    if not item_id:
        await state.clear()
        return
    await state.clear()

    rows: List[Tuple[str, str]] = []
    sample_keys = []

    if msg.photo:
        rows.append((f"photo:{msg.photo[-1].file_id}", 'file'))
        sample_keys.append("(photo file)")
    elif msg.video:
        rows.append((f"video:{msg.video.file_id}", 'file'))
        sample_keys.append("(video file)")
    elif msg.document:
        rows.append((f"document:{msg.document.file_id}", 'file'))
        sample_keys.append("(document file)")
    elif msg.text:
        lines = [L.strip() for L in msg.text.split('\n') if L.strip()]
        for k in lines:
            rows.append((k, 'text'))
            sample_keys.append(k)
    else:
        return await msg.answer("❌ Unsupported format!", reply_markup=cancel_inline())

    added = add_stock_rows_bulk(item_id, rows)

    key_list = "\n".join([f"<code>{k}</code>" for k in sample_keys[:20]])
    if len(sample_keys) > 20:
        key_list += f"\n... and {len(sample_keys)-20} more"

    await msg.answer(f"✅ <b>DONE FOR ADD STOCK {added}!</b>\n\n<b>NAME KEY</b>\n{key_list}")

    item = get_item(item_id)
    if not item:
        return

    cat = get_category(item["cat_id"])
    cat_name = cat["name"] if cat else item["name"]

    plan_icon = _fmt_premium_icon(item.get("icon"))
    alert = (
        f'<tg-emoji emoji-id="6269366244462301331">📢</tg-emoji> <b>ស្តុកថ្មីបានបន្ថែមរួចរាល់ហើយ!</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'- <tg-emoji emoji-id="6284847737548578337">📦</tg-emoji> <b>ទំនិញ៖</b> {cat_name}\n'
        f'- <tg-emoji emoji-id="5413879192267805083">⏳</tg-emoji> <b>រយៈពេល៖</b> {plan_icon}{item["name"]}\n'
        f'- <tg-emoji emoji-id="6284847737548578337">🔑</tg-emoji> <b>ចំនួនបន្ថែមស្តុកថ្មី បរិមាណ៖ Key(s):</b> {added}\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="6005570495603282482">✨</tg-emoji> រួចរាល់សម្រាប់ទិញឥឡូវនេះ!\n'
        f'<tg-emoji emoji-id="6102563003314479651">👉</tg-emoji> ចុចប៊ូតុង '
        f'<tg-emoji emoji-id="6147439566107186310">🛒</tg-emoji> <b>BUY NOW</b> ខាងក្រោមដើម្បីទិញ៖'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[IBtn(
        text="BUY NOW", callback_data=f"sel_item_{item_id}",
        style="primary", icon_custom_emoji_id="5312361253610475399")]])
    run_bg_task(broadcast_users(alert, kb))


async def _render_view_key(call: CallbackQuery, item_id: int):
    item = get_item(item_id)
    if not item:
        return await bot.send_message(call.message.chat.id, "Item not found.")
    cat = get_category(item["cat_id"])
    cat_name = cat["name"] if cat else "?"

    stocks = get_stock_list(item_id)

    if not stocks:
        text = (f"🔑 <b>VIEW KEY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏷 Product: <b>{cat_name}</b>\n"
                f"📌 Plan: <b>{item['name']}</b>\n\n"
                f"❌ <i>No keys in stock.</i>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [IBtn(text="Back", callback_data=f"stkopt_{item_id}",
                  style="primary", icon_custom_emoji_id=ICON_BACK)]
        ])
        await safe_edit_or_send(call, text, kb)
        return

    key_lines = [s[1] for s in stocks]
    txt_content = "\n".join(key_lines)
    file_bytes = txt_content.encode("utf-8")

    clean_filename = f"{item['name'].replace(' ', '_')}_keys.txt"
    file = BufferedInputFile(file_bytes, filename=clean_filename)

    caption = (
        f"🔑 <b>VIEW ALL KEYS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Product: <b>{cat_name}</b>\n"
        f"📌 Plan: <b>{item['name']}</b>\n"
        f"📊 Total Keys: <b>{len(stocks)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data=f"stkopt_{item_id}",
              style="primary", icon_custom_emoji_id=ICON_BACK)]
    ])

    try:
        await call.message.delete()
    except Exception:
        pass

    await bot.send_document(
        chat_id=call.message.chat.id,
        document=file,
        caption=caption,
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("viewkey_"))
@auto_answer()
async def view_key_handler(call: CallbackQuery):
    try:
        item_id = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    await _render_view_key(call, item_id)

# ====================================================================================================
# [ BROADCAST / RESELLER / ADMINS / MAINTENANCE / START MEDIA ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_broadcast")
@auto_answer()
async def adm_broadcast(call: CallbackQuery, state: FSMContext):
    await safe_edit_or_send(call,
        "✍️ Send a message to broadcast to all users:\n(Link / Video / File / Picture / Voice / Text):",
        cancel_inline())
    await state.set_state(AdminBroadcast.msg)


@dp.message(AdminBroadcast.msg)
async def execute_broadcast(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Sending...")
    run_bg_task(_do_broadcast(msg))


async def _do_broadcast(msg: Message):
    users = db_all_users()
    success, fail = 0, 0
    html_caption = msg.html_text or ""

    for uid_str in users:
        try:
            uid = int(uid_str)
            if msg.text:
                await bot.send_message(uid, html_caption)
            elif msg.photo:
                await bot.send_photo(uid, msg.photo[-1].file_id, caption=html_caption)
            elif msg.video:
                await bot.send_video(uid, msg.video.file_id, caption=html_caption)
            elif msg.document:
                await bot.send_document(uid, msg.document.file_id, caption=html_caption)
            elif msg.audio:
                await bot.send_audio(uid, msg.audio.file_id, caption=html_caption)
            elif msg.voice:
                await bot.send_voice(uid, msg.voice.file_id, caption=html_caption)
            success += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            fail += 1
        except TelegramForbiddenError:
            fail += 1
        except Exception as e:
            logger.warning(f"Broadcast to {uid_str} failed: {e}")
            fail += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            msg.chat.id,
            f"📊 Sent to <b>{success+fail}</b> users \n"
            f"- ✅ Success: <b>{success}</b> \n- ❌ Failed: <b>{fail}</b>")
    except Exception:
        pass


# ----- MANAGE RESELLER -----

@dp.callback_query(F.data == "adm_reseller")
@auto_answer()
async def adm_reseller(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="View Resellers (Total Buy)", callback_data="view_resellers",
              style="success", icon_custom_emoji_id=ICON_RES_TITLE)],
        [IBtn(text="Add Reseller", callback_data="add_reseller", style="primary",
              icon_custom_emoji_id=ICON_CREATE_PRODUCT),
         IBtn(text="Del Reseller", callback_data="del_reseller", style="danger",
              icon_custom_emoji_id=ICON_DELETE_PRODUCT)],
        [IBtn(text="Add Reseller VIP", callback_data="add_reseller_vip", style="primary",
              icon_custom_emoji_id=ICON_ADD_RES_VIP),
         IBtn(text="Delete Reseler VIP", callback_data="del_reseller_vip", style="danger",
              icon_custom_emoji_id=ICON_DEL_RES_VIP)],
        [IBtn(text="Edit Price Reseller Normal", callback_data="edit_res_norm_price", style="primary",
              icon_custom_emoji_id=ICON_EDIT_RES_NORM)],
        [IBtn(text="Edit Price Reseller VIP", callback_data="edit_res_vip_price", style="primary",
              icon_custom_emoji_id=ICON_EDIT_RES_VIP)],
        [IBtn(text="Back", callback_data="back_admin", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(call, "<b>Manage Reseller</b>\n\nChoose an option:", kb)


# ✅ កែសម្រួល៖ RESELLER LIST (រួមទាំង Reseller Normal និង Reseller VIP)
@dp.callback_query(F.data == "view_resellers")
@auto_answer()
async def view_resellers(call: CallbackQuery):
    resellers = db_get_resellers()
    if not resellers:
        return await bot.send_message(call.message.chat.id, "❌ No resellers yet.")

    lines = [
        f"<tg-emoji emoji-id=\"{ICON_RES_TITLE}\">👥</tg-emoji><b>RESELLER LIST</b>",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    grand_orders = 0
    grand_money = 0.0

    for idx, r in enumerate(resellers, 1):
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(
            f"\n<b>{idx}. {r['first_name']}</b> (<b>{r['role']}</b>)\n"
            f"   <tg-emoji emoji-id=\"{ICON_RES_ID}\">🆔</tg-emoji> ID: <code>{r['id']}</code>\n"
            f"   <tg-emoji emoji-id=\"{ICON_RES_USER}\">📛</tg-emoji> User: {uname}\n"
            f"   <tg-emoji emoji-id=\"{ICON_RES_ORDERS}\">📦</tg-emoji> Orders: <b>{r['total_orders']}</b>\n"
            f"   <tg-emoji emoji-id=\"{ICON_RES_PRICE}\">💵</tg-emoji> Total Buy: <b>${r['total_spent']:.2f}</b>"
        )
        grand_orders += r["total_orders"]
        grand_money += r["total_spent"]

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<tg-emoji emoji-id=\"{ICON_RES_TITLE}\">👥</tg-emoji> <b>Total Resellers:</b> <b>{len(resellers)}</b>")
    lines.append(f"<tg-emoji emoji-id=\"{ICON_RES_ORDERS}\">📦</tg-emoji> <b>Total Buy (Orders):</b> <b>{grand_orders}</b>")
    lines.append(f"<tg-emoji emoji-id=\"{ICON_RES_TOTAL_MONEY}\">💰</tg-emoji> <b>Total Buy (Money):</b> <b>${grand_money:.2f}</b>")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n\n<i>... truncated</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Refresh", callback_data="view_resellers", style="primary",
              icon_custom_emoji_id=ICON_REFRESH),
         IBtn(text="Back", callback_data="adm_reseller", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data == "add_reseller")
@auto_answer()
async def add_reseller_st(call: CallbackQuery, state: FSMContext):
    await safe_edit_or_send(call, "Enter User ID to add as Reseller:", cancel_inline())
    await state.set_state(AdminReseller.add_id)


@dp.message(AdminReseller.add_id)
async def add_reseller_save(msg: Message, state: FSMContext):
    try:
        rid = int((msg.text or "").strip())
        db_upsert_user(rid, is_reseller=1)
        await msg.answer(f"✅ User <code>{rid}</code> is now a Reseller (ទទួលបានតម្លៃពិសេស)!")
    except ValueError:
        await msg.answer("❌ Invalid ID.")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


@dp.callback_query(F.data == "del_reseller")
@auto_answer()
async def del_reseller_st(call: CallbackQuery, state: FSMContext):
    await safe_edit_or_send(call, "Enter User ID to remove from Reseller:", cancel_inline())
    await state.set_state(AdminReseller.del_id)


@dp.message(AdminReseller.del_id)
async def del_reseller_save(msg: Message, state: FSMContext):
    try:
        rid = int((msg.text or "").strip())
        db_upsert_user(rid, is_reseller=0)
        await msg.answer(f"✅ User <code>{rid}</code> removed from Reseller.")
    except ValueError:
        await msg.answer("❌ Invalid ID.")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


@dp.callback_query(F.data == "add_reseller_vip")
@auto_answer()
async def add_reseller_vip_st(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_reseller", style="primary", icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(
        call,
        f"<tg-emoji emoji-id=\"{ICON_ADD_RES_VIP}\">👑</tg-emoji> Enter User ID to add as <b>Reseller VIP</b>:",
        kb
    )
    await state.set_state(AdminReseller.add_vip_id)


@dp.message(AdminReseller.add_vip_id)
async def add_reseller_vip_save(msg: Message, state: FSMContext):
    try:
        rid = int((msg.text or "").strip())
        db_upsert_user(rid, is_reseller_vip=1)
        await msg.answer(f"✅ User <code>{rid}</code> is now a <b>Reseller VIP</b>!")
    except ValueError:
        await msg.answer("❌ Invalid ID.")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


@dp.callback_query(F.data == "del_reseller_vip")
@auto_answer()
async def del_reseller_vip_st(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_reseller", style="primary", icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(
        call,
        f"<tg-emoji emoji-id=\"{ICON_DEL_RES_VIP}\">🗑️</tg-emoji> Enter User ID to remove from <b>Reseller VIP</b>:",
        kb
    )
    await state.set_state(AdminReseller.del_vip_id)


@dp.message(AdminReseller.del_vip_id)
async def del_reseller_vip_save(msg: Message, state: FSMContext):
    try:
        rid = int((msg.text or "").strip())
        db_upsert_user(rid, is_reseller_vip=0)
        await msg.answer(f"✅ User <code>{rid}</code> removed from <b>Reseller VIP</b>.")
    except ValueError:
        await msg.answer("❌ Invalid ID.")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


@dp.callback_query(F.data.in_({"edit_res_norm_price", "edit_res_vip_price"}))
@auto_answer()
async def edit_reseller_price_pick(call: CallbackQuery):
    is_vip_mode = call.data == "edit_res_vip_price"
    items = get_all_items()
    if not items:
        return await bot.send_message(call.message.chat.id, "No plans yet!")

    kb = []
    for i in items:
        cat = get_category(i.get("cat_id"))
        cat_name = cat["name"] if cat else "Product"
        p_val = float(i.get("reseller_vip_price", i.get("reseller_price", i.get("price", 0.0))) if is_vip_mode else i.get("reseller_price", i.get("price", 0.0)))
        cb_prefix = "edrvip" if is_vip_mode else "edrnorm"
        kb.append([IBtn(
            text=f"{cat_name} | {i['name']} (${p_val:.2f})",
            callback_data=f"{cb_prefix}_{i['id']}",
            style="primary",
            icon_custom_emoji_id=i.get("icon") or ICON_ADD_PLAN
        )])
    kb.append([IBtn(text="Back", callback_data="adm_reseller", style="primary", icon_custom_emoji_id=ICON_BACK)])

    title = (
        f"<tg-emoji emoji-id=\"{ICON_EDIT_RES_VIP}\">👑</tg-emoji> <b>Edit Price Reseller VIP</b>"
        if is_vip_mode else
        f"<tg-emoji emoji-id=\"{ICON_EDIT_RES_NORM}\">✏️</tg-emoji> <b>Edit Price Reseller Normal</b>"
    )
    await safe_edit_or_send(call, f"{title}\n\nសូមជ្រើសរើស Product & Plan ខាងក្រោម៖", InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith(("edrnorm_", "edrvip_")))
@auto_answer()
async def edit_reseller_price_prompt(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    mode = parts[0]
    try:
        item_id = int(parts[1])
    except (ValueError, IndexError):
        return
    item = get_item(item_id)
    if not item:
        return
    cat = get_category(item.get("cat_id"))
    cat_name = cat["name"] if cat else "Product"
    cat_icon = _fmt_premium_icon(cat.get("icon") if cat else None)
    plan_icon = _fmt_premium_icon(item.get("icon"))

    is_vip_mode = (mode == "edrvip")
    field = "reseller_vip_price" if is_vip_mode else "reseller_price"
    curr_val = float(item.get(field, item.get("price", 0.0)))
    label = "Reseller VIP" if is_vip_mode else "Reseller Normal"

    await state.update_data(item_id=item_id, field=field, label=label)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="edit_res_vip_price" if is_vip_mode else "edit_res_norm_price",
              style="primary", icon_custom_emoji_id=ICON_BACK)]
    ])
    text = (
        f"📦 Product: {cat_icon}<b>{cat_name}</b>\n"
        f"📌 Plan: {plan_icon}<b>{item['name']}</b>\n"
        f"💵 តម្លៃ {label} បច្ចុប្បន្ន: <b>${curr_val:.2f}</b>\n\n"
        f"👉 សូមបញ្ចូលតម្លៃ {label} ថ្មី ($) (ឧ. 3.5)៖"
    )
    await safe_edit_or_send(call, text, kb)
    await state.set_state(AdminEditResellerPrice.amount)


@dp.message(AdminEditResellerPrice.amount)
async def edit_reseller_price_save(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace("$", "")
    try:
        val = round(float(raw), 2)
        if val < 0:
            raise ValueError
    except Exception:
        return await msg.answer("❌ សូមបញ្ចូលតម្លៃជាលេខត្រឹមត្រូវ (ឧ. 3.5)៖", reply_markup=cancel_inline())

    data = await state.get_data()
    item_id = data["item_id"]
    field = data["field"]
    label = data["label"]
    update_item_single_price(item_id, field, val)
    item = get_item(item_id)
    cat = get_category(item.get("cat_id")) if item else None
    cat_name = cat["name"] if cat else "Product"
    cat_icon = _fmt_premium_icon(cat.get("icon") if cat else None)
    plan_icon = _fmt_premium_icon(item.get("icon") if item else None)
    plan_name = item["name"] if item else f"ID {item_id}"
    await state.clear()

    await msg.answer(
        f"✅ <b>កែប្រែតម្លៃ {label} ជោគជ័យ!</b>\n\n"
        f"📦 Product: {cat_icon}<b>{cat_name}</b>\n"
        f"📌 Plan: {plan_icon}<b>{plan_name}</b>\n"
        f"💵 តម្លៃ {label} ថ្មី: <b>${val:.2f}</b>"
    )


# ====================================================================================================
# [ 👑 MANAGE ADMINS SYSTEM ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_manage_admins")
@auto_answer()
async def adm_manage_admins(call: CallbackQuery):
    await safe_edit_or_send(call, "<b>MANAGE ADMINS</b>\n\nសូមជ្រើសរើសជម្រើសខាងក្រោម៖",
                             manage_admins_kb())


@dp.callback_query(F.data == "adm_view_admins")
@auto_answer()
async def adm_view_admins(call: CallbackQuery):
    admins = db_get_admins()
    lines = [
        f"<tg-emoji emoji-id=\"{ICON_VIEW_ADMINS}\">👑</tg-emoji> <b>បញ្ជីរាយនាម ADMIN ទាំងអស់</b>",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for idx, a in enumerate(admins, 1):
        uname = f"@{a['username']}" if a["username"] else "—"
        role = " <b>Owner</b>" if a["is_owner"] else "<b>Admin</b>"
        lines.append(
            f"<b>{idx}. {a['first_name']}</b> ({role})\n"
            f"   🆔 ID: <code>{a['id']}</code>\n"
            f"   📛 User: {uname}\n"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"👥 <b>សរុប Admin:</b> <b>{len(admins)}</b> នាក់")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="🔄 Refresh", callback_data="adm_view_admins", style="primary"),
         IBtn(text="Back", callback_data="adm_manage_admins", style="primary",
              icon_custom_emoji_id=ICON_BACK)]
    ])
    await safe_edit_or_send(call, "\n".join(lines), kb)


@dp.callback_query(F.data == "adm_add_admin")
@auto_answer()
async def adm_add_admin_st(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in (OWNER_ID, get_current_owner_id()):
        return await quick_answer(call, "⚠️ មានតែ Owner ប៉ុណ្ណោះដែលអាចតែងតាំង Admin បាន!", show_alert=True)
    text = (f"<tg-emoji emoji-id=\"{ICON_ADD_ADMIN}\">➕</tg-emoji> "
            f"<b>Add Admin:</b>\n\nសូមផ្ញើ <b>User ID</b> របស់បុគ្គលដែលអ្នកចង់តែងតាំងជា Admin:")
    await safe_edit_or_send(call, text, cancel_inline())
    await state.set_state(AdminAdmin.add_id)


@dp.message(AdminAdmin.add_id)
async def adm_add_admin_save(msg: Message, state: FSMContext):
    try:
        target_id = int((msg.text or "").strip())
        db_upsert_user(target_id, is_admin=1)
        await msg.answer(f"✅ User ID <code>{target_id}</code> ត្រូវបានតែងតាំងជា <b>Admin</b> ដោយជោគជ័យ!")
    except ValueError:
        await msg.answer("❌ Invalid ID! សូមបញ្ចូល User ID ជាលេខត្រឹមត្រូវ។")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


@dp.callback_query(F.data == "adm_del_admin")
@auto_answer()
async def adm_del_admin_st(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in (OWNER_ID, get_current_owner_id()):
        return await quick_answer(call, "មានតែ Owner ប៉ុណ្ណោះដែលអាចដកសិទ្ធិ Admin បាន!", show_alert=True)
    text = (f"<tg-emoji emoji-id=\"{ICON_DEL_ADMIN}\">🗑️</tg-emoji> "
            f"<b>Del Admin:</b>\n\nសូមផ្ញើ <b>User ID</b> របស់ Admin ដែលអ្នកចង់ដកសិទ្ធិចេញ (មិនអាចដក Owner បានទេ):")
    await safe_edit_or_send(call, text, cancel_inline())
    await state.set_state(AdminAdmin.del_id)


@dp.message(AdminAdmin.del_id)
async def adm_del_admin_save(msg: Message, state: FSMContext):
    try:
        target_id = int((msg.text or "").strip())
        if target_id in (OWNER_ID, get_current_owner_id()):
            await state.clear()
            return await msg.answer("❌ <b>មិនអាចដកសិទ្ធិ Owner បានទេ!</b>")
        db_upsert_user(target_id, is_admin=0)
        await msg.answer(f"✅ User ID <code>{target_id}</code> ត្រូវបានដកសិទ្ធិពី Admin រួចរាល់។")
    except ValueError:
        await msg.answer("❌ Invalid ID! សូមបញ្ចូល User ID ជាលេខត្រឹមត្រូវ។")
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")
    await state.clear()


# ====================================================================================================
# [ 🛠️ MAINTENANCE SYSTEM ]
# ====================================================================================================

@dp.callback_query(F.data == "adm_maintenance")
@auto_answer()
async def adm_maintenance_menu(call: CallbackQuery):
    text, kb = maintenance_kb()
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data == "adm_maint_open")
@auto_answer("✅ Bot ត្រូវបានបើកដំណើរការធម្មតាវិញ!", alert=True)
async def adm_maint_open_handler(call: CallbackQuery):
    db_set_setting("maintenance", "0")
    text, kb = maintenance_kb()
    await safe_edit_or_send(call, text, kb)


@dp.callback_query(F.data == "adm_maint_close")
@auto_answer("🔴 Bot ត្រូវបានបិទ (Maintenance Mode)!", alert=True)
async def adm_maint_close_handler(call: CallbackQuery):
    db_set_setting("maintenance", "1")
    text, kb = maintenance_kb()
    await safe_edit_or_send(call, text, kb)


# ----- START MEDIA -----

@dp.callback_query(F.data == "adm_start_media")
@auto_answer()
async def adm_start_media(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_bot_content", style="primary",
              icon_custom_emoji_id=ICON_BACK_NEW)]
    ])
    await safe_edit_or_send(
        call,
        f"<tg-emoji emoji-id=\"{ICON_SET_START_MEDIA}\">🖼️</tg-emoji> "
        f"Send Video, GIF, File, or Photo for /start (or type 'del' to remove):",
        kb
    )
    await state.set_state(AdminStartMedia.media)


@dp.message(AdminStartMedia.media)
async def save_start_media(msg: Message, state: FSMContext):
    if msg.text and msg.text.lower() == 'del':
        data = _load_db()
        data["settings"].pop("start_media", None)
        _save_db(data)
    elif msg.photo:
        db_set_setting('start_media', f"photo:{msg.photo[-1].file_id}")
    elif msg.video:
        db_set_setting('start_media', f"video:{msg.video.file_id}")
    elif msg.animation:
        db_set_setting('start_media', f"animation:{msg.animation.file_id}")
    elif msg.document:
        db_set_setting('start_media', f"document:{msg.document.file_id}")
    else:
        return await msg.answer("❌ Please send Video, GIF, File, or Photo!")
    await msg.answer("✅ Saved!", reply_markup=bot_content_kb())
    await state.clear()


@dp.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery):
    await quick_answer(call)


# ====================================================================================================
# [ ⚙️ DISABLE / ENABLE FUNCTION & CLOSE PRODUCT ]
# ====================================================================================================

def _func_toggle_kb(is_enable_mode: bool) -> InlineKeyboardMarkup:
    funcs = [
        ("BUY NOW", "fn_buy_now"),
        ("Top Up", "fn_top_up"),
        ("How to buy", "fn_how_to_buy"),
    ]
    rows = []
    for label, key in funcs:
        enabled = get_setting(key, "1") == "1"
        state_str = "ON" if enabled else "OFF"
        btn_style = "success" if enabled else "danger"
        cb = f"fnset_{'on' if is_enable_mode else 'off'}_{key}"
        rows.append([IBtn(text=f"{label} ({state_str})", callback_data=cb, style=btn_style)])
    rows.append([IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
                      icon_custom_emoji_id=ICON_BACK_NEW)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_disable_func")
@auto_answer()
async def adm_disable_func_menu(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="6021440013214948027">🚫</tg-emoji> <b>Disable function</b>\n\nជ្រើសរើសមុខងារដែលអ្នកចង់បិទ៖',
        _func_toggle_kb(is_enable_mode=False)
    )


@dp.callback_query(F.data == "adm_enable_func")
@auto_answer()
async def adm_enable_func_menu(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="5445333299360723379">✅</tg-emoji> <b>Enable the function</b>\n\nជ្រើសរើសមុខងារដែលអ្នកចង់បើកវិញ៖',
        _func_toggle_kb(is_enable_mode=True)
    )


@dp.callback_query(F.data.startswith("fnset_"))
async def adm_func_set_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    parts = call.data.split("_", 2)
    action = parts[1]  # on / off
    key = parts[2]
    is_enable_mode = (action == "on")
    db_set_setting(key, "1" if is_enable_mode else "0")
    await quick_answer(call, "✅ បានបើក!" if is_enable_mode else "🔴 បានបិទ!", show_alert=True)
    title = (
        '<tg-emoji emoji-id="5445333299360723379">✅</tg-emoji> <b>Enable the function</b>\n\nជ្រើសរើសមុខងារដែលអ្នកចង់បើកវិញ៖'
        if is_enable_mode else
        '<tg-emoji emoji-id="6021440013214948027">🚫</tg-emoji> <b>Disable function</b>\n\nជ្រើសរើសមុខងារដែលអ្នកចង់បិទ៖'
    )
    await safe_edit_or_send(call, title, _func_toggle_kb(is_enable_mode=is_enable_mode))


def _close_product_kb() -> InlineKeyboardMarkup:
    data = _load_db()
    rows = []
    for c in data.get("categories", []):
        is_closed = bool(c.get("is_closed", False))
        status_lbl = "🔴 Closed" if is_closed else "🟢 Open"
        style = "danger" if is_closed else "success"
        rows.append([IBtn(
            text=f"{c['name']} ({status_lbl})",
            callback_data=f"togcat_{c['id']}",
            style=style,
            icon_custom_emoji_id=c.get("icon")
        )])
    rows.append([IBtn(text="Back", callback_data="adm_manager_setting", style="primary",
                      icon_custom_emoji_id=ICON_BACK_NEW)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_close_product")
@auto_answer()
async def adm_close_product_menu(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="6021440013214948027">🔒</tg-emoji> <b>Close Product</b>\n\nចុចលើ Product ដើម្បីបិទ ឬបើកវិញ៖',
        _close_product_kb()
    )


@dp.callback_query(F.data.startswith("togcat_"))
async def adm_toggle_cat_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user.id):
        return
    try:
        cid = int(call.data.split("_")[1])
    except (ValueError, IndexError):
        return
    data = _load_db()
    new_state = False
    for c in data.get("categories", []):
        if c["id"] == cid:
            new_state = not bool(c.get("is_closed", False))
            c["is_closed"] = new_state
            break
    _save_db(data)
    await quick_answer(call, "🔴 Product Closed!" if new_state else "🟢 Product Opened!", show_alert=True)
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="6021440013214948027">🔒</tg-emoji> <b>Close Product</b>\n\nចុចលើ Product ដើម្បីបិទ ឬបើកវិញ៖',
        _close_product_kb()
    )


# ====================================================================================================
# [ 🤖 CLONE BOT CUSTOMER SYSTEM ]
# ====================================================================================================

class CloneBotFSM(StatesGroup):
    token = State()
    admin_id = State()
    change_token = State()
    change_admin_id = State()


def _get_clones() -> List[dict]:
    return _load_db(DB_JSON_PATH).get("clones", [])


def _save_clones(clones: List[dict]):
    data = _load_db(DB_JSON_PATH)
    data["clones"] = clones
    _save_db(data, DB_JSON_PATH)


async def _run_clone_bot_loop(clone_id: str, token: str, owner_id: int, db_path: str):
    c_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _bot_runtime_meta[token] = {"db_path": db_path, "owner_id": owner_id, "clone_id": clone_id}
    _clone_bot_objs[clone_id] = c_bot
    init_db(db_path=db_path, owner_id=owner_id)
    try:
        await c_bot.delete_webhook(drop_pending_updates=True)
        await c_bot.set_my_commands([
            BotCommand(command="start", description="ចាប់ផ្ដើម / start"),
            BotCommand(command="buy_now", description="ទិញទំនិញ / Buy goods"),
            BotCommand(command="my_orders", description="ការបញ្ជាទិញរបស់ខ្ញុំ / My Orders"),
        ])
    except Exception as e:
        logger.warning(f"Clone {clone_id} init failed: {e}")

    logger.info(f"🤖 Clone bot {clone_id} started polling...")
    offset = 0
    allowed = dp.resolve_used_update_types()
    try:
        while True:
            try:
                updates = await c_bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=allowed,
                )
                for upd in updates:
                    offset = upd.update_id + 1
                    run_bg_task(dp.feed_update(c_bot, upd))
            except asyncio.CancelledError:
                raise
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                logger.error(f"Clone {clone_id} polling error: {e}")
                await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        logger.info(f"🛑 Clone bot {clone_id} stopped.")
    finally:
        _bot_runtime_meta.pop(token, None)
        _clone_bot_objs.pop(clone_id, None)
        try:
            await c_bot.session.close()
        except Exception:
            pass


async def start_clone_bot_task(clone_obj: dict):
    cid = clone_obj["id"]
    await stop_clone_bot_task(cid)
    t = asyncio.create_task(_run_clone_bot_loop(
        clone_id=cid,
        token=clone_obj["token"],
        owner_id=int(clone_obj["owner_id"]),
        db_path=clone_obj["db_path"]
    ))
    _clone_tasks[cid] = t


async def stop_clone_bot_task(cid: str):
    t = _clone_tasks.pop(cid, None)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    c_bot = _clone_bot_objs.pop(cid, None)
    if c_bot:
        _bot_runtime_meta.pop(c_bot.token, None)
        try:
            await c_bot.session.close()
        except Exception:
            pass


def clone_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="Clone Bot", callback_data="adm_clone_add", style="success",
                 icon_custom_emoji_id="5334673106202010226"),
            IBtn(text="My Bot", callback_data="adm_clone_list", style="primary",
                 icon_custom_emoji_id="5355051922862653659"),
        ],
        [
            IBtn(text="Back", callback_data="back_admin", style="primary",
                 icon_custom_emoji_id="5352759161945867747"),
        ]
    ])


@dp.callback_query(F.data == "adm_clone_menu")
@auto_answer()
async def adm_clone_menu_handler(call: CallbackQuery, state: FSMContext):
    if is_clone_bot() or not is_admin_user(call.from_user.id):
        return
    await state.clear()
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="6325393233845167901">🤖</tg-emoji> <b>Clone Bot Customer</b>\n\nសូមជ្រើសរើសមុខងារខាងក្រោម៖',
        clone_menu_kb()
    )


@dp.callback_query(F.data == "adm_clone_add")
@auto_answer()
async def adm_clone_add_prompt(call: CallbackQuery, state: FSMContext):
    if is_clone_bot() or not is_admin_user(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_clone_menu", style="primary",
              icon_custom_emoji_id="5352759161945867747")]
    ])
    await safe_edit_or_send(
        call,
        '<tg-emoji emoji-id="5334673106202010226">🤖</tg-emoji> <b>Clone Bot</b>\n\nសូមផ្ញើ <b>Bot Token</b> របស់អតិថិជន៖',
        kb
    )
    await state.set_state(CloneBotFSM.token)


@dp.message(CloneBotFSM.token)
async def adm_clone_add_token(msg: Message, state: FSMContext):
    tok = (msg.text or "").strip()
    if ":" not in tok:
        return await msg.answer("❌ Bot Token មិនត្រឹមត្រូវទេ!")
    try:
        test_bot = Bot(token=tok)
        me = await test_bot.get_me()
        await test_bot.session.close()
    except Exception:
        return await msg.answer("❌ Bot Token មិនដំណើរការទេ! សូមពិនិត្យម្តងទៀត។")

    await state.update_data(token=tok, bot_username=me.username or "", bot_name=me.first_name or "Clone Bot")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data="adm_clone_menu", style="primary",
              icon_custom_emoji_id="5352759161945867747")]
    ])
    await msg.answer(
        f"✅ <b>Bot Verified:</b> @{me.username} (<code>{me.first_name}</code>)\n\n"
        f"👉 សូមផ្ញើ <b>User ID Admin</b> សម្រាប់ Bot នេះ (ឧ. <code>12345678</code>)៖",
        reply_markup=kb
    )
    await state.set_state(CloneBotFSM.admin_id)


@dp.message(CloneBotFSM.admin_id)
async def adm_clone_add_save(msg: Message, state: FSMContext):
    try:
        adm_id = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ សូមបញ្ចូល User ID ជាលេខត្រឹមត្រូវ!")

    data = await state.get_data()
    await state.clear()
    cid = f"c{int(time.time())}"
    db_path = os.path.join("clones", f"{cid}.json")
    clone_obj = {
        "id": cid,
        "token": data["token"],
        "username": data["bot_username"],
        "name": data["bot_name"],
        "owner_id": adm_id,
        "db_path": db_path,
        "status": "running",
    }
    clones = _get_clones()
    clones.append(clone_obj)
    _save_clones(clones)

    await start_clone_bot_task(clone_obj)
    await msg.answer(
        f"✅ <b>Clone Bot ជោគជ័យ & កំពុងដំណើរការ!</b>\n\n"
        f"🤖 Bot: @{clone_obj['username']}\n"
        f"👤 Admin ID: <code>{adm_id}</code>",
        reply_markup=clone_menu_kb()
    )


@dp.callback_query(F.data == "adm_clone_list")
@auto_answer()
async def adm_clone_list_handler(call: CallbackQuery):
    if is_clone_bot() or not is_admin_user(call.from_user.id):
        return
    clones = _get_clones()
    rows = []
    for c in clones:
        is_run = c["id"] in _clone_tasks and not _clone_tasks[c["id"]].done()
        dot = "🟢" if is_run else "🔴"
        rows.append([IBtn(
            text=f"{dot} @{c.get('username') or c.get('name')}",
            callback_data=f"clone_view_{c['id']}",
            style="success" if is_run else "danger"
        )])
    rows.append([IBtn(text="Back", callback_data="adm_clone_menu", style="primary",
                      icon_custom_emoji_id="5352759161945867747")])
    txt = (
        '<tg-emoji emoji-id="5355051922862653659">🤖</tg-emoji> <b>My Bot</b>\n\n'
        + ("ជ្រើសរើស Bot ដើម្បីគ្រប់គ្រង៖" if clones else "<i>មិនទាន់មាន Bot ដែលបាន Clone នៅឡើយទេ។</i>")
    )
    await safe_edit_or_send(call, txt, InlineKeyboardMarkup(inline_keyboard=rows))


def _clone_detail_view(cid: str):
    c = next((x for x in _get_clones() if x["id"] == cid), None)
    if not c:
        return None, None
    is_run = cid in _clone_tasks and not _clone_tasks[cid].done()
    status_str = "🟢 Running" if is_run else "🔴 Stopped"
    txt = (
        f'<tg-emoji emoji-id="5355051922862653659">🤖</tg-emoji> <b>@{c.get("username") or c.get("name")}</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'🆔 Admin ID: <code>{c["owner_id"]}</code>\n'
        f'📊 Status: <b>{status_str}</b>'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            IBtn(text="Start", callback_data=f"clone_start_{cid}", style="success",
                 icon_custom_emoji_id="6323433929894272747"),
            IBtn(text="Restart", callback_data=f"clone_restart_{cid}", style="primary",
                 icon_custom_emoji_id="5807622114424924272"),
            IBtn(text="Stop", callback_data=f"clone_stop_{cid}", style="danger",
                 icon_custom_emoji_id="6323489940562780480"),
        ],
        [
            IBtn(text="Chenge Token", callback_data=f"clone_chtoken_{cid}", style="primary",
                 icon_custom_emoji_id="5334673106202010226"),
            IBtn(text="Chenge ID", callback_data=f"clone_chid_{cid}", style="primary",
                 icon_custom_emoji_id="5330115548900501467"),
        ],
        [
            IBtn(text="Delete Bot", callback_data=f"clone_del_{cid}", style="danger",
                 icon_custom_emoji_id="6203802731170242978"),
            IBtn(text="Back", callback_data="adm_clone_list", style="primary",
                 icon_custom_emoji_id="5352759161945867747"),
        ]
    ])
    return txt, kb


@dp.callback_query(F.data.startswith("clone_view_"))
@auto_answer()
async def clone_view_handler(call: CallbackQuery):
    cid = call.data.split("_", 2)[2]
    txt, kb = _clone_detail_view(cid)
    if txt:
        await safe_edit_or_send(call, txt, kb)


@dp.callback_query(F.data.startswith(("clone_start_", "clone_restart_", "clone_stop_")))
async def clone_power_handler(call: CallbackQuery):
    parts = call.data.split("_", 2)
    action, cid = parts[1], parts[2]
    clones = _get_clones()
    target = next((x for x in clones if x["id"] == cid), None)
    if not target:
        return await quick_answer(call, "Bot not found!", show_alert=True)

    if action == "stop":
        target["status"] = "stopped"
        _save_clones(clones)
        await stop_clone_bot_task(cid)
        await quick_answer(call, "🔴 Bot Stopped!", show_alert=True)
    elif action == "start":
        target["status"] = "running"
        _save_clones(clones)
        await start_clone_bot_task(target)
        await quick_answer(call, "🟢 Bot Started!", show_alert=True)
    elif action == "restart":
        target["status"] = "running"
        _save_clones(clones)
        await start_clone_bot_task(target)
        await quick_answer(call, "🔄 Bot Restarted!", show_alert=True)

    txt, kb = _clone_detail_view(cid)
    if txt:
        await safe_edit_or_send(call, txt, kb)


@dp.callback_query(F.data.startswith("clone_chtoken_"))
@auto_answer()
async def clone_chtoken_prompt(call: CallbackQuery, state: FSMContext):
    cid = call.data.split("_", 2)[2]
    await state.update_data(cid=cid)
    await state.set_state(CloneBotFSM.change_token)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data=f"clone_view_{cid}", style="primary",
              icon_custom_emoji_id="5352759161945867747")]
    ])
    await safe_edit_or_send(call, "🔑 សូមផ្ញើ <b>Bot Token ថ្មី</b>៖", kb)


@dp.message(CloneBotFSM.change_token)
async def clone_chtoken_save(msg: Message, state: FSMContext):
    tok = (msg.text or "").strip()
    try:
        test_bot = Bot(token=tok)
        me = await test_bot.get_me()
        await test_bot.session.close()
    except Exception:
        return await msg.answer("❌ Bot Token មិនត្រឹមត្រូវទេ!")
    cid = (await state.get_data())["cid"]
    await state.clear()
    clones = _get_clones()
    target = next((x for x in clones if x["id"] == cid), None)
    if target:
        target["token"] = tok
        target["username"] = me.username or ""
        target["name"] = me.first_name or "Clone Bot"
        target["status"] = "running"
        _save_clones(clones)
        await start_clone_bot_task(target)
    await msg.answer(f"✅ បានប្ដូរ Token ទៅ @{me.username} & កំពុងដំណើរការ!")


@dp.callback_query(F.data.startswith("clone_chid_"))
@auto_answer()
async def clone_chid_prompt(call: CallbackQuery, state: FSMContext):
    cid = call.data.split("_", 2)[2]
    await state.update_data(cid=cid)
    await state.set_state(CloneBotFSM.change_admin_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IBtn(text="Back", callback_data=f"clone_view_{cid}", style="primary",
              icon_custom_emoji_id="5352759161945867747")]
    ])
    await safe_edit_or_send(call, "🆔 សូមផ្ញើ <b>Admin User ID ថ្មី</b>៖", kb)


@dp.message(CloneBotFSM.change_admin_id)
async def clone_chid_save(msg: Message, state: FSMContext):
    try:
        new_aid = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ User ID មិនត្រឹមត្រូវទេ!")
    cid = (await state.get_data())["cid"]
    await state.clear()
    clones = _get_clones()
    target = next((x for x in clones if x["id"] == cid), None)
    if target:
        target["owner_id"] = new_aid
        _save_clones(clones)
        if target.get("status") == "running":
            await start_clone_bot_task(target)
    await msg.answer(f"✅ បានប្ដូរ Admin ID ទៅ <code>{new_aid}</code> ដោយជោគជ័យ!")


@dp.callback_query(F.data.startswith("clone_del_"))
async def clone_del_handler(call: CallbackQuery):
    cid = call.data.split("_", 2)[2]
    await stop_clone_bot_task(cid)
    clones = _get_clones()
    new_clones = [x for x in clones if x["id"] != cid]
    _save_clones(new_clones)
    await quick_answer(call, "🗑️ Bot Deleted!", show_alert=True)
    await adm_clone_list_handler(call)


# ====================================================================================================
# [ MAIN ]
# ====================================================================================================

async def main():
    logger.info("🚀 BOT IS RUNNING...")

    try:
        await _main_bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"delete_webhook failed: {e}")

    try:
        await _main_bot.set_my_commands([
            BotCommand(command="start", description="ចាប់ផ្ដើម / start"),
            BotCommand(command="buy_now", description="ទិញទំនិញ / Buy goods"),
            BotCommand(command="my_orders", description="ការបញ្ជាទិញរបស់ខ្ញុំ / My Orders"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")

    for c_obj in _get_clones():
        if c_obj.get("status") == "running":
            await start_clone_bot_task(c_obj)

    try:
        await dp.start_polling(
            _main_bot,
            polling_timeout=30,
            backoff=0.02,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        for cid in list(_clone_tasks.keys()):
            await stop_clone_bot_task(cid)
        await close_http()
        await _main_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
