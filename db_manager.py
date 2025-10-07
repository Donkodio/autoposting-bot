# db_manager.py
from utils import load_json_safe, atomic_write_json
import logging

log = logging.getLogger("autopost.db")

class DBManager:
    def __init__(self, path):
        self.path = path
        self._data = load_json_safe(path, {})
        # ensure keys
        self._data.setdefault("temp_posts", {})
        self._data.setdefault("edits", {})
        atomic_write_json(self.path, self._data)

    def all(self):
        return self._data

    def get_temp_posts(self):
        return self._data.setdefault("temp_posts", {})

    def set_temp_post(self, key, value):
        self._data.setdefault("temp_posts", {})[key] = value
        self._save()

    def del_temp_post(self, key):
        if key in self._data.get("temp_posts", {}):
            del self._data["temp_posts"][key]
            self._save()

    def set_published(self, key, group_key, entry):
        # group_key: "g1" or "g2" for example
        self._data.setdefault(key, {})
        self._data[key].setdefault("published", {})[group_key] = entry
        self._save()

    def update_temp_caption(self, key, caption, message_id=None):
        t = self._data.setdefault("temp_posts", {}).get(key)
        if t:
            t["caption"] = caption
            if message_id:
                t["message_id"] = message_id
            self._save()

    def set_edit_state(self, chat_id: str, state: dict):
        self._data.setdefault("edits", {})[str(chat_id)] = state
        self._save()

    def pop_edit_state(self, chat_id: str):
        self._data.get("edits", {}).pop(str(chat_id), None)
        self._save()

    def get_edit_state(self, chat_id: str):
        return self._data.get("edits", {}).get(str(chat_id))

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except Exception:
            log.exception("DBManager _save failed")
