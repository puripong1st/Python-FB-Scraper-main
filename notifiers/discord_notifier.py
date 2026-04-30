"""
notifiers/discord_notifier.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ส่งแจ้งเตือนผ่าน Discord Webhook — embed สีประจำเพจ + AI result
"""

import requests
import socket
from datetime import datetime, timedelta, timezone
from ssl_helper import get_ca_bundle


class DiscordNotifier:
    # สีประจำแต่ละเพจ — มองปั๊บรู้ทันทีว่ามาจากไหน
    PAGE_COLORS = {
        "khaosod":               0xE53935,
        "TheReportersTH":        0x1565C0,
        "NationOnline":          0x2E7D32,
        "MorningNewsTV3":        0x6A1B9A,
        "ThePoliticsByMatichon": 0xE65100,
        "MatichonOnline":        0xF57F17,
        "thestandardth":         0x00838F,
        "Ch7HDNews":             0xC62828,
        "thairath":              0xAD1457,
        "tnamcot":               0x00695C,
    }
    DEFAULT_COLOR = 0x1877F2  # Facebook Blue

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _send(self, payload: dict) -> bool:
        if not self.webhook_url:
            return False
        try:
            ca_bundle = get_ca_bundle()
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
                verify=ca_bundle if ca_bundle else True,
            )
            return resp.status_code in (200, 204)
        except requests.RequestException as e:
            print(f"⚠️ [Discord] ส่งไม่ได้: {e}")
            return False

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _smart_truncate(self, text: str, limit: int, post_url: str) -> str:
        """ตัดข้อความอย่างฉลาด — ไม่ตัดกลางคำ"""
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in ["\n\n", "\n", "। ", "。", ". "]:
            idx = cut.rfind(sep)
            if idx > limit * 0.65:
                cut = cut[:idx]
                break
        return cut + f"\n\n*[…อ่านต่อในโพสต์ต้นฉบับ]({post_url})*"

    def send_start(self, page_count: int = 0, keyword_count: int = 0, loop_min: int = 0, hours_back: int = 6):
        host = socket.gethostname()
        kw_label = f"`{keyword_count} คำ`" if keyword_count else "`ทั้งหมด (ไม่กรอง)`"
        embed = {
            "color": 0x43A047,
            "author": {"name": "🟢  ระบบ Scraper เริ่มทำงานแล้ว"},
            "description": f"🖥️  เครื่อง: `{host}`",
            "fields": [
                {"name": "📋  เพจที่ติดตาม",      "value": f"`{page_count} เพจ`",        "inline": True},
                {"name": "🔑  Keywords",            "value": kw_label,                     "inline": True},
                {"name": "🔄  วนซ้ำทุก",           "value": f"`{loop_min} นาที`",         "inline": True},
                {"name": "📅  ดึงย้อนหลัง",        "value": f"`{hours_back} ชั่วโมง`",    "inline": True},
                {"name": "⏰  เวลาเริ่มต้น",        "value": f"`{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}`", "inline": True},
                {"name": "🔔  ช่องทางแจ้งเตือน",   "value": "`Discord ✅`",               "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})

    def send_post(self, page_name: str, page_url: str, post_url: str, content: str,
                  found_keywords: list, image_url: str = None, ai_result: dict = None):
        color = self.PAGE_COLORS.get(page_name, self.DEFAULT_COLOR)
        now   = datetime.now()
        content_display = self._smart_truncate(content, 900, post_url)
        kw_chips = "  ".join(f"`{kw}`" for kw in found_keywords) if found_keywords else "*—*"

        fields = [
            {"name": "🔗 URL โพสต์", "value": f"[เปิดใน Facebook]({post_url})", "inline": False},
        ]

        if ai_result:
            score   = ai_result.get("score", 0)
            reason  = ai_result.get("reason", "-")
            persons = ", ".join(ai_result.get("persons", [])) if ai_result.get("persons") else "-"
            if score >= 8:   color = 0xFF0000
            elif score >= 5: color = 0xFFA500
            fields.extend([
                {"name": "🤖 AI Score",        "value": f"`{score}/10`",  "inline": True},
                {"name": "👤 บุคคลที่พบ",      "value": f"`{persons}`",   "inline": True},
                {"name": "💡 สรุปประเด็น (AI)", "value": reason,           "inline": False},
            ])

        fields.extend([
            {"name": "🔍 Keywords ที่ตรง", "value": kw_chips, "inline": False},
            {"name": "🕐 ตรวจพบเมื่อ",    "value": f"`{now.strftime('%d/%m/%Y %H:%M:%S')} น.`", "inline": True},
        ])

        embed = {
            "color": color,
            "author": {"name": f"📰 {page_name}", "url": page_url},
            "title": "📌 คลิกเพื่ออ่านโพสต์ต้นฉบับ",
            "url": post_url,
            "description": content_display,
            "fields": fields,
            "footer": {"text": f"FB News Monitor • PRP • {page_name}"},
            "timestamp": self._utc_now_iso(),
        }
        if image_url:
            embed["image"] = {"url": image_url}
        self._send({"embeds": [embed]})

    def send_cycle_complete(self, duration_sec: float, next_run_min: int, total_new: int = 0, pages_count: int = 0):
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        next_time   = (datetime.now() + timedelta(minutes=next_run_min)).strftime("%H:%M")
        posts_val   = f"`{total_new} โพสต์`" if total_new > 0 else "`ไม่มีโพสต์ใหม่`"
        posts_color = 0x43A047 if total_new > 0 else 0x1E88E5
        embed = {
            "color": posts_color,
            "author": {"name": "✅  สแกนรอบนี้เสร็จสิ้น"},
            "fields": [
                {"name": "🆕  โพสต์ใหม่ที่พบ",    "value": posts_val,                                "inline": True},
                {"name": "📋  เพจที่สแกน",          "value": f"`{pages_count} เพจ`",                 "inline": True},
                {"name": "⏱  เวลาที่ใช้",          "value": f"`{mins} นาที {secs} วินาที`",          "inline": True},
                {"name": "⏳  รอบถัดไปในอีก",       "value": f"`{next_run_min} นาที`",               "inline": True},
                {"name": "🕐  รอบถัดไปประมาณ",      "value": f"`{next_time} น.`",                    "inline": True},
                {"name": "📅  วันที่",               "value": f"`{datetime.now().strftime('%d/%m/%Y')}`", "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})

    def send_obstacle(self, obstacle_type: str, page_url: str = ""):
        page_info = f"\n**เพจที่ติดปัญหา:** {page_url}" if page_url else ""
        embed = {
            "color": 0xE53935,
            "author": {"name": "🚨  บอทหยุดทำงานชั่วคราว — ต้องการความช่วยเหลือ!"},
            "description": (
                f"**บอทติดหน้า:** `{obstacle_type}`{page_info}\n\n"
                "**วิธีแก้ไข:**\n"
                "1️⃣  เปิดหน้าต่าง Browser\n"
                "2️⃣  แก้ไขตามที่หน้าจอแจ้ง\n"
                "3️⃣  กดปุ่ม **Resume** บนโปรแกรม\n\n"
                "*บอทจะทำงานต่ออัตโนมัติหลังกด Resume*"
            ),
            "fields": [
                {"name": "⏰  เวลาที่เกิดปัญหา", "value": f"`{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} น.`", "inline": True},
                {"name": "🚫  ประเภทปัญหา",       "value": f"`{obstacle_type}`",                                    "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"content": "@everyone", "embeds": [embed]})

    def send_stopped(self, total_runtime_sec: float = 0, total_posts_found: int = 0):
        if total_runtime_sec > 0:
            h = int(total_runtime_sec // 3600)
            m = int((total_runtime_sec % 3600) // 60)
            s = int(total_runtime_sec % 60)
            runtime_str = f"`{h}ชม. {m}นาที {s}วิ`" if h > 0 else f"`{m}นาที {s}วิ`"
        else:
            runtime_str = "`-`"
        embed = {
            "color": 0x757575,
            "author": {"name": "🔴  ระบบ Scraper หยุดทำงานแล้ว"},
            "description": "*หยุดโดยผู้ใช้งาน — กด Start เพื่อเริ่มใหม่*",
            "fields": [
                {"name": "⏱  รันไปทั้งหมด",        "value": runtime_str,                    "inline": True},
                {"name": "📰  โพสต์ที่พบทั้งหมด",   "value": f"`{total_posts_found} โพสต์`", "inline": True},
                {"name": "🕐  หยุดเมื่อ",            "value": f"`{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} น.`", "inline": True},
            ],
            "footer": {"text": "FB News Monitor  •  PRP"},
            "timestamp": self._utc_now_iso(),
        }
        self._send({"embeds": [embed]})
