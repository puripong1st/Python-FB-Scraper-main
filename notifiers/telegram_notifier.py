"""
notifiers/telegram_notifier.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TelegramNotifier  — ส่งข้อความ / รูปภาพพร้อม inline keyboard
TelegramListener  — polling loop รับ callback query (บันทึก/ลบ)
"""

import threading
import requests
from datetime import datetime, timedelta
from ssl_helper import get_ca_bundle


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFIER
# ─────────────────────────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.api_url   = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def _send(self, text: str, keyboard: dict = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            ca_bundle = get_ca_bundle()
            resp = requests.post(
                self.api_url,
                json=payload,
                timeout=10,
                verify=ca_bundle if ca_bundle else True,
            )
            return resp.status_code == 200
        except requests.RequestException as e:
            print(f"⚠️ [Telegram] ส่งข้อความไม่สำเร็จ: {e}")
            return False

    def _send_photo(self, photo_url: str, caption: str, keyboard: dict = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            ca_bundle = get_ca_bundle()
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                json=payload,
                timeout=10,
                verify=ca_bundle if ca_bundle else True,
            )
            return resp.status_code == 200
        except requests.RequestException as e:
            print(f"⚠️ [Telegram] ส่งรูปภาพไม่สำเร็จ: {e}")
            return False

    def send_start(self, page_count: int = 0, keyword_count: int = 0, loop_min: int = 0, hours_back: int = 6):
        kw_label = f"{keyword_count} คำ" if keyword_count else "ทั้งหมด (ไม่กรอง)"
        self._send(
            f"🟢 <b>เริ่มระบบ Scraper แล้ว</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 เพจที่ติดตาม: <b>{page_count} เพจ</b>\n"
            f"🔑 Keywords: <b>{kw_label}</b>\n"
            f"🔄 วนซ้ำทุก: <b>{loop_min} นาที</b>\n"
            f"📅 ดึงย้อนหลัง: <b>{hours_back} ชั่วโมง</b>\n"
            f"🔔 ช่องทาง: <b>Telegram ✅</b>\n"
            f"⏰ เวลาเริ่ม: <b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</b>"
        )

    def send_post(self, page_name: str, page_url: str, post_url: str, content: str,
                  found_keywords: list, image_url: str = None):
        kw_str      = ", ".join(found_keywords) if found_keywords else "-"
        kw_count    = len(found_keywords)
        char_count  = len(content)
        detected_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💾 บันทึกข่าวนี้", "callback_data": "save_news"},
                    {"text": "🗑️ ลบข้อความ",    "callback_data": "delete_news"},
                ],
                [
                    {"text": "🌐 ดูเพจต้นทาง", "url": page_url},
                    {"text": "📰 เปิดโพสต์",    "url": post_url},
                ],
            ]
        }

        if image_url:
            snippet = content[:800] + "\n\n...[อ่านต่อในลิงก์]" if len(content) > 800 else content
            caption = (
                f"📢 <b>ข่าวจาก {page_name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{snippet}\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔑 <b>Keywords ({kw_count} คำ):</b> {kw_str}\n"
                f"📝 <b>ความยาว:</b> {char_count:,} ตัวอักษร\n"
                f"🕐 <b>ตรวจพบ:</b> {detected_at} น.\n"
                f"🔗 <a href='{post_url}'>เปิดโพสต์ต้นฉบับ</a>"
            )
            self._send_photo(image_url, caption, keyboard)
        else:
            snippet = content[:2000] + "\n\n...[อ่านต่อในลิงก์]" if len(content) > 2000 else content
            text = (
                f"📢 <b>ข่าวจาก {page_name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{snippet}\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔑 <b>Keywords ({kw_count} คำ):</b> {kw_str}\n"
                f"📝 <b>ความยาว:</b> {char_count:,} ตัวอักษร\n"
                f"🕐 <b>ตรวจพบ:</b> {detected_at} น.\n"
                f"🔗 <a href='{post_url}'>เปิดโพสต์ต้นฉบับ</a>"
            )
            self._send(text, keyboard)

    def send_cycle_complete(self, duration_sec: float, next_run_min: int, total_new: int = 0, pages_count: int = 0):
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        next_time  = (datetime.now() + timedelta(minutes=next_run_min)).strftime("%H:%M")
        posts_line = f"🆕 โพสต์ใหม่: <b>{total_new} โพสต์</b>" if total_new > 0 else "🆕 โพสต์ใหม่: <b>ไม่มีโพสต์ใหม่</b>"
        self._send(
            f"✅ <b>สแกนรอบนี้เสร็จสิ้น</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{posts_line}\n"
            f"📋 เพจที่สแกน: <b>{pages_count} เพจ</b>\n"
            f"⏱ ระยะเวลา: <b>{mins}m {secs}s</b>\n"
            f"⏳ รอบถัดไปในอีก: <b>{next_run_min} นาที</b>\n"
            f"🕐 ประมาณ: <b>{next_time} น.</b>"
        )

    def send_obstacle(self, obstacle_type: str, page_url: str = ""):
        page_line = f"\n🌐 เพจที่ติดปัญหา: {page_url}" if page_url else ""
        self._send(
            f"🚨 <b>บอทติดหน้า {obstacle_type}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ เวลา: <b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} น.</b>{page_line}\n\n"
            f"📌 <b>วิธีแก้ไข:</b>\n"
            f"1️⃣ เปิดหน้าต่าง Browser\n"
            f"2️⃣ แก้ไขตามที่หน้าจอแจ้ง\n"
            f"3️⃣ กดปุ่ม <b>Resume</b> บนโปรแกรม"
        )

    def send_stopped(self, total_runtime_sec: float = 0, total_posts_found: int = 0):
        if total_runtime_sec > 0:
            h = int(total_runtime_sec // 3600)
            m = int((total_runtime_sec % 3600) // 60)
            s = int(total_runtime_sec % 60)
            runtime_str = f"{h}ชม. {m}นาที {s}วิ" if h > 0 else f"{m}นาที {s}วิ"
        else:
            runtime_str = "-"
        self._send(
            f"🔴 <b>ระบบ Scraper หยุดทำงานแล้ว</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏱ รันไปทั้งหมด: <b>{runtime_str}</b>\n"
            f"📰 โพสต์ที่พบทั้งหมด: <b>{total_posts_found} โพสต์</b>\n"
            f"🕐 หยุดเมื่อ: <b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} น.</b>\n"
            f"<i>หยุดโดยผู้ใช้งาน — กด Start เพื่อเริ่มใหม่</i>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# LISTENER (callback query polling)
# ─────────────────────────────────────────────────────────────────────────────

class TelegramListener(threading.Thread):
    def __init__(self, bot_token: str):
        super().__init__(daemon=True)
        self.bot_token   = bot_token
        self.api_url     = f"https://api.telegram.org/bot{bot_token}/"
        self.offset      = None
        self._stop_event = threading.Event()

    def run(self):
        if not self.bot_token:
            return
        while not self._stop_event.is_set():
            try:
                payload = {"timeout": 20}
                if self.offset:
                    payload["offset"] = self.offset
                ca_bundle = get_ca_bundle()
                resp = requests.post(
                    self.api_url + "getUpdates",
                    json=payload,
                    timeout=25,
                    verify=ca_bundle if ca_bundle else True,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for result in data["result"]:
                            self.offset = result["update_id"] + 1
                            if "callback_query" in result:
                                self._handle_callback(result["callback_query"])
            except Exception as e:
                print(f"[TelegramListener] polling error: {e}")
                import time
                time.sleep(3)

    def _handle_callback(self, cb: dict):
        cb_id   = cb.get("id")
        data    = cb.get("data")
        msg     = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        msg_id  = msg.get("message_id")

        if not chat_id or not msg_id:
            return

        ca = get_ca_bundle()
        verify_flag = ca if ca else True

        if data == "save_news":
            new_keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ บันทึกข่าวเรียบร้อยแล้ว", "callback_data": "already_saved"}]
                ]
            }
            requests.post(self.api_url + "editMessageReplyMarkup", json={
                "chat_id": chat_id, "message_id": msg_id, "reply_markup": new_keyboard
            }, verify=verify_flag)
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "อัปเดตสถานะการบันทึกแล้ว!"
            }, verify=verify_flag)

        elif data == "delete_news":
            requests.post(self.api_url + "deleteMessage", json={
                "chat_id": chat_id, "message_id": msg_id
            }, verify=verify_flag)
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "ลบข่าวนี้ออกจากแชทแล้ว"
            }, verify=verify_flag)

        elif data == "already_saved":
            requests.post(self.api_url + "answerCallbackQuery", json={
                "callback_query_id": cb_id, "text": "ข่าวนี้ถูกบันทึกไปแล้วครับ!"
            }, verify=verify_flag)

    def stop(self):
        self._stop_event.set()
