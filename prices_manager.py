# prices_manager.py
import os
import logging
from utils import load_json_safe, atomic_write_json, normalize_text

log = logging.getLogger("autopost.prices")

class PricesManager:
    def __init__(self, path: str, min_price: int = 1, max_price: int = 100000):
        self.path = path
        self.min_price = min_price
        self.max_price = max_price
        self.raw = load_json_safe(path, {})
        # normalized lookup
        self.normalized = {}
        for k, v in self.raw.items():
            self.normalized[normalize_text(k)] = v

    def get(self, model: str):
        return self.normalized.get(normalize_text(model))

    def set(self, model: str, price):
        # validate numeric if not empty
        if price in (None, ""):
            # user intends empty -> set but do not overwrite valid existing price with empty
            self.normalized.setdefault(normalize_text(model), "")
            self.raw.setdefault(model, "")
            atomic_write_json(self.path, self.raw)
            return True

        try:
            p = int(price)
        except Exception:
            log.warning("Attempt to set non-int price %r for %s", price, model)
            return False

        if p < self.min_price or p > self.max_price:
            log.warning("Price %d for %s out of allowed bounds [%d,%d]", p, model, self.min_price, self.max_price)
            return False

        # persist: try to preserve readable key if exists
        # if model key exists verbatim in raw, update it, else add model as given
        found_key = None
        for raw_k in list(self.raw.keys()):
            if normalize_text(raw_k) == normalize_text(model):
                found_key = raw_k
                break
        if found_key:
            self.raw[found_key] = p
        else:
            self.raw[model] = p
        self.normalized[normalize_text(model)] = p
        atomic_write_json(self.path, self.raw)
        log.info("Set price for %s -> %s", model, p)
        return True

    def add_missing_models(self, model_list):
        changed = False
        for m in model_list:
            nk = normalize_text(m)
            if nk not in self.normalized:
                self.raw.setdefault(m, "")
                self.normalized[nk] = ""
                changed = True
        if changed:
            atomic_write_json(self.path, self.raw)
            log.info("Updated prices.json with new empty entries: %d", len(model_list))
