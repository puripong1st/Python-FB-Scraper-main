"""
sheets_manager.py
━━━━━━━━━━━━━━━━━
GoogleSheetsManager — อัปโหลดข้อมูลข่าวลง Google Sheets
ต้องการ: gspread, oauth2client, และไฟล์ Service Account .json
"""

import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials


class GoogleSheetsManager:
    def __init__(self, creds_path: str, sheet_name: str, log_callback):
        self.log   = log_callback
        self.sheet = None

        if not creds_path or not sheet_name or not os.path.exists(creds_path):
            return

        try:
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds  = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
            client = gspread.authorize(creds)
            self.sheet = client.open(sheet_name).sheet1
            self.log("✅ เชื่อมต่อ Google Sheets สำเร็จ")
        except Exception as e:
            self.log(f"❌ Google Sheets Connection Error: {e}")

    def upload_news(self, page_name: str, post_url: str, post_text: str,
                    persons: list, score: int, reason: str) -> bool:
        if not self.sheet:
            return False
        try:
            row_data = [
                page_name,         # 1. ชื่อเพจ
                post_url,          # 2. ลิ้งข่าว
                post_text,         # 3. เนื้อหาข่าว
                ", ".join(persons),# 4. คนที่ถูกระบุ
                score,             # 5. ความเกี่ยวข้อง
                reason,            # 6. เหตุผลของ AI
            ]
            self.sheet.append_row(row_data)
            return True
        except Exception as e:
            self.log(f"❌ อัปโหลดลง Google Sheets ไม่สำเร็จ: {e}")
            return False
