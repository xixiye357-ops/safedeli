#!/usr/bin/env python3
"""Safe Deli minimal WSGI API. Standard-library only; no production deployment implied."""
from __future__ import annotations
import base64, csv, hashlib, io, json, logging, math, os, re, secrets, sqlite3, hmac, time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote, unquote, parse_qs, urlencode, urlsplit
from database_config import resolve_database_path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "safedeli-api-test.db"
# Keep the development default separate so an unconfigured API cannot touch
# the Bot's historical orders.db by accident. Explicit paths resolve the same
# way in both services.
DB_PATH = resolve_database_path(os.environ.get("SAFEDELI_DB_PATH"), DEFAULT_DB, ROOT)
ADMIN_TOKEN = os.environ.get("SAFEDELI_ADMIN_TOKEN", "")
# The server-route order writer (POST /api/public/orders) is a
# separate path from the Telegram Bot writer. Keep it disabled by
# default until single-writer/idempotency reconciliation is deployed.
# (The old unauthenticated POST /api/orders was deleted 2026-11-15,
# M-9; this flag now gates only the server-route endpoint.)
API_ORDERS_ENABLED = os.environ.get("SAFEDELI_API_ORDERS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}

# Server-route orders (2026-11-12): the Mini App posts orders to
# POST /api/public/orders when a Telegram sendData launch is unavailable
# (menu-button / link launches have initData but no query_id, and
# Telegram only allows sendData from a keyboard-button launch).
# BOT_TOKEN validates the Telegram initData HMAC — environment only,
# the SAME value as the bot's .env; the owner adds it to api.env at
# cutover, never in chat, source, or a ZIP. Without it the endpoint
# refuses every order (fail closed). OWNER_CHAT_ID is the owner bypass
# for night orders and the only trusted source of is_test (same as bot.py).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")
# Shop hours source of truth is the SAME files the bot reads
# (shop_hours.json + shop_closed.json in the project root), so the API
# and the bot can never disagree about open/closed. Overridable for tests.
SHOP_HOURS_FILE = Path(os.environ.get("SAFEDELI_SHOP_HOURS_FILE", str(ROOT / "shop_hours.json")))
SHOP_CLOSED_FILE = Path(os.environ.get("SAFEDELI_SHOP_CLOSED_FILE", str(ROOT / "shop_closed.json")))

# --- Admin roles and sessions (Main Dashboard editability, 2026-11-11) -------
# Roles enforced server-side on every admin endpoint (never UI-only):
#   owner   — everything (settings, hours, banners, forms, prices, ledger,
#             admin users, approve).
#   finance — money only: product prices, financial ledger, plus reads.
#   support — view-only: reads only, no state-changing call is permitted.
#   operations — delivery/operations (2026-11-12, unified dashboard):
#             order verification, rider assignment, delivery status changes,
#             plus reads. No ledger, settings, admin users, or approve/reject.
#   merchant — shop owner (2026-11-12, unified dashboard): only shops
#             linked to their account (admin_users.shop_id = primary
#             shop, plus merchant_shops links since v19.70). The
#             selected shop lives on the session row and is re-validated
#             on every request; every merchant endpoint scopes by the
#             session's selected shop, never by request data.
# The legacy single bearer token (SAFEDELI_ADMIN_TOKEN) keeps working and is
# treated as owner: it is the bootstrap credential. Create the first admin
# user with it, then the token may be removed — sessions keep working as
# long as at least one active admin user exists.
ADMIN_ROLES = ("owner", "finance", "support", "operations", "merchant")
SESSION_TTL_SECONDS = 12 * 3600
# Login rate limiting (2026-11-11): 5 failed attempts per username,
# then a 15-minute lockout. In-memory only — it resets when the API
# process restarts, which is acceptable for a single-process API.
# The legacy admin token is NOT rate-limited (separate bootstrap
# credential, so the owner is never locked out). Failed attempts are
# NOT written to audit_log (an attacker could flood the log). The
# lockout applies equally to unknown usernames, so it cannot be used
# to probe which usernames exist.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes
_LOGIN_ATTEMPTS = {}  # username -> (failures, locked_until_epoch_or_None)
_PASSWORD_ITERATIONS = 210_000
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Registration-form fields the bot requires to accept a shop (bot.py
# parse_shop_register): removing any of these would break registrations.
REQUIRED_FORM_FIELDS = ("shop_name", "owner_name", "phone", "address", "location")
FORM_FIELD_TYPES = ("text", "phone", "number", "location", "photo", "textarea", "select")
# Max options on one select field (2026-11-13, Min: raise 20 -> 36 so the
# registration form's category select can hold all 36 flat shop types).
MAX_SELECT_OPTIONS = 36


# Shop types (2026-11-12, Min — CORRECTED 10:28: NO groups; each type
# is its own top-level category, e.g. fruit shops do NOT sit under a
# broad "Food & drink" group). The store is a FLAT list; the dashboard
# manages it and the Mini App v17 carries the same flat list hardcoded
# for now (the app reading this list is the D5 wiring, later).
def _slugify(value):
    """Lowercase slug for a shop-type key (English name -> key)."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def _default_shop_types():
    """Seed list (36 flat types) shown when no shop_types row exists."""
    seeded = [
        ("restaurant", "စားသောက်ဆိုင်", "Restaurant"),
        ("tea-shop", "လက်ဖက်ရည်ဆိုင်", "Tea shop"),
        ("coffee-shop", "ကော်ဖီဆိုင်", "Coffee shop"),
        ("drinks", "အအေးဆိုင်", "Drinks / Bubble Tea"),
        ("bakery", "မုန့်ဆိုင်", "Bakery & Snacks"),
        ("fruit", "သစ်သီးဆိုင်", "Fruit shop"),
        ("vegetables", "ဟင်းသီးဟင်းရွက်ဆိုင်", "Vegetables"),
        ("meat", "အသားဆိုင်", "Meat shop"),
        ("fish", "ငါးဆိုင်", "Fish & Seafood"),
        ("grocery", "ကုန်စုံဆိုင်", "Grocery"),
        ("supermarket", "စူပါမားကတ်", "Supermarket"),
        ("clothing", "အဝတ်အထည်ဆိုင်", "Clothing"),
        ("shoes", "ဖိနပ်ဆိုင်", "Shoes"),
        ("electronics", "လျှပ်စစ်ပစ္စည်းဆိုင်", "Electronics"),
        ("phone", "ဖုန်းဆိုင်", "Phone shop"),
        ("computer", "ကွန်ပျူတာဆိုင်", "Computer shop"),
        ("pharmacy", "ဆေးဆိုင်", "Pharmacy"),
        ("cosmetics", "အလှကုန်ဆိုင်", "Cosmetics & Beauty"),
        ("books", "စာအုပ်ဆိုင်", "Bookshop"),
        ("toys", "ကစားစရာဆိုင်", "Toys & Kids"),
        ("home", "အိမ်သုံးပစ္စည်းဆိုင်", "Home & Kitchen"),
        ("flowers", "ပန်းဆိုင်", "Flower shop"),
        ("gold", "ရွှေဆိုင်", "Gold & Jewellery"),
        ("watches", "နာရီဆိုင်", "Watches"),
        ("sports", "အားကစားပစ္စည်းဆိုင်", "Sports goods"),
        ("furniture", "ပရိဘောဂဆိုင်", "Furniture"),
        ("hardware", "ဆောက်လုပ်ရေးပစ္စည်းဆိုင်", "Hardware & Building materials"),
        ("motorbike-parts", "ဆိုင်ကယ်အပိုပစ္စည်းဆိုင်", "Motorbike parts"),
        ("pet", "တိရစ္ဆာန်အစာဆိုင်", "Pet supplies"),
        ("barber", "ဆံပင်ညှပ်ဆိုင်", "Barber & Salon"),
        ("salon", "အလှပြင်ဆိုင်", "Beauty salon"),
        ("laundry", "လျှော်ဖွပ်ဆိုင်", "Laundry"),
        ("photo", "ဓာတ်ပုံဆိုင်", "Photo studio"),
        ("printing", "ပုံနှိပ်ဆိုင်", "Printing"),
        ("gifts", "လက်ဆောင်ဆိုင်", "Gifts"),
        ("other", "အခြား", "Other"),
    ]
    return [{"key": key, "name_my": name_my, "name_en": name_en,
             "enabled": True, "sort_order": i + 1}
            for i, (key, name_my, name_en) in enumerate(seeded)]


def _default_settings():
    """Defaults shown when no dashboard_settings row exists yet."""
    return {
        "global_hours": {"open": "06:00", "close": "22:00", "timezone": "Asia/Yangon"},
        "delivery": {"min_fee_baht": 30, "currency": "THB"},
        "music_volume": 50,
        "contact": {"phone": "09441582958", "telegram": "@SafeDeliveryone"},
        "banners": [],
        "help_videos": [],
        "hero_image": None,
        "promo_slots": [],
        "featured_shop_ids": [],
        "shop_types": _default_shop_types(),
        "registration_form": {"fields": [
            {"id": "shop_name", "label": "ဆိုင်အမည်", "type": "text", "required": True},
            {"id": "owner_name", "label": "ဆိုင်ရှင် နာမည်", "type": "text", "required": True},
            {"id": "phone", "label": "ဖုန်းနံပါတ်", "type": "phone", "required": True},
            {"id": "address", "label": "ဆိုင်လိပ်စာ", "type": "text", "required": True},
            {"id": "category", "label": "အမျိုးအစား", "type": "select", "required": True,
             "options": ["food", "grocery", "other"]},
            {"id": "location", "label": "ဆိုင်တည်နေရာ (GPS pin)", "type": "location", "required": True},
            {"id": "map_link", "label": "Maps လင့်ခ်", "type": "text", "required": False},
            {"id": "note", "label": "မှတ်ချက်", "type": "textarea", "required": False},
            {"id": "shop_message", "label": "ဆိုင်ရှင် ပြောချင်စကား", "type": "textarea", "required": False},
            {"id": "shop_photo", "label": "ဆိုင်ဓာတ်ပုံ", "type": "photo", "required": False},
            {"id": "menu_photo", "label": "မီနူး / ဈေးနှုန်း ဓာတ်ပုံ", "type": "photo", "required": False},
        ]},
    }


def _hash_password(password, salt=None):
    """pbkdf2-SHA256 password hash. Stored format:
    pbkdf2_sha256$iterations$base64(salt)$base64(hash). No plaintext anywhere."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return (f"pbkdf2_sha256${_PASSWORD_ITERATIONS}$"
            f"{base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(digest).decode()}")


def _verify_password(password, stored):
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def _session_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _login_locked(username):
    """True when this username is inside its 15-minute lockout."""
    entry = _LOGIN_ATTEMPTS.get(username)
    if not entry:
        return False
    failures, locked_until = entry
    if locked_until is not None:
        if time.time() < locked_until:
            return True
        del _LOGIN_ATTEMPTS[username]
    return False


def _login_failed(username):
    """Record a failed login; lock the username after 5 failures."""
    failures, locked_until = _LOGIN_ATTEMPTS.get(username, (0, None))
    failures += 1
    if failures >= LOGIN_MAX_FAILURES:
        locked_until = time.time() + LOGIN_LOCKOUT_SECONDS
    _LOGIN_ATTEMPTS[username] = (failures, locked_until)


def _login_succeeded(username):
    """Clear the failure counter after a successful login."""
    _LOGIN_ATTEMPTS.pop(username, None)


def _audit(con, actor_id, action, entity_type, entity_id, details=None):
    """Append one immutable change-log row. Callers pass a live connection
    inside the same transaction as the change itself."""
    con.execute(
        "INSERT INTO audit_log(actor_type,actor_id,action,entity_type,entity_id,details,created_at)"
        " VALUES('admin',?,?,?,?,?,?)",
        (actor_id, action, entity_type,
         str(entity_id) if entity_id is not None else None,
         json.dumps(details or {}, ensure_ascii=False), utc_now()))


def _log_error(error_type, message, order_no=None):
    """Append one row to error_log (the dashboard Errors view + the
    bot's error-alert job read it). Best-effort only — never raises
    into the request. message must be a fixed string, never user
    data. Caps the table at the latest 500 rows."""
    try:
        with connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS error_log (" 
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, " 
                        "message TEXT NOT NULL, order_no TEXT, " 
                        "alerted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            con.execute("INSERT INTO error_log(type,message,order_no,created_at) VALUES(?,?,?,?)",
                        (error_type, message[:500], order_no, utc_now()))
            con.execute("DELETE FROM error_log WHERE id NOT IN "
                        "(SELECT id FROM error_log ORDER BY id DESC LIMIT 500)")
    except Exception:
        pass


def _is_valid_time(value):
    return isinstance(value, str) and bool(_TIME_RE.fullmatch(value))


def _is_valid_date(value):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _is_https_url(value, max_length=2048):
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _is_http_url(value, max_length=2048):
    """Like _is_https_url but also accepts plain http (Maps short links
    and older share links are sometimes http)."""
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _clean_url_list(value, max_items=20):
    """Normalise a shop media URL list: a list of HTTPS URLs, or a JSON
    string encoding one. Returns (json_text_or_None, error). Blank /
    empty -> None (clears the field). Duplicates are dropped."""
    if value is None:
        return None, None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None, None
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None, "must be a list of HTTPS URLs"
    if not isinstance(value, list):
        return None, "must be a list of HTTPS URLs"
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            return None, "must be a list of HTTPS URLs"
        item = item.strip()
        if not item:
            continue
        if len(item) > 2048 or not _is_https_url(item):
            return None, "must be a list of HTTPS URLs"
        if item not in cleaned:
            cleaned.append(item)
    if len(cleaned) > max_items:
        return None, "at most %d links" % max_items
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None, None


def _shop_media_list(value):
    """Parse a stored shop media JSON list back to a list for API
    responses. Tolerates NULL, a blank/bad string (returns []) and an
    already parsed list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# --- Shop media upload (2026-11-13) ---------------------------------------
# Min uploads shop photos and the 1-minute video himself, straight from
# his phone's files — no links, no third-party host. The dashboard
# POSTs the file to /api/admin/media; the API stores it on the same
# server (the VPS he already pays for) and returns an HTTPS URL that
# goes into the shop's photo/menu/video field, so the Mini App can
# show the real picture. Files are validated by their magic bytes
# (never by the filename or the browser's claimed type), stored under
# a server-generated name (no user-controlled path), and served by the
# API itself at /media/<kind>/<name> — no Caddy change is needed.
# Size caps: photos 10 MB (a phone photo is 2-6 MB); video 100 MB (a
# 1-minute phone video is roughly 60-150 MB, so 100 MB fits most
# 1-minute clips — Min can re-record shorter / lower quality if a
# clip is over the cap).
_MEDIA_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_MEDIA_VIDEO_MAX_BYTES = 100 * 1024 * 1024
_MEDIA_SLACK = 64 * 1024  # multipart overhead above the file itself
_MEDIA_KINDS = {"shop": "image", "interior": "image", "menu": "image", "video": "video", "product": "image", "receipt": "image", "promo": "image"}

# Home dishes (2026-11-18, Min: the 21 home-page dishes must be
# manageable from the dashboard, plus as many more as he likes).
# category = one of the Mini App's category chips (minus "all");
# glyph = the food type the 4 derived chips (snacks/rice/noodles/bbq)
# filter on — "bowl" means "no derived chip".
_HOME_DISH_CATEGORIES = {"breakfast", "lunch", "snacks", "rice", "noodles", "bbq", "dinner", "fast", "drinks"}
_HOME_DISH_GLYPHS = {"noodle", "salad", "soup", "grill", "fried", "pizza", "burger", "sushi", "hotpot", "seafood", "dumpling", "bao", "boba", "coffee", "juice", "cake", "icecream", "bowl"}


def _parse_dish_options(raw):
    """Parse stored dish-options JSON. Returns the list, or None when
    empty/invalid (an invalid stored value never breaks the menu)."""
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(val, list) or not val:
        return None
    return val


def _clean_dish_options(value):
    """Validate dish option groups. Returns (canonical_json_or_None, error).
    None/[] clears options. Shape: [{"group": str, "required": bool,
    "multi": bool, "min": int, "max": int, "step": str (optional label
    grouping this group into a step on the Mini App sheet),
    "choices": [{"name": str, "price": number}]}] -- exactly what the
    Mini App option sheet reads (v19.46 single-pick, v19.91 multi-pick).
    Single-pick groups (multi false) take exactly one choice; multi groups
    take between min and max choices. Old rows without multi/min/max are
    read as single-pick by the app; the canonical form always carries them."""
    if value is None:
        return None, None
    if not isinstance(value, list):
        return None, "options must be a list of groups"
    if len(value) == 0:
        return None, None
    if len(value) > 12:
        return None, "at most 12 option groups"
    cleaned = []
    for g in value:
        if not isinstance(g, dict):
            return None, "each option group must be an object"
        label = str(g.get("group") or "").strip()
        if not label or len(label) > 40:
            return None, "each option group needs a name (at most 40 characters)"
        required = g.get("required", True)
        if not isinstance(required, bool):
            return None, "option group 'required' must be true or false"
        multi = g.get("multi", False)
        if not isinstance(multi, bool):
            return None, "option group 'multi' must be true or false"
        step = g.get("step", "")
        if not isinstance(step, str):
            return None, "option group 'step' must be text"
        step = step.strip()
        if len(step) > 40:
            return None, "option group 'step' must be at most 40 characters"
        choices = g.get("choices")
        if not isinstance(choices, list) or not choices or len(choices) > 20:
            return None, "each option group needs 1 to 20 choices"
        cc = []
        for c in choices:
            if not isinstance(c, dict):
                return None, "each option choice must be an object"
            name = str(c.get("name") or "").strip()
            if not name or len(name) > 80:
                return None, "each option choice needs a name (at most 80 characters)"
            price = c.get("price", 0)
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price < 0 or price > 100000):
                return None, "option choice price must be a non-negative number"
            cc.append({"name": name, "price": float(price)})
        n = len(cc)
        if multi:
            mn = g.get("min", 1 if required else 0)
            mx = g.get("max", n)
            if (isinstance(mn, bool) or not isinstance(mn, int)
                    or isinstance(mx, bool) or not isinstance(mx, int)):
                return None, "option group 'min'/'max' must be whole numbers"
            if mn < 0 or mx < mn or mx > n:
                return None, "option group 'min'/'max' must be within 0 and the number of choices"
        else:
            mn, mx = (1 if required else 0), 1
        cleaned.append({"group": label, "required": required, "multi": multi,
                        "min": mn, "max": mx, "step": step, "choices": cc})
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")), None



def _add_missing_home_dish_columns(con):
    """Add the home_dishes options + shop_id columns when missing
    (migrations 016/017). Additive only; existing rows keep NULL
    (no options, no shop link)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "home_dishes" not in tables:
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(home_dishes)")}
    if "options" not in cols:
        con.execute("ALTER TABLE home_dishes ADD COLUMN options TEXT")
    if "shop_id" not in cols:
        con.execute("ALTER TABLE home_dishes ADD COLUMN shop_id INTEGER")


def _home_dish_row(r):
    return {"id": r["id"], "name": r["name"], "description": r["description"],
            "price": r["price"], "currency": r["currency"], "image_url": r["image_url"],
            "category": r["category"], "glyph": r["glyph"], "unit": r["unit"],
            "sort_order": r["sort_order"], "is_available": bool(r["is_available"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "options": _parse_dish_options(r["options"]) if "options" in r.keys() else None,
            "shop_id": r["shop_id"] if "shop_id" in r.keys() else None}


_PROMO_BANNER_COLUMNS = ("id,shop_id,image_url,dish_id,dish_name,"
                         "sort_order,is_active,created_at,updated_at")


def _ensure_shop_promo_banners(con):
    """Create the promo-banners table when missing (idempotent). The
    shared DDL string also creates it at startup; this keeps a handler
    self-sufficient on a DB that predates it."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS shop_promo_banners ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " shop_id INTEGER NOT NULL REFERENCES shops(id),"
        " image_url TEXT NOT NULL, dish_id INTEGER, dish_name TEXT NOT NULL,"
        " sort_order INTEGER NOT NULL DEFAULT 0,"
        " is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_promo_banners_shop"
        " ON shop_promo_banners(shop_id, sort_order, id)")


def _promo_banner_row(r):
    return {"id": r["id"], "shop_id": r["shop_id"], "image_url": r["image_url"],
            "dish_id": r["dish_id"], "dish_name": r["dish_name"],
            "sort_order": r["sort_order"], "is_active": bool(r["is_active"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def _promo_dish_ok(con, shop_id, dish_id):
    """A linked dish must be one of the shop's own home_dishes rows.
    When the home_dishes table predates the shop link, an id match
    alone is enough; when the table is absent entirely, nothing can
    be checked and the id is stored as-is."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "home_dishes" not in tables:
        return True
    cols = {r[1] for r in con.execute("PRAGMA table_info(home_dishes)")}
    if "shop_id" in cols:
        row = con.execute(
            "SELECT id FROM home_dishes WHERE id=? AND shop_id=?",
            (dish_id, shop_id)).fetchone()
    else:
        row = con.execute(
            "SELECT id FROM home_dishes WHERE id=?", (dish_id,)).fetchone()
    return row is not None


def _clean_promo_banner_fields(data, is_new):
    """Validate a banner create/update body. Updates are partial: only
    keys present in the body are returned. Returns (fields, error)."""
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"
    fields = {}
    if "image_url" in data:
        if not _is_https_url(data["image_url"]):
            return None, "image_url must be a valid HTTPS URL"
        fields["image_url"] = data["image_url"]
    elif is_new:
        return None, "image_url is required"
    if "dish_name" in data:
        name = data["dish_name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            return None, "dish_name must be 1-120 characters"
        fields["dish_name"] = name.strip()
    elif is_new:
        return None, "dish_name is required"
    if "dish_id" in data:
        if data["dish_id"] is None:
            fields["dish_id"] = None
        elif isinstance(data["dish_id"], bool) or not isinstance(data["dish_id"], int) or data["dish_id"] < 1:
            return None, "dish_id must be a positive integer"
        else:
            fields["dish_id"] = data["dish_id"]
    if "sort_order" in data:
        if isinstance(data["sort_order"], bool) or not isinstance(data["sort_order"], int):
            return None, "sort_order must be an integer"
        fields["sort_order"] = data["sort_order"]
    if "is_active" in data:
        v = data["is_active"]
        if isinstance(v, bool):
            fields["is_active"] = 1 if v else 0
        elif isinstance(v, int) and v in (0, 1):
            fields["is_active"] = v
        else:
            return None, "is_active must be true or false"
    if is_new and "is_active" not in fields:
        fields["is_active"] = 1
    return fields, None


def _dish_shop_ok(con, shop_id):
    """A dish may only link to an approved shop (active + verified),
    mirroring the product / merchant-account rule."""
    row = con.execute(
        "SELECT id FROM shops WHERE id=? AND status='active' AND verified=1",
        (shop_id,)).fetchone()
    return row is not None


def _clean_home_dish_fields(data, is_new):
    """Validate home-dish fields. Returns (fields, error). `fields` holds
    only the keys the caller should write. On create, name + category
    are required; on update every field is optional."""
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"
    fields = {}
    if "name" in data or is_new:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 200:
            return None, "name is required (at most 200 characters)"
        fields["name"] = name
    if "description" in data:
        description = data.get("description")
        if description is None:
            fields["description"] = None
        elif not isinstance(description, str) or len(description) > 2000:
            return None, "description must be at most 2000 characters"
        else:
            fields["description"] = description
    if "price" in data or is_new:
        price = data.get("price")
        if price is None:
            fields["price"] = None
        elif isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            return None, "price must be a non-negative number or null"
        else:
            fields["price"] = float(price)
    if "currency" in data:
        currency = str(data.get("currency") or "").strip().upper()
        if currency not in {"THB", "MMK"}:
            return None, "Safe Deli currently supports THB and MMK currencies only"
        fields["currency"] = currency
    if "image_url" in data:
        image_url = data.get("image_url")
        if isinstance(image_url, str) and not image_url.strip():
            fields["image_url"] = None
        elif image_url is None:
            fields["image_url"] = None
        elif not isinstance(image_url, str) or len(image_url) > 2048 or not _is_https_url(image_url):
            return None, "image_url must be a valid HTTPS URL or null"
        else:
            fields["image_url"] = image_url
    if "category" in data or is_new:
        category = str(data.get("category") or "").strip()
        if category not in _HOME_DISH_CATEGORIES:
            return None, "category must be one of: " + ", ".join(sorted(_HOME_DISH_CATEGORIES))
        fields["category"] = category
    if "glyph" in data:
        glyph = str(data.get("glyph") or "").strip() or "bowl"
        if glyph not in _HOME_DISH_GLYPHS:
            return None, "glyph is not a known food type"
        fields["glyph"] = glyph
    if "unit" in data:
        unit = str(data.get("unit") or "").strip()
        if not unit:
            unit = "၁ ပွဲ"
        if len(unit) > 40:
            return None, "unit must be at most 40 characters"
        fields["unit"] = unit
    if "sort_order" in data:
        sort_order = data.get("sort_order")
        if isinstance(sort_order, bool) or not isinstance(sort_order, int) or sort_order < 0:
            return None, "sort_order must be a non-negative integer"
        fields["sort_order"] = sort_order
    if "is_available" in data:
        available = data.get("is_available")
        if not isinstance(available, bool):
            return None, "is_available must be true or false"
        fields["is_available"] = 1 if available else 0
    if "options" in data:
        cleaned, err = _clean_dish_options(data.get("options"))
        if err is not None:
            return None, err
        fields["options"] = cleaned
    if "shop_id" in data:
        shop_id = data.get("shop_id")
        if shop_id is None:
            fields["shop_id"] = None
        elif isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id < 1:
            return None, "shop_id must be a shop id or null"
        else:
            fields["shop_id"] = shop_id
    return fields, None
_MEDIA_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp|mp4|webm|mov)$")
_MEDIA_CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "mp4": "video/mp4", "webm": "video/webm",
    "mov": "video/quicktime",
}


class _MediaTooLarge(ValueError):
    """Raised when an upload body exceeds the read cap."""


def _media_dir():
    """Directory for uploaded shop media. Overridable with
    SAFEDELI_MEDIA_DIR (the production service sets it to
    /root/safedeli/media, outside the code dir so redeploys keep the
    files). Default: a media/ folder next to the database file, so
    tests (which use a temp DB) get a temp media dir automatically."""
    raw = (os.environ.get("SAFEDELI_MEDIA_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(DB_PATH).parent / "media"


def _public_base_url():
    """Public base URL for media links. Overridable with
    SAFEDELI_PUBLIC_BASE_URL; the production default is the dashboard
    domain. The Mini App needs an absolute URL (it runs on GitHub
    Pages, a different origin)."""
    return (os.environ.get("SAFEDELI_PUBLIC_BASE_URL") or "https://safedeli.duckdns.org").rstrip("/")


def _detect_media(data):
    """Detect an uploaded file by its magic bytes. Returns
    (media_type, extension, content_type) or None. The filename and
    the browser's claimed Content-Type are never trusted."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return ("image", "jpg", "image/jpeg")
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image", "png", "image/png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image", "webp", "image/webp")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        # MP4 and QuickTime MOV share the ftyp box; the brand tells
        # them apart ("qt  " = QuickTime, anything else = MP4).
        if data[8:12] == b"qt  ":
            return ("video", "mov", "video/quicktime")
        return ("video", "mp4", "video/mp4")
    if len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3":
        return ("video", "webm", "video/webm")
    return None


def _parse_multipart(environ, max_bytes):
    """Parse a multipart/form-data body (stdlib only; the cgi module
    is deprecated). Returns (fields, files) where fields maps a text
    part name to its string value and files maps a file part name to
    (filename, content_type, data). Raises ValueError on a malformed
    body and _MediaTooLarge when the declared body exceeds max_bytes
    (the body is not read at all in that case)."""
    content_type = (environ.get("CONTENT_TYPE") or "").strip()
    media_type, _, params = content_type.partition(";")
    if media_type.strip().lower() != "multipart/form-data":
        raise ValueError("Content-Type must be multipart/form-data")
    boundary = None
    for part in params.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
    if not boundary or len(boundary) > 70:
        raise ValueError("multipart boundary is missing or invalid")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (ValueError, TypeError):
        raise ValueError("Invalid Content-Length")
    if length < 1:
        raise ValueError("Request body is missing")
    if length > max_bytes:
        raise _MediaTooLarge("Request body is too large")
    raw = environ["wsgi.input"].read(length)
    if len(raw) != length:
        raise ValueError("Request body is incomplete")
    delim = b"--" + boundary.encode("ascii")
    chunks = raw.split(delim)
    fields, files = {}, {}
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, sep, body = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" not in line:
                continue
            key, _, val = line.partition(b":")
            headers[key.strip().lower().decode("latin-1")] = val.strip().decode("latin-1")
        disp = headers.get("content-disposition", "")
        m = re.search(r'name="([^"]*)"', disp)
        if not m:
            continue
        name = m.group(1)
        fm = re.search(r'filename="([^"]*)"', disp)
        if fm is not None:
            files[name] = (fm.group(1), headers.get("content-type", ""), body)
        else:
            try:
                fields[name] = body.decode("utf-8")
            except UnicodeDecodeError:
                fields[name] = body.decode("utf-8", "replace")
    return fields, files


def _validate_settings_patch(patch):
    """Validate a PUT /api/admin/settings body. Returns (cleaned, error).
    Only whitelisted top-level keys are accepted; unknown keys are rejected."""
    if not isinstance(patch, dict) or not patch:
        return None, "No settings provided"
    allowed = {"global_hours", "delivery", "banners", "registration_form",
               "hero_image", "promo_slots", "featured_shop_ids", "contact",
               "music_volume", "help_videos"}
    unknown = sorted(set(patch) - allowed)
    if unknown:
        return None, "Unknown settings key: " + ", ".join(unknown)
    cleaned = {}
    if "contact" in patch:
        contact = patch["contact"]
        if not isinstance(contact, dict):
            return None, "contact must be an object"
        phone = contact.get("phone")
        if not isinstance(phone, str) or not phone.strip() or len(phone.strip()) > 40:
            return None, "contact phone is required (max 40 characters)"
        phone = phone.strip()
        if not re.fullmatch(r"\+?[0-9][0-9 \-]{7,39}", phone):
            return None, "contact phone must be a phone number"
        telegram = contact.get("telegram")
        if not isinstance(telegram, str) or not telegram.strip() or len(telegram.strip()) > 40:
            return None, "contact telegram is required (max 40 characters)"
        telegram = telegram.strip()
        if not telegram.startswith("@") or len(telegram) < 2:
            return None, "contact telegram must start with @"
        cleaned["contact"] = {"phone": phone, "telegram": telegram}
    if "global_hours" in patch:
        hours = patch["global_hours"]
        if not isinstance(hours, dict):
            return None, "global_hours must be an object"
        open_time, close_time = hours.get("open"), hours.get("close")
        if not _is_valid_time(open_time) or not _is_valid_time(close_time):
            return None, "Opening and closing times must use HH:MM"
        if open_time == close_time:
            return None, "Opening and closing times must differ"
        if open_time > close_time:
            return None, "Opening time must be before closing time"
        cleaned["global_hours"] = {"open": open_time, "close": close_time,
                                    "timezone": "Asia/Yangon"}
    if "delivery" in patch:
        delivery = patch["delivery"]
        if not isinstance(delivery, dict):
            return None, "delivery must be an object"
        fee = delivery.get("min_fee_baht")
        if isinstance(fee, bool) or not isinstance(fee, (int, float)) or not math.isfinite(fee) or fee < 0:
            return None, "min_fee_baht must be a non-negative number"
        cleaned["delivery"] = {"min_fee_baht": round(float(fee), 2), "currency": "THB"}
    if "music_volume" in patch:
        vol = patch["music_volume"]
        if (isinstance(vol, bool) or not isinstance(vol, (int, float))
                or not math.isfinite(vol) or vol < 0 or vol > 100):
            return None, "music_volume must be a number from 0 to 100"
        cleaned["music_volume"] = int(vol)
    if "banners" in patch:
        banners = patch["banners"]
        if not isinstance(banners, list) or len(banners) > 20:
            return None, "banners must be a list of at most 20 items"
        cleaned_banners = []
        for item in banners:
            if not isinstance(item, dict):
                return None, "Each banner must be an object"
            image_url = item.get("image_url")
            if not _is_https_url(image_url):
                return None, "Each banner image_url must be a valid HTTPS URL"
            link_url = item.get("link_url")
            if link_url is not None and link_url != "" and not _is_https_url(link_url):
                return None, "Each banner link_url must be a valid HTTPS URL"
            title = item.get("title", "")
            if not isinstance(title, str) or len(title) > 120:
                return None, "Each banner title must be at most 120 characters"
            sort_order = item.get("sort_order", 0)
            if isinstance(sort_order, bool) or not isinstance(sort_order, int):
                return None, "Each banner sort_order must be an integer"
            cleaned_banners.append({
                "image_url": image_url,
                "link_url": link_url or "",
                "title": title,
                "sort_order": sort_order,
                "is_active": bool(item.get("is_active", True)),
            })
        cleaned["banners"] = sorted(cleaned_banners, key=lambda b: b["sort_order"])
    if "help_videos" in patch:
        videos = patch["help_videos"]
        if not isinstance(videos, list) or len(videos) > 20:
            return None, "help_videos must be a list of at most 20 items"
        cleaned_videos = []
        for item in videos:
            if not isinstance(item, dict):
                return None, "Each help video must be an object"
            video_url = item.get("video_url")
            if not _is_https_url(video_url):
                return None, "Each help video video_url must be a valid HTTPS URL"
            title = item.get("title", "")
            if not isinstance(title, str) or len(title) > 120:
                return None, "Each help video title must be at most 120 characters"
            sort_order = item.get("sort_order", 0)
            if isinstance(sort_order, bool) or not isinstance(sort_order, int):
                return None, "Each help video sort_order must be an integer"
            cleaned_videos.append({
                "video_url": video_url,
                "title": title,
                "sort_order": sort_order,
                "is_active": bool(item.get("is_active", True)),
            })
        cleaned["help_videos"] = sorted(cleaned_videos, key=lambda v: v["sort_order"])
    if "hero_image" in patch:
        # Media view hero (same style as the existing banner key):
        # one object, or null to clear it.
        hero = patch["hero_image"]
        if hero is None:
            cleaned["hero_image"] = None
        else:
            if not isinstance(hero, dict):
                return None, "hero_image must be an object"
            image_url = hero.get("image_url")
            if not _is_https_url(image_url):
                return None, "hero_image image_url must be a valid HTTPS URL"
            title = hero.get("title", "")
            if not isinstance(title, str) or len(title) > 120:
                return None, "hero_image title must be at most 120 characters"
            link = hero.get("link", "")
            if link not in (None, "") and not _is_https_url(link):
                return None, "hero_image link must be a valid HTTPS URL or empty"
            cleaned["hero_image"] = {"image_url": image_url, "title": title,
                                     "link": link or ""}
    if "promo_slots" in patch:
        slots = patch["promo_slots"]
        if not isinstance(slots, list) or len(slots) > 8:
            return None, "promo_slots must be a list of at most 8 items"
        cleaned_slots = []
        for item in slots:
            if not isinstance(item, dict):
                return None, "Each promo slot must be an object"
            image_url = item.get("image_url")
            if not _is_https_url(image_url):
                return None, "Each promo slot image_url must be a valid HTTPS URL"
            title = item.get("title", "")
            if not isinstance(title, str) or len(title) > 120:
                return None, "Each promo slot title must be at most 120 characters"
            link = item.get("link", "")
            if link not in (None, "") and not _is_https_url(link):
                return None, "Each promo slot link must be a valid HTTPS URL or empty"
            cleaned_slots.append({"title": title, "image_url": image_url,
                                  "link": link or "",
                                  "active": bool(item.get("active", True))})
        cleaned["promo_slots"] = cleaned_slots
    if "featured_shop_ids" in patch:
        ids = patch["featured_shop_ids"]
        if (not isinstance(ids, list) or len(ids) > 20
                or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids)
                or len(set(ids)) != len(ids)):
            return None, "featured_shop_ids must be unique positive integers (max 20)"
        cleaned["featured_shop_ids"] = list(ids)
    if "registration_form" in patch:
        form = patch["registration_form"]
        if not isinstance(form, dict) or not isinstance(form.get("fields"), list):
            return None, "registration_form.fields must be a list"
        fields = form["fields"]
        if not fields or len(fields) > 30:
            return None, "registration_form.fields must have 1 to 30 entries"
        seen, cleaned_fields = set(), []
        for field in fields:
            if not isinstance(field, dict):
                return None, "Each form field must be an object"
            fid = field.get("id")
            if not isinstance(fid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", fid):
                return None, "Each form field id must be a lowercase slug"
            if fid in seen:
                return None, "Duplicate form field id: " + fid
            seen.add(fid)
            label = field.get("label")
            if not isinstance(label, str) or not label.strip() or len(label) > 120:
                return None, "Each form field label is required (max 120 characters)"
            ftype = field.get("type")
            if ftype not in FORM_FIELD_TYPES:
                return None, "Invalid form field type: " + str(ftype)
            options = field.get("options")
            if ftype == "select":
                if (not isinstance(options, list) or not options or len(options) > MAX_SELECT_OPTIONS
                        or any(not isinstance(o, str) or not o.strip() or len(o) > 80 for o in options)):
                    return None, f"Select fields need 1 to {MAX_SELECT_OPTIONS} options"
            elif options is not None:
                return None, "options are only allowed for select fields"
            cleaned_fields.append({
                "id": fid, "label": label.strip(), "type": ftype,
                "required": bool(field.get("required", False)),
                **({"options": options} if ftype == "select" else {}),
            })
        # The bot rejects registrations missing these fields; the dashboard
        # must not be able to remove them.
        missing = [f for f in REQUIRED_FORM_FIELDS
                   if f not in {c["id"] for c in cleaned_fields if c["required"]}]
        if missing:
            return None, "Required form fields must stay required: " + ", ".join(missing)
        cleaned["registration_form"] = {"fields": cleaned_fields}
    return cleaned, None


def _validate_shop_hours(body):
    """Validate a PUT /api/admin/shops/{id}/hours body. Returns (cleaned, error)."""
    if not isinstance(body, dict):
        return None, "Request body must be an object"
    unknown = sorted(set(body) - {"open_time", "close_time", "open_days", "closed_dates"})
    if unknown:
        return None, "Unknown shop hours key: " + ", ".join(unknown)
    open_time, close_time = body.get("open_time"), body.get("close_time")
    if open_time is not None and not _is_valid_time(open_time):
        return None, "open_time must use HH:MM or be null (use global hours)"
    if close_time is not None and not _is_valid_time(close_time):
        return None, "close_time must use HH:MM or be null (use global hours)"
    if (open_time is None) != (close_time is None):
        return None, "open_time and close_time must be set together or both null"
    if open_time is not None and open_time == close_time:
        return None, "Opening and closing times must differ"
    open_days = body.get("open_days")
    if open_days is not None:
        if (not isinstance(open_days, list) or not open_days
                or any(isinstance(d, bool) or not isinstance(d, int) or d < 0 or d > 6 for d in open_days)
                or len(set(open_days)) != len(open_days)):
            return None, "open_days must be unique weekday numbers 0 (Sunday) to 6 (Saturday)"
    closed_dates = body.get("closed_dates")
    if closed_dates is not None:
        if (not isinstance(closed_dates, list) or len(closed_dates) > 366
                or any(not _is_valid_date(d) for d in closed_dates)
                or len(set(closed_dates)) != len(closed_dates)):
            return None, "closed_dates must be unique valid YYYY-MM-DD dates (max 366)"
    return {
        "open_time": open_time,
        "close_time": close_time,
        "open_days": open_days if open_days is not None else [1, 2, 3, 4, 5, 6, 0],
        "closed_dates": closed_dates if closed_dates is not None else [],
    }, None


def _validate_shop_type(data, existing, is_new, current_key=None):
    """Validate a shop-type body. Returns (cleaned, error).
    POST (is_new): key auto-generated from the English name when omitted;
    Burmese name is required. PUT: only the fields present are validated;
    the key itself can never change. There is NO group field (flat list,
    Min's correction 2026-11-12 10:28)."""
    if not isinstance(data, dict):
        return None, "Request body must be an object"
    unknown = sorted(set(data) - {"key", "name_my", "name_en", "enabled", "sort_order"})
    if unknown:
        return None, "Unknown shop type key: " + ", ".join(unknown)
    cleaned = {}
    if is_new:
        key = data.get("key")
        if key is None:
            key = _slugify(data.get("name_en"))
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9\-]{0,39}", key):
            return None, "key must be a lowercase slug (letters, numbers, hyphens)"
        if any(t.get("key") == key for t in existing):
            return None, "A shop type with this key already exists: " + key
        cleaned["key"] = key
    elif "key" in data and data["key"] != current_key:
        return None, "key cannot be changed"
    if is_new or "name_my" in data:
        name_my = data.get("name_my")
        if not isinstance(name_my, str) or not name_my.strip() or len(name_my.strip()) > 80:
            return None, "Burmese name is required (max 80 characters)"
        cleaned["name_my"] = name_my.strip()
    if "name_en" in data:
        name_en = data["name_en"]
        if not isinstance(name_en, str) or len(name_en.strip()) > 80:
            return None, "English name must be at most 80 characters"
        cleaned["name_en"] = name_en.strip()
    elif is_new:
        cleaned["name_en"] = ""
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return None, "enabled must be true or false"
        cleaned["enabled"] = data["enabled"]
    elif is_new:
        cleaned["enabled"] = True
    if "sort_order" in data:
        order = data["sort_order"]
        if isinstance(order, bool) or not isinstance(order, int):
            return None, "sort_order must be an integer"
        cleaned["sort_order"] = order
    elif is_new:
        cleaned["sort_order"] = max((t.get("sort_order", 0) for t in existing), default=0) + 1
    return cleaned, None


def _save_setting(con, key, value, actor_id):
    """Upsert one dashboard_settings row (same statement the PUT
    /api/admin/settings handler uses)."""
    con.execute("INSERT INTO dashboard_settings(key,value_json,updated_at,updated_by)"
                " VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " value_json=excluded.value_json,"
                " updated_at=excluded.updated_at,"
                " updated_by=excluded.updated_by",
                (key, json.dumps(value, ensure_ascii=False), utc_now(), actor_id))


def _shop_type_in_use(con, shop_type):
    """True when any shop or pending registration stores this type.
    Categories are free text, so match the key, the Burmese name and
    the English name (English compared case-insensitively). Legacy
    values that match no type key (e.g. 'food') use no type."""
    names = {shop_type["key"].lower(), shop_type["name_my"],
             (shop_type.get("name_en") or "").lower()}
    for table in ("shops", "shop_registrations"):
        if not _table_exists(con, table):
            continue
        for row in con.execute(f"SELECT category FROM {table}").fetchall():
            cat = (row["category"] or "").strip()
            if cat and (cat in names or cat.lower() in names):
                return True
    return False


def _validate_shop_body(data, types, is_new):
    """Validate a manual add-shop (POST) or edit-shop (PUT) body.
    Returns (cleaned, error). Same required fields as a registration
    (name, category, address, phone); the category must be an ENABLED
    shop-type key from the dashboard list. GPS pin is optional here —
    a shop without a pin opens closed (is_open=0) until the owner
    adds the pin. Status may only move active <-> suspended."""
    if not isinstance(data, dict):
        return None, "Request body must be an object"
    allowed = {"name", "category", "address", "phone", "latitude", "longitude",
               "status", "is_open", "area", "photo_url",
               "maps_url", "video_url", "photo_urls", "interior_photo_urls", "menu_url"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        return None, "Unknown shop key: " + ", ".join(unknown)
    enabled_keys = {t["key"] for t in types if t.get("enabled")}
    cleaned = {}
    if is_new or "name" in data:
        name = data.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
            return None, "Shop name is required (max 160 characters)"
        cleaned["name"] = name.strip()
    if is_new or "category" in data:
        category = data.get("category")
        if not isinstance(category, str) or category.strip() not in enabled_keys:
            return None, "Category must be an enabled shop type"
        cleaned["category"] = category.strip()
    if is_new or "address" in data:
        address = data.get("address")
        if not isinstance(address, str) or not address.strip() or len(address.strip()) > 500:
            return None, "Shop address is required (max 500 characters)"
        cleaned["address"] = address.strip()
    if is_new or "phone" in data:
        phone = data.get("phone")
        if not isinstance(phone, str) or not phone.strip() or len(phone.strip()) > 40:
            return None, "Shop phone is required (max 40 characters)"
        cleaned["phone"] = phone.strip()
    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is not None or lon is not None:
        try:
            lat, lon = float(lat), float(lon)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError()
        except (TypeError, ValueError):
            return None, "Invalid GPS coordinates"
        cleaned["latitude"] = lat
        cleaned["longitude"] = lon
    if "status" in data:
        if data["status"] not in ("active", "suspended"):
            return None, "Shop status can only be active or suspended"
        cleaned["status"] = data["status"]
    if "is_open" in data:
        if data["is_open"] not in (0, 1, True, False):
            return None, "is_open must be 0 or 1"
        cleaned["is_open"] = 1 if data["is_open"] else 0
    if "area" in data:
        # Optional city/area label (e.g. Tachileik). Null/blank clears it.
        area = data["area"]
        if area is not None and (not isinstance(area, str) or len(area.strip()) > 120):
            return None, "area must be a string of at most 120 characters"
        cleaned["area"] = area.strip() if isinstance(area, str) and area.strip() else None
    if "photo_url" in data:
        # Optional shop photo URL. Null/blank clears it; a real URL must
        # be HTTPS. The dashboard upload (POST /api/admin/media,
        # kind=shop) stores the photo and returns this URL.
        photo_url = data["photo_url"]
        if isinstance(photo_url, str) and not photo_url.strip():
            photo_url = None
        if photo_url is not None and (not isinstance(photo_url, str)
                                      or len(photo_url) > 2048
                                      or not _is_https_url(photo_url)):
            return None, "photo_url must be a valid HTTPS URL or null"
        cleaned["photo_url"] = photo_url
    if "maps_url" in data:
        # Optional Google Maps link. Null/blank clears it; a real link
        # may be http or https (Maps short links are sometimes http).
        # On create, when no link is given but a GPS pin is set, the
        # link is auto-built from the pin below.
        maps_url = data["maps_url"]
        if isinstance(maps_url, str) and not maps_url.strip():
            maps_url = None
        if maps_url is not None and (not isinstance(maps_url, str)
                                     or len(maps_url) > 2048
                                     or not _is_http_url(maps_url)):
            return None, "maps_url must be a valid Maps link (http or https)"
        cleaned["maps_url"] = maps_url.strip() if isinstance(maps_url, str) else maps_url
    if "video_url" in data:
        # Optional 1-minute shop video guide link. The dashboard upload
        # (POST /api/admin/media, kind=video) stores the video and
        # returns this URL. Null/blank clears it.
        video_url = data["video_url"]
        if isinstance(video_url, str) and not video_url.strip():
            video_url = None
        if video_url is not None and (not isinstance(video_url, str)
                                      or len(video_url) > 2048
                                      or not _is_https_url(video_url)):
            return None, "video_url must be a valid HTTPS URL or null"
        cleaned["video_url"] = video_url
    if "menu_url" in data:
        # Optional menu photo/page link. The dashboard upload (POST
        # /api/admin/media, kind=menu) stores the menu photo and
        # returns this URL. Null/blank clears it.
        menu_url = data["menu_url"]
        if isinstance(menu_url, str) and not menu_url.strip():
            menu_url = None
        if menu_url is not None and (not isinstance(menu_url, str)
                                     or len(menu_url) > 2048
                                     or not _is_https_url(menu_url)):
            return None, "menu_url must be a valid HTTPS URL or null"
        cleaned["menu_url"] = menu_url
    for _list_key in ("photo_urls", "interior_photo_urls"):
        # Optional extra photo links (one HTTPS URL per line in the
        # form). Stored as a JSON array; blank/empty clears the field.
        if _list_key in data:
            cleaned_list, _list_err = _clean_url_list(data[_list_key])
            if _list_err:
                return None, "%s %s" % (_list_key, _list_err)
            cleaned[_list_key] = cleaned_list
    if (is_new and "maps_url" not in data
            and "latitude" in cleaned and "longitude" in cleaned):
        # No Maps link given on create but a pin is set: build the link
        # from the pin (same shape as the order location links). On
        # edit the link is only changed when explicitly sent, so an
        # existing custom link is never clobbered here.
        cleaned["maps_url"] = "https://maps.google.com/?q=%s,%s" % (
            cleaned["latitude"], cleaned["longitude"])
    return cleaned, None


# --- Premium Dashboard v2 helpers (2026-11-11) ---------------------------
# Days convention (verified against bot.py _parse_shop_hours and the
# Mini App shopOpenStatus in safedeli-mini-app-v6.html): weekdays are
# JS getDay numbers, Sunday=0 .. Saturday=6. Python's
# datetime.weekday() is Monday=0 .. Sunday=6, so
# js_day = (weekday + 1) % 7. The shop_hours open_days validation
# above uses the same convention (0 Sunday .. 6 Saturday).
YANGON_TZ = ZoneInfo("Asia/Yangon")
ACTIVE_DELIVERY_STATUSES = ("assigned", "accepted", "picked_up", "delivering")
# Finance rule (read from the ledger code itself, not guessed): POST
# /api/admin/ledger always inserts status='confirmed', and the ledger
# table allows pending/confirmed/failed/reversed. Only
# status='confirmed' rows are real money: 'pending' is not yet
# confirmed, 'failed' never landed, and 'reversed' is already undone
# by its reversal entry. Mixed currencies are summed per currency and
# the dominant currency — the one with the largest confirmed in+out
# volume, ties broken alphabetically — is reported; other currencies
# are ignored, never converted (no exchange rate is stored anywhere).
# Today/month use the Asia/Yangon calendar date and the existing
# substr(created_at,1,10) pattern: stored timestamps keep their local
# date prefix, so the date bucket is the stored prefix.


def _minutes_from_hhmm(value):
    if not _is_valid_time(value):
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _is_open_at(open_time, close_time, open_days, closed_dates, now):
    """True when Yangon datetime `now` falls inside the hours window.
    open_days uses the JS getDay convention (0=Sunday..6=Saturday).
    closed_dates are YYYY-MM-DD Yangon dates that close the shop for
    the whole day. Overnight hours (close <= open) span midnight: the
    shop is open from open_time today until close_time tomorrow, so a
    time after midnight counts when YESTERDAY was an open day (and
    yesterday was not a closed date). A closed date today always wins."""
    if open_time is None or close_time is None:
        return False
    open_min = _minutes_from_hhmm(open_time)
    close_min = _minutes_from_hhmm(close_time)
    if open_min is None or close_min is None or open_min == close_min:
        return False
    today = now.strftime("%Y-%m-%d")
    if today in (closed_dates or []):
        return False
    days = set(open_days or [])
    js_today = (now.weekday() + 1) % 7
    mins = now.hour * 60 + now.minute
    if close_min > open_min:
        return js_today in days and open_min <= mins < close_min
    # Overnight: open today from open_time, or still open after
    # midnight from yesterday's opening.
    if js_today in days and mins >= open_min:
        return True
    yesterday = now - timedelta(days=1)
    if yesterday.strftime("%Y-%m-%d") in (closed_dates or []):
        return False
    js_yesterday = (yesterday.weekday() + 1) % 7
    return js_yesterday in days and mins < close_min


def _global_hours(con):
    """(open, close) from dashboard_settings key 'global_hours' — the
    exact key the settings code uses. Falls back to the 06:00-22:00
    default when unset or unreadable."""
    try:
        row = con.execute(
            "SELECT value_json FROM dashboard_settings WHERE key='global_hours'").fetchone()
        if row:
            value = json.loads(row["value_json"])
            if (isinstance(value, dict) and _is_valid_time(value.get("open"))
                    and _is_valid_time(value.get("close"))):
                return value["open"], value["close"]
    except (ValueError, TypeError, sqlite3.Error):
        pass
    return "06:00", "22:00"


def _shop_hours_map(con):
    """{shop_id: {open_time, close_time, open_days, closed_dates}}.
    A row with NULL open/close means 'use global hours'. Malformed
    JSON falls back to the all-days / no-closed-dates defaults."""
    result = {}
    try:
        rows = con.execute(
            "SELECT shop_id, open_time, close_time, open_days, closed_dates"
            " FROM shop_hours").fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        try:
            open_days = json.loads(row["open_days"]) if row["open_days"] else [1, 2, 3, 4, 5, 6, 0]
        except (ValueError, TypeError):
            open_days = [1, 2, 3, 4, 5, 6, 0]
        try:
            closed_dates = json.loads(row["closed_dates"]) if row["closed_dates"] else []
        except (ValueError, TypeError):
            closed_dates = []
        if not isinstance(open_days, list):
            open_days = [1, 2, 3, 4, 5, 6, 0]
        if not isinstance(closed_dates, list):
            closed_dates = []
        result[row["shop_id"]] = {"open_time": row["open_time"],
                                  "close_time": row["close_time"],
                                  "open_days": open_days,
                                  "closed_dates": closed_dates}
    return result


def _shop_is_open_now(shop, hours_map, global_open, global_close, now):
    """A shop counts as open now only when status='active' AND
    verified=1 AND the current Yangon time falls inside its hours:
    its own shop_hours row when one exists with open/close set, else
    the global hours. A shop with no hours anywhere falls back to the
    global default 06:00-22:00 (what _global_hours returns when no
    global_hours setting exists)."""
    if shop["status"] != "active" or shop["verified"] != 1:
        return False
    hours = hours_map.get(shop["id"])
    if hours and hours.get("open_time") and hours.get("close_time"):
        return _is_open_at(hours["open_time"], hours["close_time"],
                           hours["open_days"], hours["closed_dates"], now)
    return _is_open_at(global_open, global_close, [1, 2, 3, 4, 5, 6, 0], [], now)


def _parse_new_days(query):
    """new_days query param: default 7, clamped to 1-30. A
    non-integer value is invalid (400); an out-of-range integer is
    clamped, not rejected."""
    raw = (query.get("new_days", [""])[0] or "").strip()
    if not raw:
        return 7, None
    try:
        value = int(raw)
    except ValueError:
        return None, "new_days must be an integer"
    return min(30, max(1, value)), None


def _parse_created_at(value):
    """Parse a stored ISO timestamp into an aware Yangon datetime.
    Naive timestamps are treated as Yangon local time (that is how
    the bot stores them); aware ones are converted. None when
    unparseable — never a crash, never an invented date."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=YANGON_TZ)
    return parsed.astimezone(YANGON_TZ)


def _shop_is_new(created_at, new_days, now):
    """True when the shop was created within the last `new_days`
    days (age between 0 and N days, measured in Yangon time)."""
    created = _parse_created_at(created_at)
    if created is None:
        return False
    age_seconds = (now - created).total_seconds()
    return 0 <= age_seconds <= new_days * 86400


def _table_exists(con, name):
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _active_rider_chat_ids(con):
    """chat_ids with an active delivery (assigned/accepted/picked_up/
    delivering). Tolerates a missing deliveries table."""
    if not _table_exists(con, "deliveries"):
        return set()
    placeholders = ",".join("?" for _ in ACTIVE_DELIVERY_STATUSES)
    rows = con.execute(
        f"SELECT DISTINCT rider_chat_id FROM deliveries WHERE status IN ({placeholders})"
        " AND rider_chat_id IS NOT NULL", ACTIVE_DELIVERY_STATUSES).fetchall()
    return {r[0] for r in rows}


def _rider_counts(con):
    """(available, counts, rider rows). Riders are created by the BOT,
    not by api.py: when the riders table does not exist yet the counts
    are all 0 and available is False. Status values are the bot's
    exact DDL values: 'pending' / 'approved' / 'suspended'. on_duty =
    approved AND has an active delivery; resting = approved with no
    active delivery."""
    if not _table_exists(con, "riders"):
        return False, {"total": 0, "on_duty": 0, "resting": 0,
                       "suspended": 0, "pending": 0}, []
    rows = con.execute("SELECT * FROM riders").fetchall()
    active = _active_rider_chat_ids(con)
    counts = {"total": len(rows), "on_duty": 0, "resting": 0,
              "suspended": 0, "pending": 0}
    for row in rows:
        status = row["status"]
        if status == "approved":
            if row["chat_id"] is not None and row["chat_id"] in active:
                counts["on_duty"] += 1
            else:
                counts["resting"] += 1
        elif status == "suspended":
            counts["suspended"] += 1
        elif status == "pending":
            counts["pending"] += 1
    return True, counts, rows


def _ledger_finance(con, today, month_prefix, chart_dates):
    """Confirmed-only finance (see the module comment above). Returns
    (finance dict, {date: income}, currency). No confirmed rows: all
    zeros, currency THB."""
    try:
        rows = con.execute(
            "SELECT direction, currency, amount, substr(created_at,1,10) AS day"
            " FROM financial_ledger WHERE status='confirmed'").fetchall()
    except sqlite3.Error:
        rows = []
    per_currency = {}
    for row in rows:
        currency = str(row["currency"] or "").upper()
        entry = per_currency.setdefault(currency, {"in": 0.0, "out": 0.0, "volume": 0.0})
        amount = float(row["amount"] or 0)
        if row["direction"] == "in":
            entry["in"] += amount
        elif row["direction"] == "out":
            entry["out"] += amount
        entry["volume"] += amount
    if not per_currency:
        return ({"income_today": 0, "income_month": 0, "expense_today": 0,
                 "expense_month": 0, "net_today": 0, "net_month": 0,
                 "currency": "THB"},
                {d: 0 for d in chart_dates}, "THB")
    currency = sorted(per_currency, key=lambda c: (-per_currency[c]["volume"], c))[0]

    def _sum(direction, day=None, month=None):
        total = 0.0
        for row in rows:
            if str(row["currency"] or "").upper() != currency:
                continue
            if row["direction"] != direction:
                continue
            if day is not None and row["day"] != day:
                continue
            if month is not None and not str(row["day"] or "").startswith(month):
                continue
            total += float(row["amount"] or 0)
        return round(total, 2)

    income_today = _sum("in", day=today)
    expense_today = _sum("out", day=today)
    income_month = _sum("in", month=month_prefix)
    expense_month = _sum("out", month=month_prefix)
    finance = {"income_today": income_today, "income_month": income_month,
               "expense_today": expense_today, "expense_month": expense_month,
               "net_today": round(income_today - expense_today, 2),
               "net_month": round(income_month - expense_month, 2),
               "currency": currency}
    return finance, {d: _sum("in", day=d) for d in chart_dates}, currency


def _registration_completeness(reg):
    """Owner's checklist for one shop registration. 8 required items;
    complete only when all 8 are done. Never invents data: a NULL or
    empty stored field means NOT done, and malformed JSON means NOT
    done, never a crash. The video item is optional and never blocks
    complete. Labels are Burmese (label_my). `reg` may be a sqlite
    Row or a dict; missing columns read as None via _row_get."""
    def _non_empty(value):
        return isinstance(value, str) and bool(value.strip())

    items = []

    def add(key, label_my, done, optional=False):
        item = {"key": key, "label_my": label_my, "done": bool(done)}
        if optional:
            item["optional"] = True
        items.append(item)

    add("shop_name", "ဆိုင်အမည်", _non_empty(_row_get(reg, "shop_name")))
    add("owner_name", "ဆိုင်ရှင်အမည်", _non_empty(_row_get(reg, "owner_name")))
    add("phone", "ဖုန်းနံပါတ်", _non_empty(_row_get(reg, "phone")))
    add("address", "ဆိုင်လိပ်စာ", _non_empty(_row_get(reg, "address")))
    add("location_pin", "တည်နေရာ (GPS pin)",
        _row_get(reg, "location_lat") is not None
        and _row_get(reg, "location_lon") is not None)
    # photos_3: the 3-photo tick — storefront with name / interior /
    # owner smiling — confirmed by photos_confirmed == 1.
    add("photos_3", "ဓာတ်ပုံ ၃ ပုံ (ဆိုင်ရှေ့ / ဆိုင်အတွင်း / ဆိုင်ရှင်)",
        _row_get(reg, "photos_confirmed") == 1)
    menu_done = False
    raw_menu = _row_get(reg, "menu_json")
    if isinstance(raw_menu, str) and raw_menu.strip():
        try:
            menu = json.loads(raw_menu)
        except (ValueError, TypeError):
            menu = None
        if isinstance(menu, dict):
            menu_items = menu.get("items")
            if isinstance(menu_items, list) and menu_items:
                menu_done = all(
                    isinstance(item, dict)
                    and _non_empty(item.get("name"))
                    and isinstance(item.get("price_baht"), (int, float))
                    and not isinstance(item.get("price_baht"), bool)
                    and float(item["price_baht"]) > 0
                    for item in menu_items)
    add("menu_prices", "မီနူး နှင့် ဈေးနှုန်း", menu_done)
    hours_done = False
    raw_hours = _row_get(reg, "hours_json")
    if isinstance(raw_hours, str) and raw_hours.strip():
        try:
            hours = json.loads(raw_hours)
        except (ValueError, TypeError):
            hours = None
        if isinstance(hours, dict):
            days = hours.get("days")
            days_ok = (isinstance(days, list) and len(days) > 0 and all(
                (isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6)
                or (isinstance(d, str) and d.isdigit() and 0 <= int(d) <= 6)
                for d in days))
            hours_done = (_is_valid_time(hours.get("open"))
                          and _is_valid_time(hours.get("close")) and days_ok)
    add("hours", "ဖွင့်ချိန်", hours_done)
    add("video", "လမ်းညွှန်ဗီဒီယို",
        _non_empty(_row_get(reg, "video_file_id")), optional=True)
    missing = [item["key"] for item in items
               if not item["done"] and not item.get("optional")]
    return {"items": items, "complete": not missing, "missing": missing}


def _parse_recorded_at(value):
    """Parse a rider_positions recorded_at into an aware UTC datetime;
    naive values are treated as UTC. None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --- Deliveries + rider route (Main Dashboard, 2026-11-11) -----------------
# One Google Maps directions link per delivery: rider -> shop pickup ->
# customer drop-off. Built with the public
# https://www.google.com/maps/dir/?api=1 format; no API key is needed
# for a directions link. Coordinates are exact; a plain address string
# is the fallback when no pin exists. The link is recomputed on read
# from the latest rider position, so it never goes stale when the rider
# moves. Live positions arrive from the bot (future hook: a rider's
# Telegram live-location message POSTed here); until then the route
# starts at the shop and the dashboard honestly shows "no live
# position yet" instead of inventing one.
DELIVERY_STATUSES = ("created", "assigned", "accepted", "picked_up",
                     "delivering", "delivered", "failed", "cancelled")
DELIVERY_TERMINAL = ("delivered", "failed", "cancelled")
DELIVERY_NEXT = {
    "created": ("assigned", "failed", "cancelled"),
    "assigned": ("accepted", "failed", "cancelled"),
    "accepted": ("picked_up", "failed", "cancelled"),
    "picked_up": ("delivering", "failed", "cancelled"),
    "delivering": ("delivered", "failed", "cancelled"),
}
# Order lifecycle (orders.order_status, the API-side status; the bot's
# separate orders.status column holds the shop decision
# 'new'/'shop_confirmed'/'shop_rejected'). A status change is made by
# the owner or operations through POST /api/admin/orders/{id}/status;
# every change writes an order_status_events row and an audit row.
# "waiting" (Min 2026-11-24): a held order. The owner pressed Wait
# in the dashboard, or the shop pressed Wait on its bot card. Not
# terminal: waiting -> confirmed / cancelled. Never counted as
# pending, so the dashboard repeat alarm stops while held.
ORDER_STATUSES = ("pending", "waiting", "confirmed", "preparing", "delivering",
                  "delivered", "cancelled")
ORDER_TERMINAL = ("delivered", "cancelled")
ORDER_NEXT = {
    "pending": ("waiting", "confirmed", "cancelled"),
    "waiting": ("confirmed", "cancelled"),
    "confirmed": ("preparing", "cancelled"),
    "preparing": ("delivering", "cancelled"),
    "delivering": ("delivered", "cancelled"),
}
POSITION_SOURCES = ("telegram_live", "manual", "api")


def _is_valid_lat_lon(lat, lon):
    return (isinstance(lat, (int, float)) and not isinstance(lat, bool)
            and isinstance(lon, (int, float)) and not isinstance(lon, bool)
            and math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180)


def _parse_lat_lon(value):
    """Parse a 'lat,lon' string (as the bot stores pins in pickup/dropoff)
    into (lat, lon) floats, or None when it is not a coordinate pair."""
    if not isinstance(value, str):
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not _is_valid_lat_lon(lat, lon):
        return None
    return (lat, lon)


def _row_get(row, key, default=None):
    """Safe column read from a sqlite3.Row: a missing column raises
    IndexError (bot orders have no shop_id). Returns default instead —
    the caller then falls back gracefully, never inventing data."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _maps_point(lat, lon, address):
    """One Google Maps point: 'lat,lon' when coordinates exist, else the
    address text, else None. Coordinates are exact; addresses are a fallback."""
    if lat is not None and lon is not None and _is_valid_lat_lon(lat, lon):
        return f"{lat:.6f},{lon:.6f}"
    if isinstance(address, str) and address.strip():
        return address.strip()[:500]
    return None


def build_maps_directions_url(origin, destination, waypoints=None, travel_mode="driving"):
    """Build a Google Maps directions link (no API key needed).
    origin/destination/waypoints are 'lat,lon' strings or address text.
    Returns the URL string, or None when origin or destination is missing."""
    if not origin or not destination:
        return None
    params = {"api": "1", "origin": origin, "destination": destination}
    if waypoints:
        params["waypoints"] = "|".join(waypoints)
    if travel_mode:
        params["travelmode"] = travel_mode
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def _latest_rider_position(con, delivery_id):
    row = con.execute(
        "SELECT lat,lon,accuracy_m,source,recorded_at FROM rider_positions"
        " WHERE delivery_id=? ORDER BY recorded_at DESC,id DESC LIMIT 1",
        (delivery_id,)).fetchone()
    return dict(row) if row else None


def _delivery_route_url(delivery, rider_pos):
    """One continuous route: rider -> shop pickup -> customer drop-off.
    When no rider position is known yet, the route starts at the shop
    (origin = pickup) so the link is still usable."""
    pickup = _maps_point(delivery.get("pickup_lat"), delivery.get("pickup_lon"),
                         delivery.get("pickup_address"))
    dropoff = _maps_point(delivery.get("dropoff_lat"), delivery.get("dropoff_lon"),
                          delivery.get("dropoff_address"))
    if not dropoff:
        return None
    if rider_pos:
        origin = _maps_point(rider_pos.get("lat"), rider_pos.get("lon"), None)
        if origin and pickup and origin != pickup:
            return build_maps_directions_url(origin, dropoff, [pickup])
    if not pickup:
        return None
    return build_maps_directions_url(pickup, dropoff)


def _delivery_public(con, row):
    """Serialize a delivery row with its live-computed route link and the
    latest rider position (or None when no position was ever reported)."""
    d = dict(row)
    pos = _latest_rider_position(con, d["id"])
    d["route_url"] = _delivery_route_url(d, pos)
    d["latest_position"] = pos
    if pos:
        try:
            recorded = datetime.fromisoformat(pos["recorded_at"])
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=timezone.utc)
            d["latest_position_age_seconds"] = max(
                0, int((datetime.now(timezone.utc) - recorded).total_seconds()))
        except ValueError:
            d["latest_position_age_seconds"] = None
    else:
        d["latest_position_age_seconds"] = None
    return d


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that commits/rolls back and always closes on context exit."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(db_path=None, *, create=False):
    """Open the configured database without silently creating a new empty file.

    Only the explicit development/test initializer may request create=True.
    Request handlers must fail closed when the configured DB is missing.
    """
    path = Path(db_path or DB_PATH)
    if not create and not path.is_file():
        raise FileNotFoundError(f"Configured Safe Deli database does not exist: {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_response(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store"),
                            ("X-Content-Type-Options", "nosniff"),
                            ("X-Frame-Options", "DENY"),
                            ("Referrer-Policy", "no-referrer"),
                            ("Permissions-Policy", "camera=(), microphone=(), geolocation=()")])
    return [body]


def read_json(environ):
    """Read a bounded JSON request body; reject ambiguous/non-JSON payloads."""
    content_type = (environ.get("CONTENT_TYPE") or "").strip().lower()
    media_type = content_type.split(";", 1)[0].strip()
    if media_type != "application/json":
        raise ValueError("Content-Type must be application/json")
    if ";" in content_type:
        parameters = [part.strip() for part in content_type.split(";")[1:] if part.strip()]
        if any(part not in {"charset=utf-8", "charset=utf8"} for part in parameters):
            raise ValueError("JSON charset must be UTF-8")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (ValueError, TypeError):
        raise ValueError("Invalid Content-Length")
    if length < 1 or length > 256_000:
        raise ValueError("Request body is missing or too large")
    raw = environ["wsgi.input"].read(length)
    if len(raw) != length:
        raise ValueError("Request body is incomplete")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Request body must be valid UTF-8")
    def reject_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("JSON object contains a duplicate key")
            result[key] = item
        return result

    def reject_nonstandard_constant(_value):
        raise ValueError("JSON contains a non-standard numeric value")

    value = json.loads(decoded, object_pairs_hook=reject_duplicate_keys,
                       parse_constant=reject_nonstandard_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def admin_token_is_configured():
    """Require a high-entropy-length secret before exposing any Admin API route."""
    return isinstance(ADMIN_TOKEN, str) and len(ADMIN_TOKEN) >= 32


def require_admin(environ):
    if not admin_token_is_configured():
        return False
    supplied = environ.get("HTTP_AUTHORIZATION", "")
    expected = f"Bearer {ADMIN_TOKEN}"
    # Constant-time comparison reduces timing leakage for the configured secret.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _admin_auth_available():
    """True when any admin authentication exists: the legacy token, or at
    least one active admin user. Used to fail closed when nothing is set up."""
    if admin_token_is_configured():
        return True
    try:
        with connect() as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "admin_users" not in tables:
                return False
            return con.execute("SELECT COUNT(*) FROM admin_users WHERE is_active=1").fetchone()[0] > 0
    except Exception:
        return False



def _items_json_total(text):
    """Items total from an order's items_json (Mini App structured items).

    Each entry contributes price * qty when both are sane; malformed
    entries and unparseable JSON contribute 0. Display/aggregation
    only — prices here are never trusted for charging.
    """
    try:
        items = json.loads(text)
    except (TypeError, ValueError):
        return 0.0
    if not isinstance(items, list):
        return 0.0
    total = 0.0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        price = entry.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        try:
            qty = int(entry.get("qty"))
        except (TypeError, ValueError):
            continue
        if qty < 1 or price < 0:
            continue
        total += float(price) * qty
    return round(total, 2)


def _merchant_range_totals(con, shop_id, date_from, date_to, has_test):
    """Range totals + analytics for one merchant shop (audit 2026-11-24).

    Sales = items only: order_items.line_total where the order was
    built from products, else the items_json sum. The delivery fee is
    NEVER included. Earnings = sales (commission is 0%). Settled =
    confirmed merchant_settlement ledger rows in the range. Unsettled
    = earnings - settled. Customers are counted by sender_phone over
    the shop's own non-test orders; a phone is "new" when its first
    order falls inside the range, and a "confirmed buyer" when it has
    an order at confirmed stage or further inside the range. Visitors
    are order-customers only — nothing tracks page visits.
    """
    test_clause = " AND COALESCE(is_test,0)=0" if has_test else ""
    where = ("shop_id=? AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?" + test_clause)
    params = [shop_id, date_from, date_to]
    orders_in_range = con.execute(
        "SELECT COUNT(*) FROM orders WHERE " + where, params).fetchone()[0]
    # Items built from products (order_items rows win for that order).
    # Cancelled orders never count as sales.
    not_cancelled = " AND COALESCE(o.order_status,'pending')<>'cancelled'"
    items_sum = con.execute(
        "SELECT COALESCE(SUM(oi.line_total),0) FROM order_items oi"
        " JOIN orders o ON o.id = oi.order_id WHERE o.shop_id=? AND"
        " substr(o.created_at,1,10)>=? AND substr(o.created_at,1,10)<=?" + test_clause + not_cancelled,
        params).fetchone()[0] or 0.0
    # API orders without order_items rows: fall back to items_json.
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    json_sum = 0.0
    if "items_json" in cols:
        for row in con.execute(
                "SELECT o.items_json FROM orders o WHERE " + where +
                " AND COALESCE(o.order_status,'pending')<>'cancelled'"
                " AND NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id=o.id)",
                params).fetchall():
            json_sum += _items_json_total(row["items_json"])
    sales = round(float(items_sum) + json_sum, 2)
    settled = con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM financial_ledger"
        " WHERE shop_id=? AND entry_type='merchant_settlement' AND status='confirmed'"
        " AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?",
        (shop_id, date_from, date_to)).fetchone()[0] or 0.0
    settled = round(float(settled), 2)
    hint = con.execute(
        "SELECT currency FROM financial_ledger WHERE shop_id=?"
        " ORDER BY created_at DESC,id DESC LIMIT 1", (shop_id,)).fetchone()
    phones = [r[0] for r in con.execute(
        "SELECT DISTINCT sender_phone FROM orders WHERE " + where +
        " AND sender_phone IS NOT NULL AND TRIM(sender_phone)<>''",
        params).fetchall()]
    total_customers = len(phones)
    new_customers = 0
    confirmed_buyers = 0
    if phones:
        firsts = {r[0]: r[1] for r in con.execute(
            "SELECT sender_phone, MIN(substr(created_at,1,10)) FROM orders"
            " WHERE shop_id=?" + test_clause +
            " AND sender_phone IS NOT NULL AND TRIM(sender_phone)<>''"
            " GROUP BY sender_phone", [shop_id]).fetchall()}
        new_customers = sum(
            1 for p in phones if date_from <= (firsts.get(p) or "") <= date_to)
        buyers = {r[0] for r in con.execute(
            "SELECT DISTINCT sender_phone FROM orders WHERE " + where +
            " AND COALESCE(order_status,'pending') IN"
            " ('confirmed','preparing','delivering','delivered')"
            " AND sender_phone IS NOT NULL AND TRIM(sender_phone)<>''",
            params).fetchall()}
        confirmed_buyers = len(buyers)
    buyer_rate = round(100.0 * confirmed_buyers / total_customers, 1) if total_customers else 0.0
    cancelled = con.execute(
        "SELECT order_no,customer_name,sender_phone,created_at FROM orders WHERE " + where +
        " AND COALESCE(order_status,'pending')='cancelled'"
        " ORDER BY created_at DESC,id DESC LIMIT 50", params).fetchall()
    return {
        "orders_in_range": orders_in_range,
        "sales_total": sales,
        "earnings_total": sales,
        "settled_total": settled,
        "unsettled_total": round(sales - settled, 2),
        "sales_currency": hint["currency"] if hint else None,
        "analytics": {
            "total_customers": total_customers,
            "new_customers": new_customers,
            "confirmed_buyers": confirmed_buyers,
            "buyer_rate_percent": buyer_rate,
            "visitors": total_customers,
        },
        "cancelled_orders": [
            {"order_no": r["order_no"], "customer_name": r["customer_name"],
             "phone": r["sender_phone"], "created_at": r["created_at"],
             "reason": None} for r in cancelled],
    }


def _items_json_lines(text):
    """Per-line (name, qty, price-or-None) records from items_json.

    Mirrors _sanitize_items_structured: entries without a name or
    with qty < 1 are dropped; a non-numeric or negative price becomes
    None (the line still counts its quantity, but never an amount).
    Display/aggregation only — prices here are never trusted for
    charging.
    """
    try:
        items = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    lines = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("name_my")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            qty = int(entry.get("qty"))
        except (TypeError, ValueError):
            continue
        if qty < 1:
            continue
        price = entry.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            price = None
        else:
            price = round(float(price), 2)
        lines.append((name.strip()[:120], qty, price))
    return lines


def _merchant_item_sales(con, shop_id, date_from, date_to, has_test):
    """Per-item sales for one merchant shop over a date range (v19.69).

    order_items rows win for their order; orders without order_items
    rows fall back to items_json (the _merchant_range_totals pattern).
    Cancelled and test orders never count; the delivery fee is never
    included (it is not an item). An item with any null-price
    items_json line has amount None (its quantity still counts);
    amounts_indicative is true when any amount came from items_json,
    whose prices are display-only. Items are sorted by qty desc
    (name asc on ties). The totals amount is None when any item
    amount is None, else the sum of the item amounts; totals.orders
    counts only orders that contributed at least one item line.
    Currency follows the existing ledger-hint pattern (latest
    financial_ledger row for the shop), else the shop's product
    currency, else THB (the products/home_dishes DDL default).
    """
    by_name = {}
    order_ids = set()

    def _add(name, qty, amount, has_null, order_id, from_json):
        slot = by_name.setdefault(
            name, {"qty": 0, "amount": 0.0, "has_null": False,
                   "orders": set(), "from_json": False})
        slot["qty"] += qty
        if has_null:
            slot["has_null"] = True
        elif amount is not None:
            slot["amount"] += amount
        if from_json and amount is not None:
            slot["from_json"] = True
        slot["orders"].add(order_id)
        order_ids.add(order_id)

    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    params = [shop_id, date_from, date_to]
    test_clause = " AND COALESCE(o.is_test,0)=0" if has_test else ""
    not_cancelled = " AND COALESCE(o.order_status,'pending')<>'cancelled'"
    date_clause = ("o.shop_id=? AND substr(o.created_at,1,10)>=?"
                   " AND substr(o.created_at,1,10)<=?")
    if "order_items" in tables:
        for row in con.execute(
                "SELECT oi.product_name_snapshot, oi.quantity, oi.line_total,"
                " oi.order_id FROM order_items oi JOIN orders o"
                " ON o.id = oi.order_id WHERE " + date_clause + test_clause + not_cancelled,
                params).fetchall():
            name = row["product_name_snapshot"]
            if not isinstance(name, str) or not name.strip():
                continue
            _add(name.strip()[:120], int(row["quantity"]),
                 float(row["line_total"]), False, row["order_id"], False)
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    if "items_json" in cols:
        fallback_where = date_clause + test_clause + not_cancelled
        if "order_items" in tables:
            fallback_where += (" AND NOT EXISTS (SELECT 1 FROM order_items oi"
                               " WHERE oi.order_id=o.id)")
        for row in con.execute(
                "SELECT o.id, o.items_json FROM orders o WHERE " + fallback_where,
                params).fetchall():
            for name, qty, price in _items_json_lines(row["items_json"]):
                _add(name, qty, price, price is None, row["id"], True)
    items = []
    any_null = False
    amounts_indicative = False
    for name, slot in by_name.items():
        if slot["has_null"]:
            amount = None
            any_null = True
        else:
            amount = round(slot["amount"], 2)
        if slot["from_json"]:
            amounts_indicative = True
        items.append({"name": name, "qty": slot["qty"], "amount": amount,
                      "orders": len(slot["orders"])})
    items.sort(key=lambda it: (-it["qty"], it["name"]))
    total_amount = None if any_null else round(
        sum(it["amount"] for it in items), 2)
    currency = None
    try:
        hint = con.execute(
            "SELECT currency FROM financial_ledger WHERE shop_id=?"
            " ORDER BY created_at DESC,id DESC LIMIT 1", (shop_id,)).fetchone()
        currency = hint["currency"] if hint else None
    except sqlite3.Error:
        currency = None
    if not currency:
        try:
            prow = con.execute(
                "SELECT currency FROM products WHERE shop_id=?"
                " AND currency IS NOT NULL ORDER BY id LIMIT 1",
                (shop_id,)).fetchone()
            currency = prow["currency"] if prow else None
        except sqlite3.Error:
            currency = None
    return {
        "items": items,
        "totals": {"qty": sum(it["qty"] for it in items),
                   "amount": total_amount,
                   "orders": len(order_ids)},
        "amounts_indicative": amounts_indicative,
        "currency": currency or "THB",
    }

def _owned_shop_ids(con, user_id):
    """Shop ids owned by a merchant: the primary admin_users.shop_id
    plus merchant_shops links (migration 018). Tolerates a database
    that predates migration 018 (no mapping table) by returning the
    primary link only. Returns a set, possibly empty. Never raises."""
    owned = set()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
        if "shop_id" in cols:
            row = con.execute("SELECT shop_id FROM admin_users WHERE id=?", (user_id,)).fetchone()
            if row and row["shop_id"] is not None:
                owned.add(row["shop_id"])
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "merchant_shops" in tables:
            for r in con.execute("SELECT shop_id FROM merchant_shops WHERE admin_user_id=?", (user_id,)).fetchall():
                if r["shop_id"] is not None:
                    owned.add(r["shop_id"])
    except sqlite3.Error:
        pass
    return owned


def _ensure_merchant_shops(con):
    """Create the multi-shop mapping table and the session selection
    column when a database predates migration 018 (same additive,
    idempotent pattern as _add_missing_home_dish_columns). Lets the
    switch/link endpoints self-heal instead of failing on an
    un-migrated database; the migration itself still owns backfill."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "merchant_shops" not in tables:
        con.execute(
            "CREATE TABLE IF NOT EXISTS merchant_shops ("
            "  admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),"
            "  shop_id INTEGER NOT NULL REFERENCES shops(id),"
            "  created_at TEXT NOT NULL,"
            "  PRIMARY KEY (admin_user_id, shop_id))")
    if "admin_sessions" in tables:
        cols = {r[1] for r in con.execute("PRAGMA table_info(admin_sessions)")}
        if "selected_shop_id" not in cols:
            con.execute("ALTER TABLE admin_sessions ADD COLUMN selected_shop_id INTEGER REFERENCES shops(id)")


def _merchant_shop_active(con, user_id):
    """True when the merchant owns at least one shop that exists, is
    active and verified (v19.70: any owned shop counts, not just the
    selected one — a suspended selected shop must not lock the
    merchant out of their healthy shops; per-shop usability is
    enforced per request by _caller_shop_id). Fail closed (False)
    on any schema surprise."""
    try:
        owned = _owned_shop_ids(con, user_id)
        if not owned:
            return False
        placeholders = ",".join("?" for _ in owned)
        row = con.execute(
            "SELECT COUNT(*) AS n FROM shops WHERE id IN (" + placeholders + ")"
            " AND status='active' AND verified=1", tuple(owned)).fetchone()
        return row is not None and row["n"] > 0
    except sqlite3.Error:
        return False


def _caller_shop_id(con, auth):
    """The merchant's EFFECTIVE shop id for this request, else None.
    The session row's selected shop (NULL = primary link) is honoured
    only when the shop is owned by this merchant AND exists, active
    and verified — re-validated from the database on every call.
    Anything else fails closed to None (every call site turns that
    into 403). The value never comes from request data, so a crafted
    shop_id cannot reach another shop's data."""
    role, _username, user_id, selected = auth
    if role != "merchant" or user_id is None:
        return None
    try:
        owned = _owned_shop_ids(con, user_id)
        if not owned:
            return None
        if selected is not None:
            candidate = selected
        else:
            row = con.execute("SELECT shop_id FROM admin_users WHERE id=?", (user_id,)).fetchone()
            candidate = row["shop_id"] if row else None
        if candidate is None or candidate not in owned:
            return None
        shop = con.execute("SELECT status, verified FROM shops WHERE id=?", (candidate,)).fetchone()
        if not shop or shop["status"] != "active" or shop["verified"] != 1:
            return None
        return candidate
    except sqlite3.Error:
        return None


def authenticate(environ):
    """Resolve a Bearer credential to (role, username, user_id,
    selected_shop_id) or None. selected_shop_id is the session row's
    selection (None = primary shop); it is None for the legacy token
    and for non-merchant roles.

    The legacy SAFEDELI_ADMIN_TOKEN is accepted as owner (bootstrap
    credential). Otherwise the token must be a live, unrevoked, unexpired
    admin session; only the SHA-256 hash is ever stored or compared."""
    supplied = environ.get("HTTP_AUTHORIZATION", "") or ""
    if not supplied.startswith("Bearer "):
        return None
    token = supplied[len("Bearer "):].strip()
    if not token:
        return None
    if admin_token_is_configured() and hmac.compare_digest(token, ADMIN_TOKEN):
        return ("owner", "legacy-admin-token", None, None)
    try:
        with connect() as con:
            row = con.execute(
                "SELECT s.*, u.username, u.role, u.is_active"
                " FROM admin_sessions s JOIN admin_users u ON u.id = s.admin_user_id"
                " WHERE s.token_hash = ?", (_session_token_hash(token),)).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None or row["is_active"] != 1:
                return None
            if row["role"] not in ADMIN_ROLES:
                return None
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                return None
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                return None
            # A merchant session is valid while they own at least one
            # active, verified shop (v19.70). Suspending one shop no
            # longer locks the whole login out: the selected shop's
            # usability is enforced per request by _caller_shop_id.
            if row["role"] == "merchant" and not _merchant_shop_active(con, row["admin_user_id"]):
                return None
            try:
                con.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                            (utc_now(), _session_token_hash(token)))
            except sqlite3.Error:
                pass
            selected = row["selected_shop_id"] if "selected_shop_id" in row.keys() else None
            return (row["role"], row["username"], row["admin_user_id"], selected)
    except (FileNotFoundError, sqlite3.Error):
        return None


def require_roles(environ, *roles):
    """Return (role, username, user_id, selected_shop_id) when
    authenticated with an allowed role, else None. Empty roles means
    any authenticated admin role."""
    auth = authenticate(environ)
    if auth is None:
        return None
    if roles and auth[0] not in roles:
        return None
    return auth


def _auth_response(environ, start_response, *roles):
    """Return (auth, error_response). auth is None on failure and the
    caller must return error_response (401 unauthenticated / 403 wrong role)."""
    auth = authenticate(environ)
    if auth is None:
        return None, json_response(start_response, "401 Unauthorized",
                                   {"error": "Admin authorization required"})
    if roles and auth[0] not in roles:
        return None, json_response(start_response, "403 Forbidden",
                                   {"error": "Role not permitted for this action"})
    return auth, None


# Marketplace tables (shops / products / order_items / deliveries / ...).
# Shared by the fresh-DB path and the existing-DB tolerance path below:
# every statement is CREATE TABLE / INDEX / TRIGGER IF NOT EXISTS, so
# running it on a DB that already has some of these tables changes
# nothing. It is the same DDL the fresh path always ran.
_MARKETPLACE_TABLES_DDL = """CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, registration_id INTEGER, name TEXT NOT NULL,
  category TEXT NOT NULL, address TEXT, phone TEXT, latitude REAL, longitude REAL,
  photo_url TEXT, area TEXT,
  maps_url TEXT, video_url TEXT, photo_urls TEXT, interior_photo_urls TEXT, menu_url TEXT,
  menu_updated_at TEXT,
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  is_open INTEGER NOT NULL DEFAULT 0 CHECK(is_open IN (0,1)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','suspended','rejected')),
  created_at TEXT NOT NULL, FOREIGN KEY(registration_id) REFERENCES shop_registrations(id));
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name_my TEXT NOT NULL, name_en TEXT, parent_id INTEGER,
  category_type TEXT NOT NULL CHECK(category_type IN ('food','grocery','other')),
  meal_period TEXT CHECK(meal_period IN ('breakfast','lunch','dinner') OR meal_period IS NULL),
  sort_order INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
  FOREIGN KEY(parent_id) REFERENCES categories(id));
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL, category_id INTEGER,
  name TEXT NOT NULL, description TEXT, price REAL NOT NULL CHECK(price>=0), currency TEXT NOT NULL DEFAULT 'THB',
  image_url TEXT, group_name TEXT NOT NULL DEFAULT 'Food', sort_order INTEGER NOT NULL DEFAULT 0, is_available INTEGER NOT NULL DEFAULT 1 CHECK(is_available IN (0,1)),
  created_at TEXT NOT NULL, updated_at TEXT, FOREIGN KEY(shop_id) REFERENCES shops(id),
  FOREIGN KEY(category_id) REFERENCES categories(id));
CREATE TABLE IF NOT EXISTS home_dishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT,
  price REAL CHECK(price IS NULL OR price>=0), currency TEXT NOT NULL DEFAULT 'THB',
  image_url TEXT, category TEXT NOT NULL, glyph TEXT NOT NULL DEFAULT 'bowl',
  unit TEXT NOT NULL DEFAULT '၁ ပွဲ', sort_order INTEGER NOT NULL DEFAULT 0,
  is_available INTEGER NOT NULL DEFAULT 1 CHECK(is_available IN (0,1)),
  created_at TEXT NOT NULL, updated_at TEXT, options TEXT, shop_id INTEGER);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_id INTEGER,
  product_name_snapshot TEXT NOT NULL, unit_price_snapshot REAL NOT NULL CHECK(unit_price_snapshot>=0),
  quantity INTEGER NOT NULL CHECK(quantity>0), line_total REAL NOT NULL CHECK(line_total>=0),
  FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(product_id) REFERENCES products(id));
CREATE TABLE IF NOT EXISTS service_bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT NOT NULL UNIQUE,
  service TEXT NOT NULL, service_name TEXT NOT NULL,
  customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL,
  fields_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new',
  picked_up INTEGER NOT NULL DEFAULT 0, picked_up_at TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_status_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, status TEXT NOT NULL,
  note TEXT, actor_type TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL, order_id INTEGER NOT NULL UNIQUE,
  stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5), comment TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  created_at TEXT NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(shop_id) REFERENCES shops(id));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_id TEXT, details TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dashboard_settings (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT);
CREATE TABLE IF NOT EXISTS shop_hours (
  shop_id INTEGER PRIMARY KEY, open_time TEXT, close_time TEXT,
  open_days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,0]',
  closed_dates TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL, updated_by TEXT,
  FOREIGN KEY (shop_id) REFERENCES shops(id));
CREATE TABLE IF NOT EXISTS admin_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner','admin','operations','finance','support','merchant')),
  password_hash TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  shop_id INTEGER REFERENCES shops(id),
  created_at TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS admin_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, admin_user_id INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, revoked_at TEXT, last_seen_at TEXT,
  selected_shop_id INTEGER REFERENCES shops(id),
  FOREIGN KEY (admin_user_id) REFERENCES admin_users(id));
CREATE TABLE IF NOT EXISTS merchant_shops (
  admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (admin_user_id, shop_id));
CREATE TABLE IF NOT EXISTS admin_report_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_user_id INTEGER,
  report_type TEXT NOT NULL,
  date_from TEXT,
  date_to TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Yangon',
  filters_json TEXT NOT NULL DEFAULT '{}',
  row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
  generated_at TEXT NOT NULL,
  export_format TEXT NOT NULL CHECK (export_format IN ('view','csv','pdf')),
  FOREIGN KEY (admin_user_id) REFERENCES admin_users(id));
CREATE INDEX IF NOT EXISTS idx_admin_report_runs_generated ON admin_report_runs(generated_at, id);
CREATE TABLE IF NOT EXISTS financial_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, shop_id INTEGER,
  entry_type TEXT NOT NULL CHECK (entry_type IN ('order_charge','delivery_fee','refund','merchant_settlement','adjustment','reversal')),
  amount REAL NOT NULL CHECK (amount >= 0),
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  currency TEXT NOT NULL CHECK (length(currency) = 3),
  payment_method TEXT, external_reference TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','failed','reversed')),
  recorded_by_type TEXT NOT NULL CHECK (recorded_by_type IN ('admin','finance','system')),
  recorded_by_id TEXT, note TEXT, created_at TEXT NOT NULL, confirmed_at TEXT,
  FOREIGN KEY (order_id) REFERENCES orders(id),
  FOREIGN KEY (shop_id) REFERENCES shops(id));
CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
BEFORE UPDATE ON audit_log BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
BEFORE DELETE ON audit_log BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_financial_ledger_no_update
BEFORE UPDATE ON financial_ledger BEGIN
  SELECT RAISE(ABORT, 'financial_ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_financial_ledger_no_delete
BEFORE DELETE ON financial_ledger BEGIN
  SELECT RAISE(ABORT, 'financial_ledger is append-only');
END;
CREATE TABLE IF NOT EXISTS shop_promo_banners (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  image_url TEXT NOT NULL,
  dish_id INTEGER,
  dish_name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_promo_banners_shop ON shop_promo_banners(shop_id, sort_order, id);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  rider_name TEXT,
  rider_phone TEXT,
  rider_chat_id INTEGER,
  pickup_lat REAL,
  pickup_lon REAL,
  pickup_address TEXT,
  dropoff_lat REAL,
  dropoff_lon REAL,
  dropoff_address TEXT,
  status TEXT NOT NULL DEFAULT 'created'
    CHECK (status IN ('created','assigned','accepted','picked_up','delivering','delivered','failed','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  created_by TEXT,
  delivery_fee REAL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_order_status ON deliveries(order_id, status);
CREATE INDEX IF NOT EXISTS idx_deliveries_rider_chat ON deliveries(rider_chat_id);
CREATE TABLE IF NOT EXISTS rider_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id INTEGER NOT NULL,
  lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
  accuracy_m REAL CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
  source TEXT NOT NULL DEFAULT 'api'
    CHECK (source IN ('telegram_live','manual','api')),
  recorded_at TEXT NOT NULL,
  FOREIGN KEY (delivery_id) REFERENCES deliveries(id)
);
CREATE INDEX IF NOT EXISTS idx_rider_positions_delivery_recorded
  ON rider_positions(delivery_id, recorded_at, id);
CREATE TABLE IF NOT EXISTS delivery_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('created','assigned','accepted','picked_up','delivering','delivered','failed','reassigned','cancelled')),
  worker_ref TEXT,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('admin','operations','worker','system')),
  actor_id TEXT,
  note TEXT,
  proof_ref TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_events_order_created ON delivery_events(order_id, created_at, id);
CREATE TRIGGER IF NOT EXISTS trg_delivery_events_no_update
BEFORE UPDATE ON delivery_events BEGIN
  SELECT RAISE(ABORT, 'delivery_events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_delivery_events_no_delete
BEFORE DELETE ON delivery_events BEGIN
  SELECT RAISE(ABORT, 'delivery_events are append-only');
END;"""


def _add_missing_order_columns(con):
    """Add the API's orders columns when missing. Bot databases lack
    shop_id / delivery_fee / order_status (the bot stores the shop NAME
    in orders.shop and has no delivery fee) — adding them is additive
    only; existing rows keep NULL."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    for name, ddl in (("shop_id", "ALTER TABLE orders ADD COLUMN shop_id INTEGER"),
                      ("delivery_fee", "ALTER TABLE orders ADD COLUMN delivery_fee REAL"),
                      # Fee-confirm (2026-11-20, v19.54): the delivery
                      # fee Min types per order in the Dashboard + the
                      # customer's accept/decline. fee_status: NULL
                      # (not set) / 'pending' (waiting for the
                      # customer) / 'accepted' / 'declined'.
                      # fee_msg_sent: 0 pending, 2 sending, 1 sent.
                      # fee_note: Min's short reason (optional),
                      # shown to the customer with the fee.
                      ("fee_status", "ALTER TABLE orders ADD COLUMN fee_status TEXT"),
                      ("fee_msg_sent", "ALTER TABLE orders ADD COLUMN fee_msg_sent INTEGER NOT NULL DEFAULT 0"),
                      ("fee_note", "ALTER TABLE orders ADD COLUMN fee_note TEXT"),
                      # Shop-card flag (2026-11-20, v19.55): the bot's
                      # phone-fee job marks it when the shop card is
                      # sent for an accepted phone order (or when no
                      # separate shop card is needed), so the job
                      # never re-sends it.
                      ("shop_card_sent", "ALTER TABLE orders ADD COLUMN shop_card_sent INTEGER NOT NULL DEFAULT 0"),
                      ("order_status", "ALTER TABLE orders ADD COLUMN order_status TEXT"),
                      # Test-order flag (2026-11-12, v11 follow-up): the
                      # bot stores 1 for owner test orders from sample
                      # shops; old rows keep NULL, treated as real (0).
                      ("is_test", "ALTER TABLE orders ADD COLUMN is_test INTEGER"),
                      # Server-route pickup (2026-11-12, migration 011):
                      # picked_up=1 once the bot pickup job claims an
                      # API order; picked_up_at records when. Old rows
                      # keep NULL, treated as unpicked (0).
                      ("picked_up", "ALTER TABLE orders ADD COLUMN picked_up INTEGER NOT NULL DEFAULT 0"),
                      ("picked_up_at", "ALTER TABLE orders ADD COLUMN picked_up_at TEXT"),
                      # Idempotency key (2026-11-12, migration 011): the
                      # bot schema has it, a DB born from the API DDL
                      # may not — the partial unique index below is
                      # what actually deduplicates retries.
                      ("idempotency_key", "ALTER TABLE orders ADD COLUMN idempotency_key TEXT"),
                      # Receipt photo (2026-11-14, migration 015): the
                      # customer uploads a screenshot of the order receipt
                      # via POST /api/public/order-receipt; the bot pickup
                      # job sends it to the owner. receipt_photo_sent: 0
                      # pending, 2 claimed/sending, 1 sent.
                      ("receipt_photo_path", "ALTER TABLE orders ADD COLUMN receipt_photo_path TEXT"),
                      ("receipt_photo_at", "ALTER TABLE orders ADD COLUMN receipt_photo_at TEXT"),
                      ("receipt_photo_sent", "ALTER TABLE orders ADD COLUMN receipt_photo_sent INTEGER NOT NULL DEFAULT 0"),
                      ("receipt_file_id", "ALTER TABLE orders ADD COLUMN receipt_file_id TEXT"),
                      # Voucher + Telegram identity (2026-11-22, v19.59):
                      # telegram_username stores the Telegram @username
                      # from initData so Min can reach the customer.
                      # items_json stores the Mini App's structured
                      # items (name/qty/price) DISPLAY ONLY — never
                      # used for money; settlement uses admin prices.
                      # Both nullable; old rows keep NULL.
                      ("telegram_username", "ALTER TABLE orders ADD COLUMN telegram_username TEXT"),
                      ("items_json", "ALTER TABLE orders ADD COLUMN items_json TEXT")):
        if name not in cols:
            con.execute(ddl)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency_key "
        "ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL")


def _apply_unified_migrations(con):
    """Apply migrations 006-018 (unified dashboard, 2026-11-12; 018 multi-shop, 2026-11-25). Idempotent:
    each step checks the current schema first, so running it on a fresh DB
    (already born correct via the DDL above) or an already-migrated DB only
    records the version. The 006 recreation is skipped when the CHECK
    already contains 'merchant' and the shop_id column exists. Raises on
    failure; the caller owns the commit."""
    try:
        versions = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    except sqlite3.Error:
        versions = set()
    mig_dir = ROOT / "migrations"
    if 6 not in versions:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "admin_users" not in tables:
            raise RuntimeError("Migration 006 needs the admin_users table; run the reviewed additive migration first")
        check_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='admin_users'").fetchone()[0] or ""
        cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
        if "'merchant'" in check_sql and "shop_id" in cols:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            con.executescript((mig_dir / "006_merchant_role_and_link.sql").read_text(encoding="utf-8"))
    if 7 not in versions:
        con.executescript((mig_dir / "007_order_shop_backfill.sql").read_text(encoding="utf-8"))
    if 8 not in versions:
        cols = {r[1] for r in con.execute("PRAGMA table_info(deliveries)")}
        if "delivery_fee" in cols:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            con.executescript((mig_dir / "008_delivery_fee.sql").read_text(encoding="utf-8"))
    if 9 not in versions:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "shops" not in tables:
            return
        cols = {r[1] for r in con.execute("PRAGMA table_info(shops)")}
        if "photo_url" in cols and "area" in cols:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (9, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            con.executescript((mig_dir / "009_public_catalogue_fields.sql").read_text(encoding="utf-8"))
    if 10 not in versions:
        # v19 customer reviews (Min, 2026-11-12): the reviews table was
        # born in migration 001 as a complaints shape (reason + status
        # pending/reviewed/dismissed/actioned) and no endpoint ever wrote
        # a row to it. Migration 010 reshapes it to comment + status
        # pending/approved/rejected. A DB with no reviews table gets the
        # new shape directly; a DB already in the new shape (fresh DDL)
        # only records the version; an old-shape DB runs the rebuild.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "reviews" not in tables:
            con.execute(
                "CREATE TABLE reviews ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL,"
                "  order_id INTEGER NOT NULL UNIQUE,"
                "  stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5), comment TEXT,"
                "  status TEXT NOT NULL DEFAULT 'pending'"
                "  CHECK (status IN ('pending','approved','rejected')),"
                "  created_at TEXT NOT NULL,"
                "  FOREIGN KEY(order_id) REFERENCES orders(id),"
                "  FOREIGN KEY(shop_id) REFERENCES shops(id))")
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (10, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(reviews)")}
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()[0] or ""
            if "comment" in cols and "'approved'" in sql:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (10, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                con.executescript((mig_dir / "010_shop_reviews.sql").read_text(encoding="utf-8"))
    if 11 not in versions:
        # Server-route orders (2026-11-12, migration 011): the pickup
        # columns + idempotency key on orders. SQLite has no conditional
        # ADD COLUMN, so _add_missing_order_columns() adds only what is
        # missing (checked via PRAGMA table_info) and creates the
        # partial unique index; this block only records the version.
        _add_missing_order_columns(con)
        con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                    " VALUES (11, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    if 12 not in versions:
        # Shop media fields (2026-11-13, migration 012): maps_url,
        # video_url, photo_urls, interior_photo_urls, menu_url on shops.
        # URL links only — no file upload / hosting exists yet.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "shops" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (12, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(shops)")}
            new_cols = {"maps_url", "video_url", "photo_urls", "interior_photo_urls", "menu_url"}
            if new_cols <= cols:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (12, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                con.executescript((mig_dir / "012_shop_media_fields.sql").read_text(encoding="utf-8"))
    if 13 not in versions:
        # Menu timestamp (2026-11-13, migration 013): menu_updated_at
        # on shops — when the menu was last set/changed. Drives the
        # gold NEW badge (fresh menu, 7 days by default). A DB born
        # with the column (fresh DDL) only records the version.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "shops" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (13, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(shops)")}
            if "menu_updated_at" in cols:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (13, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                con.executescript((mig_dir / "013_shop_menu_updated_at.sql").read_text(encoding="utf-8"))
    if 14 not in versions:
        # Product group + order (2026-11-13, migration 014):
        # group_name + sort_order on products, so the owner can
        # arrange the menu into groups and order the dishes. A DB
        # born with the columns (fresh DDL) only records the version.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "products" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (14, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(products)")}
            if {"group_name", "sort_order"} <= cols:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (14, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                con.executescript((mig_dir / "014_product_group_order.sql").read_text(encoding="utf-8"))
    if 15 not in versions:
        # Receipt photo (2026-11-14, migration 015): receipt_photo_path /
        # receipt_photo_at / receipt_photo_sent / receipt_file_id on
        # orders. SQLite has no conditional ADD COLUMN, so
        # _add_missing_order_columns() adds only what is missing
        # (checked via PRAGMA table_info); this block only records the
        # version.
        _add_missing_order_columns(con)
        con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                    " VALUES (15, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    if 16 not in versions:
        # Dish options (2026-11-23, migration 016): options JSON on
        # home_dishes, so the Mini App option sheet has data to show.
        # A DB born with the column (fresh DDL) only records the version.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "home_dishes" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (16, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(home_dishes)")}
            if "options" in cols:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (16, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                _add_missing_home_dish_columns(con)
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (16, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    if 17 not in versions:
        # Dish shop link (2026-11-24, migration 017): shop_id on
        # home_dishes, so a dish can belong to one shop and render on
        # that shop's page in the Mini App. NULL = home menu (no shop).
        # A DB born with the column (fresh DDL) only records the version.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "home_dishes" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (17, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            cols = {r[1] for r in con.execute("PRAGMA table_info(home_dishes)")}
            if "shop_id" in cols:
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (17, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
            else:
                _add_missing_home_dish_columns(con)
                con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                            " VALUES (17, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    if 18 not in versions:
        # Multi-shop merchants (2026-11-25, migration 018): a merchant
        # may own several shops. merchant_shops links accounts to shops
        # (backfilled from the existing one-shop admin_users.shop_id, so
        # current accounts keep working untouched), and the session row
        # carries the selected shop (NULL = primary shop). Purely
        # additive; old code ignores both objects.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "admin_users" not in tables or "admin_sessions" not in tables:
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (18, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        else:
            _ensure_merchant_shops(con)
            cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
            if "shop_id" in cols:
                con.execute(
                    "INSERT OR IGNORE INTO merchant_shops(admin_user_id, shop_id, created_at)"
                    " SELECT id, shop_id, strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM admin_users"
                    " WHERE role='merchant' AND shop_id IS NOT NULL")
            con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at)"
                        " VALUES (18, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")


def initialize_database(db_path=None):
    """Initialize a fresh/disposable DB, or add the missing marketplace
    tables + orders columns to an existing bot DB (additive only).
    A DB with no orders table at all still needs the reviewed
    migration first."""
    path = Path(db_path or DB_PATH)
    if path.exists() and path.stat().st_size:
        con = connect(path)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "orders" not in tables:
                raise RuntimeError("Existing DB is not marketplace-ready; run the reviewed additive migration first")
            # Tolerance for bot databases (2026-11-11): the bot's
            # orders.db has orders + shop_registrations but no
            # marketplace tables (shops / products / order_items /
            # deliveries / ...) and no shop_id / delivery_fee /
            # order_status columns on orders. Create only what is
            # missing — every statement is IF NOT EXISTS, so existing
            # tables and rows are untouched — and add the missing
            # orders columns. Additive only; nothing is dropped,
            # rewritten, or reset. shop_registrations is NOT created
            # here: it belongs to the bot's own schema (the bot
            # creates it on first start), and the dashboard's
            # registration views read it.
            con.executescript(_MARKETPLACE_TABLES_DDL)
            _add_missing_order_columns(con)
            _apply_unified_migrations(con)
            con.commit()
        finally:
            con.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path, create=True)
    try:
        con.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        # Fresh test/dev DB baseline, derived from the legacy schema.
        con.executescript(_MARKETPLACE_TABLES_DDL)
        _add_missing_order_columns(con)
        _apply_unified_migrations(con)
        con.commit()
    finally:
        con.close()


def _mask_phone(phone):
    """Mask a customer phone for roles that may read orders but must not
    see full contact details (support, operations). Keeps the first
    three characters so rows stay distinguishable, e.g. '094****58'.
    Empty/None stays empty."""
    if not phone:
        return phone
    text = str(phone)
    if len(text) <= 4:
        return "*" * len(text)
    return text[:3] + "*" * (len(text) - 5) + text[-2:]


def _parse_date_range(query):
    """?date_from= / ?date_to= (YYYY-MM-DD) for report exports. Returns
    (date_from, date_to, error): error is a message string, or None."""
    date_from = (query.get("date_from", [""])[0] or "").strip()
    date_to = (query.get("date_to", [""])[0] or "").strip()
    for value in (date_from, date_to):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None, None, "Dates must use YYYY-MM-DD"
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None, None, "Dates must use YYYY-MM-DD"
    if date_from and date_to and date_from > date_to:
        return None, None, "date_from must not be after date_to"
    return date_from, date_to, None


def _csv_text(header, rows):
    """Build a CSV document (UTF-8, CRLF rows) from a header string and
    row lists. Values are quoted by the csv module, never by hand, so
    commas and quotes in names/items cannot break the file."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header.split(","))
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def _csv_response(start_response, filename, body):
    """Download response for a CSV export. The filename is fixed by the
    endpoint, never taken from the request."""
    start_response("200 OK", [("Content-Type", "text/csv; charset=utf-8"),
                              ("Content-Length", str(len(body))),
                              ("Content-Disposition", f'attachment; filename="{filename}"'),
                              ("Cache-Control", "no-cache")])
    return [body]


def _serve_file(start_response, file_path, content_type):
    """Serve a static file from disk (dashboard HTML, logo). The file is
    read on each request: the dashboard is small and changes rarely."""
    try:
        data = file_path.read_bytes()
    except FileNotFoundError:
        return json_response(start_response, "404 Not Found", {"error": "File not found"})
    start_response("200 OK", [("Content-Type", content_type),
                              ("Content-Length", str(len(data))),
                              ("Cache-Control", "no-cache")])
    return [data]


# Public GET paths the browser may read cross-origin (D5 catalogue).
# Admin routes are deliberately excluded. Keep this set in sync with
# the OPTIONS handler below.
_PUBLIC_CORS_PATHS = frozenset({
    "/api/catalogue", "/api/shops", "/api/settings/public",
    "/api/public/shops", "/api/public/products",
    "/api/public/reviews", "/api/public/order-status",
    "/api/public/orders", "/api/public/order-receipt",
    "/api/public/popular-today", "/api/public/service-bookings",
    "/api/route",
    "/api/matrix",
})

# Public POST paths: the Mini App submits a review and (server-route,
# 2026-11-12) an order cross-origin (POST + JSON body), so those two
# public paths allow POST and Content-Type. Every other public path
# stays GET-only.
_PUBLIC_POST_PATHS = frozenset({"/api/public/reviews", "/api/public/orders", "/api/public/order-receipt", "/api/public/service-bookings"})

# Built-in CORS origins for the public catalogue: the Mini App on
# GitHub Pages and the unified dashboard on DuckDNS (D2). No wildcard.
_BUILTIN_CORS_ORIGINS = frozenset({
    "https://xixiye357-ops.github.io",
    "https://safedeli.duckdns.org",
})


def _cors_allowed_origins():
    """Return the explicit HTTPS origins allowed to read the public
    catalogue. The Mini App (GitHub Pages) and the dashboard (DuckDNS,
    D2) are built-in defaults — the public catalogue is designed to be
    read from those two origins; extra origins come from the
    SAFEDELI_CORS_ALLOWED_ORIGINS env var. Wildcard CORS is never allowed."""
    origins = set(_BUILTIN_CORS_ORIGINS)
    raw = os.environ.get("SAFEDELI_CORS_ALLOWED_ORIGINS", "")
    for item in raw.split(","):
        origin = item.strip()
        if not origin or len(origin) > 255:
            continue
        try:
            parsed = urlsplit(origin)
            # An origin is scheme + authority only: no credentials, path,
            # query, fragment, wildcard, or non-HTTPS scheme.
            if (parsed.scheme == "https" and parsed.hostname and parsed.netloc
                    and parsed.username is None and parsed.password is None
                    and parsed.path == "" and parsed.query == ""
                    and parsed.fragment == "" and "*" not in parsed.netloc):
                parsed.port  # force validation of malformed port numbers
                origins.add(f"https://{parsed.netloc.lower()}")
        except ValueError:
            continue
    return origins


# --- Server-route orders (2026-11-12) -------------------------------------
# The Mini App posts orders here when a Telegram sendData launch is
# unavailable (menu-button / link launches have initData but no
# query_id). The API validates the Telegram session, writes the order
# once (idempotency_key), and the bot pickup job sends the same
# confirmations as a sendData order. Every rule here mirrors bot.py
# (parse_mini_app_order / can_order_now / fmt_place) so the two
# writers can never disagree; the bot re-validates at pickup, so a
# drift would be caught twice.
_INITDATA_MAX_AGE_SECONDS = 86400  # 24 hours
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_PUBLIC_ORDER_RATE_LIMITS = {}  # key -> [epoch seconds]; in-memory only, resets on restart


def _validate_init_data(raw):
    """Validate a Telegram WebApp initData string (Bot API 'Validating
    data received via the Mini App'): HMAC-SHA256 with the BOT_TOKEN,
    constant-time compare, auth_date within 24h. Returns the user dict
    (with integer id) or None. BOT_TOKEN missing -> None (fail closed).
    Diagnostic (2026-11-14, UNDEPLOYED — working copy only): on failure
    logs only the reason + raw length to stdout (journal). Never logs
    the initData value, the token, or user data."""
    def _fail(reason):
        try:
            print(f"safedeli initdata fail: {reason} len={len(raw) if isinstance(raw, str) else -1}", flush=True)
        except Exception:
            pass
        return None
    if not isinstance(raw, str) or not raw:
        return _fail("empty")
    if not BOT_TOKEN:
        return _fail("no_token")
    try:
        params = parse_qs(raw, keep_blank_values=True)
    except ValueError:
        return _fail("parse_error")
    received = (params.get("hash") or [None])[0]
    if not received:
        return _fail("no_hash")
    pairs = sorted((k, v[0]) for k, v in params.items() if k != "hash")
    check_string = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return _fail("hmac_mismatch")
    try:
        auth_date = int((params.get("auth_date") or ["0"])[0])
    except (TypeError, ValueError):
        return _fail("auth_date_bad")
    if abs(int(time.time()) - auth_date) > _INITDATA_MAX_AGE_SECONDS:
        return _fail("auth_date_old")
    user_raw = (params.get("user") or [None])[0]
    if not user_raw:
        return _fail("no_user")
    try:
        user = json.loads(user_raw)
    except ValueError:
        return _fail("user_json_bad")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return _fail("user_id_bad")
    return user


def _write_shop_hours_file(hours):
    """Write shop_hours.json (the file bot.py reads) from dashboard
    global_hours. Atomic via a temp file + os.replace so the bot never
    reads a half-written file. Returns None on success, an error
    string on failure (the settings are already saved then)."""
    try:
        data = json.dumps({"open": hours["open"], "close": hours["close"]},
                          ensure_ascii=False)
        tmp = SHOP_HOURS_FILE.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, SHOP_HOURS_FILE)
        return None
    except OSError as exc:
        return str(exc)


def _server_route_hours_minutes():
    """Owner-set hours as (open_min, close_min) from shop_hours.json;
    missing / broken / invalid -> the 06:00-22:00 defaults (same as bot.py)."""
    try:
        with open(SHOP_HOURS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, AttributeError):
        data = None
    if isinstance(data, dict):
        try:
            open_min = _minutes_from_hhmm(data.get("open"))
            close_min = _minutes_from_hhmm(data.get("close"))
        except (TypeError, ValueError):
            open_min = close_min = None
        if open_min is not None and close_min is not None and open_min < close_min:
            return open_min, close_min
    return 360, 1320


def _server_route_next_transition(after_dt):
    """Next datetime the schedule flips (open->closed or closed->open)
    strictly after `after_dt` (Yangon wall time). Mirrors bot.py."""
    yangon = ZoneInfo("Asia/Yangon")
    after_dt = after_dt.astimezone(yangon) if after_dt.tzinfo else after_dt.replace(tzinfo=yangon)
    open_min, close_min = _server_route_hours_minutes()
    day = after_dt.date()
    candidates = []
    for d in (day, day + timedelta(days=1)):
        candidates.append(datetime(d.year, d.month, d.day, open_min // 60, open_min % 60, tzinfo=yangon))
        candidates.append(datetime(d.year, d.month, d.day, close_min // 60, close_min % 60, tzinfo=yangon))
    return min(c for c in candidates if c > after_dt)


def _server_route_read_override():
    """(mode, set_at) from shop_closed.json — a faithful port of
    bot._read_shop_override, including the old {"closed": bool}
    format (set_at = file mtime)."""
    try:
        with open(SHOP_CLOSED_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, AttributeError):
        return "auto", None
    if not isinstance(data, dict):
        return "auto", None
    mode = data.get("mode")
    if mode in ("open", "closed", "auto"):
        set_at = None
        raw = data.get("set_at")
        if isinstance(raw, str):
            try:
                set_at = datetime.fromisoformat(raw)
            except ValueError:
                set_at = None
        return mode, set_at
    if data.get("closed"):
        try:
            mtime = os.path.getmtime(SHOP_CLOSED_FILE)
            set_at = datetime.fromtimestamp(mtime, tz=ZoneInfo("Asia/Yangon"))
        except OSError:
            set_at = None
        return "closed", set_at
    return "auto", None


def _server_route_override(now):
    """Live manual override ('open'/'closed'/None) — a faithful port
    of bot.active_override(): an override with NO set_at is NOT in
    force (a hand-written file without one is ignored, exactly like
    the bot), and an override expires at the next schedule flip."""
    yangon = ZoneInfo("Asia/Yangon")
    now = now.astimezone(yangon) if now.tzinfo else now.replace(tzinfo=yangon)
    mode, set_at = _server_route_read_override()
    if mode not in ("open", "closed") or set_at is None:
        return None
    set_at = set_at.astimezone(yangon) if set_at.tzinfo else set_at.replace(tzinfo=yangon)
    if now < set_at:
        return None  # the override was not set yet at this time
    if now >= _server_route_next_transition(set_at):
        return None  # expired: the schedule has taken over again
    return mode


def _server_route_is_open_now(now=None):
    """True when the shop is open at `now` (Yangon wall time)."""
    yangon = ZoneInfo("Asia/Yangon")
    now = now or datetime.now(yangon)
    now = now.astimezone(yangon) if now.tzinfo else now.replace(tzinfo=yangon)
    override = _server_route_override(now)
    if override == "open":
        return True
    if override == "closed":
        return False
    open_min, close_min = _server_route_hours_minutes()
    minutes = now.hour * 60 + now.minute
    return open_min <= minutes < close_min


def _server_route_can_order_now(user_id, now=None):
    """Mirror of bot.can_order_now (v19.15 24/7 ORDERING): a manual
    close blocks everyone (incl. the owner); otherwise ordering is
    ALWAYS open — the 06:00–22:00 window is the DELIVERY window now,
    not the ordering window. `user_id` is kept for the caller's shape
    (the rule no longer depends on who is ordering)."""
    if _server_route_override(now or datetime.now(ZoneInfo("Asia/Yangon"))) == "closed":
        return False
    return True


def _normalize_mm_phone_api(text):
    """Myanmar phone normalisation, byte-identical to bot.normalize_mm_phone."""
    if not text:
        return None
    t = re.sub(r"[\s\-()]", "", str(text).strip())
    if t.startswith("+959"):
        t = "09" + t[4:]
    elif t.startswith("959"):
        t = "09" + t[3:]
    if re.fullmatch(r"09\d{7,9}", t):
        return t
    return None


def _api_place(value):
    """Normalise a Mini App pickup/drop-off value to (place_dict, stored_str),
    mirroring bot._mini_app_place + bot.fmt_place byte-for-byte: a pin
    dict becomes '📍 https://maps.google.com/?q=lat,lon', anything else
    becomes '🏠 <text>'. An out-of-range pin is not a place (empty text)."""
    if isinstance(value, dict):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("longitude"))
        if lat is not None and lon is not None:
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                lat_f = lon_f = None
            if (lat_f is not None and math.isfinite(lat_f) and math.isfinite(lon_f)
                    and -90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                return {"type": "pin", "lat": lat_f, "lon": lon_f}, f"📍 https://maps.google.com/?q={lat_f},{lon_f}"
        text = str(value.get("text") or value.get("address") or "").strip()
        return {"type": "text", "text": text}, f"🏠 {text}"
    text = str(value or "").strip()
    return {"type": "text", "text": text}, f"🏠 {text}"


def _api_customer_location(value):
    """Normalise the Mini App's OPTIONAL customer_location field, mirroring
    bot._normalize_customer_location: a clean dict or None — the order is
    never rejected for a bad location, and a location is never invented."""
    if not isinstance(value, dict):
        return None
    maps_url = value.get("maps_url")
    maps_url = str(maps_url).strip() if isinstance(maps_url, str) else ""
    if maps_url and not (maps_url.startswith("http://") or maps_url.startswith("https://")):
        maps_url = ""
    lat = value.get("lat")
    lon = value.get("lon")
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        lat = lon = None
    if not maps_url and lat is not None:
        maps_url = f"https://maps.google.com/?q={lat},{lon}"
    address = value.get("address")
    address = str(address).strip() if isinstance(address, str) else ""
    source = value.get("source")
    source = source if source in ("gps", "manual") else "gps"
    if not maps_url and address:
        # v19.22 MAPS LINK FOR TYPED ADDRESSES (owner 2026-11-15 18:58):
        # mirrors bot._normalize_customer_location — a typed address
        # (no GPS pin) gets a Maps search link instead of no link.
        maps_url = "https://maps.google.com/?q=" + quote(address)
    if not maps_url and not address:
        return None
    return {"maps_url": maps_url, "lat": lat, "lon": lon, "address": address, "source": source}


def _pick_field_api(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _sanitize_items_structured(value):
    """Sanitize the Mini App's items_structured list for DISPLAY ONLY
    (the bot voucher). Returns a JSON string or None. Prices here are
    never trusted for charging; the admin quotes prices afterwards.
    Entries without a name or with qty < 1 are dropped; at most 100
    items; names <= 120 chars; prices >= 0 or None."""
    if not isinstance(value, list) or not value:
        return None
    items = []
    for entry in value[:100]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name_my") or entry.get("name")
        name = str(name).strip() if isinstance(name, str) else ""
        if not name:
            continue
        try:
            qty = int(entry.get("qty"))
        except (TypeError, ValueError):
            continue
        if qty < 1 or qty > 99:
            continue
        price = entry.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            price = None
        elif price < 0:
            price = None
        else:
            price = round(float(price), 2)
        unit = entry.get("unit")
        unit = str(unit).strip()[:20] if isinstance(unit, str) else ""
        items.append({"name": name[:120], "qty": qty, "price": price, "unit": unit})
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _validate_public_order(data, user_id, username=None):
    """Validate a Mini App order payload the same way bot.parse_mini_app_order
    does (same required fields, same phone rule, same UUID submission id).
    Returns (order_dict, None) or (None, error_message). No prices are ever
    accepted for charging — the admin quotes the item price + delivery fee
    afterwards; items_structured prices are stored display-only for the receipt.
    is_test is trusted only from the owner (same rule as the bot)."""
    if not isinstance(data, dict):
        return None, "Order data is missing"
    if data.get("type") != "mini_app_order":
        return None, "Order data is missing"
    items = _pick_field_api(data, "items", "item")
    if isinstance(items, list):
        items = ", ".join(str(i).strip() for i in items if str(i).strip())
    items = str(items or "").strip()
    if not items:
        return None, "Items are missing"
    pickup, pickup_stored = _api_place(_pick_field_api(data, "pickup", "pickup_location"))
    if not (pickup["type"] == "pin" or pickup["text"]):
        return None, "Pickup location is missing"
    dropoff, dropoff_stored = _api_place(
        _pick_field_api(data, "dropoff", "drop_off", "dropoff_location", "drop_off_location"))
    if not (dropoff["type"] == "pin" or dropoff["text"]):
        return None, "Drop-off location is missing"
    name = str(_pick_field_api(data, "name", "customer_name") or "").strip()
    if not name:
        return None, "Name is missing"
    if len(name) > 120:
        return None, "Name is too long"
    phone = _normalize_mm_phone_api(str(_pick_field_api(data, "phone", "sender_phone") or ""))
    if not phone:
        return None, "Phone number is invalid (09...)"
    note = str(_pick_field_api(data, "note", "notes") or "").strip()
    if len(note) > 1000:
        return None, "Note is too long"
    shop = str(_pick_field_api(data, "shop", "shop_name") or "").strip()
    if len(shop) > 200:
        return None, "Shop name is too long"
    shop_id = str(_pick_field_api(data, "shop_id", "shopId", "shopid") or "").strip()
    if len(shop_id) > 64:
        return None, "Shop id is too long"
    idempotency_key = str(data.get("submission_id") or "").strip().lower()
    if not _UUID_RE.fullmatch(idempotency_key):
        return None, "Order reference is invalid"
    is_owner = user_id is not None and bool(OWNER_CHAT_ID) and str(user_id) == str(OWNER_CHAT_ID)
    uname = str(username).strip().lstrip("@")[:64] if isinstance(username, str) else ""
    return {
        "customer_name": name,
        "customer_chat_id": int(user_id) if user_id is not None else None,
        "telegram_username": uname or None,
        "items_json": _sanitize_items_structured(data.get("items_structured")),
        "sender_phone": phone,
        "pickup": pickup_stored,
        "dropoff": dropoff_stored,
        "receiver_phone": phone,
        "item": items,
        "shop": shop,
        "shop_id_text": shop_id,
        "is_test": 1 if (data.get("is_test") and is_owner) else 0,
        "note": note,
        "customer_location": _api_customer_location(data.get("customer_location")),
        "idempotency_key": idempotency_key,
    }, None


def _next_public_order_no(con, now):
    """Next daily order number without reusing gaps — same MAX()+1 rule
    as bot.next_order_no (COUNT()+1 would collide after a gap)."""
    day = now.strftime("%Y%m%d")
    cur = con.execute(
        "SELECT COALESCE(MAX(CAST(substr(order_no, 13) AS INTEGER)), 0) "
        "FROM orders WHERE order_no LIKE ? AND length(order_no) = 15 "
        "AND substr(order_no, 13) GLOB '[0-9][0-9][0-9]'",
        (f"SD-{day}-%",),
    )
    seq = cur.fetchone()[0] + 1
    return f"SD-{day}-{seq:03d}"


def _public_order_rate_limited(key, limit, window=60):
    """In-memory sliding-window rate limit (same precedent as the login
    lockout): resets on restart, which is acceptable for a single-process API."""
    now = time.time()
    hits = [t for t in _PUBLIC_ORDER_RATE_LIMITS.get(key, []) if now - t < window]
    hits.append(now)
    _PUBLIC_ORDER_RATE_LIMITS[key] = hits
    return len(hits) > limit


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    origin = (environ.get("HTTP_ORIGIN") or "").strip().rstrip("/")
    allowed_origins = _cors_allowed_origins()

    # Public catalogue only. Admin routes are deliberately excluded, except
    # the safe public settings subset (banners/hours/form fields) that the
    # Mini App fetches; it carries no sensitive data and allows GET only.
    original_start_response = start_response
    def cors_start_response(status, headers, exc_info=None):
        headers = list(headers)
        headers.append(("Vary", "Origin"))
        if origin and origin in allowed_origins and path in _PUBLIC_CORS_PATHS:
            # v19 reviews + server-route orders: the Mini App submits
            # cross-origin (POST + JSON body), so those public paths
            # allow POST and Content-Type. Every other public path
            # stays GET-only.
            if path in _PUBLIC_POST_PATHS:
                methods, allow_headers = "GET, POST, OPTIONS", "Accept, Content-Type"
            else:
                methods, allow_headers = "GET, OPTIONS", "Accept"
            headers.extend([
                ("Access-Control-Allow-Origin", origin),
                ("Access-Control-Allow-Methods", methods),
                ("Access-Control-Allow-Headers", allow_headers),
                ("Access-Control-Max-Age", "600"),
            ])
        if exc_info is not None:
            return original_start_response(status, headers, exc_info)
        return original_start_response(status, headers)

    start_response = cors_start_response
    if method == "OPTIONS":
        if path not in _PUBLIC_CORS_PATHS:
            return json_response(start_response, "404 Not Found", {"error": "Not found"})
        if not origin or origin not in allowed_origins:
            return json_response(start_response, "403 Forbidden", {"error": "Origin not allowed"})
        requested_method = (environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD") or "").upper()
        requested_headers = (environ.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS") or "").lower().split(",")
        # GET preflights stay as before on every public path. A POST
        # preflight is allowed ONLY for the public POST paths (reviews,
        # server-route orders) and only with Accept/Content-Type headers.
        if requested_method == "GET":
            if any(h.strip() not in {"", "accept"} for h in requested_headers):
                return json_response(start_response, "403 Forbidden", {"error": "CORS preflight not allowed"})
        elif (requested_method == "POST" and path in _PUBLIC_POST_PATHS
                and all(h.strip() in {"", "accept", "content-type"} for h in requested_headers)):
            pass
        else:
            return json_response(start_response, "403 Forbidden", {"error": "CORS preflight not allowed"})
        return json_response(start_response, "204 No Content", {})
    # Fail closed on Admin misconfiguration. Public customer endpoints remain
    # available, but Admin endpoints return 503 until a strong secret is set
    # or at least one active admin user exists (sessions keep working if the
    # legacy token is later removed).
    if path.startswith("/api/admin/") and not _admin_auth_available():
        return json_response(start_response, "503 Service Unavailable", {
            "error": "Admin API is not configured securely"})

    try:
        if method == "GET" and path == "/health":
            # Liveness only: this confirms the process is answering requests.
            return json_response(start_response, "200 OK", {"ok": True, "service": "safedeli-api"})
        if method == "GET" and path == "/ready":
            # Readiness is deliberately separate from liveness so an API process
            # with a missing, damaged, or incomplete DB is not sent live traffic.
            required_tables = {"orders", "shop_registrations", "shops", "products", "order_items"}
            with connect() as con:
                existing = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                missing = sorted(required_tables - existing)
                if missing:
                    return json_response(start_response, "503 Service Unavailable", {
                        "ready": False, "reason": "database_schema_incomplete"})
                integrity = con.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    return json_response(start_response, "503 Service Unavailable", {
                        "ready": False, "reason": "database_integrity_check_failed"})
            return json_response(start_response, "200 OK", {
                "ready": True, "service": "safedeli-api", "database": "ready"})
        # Read-only Admin reporting endpoints. They read the same SQLite DB as
        # the bot when SAFEDELI_DB_PATH is explicitly pointed at orders.db.
        if method == "GET" and path == "/api/admin/dashboard/summary":
            # Premium Dashboard v2 summary. All existing fields are kept
            # for backward compatibility. Finance is owner/finance only:
            # support and operations see finance_visible=false and no
            # finance object at all. Merchants never use this endpoint
            # (they have GET /api/merchant/summary, scoped to their shop).
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            _new_days, new_days_err = _parse_new_days(query)
            if new_days_err:
                return json_response(start_response, "400 Bad Request",
                                     {"error": new_days_err})
            role = auth[0]
            finance_visible = role not in ("support", "operations")
            now_yangon = datetime.now(YANGON_TZ)
            today = now_yangon.strftime("%Y-%m-%d")
            month_prefix = now_yangon.strftime("%Y-%m")
            chart_dates = [(now_yangon - timedelta(days=i)).strftime("%Y-%m-%d")
                           for i in range(6, -1, -1)]
            with connect() as con:
                # Test orders (2026-11-12, v11 follow-up) are excluded
                # from every order count here: owner test orders from
                # sample shops must never inflate totals, today counts,
                # status breakdowns, the chart, or the recent list.
                # Finance needs no filter: it reads financial_ledger
                # only, and orders never write ledger rows.
                _not_test = "COALESCE(is_test,0)=0"
                orders_total = con.execute(
                    f"SELECT COUNT(*) AS total FROM orders WHERE {_not_test}").fetchone()["total"]
                orders_pending = con.execute(
                    "SELECT COUNT(*) AS total FROM orders"
                    f" WHERE {_not_test} AND COALESCE(order_status,'pending')='pending'").fetchone()["total"]
                orders_today = con.execute(
                    "SELECT COUNT(*) AS total FROM orders"
                    f" WHERE {_not_test} AND substr(created_at,1,10)=?",
                    (today,)).fetchone()["total"]
                orders_by_status = {r["status"]: r["total"] for r in con.execute(
                    "SELECT COALESCE(order_status,'pending') AS status, COUNT(*) AS total"
                    f" FROM orders WHERE {_not_test} GROUP BY COALESCE(order_status,'pending')").fetchall()}
                registrations_pending = con.execute(
                    "SELECT COUNT(*) AS total FROM shop_registrations WHERE status='pending'").fetchone()["total"]
                shops_total = con.execute("SELECT COUNT(*) AS total FROM shops").fetchone()["total"]
                shops_active = con.execute(
                    "SELECT COUNT(*) AS total FROM shops WHERE status='active'").fetchone()["total"]
                shops_pending = con.execute(
                    "SELECT COUNT(*) AS total FROM shops WHERE status='pending'").fetchone()["total"]
                shops_suspended = con.execute(
                    "SELECT COUNT(*) AS total FROM shops WHERE status='suspended'").fetchone()["total"]
                active_shops = con.execute(
                    "SELECT COUNT(*) AS total FROM shops WHERE status='active' AND verified=1").fetchone()["total"]
                shop_rows = con.execute(
                    "SELECT id, status, verified FROM shops WHERE status='active'").fetchall()
                hours_map = _shop_hours_map(con)
                global_open, global_close = _global_hours(con)
                open_now = sum(1 for s in shop_rows
                               if _shop_is_open_now(s, hours_map, global_open, global_close, now_yangon))
                # closed_now counts verified-active shops that are not
                # open now, so open_now + closed_now == active_shops.
                closed_now = active_shops - open_now
                riders_available, rider_counts, _rider_rows = _rider_counts(con)
                deliveries_by_status = {r["status"]: r["total"] for r in con.execute(
                    "SELECT status, COUNT(*) AS total FROM deliveries GROUP BY status").fetchall()}
                deliveries_total = con.execute(
                    "SELECT COUNT(*) AS total FROM deliveries").fetchone()["total"]
                finance, chart_income, _currency = _ledger_finance(
                    con, today, month_prefix, chart_dates)
                orders_by_day = {r["day"]: r["total"] for r in con.execute(
                    "SELECT substr(created_at,1,10) AS day, COUNT(*) AS total FROM orders"
                    f" WHERE {_not_test} GROUP BY substr(created_at,1,10)").fetchall()}
                recent = con.execute(
                    "SELECT order_no, created_at, customer_name, shop, item, order_status, delivery_fee"
                    f" FROM orders WHERE {_not_test} ORDER BY created_at DESC, id DESC LIMIT 5").fetchall()
            payload = {"timezone": "Asia/Yangon", "orders_total": orders_total,
                "orders_pending": orders_pending, "registrations_pending": registrations_pending,
                "active_shops": active_shops, "data_source": "configured_sqlite_database",
                "as_of": now_yangon.isoformat(timespec="seconds"), "role": role,
                "finance_visible": finance_visible,
                "orders": {"total": orders_total, "today": orders_today,
                           "by_status": orders_by_status},
                "shops": {"total": shops_total, "active": shops_active,
                          "open_now": open_now, "closed_now": closed_now,
                          "pending": shops_pending, "suspended": shops_suspended},
                "riders": rider_counts, "riders_available": riders_available,
                "deliveries": {"total": deliveries_total, "by_status": deliveries_by_status},
                "chart": [{"date": d, "income": chart_income[d],
                           "orders": orders_by_day.get(d, 0)} for d in chart_dates],
                "recent_orders": [{
                    "order_no": r["order_no"], "created_at": r["created_at"],
                    "customer_name": r["customer_name"], "shop": r["shop"],
                    "item": r["item"],
                    "order_status": r["order_status"] if r["order_status"] is not None else "pending",
                    "delivery_fee": r["delivery_fee"]} for r in recent]}
            if finance_visible:
                payload["finance"] = finance
            return json_response(start_response, "200 OK", payload)
        if method == "GET" and path in ("/api/admin/orders", "/api/admin/registrations"):
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            # Merchants never see the platform's shop applications.
            if path.endswith("/registrations") and auth[0] == "merchant":
                return json_response(start_response, "403 Forbidden",
                                     {"error": "Role not permitted for this action"})
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            date_from = (query.get("date_from", [""])[0] or "").strip()
            date_to = (query.get("date_to", [""])[0] or "").strip()
            search = (query.get("q", [""])[0] or "").strip()[:100]
            try:
                for value in (date_from, date_to):
                    if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError("Dates must use YYYY-MM-DD")
                    if value:
                        datetime.strptime(value, "%Y-%m-%d")
                if date_from and date_to and date_from > date_to:
                    raise ValueError("date_from must not be after date_to")
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid date range or pagination"})
            clauses, params = [], []
            # Stored timestamps retain their original local calendar date prefix;
            # this avoids shifting a Yangon date into the previous UTC date.
            if date_from:
                clauses.append("substr(created_at,1,10)>=?"); params.append(date_from)
            if date_to:
                clauses.append("substr(created_at,1,10)<=?"); params.append(date_to)
            if path.endswith("/orders"):
                # Test orders are excluded by default (matching the
                # summary); the owner may pass ?include_test=1 to see
                # them, labelled TEST in the dashboard.
                include_test = (query.get("include_test", [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
                if not include_test:
                    clauses.append("COALESCE(is_test,0)=0")
                # Merchants see only their own shop's orders. The shop
                # comes from the session, never from the request.
                if auth[0] == "merchant":
                    with connect() as con:
                        merchant_shop = _caller_shop_id(con, auth)
                    if merchant_shop is None:
                        return json_response(start_response, "403 Forbidden",
                                             {"error": "Merchant account is not linked to a shop"})
                    clauses.append("shop_id=?"); params.append(merchant_shop)
                if search:
                    # Order lookup (Min 2026-11-19): order number OR the
                    # phone given at order time (sender_phone), partial
                    # match on either — plus the existing name/shop match.
                    clauses.append("(COALESCE(order_no,'') LIKE ? OR COALESCE(customer_name,'') LIKE ? OR COALESCE(shop,'') LIKE ? OR COALESCE(sender_phone,'') LIKE ?)")
                    params.extend([f"%{search}%"] * 4)
                # Stage filter (2026-11-22, v19.59, points 6/20): the 4
                # workflow stages as display labels over the stored
                # statuses. A fee-accepted order still shows 'pending'
                # until the dashboard moves it, so it counts as
                # confirmed. Unknown values are ignored (no clause).
                stage = (query.get("stage", [""])[0] or "").strip()
                if stage == "new":
                    clauses.append("(COALESCE(order_status,'pending') IN ('pending','waiting') AND COALESCE(fee_status,'')='')")
                elif stage == "waiting":
                    clauses.append("(COALESCE(order_status,'pending') IN ('pending','waiting') AND fee_status='pending')")
                elif stage == "confirmed":
                    clauses.append("(COALESCE(order_status,'pending') IN ('confirmed','preparing','delivering') OR (COALESCE(order_status,'pending') IN ('pending','waiting') AND fee_status='accepted'))")
                elif stage == "delivered":
                    clauses.append("COALESCE(order_status,'pending')='delivered'")
                elif stage == "cancelled":
                    clauses.append("COALESCE(order_status,'pending')='cancelled'")
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                with connect() as con:
                    total = con.execute("SELECT COUNT(*) FROM orders" + where, params).fetchone()[0]
                    fee_total = con.execute("SELECT COALESCE(SUM(delivery_fee),0) FROM orders" + where, params).fetchone()[0]
                    rows = con.execute("SELECT id,order_no,created_at,customer_name,sender_phone,shop,item,dropoff,source,order_status,delivery_fee,fee_status,fee_note,is_test FROM orders" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, (page-1)*limit)).fetchall()
                return json_response(start_response, "200 OK", {"timezone": "Asia/Yangon", "page": page, "limit": limit,
                    "total": total, "fee_total": fee_total, "orders": [dict(r) for r in rows]})
            if search:
                clauses.append("(COALESCE(shop_name,'') LIKE ? OR COALESCE(owner_name,'') LIKE ? OR COALESCE(phone,'') LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with connect() as con:
                total = con.execute("SELECT COUNT(*) FROM shop_registrations" + where, params).fetchone()[0]
                # The v5 checklist columns (menu_json, hours_json,
                # photos_confirmed, photos_note, video_file_id,
                # video_duration) exist in bot-created DBs but may be
                # missing in older bot-shaped DBs that were only given
                # the marketplace tables: select only the columns that
                # exist, and read the rest as None via _row_get.
                existing_cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(shop_registrations)")}
                extra_cols = [c for c in ("menu_json", "hours_json", "photos_confirmed",
                                          "photos_note", "video_file_id", "video_duration")
                              if c in existing_cols]
                select_cols = ("id,created_at,shop_name,owner_name,phone,address,category,status,"
                               "location_lat,location_lon,map_link")
                if extra_cols:
                    select_cols += "," + ",".join(extra_cols)
                rows = con.execute("SELECT " + select_cols + " FROM shop_registrations" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, (page-1)*limit)).fetchall()
                registrations = []
                for r in rows:
                    entry = dict(r)
                    entry["completeness"] = _registration_completeness(entry)
                    registrations.append(entry)
            return json_response(start_response, "200 OK", {"timezone": "Asia/Yangon", "page": page, "limit": limit,
                "total": total, "registrations": registrations})
        # Authenticated, read-only Admin catalogue inspection. These endpoints
        # intentionally expose merchant contact details only to an authorized
        # Admin; public shop responses below never include them.
        if method == "GET" and path == "/api/admin/shops":
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            search = (query.get("q", [""])[0] or "").strip()[:100]
            status_filter = (query.get("status", [""])[0] or "").strip().lower()
            if status_filter and status_filter not in {"pending", "active", "suspended", "rejected"}:
                return json_response(start_response, "400 Bad Request", {"error": "Invalid shop status"})
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid pagination"})
            new_days, new_days_err = _parse_new_days(query)
            if new_days_err:
                return json_response(start_response, "400 Bad Request", {"error": new_days_err})
            clauses, params = [], []
            if status_filter:
                clauses.append("s.status=?"); params.append(status_filter)
            if search:
                clauses.append("(COALESCE(s.name,'') LIKE ? OR COALESCE(s.category,'') LIKE ? OR COALESCE(s.address,'') LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            # Merchants see only their own shop (any status/search filter
            # still applies on top of it).
            if auth[0] == "merchant":
                with connect() as con:
                    merchant_shop = _caller_shop_id(con, auth)
                if merchant_shop is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                clauses.append("s.id=?"); params.append(merchant_shop)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with connect() as con:
                total = con.execute("SELECT COUNT(*) FROM shops s" + where, params).fetchone()[0]
                rows = con.execute("""SELECT s.id,s.registration_id,s.name,s.category,s.address,s.phone,
                    s.latitude,s.longitude,s.photo_url,s.area,s.maps_url,s.video_url,
                    s.photo_urls,s.interior_photo_urls,s.menu_url,s.menu_updated_at,
                    s.verified,s.is_open,s.status,s.created_at,
                    (SELECT COUNT(*) FROM products p WHERE p.shop_id=s.id) AS products_total,
                    (SELECT COUNT(*) FROM products p WHERE p.shop_id=s.id AND p.is_available=1) AS products_available
                    FROM shops s""" + where + " ORDER BY s.created_at DESC,s.id DESC LIMIT ? OFFSET ?",
                    (*params, limit, (page-1)*limit)).fetchall()
                now_yangon = datetime.now(YANGON_TZ)
                hours_map = _shop_hours_map(con)
                global_open, global_close = _global_hours(con)
                shops = []
                for r in rows:
                    shop = dict(r)
                    # Media link lists are stored as JSON text; the
                    # dashboard edit form needs real arrays.
                    shop["photo_urls"] = _shop_media_list(shop.get("photo_urls"))
                    shop["interior_photo_urls"] = _shop_media_list(shop.get("interior_photo_urls"))
                    # is_new: an active shop created within the last
                    # new_days days (shops.created_at is the approval
                    # time, set to utc_now() when approved).
                    shop["is_new"] = (shop["status"] == "active"
                                      and _shop_is_new(shop["created_at"], new_days, now_yangon))
                    # menu_is_new: the shop's menu was set/changed
                    # within the same NEW window. Drives the gold NEW
                    # badge on the menu (Mini App picture corner +
                    # dashboard edit form).
                    shop["menu_is_new"] = _shop_is_new(shop.get("menu_updated_at"), new_days, now_yangon)
                    shop["is_open_now"] = _shop_is_open_now(
                        shop, hours_map, global_open, global_close, now_yangon)
                    shops.append(shop)
            return json_response(start_response, "200 OK", {"page": page, "limit": limit,
                "total": total, "shops": shops})
        # --- Shop promo banners (v19.68, Min 2026-11-24) -------------
        # Per-shop carousel images on the shop overview tab. A banner
        # always names one dish of the SAME shop; tapping it in the app
        # adds that dish to the cart. At most 20 banners per shop.
        admin_shop_banners = re.fullmatch(r"/api/admin/shops/(\d+)/promo-banners", path)
        if admin_shop_banners and method in ("GET", "POST"):
            auth, error = _auth_response(environ, start_response, "owner", "merchant")
            if error is not None:
                return error
            shop_id = int(admin_shop_banners.group(1))
            with connect() as con:
                _ensure_shop_promo_banners(con)
                shop = con.execute("SELECT id FROM shops WHERE id=?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                if auth[0] == "merchant" and _caller_shop_id(con, auth) != shop_id:
                    return json_response(start_response, "403 Forbidden", {"error": "Role not permitted for this action"})
                if method == "GET":
                    rows = con.execute(
                        "SELECT " + _PROMO_BANNER_COLUMNS +
                        " FROM shop_promo_banners WHERE shop_id=? ORDER BY sort_order,id",
                        (shop_id,)).fetchall()
                    return json_response(start_response, "200 OK", {
                        "shop_id": shop_id,
                        "banners": [_promo_banner_row(r) for r in rows]})
                data = read_json(environ)
                fields, err = _clean_promo_banner_fields(data, True)
                if err is not None:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                count = con.execute(
                    "SELECT COUNT(*) FROM shop_promo_banners WHERE shop_id=?", (shop_id,)).fetchone()[0]
                if count >= 20:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "At most 20 promo banners per shop"})
                if fields.get("dish_id") is not None and not _promo_dish_ok(con, shop_id, fields["dish_id"]):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "dish_id does not belong to this shop"})
                if fields.get("sort_order") is None:
                    fields["sort_order"] = con.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) FROM shop_promo_banners WHERE shop_id=?",
                        (shop_id,)).fetchone()[0] + 1
                now = utc_now()
                cur = con.execute(
                    "INSERT INTO shop_promo_banners(shop_id,image_url,dish_id,dish_name,sort_order,is_active,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (shop_id, fields["image_url"], fields.get("dish_id"), fields["dish_name"],
                     fields["sort_order"], fields.get("is_active", 1), now, now))
                banner_id = cur.lastrowid
                _audit(con, auth[1], "shop_promo_banner.create", "shop_promo_banner", banner_id,
                       {"shop_id": shop_id, "dish_name": fields["dish_name"]})
                row = con.execute(
                    "SELECT " + _PROMO_BANNER_COLUMNS + " FROM shop_promo_banners WHERE id=?",
                    (banner_id,)).fetchone()
            return json_response(start_response, "201 Created", {"banner": _promo_banner_row(row)})
        admin_banner = re.fullmatch(r"/api/admin/shop-promo-banners/(\d+)", path)
        if admin_banner and method in ("PUT", "DELETE"):
            auth, error = _auth_response(environ, start_response, "owner", "merchant")
            if error is not None:
                return error
            banner_id = int(admin_banner.group(1))
            with connect() as con:
                _ensure_shop_promo_banners(con)
                row = con.execute(
                    "SELECT " + _PROMO_BANNER_COLUMNS + " FROM shop_promo_banners WHERE id=?",
                    (banner_id,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Promo banner not found"})
                if auth[0] == "merchant" and _caller_shop_id(con, auth) != row["shop_id"]:
                    return json_response(start_response, "403 Forbidden", {"error": "Role not permitted for this action"})
                if method == "DELETE":
                    con.execute("DELETE FROM shop_promo_banners WHERE id=?", (banner_id,))
                    _audit(con, auth[1], "shop_promo_banner.delete", "shop_promo_banner", banner_id,
                           {"shop_id": row["shop_id"]})
                    return json_response(start_response, "200 OK", {"deleted": True, "id": banner_id})
                data = read_json(environ)
                fields, err = _clean_promo_banner_fields(data, False)
                if err is not None:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                if fields.get("dish_id") is not None and not _promo_dish_ok(con, row["shop_id"], fields["dish_id"]):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "dish_id does not belong to this shop"})
                updated_keys = [k for k in ("image_url", "dish_id", "dish_name", "sort_order", "is_active") if k in fields]
                if updated_keys:
                    sets = [k + "=?" for k in updated_keys]
                    params = [fields[k] for k in updated_keys]
                    sets.append("updated_at=?")
                    params.append(utc_now())
                    params.append(banner_id)
                    con.execute("UPDATE shop_promo_banners SET " + ", ".join(sets) + " WHERE id=?", params)
                    _audit(con, auth[1], "shop_promo_banner.update", "shop_promo_banner", banner_id,
                           {"shop_id": row["shop_id"], "updated": updated_keys})
                row = con.execute(
                    "SELECT " + _PROMO_BANNER_COLUMNS + " FROM shop_promo_banners WHERE id=?",
                    (banner_id,)).fetchone()
            return json_response(start_response, "200 OK", {"banner": _promo_banner_row(row)})
        if method == "GET" and path == "/api/admin/riders":
            # Owner, finance, support and operations may read riders.
            # Merchants never see the platform's rider list. Phone
            # numbers are admin-only data — this endpoint is
            # admin-authenticated, the same as the shops list which
            # already returns phones; it is never added to a public
            # endpoint. The riders table is created by the BOT, not by
            # api.py: when it does not exist yet the endpoint still
            # answers with an empty list.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            with connect() as con:
                available, _counts, rows = _rider_counts(con)
                active_chat_ids = _active_rider_chat_ids(con) if available else set()
                deliveries_exist = _table_exists(con, "deliveries")
                positions_exist = _table_exists(con, "rider_positions")
                orders_exist = _table_exists(con, "orders")
                now_utc = datetime.now(timezone.utc)
                riders = []
                for row in rows:
                    chat_id = row["chat_id"]
                    active_delivery = None
                    active_delivery_id = None
                    if deliveries_exist and chat_id is not None:
                        placeholders = ",".join("?" for _ in ACTIVE_DELIVERY_STATUSES)
                        drow = con.execute(
                            "SELECT d.id, d.status, o.order_no FROM deliveries d"
                            " LEFT JOIN orders o ON o.id=d.order_id"
                            f" WHERE d.rider_chat_id=? AND d.status IN ({placeholders})"
                            " ORDER BY d.created_at DESC, d.id DESC LIMIT 1",
                            (chat_id, *ACTIVE_DELIVERY_STATUSES)).fetchone()
                        if drow:
                            active_delivery_id = drow["id"]
                            active_delivery = {"order_no": drow["order_no"],
                                               "status": drow["status"]}
                    on_duty = (row["status"] == "approved"
                               and chat_id is not None and chat_id in active_chat_ids)
                    latest_position = None
                    if positions_exist and deliveries_exist and chat_id is not None:
                        pos = None
                        # Newest position for the active delivery first;
                        # otherwise the newest position from any of
                        # this rider's deliveries.
                        if active_delivery_id is not None:
                            pos = con.execute(
                                "SELECT lat, lon, recorded_at, source FROM rider_positions"
                                " WHERE delivery_id=? ORDER BY recorded_at DESC, id DESC LIMIT 1",
                                (active_delivery_id,)).fetchone()
                        if pos is None:
                            pos = con.execute(
                                "SELECT rp.lat, rp.lon, rp.recorded_at, rp.source"
                                " FROM rider_positions rp JOIN deliveries d ON d.id=rp.delivery_id"
                                " WHERE d.rider_chat_id=? ORDER BY rp.recorded_at DESC, rp.id DESC LIMIT 1",
                                (chat_id,)).fetchone()
                        if pos:
                            recorded = _parse_recorded_at(pos["recorded_at"])
                            age_minutes = None
                            if recorded is not None:
                                age_minutes = max(
                                    0, int((now_utc - recorded).total_seconds()) // 60)
                            latest_position = {"lat": pos["lat"], "lon": pos["lon"],
                                               "recorded_at": pos["recorded_at"],
                                               "age_minutes": age_minutes,
                                               "source": pos["source"]}
                    riders.append({"id": row["id"], "name": row["name"],
                                   "phone": row["phone"], "chat_id": chat_id,
                                   "status": row["status"], "on_duty": on_duty,
                                   "active_delivery": active_delivery,
                                   "latest_position": latest_position,
                                   "last_job_at": row["last_job_at"]})
            return json_response(start_response, "200 OK",
                                 {"riders_available": available, "total": len(riders),
                                  "riders": riders})
        if method == "GET" and path == "/api/admin/products":
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            search = (query.get("q", [""])[0] or "").strip()[:100]
            availability = (query.get("availability", [""])[0] or "").strip().lower()
            shop_id = (query.get("shop_id", [""])[0] or "").strip()
            if availability and availability not in {"available", "unavailable"}:
                return json_response(start_response, "400 Bad Request", {"error": "Invalid availability filter"})
            clauses, params = [], []
            if availability:
                clauses.append("p.is_available=?"); params.append(1 if availability == "available" else 0)
            if search:
                clauses.append("(COALESCE(p.name,'') LIKE ? OR COALESCE(p.description,'') LIKE ? OR COALESCE(s.name,'') LIKE ?)")
                params.extend([f"%{search}%"] * 3)
            if shop_id:
                try:
                    sid = int(shop_id)
                    if sid < 1: raise ValueError()
                except ValueError:
                    return json_response(start_response, "400 Bad Request", {"error": "Invalid shop_id"})
                if auth[0] != "merchant":
                    clauses.append("p.shop_id=?"); params.append(sid)
            # Merchants see only their own shop's products. A ?shop_id=
            # in the request is ignored for them — the session decides.
            if auth[0] == "merchant":
                with connect() as con:
                    merchant_shop = _caller_shop_id(con, auth)
                if merchant_shop is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                clauses.append("p.shop_id=?"); params.append(merchant_shop)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid pagination"})
            with connect() as con:
                total = con.execute("SELECT COUNT(*) FROM products p JOIN shops s ON s.id=p.shop_id" + where, params).fetchone()[0]
                rows = con.execute("""SELECT p.id,p.shop_id,s.name AS shop_name,p.category_id,p.name,p.description,
                    p.price,p.currency,p.image_url,p.group_name,p.sort_order,p.is_available,p.created_at,p.updated_at
                    FROM products p JOIN shops s ON s.id=p.shop_id""" + where +
                    " ORDER BY p.group_name,p.sort_order,p.id LIMIT ? OFFSET ?",
                    (*params, limit, (page-1)*limit)).fetchall()
            return json_response(start_response, "200 OK", {"page": page, "limit": limit,
                "total": total, "products": [dict(r) for r in rows]})
        if method == "GET" and path == "/api/admin/home-dishes":
            # Home dishes (2026-11-18, Min): the dishes on the Mini App
            # home page, managed from the dashboard — the baked-in 21
            # plus as many more as he likes. Owner only.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            with connect() as con:
                _add_missing_home_dish_columns(con)
                rows = con.execute(
                    "SELECT * FROM home_dishes ORDER BY sort_order, id").fetchall()
            return json_response(start_response, "200 OK",
                                 {"dishes": [_home_dish_row(r) for r in rows]})
        if method == "POST" and path == "/api/admin/home-dishes":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            fields, err = _clean_home_dish_fields(data, True)
            if err is not None:
                return json_response(start_response, "400 Bad Request", {"error": err})
            with connect() as con:
                _add_missing_home_dish_columns(con)
                if fields.get("shop_id") is not None and not _dish_shop_ok(con, fields["shop_id"]):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only an approved shop can be linked to a dish"})
                max_order = con.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) FROM home_dishes").fetchone()[0]
                cur = con.execute(
                    "INSERT INTO home_dishes(name,description,price,currency,image_url,category,glyph,unit,sort_order,is_available,created_at,options,shop_id) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (fields["name"], fields.get("description"), fields.get("price"),
                     fields.get("currency", "THB"), fields.get("image_url"), fields["category"],
                     fields.get("glyph", "bowl"), fields.get("unit", "၁ ပွဲ"), max_order + 1, utc_now(),
                     fields.get("options"), fields.get("shop_id")))
                dish_id = cur.lastrowid
                _audit(con, auth[1], "home_dish.create", "home_dish", dish_id,
                       {"name": fields["name"], "category": fields["category"]})
            return json_response(start_response, "201 Created", {"dish_id": dish_id})
        admin_shop_products = re.fullmatch(r"/api/admin/shops/(\d+)/products", path)
        if method == "GET" and admin_shop_products:
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            shop_id = int(admin_shop_products.group(1))
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid pagination"})
            with connect() as con:
                shop = con.execute("SELECT id,name,status,verified,is_open FROM shops WHERE id=?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                if auth[0] == "merchant" and shop_id != _caller_shop_id(con, auth):
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Role not permitted for this action"})
                total = con.execute("SELECT COUNT(*) FROM products WHERE shop_id=?", (shop_id,)).fetchone()[0]
                rows = con.execute("""SELECT id,shop_id,category_id,name,description,price,currency,
                    image_url,group_name,sort_order,is_available,created_at,updated_at FROM products WHERE shop_id=?
                    ORDER BY group_name,sort_order,id LIMIT ? OFFSET ?""",
                    (shop_id, limit, (page-1)*limit)).fetchall()
            return json_response(start_response, "200 OK", {"shop": dict(shop), "page": page,
                "limit": limit, "total": total, "products": [dict(r) for r in rows]})
        if method == "GET" and path == "/api/catalogue":
            # Single-request, bounded public catalogue for mobile Mini Apps.
            # Window ranking caps products per shop without exposing owner contacts.
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                shop_limit = min(30, max(1, int(query.get("shop_limit", ["30"])[0])))
                product_limit = min(100, max(1, int(query.get("product_limit", ["100"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid catalogue limits"})
            with connect() as con:
                rows = con.execute("""WITH public_shops AS (
                    SELECT s.id,s.name,s.category,s.address
                    FROM shops s
                    WHERE s.status='active' AND s.verified=1 AND s.is_open=1
                      AND EXISTS (SELECT 1 FROM products p WHERE p.shop_id=s.id AND p.is_available=1)
                    ORDER BY s.name,s.id LIMIT ?
                ), ranked_products AS (
                    SELECT p.id,p.shop_id,p.name AS product_name,p.description AS product_description,
                           p.price,p.currency,p.image_url,p.group_name,p.sort_order, ROW_NUMBER() OVER (PARTITION BY p.shop_id ORDER BY p.group_name,p.sort_order,p.name,p.id) AS rn
                    FROM products p JOIN public_shops s ON s.id=p.shop_id
                    WHERE p.is_available=1
                )
                SELECT s.id,s.name,s.category,s.address,
                       p.id AS product_id,p.product_name,p.product_description,
                       p.price,p.currency,p.image_url,p.group_name,p.sort_order
                FROM public_shops s LEFT JOIN ranked_products p
                  ON p.shop_id=s.id AND p.rn<=?
                ORDER BY s.name,s.id,p.group_name,p.sort_order,p.product_name,p.id""", (shop_limit, product_limit)).fetchall()
            catalogue = {}
            for row in rows:
                sid = row["id"]
                if sid not in catalogue:
                    catalogue[sid] = {"id": sid, "name": row["name"], "category": row["category"],
                                      "address": row["address"], "products": []}
                if row["product_id"] is not None:
                    catalogue[sid]["products"].append({
                        "id": row["product_id"], "name": row["product_name"],
                        "description": row["product_description"], "price": row["price"],
                        "currency": row["currency"], "image_url": row["image_url"],
                        "group": row["group_name"], "sort_order": row["sort_order"]})
            return json_response(start_response, "200 OK", {"shops": list(catalogue.values()),
                "shop_limit": shop_limit, "product_limit": product_limit})
        if method == "GET" and path == "/api/shops":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid catalogue limit"})
            with connect() as con:
                rows = con.execute("SELECT s.id,s.name,s.category,s.address FROM shops s WHERE s.status='active' AND s.verified=1 AND s.is_open=1 AND EXISTS (SELECT 1 FROM products p WHERE p.shop_id=s.id AND p.is_available=1) ORDER BY s.name LIMIT ?", (limit,)).fetchall()
            return json_response(start_response, "200 OK", {"shops": [dict(r) for r in rows]})
        match = re.fullmatch(r"/api/shops/(\d+)/products", path)
        if method == "GET" and match:
            shop_id = int(match.group(1))
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                limit = min(100, max(1, int(query.get("limit", ["100"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid catalogue limit"})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1 AND is_open=1", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                rows = con.execute("SELECT id,name,description,price,currency,image_url FROM products WHERE shop_id=? AND is_available=1 ORDER BY name LIMIT ?", (shop_id, limit)).fetchall()
            return json_response(start_response, "200 OK", {"products": [dict(r) for r in rows]})
        if method == "POST" and path == "/api/merchant-applications":
            data = read_json(environ)
            required = ("shop_name", "owner_name", "phone", "address", "category")
            if any(not isinstance(data.get(k), str) or not data[k].strip() for k in required):
                return json_response(start_response, "400 Bad Request", {"error": "Missing required merchant details"})
            field_limits = {"shop_name": 160, "owner_name": 120, "phone": 40,
                            "address": 500, "category": 80, "note": 1000, "shop_message": 1000}
            if any(len(str(data.get(k) or "")) > limit for k, limit in field_limits.items()):
                return json_response(start_response, "400 Bad Request", {"error": "One or more merchant fields exceed the allowed length"})
            lat, lon = data.get("location_lat"), data.get("location_lon")
            map_link = str(data.get("map_link") or "").strip()
            if map_link:
                parsed_map = urlsplit(map_link)
                if len(map_link) > 2048 or parsed_map.scheme != "https" or not parsed_map.hostname:
                    return json_response(start_response, "400 Bad Request", {"error": "Map link must be a valid HTTPS URL"})
            if lat is not None or lon is not None:
                try:
                    lat, lon = float(lat), float(lon)
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError()
                except (TypeError, ValueError):
                    return json_response(start_response, "400 Bad Request", {"error": "Invalid GPS coordinates"})
            elif not map_link.startswith(("https://", "http://")):
                return json_response(start_response, "400 Bad Request", {"error": "A GPS pin or usable map link is required"})
            with connect() as con:
                cur = con.execute("""INSERT INTO shop_registrations
                    (created_at,shop_name,owner_name,phone,address,category,note,shop_message,location_lat,location_lon,map_link,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                    (utc_now(), str(data["shop_name"]).strip(), str(data["owner_name"]).strip(),
                     str(data["phone"]).strip(), str(data["address"]).strip(), str(data["category"]).strip(),
                     str(data.get("note") or ""), str(data.get("shop_message") or ""), lat, lon, map_link or None))
                reg_id = cur.lastrowid
            return json_response(start_response, "201 Created", {"application_id": reg_id, "status": "pending", "public_listing": False})
        if method == "POST" and path == "/api/admin/registrations/approve":
            # Shop approval is an owner decision; finance/support may not approve.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            reg_id = data.get("registration_id")
            if not isinstance(reg_id, int) or reg_id < 1:
                return json_response(start_response, "400 Bad Request", {"error": "Valid registration_id required"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                reg = con.execute("SELECT * FROM shop_registrations WHERE id=? AND status='pending'", (reg_id,)).fetchone()
                if not reg:
                    con.rollback()
                    return json_response(start_response, "404 Not Found", {"error": "Pending application not found"})
                # Owner's explicit rule: check ALL required things
                # completely and publish only if complete. An incomplete
                # registration is refused with the missing keys so the
                # dashboard can show exactly what is still needed.
                completeness = _registration_completeness(reg)
                if not completeness["complete"]:
                    con.rollback()
                    return json_response(start_response, "400 Bad Request", {
                        "error": "Registration is incomplete: "
                                 + ", ".join(completeness["missing"]),
                        "missing": completeness["missing"]})
                cur = con.execute("""INSERT INTO shops(registration_id,name,category,address,phone,latitude,longitude,verified,is_open,status,created_at)
                    VALUES(?,?,?,?,?,?,?,1,1,'active',?)""",
                    (reg_id, reg["shop_name"], reg["category"], reg["address"], reg["phone"], reg["location_lat"], reg["location_lon"], utc_now()))
                shop_id = cur.lastrowid
                con.execute("UPDATE shop_registrations SET status='approved' WHERE id=?", (reg_id,))
                _audit(con, auth[1], "registration.approve", "shop", shop_id,
                       {"registration_id": reg_id})
            return json_response(start_response, "200 OK", {"shop_id": shop_id, "status": "active"})
        if method == "POST" and path == "/api/admin/registrations/reject":
            # Rejecting a registration is an owner decision, mirroring
            # the approve endpoint's auth + transaction pattern. No
            # shop row is created. A rejected registration cannot be
            # approved later: approve only matches status='pending'.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            reg_id = data.get("registration_id")
            if not isinstance(reg_id, int) or isinstance(reg_id, bool) or reg_id < 1:
                return json_response(start_response, "400 Bad Request", {"error": "Valid registration_id required"})
            reason = data.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return json_response(start_response, "400 Bad Request", {"error": "A rejection reason is required"})
            reason = reason.strip()
            if len(reason) > 500:
                return json_response(start_response, "400 Bad Request", {"error": "Rejection reason must be at most 500 characters"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                reg = con.execute("SELECT * FROM shop_registrations WHERE id=? AND status='pending'", (reg_id,)).fetchone()
                if not reg:
                    con.rollback()
                    return json_response(start_response, "404 Not Found", {"error": "Pending application not found"})
                con.execute("UPDATE shop_registrations SET status='rejected' WHERE id=?", (reg_id,))
                _audit(con, auth[1], "registration.reject", "shop_registration", reg_id,
                       {"reason": reason, "shop_name": reg["shop_name"]})
            return json_response(start_response, "200 OK", {"registration_id": reg_id, "status": "rejected"})
        # --- Customer review moderation (v19, Min 2026-11-12) --------------
        # Reviews are born pending and only Min (owner) or operations may
        # approve/reject them; every decision is audit-logged. Rejecting
        # needs a written reason, like a registration rejection.
        if method == "GET" and path == "/api/admin/reviews":
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            status = (query.get("status", ["pending"])[0] or "").strip().lower()
            if status not in ("pending", "approved", "rejected", "all"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "status must be pending, approved, rejected or all"})
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid page or limit"})
            where = "" if status == "all" else "WHERE r.status=?"
            params = [] if status == "all" else [status]
            with connect() as con:
                total = con.execute(
                    "SELECT COUNT(*) FROM reviews r " + where, params).fetchone()[0]
                rows = con.execute(
                    "SELECT r.id,r.shop_id,r.order_id,r.stars,r.comment,r.status,r.created_at,"
                    " o.order_no, s.name AS shop_name FROM reviews r"
                    " LEFT JOIN orders o ON o.id=r.order_id"
                    " LEFT JOIN shops s ON s.id=r.shop_id "
                    + where + " ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?",
                    (*params, limit, (page - 1) * limit)).fetchall()
            return json_response(start_response, "200 OK", {
                "page": page, "limit": limit, "total": total,
                "reviews": [{"id": r["id"], "shop_id": r["shop_id"], "shop_name": r["shop_name"],
                             "order_id": r["order_id"], "order_no": r["order_no"],
                             "stars": r["stars"], "comment": r["comment"],
                             "status": r["status"], "created_at": r["created_at"]} for r in rows]})
        review_match = re.fullmatch(r"/api/admin/reviews/(\d+)/(approve|reject)", path)
        if method == "POST" and review_match:
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            review_id = int(review_match.group(1))
            action = review_match.group(2)
            data = read_json(environ)
            reason = None
            if action == "reject":
                reason = data.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "A rejection reason is required"})
                reason = reason.strip()
                if len(reason) > 500:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Rejection reason must be at most 500 characters"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT id,shop_id,order_id,stars FROM reviews WHERE id=? AND status='pending'",
                    (review_id,)).fetchone()
                if not row:
                    con.rollback()
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Pending review not found"})
                new_status = "approved" if action == "approve" else "rejected"
                con.execute("UPDATE reviews SET status=? WHERE id=?", (new_status, review_id))
                details = {"shop_id": row["shop_id"], "order_id": row["order_id"], "stars": row["stars"]}
                if reason is not None:
                    details["reason"] = reason
                _audit(con, auth[1], "review." + action, "review", review_id, details)
            return json_response(start_response, "200 OK",
                                 {"review_id": review_id, "status": new_status})
        if method == "POST" and path == "/api/admin/products":
            # Creating a product sets its price (money): owner + finance
            # only. Merchants cannot create products since v19.71
            # (availability-only via PUT); a merchant POST is 403.
            auth, error = _auth_response(environ, start_response, "owner", "finance")
            if error is not None:
                return error
            data = read_json(environ)
            try:
                shop_id, name, price = int(data["shop_id"]), str(data["name"]).strip(), float(data["price"])
                if not name or len(name) > 200 or not math.isfinite(price) or price < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                return json_response(start_response, "400 Bad Request", {"error": "Valid shop_id, name and non-negative price required"})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "400 Bad Request", {"error": "Only an approved shop can receive products"})
                currency = str(data.get("currency") or "").strip().upper()
                if currency not in {"THB", "MMK"}:
                    return json_response(start_response, "400 Bad Request", {"error": "Safe Deli currently supports THB and MMK currencies only"})
                description = str(data.get("description") or "")
                image_url = data.get("image_url")
                if len(description) > 2000:
                    return json_response(start_response, "400 Bad Request", {"error": "Product description is too long"})
                if image_url is not None:
                    parsed_image = urlsplit(image_url) if isinstance(image_url, str) else None
                    if (parsed_image is None or len(image_url) > 2048 or
                            parsed_image.scheme != "https" or not parsed_image.hostname):
                        return json_response(start_response, "400 Bad Request", {"error": "Product image must use a valid HTTPS URL"})
                # Menu organisation (2026-11-13, Min: keep it simple):
                # every dish sits in a group (e.g. Food, Drinks) and
                # has a position inside it. Blank group -> "Food".
                # A new dish goes at the end of its group.
                group_name = str(data.get("group_name") or "").strip()
                if not group_name:
                    group_name = "Food"
                if len(group_name) > 40:
                    return json_response(start_response, "400 Bad Request", {"error": "Group name must be at most 40 characters"})
                max_order = con.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) FROM products WHERE shop_id=? AND group_name=?",
                    (shop_id, group_name)).fetchone()[0]
                cur = con.execute("INSERT INTO products(shop_id,name,description,price,currency,image_url,group_name,sort_order,is_available,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                    (shop_id,name,description,price,currency,image_url,group_name,max_order + 1,utc_now()))
                product_id = cur.lastrowid
            return json_response(start_response, "201 Created", {"product_id": product_id})
        # POST /api/public/orders — server-route order intake (2026-11-12).
        # The Mini App posts the SAME payload it sends via tg.sendData
        # today, plus its Telegram initData. Accepts orders from ANY
        # Telegram launch (keyboard button, menu button, link) — the
        # menu-button launch has initData but no query_id, so sendData
        # cannot work there; this endpoint is what makes entry point
        # stop mattering. The bot pickup job then sends the same
        # confirmations as a sendData order. Gated by the
        # SAFEDELI_API_ORDERS_ENABLED cutover flag.
        # Idempotency: submission_id (UUID) + the partial unique index
        # — a retry returns the SAME order number, never a second order.
        # shop_id is stored only when the payload's shop_id resolves to
        # a real shop row (M-12); otherwise it stays NULL — a free-text
        # Mini App order may carry only the shop NAME (orders.shop), and
        # sample-shop test orders carry non-numeric ids like "sample-001".
        if method == "POST" and path == "/api/public/orders":
            if not API_ORDERS_ENABLED:
                return json_response(start_response, "503 Service Unavailable", {
                    "error": "API order intake is disabled until the single-writer order flow is enabled"})
            client_ip = (environ.get("REMOTE_ADDR") or "").strip() or "unknown"
            if _public_order_rate_limited("ip:" + client_ip, 30):
                _log_error("order_intake_429", "too many order attempts from one address")
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many order attempts. Please wait a minute and try again."})
            data = read_json(environ)
            user = _validate_init_data(data.get("initData"))
            if user is not None:
                if _public_order_rate_limited(f"user:{user['id']}", 10):
                    _log_error("order_intake_429", "too many order attempts for one Telegram user")
                    return json_response(start_response, "429 Too Many Requests",
                                         {"error": "Too many order attempts. Please wait a minute and try again."})
                order, error = _validate_public_order(data.get("order"), user["id"], user.get("username"))
            else:
                # Phone path (v19.55, Min's decision — phone number
                # only, free): a browser order with no Telegram
                # session is accepted on the phone number alone.
                # The customer_chat_id stays NULL; the customer
                # follows the order in the app (My Orders by phone)
                # and accepts the fee there. A phone order can never
                # be a test order (see _validate_public_order).
                order, error = _validate_public_order(data.get("order"), None)
                if error is None and _public_order_rate_limited(f"phone:{order['sender_phone']}", 10):
                    _log_error("order_intake_429", "too many order attempts for one phone number")
                    return json_response(start_response, "429 Too Many Requests",
                                         {"error": "Too many order attempts. Please wait a minute and try again."})
            if error is not None:
                _log_error("order_intake_400", error)
                return json_response(start_response, "400 Bad Request", {"error": error})
            # v19.15 24/7: the only 409 left is a manual close. The
            # open/close fields below describe the DELIVERY window
            # (06:00–22:00 default), not an ordering window.
            if not _server_route_can_order_now(user["id"] if user is not None else None):
                open_min, close_min = _server_route_hours_minutes()
                return json_response(start_response, "409 Conflict", {
                    "error": "closed",
                    "message": "Shop is closed",
                    "open": f"{open_min // 60:02d}:{open_min % 60:02d}",
                    "close": f"{close_min // 60:02d}:{close_min % 60:02d}"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                _add_missing_order_columns(con)
                existing = con.execute(
                    "SELECT order_no, customer_chat_id FROM orders WHERE idempotency_key=?",
                    (order["idempotency_key"],)).fetchone()
                if existing:
                    # A key can only replay the original customer's order.
                    if existing[1] != order["customer_chat_id"]:
                        con.rollback()
                        return json_response(start_response, "409 Conflict",
                                             {"error": "Order reference already belongs to another customer."})
                    con.commit()
                    return json_response(start_response, "200 OK", {
                        "order_no": existing[0], "status": "pending", "duplicate": True})
                now = datetime.now(ZoneInfo("Asia/Yangon"))
                order_no = _next_public_order_no(con, now)
                # M-12: store shop_id only when the payload's shop_id
                # resolves to a real shop row. An unresolvable id
                # (non-numeric sample ids like "sample-001", or a
                # numeric id with no matching shop) is NOT a reason to
                # reject the order — it stays NULL exactly as before,
                # with a warning carrying only the order number and the
                # shop id text (no customer data).
                shop_id_value = None
                shop_id_text = order["shop_id_text"]
                if shop_id_text:
                    try:
                        shop_id_int = int(shop_id_text)
                    except (TypeError, ValueError):
                        shop_id_int = None
                    if (shop_id_int is not None and con.execute(
                            "SELECT id FROM shops WHERE id=?",
                            (shop_id_int,)).fetchone()):
                        shop_id_value = shop_id_int
                    if shop_id_value is None:
                        logger.warning(
                            "public order %s: shop_id %r does not match a shop; storing NULL",
                            order_no, shop_id_text)
                customer_location_json = (
                    json.dumps(order["customer_location"], ensure_ascii=False)
                    if isinstance(order["customer_location"], dict) else None)
                con.execute(
                    """INSERT INTO orders
                       (order_no, created_at, customer_name, customer_chat_id,
                        sender_phone, pickup, dropoff, receiver_phone, item,
                        source, note, shop, shop_id, idempotency_key, status, order_status,
                        customer_location, is_test, picked_up, telegram_username, items_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                    (order_no, now.isoformat(timespec="seconds"),
                     order["customer_name"], order["customer_chat_id"],
                     order["sender_phone"], order["pickup"], order["dropoff"],
                     order["receiver_phone"], order["item"], "api",
                     order["note"], order["shop"], shop_id_value,
                     order["idempotency_key"],
                     "new", "pending", customer_location_json, order["is_test"],
                     order["telegram_username"], order["items_json"]))
            return json_response(start_response, "201 Created", {
                "order_no": order_no, "status": "pending", "duplicate": False})
        # POST /api/public/service-bookings — v19.47 (Min 2026-11-20).
        # The Mini App posts a service booking (school run, bus,
        # airport, hospital, shopping errands, documents, food,
        # products) so the bot can message Min on Telegram — the
        # same server-route pattern as orders. initData HMAC is
        # required (fail-closed, never unauthenticated); IP rate
        # limit 10/min. Idempotent on booking_id (SB-YYYYMMDD-NNN):
        # a retry returns the same row, never a second booking.
        # Not gated by SAFEDELI_API_ORDERS_ENABLED — bookings are
        # not orders. The table is created lazily here (the
        # codebase's own pattern) so a restart picks it up.
        if method == "POST" and path == "/api/public/service-bookings":
            client_ip = (environ.get("REMOTE_ADDR") or "").strip() or "unknown"
            if _public_order_rate_limited("ip:" + client_ip, 10):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many booking attempts. Please wait a minute and try again."})
            data = read_json(environ)
            user = _validate_init_data(data.get("initData"))
            if user is None:
                return json_response(start_response, "401 Unauthorized",
                                     {"error": "Invalid Telegram session. Open the Mini App from @SafeDeli_bot and try again."})
            if _public_order_rate_limited(f"user:{user['id']}", 5):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many booking attempts. Please wait a minute and try again."})
            bk = data.get("booking") if isinstance(data, dict) else None
            if not isinstance(bk, dict):
                return json_response(start_response, "400 Bad Request", {"error": "booking is required"})
            booking_id = (bk.get("booking_id") or "").strip()
            service = (bk.get("service") or "").strip()
            service_name = (bk.get("service_name") or "").strip()
            cname = (bk.get("customer_name") or "").strip()
            cphone = (bk.get("customer_phone") or "").strip()
            fields = bk.get("fields") if isinstance(bk.get("fields"), dict) else {}
            if not re.fullmatch(r"SB-\d{8}-\d{3}", booking_id):
                return json_response(start_response, "400 Bad Request", {"error": "booking_id is invalid"})
            if not service or len(service) > 40:
                return json_response(start_response, "400 Bad Request", {"error": "service is invalid"})
            if not service_name or len(service_name) > 100:
                return json_response(start_response, "400 Bad Request", {"error": "service_name is invalid"})
            if not cname or len(cname) > 100:
                return json_response(start_response, "400 Bad Request", {"error": "customer_name is required"})
            if not re.fullmatch(r"09[0-9]{7,9}", cphone):
                return json_response(start_response, "400 Bad Request", {"error": "customer_phone must be a Myanmar number starting with 09"})
            if len(fields) > 20:
                return json_response(start_response, "400 Bad Request", {"error": "too many fields"})
            fields_json = json.dumps({str(k)[:40]: str(v)[:500] for k, v in fields.items()}, ensure_ascii=False)
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("CREATE TABLE IF NOT EXISTS service_bookings ("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT NOT NULL UNIQUE, "
                            "service TEXT NOT NULL, service_name TEXT NOT NULL, "
                            "customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, customer_chat_id INTEGER, "
                            "fields_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new', "
                            "picked_up INTEGER NOT NULL DEFAULT 0, picked_up_at TEXT, "
                            "created_at TEXT NOT NULL)")
                # v19.53: booking-card ↩️ needs the customer chat id.
                _sb_cols = {r[1] for r in con.execute("PRAGMA table_info(service_bookings)")}
                if "customer_chat_id" not in _sb_cols:
                    con.execute("ALTER TABLE service_bookings ADD COLUMN customer_chat_id INTEGER")
                existing = con.execute("SELECT booking_id FROM service_bookings WHERE booking_id=?", (booking_id,)).fetchone()
                if existing:
                    con.commit()
                    return json_response(start_response, "200 OK", {"booking_id": booking_id, "status": "received", "duplicate": True})
                now = datetime.now(YANGON_TZ)
                con.execute("INSERT INTO service_bookings (booking_id, service, service_name, customer_name, customer_phone, customer_chat_id, fields_json, status, created_at) VALUES (?,?,?,?,?,?,?, 'new', ?)",
                            (booking_id, service, service_name, cname, cphone, user["id"], fields_json, now.isoformat(timespec="seconds")))
                con.commit()
            return json_response(start_response, "201 Created", {"booking_id": booking_id, "status": "received", "duplicate": False})
        # POST /api/public/order-receipt — receipt-photo Confirm (2026-11-14).
        # The Mini App uploads a screenshot of the order receipt after a
        # failed send. Multipart form-data: text fields initData +
        # order_no, file part "file". Validation runs in the same order as
        # POST /api/public/orders: IP rate limit, multipart parse,
        # initData, per-user rate limit, order lookup (own order only),
        # file magic bytes. The photo is stored under media/receipt/ and
        # the order row records the path + a pending flag for the bot
        # receipt sweep. No prices ride with the photo (the order itself
        # carries none); no public media URL is ever returned. Never
        # unauthenticated (M-9 lesson): initData HMAC is required,
        # fail-closed. Not gated by SAFEDELI_API_ORDERS_ENABLED — the
        # flag governs order intake, and a receipt for an already-accepted
        # order must still be attachable.
        if method == "POST" and path == "/api/public/order-receipt":
            client_ip = (environ.get("REMOTE_ADDR") or "").strip() or "unknown"
            if _public_order_rate_limited("ip:" + client_ip, 10):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many order attempts. Please wait a minute and try again."})
            try:
                fields, files = _parse_multipart(
                    environ, _MEDIA_IMAGE_MAX_BYTES + _MEDIA_SLACK)
            except _MediaTooLarge:
                return json_response(start_response, "413 Payload Too Large",
                                     {"error": "File is too large"})
            except ValueError as exc:
                return json_response(start_response, "400 Bad Request", {"error": str(exc)})
            user = _validate_init_data(fields.get("initData"))
            if user is None:
                return json_response(start_response, "401 Unauthorized",
                                     {"error": "Invalid Telegram session. Open the Mini App from @SafeDeli_bot and try again."})
            if _public_order_rate_limited(f"user:{user['id']}", 5):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many order attempts. Please wait a minute and try again."})
            order_no = (fields.get("order_no") or "").strip()
            with connect() as con:
                _add_missing_order_columns(con)
                row = con.execute(
                    "SELECT * FROM orders WHERE order_no = ?",
                    (order_no,)).fetchone()
                if row is None or row["customer_chat_id"] != user["id"]:
                    # Same response whether the order does not exist or
                    # belongs to someone else — no existence oracle.
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Order not found"})
                if row["source"] != "api":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Receipt photos can only be attached to app orders"})
                part = files.get("file")
                if part is None or not part[2]:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "No image file was provided"})
                _filename, _claimed_type, data = part
                detected = _detect_media(data)
                if (detected is None or detected[0] != "image"
                        or detected[1] not in ("jpg", "png", "webp")):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only JPG, PNG or WebP images are accepted"})
                _mtype, ext, _content_type = detected
                name = secrets.token_hex(16) + "." + ext
                dest_dir = _media_dir() / "receipt"
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    (dest_dir / name).write_bytes(data)
                except OSError:
                    return json_response(start_response, "500 Internal Server Error",
                                         {"error": "Could not save the uploaded file"})
                prev_path = row["receipt_photo_path"]
                now = datetime.now(ZoneInfo("Asia/Yangon")).isoformat(timespec="seconds")
                con.execute(
                    "UPDATE orders SET receipt_photo_path=?, receipt_photo_at=?,"
                    " receipt_photo_sent=0, receipt_file_id=NULL WHERE order_no=?",
                    ("receipt/" + name, now, order_no))
            # Delete the previous receipt file (best-effort) so re-uploads
            # do not orphan files. The stored path is always
            # "receipt/<32-hex>.<ext>" written by this handler; stay
            # inside the receipt dir regardless.
            if prev_path:
                try:
                    old = (_media_dir() / prev_path).resolve()
                    if old.parent == (_media_dir() / "receipt").resolve():
                        old.unlink(missing_ok=True)
                except OSError:
                    pass
            return json_response(start_response, "201 Created",
                                 {"ok": True, "order_no": order_no})
        # --- Admin login / session (Main Dashboard, 2026-11-11) -------------
        if method == "POST" and path == "/api/admin/login":
            data = read_json(environ)
            username = str(data.get("username") or "").strip()
            password = data.get("password")
            if not username or not isinstance(password, str) or not password:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "username and password are required"})
            if _login_locked(username):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many failed login attempts. Try again in 15 minutes."})
            with connect() as con:
                user = con.execute("SELECT * FROM admin_users WHERE username = ?",
                                   (username,)).fetchone()
                if (user is None or user["is_active"] != 1
                        or user["role"] not in ADMIN_ROLES
                        or not _verify_password(password, user["password_hash"])):
                    _login_failed(username)
                    return json_response(start_response, "401 Unauthorized",
                                         {"error": "Invalid username or password"})
                _login_succeeded(username)
                # A merchant whose shop is missing, suspended, or not
                # verified cannot log in. Generic error on purpose: no
                # detail is leaked about which part failed. (Not counted
                # as a failed login — the password was correct.)
                if user["role"] == "merchant" and not _merchant_shop_active(con, user["id"]):
                    return json_response(start_response, "401 Unauthorized",
                                         {"error": "Invalid username or password"})
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc)
                           + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(timespec="seconds")
                con.execute("INSERT INTO admin_sessions(admin_user_id,token_hash,created_at,expires_at,last_seen_at)"
                            " VALUES(?,?,?,?,?)",
                            (user["id"], _session_token_hash(token), utc_now(), expires, utc_now()))
                _audit(con, user["username"], "admin.login", "admin_user", user["id"], {})
            return json_response(start_response, "200 OK", {
                "token": token, "expires_at": expires, "username": user["username"],
                "display_name": user["display_name"], "role": user["role"]})
        if method == "POST" and path == "/api/admin/logout":
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            supplied = environ.get("HTTP_AUTHORIZATION", "") or ""
            token = supplied[len("Bearer "):].strip() if supplied.startswith("Bearer ") else ""
            if auth[2] is not None and token:
                with connect() as con:
                    con.execute("UPDATE admin_sessions SET revoked_at = ? WHERE token_hash = ?",
                                (utc_now(), _session_token_hash(token)))
            return json_response(start_response, "200 OK", {"logged_out": True})
        if method == "GET" and path == "/api/admin/me":
            auth, error = _auth_response(environ, start_response)
            if error is not None:
                return error
            role, username, user_id, selected = auth
            display_name = username
            shop_id, shop_name = None, None
            owned_shops = []
            if user_id is not None:
                with connect() as con:
                    row = con.execute("SELECT * FROM admin_users WHERE id = ?",
                                      (user_id,)).fetchone()
                    if row:
                        display_name = row["display_name"]
                    if role == "merchant":
                        # Display selection: the session's selected shop
                        # (or the primary link), named even when it is
                        # suspended — usability is enforced per request
                        # by _caller_shop_id, not here.
                        owned = _owned_shop_ids(con, user_id)
                        primary = _row_get(row, "shop_id") if row else None
                        candidate = selected if selected is not None else primary
                        if candidate is not None and candidate in owned:
                            shop_id = candidate
                            shop = con.execute("SELECT name FROM shops WHERE id = ?",
                                               (shop_id,)).fetchone()
                            shop_name = shop["name"] if shop else None
                        ordered = ([primary] if primary in owned else []) + sorted(
                            sid for sid in owned if sid != primary)
                        for sid in ordered:
                            srow = con.execute("SELECT name FROM shops WHERE id = ?", (sid,)).fetchone()
                            if srow:
                                owned_shops.append({"id": sid, "name": srow["name"]})
            payload = {"username": username, "display_name": display_name, "role": role,
                       "auth_method": "session" if user_id is not None else "legacy_token"}
            if role == "merchant":
                payload["shop_id"] = shop_id
                payload["shop_name"] = shop_name
                payload["shops"] = owned_shops
                payload["selected_shop_id"] = shop_id
            return json_response(start_response, "200 OK", payload)
        # --- Dashboard settings: global hours, delivery fee, banners,
        # registration-form fields (owner edits; every role reads) ------------
        def _read_settings(con):
            merged = _default_settings()
            for row in con.execute("SELECT key, value_json FROM dashboard_settings").fetchall():
                try:
                    value = json.loads(row["value_json"])
                except (ValueError, TypeError):
                    continue
                if row["key"] in merged:
                    merged[row["key"]] = value
            return merged
        if method == "GET" and path == "/api/admin/settings":
            # Merchants never read platform settings.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            with connect() as con:
                return json_response(start_response, "200 OK", _read_settings(con))
        if method == "PUT" and path == "/api/admin/settings":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            cleaned, err = _validate_settings_patch(data)
            if err:
                return json_response(start_response, "400 Bad Request", {"error": err})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                before = _read_settings(con)
                now = utc_now()
                for key, value in cleaned.items():
                    con.execute("INSERT INTO dashboard_settings(key,value_json,updated_at,updated_by)"
                                " VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                                " value_json=excluded.value_json,"
                                " updated_at=excluded.updated_at,"
                                " updated_by=excluded.updated_by",
                                (key, json.dumps(value, ensure_ascii=False), now, auth[1]))
                    _audit(con, auth[1], "settings.update", "setting", key,
                           {"before": before.get(key), "after": value})
                merged = _read_settings(con)
            payload = dict(merged)
            if "global_hours" in cleaned:
                sync_error = _write_shop_hours_file(cleaned["global_hours"])
                if sync_error is not None:
                    payload["shop_hours_error"] = sync_error
            return json_response(start_response, "200 OK", payload)
        # --- Shop types: owner-managed category list (2026-11-12) ----------
        # Stored as the 'shop_types' key in dashboard_settings (same
        # pattern as banners/promo_slots). The store is a FLAT list of
        # types — NO groups (Min's correction 2026-11-12 10:28: each type
        # is its own top-level category). Reads: owner/finance/support/
        # operations (merchants never read platform settings). Writes:
        # owner only. A type a shop uses cannot be deleted — disable it
        # instead. The Mini App v17 still carries its own hardcoded
        # copy; the app reading this list is the D5 catalogue wiring
        # (later follow-up after the dashboard deploy is verified live).
        if method == "GET" and path == "/api/admin/shop-types":
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            with connect() as con:
                types = _read_settings(con)["shop_types"]
            return json_response(start_response, "200 OK", {"types": types})
        if method == "POST" and path == "/api/admin/shop-types":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            with connect() as con:
                types = _read_settings(con)["shop_types"]
                cleaned, err = _validate_shop_type(data, types, is_new=True)
                if err:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                types = sorted(types, key=lambda t: t.get("sort_order", 0)) + [cleaned]
                _save_setting(con, "shop_types", types, auth[1])
                _audit(con, auth[1], "shop_type.create", "shop_type", cleaned["key"],
                       {"after": cleaned})
            return json_response(start_response, "201 Created", {"types": types})
        shop_type_match = re.fullmatch(r"/api/admin/shop-types/([A-Za-z0-9][A-Za-z0-9\-]{0,39})", path)
        if shop_type_match and method in ("PUT", "DELETE"):
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            tkey = shop_type_match.group(1)
            with connect() as con:
                types = _read_settings(con)["shop_types"]
                target = next((t for t in types if t.get("key") == tkey), None)
                if target is None:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Shop type not found"})
                if method == "DELETE":
                    if _shop_type_in_use(con, target):
                        return json_response(start_response, "409 Conflict",
                                             {"error": "This type is used by a shop, so it cannot be deleted. Disable it instead."})
                    types = [t for t in types if t.get("key") != tkey]
                    _save_setting(con, "shop_types", types, auth[1])
                    _audit(con, auth[1], "shop_type.delete", "shop_type", tkey,
                           {"before": target})
                    return json_response(start_response, "200 OK", {"types": types})
                data = read_json(environ)
                cleaned, err = _validate_shop_type(data, types, is_new=False, current_key=tkey)
                if err:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                updated = {**target, **cleaned}
                types = [updated if t.get("key") == tkey else t for t in types]
                _save_setting(con, "shop_types", types, auth[1])
                _audit(con, auth[1], "shop_type.update", "shop_type", tkey,
                       {"before": target, "after": updated})
            return json_response(start_response, "200 OK", {"types": types})
        if method == "POST" and path == "/api/admin/shops":
            # Manual add-shop (owner only, 2026-11-12): the owner adds a
            # shop directly, without a registration. Same required
            # fields as a registration; the category must be an enabled
            # shop type. A shop without a GPS pin opens closed until
            # the owner adds the pin (edit). Verified at once — the
            # owner adds only shops he has already checked.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            with connect() as con:
                types = _read_settings(con)["shop_types"]
                cleaned, err = _validate_shop_body(data, types, is_new=True)
                if err:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                # Double-submit guard (2026-11-13): extra taps on Add
                # created duplicate shops in the live DB. An identical
                # shop (same name + phone) created in the last 60
                # seconds is a retry, not a new shop — return it
                # instead of inserting a second row.
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
                dup = con.execute(
                    "SELECT id FROM shops WHERE name=? AND phone=? AND created_at>=?"
                    " ORDER BY id DESC LIMIT 1",
                    (cleaned["name"], cleaned["phone"], cutoff)).fetchone()
                if dup is not None:
                    return json_response(start_response, "200 OK",
                                         {"shop_id": dup["id"], "status": "active",
                                          "duplicate": True})
                has_pin = "latitude" in cleaned and "longitude" in cleaned
                cur = con.execute(
                    "INSERT INTO shops(registration_id,name,category,address,phone,latitude,longitude,"
                    "photo_url,area,maps_url,video_url,photo_urls,interior_photo_urls,menu_url,menu_updated_at,"
                    "verified,is_open,status,created_at) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'active',?)",
                    (cleaned["name"], cleaned["category"], cleaned["address"], cleaned["phone"],
                     cleaned.get("latitude"), cleaned.get("longitude"),
                     cleaned.get("photo_url"), cleaned.get("area"),
                     cleaned.get("maps_url"), cleaned.get("video_url"),
                     cleaned.get("photo_urls"), cleaned.get("interior_photo_urls"),
                     cleaned.get("menu_url"),
                     # A menu given at creation is a NEW menu — stamp it
                     # so the gold NEW badge shows from day one.
                     utc_now() if cleaned.get("menu_url") else None,
                     1 if has_pin else 0, utc_now()))
                shop_id = cur.lastrowid
                _audit(con, auth[1], "shop.create", "shop", shop_id, {"after": cleaned})
            return json_response(start_response, "201 Created",
                                 {"shop_id": shop_id, "status": "active"})
        shop_match = re.fullmatch(r"/api/admin/shops/(\d+)", path)
        if shop_match and method == "PUT":
            # Edit a shop's details / suspend / re-activate (owner only).
            # Status moves only active <-> suspended; pending/rejected
            # belong to the registration flow and cannot be set here.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            shop_id = int(shop_match.group(1))
            data = read_json(environ)
            with connect() as con:
                shop = con.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Shop not found"})
                types = _read_settings(con)["shop_types"]
                cleaned, err = _validate_shop_body(data, types, is_new=False)
                if err:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                if not cleaned:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Nothing to update"})
                if "menu_url" in cleaned:
                    # A NEW menu: the menu link was set or changed —
                    # stamp it so the gold NEW badge shows (Mini App +
                    # dashboard). Clearing the menu clears the stamp;
                    # sending the same link leaves the stamp alone.
                    new_menu = cleaned.get("menu_url")
                    if new_menu and new_menu != shop["menu_url"]:
                        cleaned["menu_updated_at"] = utc_now()
                    elif not new_menu:
                        cleaned["menu_updated_at"] = None
                before = dict(shop)
                sets = ", ".join(f"{k}=?" for k in cleaned)
                con.execute(f"UPDATE shops SET {sets} WHERE id=?",
                            (*cleaned.values(), shop_id))
                _audit(con, auth[1], "shop.update", "shop", shop_id,
                       {"before": {k: before.get(k) for k in cleaned}, "after": cleaned})
                updated = con.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
                updated = dict(updated)
                updated["photo_urls"] = _shop_media_list(updated.get("photo_urls"))
                updated["interior_photo_urls"] = _shop_media_list(updated.get("interior_photo_urls"))
            return json_response(start_response, "200 OK", updated)
        if shop_match and method == "DELETE":
            # Delete a shop for good (owner only). Safe by rule, not by
            # cascade: the DB enforces foreign keys (PRAGMA
            # foreign_keys=ON, no ON DELETE CASCADE), so the checks below
            # must run before the deletes, and the deletes run in one
            # transaction. Blocked when the shop has any order (order
            # history must never break) or a linked merchant account
            # (remove that account in Users first). When allowed, the
            # shop's hours row, products and reviews go with it; the
            # registration row stays as history, and media files on disk
            # stay (orphans are harmless, deleting by URL is risky).
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            shop_id = int(shop_match.group(1))
            with connect() as con:
                shop = con.execute("SELECT id,name FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Shop not found"})
                if con.execute("SELECT COUNT(*) AS n FROM orders WHERE shop_id = ?",
                               (shop_id,)).fetchone()["n"]:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "Shop has orders"})
                if con.execute("SELECT COUNT(*) AS n FROM admin_users WHERE shop_id = ?",
                               (shop_id,)).fetchone()["n"]:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "Shop has a merchant account"})
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "merchant_shops" in tables and con.execute(
                        "SELECT COUNT(*) AS n FROM merchant_shops WHERE shop_id = ?",
                        (shop_id,)).fetchone()["n"]:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "Shop has a merchant account"})
                shop_name = shop["name"]
                con.execute("DELETE FROM shop_hours WHERE shop_id = ?", (shop_id,))
                products_deleted = con.execute(
                    "DELETE FROM products WHERE shop_id = ?", (shop_id,)).rowcount
                reviews_deleted = con.execute(
                    "DELETE FROM reviews WHERE shop_id = ?", (shop_id,)).rowcount
                con.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
                _audit(con, auth[1], "shop.delete", "shop", shop_id,
                       {"name": shop_name, "products_deleted": products_deleted,
                        "reviews_deleted": reviews_deleted})
            return json_response(start_response, "200 OK",
                                 {"deleted": True, "shop_id": shop_id,
                                  "products_deleted": products_deleted,
                                  "reviews_deleted": reviews_deleted})
        if method == "GET" and path == "/api/settings/public":
            # Safe subset only (banners, hours, form fields, promos,
            # featured shops, contact) for the Mini App to fetch later.
            # No auth, no sensitive data, GET only. promo_slots is
            # capped at 8 here as well as in the admin validator.
            with connect() as con:
                settings = _read_settings(con)
            promo_slots = settings.get("promo_slots")
            featured_ids = settings.get("featured_shop_ids")
            return json_response(start_response, "200 OK", {
                "global_hours": settings["global_hours"],
                "delivery": settings["delivery"],
                "banners": [b for b in settings["banners"] if b.get("is_active")],
                "help_videos": [v for v in settings.get("help_videos", []) if v.get("is_active")],
                "promo_slots": list(promo_slots)[:8] if isinstance(promo_slots, list) else [],
                "featured_shop_ids": list(featured_ids) if isinstance(featured_ids, list) else [],
                "contact": settings["contact"],
                "music_volume": settings.get("music_volume", 50),
                "registration_form": settings["registration_form"]})
        # --- D5 public catalogue (2026-11-12) --------------------------------
        # The Mini App (GitHub Pages) reads the shop/product catalogue
        # from here — no auth, GET only. Only verified + active shops
        # are ever listed (never pending/rejected/suspended; the Mini
        # App sample shops live only in the app HTML, not in this DB,
        # so they can never leak here). No sensitive fields: no
        # owner_chat_id, no chat ids, no internal notes, no admin data,
        # no GPS coordinates. Shop phone is the shop's PUBLIC contact
        # number (shown on its card). NOTE: these handlers must stay
        # below _read_settings (defined above in this function).
        # Matrix proxy (v19.67): the Mini App asks for travel times
        # from the customer's location to every shop with coordinates,
        # to show distance/time on the shop cards and sort nearest
        # first. Same rules as /api/route: server-side token only,
        # Tachileik box only, capped point counts so one call cannot
        # burn the quota (Matrix bills per element).
        if method == "GET" and path == "/api/matrix":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)

            def _parse_points(raw):
                pts = []
                for part in raw.split(";"):
                    part = part.strip()
                    if not part:
                        continue
                    lon_s, _sep, lat_s = part.partition(",")
                    pts.append((float(lon_s), float(lat_s)))
                return pts

            try:
                origins = _parse_points(query.get("origins", [""])[0])
                dests = _parse_points(query.get("destinations", [""])[0])
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid coordinates"})
            if not origins or not dests:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid coordinates"})
            if len(origins) > 5 or len(dests) > 25 or len(origins) * len(dests) > 100:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Too many points"})
            all_pts = origins + dests
            if not all(-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0
                       for lon, lat in all_pts):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Coordinates out of range"})
            if not all(99.80 <= lon <= 99.98 and 20.38 <= lat <= 20.52
                       for lon, lat in all_pts):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Route outside the delivery area"})
            profile = (query.get("profile", ["driving"])[0] or "driving").strip()
            if profile not in ("driving", "driving-traffic", "walking"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid profile"})
            token = (os.environ.get("MAPBOX_TOKEN") or "").strip()
            if not token:
                return json_response(start_response, "503 Service Unavailable",
                                     {"error": "Route service is not configured"})
            coords = ";".join(f"{lon},{lat}" for lon, lat in all_pts)
            sources = ";".join(str(i) for i in range(len(origins)))
            destinations = ";".join(str(len(origins) + i) for i in range(len(dests)))
            upstream = ("https://api.mapbox.com/directions-matrix/v1/mapbox/" + profile + "/"
                        + coords + "?sources=" + sources + "&destinations=" + destinations
                        + "&annotations=distance,duration&access_token="
                        + quote(token, safe=""))
            try:
                req = urllib.request.Request(upstream, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                logging.exception("matrix proxy: upstream request failed")
                return json_response(start_response, "502 Bad Gateway",
                                     {"error": "Route service is unavailable"})
            durations = payload.get("durations")
            distances = payload.get("distances")
            if payload.get("code") != "Ok" or not isinstance(durations, list) \
                    or not isinstance(distances, list):
                return json_response(start_response, "502 Bad Gateway",
                                     {"error": "No route found"})
            return json_response(start_response, "200 OK", {
                "durations": durations,
                "distances": distances})
        # Route proxy (v19.66): the Mini App draws the road route from
        # the customer's location to the shop on the shop-detail map. The
        # Mapbox token lives in the server environment only (MAPBOX_TOKEN)
        # and is never sent to the client. Both points must sit inside
        # the Tachileik box so the endpoint cannot be used as an open
        # proxy for arbitrary Mapbox calls.
        if method == "GET" and path == "/api/route":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                from_lon = float(query.get("from_lon", [""])[0])
                from_lat = float(query.get("from_lat", [""])[0])
                to_lon = float(query.get("to_lon", [""])[0])
                to_lat = float(query.get("to_lat", [""])[0])
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid coordinates"})
            if not (-180.0 <= from_lon <= 180.0 and -90.0 <= from_lat <= 90.0
                    and -180.0 <= to_lon <= 180.0 and -90.0 <= to_lat <= 90.0):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Coordinates out of range"})
            if not (99.80 <= from_lon <= 99.98 and 20.38 <= from_lat <= 20.52
                    and 99.80 <= to_lon <= 99.98 and 20.38 <= to_lat <= 20.52):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Route outside the delivery area"})
            profile = (query.get("profile", ["driving"])[0] or "driving").strip()
            if profile not in ("driving", "driving-traffic", "walking"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid profile"})
            token = (os.environ.get("MAPBOX_TOKEN") or "").strip()
            if not token:
                return json_response(start_response, "503 Service Unavailable",
                                     {"error": "Route service is not configured"})
            coords = f"{from_lon},{from_lat};{to_lon},{to_lat}"
            upstream = ("https://api.mapbox.com/directions/v5/mapbox/" + profile + "/"
                        + coords + "?geometries=geojson&overview=full&access_token="
                        + quote(token, safe=""))
            try:
                req = urllib.request.Request(upstream, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                logging.exception("route proxy: upstream request failed")
                return json_response(start_response, "502 Bad Gateway",
                                     {"error": "Route service is unavailable"})
            if payload.get("code") != "Ok" or not payload.get("routes"):
                return json_response(start_response, "502 Bad Gateway",
                                     {"error": "No route found"})
            route = payload["routes"][0]
            geometry = route.get("geometry") or {}
            if geometry.get("type") != "LineString" or not geometry.get("coordinates"):
                return json_response(start_response, "502 Bad Gateway",
                                     {"error": "No route found"})
            return json_response(start_response, "200 OK", {
                "distance_m": route.get("distance"),
                "duration_s": route.get("duration"),
                "geometry": geometry})
        if method == "GET" and path == "/api/public/shops":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid catalogue limit"})
            with connect() as con:
                settings = _read_settings(con)
                global_open, global_close = _global_hours(con)
                hours_map = _shop_hours_map(con)
                featured = {s for s in (settings.get("featured_shop_ids") or [])
                            if isinstance(s, int) and not isinstance(s, bool)}
                rows = con.execute("""SELECT s.id,s.name,s.category,s.area,s.address,s.phone,
                    s.photo_url,s.maps_url,s.video_url,s.photo_urls,s.interior_photo_urls,s.menu_url,
                    s.verified,s.is_open,s.status,s.created_at,s.menu_updated_at,s.latitude,s.longitude
                    FROM shops s WHERE s.status='active' AND s.verified=1
                    ORDER BY s.name,s.id LIMIT ?""", (limit,)).fetchall()
                _ensure_shop_promo_banners(con)
                banner_rows = con.execute(
                    "SELECT id,shop_id,image_url,dish_id,dish_name,sort_order"
                    " FROM shop_promo_banners WHERE is_active=1"
                    " ORDER BY shop_id,sort_order,id").fetchall()
                banners_by_shop = {}
                for br in banner_rows:
                    banners_by_shop.setdefault(br["shop_id"], []).append({
                        "id": br["id"], "image_url": br["image_url"],
                        "dish_id": br["dish_id"], "dish_name": br["dish_name"],
                        "sort_order": br["sort_order"]})
                now_yangon = datetime.now(YANGON_TZ)
                shops = []
                for r in rows:
                    shop = dict(r)
                    hours = hours_map.get(shop["id"])
                    if hours and hours.get("open_time") and hours.get("close_time"):
                        effective = {"open": hours["open_time"], "close": hours["close_time"],
                                     "days": hours["open_days"]}
                    else:
                        effective = {"open": global_open, "close": global_close,
                                     "days": [1, 2, 3, 4, 5, 6, 0]}
                    shops.append({
                        "id": shop["id"], "name": shop["name"], "type": shop["category"],
                        "area": shop["area"], "address": shop["address"], "phone": shop["phone"],
                        "hours": effective,
                        # Open now only when the shop is flagged open AND
                        # the current Yangon time falls inside its hours.
                        "is_open": bool(shop["is_open"]) and _shop_is_open_now(
                            shop, hours_map, global_open, global_close, now_yangon),
                        "is_new": _shop_is_new(shop["created_at"], 7, now_yangon),
                        # menu_is_new: the shop's menu was set/changed
                        # within the last 7 days. The Mini App shows
                        # one gold NEW badge at the picture corner
                        # when the shop is new OR its menu is new.
                        "menu_is_new": _shop_is_new(shop.get("menu_updated_at"), 7, now_yangon),
                        "featured": shop["id"] in featured,
                        "photo_url": shop["photo_url"],
                        # Shop media (Min 2026-11-19): the video +
                        # extra photos + menu photo saved in the
                        # dashboard were never sent here, so the
                        # Mini App could not show them. Lists are
                        # stored as JSON text; parse them back.
                        "video_url": shop["video_url"],
                        "photo_urls": _shop_media_list(shop.get("photo_urls")),
                        "interior_photo_urls": _shop_media_list(shop.get("interior_photo_urls")),
                        "menu_url": shop["menu_url"],
                        # v19.47 (Min 2026-11-20): GPS IS served now —
                        # the app needs it for real map pins + nearby
                        # shops. Only active, verified shops reach
                        # this endpoint (the WHERE above), so a hidden
                        # or unverified shop never leaks a position.
                        "latitude": shop["latitude"],
                        "longitude": shop["longitude"],
                        "maps_url": shop["maps_url"],
                        "promo_banners": banners_by_shop.get(shop["id"], [])})
            return json_response(start_response, "200 OK", {"shops": shops})
        if method == "GET" and path == "/api/public/products":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            raw_shop = (query.get("shop_id", [""])[0] or "").strip()
            try:
                shop_id = int(raw_shop)
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id is required and must be an integer"})
            if shop_id < 1:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id must be a positive integer"})
            try:
                limit = min(100, max(1, int(query.get("limit", ["100"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid catalogue limit"})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1",
                                   (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                # v19.47 (Min 2026-11-20): sold-out dishes are SENT, not hidden —
                # the Mini App badges them "kout-thwa-pi". is_available
                # still rides in every row so the app can tell.
                rows = con.execute("""SELECT id,shop_id,name,price,currency,image_url,group_name,sort_order,is_available
                    FROM products WHERE shop_id=? ORDER BY group_name,sort_order,name,id LIMIT ?""",
                    (shop_id, limit)).fetchall()
            return json_response(start_response, "200 OK", {"products": [{
                "id": r["id"], "shop_id": r["shop_id"], "name": r["name"], "price": r["price"],
                "currency": r["currency"], "photo_url": r["image_url"],
                "group": r["group_name"], "sort_order": r["sort_order"],
                "is_available": bool(r["is_available"])} for r in rows]})
        if method == "GET" and path == "/api/public/home-dishes":
            # Public home dishes: what the Mini App home grid shows.
            # No auth — dish names, prices and photos are public. An
            # empty list means "use the baked-in dishes" (the app's
            # fallback), never an error.
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                limit = min(200, max(1, int(query.get("limit", ["200"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid limit"})
            with connect() as con:
                _add_missing_home_dish_columns(con)
                rows = con.execute(
                    "SELECT id,name,description,price,currency,image_url,category,glyph,unit,sort_order,options,shop_id "
                    "FROM home_dishes WHERE is_available=1 ORDER BY sort_order, id LIMIT ?",
                    (limit,)).fetchall()
            return json_response(start_response, "200 OK", {"dishes": [{
                "id": r["id"], "name": r["name"], "description": r["description"],
                "price": r["price"], "currency": r["currency"], "image_url": r["image_url"],
                "category": r["category"], "glyph": r["glyph"], "unit": r["unit"],
                "sort_order": r["sort_order"], "shop_id": r["shop_id"],
                "options": _parse_dish_options(r["options"])} for r in rows]})
        # GET /api/public/popular-today — v19.47 (Min 2026-11-20):
        # the 5 dish names ordered most TODAY (Yangon date), from
        # real orders only: test orders (is_test) and cancelled
        # orders never count. The Mini App badges a matching dish
        # card 'ဒီနေ့ လူကြိုက်များဆုံး'. Empty list = no real orders
        # yet today — honest, never faked. Matches by exact dish
        # NAME (orders carry free-text names, no product ids).
        if method == "GET" and path == "/api/public/popular-today":
            today = datetime.now(YANGON_TZ).strftime("%Y-%m-%d")
            with connect() as con:
                rows = con.execute(
                    "SELECT oi.product_name_snapshot AS name, "
                    "SUM(oi.quantity) AS qty "
                    "FROM order_items oi JOIN orders o ON o.id=oi.order_id "
                    "WHERE substr(o.created_at,1,10)=? "
                    "AND COALESCE(o.is_test,0)=0 AND COALESCE(o.order_status,'')!='cancelled' "
                    "GROUP BY oi.product_name_snapshot "
                    "ORDER BY qty DESC, name LIMIT 5",
                    (today,)).fetchall()
            return json_response(start_response, "200 OK", {"popular": [
                {"name": r["name"], "count": int(r["qty"])} for r in rows]})
        # --- Customer shop reviews (v19, Min 2026-11-12: real only,
        # never fake) --------------------------------------------------
        # GET /api/public/order-status?idempotency_key=<Mini App submission UUID>
        # The app's own orders carry no delivery status (only sent/saved),
        # so the app asks here whether its order was delivered before
        # showing the review prompt. The UUID is unguessable, and the
        # response carries no customer data — only the order number, the
        # shop id, the lifecycle status and whether a review exists.
        if method == "GET" and path == "/api/public/order-status":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            key = (query.get("idempotency_key", [""])[0] or "").strip()
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", key, re.IGNORECASE):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "idempotency_key is required and must be a UUID"})
            with connect() as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                if "idempotency_key" not in cols:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                extra = ",fee_status,delivery_fee,fee_note" if {"fee_status", "delivery_fee", "fee_note"} <= cols else ""
                row = con.execute(
                    "SELECT id,order_no,shop_id,order_status" + extra + " FROM orders WHERE idempotency_key=?",
                    (key,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                reviewed = con.execute(
                    "SELECT COUNT(*) FROM reviews WHERE order_id=?", (row["id"],)).fetchone()[0] > 0
            keys = row.keys()
            return json_response(start_response, "200 OK", {
                "order_no": row["order_no"],
                "shop_id": row["shop_id"],
                "order_status": row["order_status"] if row["order_status"] is not None else "pending",
                "fee_status": row["fee_status"] if "fee_status" in keys else None,
                "delivery_fee": row["delivery_fee"] if "delivery_fee" in keys else None,
                "fee_note": row["fee_note"] if "fee_note" in keys else None,
                "reviewed": reviewed})
        # GET /api/public/my-orders?phone=<Myanmar number> — v19.55
        # (Min's decision: phone number only, free). A browser
        # customer gets their account back with just their phone
        # number: the orders placed with it (latest 20, non-test),
        # with their fee state, so the app can show the fee-accept
        # buttons. The phone number IS the key — anyone who knows it
        # sees these orders (Min accepted this tradeoff).
        if method == "GET" and path == "/api/public/my-orders":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            phone = _normalize_mm_phone_api((query.get("phone", [""])[0] or ""))
            if not phone:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "phone must be a Myanmar number starting with 09"})
            client_ip = (environ.get("REMOTE_ADDR") or "").strip() or "unknown"
            if _public_order_rate_limited("myorders:" + client_ip, 30):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many requests. Please wait a minute and try again."})
            with connect() as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                if "sender_phone" not in cols:
                    return json_response(start_response, "200 OK", {"orders": []})
                extra = ",fee_status,delivery_fee,fee_note" if {"fee_status", "delivery_fee", "fee_note"} <= cols else ""
                rows = con.execute(
                    "SELECT order_no,created_at,shop,item,order_status" + extra +
                    " FROM orders WHERE sender_phone=? AND COALESCE(is_test,0)=0"
                    " ORDER BY created_at DESC,id DESC LIMIT 20", (phone,)).fetchall()
            orders = []
            for r in rows:
                keys = r.keys()
                orders.append({
                    "order_no": r["order_no"], "created_at": r["created_at"],
                    "shop": r["shop"], "item": r["item"],
                    "order_status": r["order_status"] if r["order_status"] is not None else "pending",
                    "delivery_fee": r["delivery_fee"] if "delivery_fee" in keys else None,
                    "fee_status": r["fee_status"] if "fee_status" in keys else None,
                    "fee_note": r["fee_note"] if "fee_note" in keys else None,
                })
            return json_response(start_response, "200 OK", {"orders": orders})
        # POST /api/public/orders/<order_no>/fee-decision — v19.55: a
        # phone customer accepts/declines the delivery fee IN THE APP
        # (the bot cannot message them — they have no Telegram chat
        # id). The phone number must match the order's sender_phone,
        # and the fee must still be pending (atomic UPDATE — one
        # decision only). On accept the bot's phone-fee job sends the
        # order to the shop; on decline the order is cancelled and the
        # shop never hears.
        fee_decision_match = re.fullmatch(r"/api/public/orders/(SD-\d{8}-\d{3})/fee-decision", path)
        if method == "POST" and fee_decision_match:
            data = read_json(environ)
            phone = _normalize_mm_phone_api(str(data.get("phone") or ""))
            decision = str(data.get("decision") or "").strip().lower()
            if not phone:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "phone must be a Myanmar number starting with 09"})
            if decision not in ("accept", "decline"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "decision must be accept or decline"})
            order_no = fee_decision_match.group(1)
            client_ip = (environ.get("REMOTE_ADDR") or "").strip() or "unknown"
            if _public_order_rate_limited("feedecision:" + client_ip, 30):
                return json_response(start_response, "429 Too Many Requests",
                                     {"error": "Too many requests. Please wait a minute and try again."})
            with connect() as con:
                _add_missing_order_columns(con)
                row = con.execute("SELECT id,sender_phone,fee_status,delivery_fee FROM orders WHERE order_no=?", (order_no,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                if (row["sender_phone"] or "") != phone:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "This order belongs to another phone number"})
                new_status = "accepted" if decision == "accept" else "declined"
                cur = con.execute("UPDATE orders SET fee_status=? WHERE id=? AND fee_status='pending'",
                                  (new_status, row["id"]))
                if cur.rowcount != 1:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "The fee has already been decided"})
                if decision == "decline":
                    con.execute("UPDATE orders SET order_status='cancelled' WHERE id=?", (row["id"],))
                con.commit()
            return json_response(start_response, "200 OK", {"order_no": order_no, "fee_status": new_status,
                                                            "delivery_fee": row["delivery_fee"]})
        # GET /api/public/reviews?shop_id=<id> — APPROVED reviews only,
        # plus the average + count computed from approved rows alone.
        # Zero approved reviews -> average null, count 0, empty list:
        # never a fake number, never an invented review.
        if method == "GET" and path == "/api/public/reviews":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            raw_shop = (query.get("shop_id", [""])[0] or "").strip()
            try:
                shop_id = int(raw_shop)
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id is required and must be an integer"})
            if shop_id < 1:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id must be a positive integer"})
            try:
                limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid limit"})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id=?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                agg = con.execute(
                    "SELECT COUNT(*), AVG(stars) FROM reviews WHERE shop_id=? AND status='approved'",
                    (shop_id,)).fetchone()
                rows = con.execute(
                    "SELECT id,stars,comment,created_at FROM reviews"
                    " WHERE shop_id=? AND status='approved'"
                    " ORDER BY created_at DESC,id DESC LIMIT ?",
                    (shop_id, limit)).fetchall()
            count = agg[0]
            average = round(agg[1], 1) if agg[1] is not None else None
            return json_response(start_response, "200 OK", {
                "shop_id": shop_id, "average": average, "count": count,
                "reviews": [{"id": r["id"], "stars": r["stars"],
                             "comment": r["comment"], "created_at": r["created_at"]} for r in rows]})
        # POST /api/public/reviews — one real review per delivered order.
        # The order is identified by its number (SD-...) or by the Mini
        # App submission id (idempotency_key). The order must exist,
        # belong to this shop, be delivered, not be a test order, and
        # have no review yet. Stars 1-5 (integer), comment required
        # (max 500 chars). Born pending; Min approves it in the dashboard.
        if method == "POST" and path == "/api/public/reviews":
            data = read_json(environ)
            shop_id = data.get("shop_id")
            if isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id < 1:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id is required and must be a positive integer"})
            stars = data.get("stars")
            if isinstance(stars, bool) or not isinstance(stars, int) or not 1 <= stars <= 5:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "stars must be an integer from 1 to 5"})
            comment = data.get("comment")
            if not isinstance(comment, str) or not comment.strip():
                return json_response(start_response, "400 Bad Request",
                                     {"error": "comment is required"})
            comment = comment.strip()
            if len(comment) > 500:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "comment must be at most 500 characters"})
            order_no = data.get("order_no")
            submission_id = data.get("submission_id")
            if order_no is not None and not isinstance(order_no, str):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "order_no must be a string"})
            if submission_id is not None and not isinstance(submission_id, str):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "submission_id must be a string"})
            order_no = (order_no or "").strip()
            submission_id = (submission_id or "").strip()
            if not order_no and not submission_id:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "order_no or submission_id is required"})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id=?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found", {"error": "Shop not found"})
                order = None
                if order_no:
                    order = con.execute(
                        "SELECT id,order_no,shop_id,order_status,is_test FROM orders WHERE order_no=?",
                        (order_no,)).fetchone()
                if order is None and submission_id:
                    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                    if "idempotency_key" in cols:
                        order = con.execute(
                            "SELECT id,order_no,shop_id,order_status,is_test FROM orders WHERE idempotency_key=?",
                            (submission_id,)).fetchone()
                if order is None:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                if order["shop_id"] != shop_id:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Order does not belong to this shop"})
                if order["is_test"]:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Test orders cannot be reviewed"})
                status = order["order_status"] if order["order_status"] is not None else "pending"
                if status != "delivered":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only delivered orders can be reviewed"})
                existing = con.execute(
                    "SELECT id FROM reviews WHERE order_id=?", (order["id"],)).fetchone()
                if existing:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "This order already has a review"})
                con.execute("BEGIN IMMEDIATE")
                try:
                    cur = con.execute(
                        "INSERT INTO reviews(shop_id,order_id,stars,comment,status,created_at)"
                        " VALUES(?,?,?,?,'pending',?)",
                        (shop_id, order["id"], stars, comment, utc_now()))
                    review_id = cur.lastrowid
                except sqlite3.IntegrityError:
                    con.rollback()
                    return json_response(start_response, "409 Conflict",
                                         {"error": "This order already has a review"})
            return json_response(start_response, "201 Created",
                                 {"review_id": review_id, "status": "pending"})
        # --- Per-shop hours --------------------------------------------------
        hours_match = re.fullmatch(r"/api/admin/shops/(\d+)/hours", path)
        if hours_match and method in ("GET", "PUT"):
            shop_id = int(hours_match.group(1))
            if method == "GET":
                auth, error = _auth_response(environ, start_response)
                if error is not None:
                    return error
                with connect() as con:
                    shop = con.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone()
                    if not shop:
                        return json_response(start_response, "404 Not Found",
                                             {"error": "Shop not found"})
                    if auth[0] == "merchant" and shop_id != _caller_shop_id(con, auth):
                        return json_response(start_response, "403 Forbidden",
                                             {"error": "Role not permitted for this action"})
                    row = con.execute("SELECT open_time,close_time,open_days,closed_dates,"
                                      "updated_at,updated_by FROM shop_hours WHERE shop_id = ?",
                                      (shop_id,)).fetchone()
                    settings = _read_settings(con)
                if row is None:
                    return json_response(start_response, "200 OK", {
                        "shop_id": shop_id, "custom": False,
                        "open_time": settings["global_hours"]["open"],
                        "close_time": settings["global_hours"]["close"],
                        "open_days": [1, 2, 3, 4, 5, 6, 0], "closed_dates": [],
                        "effective_open": settings["global_hours"]["open"],
                        "effective_close": settings["global_hours"]["close"]})
                return json_response(start_response, "200 OK", {
                    "shop_id": shop_id, "custom": True,
                    "open_time": row["open_time"], "close_time": row["close_time"],
                    "open_days": json.loads(row["open_days"]),
                    "closed_dates": json.loads(row["closed_dates"]),
                    "effective_open": row["open_time"], "effective_close": row["close_time"],
                    "updated_at": row["updated_at"], "updated_by": row["updated_by"]})
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            cleaned, err = _validate_shop_hours(data)
            if err:
                return json_response(start_response, "400 Bad Request", {"error": err})
            with connect() as con:
                shop = con.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Shop not found"})
                before_row = con.execute("SELECT open_time,close_time,open_days,closed_dates"
                                         " FROM shop_hours WHERE shop_id = ?", (shop_id,)).fetchone()
                before = dict(before_row) if before_row else None
                con.execute("INSERT INTO shop_hours(shop_id,open_time,close_time,open_days,"
                            "closed_dates,updated_at,updated_by) VALUES(?,?,?,?,?,?,?)"
                            " ON CONFLICT(shop_id) DO UPDATE SET"
                            " open_time=excluded.open_time, close_time=excluded.close_time,"
                            " open_days=excluded.open_days, closed_dates=excluded.closed_dates,"
                            " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                            (shop_id, cleaned["open_time"], cleaned["close_time"],
                             json.dumps(cleaned["open_days"]), json.dumps(cleaned["closed_dates"]),
                             utc_now(), auth[1]))
                _audit(con, auth[1], "shop.hours.update", "shop", shop_id,
                       {"before": before, "after": cleaned})
            return json_response(start_response, "200 OK",
                                 {"shop_id": shop_id, "custom": True, **cleaned})
        # --- Product price / availability (money: owner + finance) -----------
        product_match = re.fullmatch(r"/api/admin/products/(\d+)", path)
        if method == "PUT" and product_match:
            # Price/availability/photo: owner + finance, and a merchant
            # for their own shop's products only. A dish's photo is
            # set here (image_url) — the dashboard Products form
            # uploads it (POST /api/admin/media, kind=product) and
            # the Mini App Menu card shows it.
            auth, error = _auth_response(environ, start_response, "owner", "finance", "merchant")
            if error is not None:
                return error
            product_id = int(product_match.group(1))
            data = read_json(environ)
            if not isinstance(data, dict) or not data:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No changes provided"})
            if auth[0] == "merchant" and set(data) - {"is_available"}:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Merchants may only change is_available"})
            price, available = data.get("price"), data.get("is_available")
            has_image = "image_url" in data
            image_url = data.get("image_url")
            has_group = "group_name" in data
            group_name = data.get("group_name")
            has_order = "sort_order" in data
            sort_order = data.get("sort_order")
            has_name = "name" in data
            name = data.get("name")
            has_desc = "description" in data
            description = data.get("description")
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))
                                      or not math.isfinite(price) or price < 0):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "price must be a non-negative number"})
            if available is not None and not isinstance(available, bool):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "is_available must be true or false"})
            if has_image:
                # Null/blank clears the photo; a real URL must be HTTPS.
                if isinstance(image_url, str) and not image_url.strip():
                    image_url = None
                if image_url is not None and (not isinstance(image_url, str)
                                              or len(image_url) > 2048
                                              or not _is_https_url(image_url)):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "image_url must be a valid HTTPS URL or null"})
            if has_group:
                # Blank group is ignored (a dish always stays in a
                # group); a real group is at most 40 characters.
                if isinstance(group_name, str) and not group_name.strip():
                    has_group = False
                    group_name = None
                elif (not isinstance(group_name, str) or len(group_name.strip()) > 40):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Group name must be at most 40 characters"})
                else:
                    group_name = group_name.strip()
            if has_order:
                if (isinstance(sort_order, bool) or not isinstance(sort_order, int)
                        or sort_order < 0):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "sort_order must be a non-negative integer"})
            if has_name:
                if (not isinstance(name, str) or not name.strip()
                        or len(name.strip()) > 120):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "name must be 1-120 characters"})
                name = name.strip()
            if has_desc:
                if description is not None and (not isinstance(description, str)
                        or len(description) > 500):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "description must be at most 500 characters"})
                if isinstance(description, str) and not description.strip():
                    description = None
            if (price is None and available is None and not has_image
                    and not has_group and not has_order and not has_name and not has_desc):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Provide name and/or price and/or is_available and/or image_url and/or group_name and/or sort_order"})
            with connect() as con:
                product = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
                if not product:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Product not found"})
                if auth[0] == "merchant" and product["shop_id"] != _caller_shop_id(con, auth):
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Role not permitted for this action"})
                before = {"name": product["name"],
                          "description": product["description"],
                          "price": product["price"],
                          "is_available": bool(product["is_available"]),
                          "image_url": product["image_url"],
                          "group_name": product["group_name"],
                          "sort_order": product["sort_order"]}
                new_name = name if has_name else product["name"]
                new_desc = description if has_desc else product["description"]
                new_price = float(price) if price is not None else product["price"]
                new_avail = (1 if available else 0) if available is not None else product["is_available"]
                new_image = image_url if has_image else product["image_url"]
                new_group = group_name if has_group else product["group_name"]
                # Moving to a new group puts the dish at the end of
                # that group (unless an explicit position is given).
                if has_group and new_group != product["group_name"] and not has_order:
                    max_order = con.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) FROM products WHERE shop_id=? AND group_name=?",
                        (product["shop_id"], new_group)).fetchone()[0]
                    new_order = max_order + 1
                else:
                    new_order = sort_order if has_order else product["sort_order"]
                con.execute("UPDATE products SET name = ?, description = ?, price = ?, is_available = ?, image_url = ?, group_name = ?, sort_order = ?, updated_at = ? WHERE id = ?",
                            (new_name, new_desc, new_price, new_avail, new_image, new_group, new_order, utc_now(), product_id))
                _audit(con, auth[1], "product.price.update", "product", product_id,
                       {"before": before,
                        "after": {"name": new_name, "description": new_desc,
                                  "price": new_price, "is_available": bool(new_avail),
                                  "image_url": new_image, "group_name": new_group,
                                  "sort_order": new_order}})
                row = con.execute("SELECT p.id,p.shop_id,s.name AS shop_name,p.name,p.description,"
                                  "p.price,p.currency,p.image_url,p.group_name,p.sort_order,p.is_available,p.created_at,p.updated_at"
                                  " FROM products p JOIN shops s ON s.id = p.shop_id WHERE p.id = ?",
                                  (product_id,)).fetchone()
            return json_response(start_response, "200 OK", {"product": dict(row)})
        home_dish_match = re.fullmatch(r"/api/admin/home-dishes/(\d+)", path)
        if home_dish_match and method in ("PUT", "DELETE"):
            # Home-dish edit / delete (2026-11-18, Min): owner only.
            # PUT updates any subset of fields; DELETE removes the dish
            # outright (home dishes have no orders, so nothing breaks).
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            dish_id = int(home_dish_match.group(1))
            with connect() as con:
                _add_missing_home_dish_columns(con)
                dish = con.execute("SELECT * FROM home_dishes WHERE id = ?", (dish_id,)).fetchone()
                if not dish:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Home dish not found"})
                if method == "DELETE":
                    con.execute("DELETE FROM home_dishes WHERE id = ?", (dish_id,))
                    _audit(con, auth[1], "home_dish.delete", "home_dish", dish_id,
                           {"name": dish["name"]})
                    return json_response(start_response, "200 OK",
                                         {"deleted": True, "dish_id": dish_id})
            data = read_json(environ)
            fields, err = _clean_home_dish_fields(data, False)
            if err is not None:
                return json_response(start_response, "400 Bad Request", {"error": err})
            if not fields:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No changes provided"})
            sets = ", ".join(k + " = ?" for k in fields)
            params = list(fields.values())
            with connect() as con:
                if fields.get("shop_id") is not None and not _dish_shop_ok(con, fields["shop_id"]):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only an approved shop can be linked to a dish"})
                con.execute("UPDATE home_dishes SET " + sets + ", updated_at = ? WHERE id = ?",
                            (*params, utc_now(), dish_id))
                _audit(con, auth[1], "home_dish.update", "home_dish", dish_id,
                       {"before": {k: dish[k] for k in fields}, "after": fields})
                row = con.execute("SELECT * FROM home_dishes WHERE id = ?", (dish_id,)).fetchone()
            return json_response(start_response, "200 OK", {"dish": _home_dish_row(row)})
        # --- Financial ledger (money: owner + finance; append-only) ----------
        if method in ("GET", "POST") and path == "/api/admin/ledger":
            auth, error = _auth_response(environ, start_response, "owner", "finance")
            if error is not None:
                return error
            if method == "GET":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
                try:
                    page = max(1, int(query.get("page", ["1"])[0]))
                    limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
                except (ValueError, TypeError):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Invalid pagination"})
                clauses, params = [], []
                for key, column in (("order_id", "order_id"), ("shop_id", "shop_id"),
                                    ("entry_type", "entry_type")):
                    value = (query.get(key, [""])[0] or "").strip()
                    if value:
                        if key in ("order_id", "shop_id"):
                            try:
                                params.append(int(value))
                            except ValueError:
                                return json_response(start_response, "400 Bad Request",
                                                     {"error": "Invalid " + key})
                        else:
                            params.append(value)
                        clauses.append(f"{column}=?")
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                with connect() as con:
                    total = con.execute("SELECT COUNT(*) FROM financial_ledger" + where, params).fetchone()[0]
                    rows = con.execute("SELECT * FROM financial_ledger" + where +
                                       " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                                       (*params, limit, (page - 1) * limit)).fetchall()
                return json_response(start_response, "200 OK", {"page": page, "limit": limit,
                    "total": total, "entries": [dict(r) for r in rows]})
            data = read_json(environ)
            entry_type = data.get("entry_type")
            if entry_type not in ("order_charge", "delivery_fee", "refund",
                                  "merchant_settlement", "adjustment", "reversal"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid entry_type"})
            amount = data.get("amount")
            if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                    or not math.isfinite(amount) or amount < 0):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "amount must be a non-negative number"})
            direction = data.get("direction")
            if direction not in ("in", "out"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "direction must be 'in' or 'out'"})
            currency = str(data.get("currency") or "").strip().upper()
            if len(currency) != 3 or not currency.isalpha():
                return json_response(start_response, "400 Bad Request",
                                     {"error": "currency must be a 3-letter code"})
            order_id, shop_id = data.get("order_id"), data.get("shop_id")
            if order_id is not None and (isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "order_id must be a positive integer"})
            if shop_id is not None and (isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id < 1):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id must be a positive integer"})
            if order_id is None and shop_id is None:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "order_id or shop_id is required"})
            # Corrections are new entries with a written reason — never an
            # edit of a historical row (the table is append-only).
            reason = data.get("reason")
            if entry_type in ("adjustment", "reversal"):
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "A written reason is required for adjustments and reversals"})
            elif reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "reason must be at most 1000 characters"})
            note = data.get("note")
            if note is not None and (not isinstance(note, str) or len(note) > 1000):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "note must be at most 1000 characters"})
            payment_method = data.get("payment_method")
            if payment_method is not None and (not isinstance(payment_method, str) or len(payment_method) > 80):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "payment_method must be at most 80 characters"})
            external_reference = data.get("external_reference")
            if external_reference is not None and (not isinstance(external_reference, str) or len(external_reference) > 120):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "external_reference must be at most 120 characters"})
            with connect() as con:
                if order_id is not None and not con.execute(
                        "SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone():
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "order_id does not exist"})
                if shop_id is not None and not con.execute(
                        "SELECT 1 FROM shops WHERE id = ?", (shop_id,)).fetchone():
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "shop_id does not exist"})
                cur = con.execute(
                    "INSERT INTO financial_ledger(order_id,shop_id,entry_type,amount,direction,"
                    "currency,payment_method,external_reference,status,recorded_by_type,recorded_by_id,"
                    "note,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?, 'confirmed','admin',?,?,?,?)",
                    (order_id, shop_id, entry_type, round(float(amount), 2), direction, currency,
                     payment_method, external_reference, auth[1], reason or note or "", utc_now(), utc_now()))
                entry_id = cur.lastrowid
                _audit(con, auth[1], "ledger.create", "ledger_entry", entry_id,
                       {"entry_type": entry_type, "amount": round(float(amount), 2),
                        "direction": direction, "currency": currency,
                        "order_id": order_id, "shop_id": shop_id})
                row = con.execute("SELECT * FROM financial_ledger WHERE id = ?", (entry_id,)).fetchone()
            return json_response(start_response, "201 Created", {"entry": dict(row)})
        # --- Change log (who changed what and when; every role may read) ----
        if method == "GET" and path == "/api/admin/audit-log":
            # Merchants never read the platform-wide audit log.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Invalid pagination"})
            clauses, params = [], []
            for key, column in (("action", "action"), ("entity_type", "entity_type"),
                                ("actor_id", "actor_id")):
                value = (query.get(key, [""])[0] or "").strip()[:120]
                if value:
                    clauses.append(f"{column}=?")
                    params.append(value)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with connect() as con:
                total = con.execute("SELECT COUNT(*) FROM audit_log" + where, params).fetchone()[0]
                rows = con.execute("SELECT id,actor_type,actor_id,action,entity_type,entity_id,"
                                   "details,created_at FROM audit_log" + where +
                                   " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                                   (*params, limit, (page - 1) * limit)).fetchall()
            entries = []
            for r in rows:
                entry = dict(r)
                try:
                    entry["details"] = json.loads(entry["details"]) if entry["details"] else {}
                except (ValueError, TypeError):
                    entry["details"] = {"raw": entry["details"]}
                entries.append(entry)
            return json_response(start_response, "200 OK", {"page": page, "limit": limit,
                "total": total, "entries": entries})
        # --- Admin users (owner only) ----------------------------------------
        if method == "POST" and path == "/api/admin/users":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            username = str(data.get("username") or "").strip()
            display_name = str(data.get("display_name") or "").strip()
            role = data.get("role")
            password = data.get("password")
            if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "username must be 3-40 lowercase letters, digits, . _ -"})
            if not display_name or len(display_name) > 120:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "display_name is required (max 120 characters)"})
            if role not in ADMIN_ROLES:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "role must be owner, finance, support, operations or merchant"})
            if not isinstance(password, str) or not 10 <= len(password) <= 200:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "password must be 10-200 characters"})
            # A merchant account is linked to exactly one shop. The shop
            # must be an active, verified shop; any other role takes no
            # shop_id at all.
            shop_id = data.get("shop_id")
            if role == "merchant":
                if not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id < 1:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "A merchant account needs a valid shop_id"})
            elif shop_id is not None:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id is only used for merchant accounts"})
            with connect() as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
                has_shop_col = "shop_id" in cols
                if role == "merchant":
                    if not has_shop_col:
                        return json_response(start_response, "503 Service Unavailable",
                                             {"error": "Admin database is not migrated for merchant accounts"})
                    shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1",
                                       (shop_id,)).fetchone()
                    if not shop:
                        return json_response(start_response, "400 Bad Request",
                                             {"error": "shop_id must be an active, verified shop"})
                try:
                    if has_shop_col:
                        cur = con.execute(
                            "INSERT INTO admin_users(username,display_name,role,password_hash,is_active,shop_id,created_at)"
                            " VALUES(?,?,?,?,1,?,?)",
                            (username, display_name, role, _hash_password(password),
                             shop_id if role == "merchant" else None, utc_now()))
                    else:
                        cur = con.execute(
                            "INSERT INTO admin_users(username,display_name,role,password_hash,is_active,created_at)"
                            " VALUES(?,?,?,?,1,?)",
                            (username, display_name, role, _hash_password(password), utc_now()))
                except sqlite3.IntegrityError:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "Username already exists"})
                user_id = cur.lastrowid
                if role == "merchant" and has_shop_col:
                    _ensure_merchant_shops(con)
                    con.execute("INSERT OR IGNORE INTO merchant_shops(admin_user_id, shop_id, created_at) VALUES(?,?,?)",
                                (user_id, shop_id, utc_now()))
                _audit(con, auth[1], "admin.user.create", "admin_user", user_id,
                       {"username": username, "role": role,
                        "shop_id": shop_id if role == "merchant" else None})
            return json_response(start_response, "201 Created", {
                "id": user_id, "username": username, "display_name": display_name,
                "role": role, "is_active": 1,
                "shop_id": shop_id if role == "merchant" else None,
                "created_at": utc_now()})
        if method == "GET" and path == "/api/admin/users":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            with connect() as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
                shop_sel = "shop_id" if "shop_id" in cols else "NULL AS shop_id"
                rows = con.execute("SELECT id,username,display_name,role,is_active," + shop_sel + ",created_at,updated_at"
                                   " FROM admin_users ORDER BY id").fetchall()
            return json_response(start_response, "200 OK",
                                 {"users": [dict(r) for r in rows]})
        user_match = re.fullmatch(r"/api/admin/users/(\d+)", path)
        if method == "PUT" and user_match:
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            user_id = int(user_match.group(1))
            data = read_json(environ)
            if not isinstance(data, dict) or not data:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No changes provided"})
            display_name = data.get("display_name")
            role = data.get("role")
            is_active = data.get("is_active")
            shop_id_provided = "shop_id" in data
            shop_id = data.get("shop_id")
            if display_name is not None and (not isinstance(display_name, str)
                                             or not display_name.strip() or len(display_name) > 120):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "display_name must be 1-120 characters"})
            if role is not None and role not in ADMIN_ROLES:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "role must be owner, finance, support, operations or merchant"})
            if is_active is not None and not isinstance(is_active, bool):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "is_active must be true or false"})
            if shop_id_provided and shop_id is not None and (not isinstance(shop_id, int)
                                                             or isinstance(shop_id, bool) or shop_id < 1):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id must be a positive integer or null"})
            with connect() as con:
                user = con.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
                if not user:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Admin user not found"})
                if auth[2] is not None and auth[2] == user_id:
                    if is_active is False or (role is not None and role != user["role"]):
                        return json_response(start_response, "400 Bad Request",
                                             {"error": "You cannot change your own role or deactivate yourself"})
                before = {"display_name": user["display_name"], "role": user["role"],
                          "is_active": bool(user["is_active"]),
                          "shop_id": _row_get(user, "shop_id")}
                new_display = display_name.strip() if display_name is not None else user["display_name"]
                new_role = role if role is not None else user["role"]
                new_active = (1 if is_active else 0) if is_active is not None else user["is_active"]
                cols = {r[1] for r in con.execute("PRAGMA table_info(admin_users)")}
                has_shop_col = "shop_id" in cols
                # Shop link rules: a merchant must end up with a valid,
                # active, verified shop; any other role takes no link.
                new_shop = _row_get(user, "shop_id")
                if new_role == "merchant":
                    if not has_shop_col:
                        return json_response(start_response, "503 Service Unavailable",
                                             {"error": "Admin database is not migrated for merchant accounts"})
                    if shop_id_provided:
                        if shop_id is None:
                            return json_response(start_response, "400 Bad Request",
                                                 {"error": "A merchant account needs a valid shop_id"})
                        shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1",
                                           (shop_id,)).fetchone()
                        if not shop:
                            return json_response(start_response, "400 Bad Request",
                                                 {"error": "shop_id must be an active, verified shop"})
                        new_shop = shop_id
                    elif new_shop is None:
                        return json_response(start_response, "400 Bad Request",
                                             {"error": "A merchant account needs a valid shop_id"})
                elif shop_id_provided and shop_id is not None:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "shop_id is only used for merchant accounts"})
                else:
                    new_shop = None
                sets = "display_name = ?, role = ?, is_active = ?, updated_at = ?"
                params = [new_display, new_role, new_active, utc_now()]
                if has_shop_col:
                    sets += ", shop_id = ?"
                    params.append(new_shop)
                params.append(user_id)
                con.execute(f"UPDATE admin_users SET {sets} WHERE id = ?", params)
                if has_shop_col:
                    _ensure_merchant_shops(con)
                    if new_role == "merchant" and new_shop is not None:
                        con.execute("INSERT OR IGNORE INTO merchant_shops(admin_user_id, shop_id, created_at) VALUES(?,?,?)",
                                    (user_id, new_shop, utc_now()))
                    elif new_role != "merchant":
                        con.execute("DELETE FROM merchant_shops WHERE admin_user_id=?", (user_id,))
                        con.execute("UPDATE admin_sessions SET selected_shop_id=NULL WHERE admin_user_id=?", (user_id,))
                _audit(con, auth[1], "admin.user.update", "admin_user", user_id,
                       {"before": before,
                        "after": {"display_name": new_display, "role": new_role,
                                  "is_active": bool(new_active), "shop_id": new_shop}})
                shop_sel = "shop_id" if has_shop_col else "NULL AS shop_id"
                row = con.execute("SELECT id,username,display_name,role,is_active," + shop_sel + ",created_at,updated_at"
                                  " FROM admin_users WHERE id = ?", (user_id,)).fetchone()
            return json_response(start_response, "200 OK", {"user": dict(row)})
        # --- Merchant shop links (owner only, v19.70) ----------------------
        # Link extra shops to a merchant account so one login can switch
        # between them. The primary shop (admin_users.shop_id) is set only
        # via POST/PUT /api/admin/users and can never be unlinked here;
        # unlinking a selected shop clears stale selections in the same
        # transaction, so the next request falls back to the primary.
        user_shops_match = re.fullmatch(r"/api/admin/users/(\d+)/shops", path)
        user_shop_match = re.fullmatch(r"/api/admin/users/(\d+)/shops/(\d+)", path)
        if user_shops_match and method in ("GET", "POST"):
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            user_id = int(user_shops_match.group(1))
            with connect() as con:
                _ensure_merchant_shops(con)
                user = con.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
                if not user:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Admin user not found"})
                if user["role"] != "merchant":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only merchant accounts have linked shops"})
                primary = _row_get(user, "shop_id")
                owned = _owned_shop_ids(con, user_id)
                if method == "GET":
                    shops = []
                    ordered = ([primary] if primary in owned else []) + sorted(
                        sid for sid in owned if sid != primary)
                    for sid in ordered:
                        srow = con.execute("SELECT id, name, status, verified FROM shops WHERE id = ?", (sid,)).fetchone()
                        if srow:
                            shops.append({"id": srow["id"], "name": srow["name"],
                                          "status": srow["status"], "verified": bool(srow["verified"]),
                                          "primary": sid == primary})
                    return json_response(start_response, "200 OK",
                                         {"user_id": user_id, "primary_shop_id": primary, "shops": shops})
                data = read_json(environ)
                shop_id = data.get("shop_id")
                if not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id < 1:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "shop_id must be a positive integer"})
                shop = con.execute("SELECT id FROM shops WHERE id=? AND status='active' AND verified=1",
                                   (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "shop_id must be an active, verified shop"})
                if shop_id in owned:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Shop is already linked to this account"})
                con.execute("INSERT INTO merchant_shops(admin_user_id, shop_id, created_at) VALUES(?,?,?)",
                            (user_id, shop_id, utc_now()))
                _audit(con, auth[1], "merchant.shop.link", "admin_user", user_id,
                       {"shop_id": shop_id})
            return json_response(start_response, "201 Created",
                                 {"linked": True, "user_id": user_id, "shop_id": shop_id})
        if user_shop_match and method == "DELETE":
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            user_id = int(user_shop_match.group(1))
            shop_id = int(user_shop_match.group(2))
            with connect() as con:
                _ensure_merchant_shops(con)
                user = con.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
                if not user:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Admin user not found"})
                if user["role"] != "merchant":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only merchant accounts have linked shops"})
                if _row_get(user, "shop_id") == shop_id:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "The primary shop cannot be unlinked; change it via the account update"})
                linked = con.execute("SELECT 1 FROM merchant_shops WHERE admin_user_id=? AND shop_id=?",
                                     (user_id, shop_id)).fetchone()
                if not linked:
                    return json_response(start_response, "404 Not Found",
                                         {"error": "Shop is not linked to this account"})
                con.execute("DELETE FROM merchant_shops WHERE admin_user_id=? AND shop_id=?",
                            (user_id, shop_id))
                con.execute("UPDATE admin_sessions SET selected_shop_id=NULL"
                            " WHERE admin_user_id=? AND selected_shop_id=?",
                            (user_id, shop_id))
                _audit(con, auth[1], "merchant.shop.unlink", "admin_user", user_id,
                       {"shop_id": shop_id})
            return json_response(start_response, "200 OK",
                                 {"unlinked": True, "user_id": user_id, "shop_id": shop_id})
        # --- Deliveries + rider route (Main Dashboard, 2026-11-11) --------
        # The route link (rider -> shop pickup -> customer drop-off) is
        # recomputed on every read from the latest rider position, so the
        # admin always sees the current route. The rider's job card gets
        # the same link from the bot side (bot follow-up, not built here).
        if path == "/api/admin/deliveries" and method == "POST":
            # Creating a delivery job is an owner decision: it assigns a
            # rider to a real order and publishes the route link.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            data = read_json(environ)
            order_id = data.get("order_id")
            if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id < 1:
                return json_response(start_response, "400 Bad Request", {"error": "Valid order_id is required"})
            allowed = {"order_id", "rider_name", "rider_phone", "rider_chat_id",
                       "pickup_lat", "pickup_lon", "pickup_address",
                       "dropoff_lat", "dropoff_lon", "dropoff_address"}
            unknown = set(data) - allowed
            if unknown:
                return json_response(start_response, "400 Bad Request", {"error": f"Unknown field(s): {', '.join(sorted(unknown))}"})
            rider_name = data.get("rider_name")
            rider_phone = data.get("rider_phone")
            rider_chat_id = data.get("rider_chat_id")
            if rider_name is not None and (not isinstance(rider_name, str) or not rider_name.strip() or len(rider_name) > 120):
                return json_response(start_response, "400 Bad Request", {"error": "rider_name must be a non-empty string up to 120 characters"})
            if rider_phone is not None and (not isinstance(rider_phone, str) or not rider_phone.strip() or len(rider_phone) > 40):
                return json_response(start_response, "400 Bad Request", {"error": "rider_phone must be a non-empty string up to 40 characters"})
            if rider_chat_id is not None and (not isinstance(rider_chat_id, int) or isinstance(rider_chat_id, bool)):
                return json_response(start_response, "400 Bad Request", {"error": "rider_chat_id must be an integer"})
            pickup_lat, pickup_lon = data.get("pickup_lat"), data.get("pickup_lon")
            dropoff_lat, dropoff_lon = data.get("dropoff_lat"), data.get("dropoff_lon")
            if (pickup_lat is None) != (pickup_lon is None):
                return json_response(start_response, "400 Bad Request", {"error": "pickup_lat and pickup_lon must be given together"})
            if (dropoff_lat is None) != (dropoff_lon is None):
                return json_response(start_response, "400 Bad Request", {"error": "dropoff_lat and dropoff_lon must be given together"})
            if pickup_lat is not None and not _is_valid_lat_lon(pickup_lat, pickup_lon):
                return json_response(start_response, "400 Bad Request", {"error": "pickup_lat/lon are out of range"})
            if dropoff_lat is not None and not _is_valid_lat_lon(dropoff_lat, dropoff_lon):
                return json_response(start_response, "400 Bad Request", {"error": "dropoff_lat/lon are out of range"})
            for key in ("pickup_address", "dropoff_address"):
                value = data.get(key)
                if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 500):
                    return json_response(start_response, "400 Bad Request", {"error": f"{key} must be a non-empty string up to 500 characters"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not order:
                    con.rollback()
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                active = con.execute(
                    "SELECT id FROM deliveries WHERE order_id=? AND status NOT IN ('delivered','failed','cancelled')",
                    (order_id,)).fetchone()
                if active:
                    con.rollback()
                    return json_response(start_response, "409 Conflict",
                                         {"error": "An active delivery already exists for this order",
                                          "delivery_id": active["id"]})
                # Coordinates: explicit values win; otherwise the shop's
                # stored pin (from registration) for pickup and the order's
                # pin for drop-off; otherwise the address text as a Maps
                # fallback. Nothing is invented: a missing location is a
                # 400, not a guessed pin.
                # Bot orders have no shop_id column at all (the bot
                # stores the shop NAME in orders.shop): _row_get
                # returns None instead of raising, and the pickup
                # then comes from the order's own pickup data below.
                # Never invent a shop.
                shop = None
                shop_id = _row_get(order, "shop_id")
                if shop_id:
                    shop = con.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
                pickup_address = data.get("pickup_address")
                if pickup_lat is None and shop and shop["latitude"] is not None and shop["longitude"] is not None:
                    pickup_lat, pickup_lon = shop["latitude"], shop["longitude"]
                if pickup_lat is None:
                    parsed = _parse_lat_lon(_row_get(order, "pickup"))
                    if parsed:
                        pickup_lat, pickup_lon = parsed
                if pickup_address is None:
                    if shop and shop["address"]:
                        pickup_address = shop["address"]
                    elif _row_get(order, "pickup"):
                        pickup_address = _row_get(order, "pickup")
                dropoff_address = data.get("dropoff_address")
                if dropoff_lat is None:
                    parsed = _parse_lat_lon(_row_get(order, "dropoff"))
                    if parsed:
                        dropoff_lat, dropoff_lon = parsed
                if dropoff_address is None and _row_get(order, "dropoff"):
                    dropoff_address = _row_get(order, "dropoff")
                if _maps_point(pickup_lat, pickup_lon, pickup_address) is None:
                    con.rollback()
                    return json_response(start_response, "400 Bad Request", {"error": "Pickup location is missing: give pickup coordinates or an address"})
                if _maps_point(dropoff_lat, dropoff_lon, dropoff_address) is None:
                    con.rollback()
                    return json_response(start_response, "400 Bad Request", {"error": "Drop-off location is missing: give dropoff coordinates or an address"})
                now = utc_now()
                cur = con.execute(
                    "INSERT INTO deliveries(order_id,rider_name,rider_phone,rider_chat_id,"
                    "pickup_lat,pickup_lon,pickup_address,dropoff_lat,dropoff_lon,dropoff_address,"
                    "status,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order_id, rider_name, rider_phone, rider_chat_id,
                     pickup_lat, pickup_lon, pickup_address,
                     dropoff_lat, dropoff_lon, dropoff_address,
                     "created", now, now, auth[1]))
                delivery_id = cur.lastrowid
                con.execute(
                    "INSERT INTO delivery_events(order_id,event_type,actor_type,actor_id,created_at)"
                    " VALUES(?,'created','admin',?,?)", (order_id, auth[1], now))
                _audit(con, auth[1], "delivery.create", "delivery", delivery_id,
                       {"order_id": order_id, "order_no": _row_get(order, "order_no"), "rider_name": rider_name})
                row = con.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                return json_response(start_response, "201 Created", _delivery_public(con, row))
        if path == "/api/admin/deliveries" and method == "GET":
            # Owner, finance, support and operations may read deliveries;
            # rider phone/chat id are staff data, never customer-facing.
            # Merchants never see the platform's delivery list.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            status = (query.get("status", [""])[0] or "").strip()
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid pagination"})
            clauses, params = [], []
            if status:
                if status not in DELIVERY_STATUSES:
                    return json_response(start_response, "400 Bad Request", {"error": "Unknown delivery status"})
                clauses.append("d.status=?"); params.append(status)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with connect() as con:
                total = con.execute("SELECT COUNT(*) FROM deliveries d JOIN orders o ON o.id=d.order_id" + where, params).fetchone()[0]
                rows = con.execute(
                    "SELECT d.*, o.order_no, o.customer_name, o.shop AS order_shop FROM deliveries d"
                    " JOIN orders o ON o.id=d.order_id" + where +
                    " ORDER BY d.created_at DESC,d.id DESC LIMIT ? OFFSET ?",
                    (*params, limit, (page - 1) * limit)).fetchall()
                deliveries = [_delivery_public(con, row) for row in rows]
            return json_response(start_response, "200 OK", {"timezone": "Asia/Yangon", "page": page, "limit": limit,
                "total": total, "deliveries": deliveries})
        if path.startswith("/api/admin/deliveries/") and method == "GET":
            # Merchants never read a platform delivery record.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            try:
                delivery_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return json_response(start_response, "400 Bad Request", {"error": "Valid delivery id is required"})
            with connect() as con:
                row = con.execute(
                    "SELECT d.*, o.order_no, o.customer_name, o.shop AS order_shop FROM deliveries d"
                    " JOIN orders o ON o.id=d.order_id WHERE d.id=?", (delivery_id,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Delivery not found"})
                delivery = _delivery_public(con, row)
                events = con.execute(
                    "SELECT id,event_type,worker_ref,actor_type,actor_id,note,proof_ref,created_at"
                    " FROM delivery_events WHERE order_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
                    (row["order_id"],)).fetchall()
                delivery["events"] = [dict(e) for e in events]
            return json_response(start_response, "200 OK", delivery)
        if path.startswith("/api/admin/deliveries/") and method == "PUT":
            # Rider assignment, delivery fee and status changes are
            # owner or operations decisions (status transitions are
            # state-machine checked); a rider assignment is limited to
            # a rider who is currently approved. Every status change
            # writes an audit row.
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            try:
                delivery_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return json_response(start_response, "400 Bad Request", {"error": "Valid delivery id is required"})
            data = read_json(environ)
            allowed = {"rider_name", "rider_phone", "rider_chat_id", "status", "delivery_fee"}
            unknown = set(data) - allowed
            if unknown:
                return json_response(start_response, "400 Bad Request", {"error": f"Unknown field(s): {', '.join(sorted(unknown))}"})
            new_status = data.get("status")
            if new_status is not None and new_status not in DELIVERY_STATUSES:
                return json_response(start_response, "400 Bad Request", {"error": "Unknown delivery status"})
            if "delivery_fee" in data and data["delivery_fee"] is not None and (not isinstance(data["delivery_fee"], (int, float)) or isinstance(data["delivery_fee"], bool) or not math.isfinite(data["delivery_fee"]) or data["delivery_fee"] < 0):
                return json_response(start_response, "400 Bad Request", {"error": "delivery_fee must be a non-negative number or null"})
            if "rider_name" in data and data["rider_name"] is not None and (not isinstance(data["rider_name"], str) or not data["rider_name"].strip() or len(data["rider_name"]) > 120):
                return json_response(start_response, "400 Bad Request", {"error": "rider_name must be a non-empty string up to 120 characters"})
            if "rider_phone" in data and data["rider_phone"] is not None and (not isinstance(data["rider_phone"], str) or not data["rider_phone"].strip() or len(data["rider_phone"]) > 40):
                return json_response(start_response, "400 Bad Request", {"error": "rider_phone must be a non-empty string up to 40 characters"})
            if "rider_chat_id" in data and data["rider_chat_id"] is not None and (not isinstance(data["rider_chat_id"], int) or isinstance(data["rider_chat_id"], bool)):
                return json_response(start_response, "400 Bad Request", {"error": "rider_chat_id must be an integer"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                if not row:
                    con.rollback()
                    return json_response(start_response, "404 Not Found", {"error": "Delivery not found"})
                if new_status is not None and new_status != row["status"]:
                    if row["status"] in DELIVERY_TERMINAL or new_status not in DELIVERY_NEXT.get(row["status"], ()):
                        con.rollback()
                        return json_response(start_response, "400 Bad Request",
                                             {"error": f"Cannot move delivery from {row['status']} to {new_status}"})
                sets, params = [], []
                for key in ("rider_name", "rider_phone", "rider_chat_id", "delivery_fee"):
                    if key in data:
                        sets.append(f"{key}=?"); params.append(data[key])
                if new_status is not None and new_status != row["status"]:
                    sets.append("status=?"); params.append(new_status)
                if not sets:
                    con.rollback()
                    return json_response(start_response, "400 Bad Request", {"error": "Nothing to update"})
                sets.append("updated_at=?"); params.append(utc_now())
                params.append(delivery_id)
                con.execute(f"UPDATE deliveries SET {', '.join(sets)} WHERE id=?", params)
                if new_status is not None and new_status != row["status"]:
                    con.execute(
                        "INSERT INTO delivery_events(order_id,event_type,actor_type,actor_id,created_at)"
                        " VALUES(?,?,'admin',?,?)", (row["order_id"], new_status, auth[1], utc_now()))
                    _audit(con, auth[1], "delivery.status", "delivery", delivery_id,
                           {"from": row["status"], "to": new_status, "order_id": row["order_id"]})
                updated = con.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                return json_response(start_response, "200 OK", _delivery_public(con, updated))
        if path.startswith("/api/admin/deliveries/") and path.endswith("/position") and method == "POST":
            # Live rider positions are posted server-to-server by the bot
            # (legacy admin token) when a rider shares live location in
            # Telegram. Owner-only: a position is a delivery fact, and
            # only the bot hook (or the owner) may write it. Positions are
            # high-frequency, so they are NOT written to audit_log.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            try:
                delivery_id = int(path.split("/")[-2])
            except ValueError:
                return json_response(start_response, "400 Bad Request", {"error": "Valid delivery id is required"})
            data = read_json(environ)
            lat, lon = data.get("lat"), data.get("lon")
            if not _is_valid_lat_lon(lat, lon):
                return json_response(start_response, "400 Bad Request", {"error": "Valid lat/lon are required"})
            accuracy = data.get("accuracy_m")
            if accuracy is not None and (not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool) or not math.isfinite(accuracy) or accuracy < 0):
                return json_response(start_response, "400 Bad Request", {"error": "accuracy_m must be a non-negative number"})
            source = data.get("source") or "api"
            if source not in POSITION_SOURCES:
                return json_response(start_response, "400 Bad Request", {"error": "Unknown position source"})
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                if not row:
                    con.rollback()
                    return json_response(start_response, "404 Not Found", {"error": "Delivery not found"})
                if row["status"] in DELIVERY_TERMINAL:
                    con.rollback()
                    return json_response(start_response, "409 Conflict", {"error": "Delivery is already finished"})
                now = utc_now()
                con.execute(
                    "INSERT INTO rider_positions(delivery_id,lat,lon,accuracy_m,source,recorded_at)"
                    " VALUES(?,?,?,?,?,?)", (delivery_id, lat, lon, accuracy, source, now))
                updated = con.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                delivery = _delivery_public(con, updated)
            return json_response(start_response, "201 Created",
                                 {"delivery": delivery, "position": delivery["latest_position"],
                                  "route_url": delivery["route_url"]})
        # ---------- Unified Dashboard endpoints (2026-11-12) ----------
        # Merchant endpoints: every query is scoped by the shop linked
        # to the caller's session (never by a request parameter). A
        # merchant whose account has no shop gets 403.
        if method == "GET" and path == "/api/merchant/summary":
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT id,name,status,verified,is_open,phone,address,latitude,longitude,maps_url,video_url FROM shops WHERE id=?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
                date_from, date_to, date_err = _parse_date_range(query)
                if date_err:
                    return json_response(start_response, "400 Bad Request", {"error": date_err})
                today0 = datetime.now(ZoneInfo("Asia/Yangon")).date().isoformat()
                date_from = date_from or today0
                date_to = date_to or today0
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                has_test = "is_test" in cols
                base = "shop_id=?"
                params = [shop_id]
                if has_test:
                    base += " AND COALESCE(is_test,0)=0"
                today = datetime.now(ZoneInfo("Asia/Yangon")).date().isoformat()
                orders_total = con.execute("SELECT COUNT(*) FROM orders WHERE " + base, params).fetchone()[0]
                orders_today = con.execute("SELECT COUNT(*) FROM orders WHERE " + base + " AND substr(created_at,1,10)=?", (*params, today)).fetchone()[0]
                by_status = {r["status"]: r["total"] for r in con.execute(
                    "SELECT COALESCE(order_status,'pending') AS status, COUNT(*) AS total FROM orders WHERE " + base + " GROUP BY COALESCE(order_status,'pending')", params).fetchall()}
                products_total = con.execute("SELECT COUNT(*) FROM products WHERE shop_id=?", (shop_id,)).fetchone()[0]
                products_available = con.execute("SELECT COUNT(*) FROM products WHERE shop_id=? AND is_available=1", (shop_id,)).fetchone()[0]
                recent = con.execute(
                    "SELECT id,order_no,created_at,customer_name,item,order_status,delivery_fee FROM orders"
                    " WHERE " + base + " ORDER BY created_at DESC,id DESC LIMIT 10", params).fetchall()
                # Payout hint (D3, answered 2026-11-12): commission 0%,
                # the rider takes the full delivery fee, the owner
                # settles the shop after each completed order. Settled
                # totals are grouped by currency — currency is per shop
                # by city (Tachileik THB, other cities MMK) and is
                # recorded on the ledger rows, never assumed globally.
                settled = con.execute(
                    "SELECT currency, COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM financial_ledger"
                    " WHERE shop_id=? AND entry_type='merchant_settlement' AND status='confirmed'"
                    " GROUP BY currency", (shop_id,)).fetchall()
                where_o = "o.shop_id=?" + (" AND COALESCE(o.is_test,0)=0" if has_test else "")
                pending_n = con.execute(
                    "SELECT COUNT(*) FROM orders o WHERE " + where_o + " AND COALESCE(o.order_status,'pending')='delivered'"
                    " AND NOT EXISTS (SELECT 1 FROM financial_ledger f WHERE f.order_id=o.id AND f.entry_type='merchant_settlement')",
                    (shop_id,)).fetchone()[0]
                range_totals = _merchant_range_totals(con, shop_id, date_from, date_to, has_test)
            return json_response(start_response, "200 OK", {
                "shop": {"id": shop["id"], "name": shop["name"], "status": shop["status"],
                         "verified": bool(shop["verified"]), "is_open": bool(shop["is_open"]),
                         "phone": shop["phone"], "address": shop["address"],
                         "latitude": shop["latitude"], "longitude": shop["longitude"],
                         "maps_url": shop["maps_url"], "video_url": shop["video_url"]},
                "date_from": date_from, "date_to": date_to,
                "orders_total": orders_total, "orders_today": orders_today,
                "orders_in_range": range_totals["orders_in_range"],
                "sales_total": range_totals["sales_total"],
                "earnings_total": range_totals["earnings_total"],
                "settled_total": range_totals["settled_total"],
                "unsettled_total": range_totals["unsettled_total"],
                "sales_currency": range_totals["sales_currency"],
                "analytics": range_totals["analytics"],
                "cancelled_orders": range_totals["cancelled_orders"],
                "orders_by_status": by_status,
                "products_total": products_total, "products_available": products_available,
                "recent_orders": [{**dict(r), "order_status": r["order_status"] or "pending"} for r in recent],
                "settled_by_currency": [{"currency": r["currency"], "count": r["n"], "total": r["total"]} for r in settled],
                "pending_settlement_count": pending_n})
        if method == "POST" and path == "/api/merchant/switch-shop":
            # Multi-shop merchants (v19.70): switch the CURRENT session's
            # selected shop. The selection lives on the session row and
            # is re-validated on every request; there is no request
            # field that can forge it. Switching to an unowned,
            # suspended, or unverified shop is refused with the
            # selection unchanged.
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            data = read_json(environ)
            shop_id = data.get("shop_id")
            if not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id < 1:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "shop_id must be a positive integer"})
            supplied = environ.get("HTTP_AUTHORIZATION", "") or ""
            token = supplied[len("Bearer "):].strip() if supplied.startswith("Bearer ") else ""
            if not token:
                return json_response(start_response, "401 Unauthorized",
                                     {"error": "Admin authorization required"})
            with connect() as con:
                _ensure_merchant_shops(con)
                owned = _owned_shop_ids(con, auth[2])
                if shop_id not in owned:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Shop is not linked to this account"})
                shop = con.execute("SELECT name, status, verified FROM shops WHERE id = ?",
                                   (shop_id,)).fetchone()
                if not shop or shop["status"] != "active" or shop["verified"] != 1:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Shop is not available"})
                cur = con.execute("UPDATE admin_sessions SET selected_shop_id = ? WHERE token_hash = ?",
                                  (shop_id, _session_token_hash(token)))
                if cur.rowcount != 1:
                    return json_response(start_response, "500 Internal Server Error",
                                         {"error": "Could not save the selected shop"})
                _audit(con, auth[1], "merchant.shop.switch", "shop", shop_id,
                       {"shop_id": shop_id, "shop_name": shop["name"]})
            return json_response(start_response, "200 OK",
                                 {"shop_id": shop_id, "shop_name": shop["name"]})
        if method == "PUT" and path == "/api/merchant/shop":
            # Merchant edits ONLY their own shop's profile: phone,
            # address, GPS pin, Maps link, open/close, guide video
            # (video_url, v19.72 — same HTTPS validation as the owner
            # path). Name is locked
            # to the owner since v19.71. Status, category, verified,
            # photos, and anything money/bank related are NOT editable
            # here (audit 2026-11-24 decisions 1-3).
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            data = read_json(environ)
            if not isinstance(data, dict) or not data:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No changes provided"})
            allowed = {"address", "phone", "latitude", "longitude",
                       "is_open", "maps_url", "video_url"}
            unknown = sorted(set(data) - allowed)
            if unknown:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Unknown shop key: " + ", ".join(unknown)})
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                types = _read_settings(con)["shop_types"]
                cleaned, err = _validate_shop_body(data, types, is_new=False)
                if err:
                    return json_response(start_response, "400 Bad Request", {"error": err})
                if not cleaned:
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Nothing to update"})
                before = dict(shop)
                sets = ", ".join(f"{k}=?" for k in cleaned)
                con.execute(f"UPDATE shops SET {sets} WHERE id=?",
                            (*cleaned.values(), shop_id))
                _audit(con, auth[1], "merchant.shop.update", "shop", shop_id,
                       {"before": {k: before.get(k) for k in cleaned}, "after": cleaned})
                updated = con.execute("SELECT id,name,status,verified,is_open,phone,address,latitude,longitude,maps_url FROM shops WHERE id = ?", (shop_id,)).fetchone()
            return json_response(start_response, "200 OK", dict(updated))
        if method == "POST" and path == "/api/merchant/shop-video":
            # Merchant uploads their OWN shop's guide video (v19.72):
            # same validation and storage as the owner upload
            # (POST /api/admin/media, kind=video) — magic bytes,
            # 100 MB cap, server-generated name under media/video/ —
            # but the shop is forced from the caller's own link, never
            # from request data, and the saved video_url is written
            # to that shop at once. No bot change: the returned HTTPS
            # URL plays inline in the Mini App (Photos tab, v20.06).
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            try:
                fields, files = _parse_multipart(
                    environ, _MEDIA_VIDEO_MAX_BYTES + _MEDIA_SLACK)
            except _MediaTooLarge:
                return json_response(start_response, "413 Payload Too Large",
                                     {"error": "File is too large"})
            except ValueError as exc:
                return json_response(start_response, "400 Bad Request", {"error": str(exc)})
            part = files.get("file")
            if part is None or not part[2]:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No video file was provided"})
            _filename, _claimed_type, data = part
            detected = _detect_media(data)
            if detected is None or detected[0] != "video":
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Only MP4, WebM or MOV videos are accepted"})
            _mtype, ext, content_type = detected
            if len(data) > _MEDIA_VIDEO_MAX_BYTES:
                return json_response(start_response, "413 Payload Too Large",
                                     {"error": "File is too large (max 100 MB)"})
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT id,video_url FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                name = secrets.token_hex(16) + "." + ext
                dest_dir = _media_dir() / "video"
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    (dest_dir / name).write_bytes(data)
                except OSError:
                    return json_response(start_response, "500 Internal Server Error",
                                         {"error": "Could not save the uploaded file"})
                url = _public_base_url() + "/media/video/%s" % name
                before = shop["video_url"]
                con.execute("UPDATE shops SET video_url=? WHERE id=?", (url, shop_id))
                _audit(con, auth[1], "merchant.shop_video.upload", "shop", shop_id,
                       {"before": {"video_url": before}, "after": {"video_url": url}})
            return json_response(start_response, "201 Created",
                                 {"url": url, "kind": "video", "filename": name,
                                  "size": len(data), "content_type": content_type,
                                  "shop_id": shop_id})
        if method == "PUT" and path == "/api/merchant/hours":
            # Merchant edits their own shop_hours row: same validation
            # and upsert as the owner PUT /api/admin/shops/{id}/hours.
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            data = read_json(environ)
            cleaned, err = _validate_shop_hours(data)
            if err:
                return json_response(start_response, "400 Bad Request", {"error": err})
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                before_row = con.execute("SELECT open_time,close_time,open_days,closed_dates"
                                         " FROM shop_hours WHERE shop_id = ?", (shop_id,)).fetchone()
                before = dict(before_row) if before_row else None
                con.execute("INSERT INTO shop_hours(shop_id,open_time,close_time,open_days,"
                            "closed_dates,updated_at,updated_by) VALUES(?,?,?,?,?,?,?)"
                            " ON CONFLICT(shop_id) DO UPDATE SET"
                            " open_time=excluded.open_time, close_time=excluded.close_time,"
                            " open_days=excluded.open_days, closed_dates=excluded.closed_dates,"
                            " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                            (shop_id, cleaned["open_time"], cleaned["close_time"],
                             json.dumps(cleaned["open_days"]), json.dumps(cleaned["closed_dates"]),
                             utc_now(), auth[1]))
                _audit(con, auth[1], "merchant.shop.hours.update", "shop", shop_id,
                       {"before": before, "after": cleaned})
            return json_response(start_response, "200 OK",
                                 {"shop_id": shop_id, "custom": True, **cleaned})
        if method == "GET" and path == "/api/merchant/item-sales":
            # Per-item sales for the merchant's own shop (v19.69): same
            # date-range default as the summary (today, Asia/Yangon).
            # Cancelled/test orders excluded, delivery fee excluded.
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            date_from, date_to, date_err = _parse_date_range(query)
            if date_err:
                return json_response(start_response, "400 Bad Request", {"error": date_err})
            today0 = datetime.now(ZoneInfo("Asia/Yangon")).date().isoformat()
            date_from = date_from or today0
            date_to = date_to or today0
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                result = _merchant_item_sales(
                    con, shop_id, date_from, date_to, "is_test" in cols)
            return json_response(start_response, "200 OK", {
                "items": result["items"], "totals": result["totals"],
                "amounts_indicative": result["amounts_indicative"],
                "currency": result["currency"],
                "date_from": date_from, "date_to": date_to})
        if method == "GET" and path == "/api/merchant/menu":
            # Read-only: the merchant's OWN shop's home_dishes rows for
            # display only (audit 2026-11-24 decision 2 — view-only).
            # No write endpoint exists for this. Option groups come
            # from the row's own `options` column when present.
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                shop = con.execute("SELECT id FROM shops WHERE id = ?", (shop_id,)).fetchone()
                if not shop:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                _add_missing_home_dish_columns(con)
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                dishes = []
                if "home_dishes" in tables:
                    cols = {r[1] for r in con.execute("PRAGMA table_info(home_dishes)")}
                    # Without the shop link the rows cannot be scoped
                    # safely: fail closed with an empty menu.
                    if "shop_id" in cols:
                        rows = con.execute(
                            "SELECT id,name,price,image_url,is_available,options"
                            " FROM home_dishes WHERE shop_id=?"
                            " ORDER BY sort_order,id", (shop_id,)).fetchall()
                        dishes = [{
                            "id": r["id"], "name": r["name"], "price": r["price"],
                            "image": r["image_url"],
                            "is_available": bool(r["is_available"]),
                            "groups": _parse_dish_options(r["options"]) or [],
                        } for r in rows]
            return json_response(start_response, "200 OK",
                                 {"dishes": dishes, "shop_id": shop_id})
        if method == "GET" and path == "/api/merchant/payouts":
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                settled_rows = con.execute(
                    "SELECT id,order_id,amount,currency,status,note,created_at FROM financial_ledger"
                    " WHERE shop_id=? AND entry_type='merchant_settlement'"
                    " ORDER BY created_at DESC,id DESC LIMIT 50", (shop_id,)).fetchall()
                # Pending: delivered orders of this shop with no
                # settlement row yet. The amount comes from order_items
                # when the order was built from products; null
                # otherwise — the owner confirms the amount when
                # settling after each completed order (D3).
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                test_clause = " AND COALESCE(o.is_test,0)=0" if "is_test" in cols else ""
                pending = con.execute(
                    "SELECT o.id,o.order_no,o.created_at,o.customer_name,"
                    " (SELECT COALESCE(SUM(oi.line_total),NULL) FROM order_items oi WHERE oi.order_id=o.id) AS items_total"
                    " FROM orders o WHERE o.shop_id=? AND COALESCE(o.order_status,'pending')='delivered'" + test_clause +
                    " AND NOT EXISTS (SELECT 1 FROM financial_ledger f WHERE f.order_id=o.id AND f.entry_type='merchant_settlement')"
                    " ORDER BY o.created_at DESC,o.id DESC LIMIT 50", (shop_id,)).fetchall()
                # Currency hint: the currency of the shop's latest
                # ledger row (D3: Tachileik THB, other cities MMK).
                hint = con.execute("SELECT currency FROM financial_ledger WHERE shop_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (shop_id,)).fetchone()
            return json_response(start_response, "200 OK", {
                "shop_id": shop_id,
                "currency_hint": hint["currency"] if hint else None,
                "payout_rules": {"commission_percent": 0,
                                 "delivery_fee_to": "rider",
                                 "schedule": "after_each_order",
                                 "note": "Commission is 0% for now. The rider takes the full delivery fee. The owner settles the shop after each completed order. Currency follows the shop's city: Tachileik shops in THB, shops from other cities in MMK."},
                "settled": [dict(r) for r in settled_rows],
                "pending": [dict(r) for r in pending]})
        if method == "GET" and path == "/api/merchant/reports/sales.csv":
            auth, error = _auth_response(environ, start_response, "merchant")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            date_from, date_to, date_err = _parse_date_range(query)
            if date_err:
                return json_response(start_response, "400 Bad Request", {"error": date_err})
            with connect() as con:
                shop_id = _caller_shop_id(con, auth)
                if shop_id is None:
                    return json_response(start_response, "403 Forbidden",
                                         {"error": "Merchant account is not linked to a shop"})
                clauses = ["shop_id=?"]; params = [shop_id]
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                if "is_test" in cols:
                    clauses.append("COALESCE(is_test,0)=0")
                if date_from:
                    clauses.append("substr(created_at,1,10)>=?"); params.append(date_from)
                if date_to:
                    clauses.append("substr(created_at,1,10)<=?"); params.append(date_to)
                where = " WHERE " + " AND ".join(clauses)
                rows = con.execute("SELECT order_no,created_at,customer_name,item,order_status,delivery_fee FROM orders" + where + " ORDER BY created_at DESC,id DESC", params).fetchall()
                _audit(con, auth[1], "report.merchant_sales_csv", "report", None,
                       {"shop_id": shop_id, "rows": len(rows)})
                con.execute("INSERT INTO admin_report_runs(admin_user_id,report_type,date_from,date_to,filters_json,row_count,generated_at,export_format)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (auth[2], "merchant_sales_csv", date_from or None, date_to or None,
                             json.dumps({"shop_id": shop_id}, ensure_ascii=False), len(rows), utc_now(), "csv"))
            body = _csv_text("order_no,created_at,customer_name,item,order_status,delivery_fee",
                             [[r["order_no"], r["created_at"], r["customer_name"], r["item"], r["order_status"] or "pending", r["delivery_fee"]] for r in rows])
            return _csv_response(start_response, f"safe-deli-shop-{shop_id}-sales.csv", body)
        if method == "GET" and path == "/api/admin/customers":
            # Derived from orders (there is no customers table): one row
            # per customer phone, with order count + last order. Phones
            # are masked for support and operations; owner and finance
            # see the full number. Merchants never see the platform's
            # customer list.
            auth, error = _auth_response(environ, start_response,
                                         "owner", "finance", "support", "operations")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            search = (query.get("search", [""])[0] or "").strip()
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                return json_response(start_response, "400 Bad Request", {"error": "Invalid pagination"})
            with connect() as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                test_clause = "COALESCE(is_test,0)=0" if "is_test" in cols else "1=1"
                clauses = [test_clause]; params = []
                if search:
                    clauses.append("(COALESCE(customer_name,'') LIKE ? OR COALESCE(sender_phone,'') LIKE ?)")
                    params.extend([f"%{search}%"] * 2)
                where = " WHERE " + " AND ".join(clauses)
                total = con.execute("SELECT COUNT(*) FROM (SELECT 1 FROM orders" + where + " GROUP BY COALESCE(sender_phone,''), COALESCE(customer_name,''))", params).fetchone()[0]
                rows = con.execute(
                    "SELECT COALESCE(customer_name,'') AS customer_name, COALESCE(sender_phone,'') AS phone,"
                    " COUNT(*) AS orders, MAX(created_at) AS last_order_at, MAX(shop) AS last_shop"
                    " FROM orders" + where +
                    " GROUP BY COALESCE(sender_phone,''), COALESCE(customer_name,'')"
                    " ORDER BY last_order_at DESC LIMIT ? OFFSET ?", (*params, limit, (page-1)*limit)).fetchall()
            mask = auth[0] in ("support", "operations")
            customers = [{"customer_name": r["customer_name"],
                          "phone": _mask_phone(r["phone"]) if mask else r["phone"],
                          "orders": r["orders"], "last_order_at": r["last_order_at"],
                          "last_shop": r["last_shop"]} for r in rows]
            return json_response(start_response, "200 OK", {"page": page, "limit": limit, "total": total, "customers": customers})
        if method == "POST" and path == "/api/admin/orders/clear-test":
            # Erase TEST orders in bulk (owner only, Min 2026-11-18).
            # The dashboard's "clear TEST orders" button calls this when
            # Min is about to add real shops and wants the fake test
            # orders gone first. Scope is the is_test flag PLUS the 5
            # known-fake legacy numbers below. A non-listed real order
            # (is_test 0/NULL) is never touched. Two child tables are
            # append-only by trigger and can NEVER be deleted
            # (financial_ledger, delivery_events): a test order with
            # rows there is BLOCKED, not deleted, and its number is
            # reported back in blocked_order_nos. The other children
            # (items, status events, reviews, deliveries) are deleted
            # with the order, all in one transaction, audit-logged.
            # Legacy list (live check 2026-11-18): the 5 fake orders
            # Min wants erased are NOT flagged is_test=1
            # (SD-20261111-001 is NULL, the other 4 are 0), so the
            # flag alone would delete nothing. All 5 are confirmed
            # fake by Min; once deleted this list matches nothing and
            # the endpoint behaves as pure is_test=1.
            legacy_nos = ("SD-20261111-001", "SD-20261112-001",
                          "SD-20261114-001", "SD-20261114-002",
                          "SD-20261114-003")
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            with connect() as con:
                rows = con.execute(
                    "SELECT id,order_no FROM orders WHERE COALESCE(is_test,0)=1 OR order_no IN (?,?,?,?,?) ORDER BY id",
                    legacy_nos).fetchall()
                deletable, blocked = [], []
                for r in rows:
                    has_records = con.execute(
                        "SELECT 1 FROM financial_ledger WHERE order_id=? UNION ALL "
                        "SELECT 1 FROM delivery_events WHERE order_id=? LIMIT 1",
                        (r["id"], r["id"])).fetchone()
                    (blocked if has_records else deletable).append(r)
                ids = [r["id"] for r in deletable]
                order_nos = [r["order_no"] for r in deletable]
                blocked_nos = [r["order_no"] for r in blocked]
                if ids:
                    marks = ",".join("?" for _ in ids)
                    for table in ("order_items", "order_status_events", "reviews", "deliveries"):
                        con.execute(f"DELETE FROM {table} WHERE order_id IN ({marks})", ids)
                    con.execute(f"DELETE FROM orders WHERE id IN ({marks})", ids)
                _audit(con, auth[1], "orders.clear_test", "order", None,
                       {"order_nos": order_nos, "deleted": len(ids),
                        "blocked_order_nos": blocked_nos})
            return json_response(start_response, "200 OK",
                                 {"deleted": len(ids), "order_nos": order_nos,
                                  "blocked_order_nos": blocked_nos})
        order_status_match = re.fullmatch(r"/api/admin/orders/(\d+)/status", path)
        if method == "POST" and order_status_match:
            # Order lifecycle change (owner + operations). Only the
            # transitions in ORDER_NEXT are allowed; delivered and
            # cancelled are terminal. Every change writes an
            # order_status_events row and an audit row.
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            order_id = int(order_status_match.group(1))
            data = read_json(environ)
            new_status = data.get("status")
            note = data.get("note")
            if new_status not in ORDER_STATUSES:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "status must be one of: " + ", ".join(ORDER_STATUSES)})
            if note is not None and (not isinstance(note, str) or len(note) > 500):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "note must be a string up to 500 characters"})
            with connect() as con:
                row = con.execute("SELECT id,order_no,order_status FROM orders WHERE id=?", (order_id,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                current = row["order_status"] if row["order_status"] is not None else "pending"
                if new_status == current:
                    return json_response(start_response, "200 OK", {"id": row["id"], "order_no": row["order_no"], "order_status": current, "changed": False})
                if current in ORDER_TERMINAL or new_status not in ORDER_NEXT.get(current, ()):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": f"Cannot move order from {current} to {new_status}"})
                now = utc_now()
                con.execute("UPDATE orders SET order_status=? WHERE id=?", (new_status, order_id))
                con.execute("INSERT INTO order_status_events(order_id,status,note,actor_type,created_at) VALUES(?,?,?,?,?)",
                            (order_id, new_status, note, "admin", now))
                _audit(con, auth[1], "order.status", "order", order_id,
                       {"from": current, "to": new_status, "order_no": row["order_no"]})
            return json_response(start_response, "200 OK", {"id": order_id, "order_no": row["order_no"], "order_status": new_status, "changed": True})
        order_fee_match = re.fullmatch(r"/api/admin/orders/(\d+)/fee", path)
        if method == "POST" and order_fee_match:
            # Fee-confirm (2026-11-20, v19.54, Min's decision): Min
            # types the EXACT delivery fee per order in the Dashboard
            # (30 baht is only the minimum, never the fee itself).
            # The bot then asks the customer to accept it; only after
            # the customer accepts does the order go to the shop.
            # Setting the fee is allowed while the order is pending
            # or waiting (a held order still needs its exact fee)
            # and the customer has not decided yet. No
            # order_status_events row — the fee message IS the
            # notification, not a status change. Audit-logged.
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            order_id = int(order_fee_match.group(1))
            data = read_json(environ)
            fee = data.get("delivery_fee")
            fee_note = data.get("fee_note")
            if isinstance(fee, bool) or not isinstance(fee, (int, float)):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "delivery_fee must be a number"})
            fee = float(fee)
            if fee != fee or fee in (float("inf"), float("-inf")) or fee < 0 or fee > 100000:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "delivery_fee must be between 0 and 100000"})
            if fee_note is not None and (not isinstance(fee_note, str) or len(fee_note) > 200):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "fee_note must be a string up to 200 characters"})
            with connect() as con:
                _add_missing_order_columns(con)
                row = con.execute("SELECT id,order_no,order_status,fee_status,delivery_fee,fee_note,fee_msg_sent FROM orders WHERE id=?", (order_id,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                current = row["order_status"] if row["order_status"] is not None else "pending"
                if current not in ("pending", "waiting"):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": f"Cannot set the fee on an order that is {current}"})
                if row["fee_status"] in ("accepted", "declined"):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "The customer has already decided on the fee"})
                # v19.56 (Min 2026-11-21): re-confirming the SAME fee
                # must not re-send the fee message (the customer got it
                # 3x when Min tapped confirm 3 times). Only reset
                # fee_msg_sent when the fee or the note actually changed.
                _cur_fee = row["delivery_fee"]
                _cur_note = row["fee_note"]
                _cur_sent = row["fee_msg_sent"] or 0
                _changed = (fee != _cur_fee) or ((fee_note or None) != _cur_note)
                _new_sent = 0 if _changed else _cur_sent
                con.execute("UPDATE orders SET delivery_fee=?, fee_status='pending', fee_msg_sent=?, fee_note=? WHERE id=?",
                            (fee, _new_sent, (fee_note or None), order_id))
                _audit(con, auth[1], "order.fee", "order", order_id,
                       {"fee": fee, "fee_note": fee_note or None, "order_no": row["order_no"]})
            return json_response(start_response, "200 OK", {"id": order_id, "order_no": row["order_no"], "delivery_fee": fee, "fee_status": "pending", "fee_resent": _changed})
        order_fee_decision_match = re.fullmatch(r"/api/admin/orders/(\d+)/fee-decision", path)
        if method == "POST" and order_fee_decision_match:
            # Fee decision by Min (2026-11-21, v19.57): when the
            # customer never taps the fee ✅/❌, Min can decide for
            # them in the Dashboard instead of the order sitting
            # fee-pending forever. Accept -> the bot's fee-accept job
            # sends the order to the shop. Decline -> the order is
            # cancelled (the shop never hears) AND an
            # order_status_events row is written, so the bot's
            # status-notify job tells the customer (and Min).
            # Audit-logged.
            auth, error = _auth_response(environ, start_response, "owner", "operations")
            if error is not None:
                return error
            order_id = int(order_fee_decision_match.group(1))
            data = read_json(environ)
            decision = data.get("decision")
            if decision not in ("accepted", "declined"):
                return json_response(start_response, "400 Bad Request",
                                     {"error": "decision must be accepted or declined"})
            with connect() as con:
                _add_missing_order_columns(con)
                row = con.execute("SELECT id,order_no,order_status,fee_status FROM orders WHERE id=?", (order_id,)).fetchone()
                if not row:
                    return json_response(start_response, "404 Not Found", {"error": "Order not found"})
                current = row["order_status"] if row["order_status"] is not None else "pending"
                if current not in ("pending", "waiting"):
                    return json_response(start_response, "400 Bad Request",
                                         {"error": f"Cannot decide the fee on an order that is {current}"})
                if row["fee_status"] != "pending":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "The fee has not been set or has already been decided"})
                if decision == "accepted":
                    con.execute("UPDATE orders SET fee_status='accepted' WHERE id=?", (order_id,))
                else:
                    con.execute("UPDATE orders SET fee_status='declined', order_status='cancelled' WHERE id=?", (order_id,))
                    con.execute("INSERT INTO order_status_events(order_id,status,note,actor_type,created_at) VALUES(?,?,?,?,?)",
                                (order_id, "cancelled", None, "admin", utc_now()))
                _audit(con, auth[1], "order.fee_decision_admin", "order", order_id,
                       {"decision": decision, "order_no": row["order_no"]})
            return json_response(start_response, "200 OK", {"id": order_id, "order_no": row["order_no"], "fee_status": decision})
        if method == "GET" and path == "/api/admin/errors":
            # Errors view (v19.55, Min 2026-11-20): how Min knows when
            # something failed — the latest 100 error_log rows (order
            # intake failures, database errors). The bot also alerts
            # him on the system-error types. Owner-only (audit
            # 2026-11-24 gap B): merchants must not read the log.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            with connect() as con:
                con.execute("CREATE TABLE IF NOT EXISTS error_log (" 
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, " 
                            "message TEXT NOT NULL, order_no TEXT, " 
                            "alerted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
                rows = con.execute("SELECT id,type,message,order_no,created_at FROM error_log ORDER BY id DESC LIMIT 100").fetchall()
            return json_response(start_response, "200 OK", {"errors": [dict(r) for r in rows]})
        if method == "GET" and path == "/api/admin/reports/orders.csv":
            auth, error = _auth_response(environ, start_response, "owner", "finance")
            if error is not None:
                return error
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            date_from, date_to, date_err = _parse_date_range(query)
            if date_err:
                return json_response(start_response, "400 Bad Request", {"error": date_err})
            include_test = (query.get("include_test", [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
            search = (query.get("q", [""])[0] or "").strip()[:100]
            with connect() as con:
                clauses, params = [], []
                cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                if "is_test" in cols and not include_test:
                    clauses.append("COALESCE(is_test,0)=0")
                if date_from:
                    clauses.append("substr(created_at,1,10)>=?"); params.append(date_from)
                if date_to:
                    clauses.append("substr(created_at,1,10)<=?"); params.append(date_to)
                if search:
                    clauses.append("(COALESCE(order_no,'') LIKE ? OR COALESCE(customer_name,'') LIKE ? OR COALESCE(shop,'') LIKE ? OR COALESCE(sender_phone,'') LIKE ?)")
                    params.extend([f"%{search}%"] * 4)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                rows = con.execute("SELECT id,order_no,created_at,customer_name,sender_phone,shop,shop_id,item,order_status,delivery_fee,is_test FROM orders" + where + " ORDER BY created_at DESC,id DESC", params).fetchall()
                # Currency per shop, from the shop's latest ledger row
                # (D3: currency is per shop by city, recorded on the
                # ledger rows — never a single global currency).
                currency_by_shop = {}
                for lr in con.execute("SELECT shop_id,currency FROM financial_ledger WHERE shop_id IS NOT NULL ORDER BY created_at DESC,id DESC").fetchall():
                    currency_by_shop.setdefault(lr["shop_id"], lr["currency"])
                # Fallback for orders whose shop_id is still NULL (every
                # bot order before the reviewed 007 backfill runs):
                # resolve the shop by the exact shop NAME recorded on
                # the order, but only when exactly one shop has that
                # name — the same rule as the backfill. Ambiguous or
                # unknown names stay blank; currency is never guessed.
                _name_ids = {}
                for _s in con.execute("SELECT id, name FROM shops").fetchall():
                    _name_ids.setdefault(_s["name"], []).append(_s["id"])
                _unique_name_shop = {n: ids[0] for n, ids in _name_ids.items() if len(ids) == 1}
                _audit(con, auth[1], "report.orders_csv", "report", None,
                       {"rows": len(rows), "include_test": include_test})
                con.execute("INSERT INTO admin_report_runs(admin_user_id,report_type,date_from,date_to,filters_json,row_count,generated_at,export_format)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (auth[2], "orders_csv", date_from or None, date_to or None,
                             json.dumps({"include_test": include_test}, ensure_ascii=False), len(rows), utc_now(), "csv"))
            def _fee_currency(row):
                sid = row["shop_id"]
                if sid is None and row["shop"]:
                    sid = _unique_name_shop.get(row["shop"])
                return currency_by_shop.get(sid, "") if sid is not None else ""
            body = _csv_text("order_no,created_at,customer_name,phone,shop,item,order_status,delivery_fee,fee_currency,is_test",
                             [[r["order_no"], r["created_at"], r["customer_name"], r["sender_phone"], r["shop"], r["item"],
                               r["order_status"] or "pending", r["delivery_fee"], _fee_currency(r),
                               1 if r["is_test"] else 0] for r in rows])
            return _csv_response(start_response, "safe-deli-orders.csv", body)
        # ---------- Shop media upload (2026-11-13) ----------
        if method == "POST" and path == "/api/admin/media":
            # Owner only: Min uploads a shop photo or the 1-minute
            # video straight from his phone's files. The file is
            # validated by magic bytes, stored under a
            # server-generated name, and an HTTPS URL is returned for
            # the shop's photo/menu/video field. No third-party host,
            # no new domain — the same VPS, the same dashboard domain.
            auth, error = _auth_response(environ, start_response, "owner")
            if error is not None:
                return error
            try:
                fields, files = _parse_multipart(
                    environ, _MEDIA_VIDEO_MAX_BYTES + _MEDIA_SLACK)
            except _MediaTooLarge:
                return json_response(start_response, "413 Payload Too Large",
                                     {"error": "File is too large"})
            except ValueError as exc:
                return json_response(start_response, "400 Bad Request", {"error": str(exc)})
            kind = (fields.get("kind") or "shop").strip()
            if kind not in _MEDIA_KINDS:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Unknown upload kind"})
            media_type = _MEDIA_KINDS[kind]
            part = files.get("file")
            if part is None or not part[2]:
                return json_response(start_response, "400 Bad Request",
                                     {"error": "No image or video file was provided"})
            _filename, _claimed_type, data = part
            detected = _detect_media(data)
            if detected is None or detected[0] != media_type:
                if media_type == "video":
                    return json_response(start_response, "400 Bad Request",
                                         {"error": "Only MP4, WebM or MOV videos are accepted"})
                return json_response(start_response, "400 Bad Request",
                                     {"error": "Only JPG, PNG or WebP images are accepted"})
            _mtype, ext, content_type = detected
            cap = _MEDIA_VIDEO_MAX_BYTES if media_type == "video" else _MEDIA_IMAGE_MAX_BYTES
            if len(data) > cap:
                return json_response(start_response, "413 Payload Too Large",
                                     {"error": "File is too large (max %d MB)" % (cap // (1024 * 1024))})
            name = secrets.token_hex(16) + "." + ext
            dest_dir = _media_dir() / kind
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / name).write_bytes(data)
            except OSError:
                return json_response(start_response, "500 Internal Server Error",
                                     {"error": "Could not save the uploaded file"})
            url = _public_base_url() + "/media/%s/%s" % (kind, name)
            return json_response(start_response, "201 Created",
                                 {"url": url, "kind": kind, "filename": name,
                                  "size": len(data), "content_type": content_type})
        if method == "GET" and path.startswith("/media/"):
            # Public shop media: photos and videos the owner uploaded.
            # No auth — these are the shop's public pictures, the same
            # ones the shop's photo_url already points to. The name is
            # server-generated (32 hex chars), so only that exact shape
            # is accepted and the resolved path must stay inside the
            # media dir. <img> and <video> in the Mini App load these
            # cross-origin without CORS (media elements need none).
            rest = path[len("/media/"):]
            parts = rest.split("/")
            if len(parts) != 2:
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            kind, name = parts
            if kind not in _MEDIA_KINDS or not _MEDIA_NAME_RE.fullmatch(name):
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            try:
                media_dir = _media_dir().resolve()
                target = (media_dir / kind / name).resolve()
            except (OSError, ValueError):
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            if not str(target).startswith(str(media_dir)) or not target.is_file():
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            try:
                data = target.read_bytes()
            except OSError:
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            ext = name.rsplit(".", 1)[1].lower()
            start_response("200 OK", [("Content-Type", _MEDIA_CONTENT_TYPES[ext]),
                                      ("Content-Length", str(len(data))),
                                      ("Cache-Control", "public, max-age=31536000, immutable"),
                                      ("X-Content-Type-Options", "nosniff")])
            return [data]
        # ---------- Static files: the unified dashboard ----------
        if method == "GET" and path in ("/", "/unified-dashboard.html"):
            return _serve_file(start_response, ROOT / "frontend" / "unified-dashboard.html", "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/assets/"):
            # Only files inside frontend/assets are served (the real
            # Safe Deli logo); any traversal is rejected.
            name = path[len("/assets/"):]
            if not name or "/" in name or name.startswith("."):
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            assets_dir = (ROOT / "frontend" / "assets").resolve()
            asset = (assets_dir / name).resolve()
            if not str(asset).startswith(str(assets_dir)) or not asset.is_file():
                return json_response(start_response, "404 Not Found", {"error": "Route not found"})
            ctype = "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else "image/png" if name.lower().endswith(".png") else "application/octet-stream"
            return _serve_file(start_response, asset, ctype)
        return json_response(start_response, "404 Not Found", {"error": "Route not found"})
    except (ValueError, json.JSONDecodeError) as exc:
        return json_response(start_response, "400 Bad Request", {"error": str(exc)})
    except (UnicodeDecodeError, TypeError):
        return json_response(start_response, "400 Bad Request", {"error": "Invalid request data"})
    except FileNotFoundError:
        return json_response(start_response, "503 Service Unavailable", {
            "error": "Safe Deli database is not configured or is unavailable"})
    except sqlite3.Error:
        try:
            _log_error("api_500", "database error on " + path[:200])
        except Exception:
            pass
        return json_response(start_response, "500 Internal Server Error", {"error": "Database operation failed"})


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    # Never silently bootstrap an empty DB on server startup. A disposable
    # development DB may be initialized only with an explicit opt-in.
    if os.environ.get("SAFEDELI_INIT_DB") == "1":
        initialize_database()
    if not DB_PATH.is_file():
        raise SystemExit(
            f"Safe Deli database not found: {DB_PATH}. "
            "Set SAFEDELI_DB_PATH to the reviewed database file. "
            "For a disposable development database only, set SAFEDELI_INIT_DB=1."
        )
    host, port = os.environ.get("SAFEDELI_HOST", "127.0.0.1"), int(os.environ.get("SAFEDELI_PORT", "8080"))
    print(f"Safe Deli API listening on http://{host}:{port} (development server only)")
    make_server(host, port, application).serve_forever()
