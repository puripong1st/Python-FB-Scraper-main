"""
scraper.py — FacebookScraper  (Final Clean Version)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ฟีเจอร์ครบ / ไร้บัค:
  ✅ _is_logged_in()  — ตรวจ DOM จริง ไม่ใช่แค่ URL
     (แก้บัค: cookie หมดอายุแล้วไม่แจ้งเตือน เพราะ FB redirect กลับ
      facebook.com/ ซึ่ง URL ไม่มีคำว่า "login")
  ✅ cookie expired   — แจ้งเตือน Discord + Telegram + หยุดรอ Resume
  ✅ FB อินเดีย       — data-pagelet="FeedUnit_N" กรอง comment ออก 100%
  ✅ URL format ใหม่  — pfbid, story.php, photo.php, web.facebook.com
  ✅ Timestamp        — รองรับทั้งไทย + English
  ✅ ปุ่มซ่อน Browser — sync กับ state จริงผ่าน callback (thread-safe)
  ✅ Obstacle         — Checkpoint / 2FA / CAPTCHA / Identity Verify
"""

import threading
import time
import json
import re
import os
import random
import hashlib
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    InvalidSessionIdException,
)
import undetected_chromedriver as uc

from database import DatabaseManager
from notifiers import DiscordNotifier, TelegramNotifier


COOKIES_FILE = "fb_cookies.json"


