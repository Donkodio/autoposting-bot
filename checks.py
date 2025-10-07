# checks.py
import re
import os
import logging
from utils import normalize_text
from image_utils import get_image_for_model

log = logging.getLogger("autopost.checks")

def check_flavor_line_has_emoji(line: str):
    # simple heuristic: an emoji is any non-ascii-symbol or certain emoji chars
    # we assume emoji present if there's at least one unicode char outside basic punctuation/letters/digits
    # or presence of common emoji characters like '🍏','🥭','🫐','🍉','🍒' etc.
    # quicker heuristic: remove ascii letters/digits/spaces and see if anything left
    leftover = re.sub(r"[A-Za-z0-9\s\(\)\-\,\+\.\:]", "", line)
    return len(leftover.strip()) > 0

def check_caption_template_has_price(caption: str):
    # look for pattern like '🔥Цена: <digits>'
    return bool(re.search(r'🔥\s*Цена\s*:\s*\d+', caption))

def check_caption_contains_flavors(caption: str):
    return "✅" in caption

def check_image_exists_for_model(model: str, image_folder: str) -> bool:
    p = get_image_for_model(model, image_folder)
    return p is not None and os.path.exists(p)

def run_checks_on_temp_posts(temp_posts: dict, prices_manager, image_folder: str):
    """
    temp_posts: dict of temp posts {key: {model, caption, message_id, ...}}
    prices_manager: instance of PricesManager (with .get())
    Return list of error strings
    """
    errors = []
    for k, p in temp_posts.items():
        model = p.get("model", "")
        caption = p.get("caption", "")
        # price
        price = prices_manager.get(model)
        if price in (None, "", "—"):
            errors.append(f"- У модели {model} цена не указана.")
        # price line presence
        if not check_caption_template_has_price(caption):
            errors.append(f"- У модели {model} отсутствует или некорректна строка с ценой.")
        # flavors
        if not check_caption_contains_flavors(caption):
            errors.append(f"- У модели {model} отсутствуют вкусы в тексте.")
        else:
            # check each flavor line has emoji
            for line in caption.splitlines():
                if line.strip().startswith("✅"):
                    if not check_flavor_line_has_emoji(line):
                        errors.append(f"- У модели {model} в строке '{line.strip()}' отсутствуют эмодзи вкуса.")
        # image
        if not check_image_exists_for_model(model, image_folder):
            errors.append(f"- У модели {model} отсутствует изображение.")
    return errors
