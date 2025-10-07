# utils.py
import json
import os
import re
import tempfile
import shutil
import logging
from typing import Any

log = logging.getLogger("autopost.utils")


def normalize_text(s: str) -> str:
    """
    Универсальная нормализация названий:
    - lower
    - заменяет % на percent
    - заменяет / _ - . на пробелы
    - вставляет пробел между буквами и цифрами (elfbar1500 -> elf bar 1500)
    - удаляет скобки и лишние пробелы
    """
    if not s:
        return ""
    s = str(s)
    s = s.lower()
    s = s.replace("%", " percent ")
    s = s.replace("/", " ")
    s = s.replace("\\", " ")
    s = s.replace("_", " ")
    s = s.replace(".", " ")
    s = re.sub(r"[\(\)\[\]\{\}]", " ", s)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_json_safe(path: str, default: Any = None) -> Any:
    """Безопасно загружает JSON, возвращает default при ошибках."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        log.exception("JSON decode error for %s", path)
        return default
    except Exception:
        log.exception("Failed to load json %s", path)
        return default


def atomic_write_json(path: str, data: Any):
    """
    Атомарно записывает JSON (через временный файл + замена).
    Сохраняет права/атрибуты не сохраняем — простая реализация.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="tmp_json_")
    os.close(tmp_fd)
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # move to final
        shutil.move(tmp_path, path)
    except Exception:
        log.exception("atomic write failed for %s", path)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise
