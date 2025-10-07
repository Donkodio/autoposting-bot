# templates_manager.py
import logging
from utils import load_json_safe

log = logging.getLogger("autopost.templates")

class TemplatesManager:
    def __init__(self, main_path: str, second_path: str):
        self.main = load_json_safe(main_path, {})
        self.second = load_json_safe(second_path, {})

    def get_main(self, key="main_caption"):
        return self.main.get(key)

    def get_contact_info(self):
        return self.main.get("contact_info") or self.main.get("contacts") or ""

    def format_main(self, model, price, flavor_lines, group=1):
        if group == 1:
            tpl = self.main.get("main_caption") or self.main.get("caption")
            contact = self.get_contact_info()
        else:
            tpl = self.second.get("main_caption") or self.second.get("caption_second_group") or self.second.get("caption")
            contact = self.second.get("contact_info") or self.second.get("contacts") or self.get_contact_info()
        if not tpl:
            # fallback minimal
            tpl = "{model}\n{flavor_lines}\n{contact_info}"
        mapping = {
            "model": model,
            "price": price,
            "flavor_lines": flavor_lines,
            "contact_info": contact,
            "contacts": contact
        }
        class _Default(dict):
            def __missing__(self, key):
                return ""
        try:
            return tpl.format_map(_Default(mapping))
        except Exception:
            log.exception("Template format error for %s", model)
            return f"{model}\n{flavor_lines}\n{contact}"
