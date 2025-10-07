#!/usr/bin/env python3
# bot.py (complete replacement)
import os
import json
import re
import time
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import (
    BadRequest, TelegramError, NetworkError, TimedOut, RetryAfter
)
from telegram.ext import Updater, CallbackQueryHandler, MessageHandler, Filters, CommandHandler

# try to import helper modules if you have them
try:
    from image_utils import get_image_for_model, normalize_text
except Exception:
    # fallback normalize_text (basic)
    def normalize_text(s: str) -> str:
        if not s:
            return ""
        s = str(s).lower()
        s = s.replace("%", " percent ").replace("/", " ").replace("\\", " ").replace("_", " ").replace(".", " ")
        s = re.sub(r"[\(\)\[\]\{\}]", " ", s)
        s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
        s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)
        s = re.sub(r"[^\w\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def get_image_for_model(model_name: str, image_folder: str) -> Optional[str]:
        # simplest fallback: look for normalized_model + ext in folder
        nm = normalize_text(model_name).replace(" ", "")
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            p = os.path.join("images", nm + ext)
            if os.path.isfile(p):
                return p
        return None

# try to import sheet helper (user may have provided it); otherwise fallback to inline function
SHEET_HELPER_IMPORTED = False
try:
    import sheet_helpers as sheet_helpers  # expected to provide get_data()
    SHEET_HELPER_IMPORTED = True
except Exception:
    try:
        import bot_helpers_sheet as sheet_helpers
        SHEET_HELPER_IMPORTED = True
    except Exception:
        SHEET_HELPER_IMPORTED = False

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("autopost")

# Load config and JSON files
def load_json(path: str, default: Any = None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}
    except Exception:
        log.exception("Failed to load JSON: %s", path)
        return default if default is not None else {}

CFG = load_json("config.json", {})
BOT_TOKEN = CFG.get("bot_token")
USER_CHAT_ID = CFG.get("user_chat_id")
GROUP_CHAT_ID = CFG.get("group_chat_id")
SECOND_GROUP_CHAT_ID = CFG.get("second_group_chat_id")
SPREADSHEET_KEY = CFG.get("spreadsheet_key")
SHEET_NAME = CFG.get("sheet_name", "ОДНОРАЗКИ")
CREDENTIALS_FILE = CFG.get("credentials_file", "credentials.json")
DB_FILE = CFG.get("db_file", "message_ids.json")
IMAGE_FOLDER = CFG.get("image_folder", "images")
PRICES_FILE = CFG.get("prices_file", "prices.json")
TEMPLATES_FILE = CFG.get("templates_file", "templates.json")
TEMPLATES_SECOND_FILE = CFG.get("templates_second_group_file", "templates_second_group.json")
FLAVOR_EMOJIS_FILE = CFG.get("flavor_emojis_file", "flavor_emojis.json")

if not BOT_TOKEN:
    log.error("bot_token missing in config.json — cannot start")
    raise SystemExit(1)

PRICES = load_json(PRICES_FILE, {})
# normalize price keys for quick lookup
PRICES_NORM = {normalize_text(k): v for k, v in PRICES.items()}

TEMPLATES = load_json(TEMPLATES_FILE, {
    "main_caption": "❗️ОБНОВЛЕНИЕ ОСТАТКОВ❗️\n\n💥{model}💥\n\n🔥Цена: {price} zl 🔥\n👇 Заказать 👇\n📦@Diana_Elfbarchik📦\n\n💥 🎉ДОСТАВКА БЕСПЛАТНО! 💥 🎉\n📦🚚При Заказе от 3х штук! 📦🚚\n\n🤪АКТУАЛЬНОЕ НАЛИЧИЕ🤪\n\n{flavor_lines}\n\n{contact_info}",
    "contact_info": "Оформить заказ: 📩 @Diana_Elfbarchik\nНаша группа: @Elfbarchik_Store\nНаш канал: @ElfBerry_net"
})
TEMPLATES_SECOND = load_json(TEMPLATES_SECOND_FILE, {})
FLAVOR_EMOJIS = load_json(FLAVOR_EMOJIS_FILE, {})

# load or init DB
def load_db():
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(d):
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Failed to save DB file %s", DB_FILE)

DB = load_db()
DB.setdefault("temp_posts", {})  # normalized_key -> {message_id, model, chat_id, caption, posted}
DB.setdefault("edits", {})       # str(chat_id) -> {key, prompt_message_id}
save_db(DB)

# --- Safe network wrapper (retry/backoff) ---
def safe_call(fn, *args, retries=3, base_sleep=0.8, **kwargs):
    """
    Calls fn(*args, **kwargs) with retries on network related exceptions.
    Returns fn result or raises last error.
    """
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 5))
            log.warning("RetryAfter: waiting %s seconds", wait)
            time.sleep(wait + 1)
        except (NetworkError, TimedOut, ConnectionError, OSError) as e:
            attempt += 1
            if attempt > retries:
                log.exception("safe_call: giving up after %d attempts", attempt)
                raise
            sleep = base_sleep * (2 ** (attempt - 1))
            log.warning("safe_call: network error %s, retry %d/%d after %.1fs", e, attempt, retries, sleep)
            time.sleep(sleep)
        except TelegramError as e:
            # non-network Telegram errors (BadRequest etc.) propagate — caller may handle
            raise

