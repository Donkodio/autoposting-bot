#!/usr/bin/env python3
# test_build_post.py
# Обновлённый тест: читает Google Sheet, группирует модели+вкусы и собирает пост.
# Убирает из строк вкуса повторения названия модели (например "VOZOL RAVE 40000 (Mango Peach)" -> "Mango Peach")

import json
import logging
import re
from pathlib import Path

import gspread

# --- логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("test_build_post")

# --- безопасная загрузка JSON ---
def safe_load_json(path: Path, default):
    if not path.exists():
        logger.warning("Файл %s не найден. Использую значение по умолчанию.", path)
        return default
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            logger.warning("Файл %s пуст. Использую значение по умолчанию.", path)
            return default
        return json.loads(text)
    except Exception as e:
        logger.exception("Ошибка парсинга JSON %s: %s. Использую значение по умолчанию.", path, e)
        return default

# --- загрузка конфигурации ---
CFG_PATH = Path("config.json")
if not CFG_PATH.exists():
    logger.error("config.json не найден. Создай его и заполни.")
    raise SystemExit(1)

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
creds_file = cfg.get("credentials_file", "credentials.json")
spreadsheet_key = cfg.get("spreadsheet_key")
sheet_name = cfg.get("sheet_name", "ОДНОРАЗКИ")

if not spreadsheet_key:
    logger.error("В config.json отсутствует spreadsheet_key.")
    raise SystemExit(1)

# --- вспомогательные JSON ---
templates = safe_load_json(Path(cfg.get("templates_file", "templates.json")), {
    "main_caption": "❗️ОБНОВЛЕНИЕ ОСТАТКОВ❗️\n\n💥{model}💥\n\n🔥Цена: {price} zl 🔥\n👇 Заказать 👇\n📦@Diana_Elfbarchik📦\n\n💥 🎉ДОСТАВКА БЕСПЛАТНО! 💥 🎉\n📦🚚При Заказе от 3х штук! 📦🚚\n\n🤪АКТУАЛЬНОЕ НАЛИЧИЕ🤪\n\n{flavors}\n\n{contact_info}"
})
prices = safe_load_json(Path(cfg.get("prices_file", "prices.json")), {})
flavor_emojis = safe_load_json(Path(cfg.get("flavor_emojis_file", "flavor_emojis.json")), {})

# --- подключение к Google Sheets ---
try:
    gc = gspread.service_account(filename=creds_file)
    sh = gc.open_by_key(spreadsheet_key)
    ws = sh.worksheet(sheet_name)
    rows = ws.get_all_values()
    logger.info("Прочитано строк: %d", len(rows))
except Exception as e:
    logger.exception("Ошибка доступа к Google Sheets: %s", e)
    raise

# --- группировка данных по моделям ---
groups = {}
current = None
header_skipped = False

for raw in rows:
    row = [c if c is not None else "" for c in raw] + [""] * (3 - len(raw))
    a0 = row[0].strip()
    a1 = row[1].strip()
    a2 = row[2].strip()

    if not (a0 or a1 or a2):
        continue

    if not header_skipped:
        header_up = " ".join([v.upper() for v in row[:3]])
        if "SKU" in header_up or "TITLE" in header_up or "AVAILABLE" in header_up:
            header_skipped = True
            continue
        header_skipped = True

    # модель-строка: A заполнено, B пусто => это название модели (обычно объединённая ячейка)
    if a0 and not a1:
        current = a0
        groups.setdefault(current, [])
        logger.debug("Найдена модель: %s", current)
        continue

    # вкус со SKU в колонке A
    if re.match(r"^\d+", a0):
        if current is None:
            logger.warning("Встречен вкус до объявления модели: %s / %s", a0, a1)
            current = "UNKNOWN"
            groups.setdefault(current, [])
        flavor_name = a1 or a0
        try:
            qty = int(float(a2)) if a2 else 0
        except Exception:
            m = re.search(r"\d+", a2 or "")
            qty = int(m.group()) if m else 0
        groups.setdefault(current, []).append((flavor_name, qty))
        continue

    # fallback: если B заполнено — скорее всего это вкус
    if a1:
        if current is None:
            logger.warning("Встречен вкус до объявления модели (fallback): %s / %s", a0, a1)
            current = "UNKNOWN"
            groups.setdefault(current, [])
        flavor_name = a1
        try:
            qty = int(float(a2)) if a2 else 0
        except Exception:
            m = re.search(r"\d+", a2 or "")
            qty = int(m.group()) if m else 0
        groups.setdefault(current, []).append((flavor_name, qty))
        continue

    logger.warning("Неопознанная строка: %s", row)

if not groups:
    logger.error("Не найдено сгруппированных данных в таблице.")
    raise SystemExit(1)

