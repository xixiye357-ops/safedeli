#!/usr/bin/env python3
"""
Safe Deli — Telegram order bot (Tachileik, own bot, no Manybot)

Flow (updated 2026-11-05 per Min):
  /start -> welcome + buttons [📍 တည်နေရာ, blue] [💰 ဈေးနှုန်း, green]
  [📦 ပစ္စည်းမှာရန်, blue] [📞 ဖုန်းနံပါတ်ရယူပါ]
  -> [Order] -> name -> sender phone -> pickup location (pin or typed
  address) -> drop-off location -> item type -> optional item photo
  -> summary -> confirm -> order saved (SQLite) and forwarded to
  OWNER_CHAT_ID.
  -> [Location] shows the location-request keyboard at once, then
  pickup -> drop-off -> name -> sender phone -> item -> photo -> confirm.
  Telegram inline buttons cannot be coloured, so coloured-circle
  emojis mark each button.
  Mini App orders (added 2026-11-05): the Safe Deli Mini App sends
  orders with Telegram WebApp tg.sendData as JSON
  {"type": "mini_app_order", "items": ..., "pickup": ...,
  "dropoff": ..., "name": ..., "phone": ..., "note": ...}.
  They are validated, saved with source "mini_app" and the same
  SD-YYYYMMDD-### numbering, forwarded to OWNER_CHAT_ID, and the
  customer is told the admin will reply with the item price +
  delivery fee (delivery starts from 30 baht).

Config (environment or a local .env file):
  BOT_TOKEN      Telegram bot token from @BotFather (required)
  OWNER_CHAT_ID  Telegram chat id that receives orders (optional at first;
                 use /myid inside the bot to learn your id)
"""
import copy
import json
import logging
import math
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

from database_config import resolve_database_path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------------------------------------------------------- config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """Very small .env reader (KEY=VALUE per line), no extra dependency."""
    path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")


DB_PATH = str(resolve_database_path(
    os.environ.get("SAFEDELI_DB_PATH"),
    "orders.db",
    BASE_DIR,
))

BUSINESS = "Safe Deli"
TAGLINE = "Fast • Safe • Reliable"
TOWN = "တာချီလိတ်"
PHONE_MAIN = "09 441 582 958"
# Only the number Min supplied for SAFE DELI (2026-11-03). Hours:
# orders 24 hours (Min, 2026-11-06 00:51); delivery 6 AM–10 PM.
# Price starting point below is from Min's Safe Deli poster (30 baht),
# approved by Min 2026-11-05 — zones still to be confirmed by Min.
PHONES_LINE = "📞 09 441 582 958"
# NOTE: the shop-hours line and the price/location/phone texts used to
# be static constants here (frozen at import time). They are now
# functions in the service-hours section below (hours_line(),
# price_text(), location_text(), phone_text()) so they always show
# the owner's current hours.
MINI_APP_URL = os.environ.get(
    "SAFEDELI_MINI_APP_URL",
    "https://dark-morning-788d.xixiye357.workers.dev/",
).strip()  # Override only after the replacement Mini App URL is verified.

# ---------------------------------------------------------------- languages
# User language choice (Min, 2026-11-11): the WHOLE project must speak
# polite / respectful / sweet / gentle, in FOUR languages. /start first
# shows 4 language buttons; the choice is saved per user in SQLite
# (user_langs table, additive) and the welcome + shop line + main menu
# follow it. Burmese is the default.
# Chinese (zh) strings are DRAFTS — standard polite Mandarin, and
# MUST be checked by a native speaker before launch. Never present
# them as final. Shan was REMOVED from the bot entirely (Min,
# 2026-11-12): it is not offered, not stored, and an old saved
# "shn" row falls back to Burmese via get_user_lang().
LANGS = ("my", "th", "en", "zh")
DEFAULT_LANG = "my"
LANG_NAMES = {
    "my": "မြန်မာ",
    "th": "ไทย",
    "en": "English",
    "zh": "中文",
}

# Polite welcome, per language. Burmese/Thai/English are from the
# owner-approved draft (BOT-WELCOME-REDO-DRAFT-2026-11-11.md).
# Chinese is a DRAFT (see note above).
WELCOME_TEXTS = {
    "my": (
        "မင်္ဂလာပါရှင့် 💜 Safe Deli မှ ကြိုဆိုပါတယ်ရှင့်။\n\n"
        "အကို၊ အစ်မတို့အတွက် ဘာများ ကူညီပေးရမလဲရှင့်။\n\n"
        "🛍️ ပစ္စည်းမှာယူချင်ပါသလား။\n"
        "🚚 Delivery ဝန်ဆောင်မှုနဲ့ ပတ်သက်ပြီး စုံစမ်းချင်ပါသလား။\n"
        "📦 ပစ္စည်းပို့ဆောင်ချင်ပါသလား။\n"
        "💬 အခြားမေးမြန်းလိုတာများရှိရင်လည်း ဒီမှာပဲ စာပို့မေးမြန်းနိုင်ပါတယ်ရှင့်။\n\n"
        "Safe Deli — Fast • Safe • Reliable 💜"
    ),
    "th": (
        "สวัสดีค่ะ 💜 ยินดีต้อนรับสู่ Safe Deli นะคะ\n\n"
        "มีอะไรให้ช่วยบ้างคะ\n\n"
        "🛍️ อยากสั่งซื้อสินค้าหรือคะ\n"
        "🚚 อยากสอบถามเกี่ยวกับบริการเดลิเวอรีหรือคะ\n"
        "📦 อยากส่งพัสดุหรือคะ\n"
        "💬 มีคำถามอื่น ๆ ส่งข้อความมาถามได้เลยนะคะ\n\n"
        "Safe Deli — Fast • Safe • Reliable 💜"
    ),
    "en": (
        "Hello and welcome to Safe Deli! 💜\n\n"
        "How may we help you today?\n\n"
        "🛍️ Would you like to order something?\n"
        "🚚 Would you like to ask about our delivery service?\n"
        "📦 Would you like to send a parcel?\n"
        "💬 Or just send us a message with any other question.\n\n"
        "Safe Deli — Fast • Safe • Reliable 💜"
    ),
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": (
        "您好 💜 欢迎来到 Safe Deli！\n\n"
        "请问有什么可以帮您的吗？\n\n"
        "🛍️ 想订点东西吗？\n"
        "🚚 想了解一下配送服务吗？\n"
        "📦 想寄包裹吗？\n"
        "💬 有其他问题也可以直接发消息问我们哦。\n\n"
        "Safe Deli — 快速 · 安全 · 可靠 💜"
    ),
}

# Shop-button line under the welcome, per language. Burmese is the
# improved polite version the owner asked for (2026-11-11 03:56).
# Chinese is a DRAFT (see note above).
SHOP_LINE_TEXTS = {
    "my": (
        "🛒 ဆိုင်ကို ဖွင့်ချင်ရင် အောက်က \"🛒 Safe Deli ဆိုင်\" "
        "ခလုတ်ကို နှိပ်ပေးပါနော် — အော်ဒါက Bot ဆီကို တန်းရောက်သွားမှာပါ။"
    ),
    "th": (
        "🛒 ถ้าอยากเปิดร้านค้า กดปุ่ม '🛒 Safe Deli ร้านค้า' "
        "ด้านล่างได้เลยนะคะ — ออเดอร์จะส่งถึงบอททันทีค่ะ"
    ),
    "en": (
        "🛒 To open the shop, just tap the '🛒 Safe Deli Shop' button "
        "below — your order will go straight to the bot."
    ),
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": (
        "🛒 想打开商店的话，请点下面的“🛒 Safe Deli 商店”按钮哦 — "
        "您的订单会直接发送到机器人。"
    ),
}

# Main-menu button labels, per language. Chinese is a DRAFT.
MENU_LABELS = {
    "my": {
        "shop": "🛒 Safe Deli ဆိုင်",
        "location": "📍 တည်နေရာ",
        "price": "💰 ဈေးနှုန်း",
        "order": "📦 ပစ္စည်းမှာရန်",
        "phone": "📞 ဖုန်းနံပါတ်ရယူပါ",
        "language": "🌐 ဘာသာစကား",
    },
    "th": {
        "shop": "🛒 ร้าน Safe Deli",
        "location": "📍 ตำแหน่งที่อยู่",
        "price": "💰 ราคา",
        "order": "📦 สั่งสินค้า",
        "phone": "📞 เบอร์โทรศัพท์",
        "language": "🌐 ภาษา",
    },
    "en": {
        "shop": "🛒 Safe Deli Shop",
        "location": "📍 Location",
        "price": "💰 Prices",
        "order": "📦 Order",
        "phone": "📞 Phone number",
        "language": "🌐 Language",
    },
    "zh": {
        "shop": "🛒 Safe Deli 商店",
        "location": "📍 位置",
        "price": "💰 价格",
        "order": "📦 下单",
        "phone": "📞 电话",
        "language": "🌐 语言",
    },
}

# Order-flow texts, per language (Min, 2026-11-12 09:41: the other
# languages NOW, not later). Burmese is Min's exact wording and must
# stay verbatim. Thai/English are full translations of the same facts.
# Chinese is a DRAFT — a native speaker must check it.
ORDER_INTRO_TEXTS = {
    "my": (
        "📦 မင်္ဂလာပါရှင့် ပစ္စည်းမှာယူဖို့အတွက် အောက်ပါအချက်အလက်တွေကို ကျေးဇူးပြု၍ ပို့ပေးပါနော်။\n\n"
        "အဆင့် ၁ — နာမည်\n"
        "အဆင့် ၂ — ဖုန်းနံပါတ်\n"
        "အဆင့် ၃ — ယူရမည့်နေရာ\n"
        "အဆင့် ၄ — ပို့ရမည့်နေရာ\n"
        "အဆင့် ၅ — ပစ္စည်းအမျိုးအစား\n\n"
        "ပြီးရင် ဓာတ်ပုံ (ရှိရင်) ပို့ပေးပြီး အတည်ပြုပေးရုံပါပဲနော်။\n\n"
        "အဆင့် ၁ 👤 **နာမည်** ရေးပို့ပေးပါနော်။"
    ),
    "th": (
        "📦 สวัสดีค่ะ สั่งสินค้ากรุณาส่งข้อมูลด้านล่างมาให้หน่อยนะคะ\n\n"
        "ขั้นตอนที่ 1 — ชื่อ\n"
        "ขั้นตอนที่ 2 — เบอร์โทรศัพท์\n"
        "ขั้นตอนที่ 3 — สถานที่รับของ\n"
        "ขั้นตอนที่ 4 — สถานที่ส่งของ\n"
        "ขั้นตอนที่ 5 — ประเภทสินค้า\n\n"
        "จากนั้นส่งรูปถ่าย (ถ้ามี) แล้วกดยืนยันได้เลยนะคะ\n\n"
        "ขั้นตอนที่ 1 👤 **ชื่อ** กรุณาพิมพ์ชื่อส่งมานะคะ"
    ),
    "en": (
        "📦 Hello! To order something, please send us the information below.\n\n"
        "Step 1 — Name\n"
        "Step 2 — Phone number\n"
        "Step 3 — Pickup place\n"
        "Step 4 — Delivery place\n"
        "Step 5 — Item type\n\n"
        "Then just send a photo (if you have one) and confirm.\n\n"
        "Step 1 👤 **Name** Please send your name."
    ),
    # Native check needed.
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": (
        "📦 您好！订东西的话，请把下面的信息发给我们哦。\n\n"
        "第 1 步 — 姓名\n"
        "第 2 步 — 电话号码\n"
        "第 3 步 — 取货地点\n"
        "第 4 步 — 送货地点\n"
        "第 5 步 — 商品种类\n\n"
        "然后发一张照片（如果有的话），再确认一下就好啦。\n\n"
        "第 1 步 👤 **姓名** 请把姓名发给我们哦。"
    ),
}

# Delivery-fee message body, per language (the hours + phone footer
# is added by price_text()). Burmese is Min's exact wording.
PRICE_TEXTS = {
    "my": (
        "💰 ဈေးနှုန်း\n\n"
        "🚚 အနည်းဆုံး ပို့ဆောင်ခ 30 ဘတ်ကနေ စတင်ပို့ဆောင်ပေးပါတယ်ရှင့်။\n\n"
        "ဈေးနှုန်းက ဒီအချက်တွေပေါ် မူတည်ပါတယ်ရှင့်:\n"
        "• ယူရမည့်နေရာနဲ့ ပို့ရမည့်နေရာ အကွာအဝေး\n"
        "• ပစ္စည်းအမျိုးအစားနဲ့ အရွယ်အစား\n\n"
        "အတိအကျ ဈေးနှုန်းကို အော်ဒါ အတည်ပြုချိန်မှာ ဖုန်းနဲ့ ပြန်ပြောပေးပါ့မယ်ရှင့်။\n"
        "မှာယူပြီးမှ ဈေးနှုန်း ပြောင်းလဲတာ မရှိပါဘူးရှင့်။"
    ),
    "th": (
        "💰 ราคา\n\n"
        "🚚 ค่าส่งเริ่มต้นที่ขั้นต่ำ 30 บาทค่ะ\n\n"
        "ราคาขึ้นอยู่กับสิ่งเหล่านี้ค่ะ:\n"
        "• ระยะทางระหว่างสถานที่รับของกับสถานที่ส่งของ\n"
        "• ประเภทและขนาดของสินค้า\n\n"
        "ตอนยืนยันออเดอร์ เราจะโทรไปบอกราคาที่แน่นอนให้นะคะ\n"
        "สั่งแล้วราคาจะไม่เปลี่ยนค่ะ"
    ),
    "en": (
        "💰 Prices\n\n"
        "🚚 Delivery starts from a minimum fee of 30 Baht.\n\n"
        "The price depends on:\n"
        "• The distance between the pickup place and the delivery place\n"
        "• The item type and size\n\n"
        "We'll tell you the exact price by phone when we confirm your order.\n"
        "The price never changes after you order."
    ),
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": (
        "💰 价格\n\n"
        "🚚 配送费从最低 30 泰铢起哦。\n\n"
        "价格要看这些哦：\n"
        "• 取货地点和送货地点之间的距离\n"
        "• 商品种类和大小\n\n"
        "确认订单的时候，我们会打电话告诉您准确的价格哦。\n"
        "下单之后价格不会变哦。"
    ),
}

# "Tap the Order button" closing line of the fee message, per language.
# The button label matches MENU_LABELS[lang]["order"] in each language.
PRICE_ORDER_LINE_TEXTS = {
    "my": "မှာယူဖို့ \"📦 ပစ္စည်းမှာရန်\" ခလုတ်ကို နှိပ်ပေးပါနော်။",
    "th": "สั่งสินค้ากดปุ่ม \"📦 สั่งสินค้า\" ได้เลยนะคะ",
    "en": "To order, just tap the \"📦 Order\" button.",
    
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": "下单请点“📦 下单”按钮哦。",
}

# Phone-order message, per language. The phone number + hours lines
# sit between head and tail and are added by phone_text().
PHONE_HEAD_TEXTS = {
    "my": "📞 ဖုန်းဆက် မှာယူနိုင်ပါတယ်ရှင့်",
    "th": "📞 โทรสั่งได้นะคะ",
    "en": "📞 You can order by phone",
    
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": "📞 可以打电话下单哦",
}

PHONE_TAIL_TEXTS = {
    "my": (
        "ဖုန်းဆက်ရင် ကျေးဇူးပြုပြီး ဒီအချက်တွေကို ပြောပြပေးပါနော်။\n"
        "• နာမည် နဲ့ ဖုန်းနံပါတ်\n"
        "• ယူရမည့်နေရာ နဲ့ ပို့ရမည့်နေရာ\n"
        "• ပို့မည့် ပစ္စည်း အမျိုးအစား"
    ),
    "th": (
        "ตอนโทรมาสั่ง รบกวนบอกข้อมูลเหล่านี้หน่อยนะคะ\n"
        "• ชื่อและเบอร์โทรศัพท์\n"
        "• สถานที่รับของและสถานที่ส่งของ\n"
        "• ประเภทสินค้าที่จะส่ง"
    ),
    "en": (
        "When you call, please tell us:\n"
        "• Name and phone number\n"
        "• Pickup place and delivery place\n"
        "• The item type to deliver"
    ),
    # DRAFT Chinese — standard polite Mandarin. Native check needed.
    "zh": (
        "打电话的时候，请告诉我们这些哦。\n"
        "• 姓名和电话号码\n"
        "• 取货地点和送货地点\n"
        "• 要送的商品种类"
    ),
}

# ----------------------------------------------------------------
# Order-flow step prompts, per language (Min, 2026-11-12 09:55:
# the per-step prompts NOW, all at once, error-free-or-remove).
# Burmese is Min's exact wording and stays verbatim. Thai/English
# are full translations of the same facts. Chinese is a DRAFT —
# a native speaker must check it. (Shan was removed from the bot
# entirely, Min 2026-11-12; any missing key falls back to Burmese
# via lang_text(), never a wrong translation.)
# Templates use {step} (e.g. "အဆင့် ၂" / "Step 2"), {next} (the
# next step's name in that language), {addr_btn} / {loc_btn} (the
# keyboard button labels in that language), {order_no} and {shop}.
# ----------------------------------------------------------------
ORDER_STEP_TEXTS = {
    # Phone prompt: step 2 in the normal flow, step 4 in the
    # location-first flow (next = pickup place / item type).
    "phone_prompt": {
        "my": (
            "{step} 📞 **ဖုန်းနံပါတ်** ရေးပို့ပေးပါနော် "
            "(ဥပမာ 09xxxxxxxxx)။ ပြီးရင် {next} မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "{step} 📞 **เบอร์โทรศัพท์** กรุณาพิมพ์เบอร์โทรศัพท์ส่งมานะคะ "
            "(ตัวอย่าง 09xxxxxxxxx) แล้วเราจะถาม{next}ต่อนะคะ"
        ),
        "en": (
            "{step} 📞 **Phone number** Please send your phone number "
            "(for example 09xxxxxxxxx). Then we'll ask for {next}."
        ),
        "zh": (
            "{step} 📞 **电话号码** 请把电话号码发给我们哦"
            "（比如 09xxxxxxxxx）。然后我们会问您{next}哦。"
        ),
    },
    "phone_invalid": {
        "my": "ဖုန်းနံပါတ် မမှန်သေးဘူးရှင့်။ 09 နဲ့ စတဲ့ နံပါတ် ပြန်ရေးပေးပါနော်။",
        "th": "เบอร์โทรศัพท์ยังไม่ถูกต้องค่ะ กรุณาพิมพ์เบอร์ที่ขึ้นต้นด้วย 09 ส่งมาใหม่นะคะ",
        "en": "That phone number doesn't look right. Please send a number starting with 09.",
        
        "zh": "电话号码格式不对哦。请重新发一个以 09 开头的号码哦。",
    },
    # Pickup prompt (normal flow, step 3).
    "pickup_prompt": {
        "my": (
            "{step} 📍 ပစ္စည်း **ယူရမည့်နေရာ** ကို ပို့ပေးပါနော်။\n"
            "အောက်က ခလုတ် နှိပ်ပြီး လက်ရှိ တည်နေရာ ပို့ပေးပါ။\n"
            "Location မရှိရင် \"{addr_btn}\" ခလုတ် နှိပ်ပြီး "
            "လိပ်စာ အတိအကျ ရေးပေးပါနော်။\n"
            "ပြီးရင် ပို့ရမည့်နေရာ မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "{step} 📍 **สถานที่รับของ** กรุณาส่งสถานที่รับของมาให้หน่อยนะคะ\n"
            "กดปุ่มด้านล่างเพื่อส่งตำแหน่งปัจจุบันค่ะ\n"
            "ถ้าไม่มี Location กดปุ่ม \"{addr_btn}\" แล้วพิมพ์ที่อยู่ให้ชัดๆ ส่งมานะคะ\n"
            "จากนั้นเราจะถามสถานที่ส่งของค่ะ"
        ),
        "en": (
            "{step} 📍 Please send the **pickup place**.\n"
            "Tap the button below to send your current location.\n"
            "If you don't have Location, tap \"{addr_btn}\" and type the exact address.\n"
            "Then we'll ask for the delivery place."
        ),
        "zh": (
            "{step} 📍 请把**取货地点**发给我们哦。\n"
            "点下面的按钮发送您现在的位置。\n"
            "如果没有定位，点“{addr_btn}”，把准确地址写下来发给我们哦。\n"
            "然后我们会问您送货地点哦。"
        ),
    },
    "item_prompt": {
        "my": (
            "{step} 🧺 ပို့မည့် **ပစ္စည်း အမျိုးအစား** ရေးပို့ပေးပါနော် "
            "(ဥပမာ အစားအသောက်၊ အဝတ်အစား ထုပ်၊ စာရွက်စာတမ်း)။ "
            "ပြီးရင် ဓာတ်ပုံ မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "{step} 🧺 **ประเภทสินค้าที่จะส่ง** กรุณาพิมพ์บอกหน่อยนะคะ "
            "(เช่น อาหาร, เสื้อผ้า, เอกสาร) แล้วเราจะถามเรื่องรูปถ่ายค่ะ"
        ),
        "en": (
            "{step} 🧺 Please tell us the **item type** to deliver "
            "(for example food, a clothes parcel, documents). "
            "Then we'll ask for a photo."
        ),
        "zh": (
            "{step} 🧺 请告诉我们要送的**商品种类**哦"
            "（比如食物、衣服包裹、文件）。然后我们会问您要不要发照片哦。"
        ),
    },
    # Drop-off prompt: step 4 in the normal flow, step 2 in the
    # location-first flow (next = item type / name).
    "dropoff_prompt": {
        "my": (
            "ရပါပြီရှင့်။ {step} 📍 ပစ္စည်း **ပို့ရမည့်နေရာ** ကို ပို့ပေးပါနော်။ "
            "အောက်က ခလုတ် နှိပ်ပြီး လက်ရှိ တည်နေရာ ပို့ပေးပါ။ "
            "Location မရှိရင် \"{addr_btn}\" ခလုတ် နှိပ်ပြီး "
            "လိပ်စာ အတိအကျ ရေးပေးပါနော်။ "
            "ပြီးရင် {next} မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "รับทราบแล้วค่ะ {step} 📍 **สถานที่ส่งของ** กรุณาส่งมาให้หน่อยนะคะ "
            "กดปุ่มด้านล่างเพื่อส่งตำแหน่งปัจจุบันค่ะ "
            "ถ้าไม่มี Location กดปุ่ม \"{addr_btn}\" แล้วพิมพ์ที่อยู่ให้ชัดๆ ส่งมานะคะ "
            "จากนั้นเราจะถาม{next}ค่ะ"
        ),
        "en": (
            "Got it. {step} 📍 Please send the **delivery place**. "
            "Tap the button below to send your current location. "
            "If you don't have Location, tap \"{addr_btn}\" and type the exact address. "
            "Then we'll ask for {next}."
        ),
        "zh": (
            "收到啦。{step} 📍 请把**送货地点**发给我们哦。"
            "点下面的按钮发送您现在的位置。"
            "如果没有定位，点“{addr_btn}”，把准确地址写下来发给我们哦。"
            "然后我们会问您{next}哦。"
        ),
    },
    # Name prompt (location-first flow, step 3).
    "name_prompt": {
        "my": (
            "ရပါပြီရှင့်။\n\n"
            "{step} 👤 **နာမည်** ရေးပို့ပေးပါနော်။ "
            "ပြီးရင် ဖုန်းနံပါတ် မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "รับทราบแล้วค่ะ\n\n"
            "{step} 👤 **ชื่อ** กรุณาพิมพ์ชื่อส่งมานะคะ แล้วเราจะถามเบอร์โทรศัพท์ค่ะ"
        ),
        "en": (
            "Got it.\n\n"
            "{step} 👤 **Name** Please send your name. "
            "Then we'll ask for your phone number."
        ),
        "zh": (
            "收到啦。\n\n"
            "{step} 👤 **姓名** 请把姓名发给我们哦。然后我们会问您电话号码哦。"
        ),
    },
    # Photo prompt (last step) + the "no photo" skip button.
    "photo_prompt": {
        "my": (
            "{step} ပြီးပါပြီရှင့်။ နောက်ဆုံးအဆင့်ပါနော် — "
            "📷 ပစ္စည်း ဓာတ်ပုံ ရှိရင် ပို့ပေးပါ။ "
            "မရှိရင် အောက်က ခလုတ် နှိပ်ပေးပါနော်။ "
            "ပြီးရင် အတည်ပြုဖို့ ပြပေးပါမယ်ရှင့်။"
        ),
        "th": (
            "{step} เสร็จแล้วค่ะ เหลือขั้นตอนสุดท้ายนะคะ — "
            "📷 ถ้ามีรูปถ่ายสินค้า ส่งมาให้หน่อยนะคะ "
            "ถ้าไม่มี กดปุ่มด้านล่างได้เลยค่ะ แล้วเราจะแสดงให้ยืนยันค่ะ"
        ),
        "en": (
            "{step} done. One last step — "
            "📷 If you have a photo of the item, please send it. "
            "If not, tap the button below. Then we'll show it to you to confirm."
        ),
        "zh": (
            "{step} 完成啦。这是最后一步哦 — "
            "📷 如果有商品照片，请发给我们哦。"
            "如果没有，点下面的按钮哦。然后我们会给您确认哦。"
        ),
    },
    "photo_skip_btn": {
        "my": "➡️ ဓာတ်ပုံ မပါဘူး, ဆက်သွားမယ်",
        "th": "➡️ ไม่มีรูป ไปต่อเลยค่ะ",
        "en": "➡️ No photo, continue",
        
        "zh": "➡️ 没有照片，继续哦",
    },
    # Confirmation question + buttons.
    "confirm_question": {
        "my": "အချက်အလက် မှန်ကန်ပါသလားရှင့်?",
        "th": "ข้อมูลถูกต้องไหมคะ?",
        "en": "Is this information correct?",
        
        "zh": "这些信息对吗？",
    },
    "confirm_yes_btn": {
        "my": "✅ အတည်ပြုမယ်",
        "th": "✅ ยืนยัน",
        "en": "✅ Confirm",
        
        "zh": "✅ 确认",
    },
    "confirm_no_btn": {
        "my": "❌ ဖျက်မယ်",
        "th": "❌ ยกเลิก",
        "en": "❌ Cancel",
        
        "zh": "❌ 取消",
    },
    # "Order received" (bot-chat order). Built by
    # order_received_text() from head + fee/time + thanks.
    "order_received_head": {
        "my": "✅ Order လက်ခံရရှိပါပြီ။ Order နံပါတ်: {order_no}",
        "th": "✅ รับออเดอร์แล้วค่ะ เลขออเดอร์: {order_no}",
        "en": "✅ Order received! Order number: {order_no}",
        
        "zh": "✅ 已收到订单！订单号：{order_no}",
    },
    "order_received_fee_time": {
        "my": "ပို့ခ နဲ့ ကြာချိန်ကို ဖုန်းဖြင့် ပြန်လည် အတည်ပြုပေးပါမယ်။",
        "th": "เราจะโทรไปยืนยันค่าส่งกับเวลาให้นะคะ",
        "en": "We'll call you to confirm the delivery fee and time.",
        
        "zh": "我们会打电话跟您确认配送费和时间哦。",
    },
    "order_received_thanks": {
        "my": "ကျေးဇူးတင်ပါတယ် 🙏 {business}",
        "th": "ขอบคุณค่ะ 🙏 {business}",
        "en": "Thank you 🙏 {business}",
        "zh": "谢谢您 🙏 {business}",
    },
    # Order cancelled (confirm_no) / conversation stopped (/cancel).
    "confirm_cancel": {
        "my": "❌ Order ကို ဖျက်လိုက်ပါပြီရှင့်။ ပြန်စချင်ရင် /start ကို နှိပ်ပေးပါနော်။",
        "th": "❌ ยกเลิกออเดอร์แล้วค่ะ ถ้าอยากเริ่มใหม่ กด /start ได้เลยนะคะ",
        "en": "❌ Order cancelled. If you'd like to start again, tap /start.",
        
        "zh": "❌ 订单已取消哦。想重新下单的话，点 /start 哦。",
    },
    "cancel_text": {
        "my": "❌ ရပ်လိုက်ပါပြီရှင့်။ ပြန်စချင်ရင် /start ကို နှိပ်ပေးပါနော်။",
        "th": "❌ หยุดแล้วค่ะ ถ้าอยากเริ่มใหม่ กด /start ได้เลยนะคะ",
        "en": "❌ Stopped. If you'd like to start again, tap /start.",
        
        "zh": "❌ 已经停下来了哦。想重新开始的话，点 /start 哦。",
    },
    # Location-first entry: step 1 pickup prompt (location_text()
    # is sent just above it).
    "location_begin_prompt": {
        "my": (
            "{step} 📍 ပစ္စည်း **ယူရမည့်နေရာ** အဖြစ် ကိုယ့်တည်နေရာ ပို့ပေးပါနော်။\n"
            "အောက်က \"{loc_btn}\" ခလုတ် နှိပ်ပေးပါ။\n"
            "Location မရှိရင် \"{addr_btn}\" ခလုတ် နှိပ်ပြီး "
            "လိပ်စာ အတိအကျ ရေးပေးပါနော်။\n"
            "ပြီးရင် ပို့ရမည့်နေရာ မေးပါမယ်ရှင့်။"
        ),
        "th": (
            "{step} 📍 **สถานที่รับของ** กรุณาส่งตำแหน่งของคุณมาให้หน่อยนะคะ\n"
            "กดปุ่ม \"{loc_btn}\" ด้านล่างได้เลยค่ะ\n"
            "ถ้าไม่มี Location กดปุ่ม \"{addr_btn}\" แล้วพิมพ์ที่อยู่ให้ชัดๆ ส่งมานะคะ\n"
            "จากนั้นเราจะถามสถานที่ส่งของค่ะ"
        ),
        "en": (
            "{step} 📍 Please send your location as the **pickup place**.\n"
            "Tap the \"{loc_btn}\" button below.\n"
            "If you don't have Location, tap \"{addr_btn}\" and type the exact address.\n"
            "Then we'll ask for the delivery place."
        ),
        "zh": (
            "{step} 📍 请把您的位置作为**取货地点**发给我们哦。\n"
            "点下面的“{loc_btn}”按钮哦。\n"
            "如果没有定位，点“{addr_btn}”，把准确地址写下来发给我们哦。\n"
            "然后我们会问您送货地点哦。"
        ),
    },
    # Mini App order: send failed / bad payload.
    "mini_app_error": {
        "my": (
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 Order ပို့တာ အဆင်မပြေသေးပါဘူး။ "
            "Mini App ကနေ ပြန်တင်ပေးပါနော်၊ ဒါမှမဟုတ် /order နဲ့ မှာယူနိုင်ပါတယ်ရှင့်။"
        ),
        "th": "ขอโทษนะคะ 🙏 ส่งออเดอร์ไม่สำเร็จค่ะ ลองส่งใหม่จาก Mini App นะคะ หรือสั่งผ่าน /order ได้ค่ะ",
        "en": "Sorry 🙏 Something went wrong sending your order. Please try again from the Mini App, or order with /order.",
        
        "zh": "抱歉哦 🙏 订单没发出去。请从 Mini App 再发一次，或者用 /order 下单哦。",
    },
    "mini_app_retry": {
        "my": "Mini App ကနေ ပြန်တင်ပေးပါနော်၊ ဒါမှမဟုတ် /order နဲ့ မှာယူနိုင်ပါတယ်ရှင့်။",
        "th": "ลองส่งใหม่จาก Mini App นะคะ หรือสั่งผ่าน /order ได้ค่ะ",
        "en": "Please try again from the Mini App, or order with /order.",
        
        "zh": "请从 Mini App 再发一次，或者用 /order 下单哦。",
    },
    # Mini App order confirmation pieces. The fee line is Min's
    # exact line in Burmese, reworded by Min 2026-11-23 — it REPLACES the old
    # "ပို့ခ စတင် 30 ဘတ်မှ ဖြစ်ပါတယ်" wording in the Mini App
    # confirmation, which used the wording Min said is not good.
    "mini_app_price_soon": {
        "my": "အခု ပစ္စည်းဖိုးရော ပို့ခရောကို Admin ဘက်က တွက်ချက်ပြီး စလစ်ထုတ်ပေးနေပါတယ်ရှင့်။ ခဏလေးပဲ စောင့်ပေးပါနော်။",
        "th": "ตอนนี้แอดมินกำลังคำนวณราคาสินค้ากับค่าส่งและออกสลิปให้อยู่ค่ะ รอสักครู่นะคะ",
        "en": "The admin is now calculating the item price and delivery fee and issuing the slip. Please wait just a moment.",
        
        "zh": "管理员正在计算商品价格和配送费并开单据，请稍等一下哦。",
    },
    "mini_app_fee": {
        "my": "ပို့ခလေးကတော့ အနည်းဆုံး ၃၀ ဘတ် ကနေ စပါတယ်ရှင့်။",
        "th": "ค่าส่งเริ่มต้นที่ขั้นต่ำ 30 บาทค่ะ",
        "en": "The delivery fee starts from a minimum of 30 Baht.",
        
        "zh": "配送费从最低 30 泰铢起哦。",
    },
    "mini_app_test": {
        "my": "စမ်းသပ်အော်ဒါ (TEST) — နမူနာ ဆိုင်မှ စမ်းသပ်မှာယူခြင်း ဖြစ်ပါတယ်ရှင့်",
        "th": "ออเดอร์ทดลอง (TEST) — เป็นการสั่งทดลองจากร้านตัวอย่างค่ะ",
        "en": "Test order (TEST) — a trial order from a sample shop.",
        "zh": "测试订单 (TEST)——这是从示例店铺下的测试订单哦。",
    },
    "mini_app_shop": {
        "my": "ဆိုင်ကတော့ {shop} ဖြစ်ပြီး၊\nပို့ပေးရမယ့် နေရာလေးက ဒီလင့်ခ်အတိုင်း မှန်မလား တစ်ချက်ကြည့်ပေးပါဦးနော် 📍",
        "th": "ร้านคือ {shop} ค่ะ\nช่วยเช็คหน่อยนะคะว่าสถานที่จัดส่งตามลิงก์นี้ถูกต้องไหมคะ 📍",
        "en": "The shop is {shop}.\nCould you please check whether the delivery location in this link is correct? 📍",
        
        "zh": "店铺是 {shop}，\n请确认一下这个链接里的配送地点对不对哦 📍",
    },
    "mini_app_head": {
        "my": "မင်္ဂလာပါရှင့်၊ Order လေး ရောက်လာပါပြီနော် 😍\nOrder နံပါတ်လေးကတော့ {order_no} ပါရှင့်။",
        "th": "สวัสดีค่ะ ออเดอร์มาถึงแล้วนะคะ 😍\nเลขออเดอร์คือ {order_no} ค่ะ",
        "en": "Hello, your order has arrived 😍\nYour order number is {order_no}.",
        "zh": "您好，订单已收到啦 😍\n订单号是 {order_no}。",
    },
    "mini_app_thanks": {
        "my": "{business} ကို ယုံယုံကြည်ကြည်နဲ့ မှာယူပေးတဲ့အတွက် အများကြီး ကျေးဇူးတင်ပါတယ်ရှင့် 🙏",
        "th": "ขอบคุณมากๆ ค่ะที่ไว้ใจ {business} 🙏",
        "en": "Thank you so much for trusting {business} with your order 🙏",
        "zh": "非常感谢您信任 {business} 并下单 🙏",
    },
}

# Delivery area phrase per language (attested in the Mini App's
# locationL strings): the town name itself never translates, only
# the surrounding words do.
LOCATION_AREA_TEXTS = {
    "my": "တာချီလိတ်မြို့အတွင်း",
    "th": "ในเมืองท่าขี้เหล็ก",
    "en": "in Tachileik town",
    
    "zh": "大其力市区内",
}

# "How to send a location" help, per language. A missing key falls
# back to Burmese via lang_text() (a wrong how-to is worse than
# Burmese — Min's error rule).
HELP_LOCATION_TEXTS = {
    "my": (
        "📍 Location ပို့နည်း\n"
        "၁။ စာပို့သည့် နေရာဘေးက 📎 (ကလစ်) ပုံကို နှိပ်ပေးပါနော်\n"
        "၂။ \"Location / တည်နေရာ\" ကို ရွေးပေးပါနော်\n"
        "၃။ \"Send current location / လက်ရှိ တည်နေရာ ပို့မယ်\" ကို နှိပ်ပေးပါနော်\n\n"
        "Location ပို့လို့ မရရင်:\n"
        "အောက်က \"{addr_btn}\" ခလုတ် နှိပ်ပြီး\n"
        "ရပ်ကွက်၊ လမ်း၊ အိမ်အမှတ်၊ အနီးအနား မှတ်တိုင် ရေးပို့ပေးပါနော်။"
    ),
    "th": (
        "📍 วิธีส่ง Location\n"
        "1. กด 📎 (คลิป) ข้างช่องพิมพ์ข้อความนะคะ\n"
        "2. เลือก \"Location / ตำแหน่ง\" นะคะ\n"
        "3. กด \"Send current location / ส่งตำแหน่งปัจจุบัน\" นะคะ\n\n"
        "ถ้าส่ง Location ไม่ได้:\n"
        "กดปุ่ม \"{addr_btn}\" ด้านล่าง แล้วพิมพ์ที่อยู่ให้ชัดๆ "
        "(ตำบล/แขวง ถนน บ้านเลขที่ จุดสังเกตใกล้ๆ) ส่งมานะคะ"
    ),
    "en": (
        "📍 How to send Location\n"
        "1. Tap the 📎 (paperclip) next to the message box\n"
        "2. Choose \"Location\"\n"
        "3. Tap \"Send current location\"\n\n"
        "If you can't send Location:\n"
        "Tap the \"{addr_btn}\" button below and type the exact address "
        "(ward, street, house number, nearby landmark)."
    ),
    "zh": (
        "📍 怎么发定位\n"
        "1. 点消息框旁边的 📎（回形针）哦\n"
        "2. 选“Location / 位置”哦\n"
        "3. 点“Send current location / 发送当前位置”哦\n\n"
        "如果发不了定位：\n"
        "点下面的“{addr_btn}”按钮，把准确地址写下来发给我们哦。"
    ),
}

# Exact-address fallback texts, per language.
ADDRESS_BUTTON_TEXTS = {
    "my": "📝 လိပ်စာ အတိအကျ ရေးပို့မယ်",
    "th": "📝 พิมพ์ที่อยู่ให้ชัดๆ ส่งมา",
    "en": "📝 Type the exact address",
    
    "zh": "📝 写准确地址发过来",
}
ADDRESS_ASK_TEXTS = {
    "my": (
        "လိပ်စာ အတိအကျ ရေးပို့ပေးပါနော် — "
        "ရပ်ကွက်၊ လမ်း၊ အိမ်အမှတ်၊ အနီးအနား မှတ်တိုင် "
        "ပါအောင် ရေးပေးပါနော်။"
    ),
    "th": "กรุณาพิมพ์ที่อยู่ให้ชัดๆ ส่งมานะคะ — ตำบล/แขวง ถนน บ้านเลขที่ จุดสังเกตใกล้ๆ ค่ะ",
    "en": "Please type the exact address — ward, street, house number and a nearby landmark.",
    
    "zh": "请把准确地址写下来发给我们哦——街区、路名、门牌号、附近的地标哦。",
}
ADDRESS_EMPTY_TEXTS = {
    "my": (
        "လိပ်စာ မရသေးဘူးရှင့် — "
        "ရပ်ကွက်၊ လမ်း၊ အိမ်အမှတ် ရေးပို့ပေးပါနော်။"
    ),
    "th": "ยังไม่ได้รับที่อยู่ค่ะ — กรุณาพิมพ์ตำบล/แขวง ถนน บ้านเลขที่ ส่งมานะคะ",
    "en": "No address yet — please type the ward, street and house number.",
    
    "zh": "还没收到地址哦——请把街区、路名、门牌号发给我们哦。",
}

# "Send my current location" keyboard button, per language.
# A missing key falls back to Burmese via lang_text().
LOCATION_BUTTON_TEXTS = {
    "my": "📍 လက်ရှိ တည်နေရာ ပို့မယ်",
    "th": "📍 ส่งตำแหน่งปัจจุบัน",
    "en": "📍 Send my current location",
    "zh": "📍 发送我现在的位置",
}

# Order-summary labels (the customer confirmation). The owner card
# calls format_summary() with no lang, so it stays Burmese.
SUMMARY_LABELS = {
    "head": {
        "my": "🧾 Order အကျဉ်းချုပ်",
        "th": "🧾 สรุปออเดอร์",
        "en": "🧾 Order summary",
        
        "zh": "🧾 订单摘要",
    },
    "name": {
        "my": "မှာသူ", "th": "ชื่อผู้สั่ง", "en": "Name",
         "zh": "姓名",
    },
    "phone": {
        "my": "ဖုန်း", "th": "เบอร์โทรศัพท์", "en": "Phone",
         "zh": "电话",
    },
    "pickup": {
        "my": "ယူရမည့်နေရာ", "th": "สถานที่รับของ", "en": "Pickup place",
         "zh": "取货地点",
    },
    "dropoff": {
        "my": "ပို့ရမည့်နေရာ", "th": "สถานที่ส่งของ", "en": "Delivery place",
         "zh": "送货地点",
    },
    "item": {
        "my": "ပစ္စည်း", "th": "สินค้า", "en": "Item",
         "zh": "商品",
    },
    "loc_gps": {
        "my": "တည်နေရာ (GPS/Maps):", "th": "ตำแหน่ง (GPS/Maps):",
        "en": "Location (GPS/Maps):", 
        "zh": "位置 (GPS/Maps):",
    },
    "loc_text": {
        "my": "တည်နေရာ:", "th": "ตำแหน่ง:", "en": "Location:",
         "zh": "位置:",
    },
}

# Location intro (location-first entry), per language.
LOCATION_TEXTS = {
    "my": (
        "📍 တည်နေရာ\n\n"
        "{area} ပို့ဆောင်ပေးပါတယ်ရှင့်။\n"
        "မြို့အပြင် ဆိုရင် ဖုန်းဆက်ပြီး အရင် မေးပေးပါနော်။\n\n"
        "ကိုယ့်တည်နေရာကို တန်းပို့ဖို့:\n"
        "အောက်မှာ ပေါ်လာတဲ့ \"{loc_btn}\" ခလုတ်ကို\n"
        "တစ်ချက် နှိပ်လိုက်ရုံနဲ့ တန်းပို့သွားပါမယ်။\n\n"
        "Location ပို့လို့ မရရင် အောက်က \"{addr_btn}\" "
        "ခလုတ် နှိပ်ပြီး လိပ်စာ အတိအကျ ရေးပို့ပေးပါနော်။\n\n"
        "{hours}\n{phones}"
    ),
    "th": (
        "📍 ตำแหน่ง\n\n"
        "เราส่งของ{area}ค่ะ\n"
        "ถ้าอยู่นอกเมือง โทรมาถามก่อนนะคะ\n\n"
        "ส่งตำแหน่งของคุณ:\n"
        "กดปุ่ม \"{loc_btn}\" ที่ขึ้นมาด้านล่าง\n"
        "แค่กดครั้งเดียวก็ส่งไปเลยค่ะ\n\n"
        "ถ้าส่ง Location ไม่ได้ กดปุ่ม \"{addr_btn}\" ด้านล่าง "
        "แล้วพิมพ์ที่อยู่ให้ชัดๆ ส่งมานะคะ\n\n"
        "{hours}\n{phones}"
    ),
    "en": (
        "📍 Location\n\n"
        "We deliver {area}.\n"
        "If you're outside town, please call and ask first.\n\n"
        "To send your location:\n"
        "Just tap the \"{loc_btn}\" button that appears below\n"
        "and it sends straight away.\n\n"
        "If you can't send Location, tap the \"{addr_btn}\" button below "
        "and type the exact address.\n\n"
        "{hours}\n{phones}"
    ),
    "zh": (
        "📍 位置\n\n"
        "我们在{area}送货哦。\n"
        "如果在城外，请先打电话问一下哦。\n\n"
        "发送您的位置：\n"
        "点下面出现的“{loc_btn}”按钮，\n"
        "点一下就直接发出去了哦。\n\n"
        "如果发不了定位，点下面的“{addr_btn}”按钮，"
        "把准确地址写下来发给我们哦。\n\n"
        "{hours}\n{phones}"
    ),
}

# "Shop closed" messages, per language. {range} is the hours range,
# {next} the next opening, {hours} the hours line, {phones} the
# phone line.
CLOSED_MANUAL_TEXTS = {
    "my": (
        "🔒 အခု ဆိုင်ပိတ်ထားပါတယ်ရှင့်။\n\n"
        "ဆိုင်ရှင်က ဆိုင်ကို ခဏ ပိတ်ထားပါတယ်ရှင့်။ ပြန်ဖွင့်ရင် မှာယူနိုင်ပါတယ်နော်။\n\n"
        "{hours}\n{phones}"
    ),
    "th": (
        "🔒 ตอนนี้ร้านปิดแล้วค่ะ\n\n"
        "เจ้าของร้านปิดร้านชั่วคราวค่ะ เปิดแล้วสั่งได้นะคะ\n\n"
        "{hours}\n{phones}"
    ),
    "en": (
        "🔒 The shop is closed right now.\n\n"
        "The owner has closed the shop for a while. "
        "You can order again once it reopens.\n\n"
        "{hours}\n{phones}"
    ),
    "zh": (
        "🔒 现在店里关门了哦。\n\n"
        "店主暂时把店关了哦。开门之后就可以下单啦。\n\n"
        "{hours}\n{phones}"
    ),
}
# v19.15 24/7 ORDERING (2026-11-15): the schedule-closed texts
# dict is RETIRED — ordering is 24/7 now, so there is no
# schedule-closed message any more. The only closed case left is
# a manual /close, which uses CLOSED_MANUAL_TEXTS above. (The
# dict itself is deleted, not left dead: closed_text() no longer
# references it.)
# Step labels and next-step names, per language. Burmese uses
# Myanmar digits ("အဆင့် ၂"), the others Arabic digits, matching
# the order intro (ORDER_INTRO_TEXTS).
STEP_LABEL_TEMPLATES = {
    "my": "အဆင့် {n}",
    "th": "ขั้นตอนที่ {n}",
    "en": "Step {n}",
    
    "zh": "第 {n} 步",
}
STEP_NEXT_NAMES = {
    "name": {
        "my": "နာမည်", "th": "ชื่อ", "en": "name",
         "zh": "姓名",
    },
    "phone": {
        "my": "ဖုန်းနံပါတ်", "th": "เบอร์โทรศัพท์", "en": "phone number",
         "zh": "电话号码",
    },
    "pickup": {
        "my": "ယူရမည့်နေရာ", "th": "สถานที่รับของ", "en": "pickup place",
         "zh": "取货地点",
    },
    "dropoff": {
        "my": "ပို့ရမည့်နေရာ", "th": "สถานที่ส่งของ", "en": "delivery place",
         "zh": "送货地点",
    },
    "item": {
        "my": "ပစ္စည်းအမျိုးအစား", "th": "ประเภทสินค้า", "en": "item type",
         "zh": "商品种类",
    },
    "photo": {
        "my": "ဓာတ်ပုံ", "th": "รูปถ่าย", "en": "photo",
         "zh": "照片",
    },
    "confirm": {
        "my": "အတည်ပြုခြင်း", "th": "การยืนยัน", "en": "confirmation",
         "zh": "确认",
    },
}


def step_label(lang: str, n: int) -> str:
    """Step label in the user's language, e.g. 'အဆင့် ၂'."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    num = my_digits(n) if lang == "my" else str(n)
    return STEP_LABEL_TEMPLATES[lang].format(n=num)


def step_next_name(lang: str, key: str) -> str:
    """The next step's name in the user's language."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return STEP_NEXT_NAMES[key][lang]


def order_step_text(key: str, lang: str, **kw) -> str:
    """One order-flow prompt in the user's language (Burmese
    fallback for a removed translation)."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return lang_text(ORDER_STEP_TEXTS[key], lang).format(**kw)


def order_received_text(lang: str, order_no: str) -> str:
    """The bot-chat 'order received' message (head + fee/time +
    thanks), in the user's language."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return "\n".join(
        [
            order_step_text("order_received_head", lang, order_no=order_no),
            order_step_text("order_received_fee_time", lang),
            order_step_text(
                "order_received_thanks", lang, business=BUSINESS
            ),
        ]
    )


def address_button_text(lang: str) -> str:
    return lang_text(ADDRESS_BUTTON_TEXTS, lang)


def address_ask_text(lang: str) -> str:
    return lang_text(ADDRESS_ASK_TEXTS, lang)


def address_empty_text(lang: str) -> str:
    return lang_text(ADDRESS_EMPTY_TEXTS, lang)


def location_button_text(lang: str) -> str:
    return lang_text(LOCATION_BUTTON_TEXTS, lang)


def help_location_text(lang: str) -> str:
    lang = lang if lang in LANGS else DEFAULT_LANG
    return lang_text(HELP_LOCATION_TEXTS, lang).format(
        addr_btn=address_button_text(lang)
    )


# Language picker prompt (/start for a new user, or /lang). The button
# names themselves carry the meaning in all four languages.
LANG_PICKER_TEXT = (
    "🌐 ဘာသာစကား ရွေးပေးပါနော်\n"
    "🌐 Please choose your language\n"
    "🌐 กรุณาเลือกภาษาค่ะ"
)


def lang_text(texts: dict, lang: str) -> str:
    """Pick a language string, falling back to Burmese, then English."""
    if lang in texts:
        return texts[lang]
    return texts.get(DEFAULT_LANG) or texts.get("en", "")


YANGON_TZ = timezone(timedelta(hours=6, minutes=30))

# conversation states — new form order Min asked for (2026-11-05):
# name -> sender phone -> pickup -> dropoff -> item -> photo -> confirm
NAME, SENDER_PHONE, PICKUP, DROPOFF, ITEM, PHOTO, CONFIRM = range(7)


# ---------------------------------------------------------------- helpers
# Service hours — OWNER-CONTROLLED (Min, 2026-11-12 02:27): Min sets
# the daily DELIVERY window himself with /sethours, and opens or
# closes the shop whenever he wants with /open and /close.
# v19.15 24/7 ORDERING (Min, 2026-11-15 12:53): orders are accepted
# 24/7 — the 06:00–22:00 hours are the DELIVERY window now, NOT the
# ordering window. A night order is taken for morning delivery.
# The hours live in SHOP_HOURS_FILE (runtime JSON, never shipped in
# the deploy ZIP, so a redeploy never overwrites the owner's setting);
# missing/broken/invalid file -> the 06:00–22:00 defaults.
# A /open or /close override holds only until the NEXT scheduled
# open/close boundary, then the schedule takes over again — so a
# forgotten night-time /open cannot leave the shop open all night.
# History: 24-hour ordering (2026-11-06 00:51); 6 AM–10 PM fixed
# (2026-11-06 01:20); owner night-test bypass (2026-11-12 02:24,
# can_order_now below); 24/7 ordering RESTORED 2026-11-15 — the
# bypass is gone because everyone may order at night now.
OPEN_HOUR = 6
CLOSE_HOUR = 22

SHOP_HOURS_FILE = os.path.join(BASE_DIR, "shop_hours.json")

# Manual shop override (Min, 2026-11-06 01:20; reworked 2026-11-12
# 02:27): a tiny JSON file the owner toggles with /close and /open.
# It survives bot restarts. Format {"mode": "open"|"closed"|"auto",
# "set_at": "<ISO time>"}. Old files {"closed": bool} still work:
# closed -> mode "closed" stamped at the file's own mtime.
SHOP_CLOSED_FILE = os.path.join(BASE_DIR, "shop_closed.json")


def parse_hhmm(text):
    """Strict HH:MM (24-hour) -> minutes since midnight, or None."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text.strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def get_shop_hours() -> dict:
    """Owner-set hours as {"open": "HH:MM", "close": "HH:MM"}.
    Missing / broken / invalid file -> the 06:00–22:00 defaults."""
    try:
        with open(SHOP_HOURS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, AttributeError):
        data = None
    if isinstance(data, dict):
        open_min = parse_hhmm(data.get("open"))
        close_min = parse_hhmm(data.get("close"))
        if (
            open_min is not None
            and close_min is not None
            and open_min < close_min
        ):
            return {
                "open": f"{open_min // 60:02d}:{open_min % 60:02d}",
                "close": f"{close_min // 60:02d}:{close_min % 60:02d}",
            }
    return {"open": "06:00", "close": "22:00"}


def _sync_hours_to_dashboard(open_h: str, close_h: str) -> None:
    """Best-effort mirror of the owner-set hours into the dashboard
    settings (dashboard_settings.global_hours) so the dashboard shows
    what /sethours set. The JSON file stays the bot's source of truth;
    a failure here must never break /sethours, so errors are logged
    and swallowed."""
    try:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO dashboard_settings(key,value_json,updated_at,updated_by)"
                " VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " value_json=excluded.value_json,"
                " updated_at=excluded.updated_at,"
                " updated_by=excluded.updated_by",
                ("global_hours",
                 json.dumps({"open": open_h, "close": close_h,
                             "timezone": "Asia/Yangon"}, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(),
                 "bot:/sethours"))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("hours dashboard sync failed: %s", exc)


def set_shop_hours(open_h: str, close_h: str) -> None:
    """Persist the owner-set hours (callers validate first)."""
    with open(SHOP_HOURS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"open": open_h, "close": close_h}, fh)
    _sync_hours_to_dashboard(open_h, close_h)


def _shop_hours_minutes():
    hours = get_shop_hours()
    return parse_hhmm(hours["open"]), parse_hhmm(hours["close"])


def _as_yangon(dt):
    """A datetime as Yangon wall time (naive -> assumed Yangon)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=YANGON_TZ)
    return dt.astimezone(YANGON_TZ)


def next_schedule_transition(after_dt):
    """Next datetime the schedule flips (open->closed or closed->open)
    strictly after `after_dt` (Yangon wall time)."""
    after_dt = _as_yangon(after_dt)
    open_min, close_min = _shop_hours_minutes()
    day = after_dt.date()
    candidates = []
    for d in (day, day + timedelta(days=1)):
        candidates.append(
            datetime(d.year, d.month, d.day, open_min // 60,
                     open_min % 60, tzinfo=YANGON_TZ)
        )
        candidates.append(
            datetime(d.year, d.month, d.day, close_min // 60,
                     close_min % 60, tzinfo=YANGON_TZ)
        )
    return min(c for c in candidates if c > after_dt)


def _read_shop_override():
    """(mode, set_at) from SHOP_CLOSED_FILE. mode is 'open',
    'closed' or 'auto'; set_at is a datetime or None."""
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
    # Old format {"closed": bool}.
    if data.get("closed"):
        try:
            mtime = os.path.getmtime(SHOP_CLOSED_FILE)
            set_at = datetime.fromtimestamp(mtime, tz=YANGON_TZ)
        except OSError:
            set_at = None
        return "closed", set_at
    return "auto", None


def _write_shop_override(mode, set_at=None) -> None:
    if set_at is None:
        set_at = datetime.now(YANGON_TZ)
    with open(SHOP_CLOSED_FILE, "w", encoding="utf-8") as fh:
        json.dump({"mode": mode, "set_at": set_at.isoformat()}, fh)


def set_shop_override(mode, set_at=None) -> None:
    """Set a manual override: 'open', 'closed' or 'auto' (clear).
    `set_at` is the moment it was set (now by default; tests pass
    an explicit time)."""
    if mode not in ("open", "closed", "auto"):
        raise ValueError(mode)
    _write_shop_override(mode, set_at)


def active_override(now=None):
    """'open' / 'closed' if a manual /open or /close is in force at
    `now`, else None. An override holds until the next scheduled
    open/close boundary after it was set, then the schedule resumes."""
    now = _as_yangon(now or datetime.now(YANGON_TZ))
    mode, set_at = _read_shop_override()
    if mode not in ("open", "closed") or set_at is None:
        return None
    set_at = _as_yangon(set_at)
    if now < set_at:
        return None  # the override was not set yet at this time
    if now >= next_schedule_transition(set_at):
        return None  # expired: the schedule has taken over again
    return mode


def is_shop_closed_manual(now=None) -> bool:
    """True when a live /close override is in force at `now`."""
    return active_override(now) == "closed"


def set_shop_closed_manual(closed: bool) -> None:
    """Backwards-compatible wrapper: True -> manual close now,
    False -> clear any override (back to the schedule)."""
    set_shop_override("closed" if closed else "auto")


def is_open_now(now: datetime | None = None) -> bool:
    """True when the shop is open at `now`: a live /open override
    forces open, a live /close override forces closed, otherwise
    the owner-set daily hours (default 06:00–22:00)."""
    now = _as_yangon(now or datetime.now(YANGON_TZ))
    override = active_override(now)
    if override == "open":
        return True
    if override == "closed":
        return False
    open_min, close_min = _shop_hours_minutes()
    minutes = now.hour * 60 + now.minute
    return open_min <= minutes < close_min


def can_order_now_for(chat_id, now: datetime | None = None) -> bool:
    """Order-entry gate by chat id (same rule as can_order_now, usable
    without an Update — the server-route pickup job has only the
    stored customer_chat_id). v19.15 24/7 ORDERING: a manual close
    blocks everyone, including the owner; otherwise ordering is
    ALWAYS open — the 06:00–22:00 hours are the DELIVERY window now,
    not the ordering window, so a night order is taken for morning
    delivery. `chat_id` is kept for the callers' shape (the rule no
    longer depends on who is ordering)."""
    if is_shop_closed_manual(now):
        return False
    return True


def can_order_now(update, now: datetime | None = None) -> bool:
    """Order-entry gate (v19.15 24/7 ORDERING): a manual close blocks
    everyone, including the owner; otherwise ordering is always open
    — the old owner-only night-test bypass is gone because everyone
    may order at night now."""
    return can_order_now_for(update.effective_chat.id, now)


# Burmese time formatting for the owner-set hours.
_MY_DIGITS = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")


def my_digits(text) -> str:
    """Arabic digits -> Myanmar digits, e.g. 10 -> '၁၀'."""
    return str(text).translate(_MY_DIGITS)


def format_my_time(minutes: int) -> str:
    """Minutes since midnight -> Burmese, e.g. 360 -> 'မနက် ၆ နာရီ',
    1320 -> 'ည ၁၀ နာရီ', 510 -> 'မနက် ၈ နာရီ မိနစ် ၃၀'."""
    hour, minute = divmod(int(minutes), 60)
    if 5 <= hour <= 11:
        part = "မနက်"
    elif 12 <= hour <= 15:
        part = "နေ့လယ်"
    elif 16 <= hour <= 18:
        part = "ညနေ"
    else:
        part = "ည"
    hour12 = hour % 12 or 12
    text = f"{part} {my_digits(hour12)} နာရီ"
    if minute:
        text += f" မိနစ် {my_digits(minute)}"
    return text


def format_th_time(minutes: int) -> str:
    """Minutes since midnight -> Thai spoken time, e.g. 480 ->
    '8 โมงเช้า', 1320 -> '4 ทุ่ม', 510 -> '8 โมงครึ่ง'. The
    half-hour form drops the part-of-day word ('8 โมงครึ่ง',
    not '8 โมงเช้าครึ่ง')."""
    hour, minute = divmod(int(minutes), 60)
    if hour == 0:
        base, half = "เที่ยงคืน", "เที่ยงคืนครึ่ง"
    elif 1 <= hour <= 5:
        base, half = f"ตี {hour}", f"ตี {hour} ครึ่ง"
    elif 6 <= hour <= 11:
        base, half = f"{hour} โมงเช้า", f"{hour} โมงครึ่ง"
    elif hour == 12:
        base, half = "เที่ยง", "เที่ยงครึ่ง"
    elif hour == 13:
        base, half = "บ่ายโมง", "บ่ายโมงครึ่ง"
    elif 14 <= hour <= 15:
        base, half = f"บ่าย {hour - 12} โมง", f"บ่าย {hour - 12} โมงครึ่ง"
    elif 16 <= hour <= 18:
        base, half = f"{hour - 12} โมงเย็น", f"{hour - 12} โมงเย็นครึ่ง"
    else:  # 19-23
        base, half = f"{hour - 18} ทุ่ม", f"{hour - 18} ทุ่มครึ่ง"
    if minute == 30:
        return half
    if minute:
        return f"{base} {minute} นาที"
    return base


def format_en_time(minutes: int) -> str:
    """Minutes since midnight -> English, e.g. 480 -> '8 AM',
    1320 -> '10 PM', 510 -> '8:30 AM'."""
    hour, minute = divmod(int(minutes), 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if minute:
        return f"{hour12}:{minute:02d} {suffix}"
    return f"{hour12} {suffix}"



def format_zh_time(minutes: int) -> str:
    """Minutes since midnight -> Chinese (DRAFT), e.g. 480 ->
    '早上8点', 1320 -> '晚上10点', 510 -> '早上8点30分'."""
    hour, minute = divmod(int(minutes), 60)
    if 5 <= hour <= 11:
        part = "早上"
    elif hour == 12:
        part = "中午"
    elif 13 <= hour <= 18:
        part = "下午"
    elif 19 <= hour <= 23:
        part = "晚上"
    else:
        part = "凌晨"
    hour12 = hour % 12 or 12
    text = f"{part}{hour12}点"
    if minute:
        text += f"{minute}分"
    return text


def format_hours_for_lang(minutes: int, lang: str) -> str:
    """Minutes since midnight -> the hours text for one language."""
    if lang == "my":
        return format_my_time(minutes)
    if lang == "th":
        return format_th_time(minutes)
    if lang == "zh":
        return format_zh_time(minutes)
    return format_en_time(minutes)


# v19.15: the owner-set hours are the DELIVERY window now, so the
# hours line carries a delivery label in the user's language.
DELIVERY_HOURS_LABEL = {
    "my": "ပို့ချိန်",
    "th": "เวลาจัดส่ง",
    "en": "Delivery",
    "zh": "配送时间",
}


def hours_line(lang=DEFAULT_LANG) -> str:
    """The DELIVERY-hours line from the owner-set hours, in the user's
    language, e.g. '🕕 ပို့ချိန် မနက် ၆ နာရီ — ည ၁၀ နာရီ' for the
    06:00–22:00 defaults. Defaults to Burmese (all existing callers)."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    open_min, close_min = _shop_hours_minutes()
    return (
        f"🕕 {DELIVERY_HOURS_LABEL[lang]} "
        f"{format_hours_for_lang(open_min, lang)} — "
        f"{format_hours_for_lang(close_min, lang)}"
    )


def hours_range_text(lang=DEFAULT_LANG) -> str:
    """The DELIVERY-hours range without the clock emoji, for
    sentences, in the user's language — with the delivery label,
    e.g. Burmese 'ပို့ချိန် မနက် ၆ နာရီ — ည ၁၀ နာရီ'."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    open_min, close_min = _shop_hours_minutes()
    return (
        f"{DELIVERY_HOURS_LABEL[lang]} "
        f"{format_hours_for_lang(open_min, lang)} — "
        f"{format_hours_for_lang(close_min, lang)}"
    )


def next_open_text(now=None, lang=DEFAULT_LANG) -> str:
    """The next scheduled opening in the user's language, e.g.
    Burmese 'မနက် ၆ နာရီ' or 'မနက်ဖြန် မနက် ၆ နာရီ'."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    now = _as_yangon(now or datetime.now(YANGON_TZ))
    open_min, _ = _shop_hours_minutes()
    time_text = format_hours_for_lang(open_min, lang)
    today = now.hour * 60 + now.minute < open_min
    if lang == "th":
        return ("วันนี้ " if today else "พรุ่งนี้ ") + time_text
    if lang == "en":
        return ("today at " if today else "tomorrow at ") + time_text
    if lang == "zh":
        return ("今天" if today else "明天") + time_text
    if today:
        return time_text
    return "မနက်ဖြန် " + time_text


def format_until(dt, now=None) -> str:
    """Burmese for a boundary datetime, e.g. 'မနက် ၆ နာရီ' or
    'မနက်ဖြန် မနက် ၆ နာရီ'."""
    dt = _as_yangon(dt)
    now = _as_yangon(now or datetime.now(YANGON_TZ))
    text = format_my_time(dt.hour * 60 + dt.minute)
    if dt.date() != now.date():
        text = "မနက်ဖြန် " + text
    return text


# Default-hours rendering, kept for compatibility (the old static
# HOURS_LINE); new code uses hours_line() above so it always shows
# the owner's current hours.
HOURS_LINE = "🕕 မနက် ၆ နာရီ — ည ၁၀ နာရီ"


def price_text(lang=DEFAULT_LANG) -> str:
    """The delivery-fee message in the user's language (Min,
    2026-11-12 09:41: the other languages NOW, not later)."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return (
        lang_text(PRICE_TEXTS, lang)
        + f"\n\n{hours_line(lang)}\n"
        + f"{PHONES_LINE}\n\n"
        + lang_text(PRICE_ORDER_LINE_TEXTS, lang)
    )


def location_text(lang=DEFAULT_LANG) -> str:
    """The location intro in the user's language (Min, 2026-11-12
    09:55: order-flow i18n). Defaults to Burmese."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return lang_text(LOCATION_TEXTS, lang).format(
        area=LOCATION_AREA_TEXTS[lang],
        loc_btn=location_button_text(lang),
        addr_btn=address_button_text(lang),
        hours=hours_line(lang),
        phones=PHONES_LINE,
    )


def phone_text(lang=DEFAULT_LANG) -> str:
    """The phone-order message in the user's language."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return (
        lang_text(PHONE_HEAD_TEXTS, lang)
        + f"\n\n{PHONES_LINE}\n"
        + f"{hours_line(lang)}\n\n"
        + lang_text(PHONE_TAIL_TEXTS, lang)
    )


def closed_text(now=None, lang=DEFAULT_LANG) -> str:
    """The 'closed' message, with the owner-set DELIVERY hours.
    v19.15 24/7 ORDERING: ordering is always open, so the ONLY
    closed case left is a manual /close — this always returns the
    manual text now (CLOSED_MANUAL_TEXTS). `now` is kept for the
    callers' shape. In the user's language (Min, 2026-11-12 09:55:
    order-flow i18n); defaults to Burmese."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    common = {"hours": hours_line(lang), "phones": PHONES_LINE}
    return lang_text(CLOSED_MANUAL_TEXTS, lang).format(**common)


# ---------------------------------------------------------------- night orders
# v19.15 24/7 ORDERING — NIGHT AUTO-MESSAGE (Min, 2026-11-15 12:53):
# an order placed 22:00–06:00 gets this message AFTER the order
# confirmation, so the customer can keep it (morning delivery) or
# cancel it. The Burmese text is Min's FINAL approved wording,
# used EXACTLY as he gave it (correct spelling "ဖြစ်ပါစေ").
# NOTE on the first line: the task's approved text reads
# "မင်္ဂလာညချမ်းလေး ဖြစ်ပါစေရှင့်" — the earlier memory note has
# the same words with different spacing ("ချမ်း လေးဖြစ် ပါစေ").
# The task text is used verbatim; Min should confirm the spacing.
# th/en/zh below are plain DRAFT translations — NOT approved text;
# Min must check them before they are relied on.
NIGHT_ORDER_START_HOUR = 22
NIGHT_ORDER_END_HOUR = 6

NIGHT_ORDER_TEXTS = {
    "my": (
        "မင်္ဂလာညချမ်းလေး ဖြစ်ပါစေရှင့် 🌙🙏\n"
        "ညီမတို့ အော်ဒါပိတ်ချိန်လေး ရောက်နေပြီးမလို့၊ မှာထားတဲ့ အော်ဒါလေးကို မနက်ကြရင် စောစောလေး ပို့ပေးပါမယ်နော် 🌅\n"
        "အော်ဒါကို အတည်ပြုထားချင်လား ရှင့်၊ cancel လုပ်ထားချင်လား ရှင့် — အောက်က ခလုတ်နဲ့ ရွေးပေးပါနော်၊ ကျေးဇူးတင်ပါတယ်ရှင့် 💜"
    ),
    # DRAFT — Min has not approved the Thai text.
    "th": (
        "สวัสดีตอนกลางคืนค่ะ 🌙🙏\n"
        "ตอนนี้เป็นช่วงที่ร้านปิดรับออเดอร์แล้วค่ะ ออเดอร์ของคุณจะจัดส่งให้แต่เช้าพรุ่งนี้นะคะ 🌅\n"
        "ต้องการยืนยันออเดอร์ไว้ไหมคะ หรือจะยกเลิกดีคะ — กดปุ่มด้านล่างเลือกได้เลยนะคะ ขอบคุณค่ะ 💜"
    ),
    # DRAFT — Min has not approved the English text.
    "en": (
        "Good evening 🌙🙏\n"
        "Our shop is closed for orders right now, so we will deliver your order first thing in the morning 🌅\n"
        "Would you like to keep your order, or cancel it? — please choose with a button below, thank you 💜"
    ),
    # DRAFT — Min has not approved the Chinese text.
    "zh": (
        "晚上好 🌙🙏\n"
        "现在是本店停止接单的时间哦，您的订单将在明天早上尽快送达 🌅\n"
        "您想保留订单还是取消呢？— 请点击下方按钮选择，谢谢 💜"
    ),
}

# DRAFT labels for th/en/zh (same approval note as the text above);
# the Burmese labels are Min's exact words.
NIGHT_ORDER_BUTTONS = {
    "my": ("✅ အတည်ပြုထားမယ်", "❌ Cancel လုပ်မယ်"),
    "th": ("✅ ยืนยันออเดอร์ไว้", "❌ ยกเลิกออเดอร์"),
    "en": ("✅ Keep my order", "❌ Cancel it"),
    "zh": ("✅ 确认保留订单", "❌ 取消订单"),
}


def is_night_order_time(now=None) -> bool:
    """True 22:00–06:00 Yangon wall time (the night-order window)."""
    now = _as_yangon(now or datetime.now(YANGON_TZ))
    return now.hour >= NIGHT_ORDER_START_HOUR or now.hour < NIGHT_ORDER_END_HOUR


def night_order_text(lang=DEFAULT_LANG) -> str:
    """The night auto-message in the customer's language."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    return lang_text(NIGHT_ORDER_TEXTS, lang)


def night_order_keyboard(order_no, lang=DEFAULT_LANG):
    """✅ keep / ❌ cancel inline buttons for a night order."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    keep, cancel = NIGHT_ORDER_BUTTONS.get(lang) or NIGHT_ORDER_BUTTONS["my"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(keep, callback_data=f"night_ok:{order_no}"),
        InlineKeyboardButton(cancel, callback_data=f"night_cancel:{order_no}"),
    ]])


async def maybe_send_night_order_message(context, chat_id, order_no,
                                         lang=None, now=None) -> bool:
    """Send the night auto-message after an order confirmation —
    ONLY 22:00–06:00 Yangon. Best-effort: a failed send returns
    False and NEVER raises into the confirmation flow."""
    if not is_night_order_time(now):
        return False
    lang = lang if lang in LANGS else DEFAULT_LANG
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=night_order_text(lang),
            reply_markup=night_order_keyboard(order_no, lang),
        )
        return True
    except Exception:
        return False


def normalize_mm_phone(text: str):
    """Return a normalized Myanmar phone string or None if it looks wrong."""
    if not text:
        return None
    t = re.sub(r"[\s\-()]", "", text.strip())
    if t.startswith("+959"):
        t = "09" + t[4:]
    elif t.startswith("959"):
        t = "09" + t[3:]
    if re.fullmatch(r"09\d{7,9}", t):
        return t
    return None


def maps_link(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat},{lon}"


def fmt_place(place: dict) -> str:
    if place.get("type") == "pin":
        return f"📍 {maps_link(place['lat'], place['lon'])}"
    return f"🏠 {place.get('text', '')}"


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE,
            created_at TEXT,
            customer_name TEXT,
            customer_chat_id INTEGER,
            sender_phone TEXT,
            pickup TEXT,
            dropoff TEXT,
            receiver_phone TEXT,
            item TEXT,
            photo_file_id TEXT
        )"""
    )
    # Shop-owner registrations from the Mini App (2026-11-05):
    # shops stay unlisted until the admin contacts the owner and
    # gets their menu — registration only records the request.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shop_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            shop_name TEXT,
            owner_name TEXT,
            owner_chat_id INTEGER,
            phone TEXT,
            address TEXT,
            category TEXT,
            note TEXT,
            shop_message TEXT,
            location_lat REAL,
            location_lon REAL,
            map_link TEXT,
            status TEXT
        )"""
    )
    # Migrate DBs that predate the owner's-words field (2026-11-05).
    try:
        conn.execute("ALTER TABLE shop_registrations ADD COLUMN shop_message TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrate DBs that predate the mandatory location pin (2026-11-05
    # evening): a shop cannot open without its address + location.
    try:
        conn.execute("ALTER TABLE shop_registrations ADD COLUMN location_lat REAL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE shop_registrations ADD COLUMN location_lon REAL")
    except sqlite3.OperationalError:
        pass
    # Add optional fallback map link without changing existing registrations.
    try:
        conn.execute("ALTER TABLE shop_registrations ADD COLUMN map_link TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrate older DBs that lack sender_phone.
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN sender_phone TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrate DBs that predate Mini App orders (2026-11-05):
    # source is "bot" for the chat flow, "mini_app" for Mini App orders.
    # shop holds the shop name for orders placed from a shop's page.
    for column in ("source TEXT", "note TEXT", "shop TEXT"):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    # Idempotency key for Mini App retries / Telegram duplicate delivery.
    # Nullable so historical Bot orders remain unchanged. The partial unique
    # index allows legacy rows with NULL while preventing key reuse.
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency_key "
        "ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    # User language choice (2026-11-11, Part 1): one row per Telegram
    # chat id. Additive only; a missing row means "never chose"
    # and the user gets the language picker on /start.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_langs (
            chat_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL,
            updated_at TEXT
        )"""
    )
    # Order shop-decision status (2026-11-11, Part 2): 'new' while the
    # shop has not answered, then 'shop_confirmed' / 'shop_rejected'.
    # Additive only; old rows keep NULL, treated as 'new'.
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN status TEXT")
    except sqlite3.OperationalError:
        pass
    # Customer location on Mini App orders (2026-11-11, v5): the
    # customer's GPS/manual location saved as JSON, or NULL when the
    # app sent none. Additive only; old rows keep NULL.
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN customer_location TEXT")
    except sqlite3.OperationalError:
        pass
    # Test-order flag (2026-11-12, v11 follow-up): 1 when the order
    # is an owner test order from a sample shop (Mini App payload
    # is_test: true), else 0. Additive only; old rows keep NULL,
    # treated as real orders (0) everywhere it is read.
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN is_test INTEGER")
    except sqlite3.OperationalError:
        pass
    # Server-route pickup (2026-11-12): orders the Mini App posts to
    # the API (source='api') are claimed by the bot pickup job
    # (picked_up=1, picked_up_at set) and then notified exactly once.
    # Additive only; old rows keep NULL, treated as unpicked (0).
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN picked_up INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN picked_up_at TEXT")
    except sqlite3.OperationalError:
        pass

    # Status notifications (2026-11-19): the API writes dashboard
    # status changes as order_status_events rows (actor_type='admin');
    # the bot tells the customer. The API owns the table's shape, so
    # the bot only mirrors it here (CREATE IF NOT EXISTS is a no-op
    # when the API already made it) and adds the notified_at marker
    # the notify job uses to send each event exactly once.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS order_status_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, "
        "status TEXT NOT NULL, note TEXT, "
        "actor_type TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL, "
        "notified_at TEXT, FOREIGN KEY(order_id) REFERENCES orders(id))"
    )
    try:
        conn.execute("ALTER TABLE order_status_events ADD COLUMN notified_at TEXT")
    except sqlite3.OperationalError:
        pass
    # Shop menu / hours / photos / guide video (2026-11-11, v5):
    # menu_json {"text": ..., "items": [{name, price_baht}]},
    # hours_json {"open": "06:00", "close": "22:00", "days": [0..6]},
    # photos_confirmed 1 when the shop ticked the 3-photo checklist,
    # video_file_id = Telegram file_id of the guide video sent in the
    # bot chat (max 5 minutes), video_link = backup link. Additive
    # only; old registrations keep NULL/0.
    for column in (
        "menu_json TEXT",
        "hours_json TEXT",
        "photos_confirmed INTEGER",
        "photos_note TEXT",
        "video_file_id TEXT",
        "video_duration INTEGER",
        "video_link TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE shop_registrations ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    # Riders + deliveries (2026-11-11): the rider assignment flow.
    # riders: one row per delivery rider. status 'pending' until Min
    # approves (via /addrider, which approves directly, or
    # /rideron); 'suspended' riders are never assigned. base_lat /
    # base_lon is the rider's home/base pin (updated whenever they
    # share a location); last_job_at drives the rotation tiebreak.
    # Additive only; no real riders exist yet.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS riders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            name TEXT NOT NULL,
            phone TEXT,
            chat_id INTEGER UNIQUE,
            base_lat REAL,
            base_lon REAL,
            status TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','approved','suspended')),
            last_job_at TEXT
        )"""
    )
    # Deliveries + rider positions + delivery events: the SAME DDL
    # as migrations/005_deliveries_and_rider_positions.sql, so the
    # Admin Dashboard (backend/api.py) reads exactly the rows the
    # bot writes — but only when the API is pointed at this same
    # database file (SAFEDELI_DB_PATH). One active delivery per
    # order; positions are append-only history, latest row per
    # delivery is the rider's current position.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
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
          FOREIGN KEY (order_id) REFERENCES orders(id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_order_status "
        "ON deliveries(order_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_rider_chat "
        "ON deliveries(rider_chat_id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rider_positions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          delivery_id INTEGER NOT NULL,
          lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
          lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
          accuracy_m REAL CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
          source TEXT NOT NULL DEFAULT 'api'
            CHECK (source IN ('telegram_live','manual','api')),
          recorded_at TEXT NOT NULL,
          FOREIGN KEY (delivery_id) REFERENCES deliveries(id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rider_positions_delivery_recorded "
        "ON rider_positions(delivery_id, recorded_at, id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_events (
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
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_events_order_created "
        "ON delivery_events(order_id, created_at, id)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_delivery_events_no_update "
        "BEFORE UPDATE ON delivery_events BEGIN "
        "SELECT RAISE(ABORT, 'delivery_events are append-only'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_delivery_events_no_delete "
        "BEFORE DELETE ON delivery_events BEGIN "
        "SELECT RAISE(ABORT, 'delivery_events are append-only'); END"
    )
    # Finish additive schema setup before callers start a write transaction.
    conn.commit()
    return conn


def next_order_no(conn, now: datetime) -> str:
    """Return the next daily order number without reusing gaps.

    COUNT()+1 can collide with an existing order number after an order row
    is removed or imported with a sequence gap (for example 001 and 003
    exist, so COUNT()+1 incorrectly proposes 003 again). BEGIN IMMEDIATE
    in save_order() serializes this lookup and the subsequent insert.
    """
    day = now.strftime("%Y%m%d")
    cur = conn.execute(
        "SELECT COALESCE(MAX(CAST(substr(order_no, 13) AS INTEGER)), 0) "
        "FROM orders WHERE order_no LIKE ? AND length(order_no) = 15 "
        "AND substr(order_no, 13) GLOB '[0-9][0-9][0-9]'",
        (f"SD-{day}-%",),
    )
    seq = cur.fetchone()[0] + 1
    return f"SD-{day}-{seq:03d}"


def save_order(order: dict) -> tuple:
    """Save an order and return (order_no, is_replay).

    M-8 (2026-11-15): on an idempotency hit the existing order_no is
    returned with is_replay=True, so the caller can skip the customer
    confirmation and the owner card instead of sending them a second
    time. A fresh insert returns (order_no, False).
    """
    now = datetime.now(YANGON_TZ)
    conn = db_connect()
    try:
        # Serialize order-number allocation across concurrent bot updates.
        # Without this lock, two writers can both COUNT()+1 and race on the
        # same UNIQUE order_no. SQLite releases the lock on commit/rollback.
        conn.execute("BEGIN IMMEDIATE")
        idempotency_key = order.get("idempotency_key")
        customer_chat_id = order.get("customer_chat_id")
        if idempotency_key:
            existing = conn.execute(
                "SELECT order_no, customer_chat_id FROM orders WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                # A key can only replay the original customer's order.
                if existing[1] != customer_chat_id:
                    raise ValueError("Order reference already belongs to another customer.")
                conn.commit()
                return existing[0], True
        order_no = next_order_no(conn, now)
        sender_phone = order.get("sender_phone") or order.get("receiver_phone", "")
        customer_location = order.get("customer_location")
        customer_location_json = (
            json.dumps(customer_location, ensure_ascii=False)
            if isinstance(customer_location, dict)
            else None
        )
        conn.execute(
            """INSERT INTO orders
               (order_no, created_at, customer_name, customer_chat_id,
                sender_phone, pickup, dropoff, receiver_phone, item,
                photo_file_id, source, note, shop, idempotency_key, status,
                customer_location, is_test)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_no,
                now.isoformat(timespec="seconds"),
                order.get("customer_name", ""),
                customer_chat_id,
                sender_phone,
                fmt_place(order["pickup"]),
                fmt_place(order["dropoff"]),
                order.get("receiver_phone", sender_phone),
                order.get("item", ""),
                order.get("photo_file_id"),
                order.get("source", "bot"),
                order.get("note", ""),
                order.get("shop", ""),
                idempotency_key,
                "new",
                customer_location_json,
                1 if order.get("is_test") else 0,
            ),
        )
        conn.commit()
        return order_no, False
    finally:
        conn.close()


def today_orders():
    day = datetime.now(YANGON_TZ).strftime("%Y-%m-%d")
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT order_no, sender_phone, item FROM orders "
            "WHERE created_at LIKE ? ORDER BY id",
            (f"{day}%",),
        )
        return cur.fetchall()
    finally:
        conn.close()


def save_shop_registration(reg: dict) -> int:
    """Save a Mini App shop-owner registration, status 'pending'.

    The shop is NOT published by registering — Min contacts the
    owner and asks for their menu first. Returns the new row id.
    """
    now = datetime.now(YANGON_TZ)
    conn = db_connect()
    try:
        menu_json = None
        if reg.get("menu_items") or reg.get("menu_text"):
            menu_json = json.dumps(
                {
                    "text": reg.get("menu_text", ""),
                    "items": reg.get("menu_items", []),
                },
                ensure_ascii=False,
            )
        hours_json = None
        if reg.get("hours"):
            hours_json = json.dumps(reg["hours"], ensure_ascii=False)
        cur = conn.execute(
            """INSERT INTO shop_registrations
               (created_at, shop_name, owner_name, owner_chat_id,
                phone, address, category, note, shop_message,
                location_lat, location_lon, map_link, status,
                menu_json, hours_json, photos_confirmed, photos_note,
                video_file_id, video_link, video_duration)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now.isoformat(timespec="seconds"),
                reg["shop_name"],
                reg["owner_name"],
                reg.get("owner_chat_id"),
                reg["phone"],
                reg["address"],
                reg.get("category", ""),
                reg.get("note", ""),
                reg.get("shop_message", ""),
                reg.get("location_lat"),
                reg.get("location_lon"),
                reg.get("map_link", ""),
                reg.get("status", "pending"),
                menu_json,
                hours_json,
                1 if reg.get("photos_confirmed") else 0,
                reg.get("photos_note", ""),
                reg.get("video_file_id"),
                reg.get("video_link", ""),
                reg.get("video_duration"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def pending_shops():
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT id, shop_name, owner_name, phone, location_lat, "
            "location_lon FROM shop_registrations "
            "WHERE status='pending' ORDER BY id"
        )
        return cur.fetchall()
    finally:
        conn.close()


def get_shop_registration(reg_id: int):
    """Return one registration without changing its historical record.

    Column order: id, created_at, shop_name, owner_name,
    owner_chat_id, phone, address, category, note, shop_message,
    location_lat, location_lon, map_link, status, menu_json,
    hours_json, photos_confirmed, photos_note, video_file_id,
    video_duration, video_link.
    """
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT id, created_at, shop_name, owner_name, owner_chat_id, "
            "phone, address, category, note, shop_message, location_lat, "
            "location_lon, map_link, status, menu_json, hours_json, "
            "photos_confirmed, photos_note, video_file_id, video_duration, "
            "video_link FROM shop_registrations WHERE id=?",
            (reg_id,),
        ).fetchone()
    finally:
        conn.close()


def set_shop_registration_status(reg_id: int, status: str) -> bool:
    """Update review status only; never delete or publish a shop."""
    if status not in ("verified", "rejected"):
        raise ValueError("Unsupported registration status")
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE shop_registrations SET status=? WHERE id=? AND status='pending'",
            (status, reg_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def save_registrant_location(chat_id, lat, lon):
    """Store a location pin a pending registrant sent in the bot
    chat on their latest pending registration. Returns True when
    a registration was updated."""
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE shop_registrations SET location_lat=?, location_lon=? "
            "WHERE owner_chat_id=? AND status='pending'",
            (lat, lon, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_registrant_video(chat_id, file_id, duration=None) -> bool:
    """Store a guide-video file_id on the sender's latest pending
    registration (2026-11-11, v5). The video itself lives in
    Telegram; only the file_id is kept. Returns False when the
    file_id is not a string or no pending registration exists —
    the video is then only forwarded to Min, never stored."""
    if not isinstance(file_id, str) or not file_id:
        return False
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = None
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE shop_registrations SET video_file_id=?, video_duration=? "
            "WHERE id=(SELECT id FROM shop_registrations "
            "WHERE owner_chat_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1)",
            (file_id, duration, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_verified_shop_video(chat_id, file_id, duration=None) -> bool:
    """Store a new guide-video file_id on the sender's latest verified
    registration (2026-11-11, fix pass). Same rule as the pending
    capture: the video itself lives in Telegram; only the file_id is
    kept. Returns False when the file_id is not a string or no
    verified registration exists — the video is then only forwarded
    to Min, never stored."""
    if not isinstance(file_id, str) or not file_id:
        return False
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = None
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE shop_registrations SET video_file_id=?, video_duration=? "
            "WHERE id=(SELECT id FROM shop_registrations "
            "WHERE owner_chat_id=? AND status='verified' "
            "ORDER BY id DESC LIMIT 1)",
            (file_id, duration, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def pending_shop_by_chat(chat_id):
    """Return (shop_name, owner_name) for a chat with a pending
    shop registration, or None. Used to decide whether photos or
    texts that chat sends are verification material for Min.
    """
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT shop_name, owner_name FROM shop_registrations "
            "WHERE owner_chat_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def verified_shop_by_chat(chat_id):
    """Return (shop_name, owner_name) for a chat with a verified
    shop registration, or None. Used so a verified shop can update
    its guide video later in the bot chat (2026-11-11, fix pass).
    """
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT shop_name, owner_name FROM shop_registrations "
            "WHERE owner_chat_id=? AND status='verified' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def get_user_lang(chat_id):
    """Return a user's saved language code, or None if never chosen.

    Read-only: a missing database file means 'never chose' and is
    NOT created here — creating it is the writer's job (set_user_lang).
    """
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    if not os.path.exists(DB_PATH):
        return None
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT lang FROM user_langs WHERE chat_id=?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    if row and row[0] in LANGS:
        return row[0]
    return None


def set_user_lang(chat_id, lang) -> bool:
    """Save a user's language choice. Returns False for bad input."""
    if lang not in LANGS:
        return False
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    conn = db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_langs (chat_id, lang, updated_at) "
            "VALUES (?,?,?)",
            (
                chat_id,
                lang,
                datetime.now(YANGON_TZ).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return True


# ---------------------------------------------------------------- auto-translate
# Bot auto-translation (Min, 2026-11-12 17:50): the bot is the middleman
# between customer and owner. When a free-text chat message is in a
# language the reader can't read, the bot machine-translates it.
# Scope: Burmese / Thai / English / Chinese ONLY. Shan was removed
# from the bot entirely (Min, 2026-11-12 17:53) and Akha was never in
# it — those languages are never translated, never marked, and pass
# through exactly as before.
# Backend: MyMemory (api.mymemory.translated.net) — free public API,
# no key, no signup, stdlib urllib only. Limits (verified live
# 2026-11-12): ~5,000 chars/day per IP anonymous (50,000 with a
# contact email in the `de` parameter — NOT wired in; that needs the
# owner's email and his decision), max 500 UTF-8 bytes per request
# (long texts are chunked), translation-memory quality (good for
# common phrases, weaker for unseen sentences). When the quota is
# used up MyMemory answers with a "MYMEMORY WARNING: ..." string
# instead of a translation — treated as a failure, never shown.
# The Google Translate free endpoint (client=gtx) was tested and
# REJECTED: from this VM it answers 302 to google.com/sorry + 429
# (bot detection on datacenter IPs); the VPS is a datacenter IP too.
# NEVER machine-translate structured data (order cards, receipts,
# SD- numbers, items/prices/addresses, shop/rider cards) — only the
# free-text chat messages in forward_registrant_message (customer ->
# owner) and reply_customer / send_pending_customer_reply
# (owner -> customer). Every failure falls back to the ORIGINAL text
# + a short note in the recipient's language; a message is never
# dropped and a translation is always marked 🤖 so nobody mistakes
# it for the person's own words.
MT_LANGS = ("my", "th", "en", "zh")
MT_CODES = {"my": "my", "th": "th", "en": "en", "zh": "zh-CN"}
MT_URL = "https://api.mymemory.translated.net/get"
MT_TIMEOUT = 8.0
MT_MAX_BYTES = 450  # MyMemory caps q at 500 UTF-8 bytes; stay under it

MT_MARKER = {
    "my": "🤖 စက်ဖြင့် အလိုအလျောက် ဘာသာပြန်ထားပါတယ်",
    "th": "🤖 แปลอัตโนมัติโดยระบบ",
    "en": "🤖 Auto-translated",
    "zh": "🤖 机器自动翻译",
}
MT_FAILED_NOTE = {
    "my": "🤖 အလိုအလျောက် ဘာသာပြန် မရလို့ မူရင်းစာအတိုင်း ပြထားပါတယ်။",
    "th": "🤖 แปลอัตโนมัติไม่สำเร็จ จึงแสดงข้อความต้นฉบับค่ะ",
    "en": "🤖 Auto-translation didn't work, so here is the original message.",
    "zh": "🤖 自动翻译失败，以下是原文。",
}


def _mt_split_chunks(text):
    """Split free text into <= MT_MAX_BYTES UTF-8 chunks for MyMemory.

    Splits on whitespace, keeping words whole; a single over-long
    word is hard-split by bytes (never splitting a character).
    Returns a list of non-empty chunks.
    """
    chunks, cur = [], ""
    for word in re.split(r"(\s+)", text):
        if not word:
            continue
        trial = cur + word
        if len(trial.encode("utf-8")) > MT_MAX_BYTES and cur.strip():
            chunks.append(cur.strip())
            cur = word if not word.isspace() else ""
            while len(cur.encode("utf-8")) > MT_MAX_BYTES:
                piece, size = "", 0
                for ch in cur:
                    n = len(ch.encode("utf-8"))
                    if size + n > MT_MAX_BYTES:
                        break
                    piece += ch
                    size += n
                chunks.append(piece)
                cur = cur[len(piece):]
        else:
            cur = trial
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [text]


def _mt_mymemory_translate(text, src, dest):
    """One MyMemory call. Returns the translated text.

    Raises ValueError on any unusable answer (HTTP error, bad JSON,
    empty translation, quota warning) so callers fall back to the
    original text. Never returns a warning string as a translation.
    """
    params = urlencode({"q": text, "langpair": f"{src}|{dest}"})
    req = urllib.request.Request(
        MT_URL + "?" + params, headers={"User-Agent": "SafeDeliBot/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=MT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise ValueError(f"MyMemory request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("MyMemory bad response")
    if payload.get("responseStatus") != 200:
        raise ValueError(f"MyMemory status {payload.get('responseStatus')}")
    out = ((payload.get("responseData") or {}).get("translatedText") or "").strip()
    if not out or out.startswith(
        ("MYMEMORY WARNING", "QUERY LENGTH", "INVALID ")
    ):
        raise ValueError("MyMemory quota/empty answer")
    return out


def _detect_lang_by_script(text):
    """Guess my/th/en/zh from the dominant script in free text.

    Myanmar/Thai/Chinese use distinct scripts; Latin reads as
    English here (the bot's Latin-script users write English).
    Returns None when no script dominates (digits/emoji only).
    Offline and deterministic — only a hint for the owner's reply;
    a wrong guess still carries the 🤖 marker. Note: Shan shares
    the Myanmar script, so a Shan reply would read as Burmese —
    the owner replies in Burmese in practice.
    """
    counts = {"my": 0, "th": 0, "en": 0, "zh": 0}
    for ch in text or "":
        o = ord(ch)
        if 0x1000 <= o <= 0x109F:
            counts["my"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            counts["th"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["zh"] += 1
        elif ch.isascii() and ch.isalpha():
            counts["en"] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def translate_text(text, src_lang, dest_lang):
    """Machine-translate free text. Returns (status, translated).

    status is "ok" (translated is the translation), "same" (source
    and target are the same language, or the translation came back
    identical — nothing to mark), "unsupported" (either side is not
    one of my/th/en/zh — Shan/Akha are never attempted), or
    "failed" (the free service errored, quota out, or empty answer).
    Never raises; never returns a warning string as a translation.
    """
    if not text or not text.strip():
        return ("same", None)
    src = (src_lang or "").strip().lower()
    dest = (dest_lang or "").strip().lower()
    if dest not in MT_LANGS:
        return ("unsupported", None)
    if src not in MT_LANGS:
        return ("unsupported", None)
    if src == dest:
        return ("same", None)
    try:
        parts = [
            _mt_mymemory_translate(chunk, MT_CODES[src], MT_CODES[dest])
            for chunk in _mt_split_chunks(text)
        ]
    except Exception:
        return ("failed", None)
    out = " ".join(p.strip() for p in parts if p.strip())
    if not out or out.strip() == text.strip():
        return ("same", None)
    return ("ok", out)


def customer_text_for_owner(text, chat_id):
    """Build the exact owner-forward text for a customer's free-text
    message: translation + original + marker, original + failure
    note, or the original untouched (same language, Shan/Akha, or
    never chose a language — all pass through exactly as today)."""
    lang = get_user_lang(chat_id) or DEFAULT_LANG
    if lang not in MT_LANGS or lang == DEFAULT_LANG:
        return text
    status, out = translate_text(text, lang, DEFAULT_LANG)
    if status == "ok":
        return (
            f"{out}\n{MT_MARKER[DEFAULT_LANG]}\n"
            f"— — —\n"
            f"မူရင်း ({lang}): {text}"
        )
    if status == "failed":
        return f"{text}\n{MT_FAILED_NOTE[DEFAULT_LANG]}"
    return text


def owner_reply_for_customer(text, chat_id):
    """Build the exact text to deliver an owner's free-text reply to
    a customer: translated + marker, original + failure note, or the
    original untouched (same language, Shan/Akha, or never chose).
    The owner's language is guessed from the script (he writes
    Burmese in practice); a wrong guess still carries the 🤖 mark."""
    lang = get_user_lang(chat_id) or DEFAULT_LANG
    if lang not in MT_LANGS or lang == DEFAULT_LANG:
        return text
    src = _detect_lang_by_script(text) or DEFAULT_LANG
    if src == lang:
        return text
    status, out = translate_text(text, src, lang)
    if status == "ok":
        return f"{out}\n{MT_MARKER[lang]}"
    if status == "failed":
        return f"{text}\n{MT_FAILED_NOTE[lang]}"
    return text


def get_shop_owner_chat_id(shop_name):
    """Return the Telegram chat id of a shop's owner, or None.

    Looks the shop up by name in shop_registrations (verified/approved first,
    then pending). Sample shops are never registered, so they return
    None and the order goes to the owner only.
    """
    if not shop_name:
        return None
    if str(shop_name).strip().lower().startswith("sample"):
        return None
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT owner_chat_id FROM shop_registrations "
            "WHERE lower(shop_name)=lower(?) AND owner_chat_id IS NOT NULL "
            "ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'approved' THEN 0 "
            "WHEN 'pending' THEN 1 ELSE 2 END, id DESC LIMIT 1",
            (str(shop_name).strip(),),
        ).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None
    return None


def get_shop_video(shop_name, shop_id=None):
    """Return (video_file_id, duration, shop_name) for a shop's
    stored guide video, or None when the shop has none.

    Looks the shop up by name (verified/approved first, then pending), or
    by numeric registration id when shop_id is one. Sample shops
    never have a video, so they return None. Only a stored file_id
    is returned — a video is never invented or fetched from a link.
    """
    conn = db_connect()
    try:
        if shop_id is not None:
            try:
                rid = int(str(shop_id))
            except (TypeError, ValueError):
                rid = None
            if rid is not None:
                row = conn.execute(
                    "SELECT video_file_id, video_duration, shop_name "
                    "FROM shop_registrations WHERE id=? "
                    "AND video_file_id IS NOT NULL AND video_file_id != ''",
                    (rid,),
                ).fetchone()
                if row:
                    return row
        if shop_name and not str(shop_name).strip().lower().startswith("sample"):
            row = conn.execute(
                "SELECT video_file_id, video_duration, shop_name "
                "FROM shop_registrations "
                "WHERE lower(shop_name)=lower(?) "
                "AND video_file_id IS NOT NULL AND video_file_id != '' "
                "ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'approved' THEN 0 "
                "WHEN 'pending' THEN 1 ELSE 2 END, id DESC LIMIT 1",
                (str(shop_name).strip(),),
            ).fetchone()
            if row:
                return row
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------- riders + deliveries
# Rider assignment (2026-11-11, owner's decision): the NEAREST active
# rider to the shop gets the job first (fastest pickup, least fuel);
# when two riders are equally near, the one who waited longest since
# their last job gets it (rotation as tiebreak). The bot AUTO-SENDS
# the job card to the assigned rider's private chat; the Telegram
# group of riders is backup only. A rider position is NEVER invented:
# it comes from the rider's last reported position, else their
# registered base pin, else "unknown" (last in line).
# Delivery status machine — identical to backend/api.py
# (DELIVERY_NEXT): created -> assigned -> accepted -> picked_up ->
# delivering -> delivered; failed/cancelled from any non-terminal
# state. The ONLY exception is a rider decline, which resets an
# 'assigned' delivery back to 'created' (no rider) — handled by
# decline_delivery(), never by set_delivery_status().
DELIVERY_STATUSES = (
    "created", "assigned", "accepted", "picked_up",
    "delivering", "delivered", "failed", "cancelled",
)
DELIVERY_TERMINAL = ("delivered", "failed", "cancelled")
DELIVERY_NEXT = {
    "created": ("assigned", "failed", "cancelled"),
    "assigned": ("accepted", "failed", "cancelled"),
    "accepted": ("picked_up", "failed", "cancelled"),
    "picked_up": ("delivering", "failed", "cancelled"),
    "delivering": ("delivered", "failed", "cancelled"),
}

_RIDER_COLUMNS = (
    "id", "created_at", "name", "phone", "chat_id", "base_lat",
    "base_lon", "status", "last_job_at",
)
_DELIVERY_COLUMNS = (
    "id", "order_id", "rider_name", "rider_phone", "rider_chat_id",
    "pickup_lat", "pickup_lon", "pickup_address", "dropoff_lat",
    "dropoff_lon", "dropoff_address", "status", "created_at",
    "updated_at", "created_by",
)


def _dict_row(row, columns):
    """Map a plain-tuple sqlite3 row to a dict by column name.

    db_connect() sets no row_factory (existing code indexes rows
    by position), so the rider/delivery helpers convert here.
    """
    if row is None:
        return None
    return dict(zip(columns, row))


def _utc_now_iso() -> str:
    """UTC ISO timestamp for the delivery tables.

    The dashboard reads these tables and treats naive timestamps
    as UTC, so the bot writes UTC (unlike orders, which use
    Yangon time). A wrong timezone here would make the rider's
    position age display wrong on the dashboard.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_valid_lat_lon(lat, lon) -> bool:
    return (
        isinstance(lat, (int, float)) and not isinstance(lat, bool)
        and isinstance(lon, (int, float)) and not isinstance(lon, bool)
        and math.isfinite(lat) and math.isfinite(lon)
        and -90 <= lat <= 90 and -180 <= lon <= 180
    )


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km (good enough for Tachileik)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _parse_pin_text(text):
    """Extract (lat, lon) from a stored fmt_place string.

    A pin is stored as "📍 https://maps.google.com/?q=20.45,99.87"
    or as plain "20.45,99.87". Returns None when the text holds no
    valid coordinate pair — an address is never turned into a pin.
    """
    if not isinstance(text, str) or not text:
        return None
    m = re.search(r"[?&]q=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", text)
    if not m:
        m = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", text)
    if not m:
        return None
    try:
        lat, lon = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    return (lat, lon) if _is_valid_lat_lon(lat, lon) else None


def _clean_place_text(text):
    """Stored place string as a plain address (no 📍/🏠 prefix)."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"^[📍🏠]\s*", "", text).strip()


def _maps_point(lat, lon, address):
    """One Google Maps point: 'lat,lon' when coordinates exist,
    else the address text, else None. Same rule as backend/api.py."""
    if lat is not None and lon is not None and _is_valid_lat_lon(lat, lon):
        return f"{lat:.6f},{lon:.6f}"
    if isinstance(address, str) and address.strip():
        return address.strip()[:500]
    return None


def build_maps_directions_url(origin, destination, waypoints=None):
    """One Google Maps directions link (no API key needed).

    Same format as backend/api.py: origin -> waypoints -> destination,
    travelmode driving. Returns None when origin or destination is
    missing — a route is never built from a guessed point.
    """
    if not origin or not destination:
        return None
    params = {"api": "1", "origin": origin, "destination": destination}
    if waypoints:
        params["waypoints"] = "|".join(waypoints)
    params["travelmode"] = "driving"
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def get_shop_location(shop_name):
    """Return (lat, lon, address) for a shop's stored pin, or None.

    Looks the shop up by name in shop_registrations (verified/approved first,
    then pending), like get_shop_owner_chat_id. Sample shops return
    None — they have no real location.
    """
    if not shop_name or str(shop_name).strip().lower().startswith("sample"):
        return None
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT location_lat, location_lon, address FROM shop_registrations "
            "WHERE lower(shop_name)=lower(?) "
            "ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'approved' THEN 0 "
            "WHEN 'pending' THEN 1 ELSE 2 END, id DESC LIMIT 1",
            (str(shop_name).strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    lat, lon, address = row[0], row[1], row[2]
    if lat is not None and lon is not None and _is_valid_lat_lon(lat, lon):
        return (lat, lon, address or "")
    if address:
        return (None, None, address)
    return None


def save_rider(rider: dict) -> int:
    """Save a rider. Admin adds riders directly, so the default
    status is 'approved'. Returns the new row id. Raises ValueError
    for a missing name/chat id, sqlite3.IntegrityError when the
    chat id is already registered (use get_rider_by_chat first).
    """
    name = str(rider.get("name") or "").strip()
    if not name:
        raise ValueError("Rider name is required")
    try:
        chat_id = int(rider["chat_id"])
    except (TypeError, ValueError, KeyError):
        raise ValueError("Rider Telegram chat id is required")
    phone = str(rider.get("phone") or "").strip()
    base_lat, base_lon = rider.get("base_lat"), rider.get("base_lon")
    if base_lat is not None or base_lon is not None:
        if not _is_valid_lat_lon(base_lat, base_lon):
            raise ValueError("Rider base location is out of range")
    status = rider.get("status") or "approved"
    if status not in ("pending", "approved", "suspended"):
        raise ValueError("Unsupported rider status")
    conn = db_connect()
    try:
        cur = conn.execute(
            "INSERT INTO riders (created_at, name, phone, chat_id, "
            "base_lat, base_lon, status, last_job_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                _utc_now_iso(),
                name,
                phone,
                chat_id,
                base_lat,
                base_lon,
                status,
                None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_rider(rider_id):
    conn = db_connect()
    try:
        return _dict_row(conn.execute(
            "SELECT id, created_at, name, phone, chat_id, base_lat, "
            "base_lon, status, last_job_at FROM riders WHERE id=?",
            (rider_id,),
        ).fetchone(), _RIDER_COLUMNS)
    finally:
        conn.close()


def get_rider_by_chat(chat_id):
    """Return a rider row by Telegram chat id, or None."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    conn = db_connect()
    try:
        return _dict_row(conn.execute(
            "SELECT id, created_at, name, phone, chat_id, base_lat, "
            "base_lon, status, last_job_at FROM riders WHERE chat_id=?",
            (chat_id,),
        ).fetchone(), _RIDER_COLUMNS)
    finally:
        conn.close()


def list_riders(status=None):
    """All riders, newest first; optionally filtered by status."""
    conn = db_connect()
    try:
        if status:
            cur = conn.execute(
                "SELECT id, created_at, name, phone, chat_id, base_lat, "
                "base_lon, status, last_job_at FROM riders "
                "WHERE status=? ORDER BY id",
                (status,),
            )
        else:
            cur = conn.execute(
                "SELECT id, created_at, name, phone, chat_id, base_lat, "
                "base_lon, status, last_job_at FROM riders ORDER BY id"
            )
        return [_dict_row(r, _RIDER_COLUMNS) for r in cur.fetchall()]
    finally:
        conn.close()


def set_rider_status(rider_id, status) -> bool:
    """Approve / suspend / re-approve a rider. Returns False when
    the rider does not exist. Money records are never touched."""
    if status not in ("pending", "approved", "suspended"):
        raise ValueError("Unsupported rider status")
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE riders SET status=? WHERE id=?", (status, rider_id)
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def set_rider_base_location(chat_id, lat, lon) -> bool:
    """Store a rider's latest known position as their base pin.
    Returns False for a bad pin or an unknown rider."""
    if not _is_valid_lat_lon(lat, lon):
        return False
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE riders SET base_lat=?, base_lon=? WHERE chat_id=?",
            (lat, lon, chat_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_order_by_no(order_no):
    """Return one order row by its SD- number, or None.

    Column order: id, order_no, created_at, customer_name,
    customer_chat_id, sender_phone, pickup, dropoff, receiver_phone,
    item, photo_file_id, source, note, shop, idempotency_key, status,
    customer_location (JSON, or None when the app sent none),
    is_test (1 = owner test order from a sample shop, 0/NULL = real).
    """
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT id, order_no, created_at, customer_name, "
            "customer_chat_id, sender_phone, pickup, dropoff, "
            "receiver_phone, item, photo_file_id, source, note, shop, "
            "idempotency_key, status, customer_location, is_test FROM orders WHERE order_no=?",
            (order_no,),
        ).fetchone()
    finally:
        conn.close()


def get_order_by_id(order_id):
    """Return one order row by its integer id (same columns as
    get_order_by_no), or None."""
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT id, order_no, created_at, customer_name, "
            "customer_chat_id, sender_phone, pickup, dropoff, "
            "receiver_phone, item, photo_file_id, source, note, shop, "
            "idempotency_key, status, customer_location FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()


def get_delivery(delivery_id, conn=None):
    """Return one delivery row, or None. Pass an open connection
    when calling from inside a write transaction."""
    own = conn is None
    if own:
        conn = db_connect()
    try:
        return _dict_row(conn.execute(
            "SELECT id, order_id, rider_name, rider_phone, rider_chat_id, "
            "pickup_lat, pickup_lon, pickup_address, dropoff_lat, "
            "dropoff_lon, dropoff_address, status, created_at, "
            "updated_at, created_by FROM deliveries WHERE id=?",
            (delivery_id,),
        ).fetchone(), _DELIVERY_COLUMNS)
    finally:
        if own:
            conn.close()


def get_active_delivery_for_order(order_id):
    conn = db_connect()
    try:
        return _dict_row(conn.execute(
            "SELECT id, order_id, rider_name, rider_phone, rider_chat_id, "
            "pickup_lat, pickup_lon, pickup_address, dropoff_lat, "
            "dropoff_lon, dropoff_address, status, created_at, "
            "updated_at, created_by FROM deliveries "
            "WHERE order_id=? AND status NOT IN ('delivered','failed','cancelled') "
            "ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone(), _DELIVERY_COLUMNS)
    finally:
        conn.close()


def get_active_delivery_for_rider(chat_id):
    """The rider's current (non-terminal) delivery, or None."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    conn = db_connect()
    try:
        return _dict_row(conn.execute(
            "SELECT id, order_id, rider_name, rider_phone, rider_chat_id, "
            "pickup_lat, pickup_lon, pickup_address, dropoff_lat, "
            "dropoff_lon, dropoff_address, status, created_at, "
            "updated_at, created_by FROM deliveries "
            "WHERE rider_chat_id=? AND status NOT IN ('delivered','failed','cancelled') "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone(), _DELIVERY_COLUMNS)
    finally:
        conn.close()


def _resolve_delivery_points(order, pickup_lat, pickup_lon,
                             pickup_address, dropoff_lat, dropoff_lon,
                             dropoff_address):
    """Resolve pickup / drop-off points for a delivery.

    Pickup: explicit coords > shop's stored pin > pin inside the
    order's pickup string > address text. Drop-off: explicit coords
    > the order's customer_location GPS > pin inside the order's
    drop-off string > address text. Returns
    (pickup_lat, pickup_lon, pickup_address,
     dropoff_lat, dropoff_lon, dropoff_address).
    A point that resolves to neither coords nor address text is
    (None, None, None) — the caller refuses to create the delivery
    instead of inventing a location.
    """
    shop_name = order[13]
    if (pickup_lat is None) != (pickup_lon is None):
        raise ValueError("pickup_lat and pickup_lon must be given together")
    if (dropoff_lat is None) != (dropoff_lon is None):
        raise ValueError("dropoff_lat and dropoff_lon must be given together")
    if pickup_lat is not None and not _is_valid_lat_lon(pickup_lat, pickup_lon):
        raise ValueError("pickup_lat/lon are out of range")
    if dropoff_lat is not None and not _is_valid_lat_lon(dropoff_lat, dropoff_lon):
        raise ValueError("dropoff_lat/lon are out of range")
    shop_loc = get_shop_location(shop_name)
    pickup_from_shop = False
    if pickup_lat is None:
        if shop_loc and shop_loc[0] is not None:
            pickup_lat, pickup_lon = shop_loc[0], shop_loc[1]
            pickup_from_shop = True
        else:
            parsed = _parse_pin_text(order[6])
            if parsed:
                pickup_lat, pickup_lon = parsed
    if not pickup_address:
        if pickup_from_shop and shop_loc and shop_loc[2]:
            pickup_address = shop_loc[2]
        elif order[6]:
            text = _clean_place_text(order[6])
            # A pin string is a Maps URL, not an address — only real
            # address text belongs here.
            if text and not text.startswith("http"):
                pickup_address = text
    customer_address = None
    if dropoff_lat is None:
        loc = order[16]
        if isinstance(loc, str) and loc:
            try:
                loc = json.loads(loc)
            except (json.JSONDecodeError, TypeError):
                loc = None
        if isinstance(loc, dict):
            lat, lon = loc.get("lat"), loc.get("lon")
            if lat is not None and lon is not None and _is_valid_lat_lon(lat, lon):
                dropoff_lat, dropoff_lon = lat, lon
            if isinstance(loc.get("address"), str) and loc["address"].strip():
                customer_address = loc["address"].strip()
        if dropoff_lat is None:
            parsed = _parse_pin_text(order[7])
            if parsed:
                dropoff_lat, dropoff_lon = parsed
    if not dropoff_address:
        if customer_address:
            dropoff_address = customer_address
        elif order[7]:
            text = _clean_place_text(order[7])
            if text and not text.startswith("http"):
                dropoff_address = text
    return (pickup_lat, pickup_lon, pickup_address,
            dropoff_lat, dropoff_lon, dropoff_address)


def create_delivery(order_id, pickup_lat=None, pickup_lon=None,
                    pickup_address=None, dropoff_lat=None, dropoff_lon=None,
                    dropoff_address=None, actor_type="system", actor_id=None):
    """Create a delivery for an order, status 'created'.

    Raises ValueError when the order is missing, an active delivery
    already exists for it, or a pickup/drop-off point cannot be
    resolved. Writes the 'created' delivery event in the same
    transaction. Returns the new delivery id.
    """
    if actor_type not in ("admin", "operations", "worker", "system"):
        raise ValueError("Unsupported delivery actor type")
    order = get_order_by_id(order_id)
    if not order:
        raise ValueError("Order not found")
    points = _resolve_delivery_points(
        order, pickup_lat, pickup_lon, pickup_address,
        dropoff_lat, dropoff_lon, dropoff_address)
    (pickup_lat, pickup_lon, pickup_address,
     dropoff_lat, dropoff_lon, dropoff_address) = points
    if _maps_point(pickup_lat, pickup_lon, pickup_address) is None:
        raise ValueError("Pickup location is missing: give pickup coordinates or an address")
    if _maps_point(dropoff_lat, dropoff_lon, dropoff_address) is None:
        raise ValueError("Drop-off location is missing: give dropoff coordinates or an address")
    now = _utc_now_iso()
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT id FROM deliveries WHERE order_id=? "
            "AND status NOT IN ('delivered','failed','cancelled')",
            (order_id,),
        ).fetchone()
        if active:
            raise ValueError("An active delivery already exists for this order")
        cur = conn.execute(
            "INSERT INTO deliveries (order_id, rider_name, rider_phone, "
            "rider_chat_id, pickup_lat, pickup_lon, pickup_address, "
            "dropoff_lat, dropoff_lon, dropoff_address, status, "
            "created_at, updated_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'created',?,?,?)",
            (
                order_id, None, None, None,
                pickup_lat, pickup_lon, pickup_address,
                dropoff_lat, dropoff_lon, dropoff_address,
                now, now, actor_id,
            ),
        )
        delivery_id = cur.lastrowid
        conn.execute(
            "INSERT INTO delivery_events (order_id, event_type, "
            "actor_type, actor_id, created_at) "
            "VALUES (?, 'created', ?, ?, ?)",
            (order_id, actor_type, actor_id, now),
        )
        conn.commit()
        return delivery_id
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _delivery_transition(conn, delivery, new_status, actor_type, actor_id=None):
    """Move a delivery to new_status inside an open transaction.

    Enforces the DELIVERY_NEXT machine and writes the event row.
    Raises ValueError on an unknown status, a terminal delivery,
    or an illegal jump. The rider-decline reset (assigned ->
    created) is NOT allowed here — decline_delivery() owns it.
    """
    if new_status not in DELIVERY_STATUSES:
        raise ValueError("Unknown delivery status")
    current = delivery["status"]
    if current in DELIVERY_TERMINAL or new_status not in DELIVERY_NEXT.get(current, ()):
        raise ValueError(f"Cannot move delivery from {current} to {new_status}")
    now = _utc_now_iso()
    conn.execute(
        "UPDATE deliveries SET status=?, updated_at=? WHERE id=?",
        (new_status, now, delivery["id"]),
    )
    conn.execute(
        "INSERT INTO delivery_events (order_id, event_type, "
        "actor_type, actor_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (delivery["order_id"], new_status, actor_type, actor_id, now),
    )
    return now


def set_delivery_status(delivery_id, new_status, actor_type, actor_id=None) -> bool:
    """Move a delivery to new_status. Returns False when the
    delivery does not exist. Raises ValueError on an illegal jump.
    """
    if actor_type not in ("admin", "operations", "worker", "system"):
        raise ValueError("Unsupported delivery actor type")
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        delivery = get_delivery(delivery_id, conn)
        if not delivery:
            conn.rollback()
            return False
        _delivery_transition(conn, delivery, new_status, actor_type, actor_id)
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def record_rider_position(delivery_id, lat, lon, accuracy_m=None, source="manual"):
    """Store one rider position for a delivery (append-only).

    Mirrors the API's position endpoint: valid lat/lon required,
    delivery must exist and not be terminal. Returns the new row
    id. Raises ValueError otherwise. Positions are never written
    to any audit log (high frequency).
    """
    if not _is_valid_lat_lon(lat, lon):
        raise ValueError("Valid lat/lon are required")
    if source not in ("telegram_live", "manual", "api"):
        raise ValueError("Unknown position source")
    if accuracy_m is not None:
        if isinstance(accuracy_m, bool) or not isinstance(accuracy_m, (int, float)):
            raise ValueError("accuracy_m must be a non-negative number")
        if not math.isfinite(accuracy_m) or accuracy_m < 0:
            raise ValueError("accuracy_m must be a non-negative number")
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        delivery = get_delivery(delivery_id, conn)
        if not delivery:
            raise ValueError("Delivery not found")
        if delivery["status"] in DELIVERY_TERMINAL:
            raise ValueError("Delivery is already finished")
        cur = conn.execute(
            "INSERT INTO rider_positions (delivery_id, lat, lon, "
            "accuracy_m, source, recorded_at) VALUES (?,?,?,?,?,?)",
            (delivery_id, lat, lon, accuracy_m, source, _utc_now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def latest_rider_position(delivery_id, conn=None):
    """Latest reported position for a delivery, or None."""
    own = conn is None
    if own:
        conn = db_connect()
    try:
        row = conn.execute(
            "SELECT id, delivery_id, lat, lon, accuracy_m, source, "
            "recorded_at FROM rider_positions WHERE delivery_id=? "
            "ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (delivery_id,),
        ).fetchone()
        return _dict_row(row, ("id", "delivery_id", "lat", "lon",
                               "accuracy_m", "source", "recorded_at"))
    finally:
        if own:
            conn.close()


def latest_rider_position_for_chat(chat_id, conn=None):
    """Latest position reported by a rider across all their
    deliveries (their last known position), or None. Used for the
    nearest-rider distance — a rider with no reported position
    falls back to their base pin."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    own = conn is None
    if own:
        conn = db_connect()
    try:
        row = conn.execute(
            "SELECT p.lat, p.lon, p.recorded_at FROM rider_positions p "
            "JOIN deliveries d ON d.id = p.delivery_id "
            "WHERE d.rider_chat_id=? "
            "ORDER BY p.recorded_at DESC, p.id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return _dict_row(row, ("lat", "lon", "recorded_at"))
    finally:
        if own:
            conn.close()


def delivery_route_url(delivery, conn=None):
    """One continuous Google Maps route link for a delivery:
    rider -> shop pickup -> customer drop-off. Same rule as
    backend/api.py: the rider's latest position is the origin when
    known, else the route starts at the shop. Returns None when
    no drop-off point can be resolved."""
    if isinstance(delivery, dict):
        d = delivery
    else:
        d = dict(delivery)
    pickup = _maps_point(d.get("pickup_lat"), d.get("pickup_lon"), d.get("pickup_address"))
    dropoff = _maps_point(d.get("dropoff_lat"), d.get("dropoff_lon"), d.get("dropoff_address"))
    if not dropoff:
        return None
    pos = latest_rider_position(d["id"], conn)
    if pos:
        origin = _maps_point(pos.get("lat"), pos.get("lon"), None)
        if origin and pickup and origin != pickup:
            return build_maps_directions_url(origin, dropoff, [pickup])
    if not pickup:
        return None
    return build_maps_directions_url(pickup, dropoff)


def pick_nearest_rider(delivery_id, exclude_chat_ids=(), conn=None):
    """Pick the nearest APPROVED rider for a delivery.

    Distance is measured from the rider's last known position
    (latest reported position, else base pin) to the shop pickup
    point. Riders with no position at all sort last. Tiebreak:
    the rider who waited longest since their last job (never had
    a job first). Riders already on an active delivery are
    skipped — one job at a time. Returns a rider row or None.
    """
    own = conn is None
    if own:
        conn = db_connect()
    try:
        delivery = get_delivery(delivery_id, conn)
        if not delivery:
            return None
        pickup_lat, pickup_lon = delivery["pickup_lat"], delivery["pickup_lon"]
        excluded = set()
        for chat_id in exclude_chat_ids or ():
            try:
                excluded.add(int(chat_id))
            except (TypeError, ValueError):
                pass
        rows = [_dict_row(r, _RIDER_COLUMNS) for r in conn.execute(
            "SELECT id, created_at, name, phone, chat_id, base_lat, "
            "base_lon, status, last_job_at FROM riders "
            "WHERE status='approved' ORDER BY id"
        ).fetchall()]
        scored = []
        for rider in rows:
            chat_id = rider["chat_id"]
            if chat_id in excluded:
                continue
            busy = conn.execute(
                "SELECT id FROM deliveries WHERE rider_chat_id=? "
                "AND status NOT IN ('delivered','failed','cancelled') LIMIT 1",
                (chat_id,),
            ).fetchone()
            if busy:
                continue
            dist = None
            pos = latest_rider_position_for_chat(chat_id, conn)
            point = None
            if pos:
                point = (pos["lat"], pos["lon"])
            elif rider["base_lat"] is not None and rider["base_lon"] is not None:
                point = (rider["base_lat"], rider["base_lon"])
            if (point and pickup_lat is not None and pickup_lon is not None
                    and _is_valid_lat_lon(point[0], point[1])):
                dist = _haversine_km(point[0], point[1], pickup_lat, pickup_lon)
            scored.append((dist is None, dist if dist is not None else 0.0,
                           rider["last_job_at"] is not None,
                           rider["last_job_at"] or "", rider))
        if not scored:
            return None
        scored.sort(key=lambda s: (s[0], s[1], s[2], s[3]))
        return scored[0][4]
    finally:
        if own:
            conn.close()


def assign_delivery_to_rider(delivery_id, rider, actor_type="system", actor_id=None):
    """Assign a 'created' delivery to a rider (status -> assigned).

    Writes the 'assigned' event and stamps the rider's last_job_at
    in the same transaction. Raises ValueError when the delivery
    is missing or not in 'created'.
    """
    if actor_type not in ("admin", "operations", "worker", "system"):
        raise ValueError("Unsupported delivery actor type")
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        delivery = get_delivery(delivery_id, conn)
        if not delivery:
            raise ValueError("Delivery not found")
        if delivery["status"] != "created":
            raise ValueError(f"Cannot assign delivery in status {delivery['status']}")
        now = _utc_now_iso()
        conn.execute(
            "UPDATE deliveries SET rider_name=?, rider_phone=?, "
            "rider_chat_id=?, updated_at=? WHERE id=?",
            (rider["name"], rider["phone"], rider["chat_id"], now, delivery_id),
        )
        _delivery_transition(conn, delivery, "assigned", actor_type, actor_id)
        conn.execute(
            "UPDATE riders SET last_job_at=? WHERE id=?",
            (now, rider["id"]),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def decline_delivery(delivery_id, rider_chat_id, actor_type="worker"):
    """A rider declines their assigned job.

    Writes a 'reassigned' event, then offers the job to the next
    nearest rider (excluding the decliner). Returns the new rider
    row, or None when no rider is left — in that case the delivery
    goes back to 'created' (no rider) so Min can assign it by hand
    with /deliver. This reset is the one place 'assigned' ->
    'created' is allowed. Raises ValueError when the delivery is
    missing, not 'assigned', or the tapper is not the assigned
    rider.
    """
    if actor_type not in ("admin", "operations", "worker", "system"):
        raise ValueError("Unsupported delivery actor type")
    try:
        rider_chat_id = int(rider_chat_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid rider chat id")
    delivery = get_delivery(delivery_id)
    if not delivery:
        raise ValueError("Delivery not found")
    if delivery["status"] != "assigned":
        raise ValueError(f"Cannot decline delivery in status {delivery['status']}")
    if delivery["rider_chat_id"] != rider_chat_id:
        raise ValueError("Only the assigned rider can decline this delivery")
    order_id = delivery["order_id"]
    nxt = pick_nearest_rider(delivery_id, exclude_chat_ids=(rider_chat_id,))
    now = _utc_now_iso()
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = get_delivery(delivery_id, conn)
        if not fresh or fresh["status"] != "assigned" or fresh["rider_chat_id"] != rider_chat_id:
            raise ValueError("Delivery is no longer assigned to this rider")
        conn.execute(
            "INSERT INTO delivery_events (order_id, event_type, "
            "actor_type, actor_id, note, created_at) "
            "VALUES (?, 'reassigned', ?, ?, ?, ?)",
            (order_id, actor_type, str(rider_chat_id),
             "Rider declined; offered to the next nearest rider", now),
        )
        if nxt is not None:
            conn.execute(
                "UPDATE deliveries SET rider_name=?, rider_phone=?, "
                "rider_chat_id=?, updated_at=? WHERE id=?",
                (nxt["name"], nxt["phone"], nxt["chat_id"], now, delivery_id),
            )
            conn.execute(
                "UPDATE riders SET last_job_at=? WHERE id=?",
                (now, nxt["id"]),
            )
        else:
            conn.execute(
                "UPDATE deliveries SET rider_name=NULL, rider_phone=NULL, "
                "rider_chat_id=NULL, status='created', updated_at=? WHERE id=?",
                (now, delivery_id),
            )
        conn.commit()
        return nxt
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def set_order_status(order_no, status) -> bool:
    """Move an order to a shop-decision status. Only from 'new'.

    Returns False when the order is missing or already decided —
    a shop tapping twice (or two taps racing) cannot flip it back.
    Money records are never deleted; this only updates the status.
    """
    if status not in ("shop_confirmed", "shop_rejected"):
        raise ValueError("Unsupported order status")
    conn = db_connect()
    try:
        cur = conn.execute(
            "UPDATE orders SET status=? WHERE order_no=? "
            "AND (status IS NULL OR status='new')",
            (status, order_no),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def cancel_order_by_no(order_no) -> bool:
    """Cancel one order by its SD- number (status='closed',
    order_status='cancelled'). Only from a live state — an already
    closed/cancelled order returns False (a second tap cannot
    re-cancel it). Money records are never deleted; this only updates
    the status. Used by the night-order cancel button (v19.15)."""
    conn = db_connect()
    try:
        try:
            cur = conn.execute(
                "UPDATE orders SET status='closed', order_status='cancelled' "
                "WHERE order_no=? AND (status IS NULL OR status NOT IN ('closed', 'cancelled'))",
                (order_no,),
            )
        except sqlite3.OperationalError:
            cur = conn.execute(
                "UPDATE orders SET status='closed' "
                "WHERE order_no=? AND (status IS NULL OR status NOT IN ('closed', 'cancelled'))",
                (order_no,),
            )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def format_summary(order: dict, order_no: str = None, lang=DEFAULT_LANG) -> str:
    """Order summary lines. The owner/shop/rider cards call this
    with no lang, so they stay Burmese by design; the customer
    confirmation passes the customer's language (Min, 2026-11-12
    09:55: order-flow i18n)."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    labels = {k: lang_text(v, lang) for k, v in SUMMARY_LABELS.items()}
    head = f"🧾 Order {order_no}" if order_no else labels["head"]
    phone = order.get("sender_phone") or order.get("receiver_phone", "")
    lines = [
        head,
        f"{labels['name']}: {order.get('customer_name', '')}",
        f"{labels['phone']}: {phone}",
        f"{labels['pickup']}: {fmt_place(order['pickup'])}",
        f"{labels['dropoff']}: {fmt_place(order['dropoff'])}",
    ]
    # Customer location from the Mini App (v5, optional): the exact
    # receipt wording the app sends, so Min, the shop and the rider
    # all see the same location line. Absent for bot-chat orders.
    loc = order.get("customer_location")
    if isinstance(loc, dict):
        if loc.get("maps_url"):
            lines.append(f"{labels['loc_gps']} {loc['maps_url']}")
        elif loc.get("address"):
            lines.append(f"{labels['loc_text']} {loc['address']}")
    lines.append(f"{labels['item']}: {order.get('item', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Mini App orders
def _mini_app_place(value):
    """Normalise a Mini App pickup/drop-off value to a place dict.

    The Mini App may send a typed address (string), a pin
    {"lat": ..., "lon": ...} or a Telegram-style location
    {"latitude": ..., "longitude": ...}.

    M-7 (2026-11-15): a bad pin must never crash with a raw Python
    error. A dict that carries ANY coordinate key is a pin attempt,
    so a non-numeric value, an out-of-range value, or only one of
    lat/lon raises the polite Burmese ValueError — parse_mini_app_order's
    caller turns it into a polite reply + retry line. A dict with no
    coordinate key at all is still a typed address.
    """
    if isinstance(value, dict):
        has_lat = "lat" in value or "latitude" in value
        has_lon = "lon" in value or "longitude" in value
        if has_lat or has_lon:
            lat = value.get("lat", value.get("latitude"))
            lon = value.get("lon", value.get("longitude"))
            try:
                lat = float(lat)
                lon = float(lon)
            except (TypeError, ValueError):
                raise ValueError(
                    "တောင်းပန်ပါတယ်ရှင့်။ 🙏 တည်နေရာ မမှန်သေးပါဘူးရှင့်။ ပြန်ရွေးပေးပါနော်။"
                )
            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                raise ValueError(
                    "တောင်းပန်ပါတယ်ရှင့်။ 🙏 တည်နေရာ မမှန်သေးပါဘူးရှင့်။ ပြန်ရွေးပေးပါနော်။"
                )
            return {"type": "pin", "lat": lat, "lon": lon}
        text = value.get("text") or value.get("address") or ""
        return {"type": "text", "text": str(text).strip()}
    return {"type": "text", "text": str(value or "").strip()}


def _pick_field(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _normalize_customer_location(value):
    """Normalise the Mini App's OPTIONAL customer_location field (v5).

    Expected: {maps_url, lat?, lon?, address?, source: "gps"|"manual"}.
    Returns a clean dict or None when absent/invalid — the order is
    never rejected for a bad location, and a location is never
    invented. A maps_url is built from lat/lon when the app sent
    coordinates without one.
    """
    if not isinstance(value, dict):
        return None
    maps_url = value.get("maps_url")
    maps_url = str(maps_url).strip() if isinstance(maps_url, str) else ""
    if maps_url and not (
        maps_url.startswith("http://") or maps_url.startswith("https://")
    ):
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
        maps_url = maps_link(lat, lon)
    address = value.get("address")
    address = str(address).strip() if isinstance(address, str) else ""
    source = value.get("source")
    source = source if source in ("gps", "manual") else "gps"
    if not maps_url and address:
        # v19.22 MAPS LINK FOR TYPED ADDRESSES (owner 2026-11-15 18:58):
        # a customer who typed the address (no GPS pin) used to get no
        # link at all on the order card. Same Maps-search-link fallback
        # the shop-registration path already uses.
        maps_url = "https://maps.google.com/?q=" + quote(address)
    if not maps_url and not address:
        return None
    out = {"source": source}
    if maps_url:
        out["maps_url"] = maps_url
    if lat is not None:
        out["lat"] = lat
        out["lon"] = lon
    if address:
        out["address"] = address
    return out


def _parse_menu_items(value):
    """Return [{name, price_baht}] from the app's menu array (v5).

    The app sends one item per line parsed as {name, price} with
    price an int or null. Items without a name or without a positive
    numeric price are dropped — the raw menu_text is kept separately
    so nothing the shop wrote is lost. No price is ever invented.
    """
    if not isinstance(value, list):
        return []
    items = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        price = entry.get("price")
        # The app sends ints, but a digit string ("50") is the same
        # exact price — accept it rather than dropping the item.
        if isinstance(price, str) and price.strip().isdigit():
            price = int(price.strip())
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        if not name or price <= 0:
            continue
        items.append({"name": name, "price_baht": price})
    return items


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_shop_hours(value):
    """Return {open, close, days} from the app's hours object (v5),
    or None when it is missing or invalid. Days are 0=Sunday..6=Saturday.
    Invalid hours are ignored, never corrected or invented."""
    if not isinstance(value, dict):
        return None
    open_t = str(value.get("open") or "").strip()
    close_t = str(value.get("close") or "").strip()
    if not _HHMM_RE.match(open_t) or not _HHMM_RE.match(close_t):
        return None
    raw_days = value.get("days")
    if not isinstance(raw_days, list):
        return None
    days = set()
    for d in raw_days:
        if isinstance(d, bool):
            continue
        if isinstance(d, str) and d.isdigit():
            d = int(d)
        if isinstance(d, int) and 0 <= d <= 6:
            days.add(d)
    if not days:
        return None
    return {"open": open_t, "close": close_t, "days": sorted(days)}


_WEEKDAYS_MY = ("တနင်္ဂနွေ", "တနင်္လာ", "အင်္ဂါ", "ဗုဒ္ဓဟူး", "ကြာသပတေး", "သောကြာ", "စနေ")


def _format_shop_hours(hours):
    """Display string for a shop's stored hours dict, or '' when absent."""
    if not isinstance(hours, dict):
        return ""
    open_t = hours.get("open") or ""
    close_t = hours.get("close") or ""
    if not open_t or not close_t:
        return ""
    days = hours.get("days") or []
    if len(days) == 7:
        day_text = "နေ့တိုင်း"
    else:
        day_text = ", ".join(
            _WEEKDAYS_MY[d] for d in days if isinstance(d, int) and 0 <= d <= 6
        )
    text = f"{open_t}–{close_t}"
    if day_text:
        text += f" ({day_text})"
    return text


def parse_mini_app_order(data: dict) -> dict:
    """Validate a Mini App order payload and return an order dict
    ready for save_order(). Raises ValueError with a Burmese message
    for the customer when something is missing or wrong.

    Expected payload (sent by the Mini App with tg.sendData):
    {"type": "mini_app_order", "items": ..., "pickup": ...,
     "dropoff": ..., "name": ..., "phone": ..., "note": ...,
     "shop": ...} — "shop" is optional: the shop name when the
    order was placed from a shop's dedicated page, so the order
    card shows which shop it is for. "is_test" (true only for an
    owner test order from a sample shop) and "shop_id" (e.g.
    "sample-001") are parsed through; mini_app_order() only trusts
    is_test from the owner.
    "customer_location" (v5, optional) is the customer's saved
    GPS/manual location {maps_url, lat?, lon?, address?, source} —
    it is normalised, never required, and never invented.
    Key names are matched flexibly (items/item, pickup/pickup_location,
    dropoff/drop_off, name/customer_name, phone/sender_phone).
    No prices are accepted here — the admin quotes the item price +
    delivery fee afterwards, so a price can never be invented.
    """
    if not isinstance(data, dict):
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 Order အချက်အလက် ပုံစံ မမှန်သေးလို့ အတည်မပြုနိုင်သေးပါဘူး။")
    if data.get("type") != "mini_app_order":
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 Order အချက်အလက် ပုံစံ မမှန်သေးလို့ အတည်မပြုနိုင်သေးပါဘူး။")

    items = _pick_field(data, "items", "item")
    if isinstance(items, list):
        items = ", ".join(str(i).strip() for i in items if str(i).strip())
    items = str(items or "").strip()
    if not items:
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 မှာယူမည့် ပစ္စည်း မပါသေးပါဘူးရှင့်။")

    pickup = _mini_app_place(_pick_field(data, "pickup", "pickup_location"))
    if not (pickup["type"] == "pin" or pickup["text"]):
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ပစ္စည်း ယူရမည့်နေရာ မပါသေးပါဘူးရှင့်။")
    dropoff = _mini_app_place(
        _pick_field(data, "dropoff", "drop_off", "dropoff_location", "drop_off_location")
    )
    if not (dropoff["type"] == "pin" or dropoff["text"]):
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ပစ္စည်း ပို့ရမည့်နေရာ မပါသေးပါဘူးရှင့်။")

    name = str(_pick_field(data, "name", "customer_name") or "").strip()
    if not name:
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 နာမည် မပါသေးပါဘူးရှင့်။")

    phone = normalize_mm_phone(str(_pick_field(data, "phone", "sender_phone") or ""))
    if not phone:
        raise ValueError(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဖုန်းနံပါတ် ပုံစံ မမှန်သေးပါဘူး။ 09 နဲ့ စတဲ့ နံပါတ် ပြန်ရေးပေးပါနော်။"
        )

    note = str(_pick_field(data, "note", "notes") or "").strip()
    shop = str(_pick_field(data, "shop", "shop_name") or "").strip()
    # Test-order flag (2026-11-12, v11 follow-up): the Mini App sends
    # is_test: true only for an owner test order from a sample shop,
    # plus shop_id (e.g. "sample-001"). Both are carried through here;
    # mini_app_order() only trusts is_test from the owner, so a
    # crafted payload from a customer can never mark a real order
    # as a test. shop_id is informational only (the shop NAME is
    # what the order card shows); it is never invented.
    is_test = bool(data.get("is_test"))
    shop_id = str(_pick_field(data, "shop_id", "shopId", "shopid") or "").strip()
    customer_location = _normalize_customer_location(data.get("customer_location"))
    idempotency_key = str(data.get("submission_id") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", idempotency_key):
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 Order အမှတ်အသား မမှန်သေးလို့ အတည်မပြုနိုင်သေးပါဘူး။ Mini App ကို ပြန်ဖွင့်ပြီး ထပ်တင်ပေးပါနော်။")
    return {
        "customer_name": name,
        "sender_phone": phone,
        "pickup": pickup,
        "dropoff": dropoff,
        "item": items,
        "shop": shop,
        "shop_id": shop_id,
        "is_test": is_test,
        "note": note,
        "customer_location": customer_location,
        "source": "mini_app",
        "idempotency_key": idempotency_key,
    }


def parse_shop_register(data: dict) -> dict:
    """Validate a Mini App shop-registration payload and return a
    registration dict ready for save_shop_registration(). Raises
    ValueError with a Burmese message when something is missing
    or wrong.

    Expected payload (sent by the Mini App with tg.sendData):
    {"type": "shop_register", "shop": ..., "owner": ...,
     "phone": ..., "address": ..., "category": ..., "note": ...}
    Key names are matched flexibly (shop/shop_name,
    owner/owner_name, phone/owner_phone, address/location).
    Optional v5 additions: "menu" (list of {name, price} with exact
    prices), "menu_text" (the raw menu text), "hours" ({open, close,
    days}), "photos_confirmed" (the shop ticked the 3-photo checklist
    — storefront, interior, owner smiling — sent via the bot chat),
    "video_link" (backup guide-video link; the video FILE goes via
    the bot chat). Invalid menu/hours are ignored, never invented.
    Registering never publishes the shop — the admin contacts
    the owner and asks for the menu first.

    Short form (v19, Min 2026-11-12): when
    data.get("form_version") == "short-v19" the Mini App sends only
    the 5 essentials — shop name, type, phone, location, one
    storefront photo (photo_pending/photo_name; the FILE goes via
    the bot chat). Owner name is OPTIONAL for the short form (the
    old 4-step form still requires it). The location is always a
    maps link: pin -> q=lat,lon; pasted link kept verbatim; address
    -> Maps search link; the link is stored as both maps_url and
    map_link (the existing field).
    """
    if not isinstance(data, dict):
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဆိုင်အချက်အလက် ပုံစံ မမှန်သေးလို့ အတည်မပြုနိုင်သေးပါဘူး။")
    if data.get("type") != "shop_register":
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဆိုင်အချက်အလက် ပုံစံ မမှန်သေးလို့ အတည်မပြုနိုင်သေးပါဘူး။")

    short_form = data.get("form_version") == "short-v19"
    shop = str(_pick_field(data, "shop", "shop_name") or "").strip()
    if not shop:
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဆိုင်နာမည် မပါသေးပါဘူးရှင့်။")
    owner = str(_pick_field(data, "owner", "owner_name") or "").strip()
    if not owner and not short_form:
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဆိုင်ရှင် နာမည် မပါသေးပါဘူးရှင့်။")
    phone = normalize_mm_phone(str(_pick_field(data, "phone", "owner_phone") or ""))
    if not phone:
        raise ValueError(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဖုန်းနံပါတ် ပုံစံ မမှန်သေးပါဘူး။ 09 နဲ့ စတဲ့ နံပါတ် ပြန်ရေးပေးပါနော်။"
        )
    # Shop location pin (lat/lon) — Min, 2026-11-05 evening: a shop
    # cannot open without its address + location pin. The Mini App
    # sends it as {"lat": .., "lon": ..} (or latitude/longitude);
    # a pin sent later in the bot chat is stored by
    # forward_registrant_message. Absence does not reject the
    # registration — the shop simply cannot be listed until the
    # pin arrives, and both sides are told so.
    loc = data.get("location") or data.get("pin") or data.get("shop_location")
    lat = lon = None
    if isinstance(loc, dict):
        try:
            lat = float(loc.get("lat", loc.get("latitude")))
            lon = float(loc.get("lon", loc.get("longitude")))
        except (TypeError, ValueError):
            lat = lon = None
    addr_raw = _pick_field(data, "address", "shop_address")
    if not addr_raw and isinstance(data.get("location"), str):
        addr_raw = data.get("location")
    address = str(addr_raw or "").strip()
    map_link = str(_pick_field(data, "map_link", "maps_url", "google_maps_link") or "").strip()
    if not map_link and lat is not None and lon is not None:
        map_link = f"https://maps.google.com/?q={lat},{lon}"
    elif not map_link and address:
        map_link = "https://maps.google.com/?q=" + quote(address)
    if not address and lat is None and not map_link:
        raise ValueError("တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဆိုင်တည်နေရာ မပါသေးပါဘူးရှင့်။")
    category = str(_pick_field(data, "category", "shop_type") or "").strip()
    note = str(_pick_field(data, "note", "notes") or "").strip()
    shop_message = str(
        _pick_field(data, "shop_message", "message", "owner_message") or ""
    ).strip()
    menu_text = str(_pick_field(data, "menu_text") or "").strip()
    menu_items = _parse_menu_items(data.get("menu"))
    hours = _parse_shop_hours(data.get("hours"))
    photos_confirmed = data.get("photos_confirmed")
    photos_confirmed = (
        1
        if photos_confirmed is True
        or photos_confirmed == 1
        or str(photos_confirmed or "").strip().lower() in ("true", "yes", "1")
        else 0
    )
    photos_note = str(_pick_field(data, "photos_note") or "").strip()
    video_link = str(_pick_field(data, "video_link", "video_url") or "").strip()
    photo_pending = 1 if data.get("photo_pending") else 0
    photo_name = str(data.get("photo_name") or "").strip()
    if short_form and photo_pending and not photos_note:
        # Short form: the one storefront photo comes via the bot chat.
        # Keep the pending note in the DB so Min sees it at a glance.
        photos_note = "ဆိုင်ပုံ ၁ ပုံ ပို့ဖို့ ကျန်သေးတယ် — ဆိုင်ရှင် Bot chat မှာ ပို့လာမယ်"
    return {
        "shop_name": shop,
        "owner_name": owner,
        "phone": phone,
        "address": address,
        "category": category,
        "note": note,
        "shop_message": shop_message,
        "location_lat": lat,
        "location_lon": lon,
        "map_link": map_link,
        "maps_url": map_link,
        "form_version": data.get("form_version") or "",
        "photo_pending": photo_pending,
        "photo_name": photo_name,
        "menu_text": menu_text,
        "menu_items": menu_items,
        "hours": hours,
        "photos_confirmed": photos_confirmed,
        "photos_note": photos_note,
        "video_link": video_link,
        "status": "pending",
    }


async def shop_register(update, context, data, msg):
    """Handle a shop-owner registration sent from the Safe Deli
    Mini App (WebApp data). The registration is saved as pending,
    the owner is told Safe Deli will contact them for their menu,
    and Min gets a card so he can reach out and ask for the menu.
    """
    try:
        reg = parse_shop_register(data)
    except ValueError as exc:
        await msg.reply_text(
            f"{exc}\nMini App ကနေ ပြန်တင်ပေးပါနော်၊ ဒါမှမဟုတ် @SafeDeli_bot ကို "
            "တိုက်ရိုက် ဆက်သွယ်ပေးပါနော်။"
        )
        return
    reg["owner_chat_id"] = update.effective_chat.id
    reg_id = save_shop_registration(reg)

    confirm = (
        "✅ ဆိုင်စာရင်းသွင်းတာ လက်ခံရရှိပါပြီရှင့်။\n"
        "ဆိုင်ကို စစ်ဆေးဖို့ ဒီ chat ထဲမှာ ပို့ပေးပါနော် —\n"
        "📍 ဆိုင်တည်နေရာ\n"
        "📸 ဆိုင်ဓာတ်ပုံ\n"
        "📋 menu ဓာတ်ပုံ\n"
    )
    if reg.get("location_lat") is not None and reg.get("location_lon") is not None:
        confirm += "📍 ဆိုင်တည်နေရာ (location pin) ပို့ပြီး ✅\n"
    else:
        confirm += (
            "⚠️ ဆိုင်တည်နေရာ (location pin) မပို့ရသေးပါဘူးရှင့် —\n"
            "တည်နေရာ ပို့ပေးမှ ဆိုင်ဖွင့်ပေးနိုင်ပါမယ်ရှင့်။\n"
            "ဒီ chat ထဲမှာ 📍 တည်နေရာ ပို့ပေးပါနော်။\n"
        )
    confirm += (
        "Safe Deli က စစ်ဆေးပြီးမှ App ထဲမှာ ဖော်ပြပေးပါမယ်ရှင့်။\n"
        f"ကျေးဇူးတင်ပါတယ် 🙏 {BUSINESS}"
    )
    if reg.get("form_version") == "short-v19":
        # Short form (v19): one step only — ask for the ONE storefront
        # photo in the bot chat. Menu/hours come from Dashboard
        # Settings, so the old menu-photo line is dropped.
        confirm = (
            "✅ ဆိုင်စာရင်းသွင်းတာ လက်ခံရရှိပါပြီရှင့်။\n"
            "ဆိုင်ဓာတ်ပုံ ၁ ပုံကို ဒီ chat ထဲမှာ ပို့ပေးပါနော် 📸\n"
            "Safe Deli က စစ်ဆေးပြီးမှ App ထဲမှာ ဖော်ပြပေးပါမယ်ရှင့်။\n"
            f"ကျေးဇူးတင်ပါတယ် 🙏 {BUSINESS}"
        )
    await msg.reply_text(confirm)
    if OWNER_CHAT_ID:
        card = (
            f"🏪 ဆိုင်ရှင် စာရင်းသွင်းခြင်း အသစ် (#{reg_id})\n"
            f"ဆိုင်: {reg['shop_name']}\n"
            f"ဆိုင်ရှင်: {reg['owner_name'] or '-'}\n"
            f"ဖုန်း: {reg['phone']}\n"
            f"တည်နေရာ: {reg['address']}\n"
            f"အမျိုးအစား: {reg['category'] or '-'}"
        )
        if reg.get("location_lat") is not None and reg.get("location_lon") is not None:
            lat, lon = reg["location_lat"], reg["location_lon"]
            card += (
                f"\n📍 တည်နေရာ pin: ✅ "
                f"https://maps.google.com/?q={lat},{lon}"
            )
        elif reg.get("map_link"):
            card += f"\n📍 Maps လင့်ခ်: {reg['map_link']}"
            if reg.get("address"):
                card += f"\nဆိုင်လိပ်စာ: {reg['address']}"
        else:
            card += f"\n📍 တည်နေရာ pin: ❌ မရရှိသေးပါ\nလိပ်စာ: {reg.get('address') or 'မရရှိသေးပါ'}"
        if reg.get("note"):
            card += f"\nမှတ်ချက်: {reg['note']}"
        if reg.get("shop_message"):
            card += f"\n💬 ဆိုင်ရှင်စကား: {reg['shop_message']}"
        # Menu + hours + photos + guide video (v5): what the shop sent
        # at registration, so Min can verify it at a glance.
        if reg.get("menu_items"):
            card += "\n📋 Menu:"
            for item in reg["menu_items"][:10]:
                card += f"\n• {item['name']} — {item['price_baht']} ဘတ်"
            if len(reg["menu_items"]) > 10:
                card += f"\n• …နောက်ထပ် {len(reg['menu_items']) - 10} မျိုး"
        elif reg.get("menu_text"):
            card += f"\n📋 Menu စာ: {reg['menu_text'][:200]}"
        hours_text = _format_shop_hours(reg.get("hours"))
        if hours_text:
            card += f"\n🕐 ဖွင့်ချိန်: {hours_text}"
        if reg.get("form_version") == "short-v19":
            # Short form: no menu/hours/photos sections — the photo is
            # still pending in the bot chat and Min adds menu/hours in
            # Dashboard Settings.
            card += (
                "\n📸 ဆိုင်ပုံ: ပို့ဖို့ ကျန်သေးတယ် — ဆိုင်ရှင် Bot chat မှာ ပို့လာမယ်\n"
                "📋 မီနူး/ဖွင့်ချိန်: Dashboard Settings မှာ Min ထည့်မယ်"
            )
        else:
            card += (
                "\n📸 ဓာတ်ပုံ ၃ ပုံ: "
                + ("✅ အတည်ပြုပြီး" if reg.get("photos_confirmed") else "❌ မအတည်ပြုရသေးပါ")
            )
        if reg.get("photos_note") and reg.get("form_version") != "short-v19":
            card += f"\n📸 မှတ်ချက်: {reg['photos_note']}"
        if reg.get("video_link"):
            card += f"\n🔗 Video လင့်ခ်: {reg['video_link']}"
        if reg.get("form_version") == "short-v19":
            card += (
                "\n\n📋 စစ်ရန်:\n"
                "၁) ဖုန်းခေါ်ပြီး ဆိုင်ရှင် အစစ်လား မေးပါ\n"
                "၂) ဆိုင်လိပ်စာ + ဆိုင်ဓာတ်ပုံ + တည်နေရာ pin တောင်းပါ\n"
                "ဆိုင်ရှင်က လိပ်စာ/ဓာတ်ပုံ ကို ဒီ Bot မှာ ပို့လာရင် "
                "ဆရာ့ဆီ ပြန်ပို့ပေးပါမယ်။ /shops နဲ့ စာရင်း ကြည့်နိုင်ပါတယ်။"
            )
        else:
            card += (
                "\n\n📋 စစ်ရန်:\n"
                "၁) ဖုန်းခေါ်ပြီး ဆိုင်ရှင် အစစ်လား မေးပါ\n"
                "၂) ဆိုင်လိပ်စာ + ဆိုင်ဓာတ်ပုံ + တည်နေရာ pin တောင်းပါ\n"
                "၃) menu ဓာတ်ပုံက ဈေးနဲ့ ကိုက်လား ကြည့်ပါ\n"
                "ဆိုင်ရှင်က လိပ်စာ/ဓာတ်ပုံ/menu ကို ဒီ Bot မှာ ပို့လာရင် "
                "ဆရာ့ဆီ ပြန်ပို့ပေးပါမယ်။ /shops နဲ့ စာရင်း ကြည့်နိုင်ပါတယ်။"
            )
        try:
            await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=card)
        except Exception:
            pass


def build_order_confirmation(order: dict, order_no: str, lang) -> str:
    """Customer confirmation text for a Mini App order — the same text
    for a sendData order (mini_app_order) and a server-route order
    (pickup job), so the customer can never tell which path delivered
    it. The fee line is Min's exact line (2026-11-12 09:28)."""
    confirm = order_step_text("mini_app_head", lang, order_no=order_no) + "\n"
    if order.get("is_test"):
        confirm += order_step_text("mini_app_test", lang) + "\n"
    if order.get("shop"):
        confirm += order_step_text("mini_app_shop", lang, shop=order["shop"]) + "\n"
    # Customer location (v5): the same receipt line the app shows,
    # so the customer sees what the shop and Min will see.
    loc = order.get("customer_location")
    if isinstance(loc, dict):
        if loc.get("maps_url"):
            confirm += f"{loc['maps_url']}\n"
        elif loc.get("address"):
            confirm += f"{loc['address']}\n"
    confirm += (
        order_step_text("mini_app_price_soon", lang)
        + "\n"
        + order_step_text("mini_app_fee", lang)
        + "\n"
        + order_step_text("mini_app_thanks", lang, business=BUSINESS)
    )
    return confirm


def customer_reply_markup(customer_chat_id):
    """The ↩️ 'ပြန်စာပို့ရန်' button that lets the owner reply to the
    customer (begin_customer_reply, cust_reply:<chat_id>). Returns
    None when the chat id is missing or unusable — the caller then
    sends the message without a button instead of a broken
    'cust_reply:None' callback that no handler would ever match."""
    try:
        chat_id = int(customer_chat_id)
    except (TypeError, ValueError):
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ ပြန်စာပို့ရန်", callback_data=f"cust_reply:{chat_id}")
    ]])


async def send_order_cards(context, order: dict, order_no: str) -> bool:
    """Owner card + shop card for a Mini App order — the same cards for
    a sendData order and a server-route order. Returns True when the
    owner card was delivered, False otherwise.

    M-5 (2026-11-15): the owner send is no longer swallowed silently.
    A failure is logged with logger.exception, the owner send is
    retried once immediately, and the caller learns whether the card
    landed. The shop card stays best-effort, but its failure is
    logged too instead of a silent pass. Test orders never notify a
    shop: sample shops have no real owner, and a test must never
    ping a real shop."""
    owner_delivered = False
    if OWNER_CHAT_ID:
        card = f"🔔 Order အသစ် (Mini App) {order_no}\n"
        if order.get("is_test"):
            card += "စမ်းသပ်အော်ဒါ (TEST) — နမူနာ ဆိုင်မှ စမ်းသပ်မှာယူခြင်း ဖြစ်ပါတယ်ရှင့်\n"
        if order.get("shop"):
            card += f"ဆိုင်: {order['shop']}\n"
        card += f"မှာသူ: {order['customer_name']}\n"
        # v19.57 (Min 2026-11-21): Min could not tell WHICH Telegram
        # account placed an order (only the phone showed). A tappable
        # link opens the customer's account so he can message them.
        # Phone orders have no chat id and get no link (the phone is
        # already in the summary below).
        if order.get("customer_chat_id"):
            card += f"👤 အကောင့်: tg://user?id={order['customer_chat_id']}\n"
        card += format_summary(order, order_no)
        if order.get("note"):
            card += f"\nမှတ်ချက်: {order['note']}"
        card += "\n\n💰 ပစ္စည်း + ပို့ခ ဈေးကို ဧည့်သည်ကို ပြန်ပို့ပေးပါ။"
        # The ↩️ button lives on the card itself so the owner can tap
        # it and reply to the customer (same cust_reply flow as the
        # receipt photo). A card with no customer chat id carries no
        # button rather than a broken callback.
        reply_markup = customer_reply_markup(order.get("customer_chat_id"))
        if reply_markup is None:
            logger.warning(
                "owner card for %s has no customer chat id — no reply button",
                order_no,
            )
        for attempt in range(2):
            try:
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID), text=card,
                    reply_markup=reply_markup,
                )
                owner_delivered = True
                break
            except Exception:
                logger.exception(
                    "owner card failed for %s (attempt %d)", order_no, attempt + 1
                )
    # v19.54 FEE-CONFIRM (Min 2026-11-20): the shop card is HELD
    # until the customer accepts the delivery fee — the fee_decision
    # handler sends it (notify_shop_of_order) on fee_ok. A test order
    # never gets a shop card anyway; a re-delivered order whose fee
    # was already accepted goes straight to the shop.
    if not order.get("is_test") and order.get("fee_status") == "accepted":
        try:
            await notify_shop_of_order(context, order, order_no)
        except Exception:
            logger.exception("shop card failed for %s", order_no)
    return owner_delivered


async def mini_app_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an order sent from the Safe Deli Mini App (WebApp data).

    The Mini App sends JSON with tg.sendData; Telegram delivers it
    as a message with web_app_data. The order is saved with the same
    SD-YYYYMMDD-### numbering, forwarded to the admin (OWNER_CHAT_ID)
    so Min can reply to the customer with the item price + delivery
    fee, and the customer is told the price is coming.
    """
    msg = update.effective_message
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    raw = ""
    if msg and msg.web_app_data:
        raw = msg.web_app_data.data or ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await msg.reply_text(order_step_text("mini_app_error", lang))
        return
    # Shop-owner registration from the Mini App uses the same
    # WebApp-data channel with a different type.
    if isinstance(data, dict) and data.get("type") == "shop_register":
        await shop_register(update, context, data, msg)
        return
    # Guide-video watch (v5): the customer asks the bot to send the
    # shop's stored video in the chat. Watching is not an order, so
    # it is not blocked by shop hours.
    if isinstance(data, dict) and data.get("type") == "request_shop_video":
        await request_shop_video(update, context, data, msg)
        return
    # v19.15 24/7 ORDERING: orders are accepted at any hour — the
    # only thing that blocks an order now is a manual /close
    # (can_order_now). Registrations above are not orders, so they
    # still go through at night too.
    if not can_order_now(update):
        await msg.reply_text(closed_text(lang=lang))
        return
    try:
        order = parse_mini_app_order(data)
    except ValueError as exc:
        await msg.reply_text(
            f"{exc}\n" + order_step_text("mini_app_retry", lang)
        )
        return
    order["customer_chat_id"] = update.effective_chat.id
    # Test orders are trusted only from the owner (2026-11-12, v11
    # follow-up): the Mini App shows sample shops to the owner only,
    # but a crafted payload must never let a customer mark a real
    # order as a test (that would dodge counts and /deliver).
    if order.get("is_test") and not _is_owner(update):
        order["is_test"] = False
    order_no, is_replay = save_order(order)
    # M-8: a replay (same submission_id) already has its confirmation
    # and owner card — send nothing a second time.
    if is_replay:
        return

    # M-5: try the owner card FIRST, then confirm the customer. The
    # order IS saved either way, so the customer is confirmed either
    # way — but the card attempt and its failure log now precede the
    # "received" claim, instead of the claim preceding a silently
    # swallowed card failure. The order number is the serial; the
    # shop name is shown when the order came from a shop's dedicated
    # page.
    await send_order_cards(context, order, order_no)
    await msg.reply_text(build_order_confirmation(order, order_no, lang))
    # v19.15 24/7: a night order (22:00–06:00) gets the auto night
    # message AFTER the confirmation — best-effort, never blocks.
    try:
        await maybe_send_night_order_message(
            context, update.effective_chat.id, order_no, lang
        )
    except Exception:
        pass


def _shop_card_needed(order):
    """True when the order's shop has a *separate* owner chat that still
    needs its own order card. v19.60: this helper was called but never
    defined, so fee_accept_job raised NameError every 15s and no
    dashboard-accepted order ever reached its shop or got its Final
    Order message. Routing mirrors notify_shop_of_order exactly: no
    registered owner, a sample shop, or the owner being Min himself
    (he already got the owner card) -> no separate shop card."""
    if not isinstance(order, dict):
        return False
    shop_name = (order.get("shop") or "").strip()
    if not shop_name:
        return False
    try:
        shop_chat_id = get_shop_owner_chat_id(shop_name)
    except Exception:
        return False
    if not shop_chat_id:
        return False
    if OWNER_CHAT_ID:
        try:
            if shop_chat_id == int(OWNER_CHAT_ID):
                return False
        except (TypeError, ValueError):
            pass
    return True




def _owner_fee_accept_text(who, order_no, fee, shop_notified, ownerless, shop_name):
    """Owner message when a customer accepts the fee (v19.62).

    Three cases, and only three:
    - the shop card really went out -> 'sent to the shop';
    - the shop is ownerless (the main shop: no registered owner, and
      Min runs it himself) -> acceptance only, NO 'contact the shop
      owner' line — there is nobody to contact;
    - an owner exists but the card send failed -> 'couldn't contact
      the owner, call the shop' (unchanged).
    """
    head = f"✅ {who} က အော်ဒါ {order_no} ရဲ့ ပို့ခ {fee:g} ဘတ် လက်ခံလိုက်ပါပြီ"
    if shop_notified:
        return head + " — ဆိုင်ကို အော်ဒါ ပို့ပေးလိုက်ပါပြီ။"
    if ownerless:
        return head + "။"
    return (
        head
        + f" — ဒါပေမယ့် ဆိုင်ရှင် ({shop_name or '?'}) ကို "
        + "ဆက်သွယ်မရပါ။ ဆိုင်ကို ဖုန်းဆက်ပြီး အကြောင်းကြားပေးပါ။"
    )

async def notify_shop_of_order(context, order, order_no, fee=None, stage=None):
    """Send a new order card to the shop as well as the owner.

    Looks the shop up by name in shop_registrations. A missing shop,
    a sample shop, or a failed Telegram send all mean 'owner only'
    and NEVER raise into the customer flow — the customer already
    got their confirmation before this runs.
    Returns the shop chat id notified, or None.
    """
    shop_name = (order.get("shop") or "").strip()
    shop_chat_id = get_shop_owner_chat_id(shop_name)
    if not shop_chat_id:
        return None
    if OWNER_CHAT_ID:
        try:
            if shop_chat_id == int(OWNER_CHAT_ID):
                return None  # owner already got the card; do not double-send
        except (TypeError, ValueError):
            pass
    card = (
        f"🔔 အော်ဒါ အသစ် {order_no} — ဆိုင်အတွက်\n"
        f"ဆိုင်: {shop_name}\n"
        f"မှာသူ: {order.get('customer_name', '')}\n"
        + format_summary(order, order_no)
    )
    if fee is not None:
        card += f"\nပို့ဆောင်ခ: {fee:g} ဘတ်"
    if stage:
        card += f"\nအခြေအနေ: {stage}"
    if order.get("note"):
        card += f"\nမှတ်ချက်: {order['note']}"
    card += (
        "\n\nအော်ဒါကို လက်ခံမလား ဆုံးဖြတ်ပေးပါနော်။\n"
        "လက်ခံရင် ပြင်ဆင်ပေးပါနော်။ ပြင်ဆင်ပြီးရင် «🍳 ပြင်ဆင်ပြီးပြီ» ကို နှိပ်ပေးပါ — အဲဒါမှ Rider လွှတ်ပေးပါမယ်။"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ လက်ခံမယ်", callback_data=f"shop_ok:{order_no}"),
        InlineKeyboardButton("❌ လက်မခံနိုင်ဘူး", callback_data=f"shop_no:{order_no}"),
    ], [
        InlineKeyboardButton("⏳ ခဏစောင့်မယ်", callback_data=f"shop_wait:{order_no}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=shop_chat_id, text=card, reply_markup=kb
        )
    except Exception:
        return None
    return shop_chat_id


# --- Server-route order pickup (2026-11-12) ------------------------------
# The Mini App posts orders to POST /api/public/orders when a Telegram
# sendData launch is unavailable (menu-button / link launches have no
# query_id). This job claims them and sends the SAME confirmations as
# a sendData order (build_order_confirmation + send_order_cards), so
# the customer can never tell which path delivered it. The bot NEVER
# re-saves a picked-up order — the API already wrote it — and a
# claimed order is never picked up twice (atomic claim: UPDATE ...
# WHERE picked_up=0 inside BEGIN IMMEDIATE; rowcount==1 wins).
# Runs only when the cutover flag is on (SAFEDELI_API_ORDERS_ENABLED=1).

def _place_from_stored(text):
    """Rebuild a place dict from a stored fmt_place string
    ('📍 https://maps.google.com/?q=lat,lon' / '🏠 <text>') so
    format_summary renders a picked-up order exactly like a fresh one."""
    if isinstance(text, str) and text.startswith("📍 "):
        url = text[2:].strip()
        match = re.search(r"[?&]q=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url)
        if match:
            return {"type": "pin", "lat": float(match.group(1)), "lon": float(match.group(2))}
        return {"type": "text", "text": url}
    if isinstance(text, str) and text.startswith("🏠 "):
        return {"type": "text", "text": text[2:]}
    return {"type": "text", "text": str(text or "")}


def claim_api_orders(conn, limit=10):
    """Atomically claim unpicked server-route orders. Call inside the
    caller's transaction (BEGIN IMMEDIATE serializes writers, so two
    pickup runs — or a pickup racing the API — can never claim the
    same order). Returns the claimed order ids, oldest first."""
    now = datetime.now(YANGON_TZ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id FROM orders WHERE source='api' AND picked_up=0 "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    claimed = []
    for (order_id,) in rows:
        cur = conn.execute(
            "UPDATE orders SET picked_up=1, picked_up_at=? "
            "WHERE id=? AND picked_up=0",
            (now, order_id),
        )
        if cur.rowcount == 1:
            claimed.append(order_id)
    return claimed


def _media_dir():
    """Directory for uploaded media (receipt photos included).
    Mirrors backend/api.py _media_dir() exactly so the bot and the
    API always agree on where a stored file lives: env
    SAFEDELI_MEDIA_DIR first, else a media/ folder next to the
    database file (DB_PATH, resolved at bot.py:87-91). bot.py had no
    media-dir logic of its own before the receipt-photo flow, so
    this is the API's logic copied here, not a reuse of an existing
    bot.py helper."""
    raw = (os.environ.get("SAFEDELI_MEDIA_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(DB_PATH).parent / "media"


_RECEIPT_PHOTO_COLUMNS = (
    "id", "order_no", "customer_name", "customer_chat_id",
    "sender_phone", "shop", "item", "receipt_photo_path",
)


def claim_receipt_photos(conn, limit=10):
    """Atomically claim server-route orders whose receipt photo is
    waiting to be sent to the owner. Mirrors claim_api_orders: the
    claim runs inside the caller's transaction (BEGIN IMMEDIATE
    serializes writers), and UPDATE ... WHERE receipt_photo_sent=0
    with rowcount==1 wins (2 = claimed/sending). Only
    picked_up=1 rows are claimed, so the receipt only goes out
    AFTER the order card has been delivered — the owner always
    sees the card first, the photo second. Returns the claimed
    orders as dicts, oldest first."""
    rows = conn.execute(
        "SELECT " + ",".join(_RECEIPT_PHOTO_COLUMNS) + " FROM orders "
        "WHERE source='api' AND picked_up=1 "
        "AND receipt_photo_path IS NOT NULL AND receipt_photo_sent=0 "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    claimed = []
    for row in rows:
        order = _dict_row(row, _RECEIPT_PHOTO_COLUMNS)
        cur = conn.execute(
            "UPDATE orders SET receipt_photo_sent=2 "
            "WHERE id=? AND receipt_photo_sent=0",
            (order["id"],),
        )
        if cur.rowcount == 1:
            claimed.append(order)
    return claimed


def _unclaim_receipt_photo(conn, order_id):
    """Release a receipt claim so the next tick retries it. Used
    whenever the photo failed to send — a claimed receipt that
    fails must come back, never vanish silently (audit M-4
    lesson, same pattern as _unclaim_api_order)."""
    conn.execute(
        "UPDATE orders SET receipt_photo_sent=0 WHERE id=?",
        (order_id,),
    )
    conn.commit()


async def deliver_receipt_photo(context, conn, order):
    """Send one claimed receipt photo to the owner. The stored file
    is uploaded with python-telegram-bot's InputFile (an open file
    object) — the one call in this flow with no precedent in this
    codebase (every send_photo today passes a Telegram file_id);
    staging test T-0 proves it. The file handle is closed by the
    with-block either way. The caption carries the order number
    first so a photo arriving after its card is still matchable,
    and the ↩️ button reuses the existing cust_reply flow
    (begin_customer_reply) — no new accept/reject buttons.
    On success the returned file_id is stored in receipt_file_id
    and receipt_photo_sent=1; on any failure the claim is released
    for the next tick."""
    order_no = order.get("order_no") or ""
    order_id = order.get("id")
    receipt_path = order.get("receipt_photo_path") or ""
    if not receipt_path:
        logger.warning(
            "server-route pickup: receipt claimed with no path for %s", order_no
        )
        _unclaim_receipt_photo(conn, order_id)
        return
    if not OWNER_CHAT_ID:
        logger.warning(
            "server-route pickup: cannot send receipt for %s — OWNER_CHAT_ID unset",
            order_no,
        )
        _unclaim_receipt_photo(conn, order_id)
        return
    media_path = _media_dir() / receipt_path
    # Same ↩️ button as the order card; a receipt with no customer
    # chat id goes out without a button rather than a dead
    # 'cust_reply:None' callback no handler would match.
    reply_markup = customer_reply_markup(order.get("customer_chat_id"))
    if reply_markup is None:
        logger.warning(
            "server-route pickup: receipt for %s has no customer chat id — no reply button",
            order_no,
        )
    try:
        with open(media_path, "rb") as f:
            msg = await context.bot.send_photo(
                chat_id=int(OWNER_CHAT_ID),
                photo=f,
                caption=(
                    f"🧾 ပြေစာဓာတ်ပုံ — {order_no}\n"
                    f"ဆိုင်: {order.get('shop') or ''}\n"
                    f"မှာသူ: {order.get('customer_name') or ''} ({order.get('sender_phone') or ''})\n"
                    f"{order.get('item') or ''}"
                ),
                reply_markup=reply_markup,
            )
    except Exception:
        logger.exception(
            "server-route pickup: receipt photo failed for %s", order_no
        )
        _unclaim_receipt_photo(conn, order_id)
        return
    file_id = None
    try:
        photos = msg.photo
        if photos:
            file_id = photos[-1].file_id
    except Exception:
        pass
    conn.execute(
        "UPDATE orders SET receipt_photo_sent=1, receipt_file_id=? WHERE id=?",
        (file_id, order_id),
    )
    conn.commit()


async def _run_receipt_photo_phase(context, conn):
    """Receipt phase of the pickup job: claim and deliver waiting
    receipt photos. Separate from the card claim, so a stuck
    receipt can never block order cards. Each order is wrapped in
    its own try/except so one bad receipt never kills the batch.
    Tolerates a DB without the receipt columns yet (the API adds
    them on its next order request after deploy): an
    OperationalError logs once and skips the tick."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            claimed = claim_receipt_photos(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except sqlite3.OperationalError:
        logger.warning(
            "server-route pickup: receipt phase skipped — "
            "orders table has no receipt columns yet"
        )
        return
    except Exception:
        logger.exception("server-route pickup: receipt claim failed")
        return
    for order in claimed:
        try:
            await deliver_receipt_photo(context, conn, order)
        except Exception:
            logger.exception(
                "server-route pickup: receipt delivery failed for %s",
                order.get("order_no") or "",
            )


_API_ORDER_COLUMNS = (
    "id", "order_no", "customer_name", "customer_chat_id",
    "sender_phone", "pickup", "dropoff", "receiver_phone", "item",
    "shop", "is_test", "note", "customer_location", "idempotency_key",
    "items_json", "telegram_username",
)


def load_api_order(conn, order_id):
    """Load a claimed API order as the order dict mini_app_order
    works with (place dicts, customer_location dict, is_test bool).
    Returns None when the row is gone. db_connect() sets no
    row_factory, so rows are mapped with _dict_row (existing helper)."""
    try:
        row = conn.execute(
            "SELECT " + ",".join(_API_ORDER_COLUMNS) + " FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        data = _dict_row(row, _API_ORDER_COLUMNS)
    except sqlite3.OperationalError:
        # Old DB without the v19.59 columns (the API migration has not
        # run yet): retry without them rather than dropping the order.
        _base = [c for c in _API_ORDER_COLUMNS if c not in ("items_json", "telegram_username")]
        row = conn.execute(
            "SELECT " + ",".join(_base) + " FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        data = _dict_row(row, _base)
        if data is not None:
            data["items_json"] = None
            data["telegram_username"] = None
    if data is None:
        return None
    customer_location = None
    raw_loc = data.get("customer_location")
    if isinstance(raw_loc, str) and raw_loc:
        try:
            customer_location = json.loads(raw_loc)
        except ValueError:
            customer_location = None
    return {
        "order_no": data["order_no"],
        "customer_name": data["customer_name"] or "",
        "customer_chat_id": data["customer_chat_id"],
        "sender_phone": data["sender_phone"] or "",
        "pickup": _place_from_stored(data["pickup"]),
        "dropoff": _place_from_stored(data["dropoff"]),
        "receiver_phone": data["receiver_phone"] or data["sender_phone"] or "",
        "item": data["item"] or "",
        "shop": data["shop"] or "",
        "is_test": bool(data["is_test"]),
        "note": data["note"] or "",
        "customer_location": customer_location,
        "source": "api",
        "idempotency_key": data["idempotency_key"],
        "items_json": data.get("items_json"),
        "telegram_username": data.get("telegram_username") or "",
    }


# M-4 (2026-11-15): a claimed order that fails to load must not stay
# claimed forever, and Min must be told. The pickup job ticks every
# 15s, so owner notices are rate-limited to one per order per hour —
# a persistently failing order must not spam Min. Bounded at ~200
# entries (oldest dropped first); the dict resets on restart, which
# is acceptable (same precedent as the API's in-memory rate limits).
_M4_OWNER_NOTICE_LAST = {}
_M4_OWNER_NOTICE_MAX = 200
_M4_OWNER_NOTICE_INTERVAL = 3600.0


def _m4_owner_notice_due(order_id):
    """True once per order per hour. Records this attempt, so even a
    failing owner send cannot spam Min every 15s."""
    now = time.monotonic()
    last = _M4_OWNER_NOTICE_LAST.get(order_id)
    if last is not None and now - last < _M4_OWNER_NOTICE_INTERVAL:
        return False
    _M4_OWNER_NOTICE_LAST[order_id] = now
    while len(_M4_OWNER_NOTICE_LAST) > _M4_OWNER_NOTICE_MAX:
        _M4_OWNER_NOTICE_LAST.pop(next(iter(_M4_OWNER_NOTICE_LAST)), None)
    return True


async def _notify_owner_pickup_failure(context, order_id, order_no, reason):
    """Tell Min a claimed order was put back for retry. Never raises
    into the pickup job. reason is "load" (load_api_order raised) or
    "no_customer" (the order has no customer chat id)."""
    if not OWNER_CHAT_ID:
        return
    if not _m4_owner_notice_due(order_id):
        return
    if reason == "no_customer":
        text = (
            f"⚠️ Order {order_no} မှာ customer chat မပါလို့ "
            "ပြန်တင်ထားပါတယ်ရှင့် — ထပ်စမ်းပါမယ်။"
        )
    else:
        text = (
            f"⚠️ Order {order_no} ကို ဖတ်လို့ မရလို့ "
            "ပြန်တင်ထားပါတယ်ရှင့် — ထပ်စမ်းပါမယ်။"
        )
    try:
        await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
    except Exception:
        logger.exception(
            "server-route pickup: owner notice failed for %s", order_no
        )


def _unclaim_quietly(conn, order_id, order_no):
    """Release a claim without ever killing the batch: a failing
    unclaim is logged, never raised."""
    try:
        _unclaim_api_order(conn, order_id)
    except Exception:
        logger.exception("server-route pickup: unclaim failed for %s", order_no)


def _unclaim_api_order(conn, order_id):
    """Release a claim so the next tick retries the order. Used when
    the customer confirmation itself failed to send — the customer
    got nothing yet, so a retry cannot double-notify them — and,
    since M-4 (2026-11-15), when a claimed order fails to load or
    has no customer chat id."""
    conn.execute(
        "UPDATE orders SET picked_up=0, picked_up_at=NULL WHERE id=?",
        (order_id,),
    )
    conn.commit()


async def _deliver_api_order(context, conn, order_id, order, order_no):
    """Send one claimed API order's confirmations. v19.15 24/7
    ORDERING — the hours re-check is GONE: an already-accepted
    night order must NOT be cancelled here as "closed" (ordering is
    24/7; the 06:00–22:00 window is the delivery window). Only a
    live manual /close still cancels an order at pickup — the
    customer gets the closed message and the order is marked closed,
    never left silently unconfirmed.

    M-5 (2026-11-15): confirmation-first is load-bearing here — a
    failed confirmation unclaims for retry; reordering would risk
    double-confirming the customer. So the fix here is only
    send_order_cards' loud logging, not a reorder."""
    customer_chat_id = order.get("customer_chat_id")
    # v19.55 PHONE ORDERS (Min's decision): a browser order has no
    # Telegram chat id — that is legitimate, not a failure. It gets the
    # owner card (and the gated shop card) but no customer Telegram
    # message; the customer follows it in the app (My Orders by phone)
    # and accepts the fee there.
    is_phone_order = not customer_chat_id
    # is_test is trusted only from the owner — the API enforces this
    # at intake, and the pickup enforces it again (defence in depth).
    if order.get("is_test") and not (
        bool(OWNER_CHAT_ID) and str(customer_chat_id) == str(OWNER_CHAT_ID)
    ):
        order["is_test"] = False
    # v19.15 CRITICAL: no hours re-check here any more — only a live
    # manual /close cancels an already-accepted order at pickup.
    if is_shop_closed_manual():
        if customer_chat_id:
            lang = get_user_lang(customer_chat_id) or DEFAULT_LANG
            try:
                await context.bot.send_message(
                    chat_id=customer_chat_id, text=closed_text(lang=lang)
                )
            except Exception:
                pass
        try:
            conn.execute(
                "UPDATE orders SET status='closed', order_status='cancelled' WHERE id=?",
                (order_id,),
            )
        except sqlite3.OperationalError:
            conn.execute("UPDATE orders SET status='closed' WHERE id=?", (order_id,))
        conn.commit()
        return
    if not is_phone_order:
        lang = get_user_lang(customer_chat_id) or DEFAULT_LANG
        try:
            await context.bot.send_message(
                chat_id=customer_chat_id,
                text=build_order_confirmation(order, order_no, lang),
            )
        except Exception:
            logger.exception("server-route pickup: customer confirmation failed for %s", order_no)
            _unclaim_api_order(conn, order_id)
            return
        # v19.15 24/7: a night order (22:00–06:00) gets the auto night
        # message AFTER the confirmation — best-effort, never blocks.
        try:
            await maybe_send_night_order_message(
                context, customer_chat_id, order_no, lang
            )
        except Exception:
            pass
    await send_order_cards(context, order, order_no)


STATUS_NOTIFY_TEXT = {
    # One short message per dashboard-driven status. my + en are final;
    # th + zh are DRAFTS for Min to check. The 'waiting' lines
    # (2026-11-24) are DRAFTS in all 4 languages for Min's approval.
    "my": {
        "confirmed": "✅ ဆိုင်က အော်ဒါ {order_no} ကို လက်ခံလိုက်ပါပြီရှင့်။",
        "preparing": "👨‍🍳 အော်ဒါ {order_no} ကို ပြင်ဆင်နေပါပြီရှင့်။",
        "delivering": "🚚 အော်ဒါ {order_no} ကို ပို့ဆောင်နေပါပြီရှင့်။",
        "delivered": "✅ အော်ဒါ {order_no} ပို့ဆောင်ပြီးပါပြီရှင့်။ ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏",
        "cancelled": "❌ အော်ဒါ {order_no} ကို cancel လုပ်လိုက်ပါပြီရှင့်။ Admin က ဆက်သွယ်ပေးပါမယ်ရှင့်။",
        "waiting": "⏳ အော်ဒါ {order_no} ကို စစ်ဆေးနေပါတယ်ရှင့်။ ခဏလေး စောင့်ပေးပါဦးရှင့်။ 🙏",
    },
    "en": {
        "confirmed": "✅ The shop has accepted your order {order_no}.",
        "preparing": "👨‍🍳 Your order {order_no} is being prepared.",
        "delivering": "🚚 Your order {order_no} is on its way.",
        "delivered": "✅ Your order {order_no} has been delivered. Thank you! 🙏",
        "cancelled": "❌ Your order {order_no} has been cancelled. The admin will contact you.",
        "waiting": "⏳ We're checking your order {order_no}. Please wait a moment. 🙏",
    },
    "th": {
        "confirmed": "✅ ร้านรับออเดอร์ {order_no} แล้วค่ะ",
        "preparing": "👨‍🍳 กำลังเตรียมออเดอร์ {order_no} ค่ะ",
        "delivering": "🚚 ออเดอร์ {order_no} กำลังจัดส่งค่ะ",
        "delivered": "✅ จัดส่งออเดอร์ {order_no} เรียบร้อยแล้วค่ะ ขอบคุณค่ะ 🙏",
        "cancelled": "❌ ออเดอร์ {order_no} ถูกยกเลิกแล้วค่ะ แอดมินจะติดต่อกลับไปค่ะ",
        "waiting": "⏳ กำลังตรวจสอบออเดอร์ {order_no} ค่ะ กรุณารอสักครู่นะคะ 🙏",
    },
    "zh": {
        "confirmed": "✅ 商家已接受您的订单 {order_no}。",
        "preparing": "👨‍🍳 正在准备您的订单 {order_no}。",
        "delivering": "🚚 您的订单 {order_no} 正在配送中。",
        "delivered": "✅ 您的订单 {order_no} 已送达，谢谢！🙏",
        "cancelled": "❌ 您的订单 {order_no} 已取消，管理员会联系您。",
        "waiting": "⏳ 正在确认您的订单 {order_no}，请稍等。🙏",
    },
}
DELIVERED_THANKS_TEXT = {
    # Closing blessing + thanks sent after the delivered notice (Min
    # 2026-11-23, verbatim Burmese). th + zh are DRAFTS for Min to check.
    "my": "နေ့ရက်တိုင်း ပျော်ရွှင်မှုတွေနဲ့ ပြည့်နှက်နေပါစေ။ 💕\n\nနောက်တစ်ကြိမ်လည်း ပြန်လည်ဝန်ဆောင်မှုပေးခွင့်ရပါစေ။ အားပေးမှုအတွက် အထူးကျေးဇူးတင်ပါတယ်ရှင့်။ 😍💜",
    "en": "May every day be filled with happiness. 💕\n\nWe hope to have the chance to serve you again. Thank you so much for your support. 😍💜",
    "th": "ขอให้ทุกวันเต็มไปด้วยความสุขนะคะ 💕\n\nหวังว่าจะได้มีโอกาสให้บริการอีกครั้งนะคะ ขอบคุณมากๆ สำหรับการสนับสนุนค่ะ 😍💜",
    "zh": "愿每一天都充满快乐。💕\n\n希望能再次为您服务。非常感谢您的支持。😍💜",
}

STATUS_PRICE_LABELS = {
    # Items / fee / total lines for the delivering notice (Min
    # 2026-11-23): the customer pays cash on delivery, so the
    # reminder restates what is owed. my + en final; th + zh DRAFTS.
    "my": ("ပစ္စည်းဖိုး", "ပို့ဆောင်ခ", "စုစုပေါင်း"),
    "en": ("Items", "Delivery fee", "Total"),
    "th": ("ค่าสินค้า", "ค่าส่ง", "รวมทั้งหมด"),
    "zh": ("商品价格", "配送费", "总计"),
}
STATUS_NOTIFY_MAX_AGE_HOURS = 24

RIDER_STATUS_TEXT = {
    # Short Burmese status notes for the assigned rider (staff
    # language, not customer language). The rider only hears about a
    # dashboard change when the order has an active delivery.
    "confirmed": "✅ အော်ဒါ {order_no} ကို ဆိုင်က လက်ခံလိုက်ပါပြီ။",
    "preparing": "👨‍🍳 အော်ဒါ {order_no} ကို ပြင်ဆင်နေပါပြီ။",
    "delivering": "🚚 အော်ဒါ {order_no} ကို ပို့ဆောင်နေပါပြီ။",
    "delivered": "✅ အော်ဒါ {order_no} ပို့ဆောင်ပြီးပါပြီ။",
    "cancelled": "❌ အော်ဒါ {order_no} ကို cancel လုပ်လိုက်ပါပြီ။",
}


async def _tell_rider_status(context, rider_chat_id, customer_chat_id, status, order_no):
    """Best-effort rider status note. Skipped when the rider IS the
    customer or Min (one person, one message). Never raises."""
    if not rider_chat_id:
        return
    try:
        if customer_chat_id and int(rider_chat_id) == int(customer_chat_id):
            return
        if OWNER_CHAT_ID and int(rider_chat_id) == int(OWNER_CHAT_ID):
            return
    except (TypeError, ValueError):
        return
    text = RIDER_STATUS_TEXT.get(status)
    if not text:
        return
    try:
        await context.bot.send_message(
            chat_id=int(rider_chat_id), text=text.format(order_no=order_no)
        )
    except Exception:
        pass


def build_status_notification(order_no, status, lang):
    """Text for one dashboard-driven status change, or None when the
    status has no customer message (e.g. 'pending' — the initial
    state, never a dashboard change)."""
    texts = STATUS_NOTIFY_TEXT.get(lang) or STATUS_NOTIFY_TEXT.get(DEFAULT_LANG) or {}
    template = texts.get(status)
    if template is None:
        return None
    return template.format(order_no=order_no)


def _delivering_price_lines(conn, order_id, lang):
    """Items/fee/total reminder appended to the 'delivering'
    notice only (Min 2026-11-23). Returns None when no honest
    total exists (priceless items, or the fee not set yet) — a
    guessed total must never reach the customer."""
    try:
        order = load_api_order(conn, order_id)
    except Exception:
        return None
    if not isinstance(order, dict):
        return None
    _item_lines, items_total, _priceless = _voucher_items(order)
    try:
        _frow = conn.execute(
            "SELECT delivery_fee FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        fee = _frow[0] if _frow else None
    except Exception:
        fee = None
    if items_total is None or fee is None:
        return None
    labels = STATUS_PRICE_LABELS.get(lang) or STATUS_PRICE_LABELS.get(DEFAULT_LANG)
    return "\n".join(
        [
            f"{labels[0]}: {items_total:g} ဘတ်",
            f"{labels[1]}: {fee:g} ဘတ်",
            f"{labels[2]}: {items_total + fee:g} ဘတ်",
        ]
    )


def _parse_event_time(value):
    """Parse an order_status_events.created_at value (ISO, with or
    without an offset) into an aware datetime, or None when it cannot
    be read — callers treat None as fresh (send, don't drop)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def claim_status_events(conn, limit=10):
    """Atomically claim unnotified dashboard status events. Mirrors
    claim_api_orders: the claim runs inside the caller's transaction
    (BEGIN IMMEDIATE serializes writers), and INSERT OR IGNORE with
    rowcount==1 wins. Only actor_type='admin' rows are claimed — the
    bot's own card flows message the customer directly and never write
    order_status_events. Returns the claimed event ids, oldest first.

    The marker lives in a SEPARATE table (status_notify_claims), NOT
    in order_status_events.notified_at: that table is append-only (a
    trigger blocks every UPDATE — migration 002), so the old
    UPDATE-based claim always failed and no status message was ever
    sent. The claims table is the only write here; the history table
    is never touched."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS status_notify_claims ("
        "event_id INTEGER PRIMARY KEY, notified_at TEXT NOT NULL)"
    )
    now = datetime.now(YANGON_TZ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id FROM order_status_events "
        "WHERE actor_type='admin' AND id NOT IN (SELECT event_id FROM status_notify_claims) "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    claimed = []
    for (event_id,) in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO status_notify_claims (event_id, notified_at) VALUES (?, ?)",
            (event_id, now),
        )
        if cur.rowcount == 1:
            claimed.append(event_id)
    return claimed


def _unclaim_status_event(conn, event_id):
    """Release one claimed event so the next tick retries it (send
    failed). The caller logs failures; this only runs after a send
    raised, so a commit failure here must not mask that. The release
    deletes the claim row — the history table is append-only and is
    never written."""
    conn.execute(
        "DELETE FROM status_notify_claims WHERE event_id=?",
        (event_id,),
    )
    conn.commit()


async def notify_status_changes_job(context):
    """JobQueue notify (every 15s): send the customer one short message
    for each new dashboard-driven status change. One bad event never
    kills the batch, and the job itself never raises into the
    scheduler. A failed send releases the claim so the next tick
    retries it; events older than 24h are left claimed (a status
    message a day late is useless)."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("status notify: cannot open database")
        return
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = claim_status_events(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except Exception:
            logger.exception("status notify: claim failed")
            return
        for event_id in claimed:
            try:
                row = conn.execute(
                    "SELECT order_id, status, created_at FROM order_status_events "
                    "WHERE id=?",
                    (event_id,),
                ).fetchone()
            except Exception:
                logger.exception("status notify: cannot load event %s", event_id)
                continue
            if row is None:
                logger.warning("status notify: claimed event id %s is gone — skipping", event_id)
                continue
            order_id, status, created_at = row[0], row[1], row[2]
            try:
                order_row = conn.execute(
                    "SELECT order_no, customer_chat_id FROM orders WHERE id=?",
                    (order_id,),
                ).fetchone()
            except Exception:
                logger.exception("status notify: cannot load order %s", order_id)
                continue
            if order_row is None:
                logger.warning(
                    "status notify: event %s has no order %s — skipping", event_id, order_id
                )
                continue
            order_no, customer_chat_id = order_row[0], order_row[1]
            # Stale first: a status message a day late is useless, for
            # the customer, the rider and Min alike.
            event_time = _parse_event_time(created_at)
            too_old = event_time is not None and (
                datetime.now(timezone.utc) - event_time
            ).total_seconds() > STATUS_NOTIFY_MAX_AGE_HOURS * 3600
            if too_old:
                logger.info(
                    "status notify: event %s is older than %sh — skipping",
                    event_id, STATUS_NOTIFY_MAX_AGE_HOURS,
                )
                continue
            # v19.57 (Min 2026-11-21): a dashboard rejection told
            # NOBODY — not the customer, not Min's Telegram. The
            # customer is told below (Telegram) or sees it in the app
            # (phone orders). Min gets a record here for every
            # cancelled dashboard event, so he can follow up.
            if status == "cancelled" and OWNER_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=int(OWNER_CHAT_ID),
                        text=f"❌ အော်ဒါ {order_no} ကို dashboard ကနေ ပယ်ဖျက်လိုက်ပါပြီ။",
                    )
                except Exception:
                    pass
            # Ready convergence (v19.79): a move to `delivering`
            # from the dashboard (merchant/owner Ready) must call
            # the rider exactly like the Telegram Ready tap — this
            # job is the only process that sees dashboard events.
            # Best-effort: never blocks the customer message.
            # Skipped for test orders, rejected/closed orders, and
            # orders that already have an active delivery (the guard
            # inside create_and_assign_delivery makes retries safe).
            if status == "delivering":
                try:
                    _orow = conn.execute(
                        "SELECT COALESCE(is_test,0), COALESCE(status,'new') FROM orders WHERE id=?",
                        (order_id,),
                    ).fetchone()
                except Exception:
                    _orow = None
                if (_orow is not None and not _orow[0]
                        and _orow[1] not in ("shop_rejected", "closed")
                        and not get_active_delivery_for_order(order_id)):
                    try:
                        await maybe_auto_assign_delivery(context, order_no)
                    except Exception:
                        pass
            # Rider lookup (v19.57, Min 2026-11-21): a dashboard
            # status change never reached the rider — they only heard
            # from the job card. The order's active delivery (if any)
            # names the rider; they are told after the customer send
            # succeeds (or at once for a phone order, which has no
            # customer chat to message).
            rider_chat_id = None
            try:
                drow = conn.execute(
                    "SELECT rider_chat_id FROM deliveries WHERE order_id=? "
                    "AND status NOT IN ('delivered','failed','cancelled') "
                    "ORDER BY id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
                if drow and drow[0]:
                    rider_chat_id = int(drow[0])
            except Exception:
                rider_chat_id = None
            if not customer_chat_id:
                logger.warning(
                    "status notify: order %s has no customer chat id — skipping", order_no
                )
                await _tell_rider_status(context, rider_chat_id, None, status, order_no)
                continue
            text = build_status_notification(
                order_no, status, get_user_lang(customer_chat_id) or DEFAULT_LANG
            )
            if text is None:
                logger.info(
                    "status notify: event %s status %r has no message — skipping",
                    event_id, status,
                )
                continue
            if status == "delivering":
                _prices = _delivering_price_lines(
                    conn, order_id, get_user_lang(customer_chat_id) or DEFAULT_LANG
                )
                if _prices:
                    text = text + "\n" + _prices
            customer_sent = False
            try:
                await context.bot.send_message(chat_id=int(customer_chat_id), text=text)
                customer_sent = True
            except Exception:
                logger.exception("status notify: send failed for %s", order_no)
                try:
                    _unclaim_status_event(conn, event_id)
                except Exception:
                    logger.exception("status notify: unclaim failed for event %s", event_id)
            if customer_sent:
                if status == "delivered":
                    _thanks = DELIVERED_THANKS_TEXT.get(
                        get_user_lang(customer_chat_id) or DEFAULT_LANG
                    ) or DELIVERED_THANKS_TEXT.get(DEFAULT_LANG)
                    if _thanks:
                        try:
                            await context.bot.send_message(
                                chat_id=int(customer_chat_id), text=_thanks
                            )
                        except Exception:
                            logger.exception(
                                "status notify: thanks send failed for %s", order_no
                            )
                await _tell_rider_status(context, rider_chat_id, customer_chat_id, status, order_no)
    finally:
        conn.close()


# --- Service-booking pickup (v19.47, Min 2026-11-20) --------------------
# The Mini App POSTs service bookings (school run, bus, airport,
# hospital, shopping errands, documents, food, products) to the API
# instead of sendData, so a booking can never be lost to a closed
# WebApp. This job claims them and sends Min ONE card per booking in
# the owner chat, mirroring the server-route order pickup: atomic
# claim (UPDATE ... WHERE picked_up=0, rowcount==1 wins) inside
# BEGIN IMMEDIATE, one bad booking never kills the batch, and the job
# itself never raises into the scheduler. NOT gated by the orders
# flag — bookings are not orders (same as the status-notify job).
_SERVICE_BOOKING_COLUMNS = (
    "id", "booking_id", "service", "service_name", "customer_name",
    "customer_phone", "customer_chat_id", "fields_json", "created_at",
)
_SERVICE_BOOKING_LABELS = {
    "child": "ကလေး အမည်", "school": "ကျောင်း", "date": "နေ့",
    "time": "အချိန်", "place": "နေရာ", "note": "မှတ်ချက်",
    "direction": "ဦးတည်ရာ", "shop": "ဆိုင်", "item": "ပစ္စည်း",
    "people": "လူဦးရေ", "doctype": "စာရွက်စာတမ်း အမျိုးအစား",
    "flight": "လေယာဉ် / လေဆိပ်", "hospital": "ဆေးရုံ",
    "bus": "ကားဂိတ်", "market": "ဈေး / ဆိုင်",
    "passenger": "ခရီးသည် အမည်", "patient": "လူနာ အမည်",
    "pickup_place": "ကြိုရမည့်နေရာ", "drop_place": "ပို့ရမည့်နေရာ",
}


def claim_service_bookings(conn, limit=10):
    """Atomically claim unpicked service bookings. Call inside the
    caller's transaction (BEGIN IMMEDIATE serializes writers).
    Returns the claimed row ids, oldest first. The table is created
    lazily here too, so the job heals itself on a fresh DB."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS service_bookings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT NOT NULL UNIQUE, "
        "service TEXT NOT NULL, service_name TEXT NOT NULL, "
        "customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, "
        "fields_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new', "
        "picked_up INTEGER NOT NULL DEFAULT 0, picked_up_at TEXT, "
        "created_at TEXT NOT NULL)"
    )
    now = datetime.now(YANGON_TZ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id FROM service_bookings WHERE picked_up=0 "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    claimed = []
    for (booking_id,) in rows:
        cur = conn.execute(
            "UPDATE service_bookings SET picked_up=1, picked_up_at=? "
            "WHERE id=? AND picked_up=0",
            (now, booking_id),
        )
        if cur.rowcount == 1:
            claimed.append(booking_id)
    return claimed


def _service_booking_card(row):
    """One owner-chat card for a claimed booking row (dict). Fields
    the header already shows (name / phone) are skipped in the list;
    every other field is shown with its Burmese label."""
    fields = {}
    try:
        fields = json.loads(row.get("fields_json") or "{}")
    except ValueError:
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    lines = [
        "🛎️ ဝန်ဆောင်မှု ဘွတ်ကင် အသစ်",
        f"{row.get('service_name') or row.get('service') or ''} · {row.get('booking_id') or ''}",
        f"👤 {row.get('customer_name') or ''}",
        f"📞 {row.get('customer_phone') or ''}",
    ]
    for key in ("date", "time", "place", "direction", "child", "school",
                "shop", "item", "people", "doctype", "flight", "hospital",
                "bus", "market", "passenger", "patient", "pickup_place",
                "drop_place", "note"):
        val = fields.get(key)
        if val in (None, ""):
            continue
        lines.append(f"{_SERVICE_BOOKING_LABELS.get(key, key)}: {val}")
    for key, val in fields.items():
        if key in ("cname", "cphone", "date", "time", "place", "direction",
                   "child", "school", "shop", "item", "people", "doctype",
                   "flight", "hospital", "bus", "market", "passenger",
                   "patient", "pickup_place", "drop_place", "note"):
            continue
        if val in (None, ""):
            continue
        lines.append(f"{key}: {val}")
    lines.append("ဈေးနှုန်း — ဖုန်းဆက်ပြီး အတည်ပြုပေးပါနော်။")
    return "\n".join(lines)


async def pickup_service_bookings_job(context):
    """JobQueue pickup (every 15s): claim new service bookings and
    send Min one card per booking in the owner chat."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("service-booking pickup: cannot open database")
        return
    try:
        # v19.53: the booking card carries a ↩️ reply button, which
        # needs the customer chat id (stored by the API as
        # customer_chat_id). Ensure the column here too so the bot
        # never depends on the API having run its migration first.
        _sb_cols = {r[1] for r in conn.execute("PRAGMA table_info(service_bookings)")}
        if "customer_chat_id" not in _sb_cols:
            conn.execute("ALTER TABLE service_bookings ADD COLUMN customer_chat_id INTEGER")
            conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = claim_service_bookings(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except Exception:
            logger.exception("service-booking pickup: claim failed")
            return
        for booking_id in claimed:
            try:
                row = conn.execute(
                    "SELECT " + ", ".join(_SERVICE_BOOKING_COLUMNS)
                    + " FROM service_bookings WHERE id=?",
                    (booking_id,),
                ).fetchone()
                if row is None:
                    continue
                data = dict(zip(_SERVICE_BOOKING_COLUMNS, row))
                if not OWNER_CHAT_ID:
                    logger.warning(
                        "service-booking pickup: OWNER_CHAT_ID is not set; "
                        "booking %s stays claimed but unsent",
                        data.get("booking_id"),
                    )
                    continue
                # The ↩️ button lives on the card itself so the owner
                # can tap it and reply to the customer (same
                # cust_reply flow as the order card). A card with no
                # customer chat id carries no button rather than a
                # broken callback.
                _bk_reply = customer_reply_markup(data.get("customer_chat_id"))
                if _bk_reply is None:
                    logger.warning(
                        "service-booking card for %s has no customer chat id — no reply button",
                        data.get("booking_id"),
                    )
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID),
                    text=_service_booking_card(data),
                    reply_markup=_bk_reply,
                )
            except Exception:
                logger.exception(
                    "service-booking pickup: delivery failed for id %s",
                    booking_id,
                )
    finally:
        conn.close()


async def pickup_api_orders_job(context):
    """JobQueue pickup (every 15s): claim unpicked server-route orders
    and deliver their confirmations. One bad order never kills the
    batch, and the job itself never raises into the scheduler."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("server-route pickup: cannot open database")
        return
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = claim_api_orders(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except Exception:
            logger.exception("server-route pickup: claim failed")
            return
        for order_id in claimed:
            try:
                order = load_api_order(conn, order_id)
            except Exception:
                logger.exception("server-route pickup: cannot load order %s", order_id)
                # M-4: release the claim so the next tick retries it,
                # and tell Min (rate-limited) instead of losing the
                # order silently. The order number is looked up for
                # the notice — the load itself is what failed.
                _unclaim_quietly(conn, order_id, f"#{order_id}")
                order_no = None
                try:
                    row = conn.execute(
                        "SELECT order_no FROM orders WHERE id=?", (order_id,)
                    ).fetchone()
                    order_no = row[0] if row else None
                except Exception:
                    logger.exception(
                        "server-route pickup: order_no lookup failed for %s",
                        order_id,
                    )
                await _notify_owner_pickup_failure(
                    context, order_id, order_no or f"#{order_id}", "load"
                )
                continue
            if order is None:
                # Row gone between claim and load: nothing to unclaim.
                logger.warning(
                    "server-route pickup: claimed order id %s is gone — skipping",
                    order_id,
                )
                continue
            try:
                await _deliver_api_order(context, conn, order_id, order, order["order_no"])
            except Exception:
                logger.exception("server-route pickup: delivery failed for %s", order["order_no"])
        # Receipt phase: card phase first, then receipts, so the
        # owner always sees the card before its photo. Separate
        # claim, so a stuck receipt can never block order cards.
        await _run_receipt_photo_phase(context, conn)
    finally:
        conn.close()


async def request_shop_video(update, context, data, msg):
    """Send a shop's stored guide video to the customer in the bot
    chat (2026-11-11, v5). The video plays in Telegram's native
    player — no other app, no external link.

    Looks the video up by shop name, falling back to the numeric
    shop id. When no video is stored, the customer gets a polite
    reply and Min gets a short forward so he can follow up. A failed
    send is reported the same way, distinguished in Min's note.
    """
    shop_name = str(data.get("shop_name") or data.get("shop") or "").strip()
    shop_id = data.get("shop_id") or data.get("shopId")
    video = get_shop_video(shop_name, shop_id)
    customer_chat_id = update.effective_chat.id
    if video is None:
        await msg.reply_text(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏\n"
            "ဒီဆိုင်ရဲ့ လမ်းညွှန် ဗီဒီယိုက မရှိသေးပါဘူး။\n"
            "Admin က ဆက်သွယ်ပြီး ကူညီပေးပါမယ်ရှင့်။"
        )
        if OWNER_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID),
                    text=(
                        "🎬 ဗီဒီယို တောင်းခံခြင်း — မရှိသေးပါ\n"
                        f"ဆိုင်: {shop_name or shop_id or '-'}\n"
                        f"Customer chat: {customer_chat_id}"
                    ),
                )
            except Exception:
                pass
        return
    file_id, duration, found_name = video
    try:
        await context.bot.send_video(
            chat_id=customer_chat_id,
            video=file_id,
            caption=f"🎬 {found_name} ဆိုင်လမ်းညွှန် ဗီဒီယိုပါရှင့်။",
        )
    except Exception:
        await msg.reply_text(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏\n"
            "လမ်းညွှန် ဗီဒီယို ပို့လို့ မရသေးပါဘူး။\n"
            "Admin က ဆက်သွယ်ပြီး ကူညီပေးပါမယ်ရှင့်။"
        )
        if OWNER_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID),
                    text=(
                        "🎬 ဗီဒီယို တောင်းခံခြင်း — ပို့မရပါ\n"
                        f"ဆိုင်: {found_name}\n"
                        f"Customer chat: {customer_chat_id}"
                    ),
                )
            except Exception:
                pass


def _audit_shop_decision(shop_name, order_no, decision):
    """Write the shop's ✅/❌/🍳 decision into audit_log (Dashboard → မှတ်တမ်း).

    The bot-card decision used to reach Min only as a Telegram message;
    now it is also a permanent, undeletable log row, next to the
    dashboard's own entries (same columns as the API's _audit).
    Best-effort only — never raises into the shop flow.
    """
    action = ("shop_order.confirm" if decision == "shop_ok" else
              "shop_order.wait" if decision == "shop_wait" else
              "shop_order.ready" if decision == "shop_ready" else "shop_order.reject")
    try:
        conn = db_connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit_log ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL,"
                "  entity_type TEXT NOT NULL, entity_id TEXT, details TEXT, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO audit_log(actor_type,actor_id,action,entity_type,entity_id,details,created_at)"
                " VALUES('shop',?,?,?,?,?,?)",
                (shop_name, action, "order", order_no,
                 json.dumps({"shop": shop_name}, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


async def shop_order_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shop taps ✅ / ❌ on its order card; Min + customer are told.
    ⏳ Wait (Min 2026-11-24) only holds the order — the shop may
    still accept/reject after, and the customer is told directly.

    Only the shop's owner (or Min) may decide, and only once — a
    second tap is told the order is already decided. On confirm,
    the card is edited in place to a single Ready button; the
    rider is called only when the shop taps Ready (food prepared,
    v19.79, Min's decision) — never at accept. /deliver covers
    the manual case.
    """
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    decision, _, order_no = data.partition(":")
    if decision not in ("shop_ok", "shop_no", "shop_wait") or not order_no:
        return
    row = get_order_by_no(order_no)
    if not row:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီအော်ဒါကို မတွေ့တော့ပါဘူး။", show_alert=True)
        return
    # get_order_by_no column order: id, order_no, created_at,
    # customer_name, customer_chat_id, ..., shop, idempotency_key, status.
    customer_chat_id = row[4]
    shop_name = row[13]
    shop_chat_id = get_shop_owner_chat_id(shop_name)
    allowed = {shop_chat_id}
    if OWNER_CHAT_ID:
        try:
            allowed.add(int(OWNER_CHAT_ID))
        except (TypeError, ValueError):
            pass
    allowed.discard(None)
    if update.effective_chat.id not in allowed:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒါကို ဆုံးဖြတ်ဖို့ ခွင့်ပြုချက် မရှိပါဘူး။", show_alert=True)
        return
    if decision == "shop_wait":
        # Wait is NOT a decision: the shop may still tap accept/reject
        # after. Hold the order (order_status 'waiting') so the
        # owner's repeat alarm stops, and tell the customer directly
        # here — no order_status_events row, so the status-notify job
        # never double-sends.
        try:
            conn = db_connect()
            try:
                cur = conn.execute(
                    "SELECT status, COALESCE(order_status,'pending') FROM orders WHERE order_no=?",
                    (order_no,),
                ).fetchone()
                if cur is not None and cur[0] not in (None, "new"):
                    await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
                    return
                if cur is not None and cur[1] == "waiting":
                    await q.answer("စောင့်ဆိုင်းထားပြီးသားပါရှင့်။", show_alert=True)
                    return
                if cur is None or cur[1] != "pending":
                    await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
                    return
                upd = conn.execute(
                    "UPDATE orders SET order_status='waiting' WHERE order_no=? "
                    "AND COALESCE(order_status,'pending')='pending'",
                    (order_no,),
                )
                conn.commit()
                if upd.rowcount != 1:
                    await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
                    return
            finally:
                conn.close()
        except Exception:
            logger.exception("shop wait: order %s", order_no)
            await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ထပ်ကြိုးစားပေးပါ။", show_alert=True)
            return
        _audit_shop_decision(shop_name, order_no, decision)
        owner_text = (
            f"⏳ ဆိုင် ({shop_name}) က အော်ဒါ {order_no} ကို ခဏစောင့်ခိုင်းလိုက်ပါတယ်။"
        )
        customer_text = (
            f"⏳ အော်ဒါ {order_no} ကို ဆိုင်က စစ်ဆေးနေပါတယ်ရှင့်။ ခဏလေး စောင့်ပေးပါဦးရှင့်။ 🙏"
        )
        shop_text = "စောင့်ဆိုင်းထားလိုက်ပါပြီရှင့်။ စစ်ဆေးပြီးရင် လက်ခံမလား ဆုံးဖြတ်ပေးပါနော်။"
        if OWNER_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=owner_text)
            except Exception:
                pass
        if customer_chat_id:
            try:
                await context.bot.send_message(
                    chat_id=int(customer_chat_id), text=customer_text
                )
            except Exception:
                pass
        await q.message.reply_text(shop_text)
        return
    status = "shop_confirmed" if decision == "shop_ok" else "shop_rejected"
    if not set_order_status(order_no, status):
        await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
        return
    # Audit log (2026-11-21, Min's decision): the shop's ✅/❌ shows in
    # Dashboard → မှတ်တမ်း too, so Min sees it in one place.
    # Only reached for a real first decision — a duplicate tap returns
    # above, so no duplicate audit row.
    _audit_shop_decision(shop_name, order_no, decision)
    if decision == "shop_ok":
        owner_text = (
            f"✅ ဆိုင် ({shop_name}) က အော်ဒါ {order_no} ကို "
            "လက်ခံလိုက်ပါပြီ။ ပြင်ဆင်နေပါပြီ။"
        )
        customer_text = (
            f"✅ ဆိုင်က အော်ဒါ {order_no} ကို လက်ခံလိုက်ပါပြီရှင့်။ "
            "ပြင်ဆင်ပြီးရင် Rider လွှတ်ပေးပါမယ်။ ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏"
        )
        shop_text = "✅ လက်ခံလိုက်ပါပြီ။ ပြင်ဆင်ပေးပါနော်။ ပြင်ဆင်ပြီးရင် အောက်က «🍳 ပြင်ဆင်ပြီးပြီ» ကို နှိပ်ပေးပါ။"
    else:
        owner_text = (
            f"❌ ဆိုင် ({shop_name}) က အော်ဒါ {order_no} ကို "
            "လက်မခံနိုင်ပါဘူး။ Customer ကို ဆက်သွယ်ပေးပါ။"
        )
        customer_text = (
            f"တောင်းပန်ပါတယ်ရှင့်။ ဆိုင်က အော်ဒါ {order_no} ကို "
            "လက်မခံနိုင်လို့ Admin က ဆက်သွယ်ပြီး ကူညီပေးပါမယ်ရှင့်။ 🙏"
        )
        shop_text = "လက်မခံနိုင်တာ မှတ်ထားလိုက်ပါပြီရှင့်။ Admin ကို အကြောင်းကြားထားပါတယ်ရှင့်။"
    if OWNER_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=owner_text)
        except Exception:
            pass
    if customer_chat_id:
        try:
            await context.bot.send_message(
                chat_id=int(customer_chat_id), text=customer_text
            )
        except Exception:
            pass
    _sent_reply = False
    if decision == "shop_ok":
        # Ready step (v19.79, Min's decision): the rider is called
        # only when the shop taps Ready (food prepared) — never at
        # accept. Replace the card's buttons with the single Ready
        # button, in place; if the edit fails, send a new message
        # carrying the button instead (best-effort).
        _ready_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🍳 ပြင်ဆင်ပြီးပြီ", callback_data=f"shop_ready:{order_no}"),
        ]])
        try:
            await q.edit_message_reply_markup(reply_markup=_ready_kb)
        except Exception:
            try:
                await q.message.reply_text(shop_text, reply_markup=_ready_kb)
                _sent_reply = True
            except Exception:
                pass
    if not _sent_reply:
        await q.message.reply_text(shop_text)


async def shop_order_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shop taps the Ready button on its order card: the food is
    prepared, so the rider is called now (v19.79, Min's decision).

    Only the shop's owner (or Min) may tap it, and only after the
    shop has accepted (bot status 'shop_confirmed'). The order
    moves to order_status 'delivering' (rowcount-gated, so a double
    tap writes one event), the tap is audited as shop_order.ready,
    and the existing rider path is called. The button is then
    removed. Test orders are refused, as are rejected/closed
    orders; an order that already has an active delivery is not
    re-created. The state write follows the established bot pattern
    (Wait/cancel paths): direct SQL, guarded to non-terminal
    values, deliberately bypassing the API's ORDER_NEXT check.
    """
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    action, _, order_no = data.partition(":")
    if action != "shop_ready" or not order_no:
        return
    row = get_order_by_no(order_no)
    if not row:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီအော်ဒါကို မတွေ့တော့ပါဘူး။", show_alert=True)
        return
    shop_name = row[13]
    bot_status = row[15]
    is_test = row[17]
    shop_chat_id = get_shop_owner_chat_id(shop_name)
    allowed = {shop_chat_id}
    if OWNER_CHAT_ID:
        try:
            allowed.add(int(OWNER_CHAT_ID))
        except (TypeError, ValueError):
            pass
    allowed.discard(None)
    if update.effective_chat.id not in allowed:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒါကို ဆုံးဖြတ်ဖို့ ခွင့်ပြုချက် မရှိပါဘူး။", show_alert=True)
        return
    if is_test:
        await q.answer(f"🧪 {order_no} က စမ်းသပ်အော်ဒါ (TEST) ဖြစ်ပါတယ်ရှင့်။ " + "Delivery ဖန်တီးမပေးပါ။", show_alert=True)
        return
    if bot_status != "shop_confirmed":
        if bot_status in ("shop_rejected", "closed"):
            await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
        else:
            await q.answer("အရင် «✅ လက်ခံမယ်» ကို နှိပ်ပေးပါဦးရှင့်။", show_alert=True)
        return
    # State advance: delivering, unless the order is already terminal.
    # Rowcount-gated so a double tap writes no second event.
    changed = False
    try:
        conn = db_connect()
        try:
            cur = conn.execute(
                "SELECT COALESCE(order_status,'pending') FROM orders WHERE order_no=?",
                (order_no,),
            ).fetchone()
            if cur is not None and cur[0] in ("delivered", "cancelled"):
                await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
                return
            upd = conn.execute(
                "UPDATE orders SET order_status='delivering' WHERE order_no=? "
                "AND COALESCE(order_status,'pending') NOT IN ('delivered','cancelled','delivering')",
                (order_no,),
            )
            conn.commit()
            changed = upd.rowcount == 1
            if changed:
                conn.execute(
                    "INSERT INTO order_status_events(order_id,status,note,actor_type,created_at) VALUES(?,?,?,?,?)",
                    (row[0], "delivering", None, "shop",
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("shop ready: state write failed for %s", order_no)
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ထပ်ကြိုးစားပေးပါ။", show_alert=True)
        return
    if changed:
        _audit_shop_decision(shop_name, order_no, "shop_ready")
    if get_active_delivery_for_order(row[0]):
        # Rider already called (dashboard Ready, or an earlier tap):
        # say so, do not create a second delivery.
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text("Rider ကို အလုပ် ပို့ပေးပြီးသားပါရှင့်။")
        return
    try:
        await maybe_auto_assign_delivery(context, order_no)
    except Exception:
        pass
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text("✅ ပြင်ဆင်ပြီးပြီ မှတ်ထားလိုက်ပါပြီ — Rider ကို အလုပ် ပို့ပေးလိုက်ပါပြီ။")


# --- Fee-confirm (2026-11-20, v19.54, Min's decision) -------------------
# Min types the EXACT delivery fee per order in the Dashboard (30 baht
# is only the minimum, never the fee itself). The fee job asks the
# customer to accept it; only after the customer accepts does the order
# go to the shop — and only after the shop taps Ready (food prepared,
# v19.79) does the rider get the job card (maybe_auto_assign_delivery,
# called from the Ready handler and the status-notify job). A declined
# fee cancels the order and the shop never hears about it.
# Min 2026-11-23: an ownerless order (no shop to confirm)
# sends the rider job card the moment the fee is accepted.


def _ensure_fee_columns(conn):
    """Add the fee-confirm columns when missing (the API migrates them
    too; the bot must not assume they exist — both processes share the
    same orders.db). Additive only."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    for name, ddl in (
        ("fee_status", "ALTER TABLE orders ADD COLUMN fee_status TEXT"),
        ("fee_msg_sent", "ALTER TABLE orders ADD COLUMN fee_msg_sent INTEGER NOT NULL DEFAULT 0"),
        ("fee_note", "ALTER TABLE orders ADD COLUMN fee_note TEXT"),
        # Shop-card flag (2026-11-20, v19.55): the phone-fee job
        # marks it when the shop card is sent for an accepted phone
        # order (or when no separate shop card is needed).
        ("shop_card_sent", "ALTER TABLE orders ADD COLUMN shop_card_sent INTEGER NOT NULL DEFAULT 0"),
        # Voucher + Telegram identity (2026-11-22, v19.59, shared with
        # the API migration): items_json + telegram_username. The bot
        # must not assume they exist — both processes share orders.db.
        ("telegram_username", "ALTER TABLE orders ADD COLUMN telegram_username TEXT"),
        ("items_json", "ALTER TABLE orders ADD COLUMN items_json TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)


def _stage_label(order_status, fee_status):
    """Min's 4 workflow stages (2026-11-22, point 6) as a DISPLAY label.
    Internal order statuses stay unchanged; preparing/delivering are
    their own steps (Min 2026-11-22: step by step)."""
    if order_status == "cancelled" or fee_status == "declined":
        return "ပယ်ဖျက်ပြီး"
    if order_status == "delivered":
        return "ပို့ပြီး"
    if order_status == "preparing":
        return "ပြင်ဆင်နေသည်"
    if order_status == "delivering":
        return "ပို့နေသည်"
    if order_status == "waiting":
        return "စောင့်ဆိုင်းနေသည်"
    if order_status == "confirmed" or fee_status == "accepted":
        return "အတည်ပြုပြီး"
    if order_status == "pending" and fee_status == "pending":
        return "ဧည့်သည် အတည်ပြုရန် စောင့်နေဆဲ"
    return "အော်ဒါဝင်"


def _voucher_items(order):
    """(lines, priced_total, has_priceless) from order['items_json'];
    falls back to the stored item text with no computable total.
    Prices are display-only — never invented, never trusted for money."""
    raw = order.get("items_json") if isinstance(order, dict) else None
    items = None
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                items = parsed
        except ValueError:
            items = None
    if not items:
        text = (order.get("item") or "").strip() if isinstance(order, dict) else ""
        if not text:
            return [], None, False
        # Fallback: the stored text already carries the app's own
        # wording (prices included when the app knew them) — show it
        # as-is, compute no total, claim no missing price.
        return [f"• {text}"], None, False
    lines, total, priced_any, priceless = [], 0.0, False, False
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = int(it.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        price = it.get("price")
        if isinstance(price, (int, float)) and not isinstance(price, bool) and price >= 0:
            line_total = price * qty
            if qty > 1:
                lines.append(f"• {name} + {qty} ({price:g} ဘတ် × {qty} = {line_total:g} ဘတ်)")
            else:
                lines.append(f"• {name} + {qty} ({price:g} ဘတ်)")
            total += line_total
            priced_any = True
        else:
            lines.append(f"• {name} + {qty} — ဈေးနှုန်း ဆိုင်အတည်ပြုရန်")
            priceless = True
    return lines, (total if priced_any else None), priceless


def _voucher_address_lines(order, drop_label=False):
    """Full address lines for the voucher: the drop-off place plus the
    customer's GPS/Maps line when it adds information."""
    lines = []
    try:
        place = fmt_place(order['dropoff'])
        lines.append(place if drop_label else f"ပို့ရမည့်လိပ်စာ: {place}")
    except Exception:
        pass
    loc = order.get("customer_location") if isinstance(order, dict) else None
    if isinstance(loc, dict):
        joined = "\n".join(lines)
        maps_url = str(loc.get("maps_url") or "")
        address = str(loc.get("address") or "")
        if maps_url and maps_url not in joined:
            lines.append(f"📍 {maps_url}")
        if address and address not in joined:
            lines.append(f"🏠 {address}")
    return lines


def _fee_message_text(order_no, fee, fee_note, order=None):
    """The fee-confirm VOUCHER to the customer (Min's points 13/14/16):
    items + prices + full address + fee + total, with ✅/❌ below it.
    Warm, gentle, with compassion (Min's standing rule). Without an
    order dict it stays the short fee-only message."""
    if not isinstance(order, dict):
        lines = [
            f"မင်္ဂလာပါရှင့် 🙏 အော်ဒါ {order_no} အတွက် ပို့ဆောင်ခ {fee:g} ဘတ် ဖြစ်ပါတယ်ရှင့်။",
        ]
        if fee_note:
            lines.append(fee_note)
        lines.append("လက်ခံပါသလား ရှင့် — အောက်က ခလုတ်လေးနဲ့ ရွေးပေးပါနော်၊ ကျေးဇူးတင်ပါတယ်ရှင့် 💜")
        return "\n".join(lines)
    customer_name = (order.get("customer_name") or "").strip() or "—"
    phone = (order.get("sender_phone") or order.get("receiver_phone") or "").strip() or "—"
    lines = [
        "မင်္ဂလာပါရှင့် 🙏",
        f"Order {order_no} အတွက် ဘေလ်စလစ်လေး ပို့ပေးလိုက်ပါတယ်ရှင့် 🧾",
        "မှာယူသူ အချက်အလက်",
        f"👤 အမည်: {customer_name}",
        f"📞 ဖုန်း: {phone}",
        "မှာထားတဲ့ ပစ္စည်း",
    ]
    item_lines, items_total, has_priceless = _voucher_items(order)
    lines.extend(item_lines or ["• —"])
    if items_total is not None:
        lines.append(f"• ပစ္စည်းဖိုး: {items_total:g} ဘတ်")
    if has_priceless:
        lines.append("ဈေးမပါတဲ့ ပစ္စည်းဈေးကို ဆိုင်က အတည်ပြုပေးပါမယ်ရှင့်။")
    lines.append(f"• ပို့ဆောင်ခ: {fee:g} ဘတ်")
    if items_total is not None:
        lines.append(f"• စုစုပေါင်း: {items_total + fee:g} ဘတ်")
    lines.append("ပို့ပေးရမယ့် လိပ်စာ")
    lines.extend(_voucher_address_lines(order, drop_label=True))
    if fee_note:
        lines.append(fee_note)
    lines.append("အထက်ပါ အမည်၊ ဖုန်းနံပါတ်၊ လိပ်စာ နဲ့ ဈေးနှုန်းတွေ မှန်ကန်ရဲ့လား တစ်ချက်စစ်ပေးပါဦးနော် 😍 မှန်ရင် အောက်က ခလုတ်နှစ်ခုထဲက အဆင်ပြေရာလေးနဲ့ အတည်ပြုပေးပါနော်။")
    return "\n".join(lines)


def contact_keyboard():
    """Contact button under the order-confirmed message (Min
    2026-11-23; Min 12:02: the button is Min Min's own account
    (t.me/MMK2958) — the SafeDeliveryone link sent at 11:56 was
    a mistake. The phone number is a text line
    in the message itself — Telegram rejects tel: URLs in
    inline keyboards (BUTTON_URL_INVALID), which silently
    killed the whole confirmed message on 2026-11-23."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 Min Min", url="https://t.me/MMK2958"),
            ]
        ]
    )


def _identity_lines(order):
    """Name + phone lines, the voucher's 'မှာယူသူ အချက်အလက်'
    section in the same shape (Min 2026-11-23: the confirmed
    message must carry them too). Same fallbacks as the voucher:
    '—' when unknown."""
    customer_name = (order.get("customer_name") or "").strip() or "—"
    phone = (order.get("sender_phone") or order.get("receiver_phone") or "").strip() or "—"
    return [f"👤 အမည်: {customer_name}", f"📞 ဖုန်း: {phone}"]


def _final_order_text(order, order_no, fee):
    """Customer order-confirmed message — Min's sample 2026-11-23,
    word for word, plus the name + phone section he asked for at
    11:03 and the rider-contact line he asked for at 11:26. Items and prices are DELIBERATELY absent: the voucher
    above already showed them once. `fee` stays in the signature
    (both call sites pass it) but is unused here; the delivering
    notice carries the prices reminder instead. Min 12:00: the
    business phone line (📞 09441582958) was added here and the
    tel: button removed — Telegram rejects tel: URLs."""
    parts = [
        "ကျေးဇူးအများကြီးတင်ပါတယ်ရှင့် 🙏 Order ကို အတည်ပြုပေးတဲ့အတွက် စိတ်ချမ်းသာအပူအပင်ကင်းပြီး ပျော်ရွှင်မှုတွေနဲ့ နေ့ရက်တိုင်းကို ဖြတ်သန်းနိုင်ပါစေရှင် ✨",
        f"✅ Order အတည်ပြုပြီး ({order_no})",
        "မှာယူသူ အချက်အလက်",
    ]
    parts.extend(_identity_lines(order))
    parts.append("ပို့ပေးရမယ့် လိပ်စာ")
    parts.extend(_voucher_address_lines(order, drop_label=True))
    parts.append(
        "အခု ဆိုင်ဘက်ကို ဆက်သွယ်ပြီး မှာထားတာလေး ပြင်ဆင်ခိုင်းနေပါပြီရှင့်။ အဆင်သင့်ဖြစ်တာနဲ့ Rider လွှတ်ပေးပါမယ်နော်။ ခဏလေး စောင့်ဆိုင်းပေးပါဦးရှင့် 🛵💨"
    )
    parts.append("လာပို့မဲ့ Rider အကောင့်ကို အောက်မှာချပေးထားပါတယ်ရှင့်")
    parts.append("📞 09441582958")
    return "\n\n".join(parts)


async def fee_confirm_job(context):
    """JobQueue fee-confirm (every 15s): send the fee message to the
    customer for each real order whose fee Min set in the Dashboard
    (fee_status='pending', fee_msg_sent=0). One bad order never kills
    the batch, and the job itself never raises into the scheduler.
    A failed send releases the claim so the next tick retries it."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("fee confirm: cannot open database")
        return
    try:
        try:
            _ensure_fee_columns(conn)
            conn.commit()
        except Exception:
            logger.exception("fee confirm: column ensure failed")
            conn.rollback()
            return
        try:
            rows = conn.execute(
                "SELECT id, order_no, customer_chat_id, delivery_fee, fee_note FROM orders "
                "WHERE fee_status='pending' AND COALESCE(fee_msg_sent,0)=0 "
                "AND COALESCE(is_test,0)=0 AND COALESCE(order_status,'pending') IN ('pending','waiting') "
                "AND customer_chat_id IS NOT NULL ORDER BY id"
            ).fetchall()
        except Exception:
            logger.exception("fee confirm: candidate lookup failed")
            return
        for order_id, order_no, customer_chat_id, fee, fee_note in rows:
            try:
                cur = conn.execute(
                    "UPDATE orders SET fee_msg_sent=2 WHERE id=? AND COALESCE(fee_msg_sent,0)=0",
                    (order_id,),
                )
                conn.commit()
                if cur.rowcount != 1:
                    continue  # claimed by another tick/process
            except Exception:
                logger.exception("fee confirm: claim failed for %s", order_no)
                continue
            try:
                try:
                    _order = load_api_order(conn, order_id)
                except Exception:
                    logger.exception("fee confirm: cannot load order %s", order_no)
                    _order = None
                fee_text = _fee_message_text(
                    order_no, fee if fee is not None else 30, fee_note, _order
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ လက်ခံမယ်", callback_data=f"fee_ok:{order_no}"),
                    InlineKeyboardButton("❌ မလက်ခံဘူး", callback_data=f"fee_no:{order_no}"),
                ]])
                await context.bot.send_message(
                    chat_id=int(customer_chat_id), text=fee_text, reply_markup=kb
                )
                conn.execute(
                    "UPDATE orders SET fee_msg_sent=1 WHERE id=?", (order_id,)
                )
                conn.commit()
            except Exception:
                logger.exception("fee confirm: send failed for %s", order_no)
                try:
                    conn.execute(
                        "UPDATE orders SET fee_msg_sent=0 WHERE id=?", (order_id,)
                    )
                    conn.commit()
                except Exception:
                    pass
    finally:
        conn.close()


async def fee_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The customer taps ✅ / ❌ on the fee-confirm message.

    Only the customer who placed the order may decide, and only once
    — a second tap is told the fee is already decided. On accept: the
    order goes to the shop (notify_shop_of_order); the shop's ✅ then
    auto-assigns the rider as today. On decline: the order is
    cancelled and the shop never hears about it.
    """
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    decision, _, order_no = data.partition(":")
    if decision not in ("fee_ok", "fee_no") or not order_no:
        return
    row = get_order_by_no(order_no)
    if not row:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီအော်ဒါကို မတွေ့တော့ပါဘူး။", show_alert=True)
        return
    # get_order_by_no column order: id, order_no, created_at,
    # customer_name, customer_chat_id, ..., shop, idempotency_key, status.
    order_id = row[0]
    customer_chat_id = row[4]
    shop_name = row[13]
    if not customer_chat_id or update.effective_chat.id != int(customer_chat_id):
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒါကို ဆုံးဖြတ်ဖို့ ခွင့်ပြုချက် မရှိပါဘူး။", show_alert=True)
        return
    try:
        conn = db_connect()
    except Exception:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ထပ်ကြိုးစားပေးပါနော်။", show_alert=True)
        return
    order = None
    try:
        _ensure_fee_columns(conn)
        conn.commit()
        new_status = "accepted" if decision == "fee_ok" else "declined"
        cur = conn.execute(
            "UPDATE orders SET fee_status=? WHERE id=? AND fee_status='pending'",
            (new_status, order_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            await q.answer("ဒီအော်ဒါကို ဆုံးဖြတ်ပြီးသားပါရှင့်။", show_alert=True)
            return
        fee_row = conn.execute(
            "SELECT delivery_fee FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        fee = fee_row[0] if fee_row and fee_row[0] is not None else 30
        if decision == "fee_no":
            # Cancelled: the shop never hears. No order_status_events
            # row — the customer is told here, not by the notify job.
            conn.execute(
                "UPDATE orders SET order_status='cancelled' WHERE id=?", (order_id,)
            )
            conn.commit()
        if decision == "fee_ok":
            try:
                order = load_api_order(conn, order_id)
            except Exception:
                logger.exception("fee decision: cannot load order %s", order_no)
                order = None
    finally:
        conn.close()
    # v19.56: tell Min the truth — send the shop card FIRST, then say
    # whether it reached the shop. (Before, Min got "sent to the shop"
    # and then a contradictory "shop could not be reached" warning for
    # the same order.) The customer message no longer claims the shop
    # got it — it says we are contacting the shop.
    shop_notified = None
    if decision == "fee_ok" and order is not None:
        try:
            shop_notified = await notify_shop_of_order(
                context, order, order_no, fee, _stage_label("confirmed", "accepted")
            )
        except Exception:
            logger.exception("fee decision: shop card failed for %s", order_no)
            shop_notified = None
    # v19.57: mark the shop card done so the fee-accept job (which now
    # also covers Telegram orders accepted via the dashboard) does not
    # send it a second time. No card needed OR sent OK -> done. A real
    # send failure (an owner exists but the send failed) stays 0 so
    # the job retries it.
    if decision == "fee_ok" and order is not None:
        try:
            if shop_notified or not _shop_card_needed(order):
                _c2 = db_connect()
                try:
                    _c2.execute(
                        "UPDATE orders SET shop_card_sent=1 WHERE order_no=?",
                        (order_no,),
                    )
                    _c2.commit()
                finally:
                    _c2.close()
        except Exception:
            logger.exception("fee decision: shop_card_sent mark failed for %s", order_no)
    # Min 2026-11-23: no shop to confirm (ownerless order) ->
    # the rider gets the job card the moment the fee is accepted.
    if decision == "fee_ok" and order is not None:
        try:
            if not _shop_card_needed(order):
                await maybe_auto_assign_delivery(context, order_no)
        except Exception:
            logger.exception("fee decision: auto-assign failed for %s", order_no)
    if decision == "fee_ok":
        try:
            _ownerless = bool(order) and not _shop_card_needed(order)
        except Exception:
            _ownerless = False
        owner_text = _owner_fee_accept_text(
            "Customer", order_no, fee, bool(shop_notified), _ownerless, shop_name
        )
        if order is not None:
            customer_text = _final_order_text(order, order_no, fee)
        else:
            customer_text = (
                f"ကျေးဇူးတင်ပါတယ်ရှင့် 🙏 ပို့ခ {fee:g} ဘတ် လက်ခံပေးလို့ — "
                "ဆိုင်ကို ဆက်သွယ်ပေးနေပါပြီ။ ပြင်ဆင်ပြီးရင် Rider လွှတ်ပေးပါမယ်။"
            )
    else:
        owner_text = (
            f"❌ Customer က အော်ဒါ {order_no} ရဲ့ ပို့ခ {fee:g} ဘတ် "
            "မလက်ခံလို့ အော်ဒါ ပယ်ဖျက်လိုက်ပါပြီ — ဆိုင်ကို မပို့ရသေးပါ။"
        )
        customer_text = (
            "ရပါတယ်ရှင့်၊ နားလည်ပါတယ်ရှင့် 🙏 "
            f"အော်ဒါ {order_no} ကို ပယ်ဖျက်လိုက်ပါပြီ။ "
            "နောက်တစ်ခါ မှာယူရင် ကူညီပေးပါ့မယ်ရှင့်။"
        )
    _uname = order.get("telegram_username") if isinstance(order, dict) else None
    if _uname:
        owner_text += f"\nCustomer Telegram: @{_uname}"
    if OWNER_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=owner_text)
        except Exception:
            pass
    if customer_chat_id:
        try:
            await context.bot.send_message(
                chat_id=int(customer_chat_id),
                text=customer_text,
                reply_markup=contact_keyboard() if decision == "fee_ok" else None,
            )
        except Exception:
            logger.exception("fee decision: confirmed message failed for %s", order_no)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# --- Fee-accept job (2026-11-20, v19.55; generalised 2026-11-21, v19.57) ---
# A customer has no Telegram chat id on a phone order, so the bot
# cannot message them — they accept the fee in the app (POST
# /api/public/orders/.../fee-decision). The fee-accept job sends the
# order to the shop once a fee is accepted, and tells Min. v19.57: it
# now also covers a Telegram order whose fee Min accepted in the
# Dashboard (the customer never tapped ✅/❌) — the shop card goes out
# the same way, and the Telegram customer is told the order is
# proceeding. The error-alert job watches the API's error_log and
# tells Min about system errors (rate-limited to one summary message
# per tick at most).


async def fee_accept_job(context):
    """JobQueue fee-accept (every 15s): for each real order whose fee
    was accepted (fee_status='accepted', shop_card_sent=0), send the
    order to the shop and tell Min. Covers BOTH paths: a phone
    customer accepting in the app (v19.55) and a Telegram customer
    whose fee Min accepted in the Dashboard (v19.57). A shop with no
    owner can never receive a card — it is marked done and Min is
    told once, honestly (v19.56/v19.57: never "sent to the shop" when
    no shop could be reached). Claims with shop_card_sent=2 first
    (no double-send). One bad order never kills the batch, and the
    job itself never raises into the scheduler."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("fee accept: cannot open database")
        return
    try:
        try:
            _ensure_fee_columns(conn)
            conn.commit()
        except Exception:
            logger.exception("fee accept: column ensure failed")
            conn.rollback()
            return
        try:
            rows = conn.execute(
                "SELECT id, order_no FROM orders "
                "WHERE fee_status='accepted' AND COALESCE(shop_card_sent,0)=0 "
                "AND COALESCE(is_test,0)=0 ORDER BY id"
            ).fetchall()
        except Exception:
            logger.exception("fee accept: candidate lookup failed")
            return
        for order_id, order_no in rows:
            try:
                cur = conn.execute(
                    "UPDATE orders SET shop_card_sent=2 WHERE id=? AND COALESCE(shop_card_sent,0)=0",
                    (order_id,),
                )
                conn.commit()
                if cur.rowcount != 1:
                    continue  # claimed by another tick/process
            except Exception:
                logger.exception("fee accept: claim failed for %s", order_no)
                continue
            try:
                order = load_api_order(conn, order_id)
            except Exception:
                logger.exception("fee accept: cannot load order %s", order_no)
                try:
                    conn.execute("UPDATE orders SET shop_card_sent=0 WHERE id=?", (order_id,))
                    conn.commit()
                except Exception:
                    pass
                continue
            if order is None:
                continue  # row gone; the claim stays 2 (harmless)
            fee_row = conn.execute(
                "SELECT delivery_fee FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            fee = fee_row[0] if fee_row and fee_row[0] is not None else 30
            _who = "Phone customer" if not order.get("customer_chat_id") else "Customer"
            notified = None
            try:
                _need_shop_card = _shop_card_needed(order)
            except Exception:
                logger.exception("fee accept: shop-card check failed for %s", order_no)
                _need_shop_card = False
            if _need_shop_card:
                try:
                    notified = await notify_shop_of_order(
                        context, order, order_no, fee, _stage_label("confirmed", "accepted")
                    )
                except Exception:
                    logger.exception("fee accept: shop card failed for %s", order_no)
                    try:
                        conn.execute("UPDATE orders SET shop_card_sent=0 WHERE id=?", (order_id,))
                        conn.commit()
                    except Exception:
                        pass
                    continue
                if notified is None:
                    # A real send failure (an owner exists but the
                    # send failed) — release the claim for the next
                    # tick. The ownerless case never reaches here:
                    # _shop_card_needed is False for it, so no notify
                    # is attempted at all (handled below).
                    try:
                        conn.execute("UPDATE orders SET shop_card_sent=0 WHERE id=?", (order_id,))
                        conn.commit()
                    except Exception:
                        pass
                    continue
            try:
                conn.execute("UPDATE orders SET shop_card_sent=1 WHERE id=?", (order_id,))
                conn.commit()
            except Exception:
                logger.exception("fee accept: flag failed for %s", order_no)
                continue
            # Min 2026-11-23: no shop to confirm (ownerless
            # order) -> the rider gets the job card at once.
            if not _need_shop_card:
                try:
                    await maybe_auto_assign_delivery(context, order_no)
                except Exception:
                    logger.exception("fee accept: auto-assign failed for %s", order_no)
            # One honest message to Min: "sent to the shop" only when a
            # shop card really went out. When no shop could be reached
            # (ownerless shop), Min is told to call the shop — the old
            # text over-claimed here. A Telegram customer is told the
            # order is proceeding.
            if OWNER_CHAT_ID:
                try:
                    _txt = _owner_fee_accept_text(
                        _who, order_no, fee, bool(notified),
                        not _need_shop_card, order.get("shop") or "?",
                    )
                    _uname = order.get("telegram_username") if isinstance(order, dict) else None
                    if _uname:
                        _txt += f"\nCustomer Telegram: @{_uname}"
                    await context.bot.send_message(
                        chat_id=int(OWNER_CHAT_ID), text=_txt
                    )
                except Exception:
                    pass
            _cust = order.get("customer_chat_id")
            if _cust:
                try:
                    await context.bot.send_message(
                        chat_id=int(_cust),
                        text=_final_order_text(order, order_no, fee),
                        reply_markup=contact_keyboard(),
                    )
                except Exception:
                    logger.exception("fee accept job: confirmed message failed for %s", order_no)
    finally:
        conn.close()


async def error_alert_job(context):
    """JobQueue error alerts (every 60s): tell Min about new SYSTEM
    errors the API logged (api_500 database errors and intake rate
    limits). User mistakes (intake 400s) go to the dashboard Errors
    view only — they never page Min. At most one summary message per
    tick; alerted rows are marked so they are never re-sent."""
    try:
        conn = db_connect()
    except Exception:
        logger.exception("error alert: cannot open database")
        return
    try:
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(error_log)")}
        except Exception:
            return  # no error_log table yet — nothing to alert on
        if "alerted" not in cols:
            return
        try:
            rows = conn.execute(
                "SELECT id,type,message,order_no,created_at FROM error_log "
                "WHERE alerted=0 AND type IN ('api_500','order_intake_429') "
                "ORDER BY id LIMIT 5"
            ).fetchall()
        except Exception:
            logger.exception("error alert: lookup failed")
            return
        if not rows or not OWNER_CHAT_ID:
            return
        lines = ["⚠️ Safe Deli error — စနစ် error တက်နေပါတယ်ရှင့်:"]
        for _id, typ, msg, order_no, created_at in rows:
            lines.append(f"• [{typ}] {msg}" + (f" ({order_no})" if order_no else ""))
        try:
            await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text="\n".join(lines))
        except Exception:
            logger.exception("error alert: owner send failed")
            return
        try:
            ids = [r[0] for r in rows]
            conn.execute(
                f"UPDATE error_log SET alerted=1 WHERE id IN ({','.join('?' * len(ids))})", ids
            )
            conn.commit()
        except Exception:
            logger.exception("error alert: mark failed")
    finally:
        conn.close()


async def night_order_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The customer taps ✅ / ❌ on the night auto-message (v19.15).

    Only the customer who placed the order may decide, and only once
    — a second tap is told the order is already decided. Confirm:
    the order stays (delivered from 6 AM) and the owner is told the
    customer confirmed. Cancel: the order is cancelled in the DB and
    the owner is told. No reply at all: the order stays.
    """
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    decision, _, order_no = data.partition(":")
    if decision not in ("night_ok", "night_cancel") or not order_no:
        return
    row = get_order_by_no(order_no)
    if not row:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီအော်ဒါကို မတွေ့တော့ပါဘူး။", show_alert=True)
        return
    # get_order_by_no column order: id, order_no, created_at,
    # customer_name, customer_chat_id, ..., shop, idempotency_key, status.
    customer_chat_id = row[4]
    if update.effective_chat.id != customer_chat_id:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒါကို ဆုံးဖြတ်ဖို့ ခွင့်ပြုချက် မရှိပါဘူး။", show_alert=True)
        return
    if decision == "night_ok":
        owner_text = (
            f"✅ {order_no} — ဝယ်သူက ည အော်ဒါကို အတည်ပြုလိုက်ပါပြီ။ "
            "မနက် ၆ နာရီမှစ ပို့ပေးပါမယ်။"
        )
        customer_text = (
            "ကျေးဇူးတင်ပါတယ်ရှင့်။ အော်ဒါကို အတည်ပြုထားလိုက်ပါပြီ — "
            "မနက်ကြရင် စောစောလေး ပို့ပေးပါမယ်နော်။ 🙏"
        )
        await q.message.reply_text(customer_text)
    else:
        if not cancel_order_by_no(order_no):
            await q.answer("ဒီအော်ဒါကို cancel လုပ်ပြီးသားပါရှင့်။", show_alert=True)
            return
        owner_text = (
            f"❌ {order_no} — ဝယ်သူက ည အော်ဒါကို cancel လုပ်လိုက်ပါပြီ။"
        )
        customer_text = (
            "အော်ဒါကို cancel လုပ်ပေးလိုက်ပါပြီရှင့်။ "
            "ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏"
        )
        await q.message.reply_text(customer_text)
    await _tell_owner(context, owner_text)
    # Take the buttons away so a second tap cannot re-decide.
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------------------------------------------------------------- rider job cards
async def _tell_owner(context, text):
    """Best-effort owner notice; a failed send never breaks a flow."""
    if not OWNER_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
    except Exception:
        pass


async def _tell_chat(context, chat_id, text):
    """Best-effort message to one chat; never raises."""
    if not chat_id:
        return
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception:
        pass


def rider_job_card_text(delivery, order):
    """Job card text for a rider. `delivery` is a dict (see
    get_delivery); `order` is the plain-tuple row from
    get_order_by_id/get_order_by_no. The route link is the same
    one the dashboard shows (rider -> shop -> customer)."""
    route = delivery_route_url(delivery)
    lines = [f"🛵 Delivery အလုပ် — {order[1]}"]
    lines.append(f"ဆိုင်: {order[13] or '-'}")
    if delivery.get("pickup_address"):
        lines.append(f"ယူရမည့်နေရာ: {delivery['pickup_address']}")
    if delivery.get("pickup_lat") is not None and delivery.get("pickup_lon") is not None:
        lines.append(
            f"📍 ဆိုင်တည်နေရာ: {maps_link(delivery['pickup_lat'], delivery['pickup_lon'])}"
        )
    lines.append(f"ဧည့်သည်: {order[3]} — {order[5]}")
    if delivery.get("dropoff_address"):
        lines.append(f"ပို့ရမည့်နေရာ: {delivery['dropoff_address']}")
    if delivery.get("dropoff_lat") is not None and delivery.get("dropoff_lon") is not None:
        lines.append(
            f"📍 ဧည့်သည် တည်နေရာ: {maps_link(delivery['dropoff_lat'], delivery['dropoff_lon'])}"
        )
    lines.append(f"ပစ္စည်း: {order[9]}")
    if route:
        lines.append(f"🗺️ လမ်းကြောင်း (တစ်ဆက်ထဲ): {route}")
    lines.append("")
    lines.append("လက်ခံရင် ✅ ကို နှိပ်ပေးပါနော်။")
    return "\n".join(lines)


async def send_rider_job_card(context, delivery_id) -> bool:
    """Send the job card to the assigned rider's private chat.

    Returns False when the delivery has no rider or the send
    fails — the delivery stays 'assigned' and Min is told.
    """
    delivery = get_delivery(delivery_id)
    if not delivery or not delivery.get("rider_chat_id"):
        return False
    order = get_order_by_id(delivery["order_id"])
    if not order:
        return False
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ လက်ခံမယ်", callback_data=f"rider_ok:{delivery_id}"),
        InlineKeyboardButton("❌ လက်မခံနိုင်ဘူး", callback_data=f"rider_no:{delivery_id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=int(delivery["rider_chat_id"]),
            text=rider_job_card_text(delivery, order),
            reply_markup=kb,
        )
        return True
    except Exception:
        return False


async def create_and_assign_delivery(context, order_no, actor_type="system",
                                     actor_id=None, notify_owner=True):
    """Create a delivery for an order and auto-assign the nearest
    rider, then send them the job card. Returns
    {"delivery_id": int|None, "rider": dict|None, "error": str|None}.
    Never raises — every failure is reported to Min (unless
    notify_owner is False, e.g. /deliver reports it itself) and
    returned as "error", so the order flow is never blocked by
    delivery.
    """
    order = get_order_by_no(order_no)
    if not order:
        return {"delivery_id": None, "rider": None, "error": "Order not found"}
    order_id = order[0]
    active = get_active_delivery_for_order(order_id)
    if active:
        return {"delivery_id": active["id"], "rider": None,
                "error": "An active delivery already exists for this order"}
    try:
        delivery_id = create_delivery(order_id, actor_type=actor_type,
                                      actor_id=actor_id)
    except ValueError as exc:
        if notify_owner:
            await _tell_owner(
                context,
                f"🚚 Delivery ဖန်တီးမရပါ ({order_no}): {exc}\n"
                "Min က /deliver နဲ့ ကိုယ်တိုင် ဖန်တီးပေးနိုင်ပါတယ်။",
            )
        return {"delivery_id": None, "rider": None, "error": str(exc)}
    rider = pick_nearest_rider(delivery_id)
    if rider is None:
        if notify_owner:
            await _tell_owner(
                context,
                f"🚚 Delivery #{delivery_id} ({order_no}) ဖန်တီးပြီးပါပြီ —\n"
                "Rider မရှိသေးလို့ (သို့မဟုတ် အားလုံး အလုပ်များနေလို့) "
                "ဘယ်သူ့ကိုမှ မခိုင်းရသေးပါ။ /deliver နဲ့ ပြန်ခိုင်းပေးပါ။",
            )
        return {"delivery_id": delivery_id, "rider": None, "error": None}
    try:
        assign_delivery_to_rider(delivery_id, rider,
                                 actor_type=actor_type, actor_id=actor_id)
    except ValueError as exc:
        if notify_owner:
            await _tell_owner(
                context,
                f"🚚 Delivery #{delivery_id} ({order_no}) ကို Rider ခိုင်းမရပါ: {exc}",
            )
        return {"delivery_id": delivery_id, "rider": None, "error": str(exc)}
    sent = await send_rider_job_card(context, delivery_id)
    if notify_owner:
        if sent:
            await _tell_owner(
                context,
                f"🛵 Rider {rider['name']} ကို အလုပ် ပို့ပေးပြီးပါပြီ — "
                f"{order_no} (Delivery #{delivery_id})",
            )
        else:
            await _tell_owner(
                context,
                f"⚠️ Rider {rider['name']} ကို အလုပ် ပို့မရပါ — "
                f"{order_no} (Delivery #{delivery_id})။ "
                "Rider ကို ဖုန်းဆက်ပြီး အကြောင်းကြားပေးပါ။",
            )
    return {"delivery_id": delivery_id, "rider": rider, "error": None}


async def maybe_auto_assign_delivery(context, order_no):
    """Auto-assign when the shop signals Ready (food prepared). Best-effort
    wrapper around create_and_assign_delivery — never raises, so
    the shop-decision flow always finishes. Returns the delivery
    id or None."""
    try:
        result = await create_and_assign_delivery(
            context, order_no, actor_type="system", actor_id="shop_confirm")
        return result.get("delivery_id")
    except Exception:
        return None


async def rider_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rider taps a button on their job card.

    Only the assigned rider may decide. Accept: assigned ->
    accepted, Min + customer are told, and the rider gets the
    "picked up" button. Decline: the job goes to the next nearest
    rider (or back to 'created' when none is left). Picked up:
    accepted -> picked_up AND delivering in one tap (the rider is
    on the road now) — two events, one tap. Delivered: delivering
    -> delivered, Min + customer are told politely.
    """
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    action, _, delivery_id_s = data.partition(":")
    if action not in ("rider_ok", "rider_no", "rider_pickup", "rider_deliver"):
        return
    try:
        delivery_id = int(delivery_id_s)
    except ValueError:
        return
    delivery = get_delivery(delivery_id)
    if not delivery:
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီ delivery ကို မတွေ့တော့ပါဘူး။", show_alert=True)
        return
    if update.effective_chat.id != delivery.get("rider_chat_id"):
        await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒါကို ဆုံးဖြတ်ဖို့ ခွင့်ပြုချက် မရှိပါဘူး။", show_alert=True)
        return
    order = get_order_by_id(delivery["order_id"])
    order_no = order[1] if order else f"#{delivery['order_id']}"
    customer_chat_id = order[4] if order else None
    rider_name = delivery.get("rider_name") or "Rider"
    try:
        if action == "rider_ok":
            if not set_delivery_status(delivery_id, "accepted", "worker",
                                       str(delivery["rider_chat_id"])):
                await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီ delivery ကို မတွေ့တော့ပါဘူး။", show_alert=True)
                return
            await _tell_owner(
                context,
                f"✅ Rider {rider_name} က {order_no} ကို လက်ခံလိုက်ပါပြီ။ "
                "ဆိုင်ဆီ သွားနေပါပြီ။",
            )
            await _tell_chat(
                context, customer_chat_id,
                f"✅ Rider က အော်ဒါ {order_no} ကို လက်ခံလိုက်ပါပြီရှင့်။ "
                "ဆိုင်ဆီ သွားယူနေပါပြီ။ ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏",
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📦 ပစ္စည်း ယူပြီးပြီ",
                    callback_data=f"rider_pickup:{delivery_id}",
                ),
            ]])
            try:
                await context.bot.send_message(
                    chat_id=int(delivery["rider_chat_id"]),
                    text=f"✅ လက်ခံလိုက်ပါပြီ။ ဆိုင်ရောက်ရင် \"📦 ပစ္စည်း ယူပြီးပြီ\" ကို နှိပ်ပေးပါနော် — {order_no}",
                    reply_markup=kb,
                )
            except Exception:
                pass
            await q.message.reply_text("✅ လက်ခံလိုက်ပါပြီ။ ဆိုင်ဆီ သွားပေးပါနော်။")
        elif action == "rider_no":
            try:
                nxt = decline_delivery(delivery_id, delivery["rider_chat_id"])
            except ValueError as exc:
                await q.answer(str(exc), show_alert=True)
                return
            if nxt is not None:
                await _tell_owner(
                    context,
                    f"🔄 Rider {rider_name} က {order_no} ကို လက်မခံနိုင်လို့ "
                    f"နောက် Rider {nxt['name']} ကို အလုပ် ပို့ပေးလိုက်ပါပြီ။",
                )
                await send_rider_job_card(context, delivery_id)
            else:
                await _tell_owner(
                    context,
                    f"⚠️ Rider {rider_name} က {order_no} ကို လက်မခံနိုင်လို့ "
                    "ငြင်းလိုက်ပါပြီ။ နောက် Rider မရှိသေးလို့ Delivery ကို "
                    "ပြန်ဖြုတ်ထားပါတယ် — /deliver နဲ့ ပြန်ခိုင်းပေးပါ။",
                )
            await q.message.reply_text(
                "လက်မခံနိုင်တာ မှတ်ထားလိုက်ပါပြီရှင့်။ Admin ကို အကြောင်းကြားထားပါတယ်ရှင့်။"
            )
        elif action == "rider_pickup":
            rider_chat = str(delivery["rider_chat_id"])
            if not set_delivery_status(delivery_id, "picked_up", "worker", rider_chat):
                await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီ delivery ကို မတွေ့တော့ပါဘူး။", show_alert=True)
                return
            # One tap, two steps: the rider is on the road now.
            set_delivery_status(delivery_id, "delivering", "worker", rider_chat)
            await _tell_owner(
                context,
                f"📦 Rider {rider_name} က {order_no} ပစ္စည်း ယူပြီးပါပြီ။ "
                "ဧည့်သည်ဆီ ပို့နေပါပြီ။",
            )
            await _tell_chat(
                context, customer_chat_id,
                f"📦 Rider က {order_no} ပစ္စည်း ယူပြီးပါပြီ။ "
                "ဧည့်သည်ဆီ ပို့ဆောင်နေပါပြီရှင့်။ 🙏",
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ ပို့ပြီးပြီ",
                    callback_data=f"rider_deliver:{delivery_id}",
                ),
            ]])
            try:
                await context.bot.send_message(
                    chat_id=int(delivery["rider_chat_id"]),
                    text=f"📦 ပစ္စည်း ယူပြီးပါပြီ။ ဧည့်သည်ဆီ ရောက်ရင် \"✅ ပို့ပြီးပြီ\" ကို နှိပ်ပေးပါနော် — {order_no}",
                    reply_markup=kb,
                )
            except Exception:
                pass
            await q.message.reply_text("📦 ပစ္စည်း ယူပြီးပါပြီ။ ဧည့်သည်ဆီ သွားပေးပါနော်။")
        elif action == "rider_deliver":
            if not set_delivery_status(delivery_id, "delivered", "worker",
                                       str(delivery["rider_chat_id"])):
                await q.answer("တောင်းပန်ပါတယ်ရှင့်။ ဒီ delivery ကို မတွေ့တော့ပါဘူး။", show_alert=True)
                return
            await _tell_owner(
                context,
                f"✅ Rider {rider_name} က {order_no} ကို ပို့ပြီးပါပြီ။",
            )
            await _tell_chat(
                context, customer_chat_id,
                f"✅ ပစ္စည်း ရောက်ပါပြီရှင့် — {order_no}။ "
                "Safe Deli ကို သုံးစွဲပေးလို့ ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏",
            )
            try:
                _thanks = DELIVERED_THANKS_TEXT.get(
                    get_user_lang(customer_chat_id) or DEFAULT_LANG
                ) or DELIVERED_THANKS_TEXT.get(DEFAULT_LANG)
                if _thanks:
                    await _tell_chat(context, customer_chat_id, _thanks)
            except Exception:
                pass
            await q.message.reply_text(
                "✅ ပို့ပြီးပါပြီ။ ကျေးဇူးတင်ပါတယ်ရှင့်။ 🙏"
            )
    except ValueError as exc:
        # Illegal status jump (e.g. a double tap racing): tell the
        # rider plainly instead of crashing the handler.
        await q.answer(str(exc), show_alert=True)


async def safe_answer(query):
    """Answer a callback query without failing the whole handler.

    The bot talks to Telegram through a proxy, and a single call can
    time out (seen 2026-11-05 13:18, which made the bot look very
    slow / unresponsive). The reply below still goes out even if
    this answer times out.
    """
    try:
        await query.answer()
    except Exception:
        pass


# ---------------------------------------------------------------- keyboards
def main_menu_kb(lang="my"):
    # Button colours (Bot API 9.4, python-telegram-bot >= 22.7):
    # style="primary" = blue, "success" = green, "danger" = red.
    # Those are the only three colours Telegram offers — no purple,
    # yellow or custom colour. The main order button is primary blue.
    # Labels follow the user's chosen language (Part 1).
    labels = MENU_LABELS.get(lang) or MENU_LABELS[DEFAULT_LANG]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(labels["shop"], web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton(labels["location"], callback_data="show_location", style="primary")],
            [InlineKeyboardButton(labels["price"], callback_data="show_price", style="success")],
            [InlineKeyboardButton(labels["order"], callback_data="order_new", style="primary")],
            [InlineKeyboardButton(labels["phone"], callback_data="show_phone")],
            # Language switch (Min, 2026-11-12 18:52): /lang exists as a
            # typed command, but there was no tappable button for it on
            # the main menu — Min tapped around and found nothing. This
            # button re-shows the SAME picker as /lang (4 languages).
            [InlineKeyboardButton(labels["language"], callback_data="show_lang_picker")],
        ]
    )


def shop_kb(lang="my"):
    # Reply-keyboard button that opens the Mini App. This MUST be a
    # KeyboardButton (reply keyboard), not an InlineKeyboardButton and
    # not the Menu button: Telegram's WebApp.sendData() only delivers
    # to the bot when the Mini App was launched from a KEYBOARD button.
    # Menu / inline / plain-link launches silently send nothing (the
    # order then only reaches the admin via the in-app admin button,
    # and when the owner taps it himself it lands in his own Saved
    # Messages). Tapping THIS button opens the shop with a real
    # keyboard-button context, so a Mini App order auto-sends to the
    # bot chat as an SD-... card — no extra tap, no Saved Messages.
    # Label follows the user's chosen language (Part 1).
    labels = MENU_LABELS.get(lang) or MENU_LABELS[DEFAULT_LANG]
    return ReplyKeyboardMarkup(
        [[KeyboardButton(labels["shop"], web_app=WebAppInfo(url=MINI_APP_URL))]],
        resize_keyboard=True,
    )


def lang_picker_kb():
    """Four language buttons for /start (new user) and /lang."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🌐 {LANG_NAMES[code]}", callback_data=f"lang:{code}"
                )
                for code in ("my", "th")
            ],
            [
                InlineKeyboardButton(
                    f"🌐 {LANG_NAMES[code]}", callback_data=f"lang:{code}"
                )
                for code in ("en", "zh")
            ],
        ]
    )


# Exact-address fallback (Min, 2026-11-11 07:31): when a location
# has no GPS pin, the customer must also be able to enter the EXACT
# written address (ward / street / house number / nearby landmark).
# Shown as a second button under the Location button at BOTH the
# pickup and the drop-off step, for consistency.
# Backwards-compatible aliases: the Burmese strings (tests and
# older callers use these names). The per-language texts live in
# ADDRESS_*_TEXTS above; use address_*_text(lang) for the user.
ADDRESS_BUTTON_TEXT = ADDRESS_BUTTON_TEXTS[DEFAULT_LANG]
ADDRESS_ASK_TEXT = ADDRESS_ASK_TEXTS[DEFAULT_LANG]
ADDRESS_EMPTY_TEXT = ADDRESS_EMPTY_TEXTS[DEFAULT_LANG]


def location_kb(label=None, lang=DEFAULT_LANG):
    """Location keyboard in the user's language. An explicit label
    still wins (old callers); otherwise the label follows lang."""
    lang = lang if lang in LANGS else DEFAULT_LANG
    if label is None:
        label = location_button_text(lang)
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(label, request_location=True, style="primary")],
            [KeyboardButton(address_button_text(lang))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# Backwards-compatible alias: the Burmese help text (tests use
# this name). The per-language texts live in HELP_LOCATION_TEXTS;
# help_location_text(lang) is what the user reads.
HELP_LOCATION = lang_text(HELP_LOCATION_TEXTS, DEFAULT_LANG).format(
    addr_btn=ADDRESS_BUTTON_TEXTS[DEFAULT_LANG]
)

# Backwards-compatible alias: the Burmese welcome. The per-language
# texts live in WELCOME_TEXTS (see the languages section above).
WELCOME = WELCOME_TEXTS[DEFAULT_LANG]


# ---------------------------------------------------------------- handlers
async def _send_welcome(message, context, lang):
    """Send the welcome + shop line in the user's language."""
    await message.reply_text(
        lang_text(WELCOME_TEXTS, lang), reply_markup=main_menu_kb(lang)
    )
    # Persistent shop button (reply keyboard): tapping it opens the
    # Mini App with a keyboard-button context, so Mini App orders
    # auto-send to the bot chat as SD-... cards. The inline 🛒 button
    # above and the Menu button only open the shop for browsing —
    # they cannot auto-send (Telegram rule), so the shop button lives
    # here at the bottom of the chat as well.
    await message.reply_text(
        lang_text(SHOP_LINE_TEXTS, lang),
        reply_markup=shop_kb(lang),
    )


async def _maybe_link_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deep-link handler (v19.58, Min 2026-11-21): a phone/browser order
    carries no Telegram ID, so the bot cannot send its fee card. The app's
    "Confirm on Telegram" button opens t.me/SafeDeli_bot?start=ord_<no>;
    this links that Telegram chat to the order (customer_chat_id) so the
    fee-confirm job sends the card here. Never overwrites a chat id that
    belongs to a DIFFERENT Telegram user. Sends one confirmation message
    via context.bot (works for both the /start message flow and the
    language-picker callback flow)."""
    try:
        order_no = context.user_data.pop("link_order_no", None)
    except Exception:
        order_no = None
    if not order_no:
        return
    chat_id = update.effective_chat.id
    try:
        conn = db_connect()
    except Exception:
        logger.exception("order link: cannot open database")
        return
    try:
        row = conn.execute(
            "SELECT id, customer_chat_id FROM orders WHERE order_no=?",
            (order_no,),
        ).fetchone()
        if row is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"အော်ဒါ {order_no} ကို မတွေ့ပါဘူးရှင့်။ အော်ဒါနံပါတ် မှန်ကန်မှု စစ်ပေးပါရှင့်။",
            )
            return
        order_id, existing_chat = row
        if existing_chat is not None and str(existing_chat) != str(chat_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"အော်ဒါ {order_no} ကို တခြား Telegram အကောင့်နဲ့ ချိတ်ပြီးသား ဖြစ်ပါတယ်ရှင့်။",
            )
            return
        if existing_chat is None:
            conn.execute(
                "UPDATE orders SET customer_chat_id=? WHERE id=?",
                (chat_id, order_id),
            )
            conn.commit()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"အော်ဒါ {order_no} ကို Telegram နဲ့ ချိတ်ပြီးပါပြီရှင့်။ ပို့ခ အတည်ပြုစာရွက် (✅/❌ ခလုတ်) ခဏနေ ဒီမှာ ရောက်လာပါမယ်ရှင့်။",
        )
    except Exception:
        logger.exception("order link: failed for %s", order_no)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # v19.58 deep link (Min 2026-11-21): /start ord_<order_no> from the
    # app's "Confirm on Telegram" button links this Telegram chat to a
    # phone/browser order so the bot can send the fee card here. The
    # payload is carried through the language picker (new users) in
    # user_data and linked after the welcome.
    args = context.args or []
    if args and isinstance(args[0], str) and args[0].startswith("ord_"):
        context.user_data["link_order_no"] = args[0][4:]
    # First /start: ask the language first (Part 1, option A).
    # Returning users go straight to the welcome in their language.
    lang = get_user_lang(update.effective_chat.id)
    if lang is None:
        await update.effective_message.reply_text(
            LANG_PICKER_TEXT, reply_markup=lang_picker_kb()
        )
        return ConversationHandler.END
    await _send_welcome(update.effective_message, context, lang)
    await _maybe_link_order(update, context)
    return ConversationHandler.END


async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the language tapped on /start (or /lang) and show the welcome."""
    q = update.callback_query
    await safe_answer(q)
    data = str(q.data or "")
    lang = data.split(":", 1)[1] if ":" in data else ""
    if lang not in LANGS:
        await q.message.reply_text(
            LANG_PICKER_TEXT, reply_markup=lang_picker_kb()
        )
        return
    set_user_lang(update.effective_chat.id, lang)
    await _send_welcome(q.message, context, lang)
    # v19.58: a deep-link payload carried through the picker links now.
    await _maybe_link_order(update, context)


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let a user re-pick their language any time with /lang."""
    await update.effective_message.reply_text(
        LANG_PICKER_TEXT, reply_markup=lang_picker_kb()
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"သင့် Telegram chat id: {update.effective_chat.id}"
    )


async def close_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner only (Min, 2026-11-12 02:27): force the shop CLOSED right
    # now, even during open hours (e.g. when he wants to stop for
    # the day). The override holds until the next scheduled open/close
    # boundary, then the schedule takes over again. A manual close
    # blocks everyone, including the owner — v19.15: it is the ONLY
    # thing that blocks orders now (ordering is 24/7 otherwise).
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return  # owner only, stay silent for others
    now = datetime.now(YANGON_TZ)
    set_shop_override("closed", now)
    until = next_schedule_transition(now)
    await update.effective_message.reply_text(
        "🔒 ဆိုင်ပိတ်လိုက်ပါပြီ။ အော်ဒါ အသစ် လက်မခံတော့ပါ။\n"
        f"{format_until(until, now)} အထိ ပိတ်ထားပါမယ် — ပြီးရင် ဖွင့်ချိန် အတိုင်း ပြန်ဖြစ်ပါမယ်ရှင့်။\n"
        "ပြန်ဖွင့်ချင်ရင် /open ကို နှိပ်ပါ။"
    )


async def open_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner only (Min, 2026-11-12 02:27): force the shop OPEN right
    # now (v19.15: ordering is 24/7 anyway — /open now only lifts a
    # manual close / forces the delivery-open state). The override
    # holds until the next scheduled open/close boundary, then the
    # schedule takes over again.
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return  # owner only, stay silent for others
    now = datetime.now(YANGON_TZ)
    set_shop_override("open", now)
    until = next_schedule_transition(now)
    await update.effective_message.reply_text(
        "🔓 ဆိုင်ဖွင့်လိုက်ပါပြီ။ အော်ဒါ ပြန်လက်ခံပါမယ်။\n"
        f"{format_until(until, now)} အထိ ဖွင့်ထားပါမယ် — ပြီးရင် ဖွင့်ချိန် အတိုင်း ပြန်ဖြစ်ပါမယ်ရှင့်။\n"
        f"{hours_line()}"
    )


async def set_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner only (Min, 2026-11-12 02:27): set the daily DELIVERY
    # window, e.g. /sethours 06:00 23:00. v19.15: ordering is 24/7 —
    # this no longer sets "when you can order", only when delivery
    # runs. Same-day ranges only (open < close); overnight ranges
    # are rejected. Persists to SHOP_HOURS_FILE (runtime state,
    # never shipped in the deploy ZIP, so a redeploy never
    # overwrites the owner's setting).
    if not _is_owner(update):
        return  # owner only, stay silent for others
    args = list(context.args or [])
    if len(args) != 2:
        await update.effective_message.reply_text(
            "အသုံးပြုပုံ: /sethours 06:00 22:00\n"
            "ဖွင့်ချိန်နဲ့ ပိတ်ချိန်ကို နာရီ:မိနစ် နဲ့ ရေးပေးပါနော်။"
        )
        return
    open_min = parse_hhmm(args[0])
    close_min = parse_hhmm(args[1])
    if open_min is None or close_min is None:
        await update.effective_message.reply_text(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 အချိန် ပုံစံ မမှန်ပါဘူးရှင့်။\n"
            "ဥပမာ — /sethours 06:00 22:00 လို့ ရေးပေးပါနော်။"
        )
        return
    if open_min == close_min:
        await update.effective_message.reply_text(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 ဖွင့်ချိန်နဲ့ ပိတ်ချိန် တူလို့ မရပါဘူးရှင့်။\n"
            "ဥပမာ — /sethours 06:00 22:00 လို့ ရေးပေးပါနော်။"
        )
        return
    if open_min > close_min:
        await update.effective_message.reply_text(
            "တောင်းပန်ပါတယ်ရှင့်။ 🙏 ညနက်ပိုင်း ကျော်တဲ့ အချိန် မရသေးပါဘူးရှင့်။\n"
            "ဖွင့်ချိန်က ပိတ်ချိန်ထက် စောရပါမယ် — ဥပမာ /sethours 06:00 22:00 ပါ။"
        )
        return
    set_shop_hours(
        f"{open_min // 60:02d}:{open_min % 60:02d}",
        f"{close_min // 60:02d}:{close_min % 60:02d}",
    )
    now = datetime.now(YANGON_TZ)
    override = active_override(now)
    extra = ""
    if override == "open":
        extra = (
            "\nလက်ရှိ လက်နဲ့ ဖွင့်ထားတာ "
            f"{format_until(next_schedule_transition(now), now)} အထိ "
            "ရှိနေပါသေးတယ်ရှင့်။"
        )
    elif override == "closed":
        extra = (
            "\nလက်ရှိ လက်နဲ့ ပိတ်ထားတာ "
            f"{format_until(next_schedule_transition(now), now)} အထိ "
            "ရှိနေပါသေးတယ်ရှင့်။"
        )
    await update.effective_message.reply_text(
        "✅ ပို့ဆောင်ချိန် သတ်မှတ်ပြီးပါပြီ။\n"
        f"{hours_line()}\n"
        "အော်ဒါက ၂၄ နာရီ လက်ခံပါမယ် — အဲဒီအချိန်အတွင်း ပို့ဆောင်ပေးပါမယ်ရှင့်။"
        f"{extra}"
    )


async def shop_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner only (Min, 2026-11-12 02:27): show the set hours, the
    # current open/closed state, and any live manual override.
    # NOT in the public command list (owner-only, like /open /close).
    if not _is_owner(update):
        return  # owner only, stay silent for others
    now = datetime.now(YANGON_TZ)
    open_min, close_min = _shop_hours_minutes()
    state = "ဖွင့်ထားပါတယ်" if is_open_now(now) else "ပိတ်ထားပါတယ်"
    override = active_override(now)
    if override == "open":
        ov = (
            "\n🔓 လက်နဲ့ ဖွင့်ထားပါတယ် — "
            f"{format_until(next_schedule_transition(now), now)} အထိ။ "
            "ပြီးရင် ဖွင့်ချိန် အတိုင်း ပြန်ဖြစ်ပါမယ်။"
        )
    elif override == "closed":
        ov = (
            "\n🔒 လက်နဲ့ ပိတ်ထားပါတယ် — "
            f"{format_until(next_schedule_transition(now), now)} အထိ။ "
            "ပြီးရင် ဖွင့်ချိန် အတိုင်း ပြန်ဖြစ်ပါမယ်။"
        )
    else:
        ov = "\nဖွင့်ချိန် အတိုင်း လည်ပတ်နေပါတယ်။"
    await update.effective_message.reply_text(
        "🕕 ဆိုင် ဖွင့်ချိန်\n\n"
        f"ဖွင့်ချိန်: {format_my_time(open_min)} — "
        f"ပိတ်ချိန်: {format_my_time(close_min)}\n"
        f"အခု အခြေအနေ: {state}"
        f"{ov}"
    )


async def menu_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    lang = get_user_lang(q.message.chat.id) or DEFAULT_LANG
    if q.data == "show_phone":
        await q.message.reply_text(phone_text(lang))
    elif q.data == "show_price":
        await q.message.reply_text(price_text(lang))
    elif q.data == "show_lang_picker":
        # Same picker as /lang (Min, 2026-11-12 18:52): the 🌐 Language
        # button on the main menu. Tapping a language there saves it
        # via choose_language and re-shows the welcome.
        await q.message.reply_text(LANG_PICKER_TEXT, reply_markup=lang_picker_kb())
    elif q.data == "help_location":
        await q.message.reply_text(help_location_text(lang))


async def location_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Answer the tap first, even when the shop is closed — otherwise
    # the button keeps spinning and the user sees nothing happen.
    if update.callback_query:
        await safe_answer(update.callback_query)
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    if not can_order_now(update):
        await update.effective_message.reply_text(closed_text(lang=lang))
        return ConversationHandler.END
    # Min asked 2026-11-05: tapping Location should send the user's
    # current location straight away. Telegram inline buttons cannot
    # send a location by themselves — only a ReplyKeyboard button with
    # request_location=True can. So tapping Location now shows that
    # button immediately: one more tap sends the pin, and the order
    # continues from there (pickup -> dropoff -> name -> phone -> item).
    context.user_data.clear()
    context.user_data["flow"] = "location_first"
    await update.effective_message.reply_text(
        location_text(lang)
        + "\n\n"
        + order_step_text(
            "location_begin_prompt",
            lang,
            step=step_label(lang, 1),
            loc_btn=location_button_text(lang),
            addr_btn=address_button_text(lang),
        ),
        reply_markup=location_kb(lang=lang),
        parse_mode="Markdown",
    )
    return PICKUP


async def order_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Answer the tap first, even when the shop is closed — otherwise
    # the button keeps spinning and the user sees nothing happen.
    if update.callback_query:
        await safe_answer(update.callback_query)
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    if not can_order_now(update):
        await update.effective_message.reply_text(closed_text(lang=lang))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["flow"] = "normal"
    # New form Min asked for: name first, then phone, then places, then item.
    # The intro speaks the user's saved language (Min, 2026-11-12 09:41).
    await update.effective_message.reply_text(
        lang_text(ORDER_INTRO_TEXTS, lang),
        parse_mode="Markdown",
    )
    return NAME


async def name_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["customer_name"] = (update.effective_message.text or "").strip()
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    # Step numbers follow the REAL order, which differs by flow:
    # normal flow asks phone 2nd, location-first flow asks it 4th
    # (locations are already done there).
    if context.user_data.get("flow") == "location_first":
        step_no, next_key = 4, "item"
    else:
        step_no, next_key = 2, "pickup"
    await update.effective_message.reply_text(
        order_step_text(
            "phone_prompt",
            lang,
            step=step_label(lang, step_no),
            next=step_next_name(lang, next_key),
        ),
        parse_mode="Markdown",
    )
    return SENDER_PHONE


async def sender_phone_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    phone = normalize_mm_phone(update.effective_message.text or "")
    if not phone:
        await update.effective_message.reply_text(
            order_step_text("phone_invalid", lang)
        )
        return SENDER_PHONE
    context.user_data["sender_phone"] = phone
    if context.user_data.get("flow") == "location_first":
        # Pickup and drop-off are already done in this flow.
        await update.effective_message.reply_text(
            order_step_text("item_prompt", lang, step=step_label(lang, 5)),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return ITEM
    await update.effective_message.reply_text(
        order_step_text(
            "pickup_prompt",
            lang,
            step=step_label(lang, 3),
            addr_btn=address_button_text(lang),
            loc_btn=location_button_text(lang),
        ),
        reply_markup=location_kb(lang=lang),
        parse_mode="Markdown",
    )
    return PICKUP


def read_place(update: Update):
    msg = update.effective_message
    if msg.location:
        return {
            "type": "pin",
            "lat": msg.location.latitude,
            "lon": msg.location.longitude,
        }
    return {"type": "text", "text": (msg.text or "").strip()}


async def _handle_location_input(update: Update, context, which: str) -> bool:
    """Shared input logic for the PICKUP / DROPOFF steps.

    Returns True when a place was accepted and stored in
    ``context.user_data[which]`` (via the existing ``read_place``
    path); False when the step is NOT complete and the bot only asked
    for the exact address — the caller must stay in the same state.

    - A location pin is always accepted, even after the address
      button was tapped.
    - Tapping the "📝 လိပ်စာ အတိအကျ ရေးပို့မယ်" button, or sending
      any text instead of a pin, asks for the exact written address
      (ward / street / house number / nearby landmark) and waits.
    - While waiting, the next text is accepted as the address — but
      only when it is not empty; an empty message is asked again
      politely instead of continuing. A pin or an address is never
      invented.
    """
    msg = update.effective_message
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    addr_btn = address_button_text(lang)
    key = "awaiting_address_" + which
    if msg.location:
        context.user_data[which] = read_place(update)
        context.user_data.pop(key, None)
        return True
    text = (msg.text or "").strip()
    if context.user_data.get(key):
        # We already asked for the exact address.
        if text and text != addr_btn:
            context.user_data[which] = read_place(update)
            context.user_data.pop(key, None)
            return True
        # Empty text, or the button tapped again: ask again, stay.
        prompt = (
            address_ask_text(lang)
            if text == addr_btn
            else address_empty_text(lang)
        )
        await msg.reply_text(prompt, reply_markup=location_kb(lang=lang))
        return False
    # First text at this step (button tap or a typed message):
    # ask for the exact written address and wait for it.
    context.user_data[key] = True
    await msg.reply_text(address_ask_text(lang), reply_markup=location_kb(lang=lang))
    return False


async def pickup_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _handle_location_input(update, context, "pickup"):
        return PICKUP
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    # Step numbers follow the REAL order, which differs by flow:
    # normal flow asks drop-off 4th, location-first flow asks it 2nd.
    if context.user_data.get("flow") == "location_first":
        step_no, next_key = 2, "name"
    else:
        step_no, next_key = 4, "item"
    await update.effective_message.reply_text(
        order_step_text(
            "dropoff_prompt",
            lang,
            step=step_label(lang, step_no),
            next=step_next_name(lang, next_key),
            addr_btn=address_button_text(lang),
            loc_btn=location_button_text(lang),
        ),
        reply_markup=location_kb(lang=lang),
        parse_mode="Markdown",
    )
    return DROPOFF


async def dropoff_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _handle_location_input(update, context, "dropoff"):
        return DROPOFF
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    if context.user_data.get("flow") == "location_first":
        await update.effective_message.reply_text(
            order_step_text("name_prompt", lang, step=step_label(lang, 3)),
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return NAME
    await update.effective_message.reply_text(
        order_step_text("item_prompt", lang, step=step_label(lang, 5)),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return ITEM


async def item_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["item"] = (update.effective_message.text or "").strip()
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(order_step_text("photo_skip_btn", lang), callback_data="photo_skip")]]
    )
    await update.effective_message.reply_text(
        order_step_text("photo_prompt", lang, step=step_label(lang, 5)),
        reply_markup=kb,
    )
    return PHOTO


async def photo_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message.photo:
        context.user_data["photo_file_id"] = update.effective_message.photo[-1].file_id
    return await ask_confirm(update, context)


async def photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_answer(update.callback_query)
    return await ask_confirm(update, context, via_callback=True)


async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback=False):
    order = {
        "customer_name": context.user_data.get("customer_name", ""),
        "sender_phone": context.user_data.get("sender_phone", ""),
        "pickup": context.user_data["pickup"],
        "dropoff": context.user_data["dropoff"],
        "item": context.user_data.get("item", ""),
    }
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(order_step_text("confirm_yes_btn", lang), callback_data="confirm_yes", style="success"),
                InlineKeyboardButton(order_step_text("confirm_no_btn", lang), callback_data="confirm_no", style="danger"),
            ]
        ]
    )
    text = format_summary(order, lang=lang) + "\n\n" + order_step_text("confirm_question", lang)
    if via_callback:
        await update.callback_query.message.reply_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return CONFIRM


async def confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    # M-8: per-chat in-flight guard — two rapid taps on ✅ must not
    # save two orders. A second invocation while the first is still
    # running answers the callback and returns WITHOUT ending the
    # conversation, so the in-flight invocation finishes it. The
    # `is True` check (not truthiness) keeps this safe when
    # user_data is a mock in tests. The flag is popped in a finally
    # block, so a raising save_order cannot wedge it forever;
    # user_data.clear() on the happy path removes it anyway.
    if context.user_data.get("_confirm_in_flight") is True:
        return None
    context.user_data["_confirm_in_flight"] = True
    try:
        order = {
            "customer_name": context.user_data.get("customer_name", ""),
            "customer_chat_id": update.effective_chat.id,
            "sender_phone": context.user_data.get("sender_phone", ""),
            "pickup": context.user_data["pickup"],
            "dropoff": context.user_data["dropoff"],
            "item": context.user_data.get("item", ""),
            "photo_file_id": context.user_data.get("photo_file_id"),
        }
        order_no, _is_replay = save_order(order)
        lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
        await q.message.reply_text(order_received_text(lang, order_no))
        if OWNER_CHAT_ID:
            card = (
                f"🔔 Order အသစ် {order_no}\n"
                f"မှာသူ: {order['customer_name']}\n"
                + format_summary(order, order_no)
            )
            # M-6: the owner send is guarded — a Telegram failure
            # must not crash the handler after the customer was
            # already confirmed, and the conversation must still
            # end cleanly below.
            try:
                await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=card)
                if order.get("photo_file_id"):
                    await context.bot.send_photo(
                        chat_id=int(OWNER_CHAT_ID), photo=order["photo_file_id"]
                    )
            except Exception:
                logger.exception(
                    "bot-chat order: owner card failed for %s", order_no
                )
        # Also tell the shop (Part 2) — bot-chat orders have no shop, so
        # this is a no-op for them; it never blocks the customer flow.
        try:
            await notify_shop_of_order(context, order, order_no)
        except Exception:
            pass
    finally:
        context.user_data.pop("_confirm_in_flight", None)
    context.user_data.clear()
    return ConversationHandler.END


async def confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_answer(update.callback_query)
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    await update.callback_query.message.reply_text(
        order_step_text("confirm_cancel", lang)
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_chat.id) or DEFAULT_LANG
    await update.effective_message.reply_text(
        order_step_text("cancel_text", lang),
        reply_markup=shop_kb(lang),
    )
    context.user_data.clear()
    return ConversationHandler.END



async def begin_customer_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let the owner reply to a customer by tapping a button on the forwarded notice."""
    query = update.callback_query
    if str(query.message.chat_id) != str(OWNER_CHAT_ID):
        await query.answer("ခွင့်ပြုချက်မရှိပါ။", show_alert=True)
        return
    data = str(query.data or "")
    prefix = "cust_reply:"
    if not data.startswith(prefix) or not data[len(prefix):].lstrip("-").isdigit():
        await query.answer("Customer အချက်အလက် မမှန်ပါ။", show_alert=True)
        return
    customer_chat_id = int(data[len(prefix):])
    if customer_chat_id == int(OWNER_CHAT_ID):
        await query.answer("ဒီ Chat ကို ပြန်ဖြေလို့မရပါ။", show_alert=True)
        return
    context.user_data["reply_to_customer_chat_id"] = customer_chat_id
    await query.answer()
    # v19.56: show the customer's name/phone, not a raw chat id.
    _who = str(customer_chat_id)
    try:
        _conn = db_connect()
        try:
            _r = _conn.execute(
                "SELECT customer_name, sender_phone FROM orders WHERE customer_chat_id=? ORDER BY id DESC LIMIT 1",
                (customer_chat_id,),
            ).fetchone()
        finally:
            _conn.close()
        if _r and (_r[0] or _r[1]):
            _who = " ".join(p for p in (_r[0], _r[1]) if p)
    except Exception:
        pass
    await query.message.reply_text(
        f"✍️ Customer ({_who}) ကို ပြန်စာရေးပို့ပါ။\n"
        "မပို့တော့လိုပါက /cancelreply ကိုနှိပ်ပါ။"
    )


async def send_pending_customer_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the owner's next text to the customer selected with the Reply button."""
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return
    customer_chat_id = context.user_data.get("reply_to_customer_chat_id")
    if not customer_chat_id:
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("စာသားတစ်ခု ရေးပို့ပါ။")
        return
    try:
        await context.bot.send_message(
            chat_id=int(customer_chat_id),
            text=owner_reply_for_customer(text, customer_chat_id),
        )
    except Exception:
        await message.reply_text(
            "❌ Customer ဆီ စာမပို့နိုင်ပါ။ Customer က Bot ကို /start လုပ်ထားကြောင်း စစ်ပါ။"
        )
        return
    context.user_data.pop("reply_to_customer_chat_id", None)
    await message.reply_text("✅ Customer ဆီ စာပို့ပြီးပါပြီ။")


async def cancel_customer_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return
    context.user_data.pop("reply_to_customer_chat_id", None)
    await update.effective_message.reply_text("ပြန်စာရေးခြင်းကို ပယ်ဖျက်လိုက်ပါပြီ။")


async def reply_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to reply to a customer from the admin's own Telegram chat.

    Usage: /reply <customer_chat_id> <message>
    Customer chat IDs are included in forwarded customer-message notices.
    """
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return
    if len(context.args) < 2 or not str(context.args[0]).lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "အသုံးပြုပုံ: /reply CUSTOMER_CHAT_ID စာသား\n"
            "ဥပမာ: /reply 123456789 မင်္ဂလာပါ၊ ဘာကူညီပေးရမလဲ။"
        )
        return
    customer_chat_id = int(context.args[0])
    if customer_chat_id == int(OWNER_CHAT_ID):
        await update.effective_message.reply_text("ကိုယ့်အကောင့်ကို ကိုယ်ပြန်ပို့လို့မရပါ။")
        return
    message_text = " ".join(str(x) for x in context.args[1:]).strip()
    if not message_text:
        await update.effective_message.reply_text("ပို့မယ့်စာသား မရှိသေးပါ။")
        return
    try:
        await context.bot.send_message(
            chat_id=customer_chat_id,
            text=owner_reply_for_customer(message_text, customer_chat_id),
        )
    except Exception:
        await update.effective_message.reply_text(
            "❌ Customer ဆီ စာမပို့နိုင်ပါ။ Customer က Bot ကို အရင်ဆုံး /start လုပ်ထားကြောင်း စစ်ပါ။"
        )
        return
    await update.effective_message.reply_text("✅ Customer ဆီ စာပို့ပြီးပါပြီ။")


async def orders_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return  # owner only, stay silent for others
    rows = today_orders()
    if not rows:
        await update.effective_message.reply_text("ဒီနေ့ order မရှိသေးပါ။")
        return
    lines = [f"📋 ဒီနေ့ order ({len(rows)})"]
    for no, phone, item in rows:
        lines.append(f"{no} • {phone} • {item}")
    await update.effective_message.reply_text("\n".join(lines))


async def shops_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return  # owner only, stay silent for others
    rows = pending_shops()
    if not rows:
        await update.effective_message.reply_text("စာရင်းသွင်းထားတဲ့ ဆိုင် မရှိသေးပါ။")
        return
    total = len(rows)
    lines = [f"🏪 ဆိုင်စာရင်း ({total})"]
    for reg_id, shop, owner, phone, lat, lon in rows[:30]:
        loc = "📍✅" if lat is not None and lon is not None else "📍❌"
        lines.append(f"#{reg_id} • {shop} • {owner} • {phone} • {loc}")
    if total > 30:
        lines.append(f"…နောက်ထပ် {total - 30} ဆိုင် ရှိသေးတယ်။")
    await update.effective_message.reply_text("\n".join(lines))


async def shop_registration_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only detail view for one shop application."""
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /shop ID  (ဥပမာ /shop 12)")
        return
    row = get_shop_registration(int(context.args[0]))
    if not row:
        await update.effective_message.reply_text("ဒီဆိုင်မှတ်ပုံတင်ကို မတွေ့ပါ။")
        return
    (rid, created, shop, owner, chat_id, phone, address, category, note,
     owner_message, lat, lon, map_link, status, menu_json, hours_json,
     photos_confirmed, photos_note, video_file_id, video_duration,
     video_link) = row
    lines = [f"🏪 ဆိုင်မှတ်ပုံတင် #{rid}", f"ဆိုင်: {shop}", f"ဆိုင်ရှင်: {owner}",
             f"ဖုန်း: {phone}", f"အမျိုးအစား: {category or '-'}",
             f"အခြေအနေ: {status}", f"လိပ်စာ: {address or '-'}"]
    if lat is not None and lon is not None:
        lines.append(f"📍 တည်နေရာ: https://maps.google.com/?q={lat},{lon}")
    elif map_link:
        lines.append(f"📍 မြေပုံ: {map_link}")
    if note:
        lines.append(f"မှတ်ချက်: {note}")
    if owner_message:
        lines.append(f"ဆိုင်ရှင်စာ: {owner_message}")
    # Menu + hours + photos + guide video (v5): stored at registration,
    # shown here so the admin can verify them before listing the shop.
    menu = None
    if menu_json:
        try:
            menu = json.loads(menu_json)
        except (TypeError, ValueError):
            menu = None
    if isinstance(menu, dict) and menu.get("items"):
        lines.append("📋 Menu:")
        for item in menu["items"][:10]:
            lines.append(f"• {item.get('name', '')} — {item.get('price_baht', '')} ဘတ်")
        if len(menu["items"]) > 10:
            lines.append(f"• …နောက်ထပ် {len(menu['items']) - 10} မျိုး")
    elif isinstance(menu, dict) and menu.get("text"):
        lines.append(f"📋 Menu စာ: {menu['text'][:200]}")
    hours_text = ""
    if hours_json:
        try:
            hours_text = _format_shop_hours(json.loads(hours_json))
        except (TypeError, ValueError):
            hours_text = ""
    if hours_text:
        lines.append(f"🕐 ဖွင့်ချိန်: {hours_text}")
    lines.append(
        "📸 ဓာတ်ပုံ ၃ ပုံ: "
        + ("✅ အတည်ပြုပြီး" if photos_confirmed else "❌ မအတည်ပြုရသေးပါ")
    )
    if photos_note:
        lines.append(f"📸 မှတ်ချက်: {photos_note}")
    if video_file_id:
        duration_text = f" ({video_duration} စက္ကန့်)" if video_duration else ""
        lines.append(f"🎬 လမ်းညွှန် ဗီဒီယို: ✅ ရှိပါတယ်{duration_text}")
    else:
        lines.append("🎬 လမ်းညွှန် ဗီဒီယို: ❌ မရှိသေးပါ")
    if video_link:
        lines.append(f"🔗 Video လင့်ခ်: {video_link}")
    lines.append("စစ်ဆေးပြီးရင် /shopverify ID သို့မဟုတ် /shopreject ID ကို သုံးပါ။")
    await update.effective_message.reply_text("\n".join(lines))


async def _review_shop_registration(update, context, status):
    if str(update.effective_chat.id) != str(OWNER_CHAT_ID):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /shopverify ID" if status == "verified" else "အသုံးပြုပုံ: /shopreject ID")
        return
    reg_id = int(context.args[0])
    row = get_shop_registration(reg_id)
    if not row:
        await update.effective_message.reply_text("ဒီဆိုင်မှတ်ပုံတင်ကို မတွေ့ပါ။")
        return
    if not set_shop_registration_status(reg_id, status):
        await update.effective_message.reply_text("ဒီမှတ်ပုံတင်ကို စစ်ဆေးပြီးသား သို့မဟုတ် အခြေအနေပြောင်းထားပြီးပါပြီ။")
        return
    label = "အချက်အလက် စစ်ဆေးအတည်ပြုပြီးပါပြီရှင့်။ ဆိုင်ဖွင့်ခွင့်ရဖို့ menu နဲ့ ဆိုင်ပစ္စည်းတွေကို Admin က ဆက်လက်စစ်ဆေးပေးပါမယ်ရှင့်။" if status == "verified" else "တောင်းပန်ပါတယ်ရှင့်။ လက်ရှိမှတ်ပုံတင်ကို လက်မခံနိုင်သေးပါဘူး။ အသေးစိတ်အတွက် Safe Deli Admin ကို ဆက်သွယ်ပေးပါနော်။"
    await update.effective_message.reply_text(f"✅ ဆိုင်မှတ်ပုံတင် #{reg_id} — {label}")
    owner_chat_id = row[4]
    if owner_chat_id:
        try:
            await context.bot.send_message(chat_id=int(owner_chat_id), text=f"🏪 {row[2]} ဆိုင်မှတ်ပုံတင်အခြေအနေ\n{label}")
        except Exception:
            # Preserve the review decision even if Telegram cannot deliver the notice.
            pass


async def shop_verify(update, context):
    await _review_shop_registration(update, context, "verified")


async def shop_reject(update, context):
    await _review_shop_registration(update, context, "rejected")


# ---------------------------------------------------------------- rider admin commands
def _is_owner(update) -> bool:
    return str(update.effective_chat.id) == str(OWNER_CHAT_ID)


async def deliver_command(update, context):
    """Admin: /deliver SD-YYYYMMDD-### — create the delivery for
    an order and auto-assign the nearest rider. The manual case:
    bot-chat orders (no shop to confirm) or a delivery that lost
    its rider. Refuses when the shop already rejected the order.
    """
    if not _is_owner(update):
        return
    if not context.args:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /deliver SD-20261111-001")
        return
    order_no = str(context.args[0]).strip()
    order = get_order_by_no(order_no)
    if not order:
        await update.effective_message.reply_text("ဒီအော်ဒါကို မတွေ့ပါ။")
        return
    # Test orders are never delivered (2026-11-12, v11 follow-up):
    # a sample-shop test must not dispatch a rider. Refuse politely.
    if order[17]:
        await update.effective_message.reply_text(
            f"🧪 {order_no} က စမ်းသပ်အော်ဒါ (TEST) ဖြစ်ပါတယ်ရှင့်။ "
            "Delivery ဖန်တီးမပေးပါ။"
        )
        return
    if order[15] == "shop_rejected":
        await update.effective_message.reply_text(
            f"❌ ဆိုင်က {order_no} ကို လက်မခံနိုင်လို့ Delivery ဖန်တီးမပေးပါ။"
        )
        return
    active = get_active_delivery_for_order(order[0])
    if active:
        rider = active.get("rider_name") or "Rider မခိုင်းရသေးပါ"
        await update.effective_message.reply_text(
            f"Delivery #{active['id']} ({order_no}) ရှိနေပါပြီ — "
            f"အခြေအနေ: {active['status']}၊ Rider: {rider}"
        )
        return
    result = await create_and_assign_delivery(
        context, order_no, actor_type="admin",
        actor_id=str(update.effective_chat.id), notify_owner=False)
    if result["error"]:
        await update.effective_message.reply_text(f"⚠️ {result['error']}")
        return
    if result["rider"]:
        await update.effective_message.reply_text(
            f"✅ Delivery #{result['delivery_id']} ({order_no}) — "
            f"Rider {result['rider']['name']} ကို အလုပ် ပို့ပေးပြီးပါပြီ။"
        )
    else:
        await update.effective_message.reply_text(
            f"✅ Delivery #{result['delivery_id']} ({order_no}) ဖန်တီးပြီးပါပြီ — "
            "Rider မရှိသေးလို့ ဘယ်သူ့ကိုမှ မခိုင်းရသေးပါ။"
        )


async def addrider_command(update, context):
    """Admin: /addrider Name, 09123456789, 123456789 — add a rider
    (approved at once). The rider first sends /myid to the bot so
    Min knows their chat id. An existing chat id updates the
    name/phone instead of duplicating the rider.
    """
    if not _is_owner(update):
        return
    text = " ".join(context.args or "").strip()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3 or not parts[0] or not parts[2]:
        await update.effective_message.reply_text(
            "အသုံးပြုပုံ: /addrider နာမည်, ဖုန်း, chat id\n"
            "ဥပမာ: /addrider မောင်မောင်, 09123456789, 123456789\n"
            "Rider က /myid ကို Bot မှာ ပို့ပြီး chat id ကို ပြောပြပါ။"
        )
        return
    name, phone_raw, chat_raw = parts[0], parts[1], parts[2]
    phone = normalize_mm_phone(phone_raw) if phone_raw else ""
    if phone_raw and not phone:
        await update.effective_message.reply_text(
            "ဖုန်းနံပါတ် ပုံစံ မမှန်ပါ။ 09 နဲ့ စတဲ့ နံပါတ်ကို ရေးပေးပါ။"
        )
        return
    try:
        chat_id = int(chat_raw)
    except ValueError:
        await update.effective_message.reply_text("chat id က ဂဏန်းပဲ ဖြစ်ရပါမယ်။")
        return
    existing = get_rider_by_chat(chat_id)
    if existing:
        conn = db_connect()
        try:
            conn.execute(
                "UPDATE riders SET name=?, phone=? WHERE chat_id=?",
                (name, phone, chat_id),
            )
            conn.commit()
        finally:
            conn.close()
        await update.effective_message.reply_text(
            f"✅ Rider #{existing['id']} — {name} ({phone or '-'}) "
            f"အချက်အလက် ပြင်ပြီးပါပြီ။ အခြေအနေ: {existing['status']}"
        )
        return
    try:
        rider_id = save_rider(
            {"name": name, "phone": phone, "chat_id": chat_id,
             "status": "approved"}
        )
    except ValueError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return
    await update.effective_message.reply_text(
        f"✅ Rider #{rider_id} — {name} ({phone or '-'}) "
        "စာရင်းသွင်းပြီးပါပြီ။ အလုပ် ခိုင်းလို့ ရပါပြီ။"
    )


async def riders_command(update, context):
    """Admin: /riders — list all riders with status + last job."""
    if not _is_owner(update):
        return
    riders = list_riders()
    if not riders:
        await update.effective_message.reply_text(
            "Rider မရှိသေးပါ။ /addrider နဲ့ ထည့်ပေးပါ။"
        )
        return
    lines = ["🛵 Rider စာရင်း:"]
    for r in riders:
        lines.append(
            f"#{r['id']} — {r['name']} ({r['phone'] or '-'}) — "
            f"{r['status']} — chat {r['chat_id']}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def _rider_toggle(update, context, status, label):
    if not _is_owner(update):
        return
    cmd = "rideroff" if status == "suspended" else "rideron"
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"အသုံးပြုပုံ: /{cmd} ID")
        return
    rider_id = int(context.args[0])
    rider = get_rider(rider_id)
    if not rider:
        await update.effective_message.reply_text("ဒီ Rider ကို မတွေ့ပါ။")
        return
    set_rider_status(rider_id, status)
    await update.effective_message.reply_text(
        f"✅ Rider #{rider_id} ({rider['name']}) — {label}"
    )


async def rideroff_command(update, context):
    """Admin: /rideroff ID — suspend a rider (never assigned)."""
    await _rider_toggle(
        update, context, "suspended",
        "ယာယီ ရပ်ထားပါပြီ။ အလုပ် မခိုင်းတော့ပါ။",
    )


async def rideron_command(update, context):
    """Admin: /rideron ID — re-approve a suspended rider."""
    await _rider_toggle(
        update, context, "approved",
        "ပြန်ဖွင့်ပေးပါပြီ။ အလုပ် ခိုင်းလို့ ရပါပြီ။",
    )


async def _handle_rider_location(update, context, rider, loc):
    """Record a registered rider's location pin.

    With an active delivery the pin is a live position for that
    delivery (source 'telegram_live' for a live share, 'manual'
    for a one-off pin) — the dashboard reads the same
    rider_positions table. Without an active delivery it only
    updates the rider's base pin. Edited live-location updates
    are silent (they arrive every few seconds); only the first
    share gets a short confirmation. Returns True when a position
    was stored for an active delivery.
    """
    lat, lon = loc.latitude, loc.longitude
    accuracy = getattr(loc, "horizontal_accuracy", None)
    if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)):
        accuracy = None
    source = "telegram_live" if getattr(loc, "live_period", None) else "manual"
    chat_id = rider["chat_id"]
    active = get_active_delivery_for_rider(chat_id)
    stored = False
    if active:
        try:
            record_rider_position(active["id"], lat, lon, accuracy, source)
            stored = True
        except ValueError:
            stored = False
    set_rider_base_location(chat_id, lat, lon)
    if (getattr(update, "edited_message", None) is None
            and getattr(update, "message", None) is not None):
        try:
            if active and stored:
                await update.effective_message.reply_text(
                    "📍 တည်နေရာ လက်ခံရရှိပါပြီရှင့်။ "
                    "Delivery လမ်းကြောင်းမှာ Admin က မြင်ရပါမယ်ရှင့်။"
                )
            elif active:
                await update.effective_message.reply_text(
                    "📍 တည်နေရာ လက်ခံရရှိပါပြီရှင့်။"
                )
            else:
                await update.effective_message.reply_text(
                    "📍 တည်နေရာ လက်ခံရရှိပါပြီရှင့်။ "
                    "(အခု အလုပ် မရှိသေးပါဘူး — နောက် အလုပ်မှာ သုံးပါမယ်ရှင့်။)"
                )
        except Exception:
            pass
    return stored


def _customer_label(update):
    """Display name for a customer message forwarded to Min.

    Prefers the sender's first name + @username; falls back to
    "ဖောက်သည်" when Telegram gives nothing usable (and in tests,
    where these are MagicMock objects, not strings).
    """
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    name = ""
    username = ""
    for src in (user, chat):
        if src is None:
            continue
        if not name:
            fn = getattr(src, "first_name", None)
            if isinstance(fn, str) and fn:
                name = fn
        if not username:
            un = getattr(src, "username", None)
            if isinstance(un, str) and un:
                username = un
        if name and username:
            break
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or "ဖောက်သည်"


# ---------------------------------------------------------------- facts file
# Owner-confirmed facts only (Min, 2026-11-03 / 2026-11-05): hours,
# delivery from 30 baht, Tachileik, phone, Telegram. The prices list
# starts EMPTY in safedeli_facts.json — the owner fills it; the bot
# NEVER invents a price. Unknown items fall back to the admin forward.
FACTS_FILE = os.path.join(BASE_DIR, "safedeli_facts.json")
# No "hours" here: the opening hours live only in shop_hours.json
# (get_shop_hours, set with /sethours). The facts file used to
# carry them, which let the hours answer disagree with the shop
# after a /sethours change — that second source is now gone.
DEFAULT_FACTS = {
    "delivery_min_baht": 30,
    "area": "Tachileik",
    "area_names": {
        "my": "တာချီလိတ်",
        "th": "ท่าขี้เหล็ก",
        "en": "Tachileik",
        
        "zh": "大其力",
    },
    "phone": "09441582958",
    "phone_display": "09 441 582 958",
    "telegram": "@SafeDeliveryone",
    "prices": [],
}
_facts_cache = None


def load_facts():
    """Read safedeli_facts.json (cached). Falls back to DEFAULT_FACTS
    when the file is missing or broken — the bot never crashes on it."""
    global _facts_cache
    if _facts_cache is not None:
        return _facts_cache
    facts = copy.deepcopy(DEFAULT_FACTS)
    try:
        with open(FACTS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in (
                "delivery_min_baht", "area", "area_names", "phone",
                "phone_display", "telegram", "prices",
            ):
                if key in data:
                    facts[key] = data[key]
    except (OSError, ValueError):
        pass
    if not isinstance(facts.get("prices"), list):
        facts["prices"] = []
    _facts_cache = facts
    return facts


# Polite facts answers, per language. Burmese/Thai/English are written
# out; Chinese is a DRAFT (same rule as the welcome texts).
# The "hours" templates take {open}/{close} filled in from the
# owner-set shop hours (get_shop_hours) by answer_facts_question —
# never hardcoded times, so the answer always matches the shop.
FACTS_ANSWERS = {
    "hours": {
        "my": "🕕 ဆိုင်ဖွင့်ချိန်က {open} — {close} ပါရှင့်။ အဲဒီအချိန်အတွင်း မှာယူနိုင်ပါတယ်ရှင့်။",
        "th": "🕕 ร้านเปิด {open} — {close} ค่ะ สั่งได้ในช่วงเวลานั้นนะคะ",
        "en": "🕕 We are open {open} – {close}. You can order during those hours.",
        
        "zh": "🕕 营业时间是{open}–{close}哦，欢迎在这个时间下单。",
    },
    "delivery_fee": {
        "my": (
            "🚚 အနည်းဆုံး ပို့ဆောင်ခ 30 ဘတ်ကနေ စတင်ပို့ဆောင်ပေးပါတယ်ရှင့်။ "
            "တိကျတဲ့ပို့ခက ပို့မယ့်နေရာနဲ့ အော်ဒါအချက်အလက်အပေါ် မူတည်ပါတယ်ရှင့်။ "
            "Admin က စစ်ဆေးပြီး ပြန်လည်အတည်ပြုပေးပါမယ်ရှင့်။"
        ),
        "th": (
            "🚚 ค่าส่งเริ่มต้นที่ขั้นต่ำ ฿30 ค่ะ ค่าส่งที่แน่นอนขึ้นอยู่กับสถานที่จัดส่ง "
            "และรายละเอียดออเดอร์นะคะ แอดมินจะตรวจสอบและยืนยันให้ค่ะ"
        ),
        "en": (
            "🚚 Delivery starts from a minimum fee of ฿30. The exact fee depends on the "
            "delivery place and the order details — our admin will check "
            "and confirm it for you."
        ),
        
        "zh": "🚚 配送费从最低 30 泰铢起哦，具体费用要看送货地点和订单详情，管理员会确认告诉您。",
    },
    "phone": {
        "my": "📞 ဆက်သွယ်ရန် ဖုန်းနံပါတ် — {phone} ပါရှင့်။",
        "th": "📞 เบอร์ติดต่อ — {phone} ค่ะ",
        "en": "📞 You can reach us at {phone}.",
        
        "zh": "📞 联系电话是 {phone} 哦。",
    },
    "area": {
        "my": "📍 Safe Deli က {area} မြို့အတွင်း ပို့ဆောင်ပေးပါတယ်ရှင့်။ မြို့အပြင်ဆိုရင် ဖုန်းဆက်ပြီး အရင် မေးပေးပါနော်။",
        "th": "📍 Safe Deli จัดส่งภายในเมือง{area}ค่ะ ถ้านอกเมืองรบกวนโทรมาสอบถามก่อนนะคะ",
        "en": "📍 Safe Deli delivers inside {area} town. If you are outside town, please call us first to check.",
        
        "zh": "📍 Safe Deli 在大其力市内送货哦，市外请先打电话咨询。",
    },
    "telegram": {
        "my": "💬 Telegram — {telegram} ကနေ ဆက်သွယ်နိုင်ပါတယ်ရှင့်။",
        "th": "💬 ติดต่อทาง Telegram ได้ที่ {telegram} ค่ะ",
        "en": "💬 You can reach us on Telegram at {telegram}.",
        
        "zh": "💬 Telegram 联系方式是 {telegram} 哦。",
    },
    "price": {
        "my": "🍜 {item} — {price} ဘတ် ပါရှင့်။ ပို့ခက အနည်းဆုံး 30 ဘတ်က သပ်သပ်ရှိပါတယ်ရှင့်။",
        "th": "🍜 {item} — {price} บาทค่ะ ค่าส่งขั้นต่ำ 30 บาทแยกต่างหากนะคะ",
        "en": "🍜 {item} — {price} baht. Delivery (from 30 baht) is separate.",
        
        "zh": "🍜 {item} — {price} 泰铢哦，配送费（最低 30 泰铢）另算。",
    },
}


def answer_facts_question(text, lang="my"):
    """Answer a customer question from the owner-confirmed facts file.

    Returns a polite reply string, or None when the question is not
    about a known fact or a listed item price — the caller then falls
    back to the existing auto-reply + admin forward. NEVER invents
    a price or a fact.
    """
    facts = load_facts()
    t = (text or "").strip().lower()
    if not t:
        return None
    lang = lang if lang in LANGS else DEFAULT_LANG
    # Hours come from the owner-set shop hours (get_shop_hours) —
    # the single source of truth. The facts file no longer carries
    # hours: /sethours is the only way to change them, so this
    # answer can never disagree with the shop itself.
    open_min, close_min = _shop_hours_minutes()
    min_fee = facts.get("delivery_min_baht", 30)
    phone = str(facts.get("phone_display") or facts.get("phone") or "")
    telegram = str(facts.get("telegram") or "@SafeDeliveryone")
    area_names = facts.get("area_names") or {}
    area = str(area_names.get(lang) or facts.get("area") or "Tachileik")

    def ans(key, **kw):
        template = (FACTS_ANSWERS.get(key) or {}).get(lang) or FACTS_ANSWERS[key][DEFAULT_LANG]
        return template.format(**kw)

    # Item prices first: a listed item name in the question wins over
    # the generic topics below.
    for entry in facts.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item") or "").strip()
        price = entry.get("price_baht")
        if not item or price is None:
            continue
        item_l = item.lower()
        if item_l in t or (len(t) >= 2 and t in item_l):
            return ans("price", item=item, price=price)
    if any(k in t for k in ("ပို့ခ", "delivery fee", "delivery charge", "ဘယ်လောက်ကျ", "ကုန်ကျ", "ค่าส่ง", "运费")):
        return ans("delivery_fee")
    if any(k in t for k in ("နာရီ", "ဖွင့်", "ပိတ်", "hour", "open", "close", "เวลา", "เปิด", "ปิด", "营业", "几点", "ၵဝ်ႈ", "ၶမ်ႈ")):
        return ans(
            "hours",
            open=format_hours_for_lang(open_min, lang),
            close=format_hours_for_lang(close_min, lang),
        )
    if any(k in t for k in ("ဖုန်း", "phone", "โทร", "电话", "ၾူင်း", "ဆက်သွယ်")):
        return ans("phone", phone=phone)
    if any(k in t for k in ("တာချီလိတ်", "tachileik", "ဘယ်နေရာ", "ท่าขี้เหล็ก", "大其力", "တႃႈၶီႈလဵၵ်ႉ")):
        return ans("area", area=area)
    if any(k in t for k in ("telegram", "တယ်လီဂရမ်", "เทเลแกรม")):
        return ans("telegram", telegram=telegram)
    return None


def safe_business_auto_reply(customer_text: str) -> str:
    """Conservative Safe Deli FAQ replies; never guess live or unconfirmed data."""
    text = (customer_text or "").strip().lower()
    if any(k in text for k in ("ပို့ခ", "delivery fee", "delivery charge", "ဘယ်လောက်ကျ", "ကုန်ကျ")):
        return (
            "🚚 အနည်းဆုံး ပို့ဆောင်ခ 30 ဘတ်ကနေ စတင်ပို့ဆောင်ပေးပါတယ်ရှင့်။ "
            "တိကျတဲ့ပို့ခက ပို့မယ့်နေရာနဲ့ အော်ဒါအချက်အလက်အပေါ် မူတည်ပါတယ်ရှင့်။ "
            "Admin က စစ်ဆေးပြီး ပြန်လည်အတည်ပြုပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("ရပြီလား", "ရမလဲ", "ရောက်ပြီ", "အတည်ပြု",
                            "အခြေအနေ", "စစ်ပေး", "ဘယ်ရောက်", "ပို့ပြီလား",
                            "လာပြီလား", "ဘယ်လောက်ကြာ", "ဘယ်တော့ရ", "ဘယ်တော့ ရ")):
        return (
            "အော်ဒါလေး စစ်ဆေးပြီး အတည်ပြုပေးမှာ မလို့ ခဏလေးဘဲ စောင့်ပေးပါနော် "
            "Safe Deli Admin ဆီ ပို့ပေးထားပါတယ်။ စစ်ဆေးပြီး ပြန်လည်ဖြေကြားပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("အော်ဒါ", "order", "မှာထား", "မှာယူ", "ပစ္စည်းမှာ")):
        return (
            "🛍️ ပစ္စည်းမှာယူလိုပါက Safe Deli ရဲ့ Shop ကိုဖွင့်ပြီး ပစ္စည်း ရွေးချယ်နိုင်ပါတယ်ရှင့်။ "
            "အော်ဒါအခြေအနေ၊ ပစ္စည်းဈေးနှုန်း သို့မဟုတ် အော်ဒါပြင်ဆင်မှုအတွက် "
            "Admin က စစ်ဆေးပြီး ပြန်ဖြေပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("ပါဆယ်", "ပစ္စည်းပို့", "ပို့ဆောင်", "ပို့ချင်")):
        return (
            "📦 ပစ္စည်းပို့ဆောင်လိုပါက Bot ရဲ့ Delivery/Order လမ်းကြောင်းကနေ "
            "အချက်အလက် ဖြည့်ပေးပါနော်။ ပို့မယ့်နေရာနဲ့ လက်ခံမယ့်နေရာကို "
            "ထည့်ပေးပြီး Admin က အသေးစိတ် အတည်ပြုပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("ဆိုင်", "ဆိုင်ရှင်", "ဆိုင်ဖွင့်", "စာရင်းသွင်း", "merchant", "shop")):
        return (
            "🏪 Safe Deli မှာ ဆိုင်ရှင်များအတွက် အွန်လိုင်းဆိုင်ဖွင့်လှစ်ပေးတဲ့ "
            "ဝန်ဆောင်မှုရှိပါတယ်ရှင့်။ ဆိုင်အမည်၊ ပိုင်ရှင်အမည်နဲ့ ဆက်သွယ်ရန် "
            "ဖုန်းနံပါတ်ကို ပို့ပေးပါနော်။ Admin က ဆက်လက်ကူညီပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("လိပ်စာ", "တည်နေရာ", "ဘယ်နေရာ", "တာချီလိတ်", "ဘယ်မှာ")):
        return (
            "📍 Safe Deli ရဲ့ လက်ရှိ Delivery ဝန်ဆောင်မှုဧရိယာက တာချီလိတ်ကို "
            "အခြေခံထားပါတယ်ရှင့်။ သင့်ပို့ဆောင်မယ့်နေရာကို ပို့ပေးပါက "
            "Admin က ဝန်ဆောင်မှုပေးနိုင်/မနိုင် စစ်ဆေးပေးပါမယ်ရှင့်။"
        )
    if any(k in text for k in ("မင်္ဂလာ", "hello", "hi", "ဟယ်လို")):
        return (
            "မင်္ဂလာပါရှင့်။ 💜 Safe Deli မှ ကြိုဆိုပါတယ်။ "
            "ပစ္စည်းမှာယူခြင်း၊ Delivery ဝန်ဆောင်မှု၊ ပါဆယ်ပို့ဆောင်ခြင်း "
            "သို့မဟုတ် ဆိုင်ဖွင့်လှစ်ခြင်းတို့ထဲက ဘာကူညီပေးရမလဲရှင့်။"
        )
    return (
        "ဟုတ်ကဲ့ ရှင့် 💜 Admin က စစ်ဆေးပြီး ပြန်ဖြေပေးပါမယ်ရှင့်။"
    )


async def forward_registrant_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward messages to Min (Admin).

    Two senders reach Min through this handler:
    - A pending shop registrant (verification material: address,
      shop/menu photo, intro video, location pin). Their location
      pin is stored on the registration and forwarded as a map link.
    - An ordinary customer (Min, 2026-11-06: a customer who sends
      a message or a location pin in the bot chat should reach Min,
      so Min can see it and tap the map link to view the location).
      A customer's location is forwarded as a tappable Google Maps
      link; their text/photo/video is forwarded with their name.
    The owner's own messages are never forwarded back to himself.
    """
    if not OWNER_CHAT_ID:
        return
    chat_id = update.effective_chat.id
    # Never forward the owner's own messages back to himself.
    if str(chat_id) == str(OWNER_CHAT_ID):
        return
    # Rider location (2026-11-11): a registered rider's location
    # pin is a live/base position, not a customer message — record
    # it and do NOT forward it to Min. Live-location updates
    # arrive as edited messages; they reach this handler too
    # (effective_message), so no separate handler is needed.
    # (A rider mid-order-conversation is the one exception: the
    # order ConversationHandler swallows the pin first.)
    msg0 = update.effective_message
    loc0 = getattr(msg0, "location", None)
    if (loc0 is not None
            and isinstance(getattr(loc0, "latitude", None), (int, float))
            and isinstance(getattr(loc0, "longitude", None), (int, float))):
        rider = get_rider_by_chat(chat_id)
        if rider:
            await _handle_rider_location(update, context, rider, loc0)
            return
    reg = pending_shop_by_chat(chat_id)
    msg = update.effective_message
    if reg:
        shop_name, owner_name = reg
        note = ""
        if msg.video and getattr(msg.video, "duration", None):
            note = f" 🎬 {msg.video.duration} စက္ကန့်"
        # A location pin from a pending registrant is their mandatory
        # shop location (Min, 2026-11-05 evening): store it on their
        # registration and forward the map link to Min. The isinstance
        # check keeps MagicMock-based tests (whose .location is an
        # auto-mock) from taking this branch; real PTB Location
        # objects carry float latitude/longitude.
        loc = getattr(msg, "location", None)
        if loc is not None and isinstance(
            getattr(loc, "latitude", None), (int, float)
        ) and isinstance(getattr(loc, "longitude", None), (int, float)):
            lat, lon = loc.latitude, loc.longitude
            save_registrant_location(chat_id, lat, lon)
            try:
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID),
                    text=(
                        f"🏪 ဆိုင်ရှင် ဖြည့်စွက်ချက် — {shop_name} ({owner_name})\n"
                        f"📍 ဆိုင်တည်နေရာ pin ရောက်ပြီ ✅\n"
                        f"https://maps.google.com/?q={lat},{lon}"
                    ),
                )
            except Exception:
                pass
            return
        # A guide video file from a pending registrant (v5): store its
        # file_id on the registration (max 5 minutes) and confirm to
        # the shop. The shop's confirmation goes through
        # context.bot.send_message BEFORE the forward to Min, so the
        # forwarded header stays the last send_message call (an
        # existing test asserts that). A video over 5 minutes is not
        # stored; the shop is asked politely to send a shorter one.
        # Customer-branch videos are never captured — only this
        # pending-registrant branch stores a video.
        video = getattr(msg, "video", None)
        video_file_id = getattr(video, "file_id", None) if video else None
        video_duration = getattr(video, "duration", None) if video else None
        if (
            isinstance(video_file_id, str)
            and isinstance(video_duration, (int, float))
            and not isinstance(video_duration, bool)
        ):
            if video_duration > 300:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "တောင်းပန်ပါတယ်ရှင့်။ 🙏\n"
                            "လမ်းညွှန် ဗီဒီယိုက ၅ မိနစ်ထက် ပိုရှည်နေပါတယ်။\n"
                            "၅ မိနစ်အတွင်း ဗီဒီယို ပြန်ပို့ပေးပါနော်။"
                        ),
                    )
                except Exception:
                    pass
            elif save_registrant_video(chat_id, video_file_id, video_duration):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "✅ လမ်းညွှန် ဗီဒီယို လက်ခံရရှိပါပြီ။\n"
                            "Safe Deli က စစ်ဆေးပြီးမှ App ထဲမှာ ဖော်ပြပေးပါမယ်ရှင့်။\n"
                            "ကျေးဇူးတင်ပါတယ် 🙏"
                        ),
                    )
                except Exception:
                    pass
        try:
            await context.bot.send_message(
                chat_id=int(OWNER_CHAT_ID),
                text=f"🏪 ဆိုင်ရှင် ဖြည့်စွက်ချက် — {shop_name} ({owner_name}){note}",
            )
            if msg.photo:
                await context.bot.send_photo(
                    chat_id=int(OWNER_CHAT_ID),
                    photo=msg.photo[-1].file_id,
                    caption=msg.caption or "",
                )
            elif msg.video:
                await context.bot.send_video(
                    chat_id=int(OWNER_CHAT_ID),
                    video=msg.video.file_id,
                    caption=msg.caption or "",
                )
            elif msg.text:
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID), text=msg.text
                )
        except Exception:
            pass
        return
    # A verified shop sending a new guide video (2026-11-11, fix pass):
    # update its stored video_file_id (same 5-minute / 300s rule, same
    # polite reply when too long). Video only — any other message from
    # a verified shop falls through to the ordinary-customer branch
    # below and is forwarded to Min as usual, exactly as before.
    verified = verified_shop_by_chat(chat_id)
    if verified:
        v_shop_name, v_owner_name = verified
        video = getattr(msg, "video", None)
        video_file_id = getattr(video, "file_id", None) if video else None
        video_duration = getattr(video, "duration", None) if video else None
        if (
            isinstance(video_file_id, str)
            and isinstance(video_duration, (int, float))
            and not isinstance(video_duration, bool)
        ):
            if video_duration > 300:
                # Too long: not stored; the shop is asked politely
                # to send a shorter one (same wording as pending).
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "တောင်းပန်ပါတယ်ရှင့်။ 🙏\n"
                            "လမ်းညွှန် ဗီဒီယိုက ၅ မိနစ်ထက် ပိုရှည်နေပါတယ်။\n"
                            "၅ မိနစ်အတွင်း ဗီဒီယို ပြန်ပို့ပေးပါနော်။"
                        ),
                    )
                except Exception:
                    pass
            elif save_verified_shop_video(chat_id, video_file_id, video_duration):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "✅ လမ်းညွှန် ဗီဒီယို အသစ် လက်ခံရရှိပါပြီရှင့်။\n"
                            "App ထဲမှာ ဗီဒီယို အသစ် ဖော်ပြပေးပါမယ်ရှင့်။\n"
                            "ကျေးဇူးတင်ပါတယ်ရှင့် 🙏"
                        ),
                    )
                except Exception:
                    pass
            # Forward the video to Min either way (stored, or too long
            # and not stored), with a verified-shop header — same as
            # the pending branch. The header stays the last
            # send_message call, before the send_video.
            try:
                header = (
                    f"🏪 ဆိုင်ရှင် ဗီဒီယို အသစ် — {v_shop_name} ({v_owner_name})\n"
                    f"🎬 {video_duration} စက္ကန့်"
                )
                if video_duration > 300:
                    header += " (၅ မိနစ်ထက် ပိုရှည်လို့ မသိမ်းထားပါ)"
                await context.bot.send_message(
                    chat_id=int(OWNER_CHAT_ID),
                    text=header,
                )
                await context.bot.send_video(
                    chat_id=int(OWNER_CHAT_ID),
                    video=video_file_id,
                    caption=getattr(msg, "caption", "") or "",
                )
            except Exception:
                pass
            return
    # Ordinary customer (no pending registration): forward their
    # message/location to Min so Admin can see it and tap the map
    # link. Min, 2026-11-06: "location ပို့ရင် admin က နှိပ်ကြည့်လို့
    # ရအောင် လုပ်ပေးပါ" and "ငါ့ဖက်ကိုလည်း တွေးပေးဦး".
    label = _customer_label(update)
    loc = getattr(msg, "location", None)
    if loc is not None and isinstance(
        getattr(loc, "latitude", None), (int, float)
    ) and isinstance(getattr(loc, "longitude", None), (int, float)):
        lat, lon = loc.latitude, loc.longitude
        try:
            await context.bot.send_message(
                chat_id=int(OWNER_CHAT_ID),
                text=(
                    f"💬 ဖောက်သည် {label} က တည်နေရာ ပို့လိုက်ပြီ 📍\n"
                    f"https://maps.google.com/?q={lat},{lon}\n"
                    f"(chat id: {chat_id})"
                ),
            )
        except Exception:
            pass
        return
    try:
        if msg.photo:
            await context.bot.send_message(
                chat_id=int(OWNER_CHAT_ID),
                text=f"💬 ဖောက်သည် {label} က ဓာတ်ပုံ ပို့လိုက်ပြီ (chat id: {chat_id})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ ပြန်စာပို့ရန်", callback_data=f"cust_reply:{chat_id}")
                ]]),
            )
            # The ↩️ button is attached to the photo itself too: the
            # owner looks at the photo, so that is where the button
            # must be — a button only on the header message above it
            # reads as 'no button under the photo'.
            await context.bot.send_photo(
                chat_id=int(OWNER_CHAT_ID),
                photo=msg.photo[-1].file_id,
                caption=msg.caption or "",
                reply_markup=customer_reply_markup(chat_id),
            )
        elif msg.video:
            note = ""
            if getattr(msg.video, "duration", None):
                note = f" 🎬 {msg.video.duration} စက္ကန့်"
            await context.bot.send_message(
                chat_id=int(OWNER_CHAT_ID),
                text=f"💬 ဖောက်သည် {label} က ဗီဒီယို ပို့လိုက်ပြီ{note} (chat id: {chat_id})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ ပြန်စာပို့ရန်", callback_data=f"cust_reply:{chat_id}")
                ]]),
            )
            await context.bot.send_video(
                chat_id=int(OWNER_CHAT_ID),
                video=msg.video.file_id,
                caption=msg.caption or "",
                reply_markup=customer_reply_markup(chat_id),
            )
        elif msg.text:
            # Auto-translate (Min, 2026-11-12 17:50): a customer's
            # free-text message is translated to Burmese for Min when
            # their saved language is th/en/zh; same language and
            # Shan/Akha pass through exactly as before.
            lang = get_user_lang(chat_id) or DEFAULT_LANG
            forwarded = customer_text_for_owner(msg.text, chat_id)
            await context.bot.send_message(
                chat_id=int(OWNER_CHAT_ID),
                text=f"💬 ဖောက်သည် {label}: {forwarded}\\n(chat id: {chat_id})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ ပြန်စာပို့ရန်", callback_data=f"cust_reply:{chat_id}")
                ]]),
            )
            # Facts first (Part 3): a known fact or listed item price
            # is answered politely from safedeli_facts.json; anything
            # unknown falls back to the auto-reply + admin forward —
            # a price or fact is never invented.
            reply = answer_facts_question(msg.text, lang) or safe_business_auto_reply(msg.text)
            await msg.reply_text(reply)
    except Exception:
        pass
async def _set_menu_button(app):
    """One-tap Mini App entry (point 1): the Telegram menu button opens
    Safe Deli directly. A failure here must never block polling."""
    try:
        from telegram import MenuButtonWebApp
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🛒 Safe Deli", web_app=WebAppInfo(url=MINI_APP_URL)
            )
        )
    except Exception:
        logger.exception("menu button: set failed")



def build_app():
    persistence = PicklePersistence(filepath=os.path.join(BASE_DIR, "bot_state.pickle"))
    # Longer timeouts: the bot talks to Telegram through a proxy and
    # a single call can take over the default 5s read timeout
    # (TimedOut seen 2026-11-05). Longer timeouts let slow calls
    # finish instead of failing the whole handler.
    # trust_env is left on so SSL_CERT_FILE (the egress CA bundle)
    # is still honoured. Only NO_PROXY is sanitised: this VM's
    # NO_PROXY list contains "[::1]" entries which httpx tries to
    # parse as URLs and crashes on startup.
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or None
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        proxy=proxy_url,
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .request(request)
        .post_init(_set_menu_button)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(order_begin, pattern="^order_new$"),
            CallbackQueryHandler(location_begin, pattern="^show_location$"),
            CommandHandler("order", order_begin),
        ],
        states={
            NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Cancel$"),
                    name_got,
                )
            ],
            SENDER_PHONE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Cancel$"),
                    sender_phone_got,
                )
            ],
            PICKUP: [
                MessageHandler(filters.LOCATION, pickup_got),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Cancel$"),
                    pickup_got,
                ),
            ],
            DROPOFF: [
                MessageHandler(filters.LOCATION, dropoff_got),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Cancel$"),
                    dropoff_got,
                ),
            ],
            ITEM: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Regex("^Cancel$"),
                    item_got,
                )
            ],
            PHOTO: [
                MessageHandler(filters.PHOTO, photo_got),
                CallbackQueryHandler(photo_skip, pattern="^photo_skip$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_yes, pattern="^confirm_yes$"),
                CallbackQueryHandler(confirm_no, pattern="^confirm_no$"),
            ],
        },
        # The Order / Location buttons must also work while a user is
        # already mid-order: without these, a stuck conversation (e.g.
        # the user typed something and never finished) swallows the
        # tap and the buttons look dead. Entry points only fire when
        # no conversation is active; fallbacks fire in any state.
        fallbacks=[
            CommandHandler("cancel", cancel),
            # The reply-keyboard "Cancel" button sends the TEXT "Cancel"
            # (not /cancel). The state handlers above exclude it, so it
            # falls through here and really cancels instead of being
            # treated as a name/phone/address/item.
            MessageHandler(filters.Regex("^Cancel$"), cancel),
            CallbackQueryHandler(order_begin, pattern="^order_new$"),
            CallbackQueryHandler(location_begin, pattern="^show_location$"),
        ],
        name="order_flow",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CallbackQueryHandler(choose_language, pattern=r"^lang:(my|th|en|zh)$"))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("orders", orders_today))
    app.add_handler(CommandHandler("reply", reply_customer))
    app.add_handler(CommandHandler("cancelreply", cancel_customer_reply))
    app.add_handler(CallbackQueryHandler(begin_customer_reply, pattern=r"^cust_reply:-?\d+$"))
    app.add_handler(CallbackQueryHandler(shop_order_decision, pattern=r"^shop_(ok|no|wait):SD-\d{8}-\d{3}$"))
    app.add_handler(CallbackQueryHandler(shop_order_ready, pattern=r"^shop_ready:SD-\d{8}-\d{3}$"))
    app.add_handler(CallbackQueryHandler(night_order_decision, pattern=r"^night_(ok|cancel):SD-\d{8}-\d{3}$"))
    app.add_handler(CallbackQueryHandler(fee_decision, pattern=r"^fee_(ok|no):SD-\d{8}-\d{3}$"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        send_pending_customer_reply,
    ), group=1)
    app.add_handler(CommandHandler("shops", shops_list))
    app.add_handler(CommandHandler("shop", shop_registration_detail))
    app.add_handler(CommandHandler("shopverify", shop_verify))
    app.add_handler(CommandHandler("shopreject", shop_reject))
    app.add_handler(CommandHandler("close", close_shop))
    app.add_handler(CommandHandler("open", open_shop))
    # Owner-only hours control (Min, 2026-11-12 02:27): /sethours sets
    # the daily DELIVERY window, /hours shows it. NOT in the public command
    # list — the public BotFather list stays: start, order, lang,
    # myid, cancel.
    app.add_handler(CommandHandler("sethours", set_hours_command))
    app.add_handler(CommandHandler("hours", shop_hours_command))
    # Rider assignment (2026-11-11): /deliver creates a delivery
    # and auto-assigns the nearest rider; /addrider + /riders +
    # /rideroff + /rideron manage riders (admin only). Rider job
    # card buttons (accept / decline / picked up / delivered).
    app.add_handler(CommandHandler("deliver", deliver_command))
    app.add_handler(CommandHandler("addrider", addrider_command))
    app.add_handler(CommandHandler("riders", riders_command))
    app.add_handler(CommandHandler("rideroff", rideroff_command))
    app.add_handler(CommandHandler("rideron", rideron_command))
    app.add_handler(CallbackQueryHandler(
        rider_decision, pattern=r"^rider_(ok|no|pickup|deliver):\d+$"
    ))
    # Mini App orders (tg.sendData) — registered before the order
    # ConversationHandler so a WebApp-data message is never swallowed
    # by a conversation state waiting for text/location.
    app.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, mini_app_order)
    )
    # Server-route orders (2026-11-12): the Mini App posts orders to
    # the API when a Telegram sendData launch is unavailable
    # (menu-button / link launches have no query_id). The pickup job
    # claims them every 15s and sends the same confirmations as a
    # sendData order. Off until the reviewed cutover
    # (SAFEDELI_API_ORDERS_ENABLED=1 in the bot's environment).
    # python-telegram-bot's job queue needs the [job-queue] extra
    # (requirements.txt); without it job_queue is None and the job
    # simply does not run — the bot still starts, and the missing
    # pickup is visible in the startup log.
    if os.environ.get("SAFEDELI_API_ORDERS_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        if app.job_queue is not None:
            app.job_queue.run_repeating(pickup_api_orders_job, interval=15, first=10)
        else:
            logger.warning(
                "server-route pickup NOT running: python-telegram-bot "
                "[job-queue] extra is missing (pip install the extra, "
                "then restart the bot)"
            )
    # Status notifications (2026-11-19): the API writes dashboard
    # status changes as order_status_events rows, but the API cannot
    # send Telegram — the bot tells the customer. Not gated by the
    # orders flag: it watches dashboard changes, not order intake.
    if app.job_queue is not None:
        app.job_queue.run_repeating(notify_status_changes_job, interval=15, first=15)
        app.job_queue.run_repeating(pickup_service_bookings_job, interval=15, first=17)
        app.job_queue.run_repeating(fee_confirm_job, interval=15, first=20)
        app.job_queue.run_repeating(fee_accept_job, interval=15, first=22)
        app.job_queue.run_repeating(error_alert_job, interval=60, first=30)
    else:
        logger.warning(
            "status notify NOT running: python-telegram-bot "
            "[job-queue] extra is missing (pip install the extra, "
            "then restart the bot)"
        )
    app.add_handler(
        CallbackQueryHandler(
            menu_callbacks, pattern="^(show_phone|show_price|show_lang_picker|help_location)$"
        )
    )
    app.add_handler(conv)
    # Verification material (address / shop photo / menu photo /
    # shop-intro video / location pin) from pending shop registrants
    # — registered after the ConversationHandler so order-flow
    # messages are never swallowed by it.
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.TEXT | filters.LOCATION)
            & ~filters.COMMAND,
            forward_registrant_message,
        )
    )
    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Put it in .env (see .env.example).")
    app = build_app()
    print("Safe Deli bot is running (polling)...")
    # timeout=20 keeps the long-poll connection open longer so the
    # bot notices taps sooner instead of re-polling every few seconds.
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=20, poll_interval=0.0)


if __name__ == "__main__":
    main()
