# test_gspread.py (обновлённый)
import json
import gspread
from pathlib import Path

cfg = json.load(open("config.json", "r", encoding="utf-8"))
# используем удобный wrapper gspread
gc = gspread.service_account(filename=cfg["credentials_file"])
sh = gc.open_by_key(cfg["spreadsheet_key"])
ws = sh.worksheet(cfg.get("sheet_name", "ОДНОРАЗКИ"))
rows = ws.get_all_values()
print("Прочитано строк:", len(rows))
for r in rows[:10]:
    print(r)
