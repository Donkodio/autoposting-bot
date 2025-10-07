# publisher.py
import time
import logging
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from utils import normalize_text
from image_utils import get_image_for_model
from templates_manager import TemplatesManager
from checks import run_checks_on_temp_posts

log = logging.getLogger("autopost.publisher")

class Publisher:
    def __init__(self, bot, db_manager, prices_manager, templates_manager, image_folder, group1_id, group2_id):
        self.bot = bot
        self.db = db_manager
        self.prices = prices_manager
        self.templates = templates_manager
        self.image_folder = image_folder
        self.group1_id = group1_id
        self.group2_id = group2_id

    def build_temp_buttons(self, key):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Запостить", callback_data=f"post_{key}"),
             InlineKeyboardButton("Изменить", callback_data=f"edit_{key}")],
        ])

    def publish_temp_posts(self, chat_id, records):
        # group records by model
        models = {}
        for r in records:
            k = normalize_text(r['model'])
            models.setdefault(k, {'model': r['model'], 'flavors': []})
            models[k]['flavors'].append({'flavor': r['flavor'], 'available': r['available']})

        created = 0
        missing_prices = set()
        self.db._data["temp_posts"] = {}  # clear
        self.db._save()

        for k, data in sorted(models.items()):
            caption = self.templates.format_main(data['model'],
                                                 self.prices.get(data['model']) or "—",
                                                 self._flavor_lines(data['flavors']),
                                                 group=1)
            img_path = get_image_for_model(data['model'], self.image_folder)
            try:
                if img_path:
                    with open(img_path, 'rb') as ph:
                        sent = self.bot.send_photo(chat_id=chat_id, photo=ph, caption=caption, reply_markup=self.build_temp_buttons(k))
                else:
                    sent = self.bot.send_message(chat_id=chat_id, text=caption, reply_markup=self.build_temp_buttons(k))
                self.db.set_temp_post(k, {'message_id': sent.message_id, 'model': data['model'], 'chat_id': chat_id, 'caption': caption, 'posted': False})
                created += 1
                if self.prices.get(data['model']) in (None, "", "—"):
                    missing_prices.add(data['model'])
                time.sleep(0.07)
            except Exception:
                log.exception("publish_temp_posts: failed for %s", data['model'])
        return created, sorted(missing_prices)

    def publish_one(self, key):
        temp = self.db.get_temp_posts().get(key)
        if not temp:
            raise RuntimeError("temp missing")
        # forward to g1
        try:
            fwd = self.bot.forward_message(chat_id=self.group1_id, from_chat_id=temp['chat_id'], message_id=temp['message_id'])
            self.db.set_published(key, "g1", {'message_id': fwd.message_id, 'chat_id': self.group1_id})
            log.info("Forwarded %s to G1 id=%s", temp['model'], fwd.message_id)
        except Exception:
            log.exception("publish_one: forward to g1 failed for %s", temp['model'])
            raise

        # g2: send photo with second template
        try:
            # gather flavors from sheet: the caller should pass best flavors; we'll rely on caption in temp for simplicity here
            caption2 = self.templates.format_main(temp['model'], self.prices.get(temp['model']) or "—", self._extract_flavor_lines_from_caption(temp['caption']), group=2)
            img_path = get_image_for_model(temp['model'], self.image_folder)
            if img_path:
                with open(img_path, 'rb') as ph:
                    sent2 = self.bot.send_photo(chat_id=self.group2_id, photo=ph, caption=caption2)
            else:
                sent2 = self.bot.send_message(chat_id=self.group2_id, text=caption2)
            self.db.set_published(key, "g2", {'message_id': sent2.message_id, 'chat_id': self.group2_id})
            log.info("Posted %s to G2 id=%s", temp['model'], sent2.message_id)
        except Exception:
            log.exception("publish_one: send to g2 failed for %s", temp['model'])
            raise

        # mark posted
        temp['posted'] = True
        self.db.set_temp_post(key, temp)
        return True

    def update_published_caption(self, key, group="g1"):
        # update caption for published message of that key and group
        pub = self.db._data.get(key, {}).get("published", {}).get(group)
        temp = self.db.get_temp_posts().get(key)
        if not pub or not temp:
            raise RuntimeError("missing publish/temp")
        chat_id = pub['chat_id']
        message_id = pub['message_id']
        # create new caption from current temp (or current sheet if more accurate)
        caption_new = temp.get('caption')
        try:
            self.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption_new)
            # update DB status marker
            self.db.set_published(key, group, {'message_id': message_id, 'chat_id': chat_id, 'status': 'updated'})
            return True
        except Exception:
            log.exception("update_published_caption failed for %s %s", key, group)
            # fallback: send new message instead
            try:
                # send new message to chat
                img_path = get_image_for_model(temp['model'], self.image_folder)
                if img_path:
                    with open(img_path, 'rb') as ph:
                        sent = self.bot.send_photo(chat_id=chat_id, photo=ph, caption=caption_new)
                else:
                    sent = self.bot.send_message(chat_id=chat_id, text=caption_new)
                # update DB with new message id
                self.db.set_published(key, group, {'message_id': sent.message_id, 'chat_id': chat_id, 'status': 'recreated'})
                return True
            except Exception:
                log.exception("update_published_caption fallback failed")
                return False

    def check_temp_posts(self):
        temp = self.db.get_temp_posts()
        return run_checks_on_temp_posts(temp, self.prices, self.image_folder)

    def _flavor_lines(self, flavors):
        lines = []
        for f in sorted(flavors, key=lambda x: -int(x.get('available', 0))):
            em = ""  # emoji resolution left to higher level (we keep template)
            lines.append(f"✅ {f.get('flavor','')} {em} ({f.get('available', 0)} шт.)")
        return "\n".join(lines)

    def _extract_flavor_lines_from_caption(self, caption: str):
        # crude: return lines between "АКТУАЛЬНОЕ НАЛИЧИЕ" and contact_info
        lines = caption.splitlines()
        start = 0
        for i,l in enumerate(lines):
            if "АКТУАЛЬНОЕ НАЛИЧИЕ" in l:
                start = i+1
                break
        return "\n".join(lines[start:]).strip()