class FacebookScraper:

    HOME_URL = "https://www.facebook.com"

    SELECTORS = {
        "email_input": "//input[@id='email' or @name='email']",
        "pass_input":  "//input[@id='pass'  or @name='pass']",
    }

    def __init__(
        self,
        log_callback,
        db: DatabaseManager,
        discord: DiscordNotifier,
        tg: TelegramNotifier,
        ai_analyzer=None,
        sheets_manager=None,
        on_cookies_saved=None,
        on_browser_hidden=None,
        on_browser_shown=None,
    ):
        self.log            = log_callback
        self.db             = db
        self.discord        = discord
        self.tg             = tg
        self.ai_analyzer    = ai_analyzer
        self.sheets_manager = sheets_manager
        self._on_cookies_saved  = on_cookies_saved
        self._on_browser_hidden = on_browser_hidden
        self._on_browser_shown  = on_browser_shown

        self._driver       = None
        self._driver_lock  = threading.RLock()
        self._stop_event   = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._is_paused    = False

        self._browser_hidden        = False
        self._consecutive_failures  = 0
        self._cycle_count           = 0
        self._scraper_chrome_pids: set = set()

    # ── driver property ───────────────────────────────────────────────────────

    @property
    def driver(self):
        with self._driver_lock:
            return self._driver

    @driver.setter
    def driver(self, value):
        with self._driver_lock:
            self._driver = value

    # ── Chrome PID tracking ───────────────────────────────────────────────────

    def _collect_chrome_pids(self) -> set:
        try:
            import subprocess, json as _j

            def _all_pids():
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'}"
                     " | Select-Object ProcessId | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    return set()
                p = _j.loads(r.stdout)
                if isinstance(p, dict): p = [p]
                return {int(x["ProcessId"]) for x in p if x.get("ProcessId")}

            before: set = getattr(self, "_chrome_pids_before", set())
            new_pids = _all_pids() - before
            if new_pids:
                self._scraper_chrome_pids = new_pids
                return new_pids

            drv = self.driver
            if drv:
                root = drv.service.process.pid
                r2 = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'}"
                     " | Select-Object ProcessId,ParentProcessId | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10)
                if r2.stdout.strip():
                    procs = _j.loads(r2.stdout)
                    if isinstance(procs, dict): procs = [procs]
                    pids = {root}; q = [root]
                    while q:
                        p = q.pop()
                        for x in procs:
                            pp = int(x.get("ParentProcessId") or 0)
                            cp = int(x.get("ProcessId") or 0)
                            if pp == p and cp not in pids:
                                pids.add(cp); q.append(cp)
                    self._scraper_chrome_pids = pids
                    return pids
        except Exception as e:
            self.log(f"⚠️ _collect_chrome_pids: {e}")
        try:
            pids = {self.driver.service.process.pid}
        except Exception:
            pids = set()
        self._scraper_chrome_pids = pids
        return pids

    def _find_browser_hwnds(self, _debug: bool = False) -> list:
        try:
            import ctypes, ctypes.wintypes
            pids = self._scraper_chrome_pids

            found = []
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

            def _cb(hwnd, _):
                # 1. เช็ค Class Name ของ Chrome
                cls = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(hwnd, cls, 64)
                if cls.value not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0"):
                    return True
                
                # 2. เช็ค PID ว่าตรงกับบอทของเราไหม
                win_pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if pids and win_pid.value not in pids:
                    return True
                
                # 🔴 ส่วนที่เพิ่มใหม่: กรองหน้าต่างระบบของ Chrome ทิ้ง
                # เช็คความยาวของชื่อหน้าต่าง (Title) หน้าต่างหลักของเว็บจะต้องมีชื่อเสมอ
                title_length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if title_length == 0:
                    return True # ถ้าไม่มีชื่อ (หน้าต่างดำ/ระบบ) ให้ข้ามไป ไม่ต้องเก็บเข้า list

                found.append(hwnd)
                return True

            ctypes.windll.user32.EnumWindows(EnumProc(_cb), 0)
            return found
        except Exception as e:
            self.log(f"⚠️ _find_browser_hwnds: {e}")
            return []

    # ── Hide / Show ───────────────────────────────────────────────────────────

    def hide_browser(self):
        try:
            import ctypes
            SW_HIDE = 0
            hwnds = self._find_browser_hwnds()
            hidden = 0
            for hwnd in hwnds:
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
                    hidden += 1
            if hidden:
                self.log(f"👻 ซ่อน Browser แล้ว ({hidden} หน้าต่าง)")
            self._browser_hidden = True
        except Exception as e:
            self.log(f"⚠️ hide_browser: {e}")
            
        # แจ้ง UI ให้ sync ปุ่มเสมอ (แม้ ctypes จะล้มเหลว ก็ยังต้อง sync state)
        # 🛠️ เช็คความปลอดภัยก่อนเรียกใช้ฟังก์ชัน
        if hasattr(self, '_on_browser_hidden') and callable(self._on_browser_hidden):
            try:
                self._on_browser_hidden()
            except Exception as e:
                # 🔴 เปลี่ยนจาก pass เป็นพิมพ์ Error ออกมาดู
                self.log(f"⚠️ โค้ดพังที่ _on_browser_hidden: {e}")
                print(f"Error details: {e}")

    def show_browser(self):
        try:
            import ctypes
            SW_SHOW = 5     # 5 = แสดงหน้าต่างที่ถูกซ่อน (SW_HIDE)
            SW_RESTORE = 9  # 9 = คืนค่าจากที่ย่อไว้ (Minimized)
            
            hwnds = self._find_browser_hwnds()
            shown = 0
            for hwnd in hwnds:
                # แนะนำให้ใช้ SW_SHOW ด้วย เผื่อหน้าต่างถูกซ่อนแบบสมบูรณ์ (SW_HIDE) 
                # ไม่ได้แค่ย่อลง Taskbar
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW) 
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                shown += 1
                
            if shown:
                self.log(f"👁️ แสดง Browser แล้ว ({shown} หน้าต่าง)")
            self._browser_hidden = False
        except Exception as e:
            self.log(f"⚠️ show_browser: {e}")
            
        # แจ้ง UI ให้ sync ปุ่มเสมอ
        # เช็คให้ชัวร์ว่ามีตัวแปรนี้อยู่จริง และสามารถเรียกใช้งาน (callable) ได้
        if hasattr(self, '_on_browser_shown') and callable(self._on_browser_shown):
            try:
                self._on_browser_shown()
            except Exception as e:
                # 🔴 เปลี่ยนจาก pass เป็นการ log จะได้รู้ว่าทำไม UI ไม่ยอมอัปเดต
                self.log(f"⚠️ โค้ดพังที่ _on_browser_shown: {e}")
                print(f"Error details: {e}")

    def _set_chrome_icon(self):
        try:
            import ctypes
            base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
            icon = os.path.join(base, "app_icon.ico")
            if not os.path.exists(icon):
                return
            IMAGE_ICON = 1; LR_LFF = 0x0010; LR_DS = 0x0040
            WM_SETICON = 0x0080; GCL_HICON = -14; GCL_SM = -34
            hb = ctypes.windll.user32.LoadImageW(None, icon, IMAGE_ICON, 32, 32, LR_LFF)
            hs = ctypes.windll.user32.LoadImageW(None, icon, IMAGE_ICON, 16, 16, LR_LFF)
            ht = ctypes.windll.user32.LoadImageW(None, icon, IMAGE_ICON, 0, 0, LR_LFF | LR_DS)
            hwnds = []
            for _ in range(30):
                hwnds = self._find_browser_hwnds()
                if hwnds: break
                time.sleep(0.5)
            for h in hwnds:
                if hb: ctypes.windll.user32.SendMessageW(h, WM_SETICON, 1, hb)
                if hs: ctypes.windll.user32.SendMessageW(h, WM_SETICON, 0, hs)
                if ht:
                    ctypes.windll.user32.SetClassLongPtrW(h, GCL_HICON, ht)
                    ctypes.windll.user32.SetClassLongPtrW(h, GCL_SM,    ht)
            if hwnds:
                self.log(f"🎨 เปลี่ยนไอคอน ({len(hwnds)} หน้าต่าง)")
        except Exception as e:
            self.log(f"⚠️ _set_chrome_icon: {e}")

    def _safe_quit_driver(self):
        drv = self.driver
        if not drv: return
        try: drv.quit()
        except Exception: pass
        finally:
            self.driver = None
            self._browser_hidden = False
            self._scraper_chrome_pids = set()

    def _sleep_interruptible(self, seconds: float, step: float = 5.0):
        elapsed = 0.0
        while elapsed < seconds and not self._stop_event.is_set():
            time.sleep(min(step, seconds - elapsed))
            elapsed += step

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    def _start_browser(self):
        import winreg, shutil, subprocess

        try:
            os.system("taskkill /f /im chromedriver.exe /t >nul 2>&1")
            appdata = os.getenv("APPDATA")
            if appdata:
                d = os.path.join(appdata, "undetected_chromedriver")
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                    self.log("🧹 ลบแคช Driver เก่า")
        except Exception as e:
            self.log(f"⚠️ ล้างแคช: {e}")

        def _make_opts():
            o = uc.ChromeOptions()
            o.add_argument("--no-sandbox")
            o.add_argument("--disable-dev-shm-usage")
            o.add_argument("--disable-blink-features=AutomationControlled")
            o.add_argument("--lang=th-TH,th;q=0.9,en-US;q=0.8")
            o.add_argument("--window-size=1280,900")
            o.page_load_strategy = "eager"
            return o

        # ตรวจเวอร์ชัน Chrome จาก EXE
        chrome_version = None
        for cmd in [
            "(Get-Item (Get-Command chrome).Source).VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r'(Get-Item "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe").VersionInfo.ProductVersion',
        ]:
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                                   capture_output=True, text=True, timeout=8)
                v = r.stdout.strip()
                if v and v[0].isdigit():
                    chrome_version = int(v.split(".")[0])
                    self.log(f"🔎 Chrome version: {chrome_version}")
                    break
            except Exception: continue

        if not chrome_version:
            for hive, path in [
                (winreg.HKEY_CURRENT_USER,  r"Software\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
            ]:
                try:
                    key = winreg.OpenKey(hive, path)
                    v, _ = winreg.QueryValueEx(key, "version")
                    if v:
                        chrome_version = int(v.split(".")[0])
                        self.log(f"⚠️ Chrome version (Registry): {chrome_version}")
                        break
                except Exception: continue

        strategies = ([{"version_main": chrome_version}] if chrome_version else []) + [{}, {"version_main": None}]
        last_err = None
        for attempt, kwargs in enumerate(strategies, 1):
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            try:
                self.log(f"🔄 เปิด Browser รอบ {attempt}/{len(strategies)}")
                self._safe_quit_driver()
                time.sleep(1)

                # snapshot PIDs ก่อน launch เพื่อ diff หา PIDs ของเรา
                try:
                    import json as _j
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'}"
                         " | Select-Object ProcessId | ConvertTo-Json"],
                        capture_output=True, text=True, timeout=10)
                    if r.stdout.strip():
                        pp = _j.loads(r.stdout)
                        if isinstance(pp, dict): pp = [pp]
                        self._chrome_pids_before = {int(x["ProcessId"]) for x in pp if x.get("ProcessId")}
                    else:
                        self._chrome_pids_before = set()
                except Exception:
                    self._chrome_pids_before = set()

                self.driver = uc.Chrome(options=_make_opts(), use_subprocess=True, **kwargs)
                self.driver.set_page_load_timeout(60)
                self.log("🌐 Browser เปิดสำเร็จ")
                time.sleep(0.5)
                self._collect_chrome_pids()
                try: self.driver.get(self.HOME_URL)
                except Exception: pass
                threading.Thread(target=self._set_chrome_icon, daemon=True).start()
                return
            except Exception as e:
                last_err = e
                self.log(f"⚠️ รอบ {attempt} ล้มเหลว: {e}")
                self._safe_quit_driver()
                if attempt < len(strategies): time.sleep(3)

        raise RuntimeError(f"❌ เปิด Browser ไม่สำเร็จ: {last_err}")

    # ── Login detection ───────────────────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        """
        ตรวจว่า login สำเร็จหรือไม่ โดยตรวจ DOM จริง ไม่ใช่แค่ URL

        สาเหตุที่ต้องใช้วิธีนี้:
          Facebook ที่ cookie หมดอายุมักจะ redirect กลับ facebook.com/
          URL ไม่มีคำว่า "login" → การตรวจแค่ URL บอกผิดพลาดว่า login สำเร็จ
          → ทำให้ _handle_cookie_expired() ไม่ถูกเรียก → ไม่มีแจ้งเตือน

        Logic:
          1. URL มี /login / /checkpoint → False ทันที (ชัวร์ว่าไม่ได้ login)
          2. DOM มี login form → False (ชัวร์)
          3. DOM มี element ที่มีเฉพาะตอน login แล้ว → True (ชัวร์)
          4. ไม่แน่ใจ → False (conservative — ดีกว่า assume True ผิด)
        """
        try:
            # รอให้หน้าโหลดสมบูรณ์ก่อน (สูงสุด 5 วิ)
            for _ in range(10):
                try:
                    if self.driver.execute_script("return document.readyState;") == "complete":
                        break
                except Exception:
                    break
                time.sleep(0.5)

            url = self.driver.current_url.lower()

            # 1. URL บ่งชี้ว่าไม่ได้ login → False
            if any(x in url for x in ("login", "checkpoint", "two_step", "captcha", "disabled", "suspended")):
                return False

            result = self.driver.execute_script("""
                // ── Negative: มี login form = ยังไม่ login ─────────────────
                const NOT_LOGGED = [
                    'form#login_form',
                    'input#email[type="text"]',
                    'input[name="email"][autocomplete="username"]',
                    '[data-testid="royal_login_button"]',
                    'button[name="login"]',
                    '[data-pagelet="login_form"]',
                ];
                if (NOT_LOGGED.some(s => document.querySelector(s))) return false;

                // ── Positive: element ที่มีเฉพาะตอน login แล้ว ────────────
                const LOGGED_IN = [
                    'div[role="feed"]',                     // feed หลัก
                    '[data-pagelet="FeedUnit_0"]',          // โพสต์แรกใน feed
                    '[data-pagelet="ProfileTimeline"]',     // หน้า profile
                    '[data-pagelet="PageTimeline"]',        // หน้า page
                    '[aria-label="บัญชีของคุณ"]',           // menu ไทย
                    '[aria-label="Your account"]',          // menu EN
                    '[aria-label="Account"]',
                    '[aria-label="สร้างโพสต์"]',            // composer
                    '[aria-label="Create post"]',
                    '[data-pagelet="LeftRail"]',            // left sidebar
                    'div[data-pagelet="Stories"]',          // Stories bar
                ];
                if (LOGGED_IN.some(s => document.querySelector(s))) return true;

                // ── ไม่แน่ใจ → null → Python จะ treat เป็น False ─────────
                return null;
            """)

            if result is True:
                return True
            elif result is False:
                return False
            else:
                # null = ตรวจไม่ชัดเจน → conservative = False
                # (ดีกว่า assume True แล้ว cookie expired ไม่แจ้งเตือน)
                self.log("⚠️ _is_logged_in: ตรวจไม่ชัด → ถือว่ายังไม่ได้ login")
                return False

        except Exception as e:
            self.log(f"⚠️ _is_logged_in error: {e}")
            return False

    # ── Cookies ───────────────────────────────────────────────────────────────

    def _save_cookies(self):
        drv = self.driver
        if not drv: return
        try:
            cookies = drv.get_cookies()
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self.log("🍪 บันทึก Cookies สำเร็จ")
        except Exception as e:
            self.log(f"⚠️ บันทึก Cookies ไม่สำเร็จ: {e}")
            return
        if self._on_cookies_saved:
            try: self._on_cookies_saved()
            except Exception as e: self.log(f"⚠️ on_cookies_saved: {e}")

    def _load_cookies(self) -> bool:
        """
        โหลด Cookie และตรวจ session ด้วย _is_logged_in()
        ไม่ใช่แค่ตรวจ URL — แก้บัคหลักที่ทำให้ไม่แจ้งเตือน cookie หมดอายุ
        """
        if not os.path.exists(COOKIES_FILE):
            return False
        try:
            self.driver.get(self.HOME_URL)
            time.sleep(2)
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            for c in cookies:
                try: self.driver.add_cookie(c)
                except Exception: pass
            self.driver.refresh()
            time.sleep(3)

            if self._is_logged_in():
                self.log("✅ กู้คืน Session เดิมสำเร็จ")
                if self._on_cookies_saved:
                    try: self._on_cookies_saved()
                    except Exception: pass
                return True

            self.log("⚠️ Cookie ไม่ valid — session หมดอายุหรือถูก revoke")
            return False

        except Exception as e:
            self.log(f"⚠️ โหลด Cookies ไม่สำเร็จ: {e}")
            return False

    # ── Login ─────────────────────────────────────────────────────────────────

    def _type_human(self, el, text: str, delay: float = 0.06):
        el.clear(); time.sleep(0.3)
        for ch in text:
            el.send_keys(ch)
            time.sleep(random.uniform(delay * 0.7, delay * 1.5))

    def _click_login_btn(self) -> bool:
        for by, sel in [
            (By.CSS_SELECTOR, "button[name='login']"),
            (By.CSS_SELECTOR, "[data-testid='royal_login_button']"),
            (By.CSS_SELECTOR, "form button[type='submit']"),
            (By.XPATH, "//button[contains(.,'เข้าสู่ระบบ')]"),
            (By.XPATH, "//button[contains(.,'Log in') or contains(.,'Log In')]"),
            (By.XPATH, "//*[@id='loginform']//button"),
            (By.XPATH, "//div[@role='button' and (contains(.,'Log') or contains(.,'เข้า'))]"),
        ]:
            try:
                btn = WebDriverWait(self.driver, 4).until(EC.element_to_be_clickable((by, sel)))
                self.driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.3); btn.click()
                self.log("🖱️ คลิก Login สำเร็จ")
                return True
            except Exception: continue
        try:
            self.driver.find_element(By.XPATH, self.SELECTORS["pass_input"]).send_keys(Keys.RETURN)
            self.log("⌨️ กด Enter (fallback)")
            return True
        except Exception: pass
        return False

    def login(self, email: str, password: str) -> bool:
        try:
            self.driver.get(f"{self.HOME_URL}/login")
            wait = WebDriverWait(self.driver, 20)

            self.log("📧 กรอก Email...")
            ef = wait.until(EC.element_to_be_clickable((By.XPATH, self.SELECTORS["email_input"])))
            self._type_human(ef, email); time.sleep(0.4)

            self.log("🔑 กรอก Password...")
            pf = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["pass_input"])))
            self._type_human(pf, password); time.sleep(0.6)

            if not self._click_login_btn():
                self._handle_obstacle("Login Button Not Found", f"{self.HOME_URL}/login")
                if self._stop_event.is_set(): return False

            self.log("⏳ รอโหลดหลัง Login...")
            time.sleep(6)

            ob = self._detect_obstacle()
            if ob:
                self._handle_obstacle(ob, f"{self.HOME_URL}/login")
                if self._stop_event.is_set(): return False
                time.sleep(2)

            if self._is_logged_in():
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ")
                return True

            self.log("⚠️ ล็อกอินไม่สำเร็จ — กรุณาล็อกอินใน Browser แล้วกด Resume")
            self._handle_obstacle("Login ไม่สำเร็จ — กรุณาล็อกอินด้วยตัวเอง", f"{self.HOME_URL}/login")
            if self._stop_event.is_set(): return False
            time.sleep(2)
            if self._is_logged_in():
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ (manual)")
                return True
            return False

        except TimeoutException:
            self.log("⚠️ Timeout — กรุณาล็อกอินใน Browser แล้วกด Resume")
            self._handle_obstacle("Timeout Login", f"{self.HOME_URL}/login")
            if self._stop_event.is_set(): return False
            time.sleep(2)
            return self._is_logged_in()
        except Exception as e:
            self.log(f"❌ Login Error: {e}")
            return False

    # ── Obstacle ──────────────────────────────────────────────────────────────

    def _detect_obstacle(self) -> str | None:
        try:
            url   = self.driver.current_url.lower()
            title = self.driver.title.lower()
            if "checkpoint"            in url or "checkpoint"  in title: return "Checkpoint"
            if "two_step_verification" in url or "two_factor"  in url:   return "2FA"
            if "captcha"               in url or "captcha"     in title: return "CAPTCHA"
            if "login_attempt"         in url:                           return "Login Attempt Blocked"
            if "suspended"             in url or "disabled"    in url:   return "Account Suspended/Disabled"

            _ID_URL  = ("identity", "identity_verification", "id_verification",
                        "confirm_identity", "verify_identity")
            _ID_TEXT = ("confirm your identity", "ยืนยันตัวตน")
            if any(s in url for s in _ID_URL):
                return "Identity Verification"

            found = self.driver.execute_script("""
                const signals = arguments[0];
                const inHead = Array.from(document.querySelectorAll('h1,h2,h3')).some(h => {
                    const t = (h.innerText||'').toLowerCase();
                    const s = window.getComputedStyle(h);
                    return s.display!=='none' && s.visibility!=='hidden' && signals.some(x=>t.includes(x));
                });
                const inForm = Array.from(document.querySelectorAll('form')).some(f => {
                    const a = (f.action||'').toLowerCase();
                    return a.includes('identity')||a.includes('checkpoint')||a.includes('confirm');
                });
                return inHead || inForm;
            """, list(_ID_TEXT))

            if found:
                self.log("⏳ พบสัญญาณ Identity Verification — ตรวจซ้ำ 2 วิ...")
                time.sleep(2)
                if self.driver.current_url.lower() != url:
                    return None
                confirmed = self.driver.execute_script("""
                    const signals = arguments[0];
                    return Array.from(document.querySelectorAll('h1,h2,h3')).some(h => {
                        const t = (h.innerText||'').toLowerCase();
                        const s = window.getComputedStyle(h);
                        return s.display!=='none' && s.visibility!=='hidden' && signals.some(x=>t.includes(x));
                    }) || Array.from(document.querySelectorAll('form')).some(f => {
                        const a = (f.action||'').toLowerCase();
                        return a.includes('identity')||a.includes('checkpoint')||a.includes('confirm');
                    });
                """, list(_ID_TEXT))
                return "Identity Verification" if confirmed else None
        except Exception as e:
            self.log(f"⚠️ _detect_obstacle: {e}")
        return None

    def _handle_obstacle(self, obstacle_type: str, page_url: str = ""):
        self.log(f"🚨 ติด {obstacle_type} — หยุดรอ Resume")
        self.show_browser()
        self.discord.send_obstacle(obstacle_type, page_url)
        self.tg.send_obstacle(obstacle_type, page_url)
        self._resume_event.clear(); self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume — กลับมาทำงานต่อ")
        self.hide_browser()

    def _handle_cookie_expired(self, page_url: str = ""):
        """
        แจ้งเตือนและหยุดรอเมื่อ Cookie หมดอายุ
        แตกต่างจาก _handle_obstacle() ตรงที่ข้อความและ icon แจ้งเตือน
        """
        self.log("🍪 Cookie/Session หมดอายุ — กรุณาล็อกอินใน Browser แล้วกด Resume")
        self.show_browser()
        # navigate ไปหน้า login เพื่อให้ผู้ใช้เห็นชัดว่าต้องทำอะไร
        try: self.driver.get(f"{self.HOME_URL}/login")
        except Exception: pass
        self.discord.send_cookie_expired(page_url)
        self.tg.send_cookie_expired(page_url)
        self._resume_event.clear(); self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume — ตรวจสอบ Session...")
        self.hide_browser()

    def resume(self):
        self._resume_event.set()

    # ── Scroll ────────────────────────────────────────────────────────────────

    def _slow_scroll(self, scrolls: int = 4, pause: float = 2.0):
        for _ in range(scrolls):
            if self._stop_event.is_set(): break
            self.driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            time.sleep(random.uniform(pause * 0.8, pause * 1.3))

    # ── Post ID ───────────────────────────────────────────────────────────────

    def _extract_post_id(self, url: str) -> str:
        for pat in [
            r"/posts/(pfbid[A-Za-z0-9]+)",   r"/posts/(\d+)",
            r"story_fbid=(pfbid[A-Za-z0-9]+)", r"story_fbid=(\d+)",
            r"/permalink/(pfbid[A-Za-z0-9]+)", r"/permalink/(\d+)",
            r"fbid=(pfbid[A-Za-z0-9]+)",       r"fbid=(\d+)",
            r"/videos/(\d+)",   r"/reel/(\d+)",  r"[?&]v=(\d+)",
            r"/share/p/(pfbid[A-Za-z0-9]+)", r"/share/p/([^/?]+)",
            r"/share/(pfbid[A-Za-z0-9]+)",   r"/share/([^/?]+)",
            r"[?&]id=(\d+)",
        ]:
            m = re.search(pat, url)
            if m: return m.group(1)
        return hashlib.md5(url.encode()).hexdigest()

    # ── Timestamp ─────────────────────────────────────────────────────────────

    _TH_SHORT = {
        "ม.ค.":1,"ก.พ.":2,"มี.ค.":3,"เม.ย.":4,
        "พ.ค.":5,"มิ.ย.":6,"ก.ค.":7,"ส.ค.":8,
        "ก.ย.":9,"ต.ค.":10,"พ.ย.":11,"ธ.ค.":12,
    }
    _TH_LONG = {
        "มกราคม":1,"กุมภาพันธ์":2,"มีนาคม":3,"เมษายน":4,
        "พฤษภาคม":5,"มิถุนายน":6,"กรกฎาคม":7,"สิงหาคม":8,
        "กันยายน":9,"ตุลาคม":10,"พฤศจิกายน":11,"ธันวาคม":12,
    }
    _EN_MONTH = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
        "sep":9,"oct":10,"nov":11,"dec":12,
    }

    def _parse_date(self, text: str) -> "datetime | None":
        now = datetime.now()
        for abbr, m in self._TH_SHORT.items():
            if abbr in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    d, y = int(nums[0]), int(nums[-1])
                    if y > 2400: y -= 543
                    elif y < 100: y += 1957
                    try: return datetime(y, m, d)
                    except ValueError: pass
        for full, m in self._TH_LONG.items():
            if full in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    d, y = int(nums[0]), int(nums[-1])
                    if y > 2400: y -= 543
                    elif y < 100: y += 1957
                    try: return datetime(y, m, d)
                    except ValueError: pass
        tl = text.lower()
        for en, m in self._EN_MONTH.items():
            if en in tl:
                nums = [int(n) for n in re.findall(r"\d+", text) if int(n) != m]
                if len(nums) >= 2:
                    try:
                        d = next(n for n in sorted(nums) if 1 <= n <= 31)
                        y = next(n for n in sorted(nums) if n > 31)
                        return datetime(y, m, d)
                    except (StopIteration, ValueError): pass
                elif len(nums) == 1 and 1 <= nums[0] <= 31:
                    try: return datetime(now.year, m, nums[0])
                    except ValueError: pass
        return None

    def _parse_timestamp(self, raw: str, utime: int = 0, label: str = "") -> "datetime | None":
        now = datetime.now()
        if utime and utime > 0:
            try: return datetime.fromtimestamp(utime)
            except Exception: pass
        if label:
            r = self._parse_date(label)
            if r: return r
            mt = re.search(r"(\w+day,\s+)?(\w+)\s+(\d{1,2}),?\s+(\d{4})", label, re.I)
            if mt:
                mn = self._EN_MONTH.get(mt.group(2).lower()[:3])
                if mn:
                    try: return datetime(int(mt.group(4)), mn, int(mt.group(3)))
                    except ValueError: pass
        for line in raw.split("\n")[:10]:
            t = line.strip().replace("·","").replace(",","").strip()
            if not t: continue
            tl = t.lower()
            if any(p in tl for p in ("just now","เพิ่ง","เมื่อสักครู่","a few seconds")):
                return now
            if "เมื่อวาน" in tl or "yesterday" in tl:
                return now - timedelta(days=1)
            m = re.search(r"(\d+)\s*(นาที|ชั่วโมง|ชม\.?|วัน|สัปดาห์|เดือน|ปี)", tl)
            if m:
                n, u = int(m.group(1)), m.group(2)
                if "นาที"    in u: return now - timedelta(minutes=n)
                if "ชม"      in u or "ชั่วโมง" in u: return now - timedelta(hours=n)
                if "วัน"     in u: return now - timedelta(days=n)
                if "สัปดาห์" in u: return now - timedelta(weeks=n)
                if "เดือน"   in u: return now - timedelta(days=n*30)
                if "ปี"      in u: return now - timedelta(days=n*365)
            m = re.search(r"(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|[hmdswy])\b", tl)
            if m:
                n, u = int(m.group(1)), m.group(2).lower().rstrip("s")
                if u in ("minute","min","m"):  return now - timedelta(minutes=n)
                if u in ("hour","hr","h"):     return now - timedelta(hours=n)
                if u in ("day","d"):           return now - timedelta(days=n)
                if u in ("week","w"):          return now - timedelta(weeks=n)
                if u in ("month",):            return now - timedelta(days=n*30)
                if u in ("year","y"):          return now - timedelta(days=n*365)
            r = self._parse_date(t)
            if r: return r
        return None

    # ── Scrape page ───────────────────────────────────────────────────────────

    def scrape_page(self, page_url: str, keywords: list, hours_back: int) -> int:
        new_posts       = 0
        page_name       = page_url.rstrip("/").split("/")[-1]
        cutoff          = datetime.now() - timedelta(hours=hours_back)
        MAX_OLD         = 5
        consecutive_old = 0
        seen_run: set   = set()
        stop_early      = False
        scroll_rounds   = 0

        try:
            self.log(f"🔍 เข้าเพจ: {page_url}")
            self.driver.get(page_url)
            time.sleep(3)

            ob = self._detect_obstacle()
            if ob:
                self._handle_obstacle(ob, page_url)
                if self._stop_event.is_set(): return 0

            last_count    = 0
            no_growth     = 0
            rounds_no_url = 0
            MAX_SCROLLS   = 30
            MAX_NO_GROWTH = 4
            MAX_NO_URL    = 3

            while not self._stop_event.is_set() and not stop_early and scroll_rounds < MAX_SCROLLS:
                self._slow_scroll(scrolls=4, pause=2.0)
                scroll_rounds += 1

                try:
                    cur_count = len(self.driver.find_elements(
                        By.XPATH, "//div[@role='article' and not(ancestor::div[@role='article'])]"))
                except Exception:
                    cur_count = 0

                if cur_count == 0:
                    self.log(f"⚠️ ไม่พบ article บน {page_name}")
                    break
                if cur_count > last_count:
                    no_growth = 0; last_count = cur_count
                    self.log(f"📜 [{page_name}] scroll {scroll_rounds} | articles: {cur_count}")
                else:
                    no_growth += 1
                    if no_growth >= MAX_NO_GROWTH:
                        self.log(f"📄 [{page_name}] หน้าไม่โหลดเพิ่ม — จบ")
                        break

                # คลิก "ดูเพิ่มเติม" / "See More"
                try:
                    self.driver.execute_script("""
                        const SM=['ดูเพิ่มเติม','see more','See More','See more',
                                  'Read more','read more','Show more','show more'];
                        document.querySelectorAll("div[role='article']").forEach(a=>{
                            if(a.parentElement&&a.parentElement.closest("div[role='article']"))return;
                            a.querySelectorAll(
                                'div[role="button"],span[role="button"],' +
                                'div[class*="see_more"],div[class*="truncate"]'
                            ).forEach(b=>{
                                const t=(b.innerText||b.textContent||'').trim();
                                if(SM.some(s=>t===s||t.startsWith(s))){try{b.click()}catch(e){}}
                            });
                        });
                    """)
                    time.sleep(0.6)
                except Exception: pass

                # ════════════════════════════════════════════════════════════
                # ดึงข้อมูลโพสต์ — 3 วิธีเรียงจากแม่นสุด
                #   A) data-pagelet="FeedUnit_N"  — แม่นสุด comment ผ่านไม่ได้
                #   B) div[role='feed'] scan       — fallback
                #   C) global URL-pattern scan     — last resort ไม่ใช้ [data-utime]
                # ════════════════════════════════════════════════════════════
                try:
                    article_data = self.driver.execute_script("""
