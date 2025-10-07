# sheet_helpers.py
import logging
import re
from typing import List, Dict, Any
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from utils import load_json_safe, normalize_text

log = logging.getLogger("autopost.sheet")

# Загружает config.json автоматом
CFG = load_json_safe("config.json", {}) or {}
CREDENTIALS_FILE = CFG.get("credentials_file", "credentials.json")
SPREADSHEET_KEY = CFG.get("spreadsheet_key")
SHEET_NAME = CFG.get("sheet_name", "ОДНОРАЗКИ")


def get_spreadsheet():
    """
    Авторизация и возврат объекта gspread.Spreadsheet
    """
    if not CREDENTIALS_FILE or not SPREADSHEET_KEY:
        log.error("sheet_helpers: credentials_file or spreadsheet_key not set in config.json")
        return None
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            CREDENTIALS_FILE,
            ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        )
        client = gspread.authorize(creds)
        sh = client.open_by_key(SPREADSHEET_KEY)
        return sh
    except Exception:
        log.exception("Failed to authorize/access spreadsheet")
        return None


def get_data(sheet_name: str = None) -> List[Dict[str, Any]]:
    """
    Читает таблицу и возвращает список записей:
    [{'model': 'ELF BAR/1500', 'flavor': 'Blueberry', 'available': 12}, ...]
    Алгоритм устойчив к разным структурам: пытается найти колонки Title/Available,
    использует эвристики (строки заголовков модели с пустым Title и т.д.).
    """
    sheet_name = sheet_name or SHEET_NAME
    sh = get_spreadsheet()
    if not sh:
        return []

    try:
        ws = sh.worksheet(sheet_name)
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return []

        headers = rows[0]
        # определяем индексы колонок Title/Available (fallback)
        try:
            idx_title = headers.index("Title")
        except ValueError:
            idx_title = 1 if len(headers) > 1 else 0
        try:
            idx_avail = headers.index("Available")
        except ValueError:
            idx_avail = 2 if len(headers) > 2 else 2

        results = []
        current_model = None
        for r in rows[1:]:
            sku = (r[0] if len(r) > 0 else "").strip()
            title = (r[idx_title] if len(r) > idx_title else "").strip()
            avail = (r[idx_avail] if len(r) > idx_avail else "").strip()

            # heurистика: строки, где есть sku и пустой title — заголовок модели
            if sku and not title:
                current_model = sku
                continue
            # иногда модель указана в Title, а sku пуст
            if not sku and title and not avail:
                current_model = title
                continue
            # если у нас есть текущая модель и есть title => это вкус
            if current_model and title:
                m = re.match(r'^(.*?)\s*\((.+)\)$', title)
                if m:
                    # Title может быть "Name (Flavor)"
                    flavor = m.group(2).strip()
                else:
                    flavor = title.strip()
                try:
                    available_i = int(avail) if avail and str(avail).strip().isdigit() else 0
                except Exception:
                    available_i = 0
                results.append({"model": current_model, "flavor": flavor, "available": available_i})
        log.info("Read rows from sheet: %d", len(results))
        return results
    except Exception:
        log.exception("Error reading sheet data")
        return []
