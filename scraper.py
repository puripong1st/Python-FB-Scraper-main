"""
scraper.py
━━━━━━━━━━
FacebookScraper — เปิด Browser, Login, สแกนเพจ, ส่งแจ้งเตือน
ใช้ undetected-chromedriver เพื่อหลีกเลี่ยง bot detection

การแก้ไข:
  - เปลี่ยนไอคอน Browser เฉพาะหน้าต่างที่ Scraper เปิด ด้วย ctypes (ไม่ต้องใช้ pywin32)
  - ดึงทุกโพสต์รวมโพสต์สั้น — ตรวจ keyword จาก allText ด้วย
  - กรองเฉพาะโพสต์ ไม่ดึงคอมเมนต์ (กรอง top-level article เท่านั้น)
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
    SELECTORS = {
        "email_input": "//input[@id='email' or @name='email']",
        "pass_input":  "//input[@id='pass' or @name='pass']",
        "login_btn":   "//button[@name='login' or @data-testid='royal_login_button']",
        "post_story":  "[data-testid='story-subtitle'], div[role='article']",
        "feed_posts":  "div[role='feed'] > div",
    }

    HOME_URL = "https://www.facebook.com"

    def __init__(
        self,
        log_callback,
        db: DatabaseManager,
        discord: DiscordNotifier,
        tg: TelegramNotifier,
        ai_analyzer=None,
        sheets_manager=None,
        on_cookies_saved=None,
    ):
        self.log            = log_callback
        self.db             = db
        self.discord        = discord
        self.tg             = tg
        self.ai_analyzer    = ai_analyzer
        self.sheets_manager = sheets_manager
        self._on_cookies_saved = on_cookies_saved

        self._driver      = None
        self._driver_lock = threading.RLock()

        self._stop_event   = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._is_paused = False

        self._browser_hidden = False
        self._consecutive_failures = 0
        self._cycle_count = 0

        # เก็บ PID ของ chromedriver ที่ scraper เปิด เพื่อกรองเฉพาะหน้าต่างของเราเท่านั้น
        self._scraper_chrome_pids: set = set()

    # ── Thread-safe driver property ───────────────────────────────────────────

    @property
    def driver(self):
        with self._driver_lock:
            return self._driver

    @driver.setter
    def driver(self, value):
        with self._driver_lock:
            self._driver = value

    # ── Browser hide / show ───────────────────────────────────────────────────

    def _collect_chrome_pids(self) -> set:
        """เก็บ chrome.exe PIDs ที่เพิ่งเปิดขึ้นมาใหม่หลัง browser launch
        วิธี: snapshot PIDs ก่อน launch ไว้ใน _chrome_pids_before แล้ว diff
        ถ้าไม่มี snapshot fallback ใช้ child-tree จาก chromedriver PID
        """
        try:
            import subprocess, json as _json

            def _all_chrome_pids() -> set:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'} "
                     "| Select-Object ProcessId | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10
                )
                if not res.stdout.strip():
                    return set()
                procs = _json.loads(res.stdout)
                if isinstance(procs, dict):
                    procs = [procs]
                return {int(p["ProcessId"]) for p in procs if p.get("ProcessId")}

            before: set = getattr(self, '_chrome_pids_before', set())
            after  = _all_chrome_pids()
            new_pids = after - before  # PIDs ที่เพิ่มขึ้นหลัง launch

            if new_pids:
                self._scraper_chrome_pids = new_pids
                self.log(f"🔍 Chrome PIDs ที่ Scraper เปิด: {new_pids}")
                return new_pids

            # fallback — ใช้ child-tree จาก chromedriver (วิธีเดิม)
            drv = self.driver
            if drv:
                root_pid = drv.service.process.pid
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'} "
                     "| Select-Object ProcessId,ParentProcessId | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10
                )
                if result.stdout.strip():
                    procs = _json.loads(result.stdout)
                    if isinstance(procs, dict):
                        procs = [procs]
                    pids = {root_pid}
                    queue = [root_pid]
                    while queue:
                        p = queue.pop()
                        for proc in procs:
                            ppid = int(proc.get("ParentProcessId") or 0)
                            cpid = int(proc.get("ProcessId") or 0)
                            if ppid == p and cpid not in pids:
                                pids.add(cpid)
                                queue.append(cpid)
                    self._scraper_chrome_pids = pids
                    self.log(f"🔍 Chrome PIDs (fallback tree): {pids}")
                    return pids
        except Exception as e:
            self.log(f"⚠️ _collect_chrome_pids: {e}")
        pids = set()
        try:
            pids = {self.driver.service.process.pid}
        except Exception:
            pass
        self._scraper_chrome_pids = pids
        return pids

    def _find_browser_hwnds(self) -> list:
        """หา HWND ของ Chrome window ที่ Scraper เปิด โดยใช้ _scraper_chrome_pids
        ซึ่งเก็บ chrome.exe PIDs จาก snapshot diff (ไม่ใช่ chromedriver PID)
        """
        try:
            import ctypes, ctypes.wintypes
            pids = self._scraper_chrome_pids  # ชุด chrome.exe PIDs ที่ scraper เปิด

            found = []
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

            def _cb(hwnd, _):
                cls = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(hwnd, cls, 64)
                if cls.value not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0"):
                    return True
                win_pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                # กรองเฉพาะ PID ที่รู้ว่าเป็นของ scraper / ถ้าไม่มีข้อมูล fallback รับทุก Chrome
                if pids and win_pid.value not in pids:
                    return True
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            ctypes.windll.user32.EnumWindows(EnumProc(_cb), 0)
            return found
        except Exception as e:
            self.log(f"⚠️ _find_browser_hwnds: {e}")
            return []

    def hide_browser(self):
        """ซ่อนหน้าต่าง Browser ออกจากหน้าจอและ Taskbar"""
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

    def show_browser(self):
        """แสดงหน้าต่าง Browser กลับมาที่หน้าจอ"""
        try:
            import ctypes
            SW_RESTORE = 9
            hwnds = self._find_browser_hwnds()
            shown = 0
            for hwnd in hwnds:
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                shown += 1
            if shown:
                self.log(f"👁️ แสดง Browser แล้ว ({shown} หน้าต่าง)")
            self._browser_hidden = False
        except Exception as e:
            self.log(f"⚠️ show_browser: {e}")

    def _set_chrome_icon(self):
        """
        เปลี่ยนไอคอน Title bar + Taskbar ของ Chrome ที่ Scraper เปิด
        - WM_SETICON        → title bar
        - SetClassLongPtrW  → taskbar icon (GCL_HICON)
        """
        try:
            import ctypes, ctypes.wintypes

            if getattr(sys, 'frozen', False):
                icon_path = os.path.join(sys._MEIPASS, "app_icon.ico")
            else:
                icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")

            if not os.path.exists(icon_path):
                self.log("⚠️ ไม่พบ app_icon.ico — ข้ามการเปลี่ยนไอคอน")
                return

            IMAGE_ICON      = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE  = 0x0040
            WM_SETICON      = 0x0080
            ICON_SMALL      = 0
            ICON_BIG        = 1
            GCL_HICON       = -14   # class icon → taskbar
            GCL_HICONSM     = -34   # class small icon

            hicon_big  = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            hicon_sm   = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            hicon_task = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON,  0,  0,
                                                     LR_LOADFROMFILE | LR_DEFAULTSIZE)

            if not any([hicon_big, hicon_sm, hicon_task]):
                self.log("⚠️ LoadImageW ล้มเหลวทุกขนาด")
                return

            # รอให้ Chrome window แสดงขึ้นมา (สูงสุด 15 วิ)
            hwnds = []
            for _ in range(30):
                hwnds = self._find_browser_hwnds()
                if hwnds:
                    break
                time.sleep(0.5)

            if not hwnds:
                self.log("⚠️ ไม่พบหน้าต่าง Browser ที่จะเปลี่ยนไอคอน")
                return

            set_count = 0
            for hwnd in hwnds:
                try:
                    # Title bar
                    if hicon_big:  ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon_big)
                    if hicon_sm:   ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_sm)
                    # Taskbar icon — ต้องใช้ SetClassLongPtrW
                    if hicon_task:
                        ctypes.windll.user32.SetClassLongPtrW(hwnd, GCL_HICON,   hicon_task)
                        ctypes.windll.user32.SetClassLongPtrW(hwnd, GCL_HICONSM, hicon_task)
                    set_count += 1
                except Exception:
                    pass

            if set_count:
                self.log(f"🎨 เปลี่ยนไอคอน Browser แล้ว ({set_count} หน้าต่าง)")
            else:
                self.log("⚠️ ไม่สามารถเปลี่ยนไอคอนได้")
        except Exception as e:
            self.log(f"⚠️ _set_chrome_icon: {e}")

    def _safe_quit_driver(self):
        """ปิด Browser อย่างปลอดภัย"""
        drv = self.driver
        if drv is None:
            return
        try:
            drv.quit()
        except Exception:
            pass
        finally:
            self.driver = None
            self._browser_hidden = False
            self._scraper_chrome_pids = set()   # reset PID cache เมื่อปิด browser

    def _sleep_interruptible(self, seconds: float, step: float = 5.0):
        """sleep ที่ตรวจ stop_event ทุก step วินาที"""
        elapsed = 0.0
        while elapsed < seconds and not self._stop_event.is_set():
            chunk = min(step, seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    # ─────────────────────────────────────────────────────────────────────────
    # Browser lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _start_browser(self):
        import winreg
        import shutil
        import subprocess

        # 1. เคลียร์ซากเก่าและแคชอัตโนมัติ
        try:
            os.system("taskkill /f /im chromedriver.exe /t >nul 2>&1")
            appdata_path = os.getenv('APPDATA')
            if appdata_path:
                uc_cache_dir = os.path.join(appdata_path, 'undetected_chromedriver')
                if os.path.exists(uc_cache_dir):
                    shutil.rmtree(uc_cache_dir, ignore_errors=True)
                    self.log("🧹 ลบโฟลเดอร์แคช Driver เก่าทิ้งแล้ว")
        except Exception as e:
            self.log(f"⚠️ ระบบล้างแคชอัตโนมัติแจ้งเตือน: {e}")

        # 2. Helper สร้าง ChromeOptions ใหม่เสมอ
        def _make_options():
            opts = uc.ChromeOptions()
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--lang=th-TH,th;q=0.9,en-US;q=0.8")
            opts.add_argument("--window-size=1280,900")
            opts.page_load_strategy = 'eager'
            return opts

        chrome_version = None

        # 3. อ่านเวอร์ชันจากไฟล์ EXE ตรงๆ
        _ps_commands = [
            "(Get-Item (Get-Command chrome).Source).VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r'(Get-Item "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe").VersionInfo.ProductVersion',
        ]
        for ps_cmd in _ps_commands:
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=8
                )
                version_str = result.stdout.strip()
                if version_str and version_str[0].isdigit():
                    chrome_version = int(version_str.split('.')[0])
                    self.log(f"🔎 Chrome เวอร์ชันจริง (EXE): {chrome_version}")
                    break
            except Exception:
                continue

        # 4. Fallback: อ่านจาก Registry
        if not chrome_version:
            registry_keys = [
                (winreg.HKEY_CURRENT_USER,  r"Software\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
            ]
            for hive, path in registry_keys:
                try:
                    key = winreg.OpenKey(hive, path)
                    version_str, _ = winreg.QueryValueEx(key, "version")
                    if version_str:
                        chrome_version = int(version_str.split('.')[0])
                        self.log(f"⚠️ ใช้เวอร์ชันจาก Registry (อาจไม่ตรง 100%): {chrome_version}")
                        break
                except Exception:
                    continue

        if not chrome_version:
            self.log("⚠️ ตรวจไม่พบเวอร์ชัน Chrome — จะปล่อยให้ระบบเดาอัตโนมัติ")

        # 5. เปิด Browser — ลองสูงสุด 3 รอบ
        strategies = []
        if chrome_version:
            strategies.append({"version_main": chrome_version})
        strategies.append({})
        strategies.append({"version_main": None})

        last_err = None
        for attempt, kwargs in enumerate(strategies, 1):
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            try:
                self.log(f"🔄 เปิด Browser รอบที่ {attempt}/{len(strategies)} {kwargs or '(auto)'}")
                self._safe_quit_driver()
                time.sleep(1)
                # snapshot chrome.exe PIDs ก่อน launch เพื่อ diff หา PIDs ของเรา
                try:
                    import subprocess as _sp, json as _jj
                    _res = _sp.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'} "
                         "| Select-Object ProcessId | ConvertTo-Json"],
                        capture_output=True, text=True, timeout=10
                    )
                    if _res.stdout.strip():
                        _pp = _jj.loads(_res.stdout)
                        if isinstance(_pp, dict): _pp = [_pp]
                        self._chrome_pids_before = {int(p["ProcessId"]) for p in _pp if p.get("ProcessId")}
                    else:
                        self._chrome_pids_before = set()
                except Exception:
                    self._chrome_pids_before = set()
                self.driver = uc.Chrome(options=_make_options(), use_subprocess=True, **kwargs)
                self.driver.set_page_load_timeout(60)
                self.log("🌐 เปิด Browser สำเร็จ")

                # collect PID ทันทีหลังเปิด Browser — เพื่อใช้กรองหน้าต่าง
                time.sleep(0.5)
                self._collect_chrome_pids()

                def _set_icon_with_retry(max_wait: float = 15.0, interval: float = 0.5):
                    """รอจนกว่า Chrome window จะพร้อม แล้วค่อยเปลี่ยนไอคอน"""
                    deadline = time.time() + max_wait
                    while time.time() < deadline:
                        hwnds = self._find_browser_hwnds(_debug=(time.time() > deadline - interval))
                        if hwnds:
                            self._set_chrome_icon()
                            return
                        time.sleep(interval)
                    self.log("⚠️ _set_chrome_icon: รอ Chrome window ครบ 15 วิแล้วยังไม่พบ")

                # navigate ไปหน้าแรกก่อน เพื่อให้ title bar มีข้อความและ window visible
                try:
                    self.driver.get(self.HOME_URL)
                except Exception:
                    pass
                threading.Thread(target=_set_icon_with_retry, daemon=True).start()
                return
            except Exception as e:
                last_err = e
                self.log(f"⚠️ รอบ {attempt} ล้มเหลว: {e}")
                self._safe_quit_driver()
                if attempt < len(strategies):
                    time.sleep(3)

        raise RuntimeError(f"❌ เปิด Browser ไม่สำเร็จหลัง {len(strategies)} รอบ: {last_err}")

    # ── Cookies ───────────────────────────────────────────────────────────────

    def _save_cookies(self):
        drv = self.driver
        if not drv:
            return
        try:
            cookies = drv.get_cookies()
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self.log("🍪 บันทึก Session Cookies แล้ว (JSON)")
        except Exception as e:
            self.log(f"⚠️ บันทึก Cookies ไม่สำเร็จ: {e}")
            return

        if self._on_cookies_saved:
            try:
                self._on_cookies_saved()
            except Exception as e:
                self.log(f"⚠️ on_cookies_saved callback error: {e}")

    def _load_cookies(self) -> bool:
        if not os.path.exists(COOKIES_FILE):
            return False
        try:
            self.driver.get(self.HOME_URL)
            time.sleep(2)
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    self.log(f"⚠️ ข้าม cookie ที่ใส่ไม่ได้: {e}")
            self.driver.refresh()
            time.sleep(3)

            if "login" not in self.driver.current_url.lower():
                self.log("✅ กู้คืน Session เดิมสำเร็จ — ไม่ต้องล็อกอินใหม่")
                if self._on_cookies_saved:
                    try:
                        self._on_cookies_saved()
                    except Exception:
                        pass
                return True
        except Exception as e:
            self.log(f"⚠️ โหลด Cookies ไม่สำเร็จ: {e}")
        return False

    # ── Login helpers ─────────────────────────────────────────────────────────

    def _type_human(self, element, text: str, delay: float = 0.06):
        element.clear()
        time.sleep(0.3)
        for ch in text:
            element.send_keys(ch)
            time.sleep(random.uniform(delay * 0.7, delay * 1.5))

    def _click_login_button(self) -> bool:
        strategies = [
            (By.CSS_SELECTOR, "button[name='login']"),
            (By.CSS_SELECTOR, "[data-testid='royal_login_button']"),
            (By.CSS_SELECTOR, "form button[type='submit']"),
            (By.XPATH, "//button[contains(., 'เข้าสู่ระบบ')]"),
            (By.XPATH, "//button[contains(., 'Log in') or contains(., 'Log In')]"),
            (By.XPATH, "//*[@id='loginform']//button"),
            (By.XPATH, "//div[@role='button' and (contains(., 'Log') or contains(., 'เข้า'))]"),
        ]
        for by, selector in strategies:
            try:
                btn = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((by, selector))
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.3)
                btn.click()
                self.log("🖱️ คลิกปุ่ม Login สำเร็จ")
                return True
            except (TimeoutException, NoSuchElementException, Exception):
                continue

        try:
            pass_field = self.driver.find_element(By.XPATH, self.SELECTORS["pass_input"])
            pass_field.send_keys(Keys.RETURN)
            self.log("⌨️ กด Enter บน Password field (fallback)")
            return True
        except Exception as e:
            self.log(f"⚠️ fallback Enter ล้มเหลว: {e}")
        return False

    def login(self, email: str, password: str) -> bool:
        try:
            self.driver.get(f"{self.HOME_URL}/login")
            wait = WebDriverWait(self.driver, 20)

            self.log("📧 กำลังกรอก Email...")
            email_field = wait.until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["email_input"]))
            )
            self._type_human(email_field, email, delay=0.06)
            time.sleep(0.4)

            self.log("🔑 กำลังกรอก Password...")
            pass_field = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["pass_input"]))
            )
            self._type_human(pass_field, password, delay=0.05)
            time.sleep(0.6)

            self.log("🖱️ กำลังคลิกปุ่ม Login...")
            clicked = self._click_login_button()
            if not clicked:
                self.log("⚠️ หาปุ่ม Login ไม่เจอ — กรุณากด Login ใน Browser ด้วยตัวเอง แล้วกด Resume")
                self._handle_obstacle("Login Button Not Found — กรุณากด Login ด้วยตัวเอง", f"{self.HOME_URL}/login")
                if self._stop_event.is_set():
                    return False

            self.log("⏳ รอหน้าเว็บโหลดหลัง Login...")
            time.sleep(6)

            obstacle = self._detect_obstacle()
            if obstacle:
                self.log(f"⚠️ ติด {obstacle} หลังล็อกอิน")
                self._handle_obstacle(obstacle, f"{self.HOME_URL}/login")
                if self._stop_event.is_set():
                    return False
                time.sleep(2)

            current_url = self.driver.current_url.lower()
            if "login" not in current_url and "facebook.com" in current_url:
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ")
                return True

            self.log("⚠️ ยังอยู่หน้า Login — อาจ Email/Password ผิด หรือมี CAPTCHA ที่มองไม่เห็น")
            self.log("👉 กรุณาล็อกอินด้วยตัวเองในหน้าต่าง Browser แล้วกด Resume")
            self._handle_obstacle("Login ไม่สำเร็จ — กรุณาล็อกอินด้วยตัวเอง", f"{self.HOME_URL}/login")
            if self._stop_event.is_set():
                return False
            time.sleep(2)
            if "login" not in self.driver.current_url.lower():
                self._save_cookies()
                self.log("✅ ล็อกอินสำเร็จ (manual)")
                return True
            return False

        except TimeoutException:
            self.log("⚠️ Timeout ระหว่าง Login — กรุณาล็อกอินด้วยตัวเองใน Browser แล้วกด Resume")
            self._handle_obstacle("Timeout — กรุณาล็อกอินด้วยตัวเอง", f"{self.HOME_URL}/login")
            if self._stop_event.is_set():
                return False
            time.sleep(2)
            return "login" not in self.driver.current_url.lower()
        except Exception as e:
            self.log(f"❌ Login Error: {e}")
            return False

    # ── Obstacle detection ────────────────────────────────────────────────────

    def _detect_obstacle(self) -> str | None:
        url   = self.driver.current_url.lower()
        title = self.driver.title.lower()

        if "checkpoint"            in url or "checkpoint"  in title: return "Checkpoint"
        if "two_step_verification" in url or "two_factor"  in url:   return "2FA (Two-Factor Authentication)"
        if "captcha"               in url or "captcha"     in title: return "CAPTCHA"
        if "login_attempt"         in url:                           return "Login Attempt Blocked"
        if "suspended"             in url or "disabled"    in url:   return "Account Suspended/Disabled"

        _ID_URL  = ("identity", "identity_verification", "id_verification",
                    "confirm_identity", "verify_identity")
        _ID_TEXT = ("confirm your identity", "ยืนยันตัวตน")

        try:
            if any(sig in url for sig in _ID_URL):
                return "Identity Verification"

            found_verify_form = self.driver.execute_script("""
                const forms = Array.from(document.querySelectorAll('form'));
                return forms.some(f => {
                    const action = (f.action || '').toLowerCase();
                    return action.includes('identity') || action.includes('checkpoint')
                           || action.includes('confirm');
                });
            """)
            found_heading = self.driver.execute_script("""
                const signals = arguments[0];
                const headings = document.querySelectorAll('h1, h2, h3');
                for (const h of headings) {
                    const txt = h.innerText ? h.innerText.toLowerCase() : '';
                    if (!txt) continue;
                    const style = window.getComputedStyle(h);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    if (signals.some(s => txt.includes(s))) return true;
                }
                return false;
            """, list(_ID_TEXT))

            if found_verify_form or found_heading:
                self.log("⏳ พบสัญญาณ Identity Verification — ตรวจสอบซ้ำใน 2 วินาที...")
                time.sleep(2)
                url2 = self.driver.current_url.lower()
                if url2 != url:
                    return None
                confirmed = self.driver.execute_script("""
                    const signals = arguments[0];
                    const headings = document.querySelectorAll('h1, h2, h3');
                    for (const h of headings) {
                        const txt = h.innerText ? h.innerText.toLowerCase() : '';
                        if (!txt) continue;
                        const style = window.getComputedStyle(h);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        if (signals.some(s => txt.includes(s))) return true;
                    }
                    const forms = Array.from(document.querySelectorAll('form'));
                    return forms.some(f => {
                        const action = (f.action || '').toLowerCase();
                        return action.includes('identity') || action.includes('checkpoint')
                               || action.includes('confirm');
                    });
                """, list(_ID_TEXT))
                if confirmed:
                    return "Identity Verification"
                else:
                    self.log("✅ สัญญาณหายไปเอง — ไม่ใช่ Identity Verification จริง (false positive)")

        except Exception as e:
            self.log(f"⚠️ _detect_obstacle identity check: {e}")
        return None

    def _handle_obstacle(self, obstacle_type: str, page_url: str = ""):
        self.log(f"🚨 ติด {obstacle_type} — หยุดรอผู้ใช้แก้ไข กด Resume เมื่อเสร็จ")
        self.show_browser()
        self.discord.send_obstacle(obstacle_type, page_url)
        self.tg.send_obstacle(obstacle_type, page_url)
        self._resume_event.clear()
        self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume แล้ว — กลับมาทำงานต่อ")
        self.hide_browser()

    def resume(self):
        self._resume_event.set()

    # ── Scrolling ─────────────────────────────────────────────────────────────

    def _slow_scroll(self, scrolls: int = 4, pause: float = 2.0):
        for _ in range(scrolls):
            if self._stop_event.is_set():
                break
            self.driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            time.sleep(random.uniform(pause * 0.8, pause * 1.3))

    # ── Post ID & Timestamp ───────────────────────────────────────────────────

    def _extract_post_id(self, url: str) -> str | None:
        patterns = [
            r"/posts/(\d+)",
            r"/videos/(\d+)",
            r"story_fbid=(\d+)",
            r"/permalink/(\d+)",
            r"fbid=(\d+)",
            r"/reel/(\d+)",
            r"[?&]v=(\d+)",
            r"/watch/\?v=(\d+)",
            r"/share/p/([^/]+)",
            r"/share/([^/]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        return hashlib.md5(url.encode("utf-8")).hexdigest()

    _TH_MONTH_SHORT = {
        "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4,
        "พ.ค.": 5, "มิ.ย.": 6, "ก.ค.": 7, "ส.ค.": 8,
        "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
    }
    _TH_MONTH_LONG = {
        "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
        "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
        "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    }

    def _parse_thai_date(self, text: str) -> "datetime | None":
        now = datetime.now()
        for abbr, month in self._TH_MONTH_SHORT.items():
            if abbr in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    day  = int(nums[0])
                    year = int(nums[-1])
                    if year > 2400:
                        year -= 543
                    elif year < 100:
                        year += (2500 - 543) if year < 50 else (2400 - 543)
                    try:
                        return datetime(year, month, day)
                    except ValueError:
                        pass
        for full, month in self._TH_MONTH_LONG.items():
            if full in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    day  = int(nums[0])
                    year = int(nums[-1])
                    if year > 2400:
                        year -= 543
                    elif year < 100:
                        year += (2500 - 543) if year < 50 else (2400 - 543)
                    try:
                        return datetime(year, month, day)
                    except ValueError:
                        pass
        en_months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        tl = text.lower()
        for en, month in en_months.items():
            if en in tl:
                nums = re.findall(r"\d+", text)
                nums = [n for n in nums if int(n) != month]
                if len(nums) >= 2:
                    try:
                        candidates = sorted([int(n) for n in nums])
                        day  = next((n for n in candidates if 1 <= n <= 31), None)
                        year = next((n for n in candidates if n > 31), None)
                        if day and year:
                            return datetime(year, month, day)
                    except (ValueError, StopIteration):
                        pass
        return None

    def _parse_post_timestamp_text(self, raw_text: str, utime: int = 0, time_label: str = "") -> "datetime | None":
        now = datetime.now()

        if utime and utime > 0:
            try:
                return datetime.fromtimestamp(utime)
            except (OSError, OverflowError, ValueError):
                pass

        if time_label:
            result = self._parse_thai_date(time_label)
            if result:
                return result

        try:
            lines = raw_text.split("\n")[:10]
            for line in lines:
                text = line.strip().replace("·", "").replace(",", "").strip()
                if not text:
                    continue
                tl = text.lower()

                if "เพิ่ง" in tl or "เมื่อสักครู่" in tl or "just now" in tl:
                    return now
                if "เมื่อวาน" in tl or "yesterday" in tl:
                    return now - timedelta(days=1)

                m = re.search(
                    r"(\d+)\s*(นาที|ชั่วโมง|ชม\.?|วัน|สัปดาห์|เดือน|ปี|mins?|m\b|hrs?|h\b|days?|d\b|weeks?|w\b|months?|years?)",
                    tl,
                )
                if m:
                    num  = int(m.group(1))
                    unit = m.group(2)
                    if   "นาที"    in unit or "min" in unit or unit == "m": return now - timedelta(minutes=num)
                    elif "ชม"      in unit or "ชั่วโมง" in unit or "hr"  in unit or unit == "h": return now - timedelta(hours=num)
                    elif "วัน"     in unit or "day"  in unit or unit == "d": return now - timedelta(days=num)
                    elif "สัปดาห์" in unit or "week" in unit or unit == "w": return now - timedelta(weeks=num)
                    elif "เดือน"   in unit or "month" in unit: return now - timedelta(days=num * 30)
                    elif "ปี"      in unit or "year" in unit: return now - timedelta(days=num * 365)

                result = self._parse_thai_date(text)
                if result:
                    return result

        except Exception as e:
            self.log(f"⚠️ _parse_post_timestamp_text: {e}")

        return None

    def _get_articles(self) -> list:
        """
        ดึงเฉพาะ div[role='article'] ระดับบนสุด (top-level posts)
        ไม่รวม article ที่ซ้อนอยู่ข้างใน (คอมเมนต์)
        """
        try:
            articles = self.driver.find_elements(
                By.XPATH,
                "//div[@role='article' and not(ancestor::div[@role='article'])]"
            )
            return articles
        except Exception as e:
            self.log(f"⚠️ _get_articles error: {e}")
            return []

    # ── Main scrape logic ─────────────────────────────────────────────────────

    def scrape_page(self, page_url: str, keywords: list[str], hours_back: int) -> int:
        new_posts     = 0
        page_name     = page_url.rstrip("/").split("/")[-1]
        cutoff_time   = datetime.now() - timedelta(hours=hours_back)
        MAX_CONSECUTIVE_OLD = 5
        consecutive_old     = 0
        seen_this_run: set  = set()
        stop_early = False
        scroll_rounds = 0

        try:
            self.log(f"🔍 กำลังเข้าเพจ: {page_url}")
            self.driver.get(page_url)
            time.sleep(3)

            obstacle = self._detect_obstacle()
            if obstacle:
                self._handle_obstacle(obstacle, page_url)
                if self._stop_event.is_set():
                    return 0

            scroll_rounds     = 0
            MAX_SCROLL_ROUNDS = 30
            last_article_count = 0
            no_growth_rounds   = 0
            MAX_NO_GROWTH      = 4
            rounds_without_new_urls = 0
            MAX_NO_NEW_URL_ROUNDS   = 3

            while not self._stop_event.is_set() and not stop_early and scroll_rounds < MAX_SCROLL_ROUNDS:
                self._slow_scroll(scrolls=4, pause=2.0)
                scroll_rounds += 1
                articles = self._get_articles()

                if not articles:
                    self.log(f"⚠️ ไม่พบ article elements บนเพจ {page_name}")
                    break

                current_count = len(articles)
                if current_count > last_article_count:
                    no_growth_rounds   = 0
                    last_article_count = current_count
                    self.log(f"📜 [{page_name}] Scroll {scroll_rounds} | โหลด article รวม: {current_count}")
                else:
                    no_growth_rounds += 1
                    self.log(
                        f"📜 [{page_name}] Scroll {scroll_rounds} | "
                        f"ไม่มีเนื้อหาใหม่ ({no_growth_rounds}/{MAX_NO_GROWTH})"
                    )
                    if no_growth_rounds >= MAX_NO_GROWTH:
                        self.log(f"📄 [{page_name}] หน้าไม่โหลดเพิ่มแล้ว — จบการสแกน")
                        break

                # คลิก "ดูเพิ่มเติม" / "See More" ทุกโพสต์
                try:
                    self.driver.execute_script("""
                        const SEE_MORE = ['ดูเพิ่มเติม', 'see more', 'see More', 'See More', 'See more'];
                        document.querySelectorAll("div[role='article']").forEach(art => {
                            // ข้าม article ที่ซ้อนอยู่ (คอมเมนต์)
                            if (art.parentElement && art.parentElement.closest("div[role='article']")) return;
                            art.querySelectorAll(
                                'div[role="button"], span[role="button"], ' +
                                'div[class*="see_more"], div[class*="truncate"]'
                            ).forEach(btn => {
                                const t = (btn.innerText || btn.textContent || '').trim();
                                if (SEE_MORE.some(sm => t === sm || t.startsWith(sm))) {
                                    try { btn.click(); } catch(e) {}
                                }
                            });
                        });
                    """)
                    time.sleep(0.6)
                except Exception as e:
                    self.log(f"⚠️ คลิก 'ดูเพิ่มเติม' ไม่สำเร็จ: {e}")

                # ─────────────────────────────────────────────────────────────
                # ดึงข้อมูลโพสต์ด้วย JS
                # หลักการ:
                #   1. เลือกเฉพาะ top-level article (ไม่ดึงคอมเมนต์)
                #   2. ดึง postText จากหลาย selector รองรับโพสต์สั้น-ยาว
                #   3. ส่ง allText กลับมาด้วยเพื่อใช้ตรวจ keyword ของโพสต์สั้น
                # ─────────────────────────────────────────────────────────────
                try:
                    article_data: list = self.driver.execute_script("""
                        const pn = arguments[0].toLowerCase();
                        const POST_PATTERNS  = ['/posts/', 'story_fbid', '/permalink/', 'fbid=', '/share/p/', '/share/'];
                        const VIDEO_PATTERNS = ['/videos/', '/reel/', '/watch/', '?v=', '%3Fv%3D'];
                        const ALL_PATTERNS   = [...POST_PATTERNS, ...VIDEO_PATTERNS];

                        // ─── เลือกเฉพาะ article ที่เป็นโพสต์จริง ไม่เอา comment ───
                        // Facebook render comment แต่ละอันเป็น div[role='article'] เหมือนกัน
                        // แต่ comment article จะ:
                        //   1. ไม่มี link ที่ match POST_PATTERNS/VIDEO_PATTERNS เลย
                        //   2. มี ancestor ที่เป็น div[role='article'] อยู่ (nested)
                        //   3. อยู่ใน ul/li structure (comment list)
                        const allArts = Array.from(document.querySelectorAll("div[role='article']"));

                        const topArts = allArts.filter(a => {
                            // ตัดออกถ้าอยู่ใน nested article (comment)
                            if (a.parentElement && a.parentElement.closest("div[role='article']"))
                                return false;
                            // ตัดออกถ้าอยู่ใน ul/li (comment list)
                            if (a.closest('ul') || a.closest('li'))
                                return false;
                            // ต้องมี anchor ที่ match post/video pattern อย่างน้อย 1 อัน
                            const anchors = Array.from(a.querySelectorAll('a[href]'));
                            const hasPostLink = anchors.some(anchor => {
                                const h = anchor.href || '';
                                return ALL_PATTERNS.some(p => h.includes(p));
                            });
                            return hasPostLink;
                        });

                        return topArts.map(art => {
                            let postUrl = '';
                            const anchors = Array.from(art.querySelectorAll('a[href]'));

                            // Priority 1: post URLs
                            for (const a of anchors) {
                                const h = a.href || '';
                                if (POST_PATTERNS.some(p => h.includes(p))) { postUrl = h; break; }
                            }
                            // Priority 2: video/reel/watch URLs
                            if (!postUrl) {
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    if (VIDEO_PATTERNS.some(p => h.includes(p))) { postUrl = h; break; }
                                }
                            }
                            // Priority 3: ลิ้งที่มีชื่อเพจ (fallback)
                            if (!postUrl) {
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    if (h.length > 40 && h.toLowerCase().includes(pn)
                                        && !h.includes('/photos/') && !h.endsWith('/' + pn)
                                        && !h.endsWith('/' + pn + '/')) {
                                        postUrl = h; break;
                                    }
                                }
                            }

                            // ─── Helper: clone art แล้วลบ nested articles/comments ออก ───
                            // ใช้ clone เพื่อให้ innerText ไม่มี comment text ปนมา
                            const nestedArts = Array.from(art.querySelectorAll("div[role='article']"));

                            const isInComment = el => {
                                for (const na of nestedArts) {
                                    if (na.contains(el)) return true;
                                }
                                return false;
                            };

                            // clone แล้วลบ nested articles + comment/reaction UI ออก
                            const artClone = art.cloneNode(true);
                            for (const na of artClone.querySelectorAll("div[role='article']")) {
                                na.remove();
                            }
                            for (const el of artClone.querySelectorAll(
                                '[aria-label*="omment"], [aria-label*="eaction"]'
                            )) { el.remove(); }

                            let postText = '';

                            // Priority 1: selector เฉพาะของ Facebook สำหรับข้อความโพสต์
                            const msgSelectors = [
                                '[data-ad-comet-preview="message"]',
                                '[data-testid="post_message"]',
                                '[data-ad-preview="message"]',
                            ];
                            for (const selector of msgSelectors) {
                                const el = artClone.querySelector(selector);
                                if (el) {
                                    const text = (el.innerText || '').trim();
                                    if (text.length > 0) { postText = text; break; }
                                }
                            }

                            // Priority 2: div/span[dir="auto"] จาก clone (ไม่มี nested articles แล้ว)
                            if (!postText) {
                                const textEls = artClone.querySelectorAll('div[dir="auto"], span[dir="auto"]');
                                const seen = new Set();
                                const lines = [];
                                for (const el of textEls) {
                                    const t = (el.innerText || '').trim();
                                    if (t.length > 0 && !seen.has(t)) {
                                        seen.add(t);
                                        lines.push(t);
                                    }
                                }
                                postText = lines.join('\\n').trim();
                            }

                            // Priority 3: innerText ของ clone (fallback สุดท้าย)
                            if (!postText) {
                                postText = (artClone.innerText || '').trim();
                            }

                            // ─── allText: ดึงจาก original art แต่กรอง nested articles ───
                            const allTextLines = [];
                            const allSeen = new Set();
                            for (const el of art.querySelectorAll('div[dir="auto"], span[dir="auto"]')) {
                                if (isInComment(el)) continue;
                                const t = (el.innerText || '').trim();
                                if (t.length > 0 && !allSeen.has(t)) {
                                    allSeen.add(t);
                                    allTextLines.push(t);
                                }
                            }
                            const allText = allTextLines.join('\\n');

                            // ─── ดึงรูปภาพ ───
                            let imageUrl = '';
                            for (const img of art.querySelectorAll('img[src*="scontent"]')) {
                                if (isInComment(img)) continue;
                                const src = img.src || '';
                                if (!src || src.includes('emoji')) continue;
                                const w = parseInt(img.getAttribute('width') || '0');
                                if (w && w <= 100) continue;
                                imageUrl = src; break;
                            }

                            // ─── Timestamp ───
                            const rawText = (art.innerText || '').split('\\n').slice(0, 10).join('\\n');
                            let utime = 0;
                            const abbrEl = art.querySelector('abbr[data-utime]');
                            if (abbrEl) {
                                utime = parseInt(abbrEl.getAttribute('data-utime') || '0');
                            }
                            let timeLabel = '';
                            if (!utime) {
                                const timeLinks = art.querySelectorAll('a[role="link"] > span, a[href*="/posts/"] > span');
                                for (const sp of timeLinks) {
                                    if (isInComment(sp)) continue;
                                    const lbl = sp.getAttribute('aria-label') || sp.title || '';
                                    if (lbl && /\\d/.test(lbl)) { timeLabel = lbl; break; }
                                }
                            }

                            return { postUrl, postText, imageUrl, rawText, allText, utime, timeLabel };
                        });
                    """, page_name)
                except Exception as e:
                    self.log(f"⚠️ ดึง article data ล้มเหลว: {type(e).__name__} — ข้ามรอบนี้")
                    article_data = []

                new_in_this_round = False

                for data in article_data:
                    if self._stop_event.is_set() or stop_early:
                        break
                    self._resume_event.wait()

                    try:
                        post_url = data.get("postUrl", "")

                        # โพสต์ที่ไม่มี URL (text-only / shared text)
                        # สร้าง synthetic URL จาก hash ของ text เพื่อใช้เป็น ID
                        _raw_text_for_id = (data.get("postText") or data.get("allText") or data.get("rawText") or "").strip()
                        if not post_url:
                            if not _raw_text_for_id:
                                continue  # ไม่มีทั้ง URL และ text — ข้ามได้เลย
                            _text_hash = hashlib.md5(_raw_text_for_id.encode("utf-8")).hexdigest()[:16]
                            post_url = f"text_post://{page_name}/{_text_hash}"

                        post_url_clean = post_url.split("?")[0].rstrip("/")

                        if post_url_clean in seen_this_run:
                            continue
                        seen_this_run.add(post_url_clean)
                        new_in_this_round = True

                        post_id = self._extract_post_id(post_url_clean)
                        if not post_id:
                            # fallback: ใช้ hash ของ URL เป็น post_id
                            post_id = hashlib.md5(post_url_clean.encode("utf-8")).hexdigest()[:16]

                        if self.db.is_seen(post_id) or self.db.is_seen_by_url(post_url_clean):
                            continue

                        # ตรวจเวลาโพสต์
                        post_time = self._parse_post_timestamp_text(
                            data.get("rawText", ""),
                            utime=int(data.get("utime") or 0),
                            time_label=data.get("timeLabel", ""),
                        )
                        if post_time is not None:
                            if post_time < cutoff_time:
                                consecutive_old += 1
                                self.log(
                                    f"⏩ ข้ามโพสต์เก่า ({consecutive_old}/{MAX_CONSECUTIVE_OLD}) "
                                    f"| พบเวลา: {post_time.strftime('%d/%m/%Y %H:%M')}"
                                )
                                if consecutive_old >= MAX_CONSECUTIVE_OLD:
                                    self.log(
                                        f"🏁 เจอโพสต์เก่าเลยกำหนด ติดต่อกัน {MAX_CONSECUTIVE_OLD} "
                                        f"รายการ — หยุดสแกนเพจนี้"
                                    )
                                    stop_early = True
                                    break
                                continue
                            else:
                                consecutive_old = 0
                                self.log(f"✅ โพสต์ใหม่ | เวลา: {post_time.strftime('%d/%m/%Y %H:%M')}")
                        else:
                            self.log("⏩ ข้ามโพสต์ (อ่านเวลาไม่ออก — ป้องกันโพสต์เก่าหลุด)")
                            continue

                        # ─── รวบรวม text สำหรับตรวจ keyword ───────────────
                        post_text  = data.get("postText", "").strip()
                        all_text   = data.get("allText", "").strip()
                        raw_text   = data.get("rawText", "").strip()

                        # ใช้ allText/rawText เป็น fallback สำหรับโพสต์สั้น/โพสต์รูป
                        if not post_text:
                            post_text = all_text or raw_text

                        image_url = data.get("imageUrl") or None

                        # ─── ตรวจ keyword ───────────────────────────────────
                        # ตรวจจาก postText + allText + rawText
                        # รองรับโพสต์ทุกขนาด รวมโพสต์สั้นหรือโพสต์รูป
                        found_keywords = []
                        if keywords:
                            texts_to_check = []
                            for src in (post_text, all_text, raw_text):
                                lowered = src.lower() if src else ""
                                if lowered and lowered not in texts_to_check:
                                    texts_to_check.append(lowered)

                            for kw in keywords:
                                kw_lower = kw.lower().strip()
                                for text in texts_to_check:
                                    if kw_lower in text:
                                        if kw not in found_keywords:
                                            found_keywords.append(kw)
                                        break

                            if not found_keywords:
                                continue

                        # ตัดข้อความเวลาออกจากต้นโพสต์
                        if post_text:
                            lines = post_text.split("\n")
                            for i, line in enumerate(lines):
                                trimmed = line.strip()
                                if trimmed and len(trimmed) > 2:
                                    lower = trimmed.lower()
                                    if not ("เมื่อ" in lower or "เพิ่ง" in lower or "yesterday" in lower or
                                            "just now" in lower or re.match(r"^\d+.*[นาทีชั่วโมงวันสัปดาห์เดือนปี]", lower) or
                                            re.match(r"^\d+.*min|hr|day|week|month|year", lower)):
                                        post_text = "\n".join(lines[i:]).strip()
                                        break

                        self.log(f"✅ พบ keyword: {found_keywords} | {post_url_clean[:70]}...")

                        # ─── AI วิเคราะห์ ────────────────────────────────────
                        ai_result = None
                        if self.ai_analyzer and post_text:
                            ai_result = self.ai_analyzer.analyze(post_text)
                            if ai_result and ai_result.get("is_target") and ai_result.get("score", 0) >= 6:
                                self.log(f"🎯 [AI PASS] คะแนน: {ai_result.get('score')}/10")
                                if self.sheets_manager:
                                    self.sheets_manager.upload_news(
                                        page_name=page_name,
                                        post_url=post_url_clean,
                                        post_text=post_text,
                                        persons=ai_result.get("persons", []),
                                        score=ai_result.get("score", 0),
                                        reason=ai_result.get("reason", "")
                                    )
                                    self.log("💾 บันทึกลง Google Sheets เรียบร้อย")

                        # ─── ส่ง Notification ─────────────────────────────────
                        self.discord.send_post(page_name, page_url, post_url_clean, post_text,
                                               found_keywords, image_url, ai_result=ai_result)
                        self.tg.send_post(page_name, page_url, post_url_clean, post_text,
                                          found_keywords, image_url)

                        self.db.mark_seen(post_id, page_url, post_url_clean)
                        new_posts += 1
                        time.sleep(1)

                    except StaleElementReferenceException:
                        continue
                    except OSError as e:
                        if "cacert.pem" in str(e) or "certificate" in str(e).lower():
                            try:
                                import certifi as _certifi
                                real = _certifi.where()
                                if os.path.isfile(real):
                                    os.environ["SSL_CERT_FILE"]      = real
                                    os.environ["REQUESTS_CA_BUNDLE"] = real
                            except Exception:
                                pass
                        else:
                            self.log(f"⚠️ ข้ามโพสต์ที่อ่านไม่ได้: OSError: {e}")
                        continue
                    except Exception as e:
                        self.log(f"⚠️ ข้ามโพสต์ที่อ่านไม่ได้: {type(e).__name__}: {e}")
                        continue

                # ตรวจ URL ใหม่ต่อรอบ
                if new_in_this_round:
                    rounds_without_new_urls = 0
                else:
                    rounds_without_new_urls += 1
                    self.log(
                        f"⚠️ [{page_name}] ไม่พบ URL ใหม่รอบนี้ "
                        f"({rounds_without_new_urls}/{MAX_NO_NEW_URL_ROUNDS})"
                    )
                    if rounds_without_new_urls >= MAX_NO_NEW_URL_ROUNDS:
                        self.log(
                            f"📄 ไม่พบ URL ใหม่ติดต่อกัน {MAX_NO_NEW_URL_ROUNDS} รอบ "
                            f"บนเพจ {page_name} — จบการสแกน"
                        )
                        break

        except InvalidSessionIdException as e:
            self.log(f"❌ Browser session หมดอายุระหว่างสแกน {page_name} — จะเปิด Browser ใหม่รอบหน้า")
            raise
        except WebDriverException as e:
            self.log(f"❌ WebDriver Error ที่เพจ {page_name}: {e}")
        except Exception as e:
            self.log(f"❌ Error scraping {page_name}: {e}")

        self.log(f"📊 สแกนเพจ {page_name} เสร็จ | Scroll {scroll_rounds} รอบ | โพสต์ใหม่: {new_posts}")
        return new_posts

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(
        self,
        email: str,
        password: str,
        page_urls: list[str],
        keywords: list[str],
        hours_back: int,
        loop_minutes: int,
    ):
        MAX_CONSECUTIVE_FAILURES = 5
        RETRY_WAIT_SECONDS       = 300

        _started_successfully = False
        _session_start = time.time()
        _total_posts_all_cycles = 0
        last_cleanup_date = None

        try:
            self.discord.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            self.tg.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            _started_successfully = True

            while not self._stop_event.is_set():

                now = datetime.now()
                if now.hour >= 9 and last_cleanup_date != now.date():
                    self.log("🧹 ถึงเวลา 09:00 น. | เริ่มล้างข้อมูล Database เก่า...")
                    if self.db.cleanup_old_data():
                        self.log("✅ ลบข้อมูลเก่าสำเร็จและคืนพื้นที่แล้ว")
                    last_cleanup_date = now.date()

                self._cycle_count += 1
                cycle_start = time.time()
                self.log(f"\n{'='*50}")
                self.log(f"🔄 รอบที่ {self._cycle_count} | {now.strftime('%d/%m/%Y %H:%M:%S')}")

                cycle_ok = False
                try:
                    self._start_browser()

                    if not self._load_cookies():
                        self.log("🔑 ไม่มี Session เดิม — เริ่มล็อกอินใหม่")
                        if not self.login(email, password):
                            raise RuntimeError("Login ล้มเหลว — cookies หมดอายุหรือ password ผิด")

                    time.sleep(1)
                    self.hide_browser()

                    total_new = 0
                    for url in page_urls:
                        if self._stop_event.is_set():
                            break
                        url = url.strip()
                        if not url:
                            continue
                        count = self.scrape_page(url, keywords, hours_back)
                        total_new += count
                        self.log(f"📊 เพจ {url.split('/')[-1]}: พบ {count} โพสต์ใหม่")
                        if not self._stop_event.is_set():
                            time.sleep(random.uniform(2.0, 5.0))

                    if self._stop_event.is_set():
                        break

                    _total_posts_all_cycles += total_new
                    duration = time.time() - cycle_start
                    self.log(f"✅ รอบสแกนเสร็จ | พบโพสต์ใหม่รวม: {total_new}")
                    self.discord.send_cycle_complete(duration, loop_minutes, total_new, len(page_urls))
                    self.tg.send_cycle_complete(duration, loop_minutes, total_new, len(page_urls))

                    self._consecutive_failures = 0
                    cycle_ok = True

                except Exception as e:
                    self._consecutive_failures += 1
                    self.log(
                        f"❌ รอบ {self._cycle_count} ล้มเหลว "
                        f"({self._consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): "
                        f"{type(e).__name__}: {e}"
                    )
                    if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        self.log(f"🔴 ล้มเหลวติดต่อกัน {MAX_CONSECUTIVE_FAILURES} รอบ — หยุดทำงาน")
                        self.discord.send_obstacle(f"FATAL: ล้มเหลว {MAX_CONSECUTIVE_FAILURES} รอบติด", "")
                        self.tg.send_obstacle(f"FATAL: ล้มเหลว {MAX_CONSECUTIVE_FAILURES} รอบติด", "")
                        break

                finally:
                    self.log("🛑 ปิด Browser ชั่วคราว...")
                    self._safe_quit_driver()

                if self._stop_event.is_set():
                    break

                if cycle_ok:
                    wait_secs = loop_minutes * 60
                    self.log(f"⏳ รอ {loop_minutes} นาทีก่อนรอบถัดไป...")
                else:
                    wait_secs = RETRY_WAIT_SECONDS
                    self.log(f"🔄 รอ {RETRY_WAIT_SECONDS // 60} นาทีก่อน retry...")

                self._sleep_interruptible(wait_secs)

        except OSError as e:
            if "cacert.pem" in str(e) or "certificate" in str(e).lower():
                self.log("⚠️ SSL Certificate Error (PyInstaller temp path) — รีสตาร์ทโปรแกรมหนึ่งครั้งเพื่อแก้ไข")
            else:
                self.log(f"❌ Fatal OSError ใน Scraper Thread: {e}")
        except Exception as e:
            self.log(f"❌ Fatal Error ใน Scraper Thread: {type(e).__name__}: {e}")
        finally:
            if _started_successfully:
                total_runtime = time.time() - _session_start
                self.discord.send_stopped(total_runtime, _total_posts_all_cycles)
                self.tg.send_stopped(total_runtime, _total_posts_all_cycles)

            self._safe_quit_driver()
            self.log("🏁 Scraper หยุดทำงานสมบูรณ์")

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()