# --- Google Sheets reading (fallback) ---
def get_sheet_records() -> List[Dict[str, Any]]:
    """
    Returns list of {'model','flavor','available'}.
    Prefer external sheet_helpers module; if absent, use local minimal implementation.
    """
    if SHEET_HELPER_IMPORTED:
        try:
            return sheet_helpers.get_data()
        except Exception:
            log.exception("sheet_helpers.get_data failed, falling back to inline reader")

    # Inline fallback: authorize and read using gspread
    if not CREDENTIALS_FILE or not SPREADSHEET_KEY:
        log.error("No credentials_file or spreadsheet_key in config.json for inline sheet reader")
        return []

    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            CREDENTIALS_FILE,
            ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sh = client.open_by_key(SPREADSHEET_KEY)
        ws = sh.worksheet(SHEET_NAME)
        rows = ws.get_all_values()
    except Exception:
        log.exception("Inline sheet read failed")
        return []

    if not rows or len(rows) < 2:
        return []

    headers = rows[0]
    try:
        idx_title = headers.index("Title")
    except ValueError:
        idx_title = 1 if len(headers) > 1 else 0
    try:
        idx_avail = headers.index("Available")
    except ValueError:
        idx_avail = 2 if len(headers) > 2 else 2

    recs = []
    current_model = None
    for r in rows[1:]:
        sku = (r[0] if len(r) > 0 else "").strip()
        title = (r[idx_title] if len(r) > idx_title else "").strip()
        avail = (r[idx_avail] if len(r) > idx_avail else "").strip()

        if sku and not title:
            current_model = sku
            continue
        if not sku and title and not avail:
            current_model = title
            continue
        if current_model and title:
            m = re.match(r'^(.*?)\s*\((.+)\)$', title)
            if m:
                flavor = m.group(2).strip()
            else:
                flavor = title.strip()
            try:
                avail_i = int(avail) if avail and str(avail).strip().isdigit() else 0
            except Exception:
                avail_i = 0
            recs.append({'model': current_model, 'flavor': flavor, 'available': avail_i})
    log.info("Read rows from sheet: %d", len(recs))
    return recs

# --- Flavor emoji helper ---
def get_flavor_emoji(flavor: str) -> str:
    if not flavor:
        return ""
    k = flavor.lower().strip()
    if k in FLAVOR_EMOJIS:
        return FLAVOR_EMOJIS[k]
    parts = re.split(r'\s+', k)
    emojis = []
    for p in parts:
        if p in FLAVOR_EMOJIS:
            emojis.append(FLAVOR_EMOJIS[p])
    if 'sour' in parts and 'sour' in FLAVOR_EMOJIS:
        emojis.append(FLAVOR_EMOJIS.get('sour'))
    if 'ice' in parts or 'icy' in parts:
        emojis.append(FLAVOR_EMOJIS.get('ice', '🧊'))
    return "".join(emojis)

# --- Caption generator ---
def generate_caption(model: str, flavors: List[Dict[str, Any]], template_group: int = 1) -> str:
    if flavors:
        lines = []
        for f in sorted(flavors, key=lambda x: -int(x.get('available', 0))):
            em = get_flavor_emoji(f.get('flavor', ''))
            lines.append(f"✅ {f.get('flavor', '')} {em} ({f.get('available', 0)} шт.)")
        flavor_lines = "\n".join(lines)
    else:
        flavor_lines = "⚠️ Нет в наличии"

    price_raw = PRICES_NORM.get(normalize_text(model))
    price_display = str(price_raw) if price_raw not in (None, "", "—") else "—"

    if template_group == 1:
        tpl = TEMPLATES.get("main_caption") or TEMPLATES.get("caption") or ""
        contact_info = TEMPLATES.get("contact_info") or TEMPLATES.get("contacts") or ""
    else:
        tpl = TEMPLATES_SECOND.get("main_caption") or TEMPLATES_SECOND.get("caption_second_group") or ""
        contact_info = TEMPLATES_SECOND.get("contact_info") or TEMPLATES_SECOND.get("contacts") or TEMPLATES.get("contact_info") or ""

    # use safe mapping to avoid KeyError
    mapping = {
        "model": model,
        "price": price_display,
        "flavor_lines": flavor_lines,
        "contact_info": contact_info,
        "contacts": contact_info
    }

    class _Default(dict):
        def __missing__(self, key):
            return ""

    try:
        return tpl.format_map(_Default(mapping))
    except Exception:
        log.exception("generate_caption: template formatting failed for %s", model)
        return f"❗️ОБНОВЛЕНИЕ ОСТАТКОВ❗️\n\n💥{model}💥\n\n🔥Цена: {price_display} zl 🔥\n\n{flavor_lines}\n\n{contact_info}"

# --- Utilities: update prices.json with missing models ---
def update_price_file_with_missing(missing_models: List[str]):
    if not missing_models:
        return
    try:
        raw = {}
        if os.path.exists(PRICES_FILE):
            try:
                with open(PRICES_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
        changed = False
        for m in missing_models:
            key_norm = normalize_text(m)
            if key_norm in PRICES_NORM:
                continue
            if m not in raw:
                raw[m] = ""
                changed = True
            PRICES_NORM[key_norm] = ""
        if changed:
            with open(PRICES_FILE, 'w', encoding='utf-8') as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            log.info("Updated %s with %d new empty entries", PRICES_FILE, len(missing_models))
    except Exception:
        log.exception("update_price_file_with_missing failed")

# --- Bot UI helpers ---
def build_main_menu():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Создать посты", callback_data='create_posts'),
         InlineKeyboardButton("Обновить посты", callback_data='update_posts')],
        [InlineKeyboardButton("Проверить на ошибки", callback_data='check_errors'),
         InlineKeyboardButton("Настройки", callback_data='settings')],
    ])
    return kb

def create_initial_prompt(bot: Bot):
    try:
        safe_call(bot.send_message, chat_id=USER_CHAT_ID, text="👋 Привет! Выберите действие:", reply_markup=build_main_menu())
    except Exception:
        log.exception("create_initial_prompt failed")