logger.info("Найдено моделей: %d", len(groups))

# --- helper: очистка названия вкуса (удаляем повтор модели и лишнее) ---
def clean_flavor_name(raw_flavor: str, model_name: str) -> str:
    if not raw_flavor:
        return ""

    s = raw_flavor.strip()

    # 1) если есть скобки — берем самую внутреннюю скобку
    m = re.findall(r'\(([^()]*)\)', s)
    if m:
        candidate = m[-1].strip()
    else:
        candidate = s

    candidate = candidate.strip()

    # 2) удаляем слова из названия модели (например 'ELF BAR', '1500', 'VOZOL', 'RAVE', '40000', '/')
    # разбиваем модель на токены (буквы/слова/цифры)
    model_tokens = re.split(r'[\s/\\\-\_]+', model_name.lower())
    # удаляем пустые и односимволь мусорные токены
    model_tokens = [t for t in model_tokens if t and len(t) >= 1]

    # поочередно убираем токены как отдельные слова (границы слов), игнорируем числа как отдельного шага
    cleaned = candidate
    for tok in sorted(model_tokens, key=lambda x: -len(x)):  # длинные токены первыми
        if not tok:
            continue
        # экранируем, удаляем вхождения как отдельные слова или рядом с пробелами/скобками
        cleaned = re.sub(r'(?i)\b' + re.escape(tok) + r'\b', ' ', cleaned)

    # 3) убираем лишние символы и скобки, двоеточия, кавычки и т.д.
    cleaned = re.sub(r'[\(\)\[\]\{\}"“”«»:\/\\_]+', ' ', cleaned)
    # убираем лишние пробелы и ведущие/хвостовые дефисы/запятые
    cleaned = re.sub(r'[,;·•]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" -–—\t\n\r ")

    # если после очистки пусто — берём исходный candidate (без модели-удалений)
    if not cleaned:
        cleaned = candidate.strip()

    # Title Case, но сохраняем уже корректные знаки (например "ice" -> "Ice")
    cleaned = " ".join([w.capitalize() for w in cleaned.split()])

    return cleaned

# --- получение эмодзи ---
def get_flavor_emoji(flavor_name: str) -> str:
    key = (flavor_name or "").lower().strip()
    if not key:
        return ""
    # прямой матч
    if key in flavor_emojis:
        return flavor_emojis[key]
    # убрать лишние символы
    key_clean = re.sub(r'[^a-z0-9\s]', '', key)
    if key_clean in flavor_emojis:
        return flavor_emojis[key_clean]
    # пробуем более короткие варианты (последние 2 слова, 1 слово)
    parts = key_clean.split()
    for n in (2, 1):
        if len(parts) >= n:
            k = " ".join(parts[-n:])
            if k in flavor_emojis:
                return flavor_emojis[k]
    # попытка найти эмодзи по отдельным словам
    found = []
    for p in parts:
        if p in flavor_emojis:
            found.append(flavor_emojis[p])
    return " ".join(found)

def format_flavor_line(raw_flavor: str, available: int, model_name: str) -> str:
    clean_name = clean_flavor_name(raw_flavor, model_name)
    emoji = get_flavor_emoji(clean_name.lower())
    if emoji:
        return f"✅ {clean_name} {emoji} ({available} шт.)"
    else:
        return f"✅ {clean_name} ({available} шт.)"

# --- построение поста ---
def normalize_model_key(name: str) -> str:
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9%]", "", s)
    return s

def build_post_text(model_name: str, flavors):
    key = normalize_model_key(model_name)
    price = prices.get(key, "—")
    price_text = f"{price} zl" if price != "—" else "—"
    lines = [format_flavor_line(n, a, model_name) for n, a in sorted(flavors, key=lambda x:(-x[1], x[0]))]
    flavors_text = "\n".join(lines) if lines else "Нет вкусов"
    tpl = templates.get("main_caption")
    contact = templates.get("contact_info", "Оформить заказ: 📩 @Diana_Elfbarchik\nНаша группа: @Elfbarchik_Store\nНаш канал: @ElfBerry_net")
    text = tpl.format(model=model_name, price=price_text, flavors=flavors_text, contact_info=contact)
    return text

# --- выбираем тестовую модель ---
wanted = None
for k in groups.keys():
    if "VOZOL" in k.upper() or "VISTA" in k.upper() or "VOZOL/RAVE" in k.upper():
        wanted = k
        break
if not wanted:
    wanted = next(iter(groups.keys()))

logger.info("Тестовая модель: %s (вкусов: %d)", wanted, len(groups[wanted]))
post = build_post_text(wanted, groups[wanted])

print("\n--- СОЗДАННЫЙ ПОСТ ---\n")
print(post)
print("\n--- КОНЕЦ ---\n")
