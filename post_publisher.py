# post_publisher.py — логика чтения таблицы, формирования текстов и отправки/редактирования/публикации постов
import logging
import os
import time
from datetime import datetime
from typing import Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utils import read_json, write_json, normalize, load_sheet_rows

log = logging.getLogger("autopost.publisher")


class PostPublisher:
    def __init__(self, config: Dict, image_index):
        self.config = config
        self.image_index = image_index
        self.db_file = config.get("db_file", "message_ids.json")
        self.prices_file = config.get("prices_file", "prices.json")
        # load prices with normalized keys
        raw_prices = read_json(self.prices_file)
        self.prices = {normalize(k): v for k, v in raw_prices.items()} if isinstance(raw_prices, dict) else {}

    # ---------------- Spreadsheet parsing ----------------
    def sheet_rows(self):
        return load_sheet_rows(self.config.get("spreadsheet_key"), self.config.get("sheet_name", "ОДНОРАЗКИ"), self.config.get("credentials_file"))

    def group_rows_into_models(self, rows) -> Dict[str, List[Dict]]:
        """
        Возвращает словарь normalized_model -> { 'model': display_name, 'flavors': [ {flavor, available}, ... ] }
        Приспосабливаемся к структуре: колонки SKU / Title / Available или похожей.
        """
        models = {}
        if not rows:
            return models
        headers = rows[0]
        # try to detect columns
        try:
            idx_sku = 0
            idx_title = headers.index("Title") if "Title" in headers else 1
            idx_avail = headers.index("Available") if "Available" in headers else 2
        except Exception:
            idx_sku, idx_title, idx_avail = 0, 1, 2

        current_model = None
        for row in rows[1:]:
            # normalize row length
            cols = (row + ["", "", ""])[: max(idx_sku, idx_title, idx_avail) + 1]
            sku = cols[idx_sku].strip()
            title = cols[idx_title].strip()
            avail = cols[idx_avail].strip()

            # detect header model rows (sku filled & title empty OR title looks like model)
            is_model_header = False
            model_name = None
            if sku and not title:
                is_model_header = True
                model_name = sku
            elif not sku and title and not avail:
                is_model_header = True
                model_name = title
            elif sku and "ELF" in sku.upper() or "VOZOL" in sku.upper() or "WAKA" in sku.upper():
                # heuristics
                is_model_header = True
                model_name = sku

            if is_model_header:
                current_model = model_name.strip()
                key = normalize(current_model)
                if key not in models:
                    models[key] = {"model": current_model, "flavors": []}
                continue

            # treat as flavor row
            if current_model and title:
                # extract flavor inside parentheses if exists
                flavor = title
                if "(" in flavor and ")" in flavor:
                    start = flavor.find("(")
                    end = flavor.rfind(")")
                    inner = flavor[start+1:end].strip()
                    if inner:
                        flavor = inner
                try:
                    avail_int = int(avail) if avail.isdigit() else 0
                except Exception:
                    avail_int = 0
                models[normalize(current_model)]["flavors"].append({"flavor": flavor.strip(), "available": avail_int})
        return models

    # ---------------- Caption / templates ----------------
    def load_templates(self):
        # templates file paths provided via config
        templ_file = self.config.get("templates_file", "templates.json")
        templ2_file = self.config.get("templates_second_group_file", "templates_second_group.json")
        t1 = read_json(templ_file) if os.path.exists(templ_file) else None
        t2 = read_json(templ2_file) if os.path.exists(templ2_file) else None
        # provide defaults
        if not t1:
            t1 = {
                "main_caption": "❗️ОБНОВЛЕНИЕ ОСТАТКОВ❗️\n\n💥{model}💥\n\n🔥Цена: {price} zl 🔥\n👇 Заказать 👇\n📦@Diana_Elfbarchik📦\n\n{extra}\n\n🤪АКТУАЛЬНОЕ НАЛИЧИЕ🤪\n\n{flavors}\n\n{contact_info}",
                "contact_info": "Оформить заказ: 📩 @Diana_Elfbarchik\nНаша группа: @Elfbarchik_Store\nНаш канал: @ElfBerry_net",
                "extra": "💥 🎉ДОСТАВКА БЕСПЛАТНО! 💥 🎉\n📦🚚При Заказе от 3х штук! 📦🚚"
            }
        if not t2:
            t2 = {
                "main_caption": "❗️ОБНОВЛЕНИЕ ОСТАТКОВ❗️\n\n💥{model}💥\n\n🔥Цена: {price} zl 🔥\n👇 Заказать 👇\n📦@VapeRoyalePL📦\n\n{extra}\n\n🤪АКТУАЛЬНОЕ НАЛИЧИЕ🤪\n\n{flavors}\n\nОформить заказ: 📩 @VapeRoyalePL\nНаш канал: @UA_Smoke_Shop",
                "extra": "💥 🎉ДОСТАВКА БЕСПЛАТНО! 💥 🎉\n📦🚚При Заказе от 3х штук! 📦🚚"
            }
        return t1, t2

    def format_caption(self, model_display: str, flavors: List[Dict], group: int = 1):
        t1, t2 = self.load_templates()
        tpl = t1 if group == 1 else t2
        price = self.prices.get(normalize(model_display), "—")
        # build flavors block
        lines = []
        for f in sorted(flavors, key=lambda x: -x["available"]):
            em = ""  # emoji mapping can be added later
            lines.append(f"✅ {f['flavor']}{(' '+em) if em else ''} ({f['available']} шт.)")
        flavors_block = "\n".join(lines) if lines else "—"
        text = tpl["main_caption"].format(model=model_display, price=price, flavors=flavors_block, contact_info=tpl.get("contact_info", ""), extra=tpl.get("extra",""))
        return text

    # ---------------- DB helpers ----------------
    def load_db(self):
        return read_json(self.db_file) if os.path.exists(self.db_file) else {}

    def save_db(self, data):
        write_json(self.db_file, data)

    def save_prices(self):
        # write back prices with original keys? we write normalized keys for simplicity
        write_json(self.prices_file, self.prices)

    # ---------------- Core: create posts in user chat ----------------
    def create_posts_in_chat(self, bot, user_chat_id: int):
        rows = self.sheet_rows()
        grouped = self.group_rows_into_models(rows)
        if not grouped:
            bot.send_message(chat_id=user_chat_id, text="Нет данных для создания постов (таблица).")
            return

        db = {}
        db["temp_posts"] = {}
        created = 0
        for key, info in grouped.items():
            caption = self.format_caption(info["model"], info["flavors"], group=1)
            img_path = self.image_index.find_best_image(info["model"])
            # auto-add missing price entries
            if normalize(info["model"]) not in self.prices:
                self.prices[normalize(info["model"])] = ""
                log.info("Добавлена модель без цены в prices: %s", info["model"])
            try:
                if img_path and os.path.isfile(img_path):
                    with open(img_path, "rb") as ph:
                        sent = bot.send_photo(chat_id=user_chat_id, photo=ph, caption=caption, reply_markup=self._per_post_kb(key))
                elif img_path:
                    # img_path might be URL (fallback) or not file -> pass directly
                    sent = bot.send_photo(chat_id=user_chat_id, photo=img_path, caption=caption, reply_markup=self._per_post_kb(key))
                else:
                    sent = bot.send_message(chat_id=user_chat_id, text=caption, reply_markup=self._per_post_kb(key))
                db["temp_posts"][key] = {"message_id": sent.message_id, "model": info["model"], "chat_id": user_chat_id, "caption": caption, "posted": False}
                created += 1
                time.sleep(0.4)
            except Exception as e:
                log.exception("Ошибка отправки поста для %s: %s", info["model"], e)

        self.save_db(db)
        self.save_prices()
        bot.send_message(chat_id=user_chat_id, text=f"Создано {created} временных постов. Проверь и нажми «Запостить».", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Запостить все", callback_data="post_all")]]))
        log.info("Создано %d временных постов", created)

    # ---------------- per-post keyboard ----------------
    def _per_post_kb(self, model_key: str):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Запостить", callback_data=f"post|{model_key}"),
                                    InlineKeyboardButton("Изменить", callback_data=f"edit|{model_key}")],
                                   [InlineKeyboardButton("В группу 1", callback_data=f"send_g1|{model_key}"),
                                    InlineKeyboardButton("В группу 2", callback_data=f"send_g2|{model_key}")]])
        return kb

    # ---------------- publish single / all ----------------
    def publish_single_to_group(self, bot, model_key: str, group_num: int, user_id: int):
        db = self.load_db()
        temp_posts = db.get("temp_posts", {})
        post = temp_posts.get(model_key)
        if not post:
            bot.send_message(chat_id=user_id, text="Временный пост не найден.")
            return
        target = self.config.get("group_chat_id") if group_num == 1 else self.config.get("second_group_chat_id")
        if not target:
            bot.send_message(chat_id=user_id, text="ID целевой группы не настроен в config.json.")
            return
        try:
            # we use send_photo by copying file from user chat (forward or copy)
            # simpler: forward original message (keeps photo)
            fwd = bot.forward_message(chat_id=target, from_chat_id=post["chat_id"], message_id=post["message_id"])
            # store mapping
            groups = db.setdefault("groups", {})
            gstore = groups.setdefault(str(target), {})
            gstore[model_key] = fwd.message_id
            post["posted"] = True
            # remove temp post
            del temp_posts[model_key]
            db["temp_posts"] = temp_posts
            self.save_db(db)
            bot.send_message(chat_id=user_id, text=f"Модель {post['model']} отправлена в группу {group_num}.")
        except Exception as e:
            log.exception("Ошибка отправки модели %s в группу %s: %s", post.get("model"), group_num, e)
            bot.send_message(chat_id=user_id, text=f"Ошибка при отправке: {e}")

    def publish_all(self, bot, user_id: int):
        db = self.load_db()
        temp_posts = db.get("temp_posts", {})
        if not temp_posts:
            bot.send_message(chat_id=user_id, text="Нет постов для публикации.")
            return
        success = 0
        failed = 0
        for k, p in list(temp_posts.items()):
            try:
                # forward to group1
                g1 = self.config.get("group_chat_id")
                fwd1 = bot.forward_message(chat_id=g1, from_chat_id=p["chat_id"], message_id=p["message_id"])
                # create second-group post with second template (send photo from local if needed)
                # find flavors from sheet
                rows = self.sheet_rows()
                grouped = self.group_rows_into_models(rows)
                flavors = grouped.get(k, {}).get("flavors", [])
                caption2 = self.format_caption(p["model"], flavors, group=2)
                img = self.image_index.find_best_image(p["model"])
                if img and os.path.isfile(img):
                    with open(img, "rb") as ph:
                        sent2 = bot.send_photo(chat_id=self.config.get("second_group_chat_id"), photo=ph, caption=caption2)
                else:
                    # fallback forward original for second group too
                    sent2 = bot.forward_message(chat_id=self.config.get("second_group_chat_id"), from_chat_id=p["chat_id"], message_id=p["message_id"])
                # record
                groups = db.setdefault("groups", {})
                groups.setdefault(str(self.config.get("group_chat_id")), {})[k] = fwd1.message_id
                groups.setdefault(str(self.config.get("second_group_chat_id")), {})[k] = sent2.message_id
                # mark posted
                del temp_posts[k]
                success += 1
                time.sleep(0.3)
            except Exception as e:
                log.exception("Ошибка при массовой публикации %s: %s", k, e)
                failed += 1
        db["temp_posts"] = temp_posts
        write_json(self.db_file, db)
        bot.send_message(chat_id=user_id, text=f"Публикация завершена. Успешно: {success}, не удалось: {failed}")

    # ---------------- editing flow ----------------
    def start_editing(self, bot, user_id: int, model_key: str):
        db = self.load_db()
        temp_posts = db.get("temp_posts", {})
        post = temp_posts.get(model_key)
        if not post:
            bot.send_message(chat_id=user_id, text="Пост для редактирования не найден.")
            return
        # send prompt
        sent = bot.send_message(chat_id=user_id, text=f"Редактирование: {post['model']}\nОтправь новое фото или/и текст (только текст заменит подпись).")
        # we store editing marker in db under special key
        editing = db.setdefault("_editing", {})
        editing[str(user_id)] = {"key": model_key, "prompt_id": sent.message_id}
        self.save_db(db)

    def apply_edit(self, bot, user_id: int, new_text: str = None, new_photo_file_id: str = None):
        db = self.load_db()
        editing = db.get("_editing", {}).get(str(user_id))
        if not editing:
            bot.send_message(chat_id=user_id, text="Нет активного редактирования.")
            return
        key = editing["key"]
        post = db.get("temp_posts", {}).get(key)
        if not post:
            bot.send_message(chat_id=user_id, text="Временный пост не найден для применения изменений.")
            db.get("_editing", {}).pop(str(user_id), None)
            self.save_db(db)
            return
        # update caption if new_text provided
        if new_text:
            # update price if found in new_text
            import re
            m = re.search(r"🔥Цена:\s*(\d+)\s*zl", new_text)
            if m:
                price = int(m.group(1))
                self.prices[normalize(post["model"])] = price
                self.save_prices()
            post["caption"] = new_text
            # edit message in user chat
            try:
                # try edit caption (if photo message)
                bot.edit_message_caption(chat_id=post["chat_id"], message_id=post["message_id"], caption=new_text, reply_markup=self._per_post_kb(key))
            except Exception:
                try:
                    bot.edit_message_text(chat_id=post["chat_id"], message_id=post["message_id"], text=new_text, reply_markup=self._per_post_kb(key))
                except Exception as e:
                    log.exception("Не удалось обновить сообщение при редактировании: %s", e)
        # photo editing currently: if new_photo_file_id provided we replace by sending new photo and store new message_id
        if new_photo_file_id:
            try:
                # delete old temp message and send new photo with caption
                try:
                    bot.delete_message(chat_id=post["chat_id"], message_id=post["message_id"])
                except Exception:
                    pass
                # new_photo_file_id may be file_id or local path — we pass as-is
                if os.path.exists(new_photo_file_id):
                    with open(new_photo_file_id, "rb") as ph:
                        sent = bot.send_photo(chat_id=post["chat_id"], photo=ph, caption=post["caption"], reply_markup=self._per_post_kb(key))
                else:
                    sent = bot.send_photo(chat_id=post["chat_id"], photo=new_photo_file_id, caption=post["caption"], reply_markup=self._per_post_kb(key))
                post["message_id"] = sent.message_id
            except Exception as e:
                log.exception("Ошибка замены фото при редактировании: %s", e)

        # cleanup editing marker
        db["_editing"].pop(str(user_id), None)
        self.save_db(db)
        bot.send_message(chat_id=user_id, text="Изменения применены.")