# --- Create temporary posts in personal chat ---
def create_posts(bot: Bot):
    chat_id = USER_CHAT_ID
    records = get_sheet_records()
    if not records:
        safe_call(bot.send_message, chat_id=chat_id, text="Нет данных для создания постов.")
        return

    models = {}
    for r in records:
        k = normalize_text(r['model'])
        models.setdefault(k, {'model': r['model'], 'flavors': []})
        models[k]['flavors'].append({'flavor': r['flavor'], 'available': r['available']})

    DB['temp_posts'] = {}
    save_db(DB)

    missing_prices = set()
    created = 0

    for k, data in sorted(models.items()):
        model_name = data['model']
        caption = generate_caption(model_name, data['flavors'], template_group=1)
        img_path = get_image_for_model(model_name, IMAGE_FOLDER)

        try:
            if img_path and os.path.isfile(img_path):
                try:
                    sent = safe_call(bot.send_photo, chat_id=chat_id, photo=open(img_path, 'rb'), caption=caption,
                                     reply_markup=InlineKeyboardMarkup([
                                         [InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                          InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                                     ]))
                except BadRequest as e:
                    log.warning("send_photo failed for %s -> fallback to text: %s", img_path, e)
                    sent = safe_call(bot.send_message, chat_id=chat_id, text=caption,
                                     reply_markup=InlineKeyboardMarkup([
                                         [InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                          InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                                     ]))
            elif img_path:
                # maybe URL
                try:
                    sent = safe_call(bot.send_photo, chat_id=chat_id, photo=img_path, caption=caption,
                                     reply_markup=InlineKeyboardMarkup([
                                         [InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                          InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                                     ]))
                except Exception:
                    sent = safe_call(bot.send_message, chat_id=chat_id, text=caption,
                                     reply_markup=InlineKeyboardMarkup([
                                         [InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                          InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                                     ]))
            else:
                sent = safe_call(bot.send_message, chat_id=chat_id, text=caption,
                                 reply_markup=InlineKeyboardMarkup([
                                     [InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                      InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                                 ]))
            DB['temp_posts'][k] = {'message_id': sent.message_id, 'model': model_name, 'chat_id': chat_id, 'caption': caption, 'posted': False}
            created += 1
            if PRICES_NORM.get(k) in (None, "", "—"):
                missing_prices.add(model_name)
            time.sleep(0.08)
        except Exception:
            log.exception("create_posts: failed to create temp post for %s", model_name)

    DB['last_create'] = datetime.now().isoformat()
    save_db(DB)

    if missing_prices:
        update_price_file_with_missing(sorted(missing_prices))
        safe_call(bot.send_message, chat_id=chat_id, text="Обнаружены модели без цены. Добавлены в prices.json (без цены):\n" + "\n".join(sorted(missing_prices)))

    safe_call(bot.send_message, chat_id=chat_id, text=f"Создано {created} временных постов.", reply_markup=build_main_menu())

# --- Callback handler ---
def callback_handler(update, context):
    query = update.callback_query
    if not query:
        return
    data = query.data
    chat_id = query.message.chat_id
    try:
        # Answer callback safely (wrap network errors)
        try:
            safe_call(query.answer, text="...")
        except Exception:
            # Network error on answering callback — log and continue
            log.warning("Failed to answer callback query (network).")

        if data == 'create_posts':
            safe_call(query.answer, text="Создаю посты...")
            create_posts(context.bot)
            try:
                query.edit_message_text("Посты созданы.", reply_markup=build_main_menu())
            except Exception:
                pass
            return

        if data == 'update_posts':
            safe_call(query.answer, text="Обновляю посты...")
            safe_call(context.bot.send_message, chat_id=chat_id, text="Обновление постов начато...")
            update_posts(context.bot)
            try:
                query.edit_message_text("Обновление завершено.", reply_markup=build_main_menu())
            except Exception:
                pass
            return

        if data == 'check_errors':
            safe_call(query.answer, text="Проверяю...")
            temp_posts = DB.get('temp_posts', {})
            errors = []
            # check price & flavors & emojis & presence of image (we check DB stored caption & image guess)
            for k, p in temp_posts.items():
                model = p.get('model')
                caption = p.get('caption', '')
                # price
                if PRICES_NORM.get(k) in (None, "", "—"):
                    errors.append(f"- У модели {model} цена не указана.")
                # flavor presence (check for '✅' lines)
                if "⚠️ Нет в наличии" in caption or not re.search(r'✅', caption):
                    errors.append(f"- У модели {model} отсутствуют вкусы или количество.")
                # emoji check (search lines starting with ✅ ... (N шт.))
                for line in caption.splitlines():
                    m = re.match(r'✅\s*(.+?)\s*(\(.+\))?$', line)
                    if m:
                        text_part = m.group(1)
                        # if there is no emoji (no non-ascii pictograph), we warn
                        if not re.search(r'[\U0001F300-\U0001FAFF\u2600-\u26FF]', text_part):
                            errors.append(f"- У модели {model} в строке '{line.strip()}' отсутствуют эмодзи вкуса.")
                # check image existence via get_image_for_model
                img_path = get_image_for_model(model, IMAGE_FOLDER)
                if not img_path or not os.path.exists(img_path):
                    errors.append(f"- У модели {model} изображение не найдено.")
            if not errors:
                safe_call(context.bot.send_message, chat_id=chat_id, text="Ошибок не найдено.", reply_markup=build_main_menu())
            else:
                safe_call(context.bot.send_message, chat_id=chat_id, text="Найдены проблемы:\n" + "\n".join(errors), reply_markup=build_main_menu())
            return

        if data == 'settings':
            safe_call(query.answer, text="Открываю настройки...")
            try:
                query.edit_message_text("Настройки пока не реализованы.", reply_markup=build_main_menu())
            except Exception:
                pass
            return

        if data.startswith("post_"):
            _, k = data.split("_", 1)
            temp = DB.get('temp_posts', {}).get(k)
            if not temp:
                safe_call(query.answer, text="Пост не найден.")
                return
            if temp.get('posted'):
                safe_call(query.answer, text="Пост уже опубликован.")
                return

            # forward to first group
            try:
                forwarded = safe_call(context.bot.forward_message, chat_id=GROUP_CHAT_ID, from_chat_id=temp['chat_id'], message_id=temp['message_id'])
                DB.setdefault(k, {})  # store published info under normalized key
                DB[k] = {'message_id': forwarded.message_id, 'model': temp['model'], 'chat_id': GROUP_CHAT_ID}
                log.info("Forwarded %s to group1 id=%s", temp['model'], forwarded.message_id)
            except Exception as e:
                log.exception("Error forwarding to group1")
                safe_call(query.answer, text=f"Ошибка при отправке в группу 1: {e}")
                return

            # post to second group as new photo + caption (use second template)
            try:
                records = get_sheet_records()
                flavors = [r for r in records if normalize_text(r['model']) == k]
                caption2 = generate_caption(temp['model'], flavors, template_group=2)
                img_path = get_image_for_model(temp['model'], IMAGE_FOLDER)
                if img_path and os.path.isfile(img_path):
                    try:
                        sent2 = safe_call(context.bot.send_photo, chat_id=SECOND_GROUP_CHAT_ID, photo=open(img_path, 'rb'), caption=caption2)
                    except BadRequest as e:
                        log.warning("send_photo to second group failed (%s) -> fallback to text", e)
                        sent2 = safe_call(context.bot.send_message, chat_id=SECOND_GROUP_CHAT_ID, text=caption2)
                elif img_path:
                    try:
                        sent2 = safe_call(context.bot.send_photo, chat_id=SECOND_GROUP_CHAT_ID, photo=img_path, caption=caption2)
                    except Exception:
                        sent2 = safe_call(context.bot.send_message, chat_id=SECOND_GROUP_CHAT_ID, text=caption2)
                else:
                    sent2 = safe_call(context.bot.send_message, chat_id=SECOND_GROUP_CHAT_ID, text=caption2)

                DB[f"{k}_second"] = {'message_id': sent2.message_id, 'model': temp['model'], 'chat_id': SECOND_GROUP_CHAT_ID}
                log.info("Posted %s to second group id=%s", temp['model'], sent2.message_id)
            except Exception:
                log.exception("Error posting to second group")
                safe_call(query.answer, text="Ошибка при отправке в вторую группу.")
                return

            # mark posted
            DB['temp_posts'][k]['posted'] = True
            save_db(DB)

            try:
                query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Опубликовано ✅", callback_data=f"posted_{k}"),
                     InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]
                ]))
            except Exception:
                pass
            safe_call(query.answer, text="Опубликовано в обе группы.")
            return

        if data.startswith("edit_"):
            _, k = data.split("_", 1)
            temp = DB.get('temp_posts', {}).get(k)
            if not temp:
                safe_call(query.answer, text="Пост не найден.")
                return
            # prompt for new text/photo
            prompt = safe_call(context.bot.send_message, chat_id=chat_id, text=f"Редактирование поста {temp['model']}\nОтправьте новый текст или фото для этого поста.",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=f"cancel_edit_{k}")]]))
            DB.setdefault('edits', {})[str(chat_id)] = {'key': k, 'prompt_message_id': prompt.message_id}
            save_db(DB)
            safe_call(query.answer, text="Ожидаю новое сообщение с изменениями.")
            return

        if data.startswith("cancel_edit_"):
            # cancel edit
            parts = data.split("_", 2)
            k = parts[-1]
            DB.get('edits', {}).pop(str(chat_id), None)
            save_db(DB)
            safe_call(query.answer, text="Редактирование отменено.")
            return

        # default
        safe_call(query.answer, text="")
    except Exception:
        log.exception("Unhandled exception in callback_handler")
        try:
            safe_call(context.bot.send_message, chat_id=chat_id, text="Произошла ошибка. Проверьте логи.")
        except Exception:
            pass

