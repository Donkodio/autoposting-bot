# storage.py
import json
from pathlib import Path

def read_json_file(p: Path):
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def write_json_file(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_config(path: Path):
    cfg = read_json_file(path)
    # minimal validation
    if not cfg.get("bot_token"):
        raise RuntimeError("config.json missing bot_token")
    return cfg
