"""
database.py
━━━━━━━━━━━
จัดการ SQLite — บันทึกโพสต์ที่เคยเห็นแล้ว ป้องกัน Duplicate
"""

import sqlite3
import threading
from datetime import datetime


class DatabaseManager:
    DB_FILE = "scraper_data.db"

    def __init__(self):
        self.conn = sqlite3.connect(self.DB_FILE, check_same_thread=False)
        self._lock = threading.RLock()   # RLock รองรับ reentrant calls
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    post_id    TEXT PRIMARY KEY,
                    page_url   TEXT,
                    post_url   TEXT UNIQUE,
                    detected_at TEXT
                )
            """)
            # Migrate: เพิ่ม UNIQUE index บน post_url สำหรับ DB เดิม
            try:
                self.conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_post_url_unique
                    ON seen_posts(post_url)
                """)
            except sqlite3.Error:
                pass  # index อาจมีอยู่แล้ว — ปลอดภัยที่จะ skip
            # ล้างข้อมูลซ้ำที่มีอยู่เดิม
            try:
                self.conn.execute("""
                    DELETE FROM seen_posts
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid) FROM seen_posts GROUP BY post_url
                    )
                """)
            except sqlite3.Error:
                pass  # ตารางอาจว่างเปล่า — ปลอดภัยที่จะ skip
            self.conn.commit()

    def is_seen(self, post_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
            )
            return cur.fetchone() is not None

    def is_seen_by_url(self, post_url: str) -> bool:
        """ตรวจ duplicate ด้วย URL โดยตรง — ครอบคลุมกรณี post_id ต่างกันแต่ URL เหมือนกัน"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM seen_posts WHERE post_url = ?", (post_url,)
            )
            return cur.fetchone() is not None

    def mark_seen(self, post_id: str, page_url: str, post_url: str):
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO seen_posts (post_id, page_url, post_url, detected_at) "
                    "VALUES (?, ?, ?, ?)",
                    (post_id, page_url, post_url, datetime.now().isoformat()),
                )
                self.conn.commit()
            except sqlite3.Error as e:
                print(f"[DB] mark_seen error: {e}")

    def cleanup_old_data(self):
        """ลบโพสต์ที่เก่ากว่า 24 ชั่วโมง และทำ VACUUM เพื่อคืนพื้นที่บนดิสก์"""
        with self._lock:
            try:
                self.conn.execute("DELETE FROM seen_posts WHERE detected_at < datetime('now', '-1 day')")
                self.conn.commit()
                self.conn.execute("VACUUM")
                self.conn.commit()
                return True
            except sqlite3.Error as e:
                print(f"[DB] Cleanup error: {e}")
                return False

    def close(self):
        self.conn.close()