# --- Message handler (edit processing + menu fallback) ---
def message_handler(update, context):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat_id
    edits = DB.get('edits', {})
    state = edits.get(str(chat_id))
    if state:
        # in edit mode
        k = state.get('key')
        temp = DB.get('temp_posts', {}).get(k)
        if not temp:
            safe_call(context.bot.send_message, chat_id=chat_id, text="Пост уже недоступен.")
            edits.pop(str(chat_id), None)
            DB['edits'] = edits
            save_db(DB)
            return

        new_text = msg.text if msg.text else None
        new_photo_file_id = None
        if msg.photo:
            new_photo_file_id = msg.photo[-1].file_id

        # if text contains price update - persist it
        if new_text:
            m = re.search(r'🔥Цена:\s*(\d+)', new_text)
            if m:
                v = m.group(1)
                PRICES_NORM[normalize_text(temp['model'])] = v
                # persist readable raw key in PRICES_FILE
                try:
                    raw = load_json(PRICES_FILE, {})
                    raw[temp['model']] = v
                    with open(PRICES_FILE, 'w', encoding='utf-8') as f:
                        json.dump(raw, f, ensure_ascii=False, indent=2)
                    log.info("Persisted price change for %s -> %s", temp['model'], v)
                except Exception:
                    log.exception("Failed to persist price change")

        # recompute caption from sheet
        records = get_sheet_records()
        flavors = [r for r in records if normalize_text(r['model']) == k]
        updated_caption = generate_caption(temp['model'], flavors, template_group=1)

        try:
            if new_photo_file_id:
                # delete old
                try:
                    safe_call(context.bot.delete_message, chat_id=temp['chat_id'], message_id=temp['message_id'])
                except Exception:
                    pass
                # send new photo by file_id into personal chat
                sent = safe_call(context.bot.send_photo, chat_id=temp['chat_id'], photo=new_photo_file_id, caption=updated_caption,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                                                    InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]]))
                DB['temp_posts'][k]['message_id'] = sent.message_id
                DB['temp_posts'][k]['caption'] = updated_caption
                save_db(DB)
                safe_call(context.bot.send_message, chat_id=chat_id, text="Изображение и текст обновлены (в чате с ботом).")
            else:
                # try to edit caption in place
                try:
                    safe_call(context.bot.edit_message_caption, chat_id=temp['chat_id'], message_id=temp['message_id'], caption=updated_caption,
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                                                 InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]]))
                    DB['temp_posts'][k]['caption'] = updated_caption
                    save_db(DB)
                    safe_call(context.bot.send_message, chat_id=chat_id, text="Текст обновлён (в чате с ботом).")
                except BadRequest as e:
                    # fallback: send a new message with updated text and update DB
                    log.warning("edit_message_caption failed, sending new message: %s", e)
                    sent = safe_call(context.bot.send_message, chat_id=temp['chat_id'], text=updated_caption,
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Запостить", callback_data=f"post_{k}"),
                                                                        InlineKeyboardButton("Изменить", callback_data=f"edit_{k}")]]))
                    DB['temp_posts'][k]['message_id'] = sent.message_id
                    DB['temp_posts'][k]['caption'] = updated_caption
                    save_db(DB)
                    safe_call(context.bot.send_message, chat_id=chat_id, text="Текст обновлён (отправлен новый пост в чате с ботом).")
        finally:
            edits.pop(str(chat_id), None)
            DB['edits'] = edits
            save_db(DB)
        return

    # not editing — show menu (for convenience) when user writes text (not command)
    if msg.text and not msg.text.startswith("/"):
        try:
            safe_call(context.bot.send_message, chat_id=chat_id, text="Выберите действие:", reply_markup=build_main_menu())
        except Exception:
            pass

