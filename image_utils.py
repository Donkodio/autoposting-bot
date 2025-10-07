# image_utils.py
import os
import re
import logging
from typing import Optional, Tuple, List
from utils import normalize_text

log = logging.getLogger("autopost.image")

try:
    from rapidfuzz import process, fuzz
    HAS_RAPIDFUZZ = True
except Exception:
    HAS_RAPIDFUZZ = False

def filename_to_key(fn: str) -> str:
    base = os.path.splitext(fn)[0]
    return normalize_text(base)

class ImageIndex:
    def __init__(self, image_folder: str):
        self.image_folder = image_folder
        self.index = {}  # key -> fullpath
        self.keys_list = []
        self._build_index()

    def _build_index(self):
        self.index.clear()
        if not os.path.isdir(self.image_folder):
            log.warning("Image folder not found: %s", self.image_folder)
            return
        for f in sorted(os.listdir(self.image_folder)):
            low = f.lower()
            if not (low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png") or low.endswith(".webp")):
                continue
            key = filename_to_key(f)
            if key not in self.index:
                self.index[key] = os.path.join(self.image_folder, f)
        self.keys_list = list(self.index.keys())
        log.info("IMAGE_INDEX built: %d entries", len(self.index))

    def refresh(self):
        self._build_index()

    def find_best_match(self, model_name: str) -> Tuple[Optional[str], Optional[str], float]:
        nm = normalize_text(model_name)
        if not nm:
            return None, None, 0.0

        if nm in self.index:
            return nm, self.index[nm], 100.0

        variants = [nm, nm.replace(" ", ""), nm.replace("percent", "%")]
        for v in variants:
            if v in self.index:
                return v, self.index[v], 95.0

        if HAS_RAPIDFUZZ and self.keys_list:
            best = process.extractOne(nm, self.keys_list, scorer=fuzz.WRatio)
            if best:
                match_key, score, _ = best
                return match_key, self.index.get(match_key), float(score)

        model_words = set(nm.split())
        best_key = None
        best_score = 0.0
        for k in self.keys_list:
            k_words = set(k.split())
            overlap = len(model_words & k_words)
            score = overlap
            if score > best_score:
                best_score = score
                best_key = k
        if best_key:
            return best_key, self.index.get(best_key), float(best_score)

        return None, None, 0.0

_global_indexes = {}

def get_global_index(folder: str) -> ImageIndex:
    key = os.path.abspath(folder)
    if key not in _global_indexes:
        _global_indexes[key] = ImageIndex(folder)
    return _global_indexes[key]

def get_image_for_model(model_name: str, image_folder: str) -> Optional[str]:
    idx = get_global_index(image_folder)
    matched_key, path, score = idx.find_best_match(model_name)
    if path and os.path.exists(path):
        return path
    return None