try {
    var pn = arguments[0].toLowerCase();
    var PP = ['/posts/','story_fbid','/permalink/','fbid=',
              '/share/p/','pfbid','story.php','photo.php','/photos/'];
    var VP = ['/videos/','/reel/','/watch/','?v=','%3Fv%3D'];
    var AP = PP.concat(VP);

    // ── Helper: ตรวจว่า element มี ancestor ที่เป็น article ไหม ──────────
    function hasArticleAncestor(el) {
        var p = el.parentElement;
        while (p) {
            if (p.getAttribute && p.getAttribute('role') === 'article') return true;
            p = p.parentElement;
        }
        return false;
    }

    // ── วิธี A: data-pagelet="FeedUnit_N" ────────────────────────────────
    // FB ใส่ label นี้บน wrapper ของแต่ละโพสต์ — comment ไม่มี label นี้
    var tops = [];
    var units = document.querySelectorAll(
        '[data-pagelet^="FeedUnit"],[data-pagelet^="GroupFeedUnit"],' +
        '[data-pagelet^="MainFeed"],[data-pagelet^="Story"]'
    );
    for (var ui = 0; ui < units.length; ui++) {
        var arts = units[ui].querySelectorAll("div[role='article']");
        for (var ai = 0; ai < arts.length; ai++) {
            if (!hasArticleAncestor(arts[ai])) {
                if (tops.indexOf(arts[ai]) === -1) tops.push(arts[ai]);
                break;
            }
        }
    }

    // ── วิธี B: div[role='feed'] ─────────────────────────────────────────
    if (tops.length === 0) {
        var feed = document.querySelector("div[role='feed']");
        if (feed) {
            var allA = feed.querySelectorAll("div[role='article']");
            for (var i = 0; i < allA.length; i++) {
                var a = allA[i];
                if (!hasArticleAncestor(a) && !a.closest('ul') && !a.closest('li')) {
                    tops.push(a);
                }
            }
        }
    }

    // ── วิธี C: global scan + URL filter ─────────────────────────────────
    if (tops.length === 0) {
        var allArts = document.querySelectorAll("div[role='article']");
        for (var i = 0; i < allArts.length; i++) {
            var a = allArts[i];
            if (hasArticleAncestor(a)) continue;
            if (a.closest('ul') || a.closest('li')) continue;
            var links = a.querySelectorAll('a[href]');
            var hasPost = false;
            for (var li = 0; li < links.length; li++) {
                var h = links[li].href || '';
                for (var pi = 0; pi < AP.length; pi++) {
                    if (h.indexOf(AP[pi]) !== -1) { hasPost = true; break; }
                }
                if (hasPost) break;
            }
            if (hasPost) tops.push(a);
        }
    }

    // ── ดึงข้อมูลแต่ละ article ───────────────────────────────────────────
    var results = [];
    for (var ti = 0; ti < tops.length; ti++) {
        try {
            var art = tops[ti];
            var anchors = Array.from(art.querySelectorAll('a[href]'));

            // หา URL
            var url = '';
            for (var pi = 0; pi < PP.length && !url; pi++) {
                for (var li = 0; li < anchors.length && !url; li++) {
                    if ((anchors[li].href || '').indexOf(PP[pi]) !== -1) url = anchors[li].href;
                }
            }
            if (!url) {
                for (var li = 0; li < anchors.length && !url; li++) {
                    var h = anchors[li].href || '';
                    for (var vi = 0; vi < VP.length; vi++) {
                        if (h.indexOf(VP[vi]) !== -1) { url = h; break; }
                    }
                }
            }
            if (!url) {
                for (var li = 0; li < anchors.length && !url; li++) {
                    if ((anchors[li].href || '').indexOf('/share/') !== -1) url = anchors[li].href;
                }
            }
            if (!url) {
                for (var li = 0; li < anchors.length && !url; li++) {
                    var h = anchors[li].href || '';
                    if (h.length > 40 && h.toLowerCase().indexOf(pn) !== -1
                        && !h.endsWith('/' + pn) && !h.endsWith('/' + pn + '/')) url = h;
                }
            }

            // nested articles = comment sections
            var nested = Array.from(art.querySelectorAll("div[role='article']"));
            function inComment(el) {
                for (var ni = 0; ni < nested.length; ni++) {
                    if (nested[ni].contains(el)) return true;
                }
                return false;
            }

            // clone article เพื่อดึง text โดยไม่มี comment
            var clone = art.cloneNode(true);
            var cloneNested = clone.querySelectorAll("div[role='article']");
            for (var ci = 0; ci < cloneNested.length; ci++) cloneNested[ci].parentNode.removeChild(cloneNested[ci]);
            var cloneReact = clone.querySelectorAll('[aria-label*="omment"],[aria-label*="eaction"]');
            for (var ci = 0; ci < cloneReact.length; ci++) cloneReact[ci].parentNode.removeChild(cloneReact[ci]);

            // post text
            var txt = '';
            var msgSels = ['[data-ad-comet-preview="message"]','[data-testid="post_message"]','[data-ad-preview="message"]'];
            for (var si = 0; si < msgSels.length && !txt; si++) {
                var el = clone.querySelector(msgSels[si]);
                if (el) txt = (el.innerText || '').trim();
            }
            if (!txt) {
                var seen = {}, ls = [];
                var dirEls = clone.querySelectorAll('div[dir="auto"],span[dir="auto"]');
                for (var di = 0; di < dirEls.length; di++) {
                    var t = (dirEls[di].innerText || '').trim();
                    if (t && !seen[t]) { seen[t] = 1; ls.push(t); }
                }
                txt = ls.join('\n').trim();
            }
            if (!txt) txt = (clone.innerText || '').trim();

            // allText (ไม่มี comment)
            var allSeen = {}, allLines = [];
            var allDirEls = art.querySelectorAll('div[dir="auto"],span[dir="auto"]');
            for (var di = 0; di < allDirEls.length; di++) {
                if (inComment(allDirEls[di])) continue;
                var t = (allDirEls[di].innerText || '').trim();
                if (t && !allSeen[t]) { allSeen[t] = 1; allLines.push(t); }
            }

            // image
            var img = '';
            var imgs = art.querySelectorAll('img[src*="scontent"]');
            for (var ii = 0; ii < imgs.length && !img; ii++) {
                if (inComment(imgs[ii])) continue;
                var src = imgs[ii].src || '';
                if (!src || src.indexOf('emoji') !== -1) continue;
                var w = parseInt(imgs[ii].getAttribute('width') || '0');
                if (w && w <= 100) continue;
                img = src;
            }

            // timestamp
            var raw = (art.innerText || '').split('\\n').slice(0, 10).join('\\n');
            var ut = 0;
            var ae = art.querySelector('abbr[data-utime]') || art.querySelector('[data-utime]');
            if (ae) ut = parseInt(ae.getAttribute('data-utime') || '0');
            var lbl = '';
            if (!ut) {
                var tSels = ['a[role="link"]>span[aria-label]',
                             'a[href*="/posts/"]>span','a[href*="story_fbid"]>span',
                             'a[href*="pfbid"]>span','abbr[title]','span[title]','a>abbr'];
                for (var si = 0; si < tSels.length && !lbl; si++) {
                    var el = art.querySelector(tSels[si]);
                    if (!el || inComment(el)) continue;
                    var v = el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || '';
                    v = v.trim();
                    if (v && (v.match(/[0-9]/) || /ago|yesterday|just now/i.test(v))) { lbl = v; }
                }
            }

            results.push({
                postUrl:   url,
                postText:  txt,
                imageUrl:  img,
                rawText:   raw,
                allText:   allLines.join('\\n'),
                utime:     ut,
                timeLabel: lbl
            });
        } catch(artErr) {
            // ข้าม article ที่ error ไม่ให้ลามทั้ง batch
        }
    }
    return results;

} catch(e) {
    // คืน array ว่างถ้า JS ทั้งก้อน error — ไม่ throw ขึ้น Python
    return [];
}
                    """, page_name)

                    if article_data:
                        self.log(f"📦 [{page_name}] เจอ {len(article_data)} articles (scroll {scroll_rounds})")
                    elif article_data is None:
                        article_data = []
                except Exception as e:
                    self.log(f"⚠️ execute_script error: {type(e).__name__}: {str(e)[:120]}")
                    article_data = []

                new_in_round = False

                for data in article_data:
                    if self._stop_event.is_set() or stop_early: break
                    self._resume_event.wait()

                    try:
                        post_url = data.get("postUrl", "")
                        raw_for_id = (
                            data.get("postText") or data.get("allText") or data.get("rawText") or ""
                        ).strip()

                        if not post_url:
                            if not raw_for_id: continue
                            post_url = f"text_post://{page_name}/{hashlib.md5(raw_for_id.encode()).hexdigest()[:16]}"

                        # normalize domain
                        post_url = (post_url
                                    .replace("web.facebook.com", "www.facebook.com")
                                    .replace("m.facebook.com",   "www.facebook.com"))

                        # normalize URL — เก็บ query params ของ story.php/photo.php
                        if any(x in post_url for x in ("story.php", "photo.php", "permalink/php")):
                            p  = urlparse(post_url)
                            q  = parse_qs(p.query)
                            qc = {k: v[0] for k, v in q.items()
                                  if k in {"story_fbid", "id", "fbid", "set", "type"}}
                            post_url_clean = urlunparse(p._replace(
                                query=urlencode(qc), fragment="")).rstrip("/")
                        else:
                            post_url_clean = post_url.split("?")[0].rstrip("/")

                        if post_url_clean in seen_run: continue
                        seen_run.add(post_url_clean)
                        new_in_round = True

                        post_id = self._extract_post_id(post_url_clean)

                        # debug URL format
                        fmt = ("pfbid"     if "pfbid"     in post_url_clean else
                               "story.php" if "story.php" in post_url_clean else
                               "text"      if post_url_clean.startswith("text_post://") else "std")
                        self.log(f"🔗 [{page_name}] fmt={fmt} | id={str(post_id)[:24]}")

                        if self.db.is_seen(post_id) or self.db.is_seen_by_url(post_url_clean):
                            continue

                        # timestamp
                        post_time = self._parse_timestamp(
                            data.get("rawText", ""),
                            utime=int(data.get("utime") or 0),
                            label=data.get("timeLabel", ""),
                        )
                        if post_time is not None:
                            if post_time < cutoff:
                                consecutive_old += 1
                                self.log(f"⏩ โพสต์เก่า ({consecutive_old}/{MAX_OLD}) | {post_time.strftime('%d/%m/%Y %H:%M')}")
                                if consecutive_old >= MAX_OLD:
                                    self.log(f"🏁 เก่าติดกัน {MAX_OLD} รายการ — หยุดเพจนี้")
                                    stop_early = True; break
                                continue
                            consecutive_old = 0
                            self.log(f"✅ โพสต์ใหม่ | {post_time.strftime('%d/%m/%Y %H:%M')}")
                        else:
                            self.log("⚠️ อ่านเวลาไม่ออก — รวมไว้ก่อน")

                        # text
                        post_text = data.get("postText", "").strip() or data.get("allText", "").strip() or data.get("rawText", "").strip()
                        image_url = data.get("imageUrl") or None

                        # keyword filter
                        found_kw = []
                        if keywords:
                            texts = list({
                                s.lower() for s in (
                                    post_text, data.get("allText",""), data.get("rawText","")
                                ) if s
                            })
                            for kw in keywords:
                                if any(kw.lower().strip() in t for t in texts):
                                    if kw not in found_kw: found_kw.append(kw)
                            if not found_kw: continue

                        # ตัด timestamp ออกจากต้นข้อความ
                        if post_text:
                            lines = post_text.split("\n")
                            for i, ln in enumerate(lines):
                                tr = ln.strip()
                                if tr and len(tr) > 2:
                                    lo = tr.lower()
                                    if not (
                                        any(p in lo for p in ("เมื่อ","เพิ่ง","yesterday","just now","ago"))
                                        or re.match(r"^\d+.*[นาทีชั่วโมงวันสัปดาห์เดือนปี]", lo)
                                        or re.match(r"^\d+.*(?:min|hr|day|week|month|year)", lo)
                                    ):
                                        post_text = "\n".join(lines[i:]).strip()
                                        break

                        self.log(f"📨 keyword: {found_kw} | {post_url_clean[:70]}")

                        # AI
                        ai_result = None
                        if self.ai_analyzer and post_text:
                            ai_result = self.ai_analyzer.analyze(post_text)
                            if ai_result and ai_result.get("is_target") and ai_result.get("score",0) >= 6:
                                self.log(f"🎯 AI PASS | score={ai_result.get('score')}/10")
                                if self.sheets_manager:
                                    self.sheets_manager.upload_news(
                                        page_name=page_name, post_url=post_url_clean,
                                        post_text=post_text,
                                        persons=ai_result.get("persons",[]),
                                        score=ai_result.get("score",0),
                                        reason=ai_result.get("reason",""),
                                    )
                                    self.log("💾 บันทึก Google Sheets")

                        # notify
                        self.discord.send_post(page_name, page_url, post_url_clean,
                                               post_text, found_kw, image_url, ai_result=ai_result)
                        self.tg.send_post(page_name, page_url, post_url_clean,
                                          post_text, found_kw, image_url)

                        self.db.mark_seen(post_id, page_url, post_url_clean)
                        new_posts += 1
                        time.sleep(1)

                    except StaleElementReferenceException:
                        continue
                    except OSError as e:
                        if "cacert.pem" in str(e) or "certificate" in str(e).lower():
                            try:
                                import certifi as _c
                                r = _c.where()
                                if os.path.isfile(r):
                                    os.environ["SSL_CERT_FILE"] = r
                                    os.environ["REQUESTS_CA_BUNDLE"] = r
                            except Exception: pass
                        else:
                            self.log(f"⚠️ OSError: {e}")
                        continue
                    except Exception as e:
                        self.log(f"⚠️ ข้ามโพสต์: {type(e).__name__}: {e}")
                        continue

                if new_in_round:
                    rounds_no_url = 0
                else:
                    rounds_no_url += 1
                    if rounds_no_url >= MAX_NO_URL:
                        self.log(f"📄 ไม่มี URL ใหม่ {MAX_NO_URL} รอบ — จบ")
                        break

        except InvalidSessionIdException:
            self.log(f"❌ Browser session หมดอายุระหว่างสแกน {page_name}")
            raise
        except WebDriverException as e:
            self.log(f"❌ WebDriver: {e}")
        except Exception as e:
            self.log(f"❌ scrape_page {page_name}: {e}")

        self.log(f"📊 {page_name} | scroll {scroll_rounds} รอบ | โพสต์ใหม่: {new_posts}")
        return new_posts

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self, email, password, page_urls, keywords, hours_back, loop_minutes):
        MAX_FAILS        = 5
        RETRY_WAIT       = 300
        _started         = False
        _t0              = time.time()
        _total           = 0
        last_cleanup     = None

        try:
            self.discord.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            self.tg.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            _started = True

            while not self._stop_event.is_set():

                now = datetime.now()
                if now.hour >= 9 and last_cleanup != now.date():
                    self.log("🧹 ล้าง DB เก่า...")
                    if self.db.cleanup_old_data():
                        self.log("✅ ล้าง DB สำเร็จ")
                    last_cleanup = now.date()

                self._cycle_count += 1
                t_cycle = time.time()
                self.log(f"\n{'='*50}")
                self.log(f"🔄 รอบที่ {self._cycle_count} | {now.strftime('%d/%m/%Y %H:%M:%S')}")

                cycle_ok = False
                try:
                    self._start_browser()

                    # ── Cookie / Login ────────────────────────────────────────
                    cookie_existed = os.path.exists(COOKIES_FILE)
                    if not self._load_cookies():
                        if cookie_existed:
                            # ── Cookie มีแต่ invalid = หมดอายุ ──────────────
                            self.log("🍪 Cookie หมดอายุ — แจ้งเตือนและหยุดรอ Resume")
                            self._handle_cookie_expired()
                            if self._stop_event.is_set(): break

                            # ตรวจ DOM หลัง Resume — navigate ก่อนเพื่อให้ DOM พร้อม
                            self.driver.get(self.HOME_URL)
                            time.sleep(3)
                            if self._is_logged_in():
                                self.log("✅ ผู้ใช้ล็อกอินสำเร็จ — บันทึก Cookie ใหม่")
                                self._save_cookies()
                            else:
                                self.log("🔑 ยังไม่ได้ล็อกอิน — ลอง auto-login...")
                                if not self.login(email, password):
                                    raise RuntimeError("Login ล้มเหลวหลัง Cookie หมดอายุ")
                        else:
                            # ── ครั้งแรก ─────────────────────────────────────
                            self.log("🔑 ไม่มี Session เดิม — ล็อกอินใหม่")
                            if not self.login(email, password):
                                raise RuntimeError("Login ล้มเหลว — ตรวจสอบ Email/Password")

                    time.sleep(1)
                    self.hide_browser()

                    total_new = 0
                    for url in page_urls:
                        if self._stop_event.is_set(): break
                        url = url.strip()
                        if not url: continue
                        cnt = self.scrape_page(url, keywords, hours_back)
                        total_new += cnt
                        self.log(f"📊 {url.split('/')[-1]}: {cnt} โพสต์ใหม่")
                        if not self._stop_event.is_set():
                            time.sleep(random.uniform(2.0, 5.0))

                    if self._stop_event.is_set(): break

                    _total += total_new
                    dur = time.time() - t_cycle
                    self.log(f"✅ รอบเสร็จ | โพสต์ใหม่: {total_new}")
                    self.discord.send_cycle_complete(dur, loop_minutes, total_new, len(page_urls))
                    self.tg.send_cycle_complete(dur, loop_minutes, total_new, len(page_urls))
                    self._consecutive_failures = 0
                    cycle_ok = True

                except Exception as e:
                    self._consecutive_failures += 1
                    self.log(f"❌ รอบ {self._cycle_count} ล้มเหลว ({self._consecutive_failures}/{MAX_FAILS}): {type(e).__name__}: {e}")
                    if self._consecutive_failures >= MAX_FAILS:
                        self.log(f"🔴 ล้มเหลว {MAX_FAILS} รอบติด — หยุดทำงาน")
                        self.discord.send_obstacle(f"FATAL: ล้มเหลว {MAX_FAILS} รอบ", "")
                        self.tg.send_obstacle(f"FATAL: ล้มเหลว {MAX_FAILS} รอบ", "")
                        break
                finally:
                    self.log("🛑 ปิด Browser ชั่วคราว...")
                    self._safe_quit_driver()

                if self._stop_event.is_set(): break

                wait = (loop_minutes * 60) if cycle_ok else RETRY_WAIT
                self.log(f"⏳ รอ {wait // 60} นาที...")
                self._sleep_interruptible(wait)

        except OSError as e:
            if "cacert.pem" in str(e) or "certificate" in str(e).lower():
                self.log("⚠️ SSL Error — รีสตาร์ทโปรแกรมหนึ่งครั้ง")
            else:
                self.log(f"❌ Fatal OSError: {e}")
        except Exception as e:
            self.log(f"❌ Fatal: {type(e).__name__}: {e}")
        finally:
            if _started:
                self.discord.send_stopped(time.time() - _t0, _total)
                self.tg.send_stopped(time.time() - _t0, _total)
            self._safe_quit_driver()
            self.log("🏁 Scraper หยุดทำงานสมบูรณ์")

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()