# --- Update published posts in groups ---
def update_posts(bot):
    try:
        records = get_sheet_records()
        models = {}
        for r in records:
            k = normalize_text(r['model'])
            models.setdefault(k, {'model': r['model'], 'flavors': []})
            models[k]['flavors'].append({'flavor': r['flavor'], 'available': r['available']})
        updated = 0
        for key, post in list(DB.items()):
            if key in ("temp_posts", "edits", "last_create"):
                continue
            chat_id = post.get('chat_id')
            model = post.get('model')
            if not chat_id or not model:
                continue
            k = normalize_text(model)
            model_data = models.get(k, {'model': model, 'flavors': []})
            template_group = 1 if chat_id == GROUP_CHAT_ID else 2
            new_caption = generate_caption(model_data['model'], model_data['flavors'], template_group=template_group)
            try:
                safe_call(bot.edit_message_caption, chat_id=chat_id, message_id=post['message_id'], caption=new_caption)
                updated += 1
                time.sleep(0.05)
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    continue
                else:
                    log.exception("Error updating caption for %s: %s", model, e)
            except Exception:
                log.exception("Error updating caption for %s", model)
        log.info("update_posts done. Updated: %d", updated)
    except Exception:
        log.exception("update_posts failure")

# --- Schedule helper thread (if you use schedule) ---
import schedule
def schedule_thread():
    while True:
        try:
            schedule.run_pending()
        except Exception:
            log.exception("Schedule run_pending failed")
        time.sleep(2)

# --- Handlers wiring & main ---
def start_cmd(update, context):
    create_initial_prompt(context.bot)

def main():
    global bot
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    bot = updater.bot

    dp.add_handler(CommandHandler('start', start_cmd))
    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(MessageHandler(Filters.photo | (Filters.text & (~Filters.command)), message_handler))

    updater.start_polling()
    log.info("Bot started.")
    create_initial_prompt(bot)

    # schedule example: every 3 hours update published posts
    schedule.every(3).hours.do(lambda: update_posts(bot))
    th = threading.Thread(target=schedule_thread, daemon=True)
    th.start()

    try:
        updater.idle()
    except KeyboardInterrupt:
        log.info("Stopped by user")

if __name__ == "__main__":
    main()
