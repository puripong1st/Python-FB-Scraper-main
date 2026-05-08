"""
scraper.py
━━━━━━━━━━
FacebookScraper — เปิด Browser, Login, สแกนเพจ, ส่งแจ้งเตือน
ใช้ undetected-chromedriver เพื่อหลีกเลี่ยง bot detection

ฟีเจอร์ทั้งหมด:
  ✅ รองรับ FB ทุก account ทุก zone (ไทย / อินเดีย / Global)
     - ตรวจจับ posts ด้วย data-pagelet="FeedUnit_N" (ไม่ดึง comment มาปน)
     - URL format ใหม่: pfbid, story.php, photo.php, web.facebook.com
  ✅ Cookie expired: แจ้งเตือน Discord+Telegram + หยุดรอ Resume
  ✅ ปุ่มซ่อน Browser sync กับ state จริงผ่าน callback
  ✅ Timestamp parser รองรับทั้งไทยและ English
  ✅ Obstacle detection: Checkpoint / 2FA / CAPTCHA / Identity Verify
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
    SELECTORS = {
        "email_input": "//input[@id='email' or @name='email']",
        "pass_input":  "//input[@id='pass' or @name='pass']",
        "login_btn":   "//button[@name='login' or @data-testid='royal_login_button']",
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

        self._driver      = None
        self._driver_lock = threading.RLock()

        self._stop_event   = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._is_paused = False

        self._browser_hidden = False
        self._consecutive_failures = 0
        self._cycle_count = 0
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

    # ── Chrome PID tracking ───────────────────────────────────────────────────

    def _collect_chrome_pids(self) -> set:
        try:
            import subprocess, json as _json

            def _all_pids():
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'}"
                     " | Select-Object ProcessId | ConvertTo-Json"],
                    capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    return set()
                p = _json.loads(r.stdout)
                if isinstance(p, dict): p = [p]
                return {int(x["ProcessId"]) for x in p if x.get("ProcessId")}

            before: set = getattr(self, "_chrome_pids_before", set())
            after = _all_pids()
            new_pids = after - before
            if new_pids:
                self._scraper_chrome_pids = new_pids
                self.log(f"🔍 Chrome PIDs: {new_pids}")
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
                    procs = _json.loads(r2.stdout)
                    if isinstance(procs, dict): procs = [procs]
                    pids = {root}
                    q = [root]
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

    def _find_browser_hwnds(self) -> list:
        try:
            import ctypes, ctypes.wintypes
            pids = self._scraper_chrome_pids
            found = []
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

            def _cb(hwnd, _):
                cls = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(hwnd, cls, 64)
                if cls.value not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0"):
                    return True
                win_pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
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

    # ── Hide / Show browser ───────────────────────────────────────────────────

    def hide_browser(self):
        try:
            import ctypes
            hwnds = self._find_browser_hwnds()
            hidden = 0
            for hwnd in hwnds:
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
                    hidden += 1
            if hidden:
                self.log(f"👻 ซ่อน Browser แล้ว ({hidden} หน้าต่าง)")
            self._browser_hidden = True
        except Exception as e:
            self.log(f"⚠️ hide_browser: {e}")
        if self._on_browser_hidden:
            try: self._on_browser_hidden()
            except Exception: pass

    def show_browser(self):
        try:
            import ctypes
            hwnds = self._find_browser_hwnds()
            shown = 0
            for hwnd in hwnds:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                shown += 1
            if shown:
                self.log(f"👁️ แสดง Browser แล้ว ({shown} หน้าต่าง)")
            self._browser_hidden = False
        except Exception as e:
            self.log(f"⚠️ show_browser: {e}")
        if self._on_browser_shown:
            try: self._on_browser_shown()
            except Exception: pass

    def _set_chrome_icon(self):
        try:
            import ctypes
            if getattr(sys, "frozen", False):
                icon_path = os.path.join(sys._MEIPASS, "app_icon.ico")
            else:
                icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
            if not os.path.exists(icon_path):
                return
            IMAGE_ICON = 1; LR_LOADFROMFILE = 0x0010; LR_DEFAULTSIZE = 0x0040
            WM_SETICON = 0x0080; GCL_HICON = -14; GCL_HICONSM = -34
            hbig  = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            hsm   = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            htask = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            hwnds = []
            for _ in range(30):
                hwnds = self._find_browser_hwnds()
                if hwnds: break
                time.sleep(0.5)
            for hwnd in hwnds:
                if hbig:  ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hbig)
                if hsm:   ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hsm)
                if htask:
                    ctypes.windll.user32.SetClassLongPtrW(hwnd, GCL_HICON,   htask)
                    ctypes.windll.user32.SetClassLongPtrW(hwnd, GCL_HICONSM, htask)
            if hwnds:
                self.log(f"🎨 เปลี่ยนไอคอน Browser ({len(hwnds)} หน้าต่าง)")
        except Exception as e:
            self.log(f"⚠️ _set_chrome_icon: {e}")

    def _safe_quit_driver(self):
        drv = self.driver
        if drv is None: return
        try: drv.quit()
        except Exception: pass
        finally:
            self.driver = None
            self._browser_hidden = False
            self._scraper_chrome_pids = set()

    def _sleep_interruptible(self, seconds: float, step: float = 5.0):
        elapsed = 0.0
        while elapsed < seconds and not self._stop_event.is_set():
            chunk = min(step, seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    def _start_browser(self):
        import winreg, shutil, subprocess

        try:
            os.system("taskkill /f /im chromedriver.exe /t >nul 2>&1")
            appdata = os.getenv("APPDATA")
            if appdata:
                uc_dir = os.path.join(appdata, "undetected_chromedriver")
                if os.path.exists(uc_dir):
                    shutil.rmtree(uc_dir, ignore_errors=True)
                    self.log("🧹 ลบแคช Driver เก่าแล้ว")
        except Exception as e:
            self.log(f"⚠️ ล้างแคช: {e}")

        def _make_options():
            opts = uc.ChromeOptions()
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--lang=th-TH,th;q=0.9,en-US;q=0.8")
            opts.add_argument("--window-size=1280,900")
            opts.page_load_strategy = "eager"
            return opts

        chrome_version = None
        ps_cmds = [
            "(Get-Item (Get-Command chrome).Source).VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r"(Get-Item 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe').VersionInfo.ProductVersion",
            r'(Get-Item "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe").VersionInfo.ProductVersion',
        ]
        for cmd in ps_cmds:
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                                   capture_output=True, text=True, timeout=8)
                v = r.stdout.strip()
                if v and v[0].isdigit():
                    chrome_version = int(v.split(".")[0])
                    self.log(f"🔎 Chrome version (EXE): {chrome_version}")
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
                self.log(f"🔄 เปิด Browser รอบ {attempt}/{len(strategies)} {kwargs or '(auto)'}")
                self._safe_quit_driver()
                time.sleep(1)

                # snapshot PIDs ก่อน launch
                try:
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'chrome.exe'}"
                         " | Select-Object ProcessId | ConvertTo-Json"],
                        capture_output=True, text=True, timeout=10)
                    if r.stdout.strip():
                        import json as _j
                        pp = _j.loads(r.stdout)
                        if isinstance(pp, dict): pp = [pp]
                        self._chrome_pids_before = {int(x["ProcessId"]) for x in pp if x.get("ProcessId")}
                    else:
                        self._chrome_pids_before = set()
                except Exception:
                    self._chrome_pids_before = set()

                self.driver = uc.Chrome(options=_make_options(), use_subprocess=True, **kwargs)
                self.driver.set_page_load_timeout(60)
                self.log("🌐 เปิด Browser สำเร็จ")
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
                if attempt < len(strategies):
                    time.sleep(3)

        raise RuntimeError(f"❌ เปิด Browser ไม่สำเร็จ: {last_err}")

    # ── Cookies ───────────────────────────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        """
        ตรวจว่า login สำเร็จหรือไม่ โดยดูจาก DOM จริง ไม่ใช่แค่ URL

        ปัญหาเดิม: ตรวจแค่ว่า URL มีคำว่า 'login' ไหม
        → Facebook ที่ cookie หมดอายุมักจะ redirect กลับ facebook.com/
          (URL ไม่มีคำว่า login) แต่จริงๆ ยังไม่ได้ login
        → ทำให้ _load_cookies() คืน True ผิดพลาด → ไม่แจ้งเตือน cookie หมดอายุ

        วิธีใหม่: ตรวจ DOM 4 ชั้น
          1. URL มี /login หรือ /checkpoint = ยังไม่ login แน่นอน
          2. มี login form อยู่ใน DOM = ยังไม่ login
          3. มี navbar ของ FB ที่ login แล้ว = login สำเร็จ
          4. ไม่มีปุ่ม Login ใน DOM = login สำเร็จ (fallback)
        """
        try:
            url = self.driver.current_url.lower()
            if any(x in url for x in ("login", "checkpoint", "two_step", "captcha")):
                return False

            result = self.driver.execute_script("""
                // ── ยังไม่ login: มี login form ──────────────────────────────
                const hasLoginForm = !!(
                    document.querySelector('form#login_form') ||
                    document.querySelector('input#email[type="text"]') ||
                    document.querySelector('input[name="email"][autocomplete="username"]') ||
                    document.querySelector('[data-testid="royal_login_button"]') ||
                    document.querySelector('button[name="login"]')
                );
                if (hasLoginForm) return false;

                // ── login แล้ว: มี element เฉพาะ logged-in user ────────────
                const loggedInSignals = [
                    '[aria-label="Facebook"][role="navigation"]',
                    '[data-testid="royal_blue_bar"]',
                    'div[role="banner"] a[href*="/profile.php"]',
                    'div[role="banner"] a[href*="facebook.com/me"]',
                    '[aria-label="บัญชีของคุณ"]',
                    '[aria-label="Your account"]',
                    '[aria-label="Account"]',
                    'a[data-testid="nav-profile"]',
                ];
                const loggedIn = loggedInSignals.some(sel => !!document.querySelector(sel));
                if (loggedIn) return true;

                // ── fallback: ถ้าไม่มี login form และไม่มี signal → assume logged in
                // (กรณี FB เปลี่ยน DOM โครงสร้าง)
                return true;
            """)
            return bool(result)
        except Exception as e:
            self.log(f"⚠️ _is_logged_in check error: {e}")
            # ถ้าตรวจไม่ได้ ให้ดู URL เป็น fallback
            url = self.driver.current_url.lower()
            return "login" not in url and "facebook.com" in url

    def _save_cookies(self):
        drv = self.driver
        if not drv: return
        try:
            cookies = drv.get_cookies()
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self.log("🍪 บันทึก Cookies แล้ว")
        except Exception as e:
            self.log(f"⚠️ บันทึก Cookies ไม่สำเร็จ: {e}")
            return
        if self._on_cookies_saved:
            try: self._on_cookies_saved()
            except Exception as e: self.log(f"⚠️ on_cookies_saved: {e}")

    def _load_cookies(self) -> bool:
        """
        โหลด cookie และตรวจว่า session ยังใช้งานได้จริงไหม
        ใช้ _is_logged_in() แทนการตรวจ URL เพื่อความแม่นยำ
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
            else:
                self.log("⚠️ Cookie ไม่ valid — session หมดอายุหรือถูก revoke")
                return False
        except Exception as e:
            self.log(f"⚠️ โหลด Cookies ไม่สำเร็จ: {e}")
        return False

    # ── Login ─────────────────────────────────────────────────────────────────

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
        for by, sel in strategies:
            try:
                btn = WebDriverWait(self.driver, 4).until(EC.element_to_be_clickable((by, sel)))
                self.driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.3)
                btn.click()
                self.log("🖱️ คลิกปุ่ม Login สำเร็จ")
                return True
            except Exception: continue
        try:
            self.driver.find_element(By.XPATH, self.SELECTORS["pass_input"]).send_keys(Keys.RETURN)
            self.log("⌨️ กด Enter (fallback)")
            return True
        except Exception as e:
            self.log(f"⚠️ fallback Enter ล้มเหลว: {e}")
        return False

    def login(self, email: str, password: str) -> bool:
        try:
            self.driver.get(f"{self.HOME_URL}/login")
            wait = WebDriverWait(self.driver, 20)

            self.log("📧 กรอก Email...")
            email_field = wait.until(EC.element_to_be_clickable((By.XPATH, self.SELECTORS["email_input"])))
            self._type_human(email_field, email, delay=0.06)
            time.sleep(0.4)

            self.log("🔑 กรอก Password...")
            pass_field = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, self.SELECTORS["pass_input"])))
            self._type_human(pass_field, password, delay=0.05)
            time.sleep(0.6)

            if not self._click_login_button():
                self.log("⚠️ หาปุ่ม Login ไม่เจอ — กรุณากด Login ใน Browser แล้วกด Resume")
                self._handle_obstacle("Login Button Not Found", f"{self.HOME_URL}/login")
                if self._stop_event.is_set(): return False

            self.log("⏳ รอหน้าเว็บโหลด...")
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

    # ── Obstacle detection ────────────────────────────────────────────────────

    def _detect_obstacle(self) -> str | None:
        url   = self.driver.current_url.lower()
        title = self.driver.title.lower()
        if "checkpoint"            in url or "checkpoint"  in title: return "Checkpoint"
        if "two_step_verification" in url or "two_factor"  in url:   return "2FA"
        if "captcha"               in url or "captcha"     in title: return "CAPTCHA"
        if "login_attempt"         in url:                           return "Login Attempt Blocked"
        if "suspended"             in url or "disabled"    in url:   return "Account Suspended/Disabled"

        _ID_URL  = ("identity", "identity_verification", "id_verification", "confirm_identity", "verify_identity")
        _ID_TEXT = ("confirm your identity", "ยืนยันตัวตน")
        try:
            if any(s in url for s in _ID_URL):
                return "Identity Verification"
            found_form = self.driver.execute_script("""
                return Array.from(document.querySelectorAll('form')).some(f => {
                    const a = (f.action||'').toLowerCase();
                    return a.includes('identity') || a.includes('checkpoint') || a.includes('confirm');
                });
            """)
            found_h = self.driver.execute_script("""
                const signals = arguments[0];
                for (const h of document.querySelectorAll('h1,h2,h3')) {
                    const t = (h.innerText||'').toLowerCase();
                    const s = window.getComputedStyle(h);
                    if (s.display==='none'||s.visibility==='hidden') continue;
                    if (signals.some(x=>t.includes(x))) return true;
                }
                return false;
            """, list(_ID_TEXT))
            if found_form or found_h:
                self.log("⏳ พบสัญญาณ Identity Verification — ตรวจซ้ำ 2 วิ...")
                time.sleep(2)
                url2 = self.driver.current_url.lower()
                if url2 != url: return None
                confirmed = self.driver.execute_script("""
                    const signals = arguments[0];
                    for (const h of document.querySelectorAll('h1,h2,h3')) {
                        const t = (h.innerText||'').toLowerCase();
                        const s = window.getComputedStyle(h);
                        if (s.display==='none'||s.visibility==='hidden') continue;
                        if (signals.some(x=>t.includes(x))) return true;
                    }
                    return Array.from(document.querySelectorAll('form')).some(f => {
                        const a = (f.action||'').toLowerCase();
                        return a.includes('identity')||a.includes('checkpoint')||a.includes('confirm');
                    });
                """, list(_ID_TEXT))
                if confirmed: return "Identity Verification"
                self.log("✅ false positive — ไม่ใช่ Identity Verification")
        except Exception as e:
            self.log(f"⚠️ _detect_obstacle: {e}")
        return None

    def _handle_obstacle(self, obstacle_type: str, page_url: str = ""):
        self.log(f"🚨 ติด {obstacle_type} — หยุดรอ Resume")
        self.show_browser()
        self.discord.send_obstacle(obstacle_type, page_url)
        self.tg.send_obstacle(obstacle_type, page_url)
        self._resume_event.clear()
        self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume แล้ว — กลับมาทำงานต่อ")
        self.hide_browser()

    def _handle_cookie_expired(self, page_url: str = ""):
        """แจ้งเตือนและหยุดรอเมื่อ Cookie หมดอายุ — ต่างจาก obstacle ตรงข้อความ"""
        self.log("🍪 Session หมดอายุ — กรุณาล็อกอินใน Browser แล้วกด Resume")
        self.show_browser()
        self.discord.send_cookie_expired(page_url)
        self.tg.send_cookie_expired(page_url)
        self._resume_event.clear()
        self._is_paused = True
        self._resume_event.wait()
        self._is_paused = False
        self.log("▶️ Resume แล้ว — ตรวจสอบ Session...")
        self.hide_browser()

    def resume(self):
        self._resume_event.set()

    # ── Scrolling ─────────────────────────────────────────────────────────────

    def _slow_scroll(self, scrolls: int = 4, pause: float = 2.0):
        for _ in range(scrolls):
            if self._stop_event.is_set(): break
            self.driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            time.sleep(random.uniform(pause * 0.8, pause * 1.3))

    # ── Post ID ───────────────────────────────────────────────────────────────

    def _extract_post_id(self, url: str) -> str:
        """
        รองรับทุก format:
          /posts/12345           → numeric ID (ไทย)
          /posts/pfbid02Abc...   → pfbid (Indian/Global)
          story.php?story_fbid=  → query param
          story.php?id=          → id param
          /share/p/pfbidXXX      → pfbid ใน share URL
        """
        patterns = [
            r"/posts/(pfbid[A-Za-z0-9]+)",
            r"/posts/(\d+)",
            r"story_fbid=(pfbid[A-Za-z0-9]+)",
            r"story_fbid=(\d+)",
            r"/permalink/(pfbid[A-Za-z0-9]+)",
            r"/permalink/(\d+)",
            r"fbid=(pfbid[A-Za-z0-9]+)",
            r"fbid=(\d+)",
            r"/videos/(\d+)",
            r"/reel/(\d+)",
            r"[?&]v=(\d+)",
            r"/share/p/(pfbid[A-Za-z0-9]+)",
            r"/share/p/([^/?]+)",
            r"/share/(pfbid[A-Za-z0-9]+)",
            r"/share/([^/?]+)",
            r"[?&]id=(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return hashlib.md5(url.encode("utf-8")).hexdigest()

    # ── Timestamp parser ──────────────────────────────────────────────────────

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
    _EN_MONTH = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def _parse_date_text(self, text: str) -> "datetime | None":
        now = datetime.now()
        # Thai short month
        for abbr, month in self._TH_MONTH_SHORT.items():
            if abbr in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    day = int(nums[0]); year = int(nums[-1])
                    if year > 2400: year -= 543
                    elif year < 100: year += 1957
                    try: return datetime(year, month, day)
                    except ValueError: pass
        # Thai long month
        for full, month in self._TH_MONTH_LONG.items():
            if full in text:
                nums = re.findall(r"\d+", text)
                if len(nums) >= 2:
                    day = int(nums[0]); year = int(nums[-1])
                    if year > 2400: year -= 543
                    elif year < 100: year += 1957
                    try: return datetime(year, month, day)
                    except ValueError: pass
        # English month
        tl = text.lower()
        for en, month in self._EN_MONTH.items():
            if en in tl:
                nums = re.findall(r"\d+", text)
                nums_int = [int(n) for n in nums if int(n) != month]
                if len(nums_int) >= 2:
                    try:
                        day  = next(n for n in sorted(nums_int) if 1 <= n <= 31)
                        year = next(n for n in sorted(nums_int) if n > 31)
                        return datetime(year, month, day)
                    except (StopIteration, ValueError): pass
                elif len(nums_int) == 1:
                    day = nums_int[0]
                    if 1 <= day <= 31:
                        try: return datetime(now.year, month, day)
                        except ValueError: pass
        return None

    def _parse_timestamp(self, raw_text: str, utime: int = 0, time_label: str = "") -> "datetime | None":
        """รองรับ timestamp ทั้ง Thai + English + Unix timestamp"""
        now = datetime.now()

        if utime and utime > 0:
            try: return datetime.fromtimestamp(utime)
            except Exception: pass

        if time_label:
            r = self._parse_date_text(time_label)
            if r: return r
            m = re.search(r"(\w+day,\s+)?(\w+)\s+(\d{1,2}),?\s+(\d{4})", time_label, re.IGNORECASE)
            if m:
                mn = self._EN_MONTH.get(m.group(2).lower()[:3])
                if mn:
                    try: return datetime(int(m.group(4)), mn, int(m.group(3)))
                    except ValueError: pass

        try:
            for line in raw_text.split("\n")[:10]:
                text = line.strip().replace("·", "").replace(",", "").strip()
                if not text: continue
                tl = text.lower()

                if any(p in tl for p in ("just now", "เพิ่ง", "เมื่อสักครู่", "a few seconds")):
                    return now
                if "เมื่อวาน" in tl or "yesterday" in tl:
                    return now - timedelta(days=1)

                # Thai relative
                m_th = re.search(r"(\d+)\s*(นาที|ชั่วโมง|ชม\.?|วัน|สัปดาห์|เดือน|ปี)", tl)
                if m_th:
                    n = int(m_th.group(1)); u = m_th.group(2)
                    if "นาที"    in u: return now - timedelta(minutes=n)
                    if "ชม"      in u or "ชั่วโมง" in u: return now - timedelta(hours=n)
                    if "วัน"     in u: return now - timedelta(days=n)
                    if "สัปดาห์" in u: return now - timedelta(weeks=n)
                    if "เดือน"   in u: return now - timedelta(days=n * 30)
                    if "ปี"      in u: return now - timedelta(days=n * 365)

                # English relative
                m_en = re.search(
                    r"(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|[hmdswy])\b", tl)
                if m_en:
                    n = int(m_en.group(1)); u = m_en.group(2).lower().rstrip("s")
                    if u in ("minute","min","m"):  return now - timedelta(minutes=n)
                    if u in ("hour","hr","h"):     return now - timedelta(hours=n)
                    if u in ("day","d"):           return now - timedelta(days=n)
                    if u in ("week","w"):          return now - timedelta(weeks=n)
                    if u in ("month",):            return now - timedelta(days=n * 30)
                    if u in ("year","y"):          return now - timedelta(days=n * 365)

                r = self._parse_date_text(text)
                if r: return r
        except Exception as e:
            self.log(f"⚠️ _parse_timestamp: {e}")
        return None

    # ── Scrape page ───────────────────────────────────────────────────────────

    def _get_top_articles(self) -> list:
        """ดึงเฉพาะ top-level article (โพสต์) ไม่รวม comment"""
        try:
            return self.driver.find_elements(
                By.XPATH,
                "//div[@role='article' and not(ancestor::div[@role='article'])]"
            )
        except Exception:
            return []

    def scrape_page(self, page_url: str, keywords: list[str], hours_back: int) -> int:
        new_posts       = 0
        page_name       = page_url.rstrip("/").split("/")[-1]
        cutoff_time     = datetime.now() - timedelta(hours=hours_back)
        MAX_OLD         = 5
        consecutive_old = 0
        seen_this_run: set = set()
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

            MAX_SCROLLS           = 30
            last_count            = 0
            no_growth             = 0
            MAX_NO_GROWTH         = 4
            rounds_no_new_url     = 0
            MAX_NO_NEW_URL        = 3

            while not self._stop_event.is_set() and not stop_early and scroll_rounds < MAX_SCROLLS:
                self._slow_scroll(scrolls=4, pause=2.0)
                scroll_rounds += 1

                if not self._get_top_articles():
                    self.log(f"⚠️ ไม่พบ article elements บน {page_name}")
                    break

                cur_count = len(self._get_top_articles())
                if cur_count > last_count:
                    no_growth = 0; last_count = cur_count
                    self.log(f"📜 [{page_name}] Scroll {scroll_rounds} | articles: {cur_count}")
                else:
                    no_growth += 1
                    self.log(f"📜 [{page_name}] Scroll {scroll_rounds} | ไม่โหลดเพิ่ม ({no_growth}/{MAX_NO_GROWTH})")
                    if no_growth >= MAX_NO_GROWTH:
                        self.log(f"📄 [{page_name}] หน้าไม่โหลดเพิ่ม — จบ")
                        break

                # คลิก "ดูเพิ่มเติม" / "See More" รองรับทุกภาษา
                try:
                    self.driver.execute_script("""
                        const SEE_MORE = ['ดูเพิ่มเติม','see more','See More','See more',
                                          'Read more','read more','Show more','show more'];
                        document.querySelectorAll("div[role='article']").forEach(art => {
                            if (art.parentElement && art.parentElement.closest("div[role='article']")) return;
                            art.querySelectorAll(
                                'div[role="button"],span[role="button"],' +
                                'div[class*="see_more"],div[class*="truncate"]'
                            ).forEach(btn => {
                                const t = (btn.innerText||btn.textContent||'').trim();
                                if (SEE_MORE.some(s => t===s || t.startsWith(s))) {
                                    try { btn.click(); } catch(e) {}
                                }
                            });
                        });
                    """)
                    time.sleep(0.6)
                except Exception as e:
                    self.log(f"⚠️ คลิก 'ดูเพิ่มเติม': {e}")

                # ════════════════════════════════════════════════════════════
                # ดึงข้อมูลโพสต์ด้วย JS
                # ตรวจจับ top-level posts 3 วิธีเรียงจากแม่นสุด:
                #   A) data-pagelet="FeedUnit_N"  → แม่นสุด comments ผ่านไม่ได้
                #   B) div[role='feed'] scan       → fallback
                #   C) global URL-pattern scan     → last resort
                # ════════════════════════════════════════════════════════════
                try:
                    article_data: list = self.driver.execute_script("""
                        const pn = arguments[0].toLowerCase();

                        const POST_PATTERNS = [
                            '/posts/', 'story_fbid', '/permalink/', 'fbid=',
                            '/share/p/', 'pfbid', 'story.php', 'photo.php', '/photos/',
                        ];
                        const VIDEO_PATTERNS = ['/videos/','/reel/','/watch/','?v=','%3Fv%3D'];
                        const ALL_PATTERNS   = [...POST_PATTERNS, ...VIDEO_PATTERNS];

                        // ── วิธี A: data-pagelet="FeedUnit_N" (แม่นสุด) ──────────────────
                        // Facebook ใส่ label นี้บน wrapper ของโพสต์ทุกตัว
                        // ทำงานได้กับทุก account ไม่ว่าสมัครจากประเทศไหน
                        // comment ไม่มี data-pagelet="FeedUnit_*" → กรองออกอัตโนมัติ
                        let topArts = [];
                        const feedUnits = document.querySelectorAll(
                            '[data-pagelet^="FeedUnit"],' +
                            '[data-pagelet^="GroupFeedUnit"],' +
                            '[data-pagelet^="MainFeed"],' +
                            '[data-pagelet^="Story"]'
                        );
                        if (feedUnits.length > 0) {
                            feedUnits.forEach(unit => {
                                const arts = Array.from(unit.querySelectorAll("div[role='article']"));
                                const best = arts.find(a =>
                                    !a.parentElement?.closest("div[role='article']")
                                );
                                if (best && !topArts.includes(best)) topArts.push(best);
                            });
                        }

                        // ── วิธี B: div[role='feed'] (fallback) ──────────────────────────
                        if (topArts.length === 0) {
                            const feed = document.querySelector("div[role='feed']");
                            if (feed) {
                                topArts = Array.from(feed.querySelectorAll("div[role='article']"))
                                    .filter(a =>
                                        !a.parentElement?.closest("div[role='article']") &&
                                        !a.closest('ul') && !a.closest('li')
                                    );
                            }
                        }

                        // ── วิธี C: global scan + URL filter (last resort) ────────────────
                        // ไม่ใช้ [data-utime] fallback เพราะ comments ก็มี [data-utime]
                        if (topArts.length === 0) {
                            topArts = Array.from(document.querySelectorAll("div[role='article']"))
                                .filter(a => {
                                    if (a.parentElement?.closest("div[role='article']")) return false;
                                    if (a.closest('ul') || a.closest('li')) return false;
                                    return Array.from(a.querySelectorAll('a[href]')).some(anchor =>
                                        ALL_PATTERNS.some(p => (anchor.href||'').includes(p))
                                    );
                                });
                        }

                        return topArts.map(art => {
                            // ── หา post URL ──────────────────────────────────────────────
                            let postUrl = '';
                            const anchors = Array.from(art.querySelectorAll('a[href]'));
                            const POST_PRIORITY = [
                                '/posts/','story_fbid','/permalink/','fbid=',
                                'pfbid','story.php','photo.php','/share/p/',
                            ];
                            for (const pat of POST_PRIORITY) {
                                for (const a of anchors) {
                                    if ((a.href||'').includes(pat)) { postUrl = a.href; break; }
                                }
                                if (postUrl) break;
                            }
                            if (!postUrl) {
                                for (const a of anchors) {
                                    if (VIDEO_PATTERNS.some(p => (a.href||'').includes(p))) {
                                        postUrl = a.href; break;
                                    }
                                }
                            }
                            if (!postUrl) {
                                for (const a of anchors) {
                                    if ((a.href||'').includes('/share/')) { postUrl = a.href; break; }
                                }
                            }
                            if (!postUrl) {
                                for (const a of anchors) {
                                    const h = a.href || '';
                                    if (h.length > 40 && h.toLowerCase().includes(pn)
                                        && !h.endsWith('/'+pn) && !h.endsWith('/'+pn+'/')) {
                                        postUrl = h; break;
                                    }
                                }
                            }

                            // ── กรองเนื้อหา comment ออกจากโพสต์ ─────────────────────────
                            const nestedArts = Array.from(art.querySelectorAll("div[role='article']"));
                            const isInComment = el => nestedArts.some(na => na.contains(el));

                            const clone = art.cloneNode(true);
                            clone.querySelectorAll("div[role='article']").forEach(n => n.remove());
                            clone.querySelectorAll(
                                '[aria-label*="omment"],[aria-label*="eaction"]'
                            ).forEach(n => n.remove());

                            // ── ดึง post text ─────────────────────────────────────────────
                            let postText = '';
                            for (const sel of [
                                '[data-ad-comet-preview="message"]',
                                '[data-testid="post_message"]',
                                '[data-ad-preview="message"]',
                            ]) {
                                const el = clone.querySelector(sel);
                                if (el) { postText = (el.innerText||'').trim(); if (postText) break; }
                            }
                            if (!postText) {
                                const seen = new Set(); const lines = [];
                                clone.querySelectorAll('div[dir="auto"],span[dir="auto"]').forEach(el => {
                                    const t = (el.innerText||'').trim();
                                    if (t && !seen.has(t)) { seen.add(t); lines.push(t); }
                                });
                                postText = lines.join('\\n').trim();
                            }
                            if (!postText) postText = (clone.innerText||'').trim();

                            // ── allText (รวม text ทั้งหมดจากโพสต์ ยกเว้น comment) ─────────
                            const allSeen = new Set(); const allLines = [];
                            art.querySelectorAll('div[dir="auto"],span[dir="auto"]').forEach(el => {
                                if (isInComment(el)) return;
                                const t = (el.innerText||'').trim();
                                if (t && !allSeen.has(t)) { allSeen.add(t); allLines.push(t); }
                            });
                            const allText = allLines.join('\\n');

                            // ── รูปภาพ ───────────────────────────────────────────────────
                            let imageUrl = '';
                            for (const img of art.querySelectorAll('img[src*="scontent"]')) {
                                if (isInComment(img)) continue;
                                const src = img.src || '';
                                if (!src || src.includes('emoji')) continue;
                                const w = parseInt(img.getAttribute('width')||'0');
                                if (w && w <= 100) continue;
                                imageUrl = src; break;
                            }

                            // ── Timestamp ─────────────────────────────────────────────────
                            const rawText = (art.innerText||'').split('\\n').slice(0,10).join('\\n');
                            let utime = 0;
                            const abbrEl = art.querySelector('abbr[data-utime],[data-utime]');
                            if (abbrEl) utime = parseInt(abbrEl.getAttribute('data-utime')||'0');

                            let timeLabel = '';
                            if (!utime) {
                                const timeSelectors = [
                                    'a[role="link"] > span[aria-label]',
                                    'a[href*="/posts/"] > span',
                                    'a[href*="story_fbid"] > span',
                                    'a[href*="pfbid"] > span',
                                    'abbr[title]','span[title]','a > abbr',
                                ];
                                for (const sel of timeSelectors) {
                                    const el = art.querySelector(sel);
                                    if (!el || isInComment(el)) continue;
                                    const lbl = el.getAttribute('aria-label')
                                             || el.getAttribute('title')
                                             || el.textContent || '';
                                    if (lbl && (/\\d/.test(lbl) || /ago|yesterday|just now/i.test(lbl))) {
                                        timeLabel = lbl.trim(); break;
                                    }
                                }
                            }

                            return { postUrl, postText, imageUrl, rawText, allText, utime, timeLabel };
                        });
                    """, page_name)

                    if article_data:
                        self.log(f"📦 [{page_name}] เจอ {len(article_data)} articles (scroll {scroll_rounds})")

                except Exception as e:
                    self.log(f"⚠️ ดึง article ล้มเหลว: {type(e).__name__} — ข้ามรอบนี้")
                    article_data = []

                new_in_this_round = False

                for data in article_data:
                    if self._stop_event.is_set() or stop_early: break
                    self._resume_event.wait()

                    try:
                        post_url = data.get("postUrl", "")
                        _raw_for_id = (
                            data.get("postText") or data.get("allText") or data.get("rawText") or ""
                        ).strip()

                        if not post_url:
                            if not _raw_for_id: continue
                            _hash = hashlib.md5(_raw_for_id.encode()).hexdigest()[:16]
                            post_url = f"text_post://{page_name}/{_hash}"

                        # ── Normalize URL ─────────────────────────────────────
                        post_url = (post_url
                                    .replace("web.facebook.com", "www.facebook.com")
                                    .replace("m.facebook.com",   "www.facebook.com"))

                        # story.php/photo.php ต้องเก็บ query params ไว้ (มิฉะนั้นทุก post ซ้ำกัน)
                        _keep_query = any(x in post_url for x in ("story.php", "photo.php", "permalink/php"))
                        if _keep_query:
                            _p  = urlparse(post_url)
                            _q  = parse_qs(_p.query)
                            _qc = {k: v[0] for k, v in _q.items() if k in {"story_fbid","id","fbid","set","type"}}
                            post_url_clean = urlunparse(_p._replace(
                                query=urlencode(_qc), fragment=""
                            )).rstrip("/")
                        else:
                            post_url_clean = post_url.split("?")[0].rstrip("/")

                        if post_url_clean in seen_this_run: continue
                        seen_this_run.add(post_url_clean)
                        new_in_this_round = True

                        post_id = self._extract_post_id(post_url_clean)

                        # Debug: แสดง URL format เพื่อ diagnose
                        _fmt = ("pfbid"      if "pfbid"      in post_url_clean else
                                "story.php"  if "story.php"  in post_url_clean else
                                "text_post"  if post_url_clean.startswith("text_post://") else
                                "standard")
                        self.log(f"🔗 [{page_name}] format={_fmt} | id={str(post_id)[:24]}")

                        if self.db.is_seen(post_id) or self.db.is_seen_by_url(post_url_clean):
                            continue

                        # ── Timestamp ─────────────────────────────────────────
                        post_time = self._parse_timestamp(
                            data.get("rawText", ""),
                            utime=int(data.get("utime") or 0),
                            time_label=data.get("timeLabel", ""),
                        )

                        if post_time is not None:
                            if post_time < cutoff_time:
                                consecutive_old += 1
                                self.log(f"⏩ โพสต์เก่า ({consecutive_old}/{MAX_OLD}) | {post_time.strftime('%d/%m/%Y %H:%M')}")
                                if consecutive_old >= MAX_OLD:
                                    self.log(f"🏁 เจอโพสต์เก่าติดต่อกัน {MAX_OLD} รายการ — หยุดเพจนี้")
                                    stop_early = True; break
                                continue
                            else:
                                consecutive_old = 0
                                self.log(f"✅ โพสต์ใหม่ | {post_time.strftime('%d/%m/%Y %H:%M')}")
                        else:
                            # อ่านเวลาไม่ออก — รวมไว้ก่อน (อาจเป็น FB ภาษาอื่น)
                            self.log("⚠️ อ่านเวลาไม่ออก — รวมไว้ (FB ภาษาอื่น / format ใหม่)")

                        # ── รวบรวม text ───────────────────────────────────────
                        post_text = data.get("postText", "").strip()
                        all_text  = data.get("allText",  "").strip()
                        raw_text  = data.get("rawText",  "").strip()
                        if not post_text: post_text = all_text or raw_text
                        image_url = data.get("imageUrl") or None

                        # ── ตรวจ keyword ──────────────────────────────────────
                        found_keywords = []
                        if keywords:
                            texts = []
                            for src in (post_text, all_text, raw_text):
                                lo = src.lower() if src else ""
                                if lo and lo not in texts: texts.append(lo)
                            for kw in keywords:
                                kw_lo = kw.lower().strip()
                                if any(kw_lo in t for t in texts):
                                    if kw not in found_keywords:
                                        found_keywords.append(kw)
                            if not found_keywords: continue

                        # ตัดข้อความ timestamp ออกจากต้น post
                        if post_text:
                            lines = post_text.split("\n")
                            for i, line in enumerate(lines):
                                trimmed = line.strip()
                                if trimmed and len(trimmed) > 2:
                                    lo = trimmed.lower()
                                    if not (
                                        any(p in lo for p in ("เมื่อ","เพิ่ง","yesterday","just now","ago"))
                                        or re.match(r"^\d+.*[นาทีชั่วโมงวันสัปดาห์เดือนปี]", lo)
                                        or re.match(r"^\d+.*(?:min|hr|day|week|month|year)", lo)
                                    ):
                                        post_text = "\n".join(lines[i:]).strip()
                                        break

                        self.log(f"✅ keyword: {found_keywords} | {post_url_clean[:70]}")

                        # ── AI วิเคราะห์ ──────────────────────────────────────
                        ai_result = None
                        if self.ai_analyzer and post_text:
                            ai_result = self.ai_analyzer.analyze(post_text)
                            if ai_result and ai_result.get("is_target") and ai_result.get("score", 0) >= 6:
                                self.log(f"🎯 AI PASS | score={ai_result.get('score')}/10")
                                if self.sheets_manager:
                                    self.sheets_manager.upload_news(
                                        page_name=page_name,
                                        post_url=post_url_clean,
                                        post_text=post_text,
                                        persons=ai_result.get("persons", []),
                                        score=ai_result.get("score", 0),
                                        reason=ai_result.get("reason", ""),
                                    )
                                    self.log("💾 บันทึก Google Sheets แล้ว")

                        # ── ส่งแจ้งเตือน ──────────────────────────────────────
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
                                import certifi as _c
                                real = _c.where()
                                if os.path.isfile(real):
                                    os.environ["SSL_CERT_FILE"] = real
                                    os.environ["REQUESTS_CA_BUNDLE"] = real
                            except Exception: pass
                        else:
                            self.log(f"⚠️ OSError: {e}")
                        continue
                    except Exception as e:
                        self.log(f"⚠️ ข้ามโพสต์: {type(e).__name__}: {e}")
                        continue

                if new_in_this_round:
                    rounds_no_new_url = 0
                else:
                    rounds_no_new_url += 1
                    self.log(f"⚠️ [{page_name}] ไม่มี URL ใหม่ ({rounds_no_new_url}/{MAX_NO_NEW_URL})")
                    if rounds_no_new_url >= MAX_NO_NEW_URL:
                        self.log(f"📄 [{page_name}] ไม่มี URL ใหม่ติดกัน {MAX_NO_NEW_URL} รอบ — จบ")
                        break

        except InvalidSessionIdException:
            self.log(f"❌ Browser session หมดอายุระหว่างสแกน {page_name}")
            raise
        except WebDriverException as e:
            self.log(f"❌ WebDriver Error: {e}")
        except Exception as e:
            self.log(f"❌ Error scraping {page_name}: {e}")

        self.log(f"📊 {page_name} | scroll {scroll_rounds} รอบ | โพสต์ใหม่: {new_posts}")
        return new_posts

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self, email, password, page_urls, keywords, hours_back, loop_minutes):
        MAX_FAILS          = 5
        RETRY_WAIT         = 300
        _started           = False
        _session_start     = time.time()
        _total_posts       = 0
        last_cleanup_date  = None

        try:
            self.discord.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            self.tg.send_start(len(page_urls), len(keywords), loop_minutes, hours_back)
            _started = True

            while not self._stop_event.is_set():

                now = datetime.now()
                if now.hour >= 9 and last_cleanup_date != now.date():
                    self.log("🧹 ล้างข้อมูล DB เก่า...")
                    if self.db.cleanup_old_data():
                        self.log("✅ ล้าง DB สำเร็จ")
                    last_cleanup_date = now.date()

                self._cycle_count += 1
                cycle_start = time.time()
                self.log(f"\n{'='*50}")
                self.log(f"🔄 รอบที่ {self._cycle_count} | {now.strftime('%d/%m/%Y %H:%M:%S')}")

                cycle_ok = False
                try:
                    self._start_browser()

                    # ── Cookie / Session ──────────────────────────────────────
                    cookie_existed = os.path.exists(COOKIES_FILE)
                    if not self._load_cookies():
                        if cookie_existed:
                            # Cookie มีแต่ใช้ไม่ได้ = หมดอายุ → แจ้งเตือน + รอ Resume
                            self.log("🍪 Cookie หมดอายุ — แจ้งเตือนผู้ใช้")
                            self._handle_cookie_expired()
                            if self._stop_event.is_set(): break
                            # ใช้ _is_logged_in() ตรวจ DOM จริง ไม่ใช่ URL
                            if self._is_logged_in():
                                self.log("✅ ผู้ใช้ล็อกอินสำเร็จ — บันทึก Cookie ใหม่")
                                self._save_cookies()
                            else:
                                self.log("🔑 ยังไม่ได้ล็อกอิน — ลอง auto-login...")
                                if not self.login(email, password):
                                    raise RuntimeError("Login ล้มเหลวหลัง Cookie หมดอายุ")
                        else:
                            # ครั้งแรก — auto login
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
                        count = self.scrape_page(url, keywords, hours_back)
                        total_new += count
                        self.log(f"📊 {url.split('/')[-1]}: {count} โพสต์ใหม่")
                        if not self._stop_event.is_set():
                            time.sleep(random.uniform(2.0, 5.0))

                    if self._stop_event.is_set(): break

                    _total_posts += total_new
                    duration = time.time() - cycle_start
                    self.log(f"✅ รอบสแกนเสร็จ | โพสต์ใหม่รวม: {total_new}")
                    self.discord.send_cycle_complete(duration, loop_minutes, total_new, len(page_urls))
                    self.tg.send_cycle_complete(duration, loop_minutes, total_new, len(page_urls))
                    self._consecutive_failures = 0
                    cycle_ok = True

                except Exception as e:
                    self._consecutive_failures += 1
                    self.log(f"❌ รอบ {self._cycle_count} ล้มเหลว ({self._consecutive_failures}/{MAX_FAILS}): {type(e).__name__}: {e}")
                    if self._consecutive_failures >= MAX_FAILS:
                        self.log(f"🔴 ล้มเหลว {MAX_FAILS} รอบติด — หยุดทำงาน")
                        self.discord.send_obstacle(f"FATAL: ล้มเหลว {MAX_FAILS} รอบติด", "")
                        self.tg.send_obstacle(f"FATAL: ล้มเหลว {MAX_FAILS} รอบติด", "")
                        break
                finally:
                    self.log("🛑 ปิด Browser ชั่วคราว...")
                    self._safe_quit_driver()

                if self._stop_event.is_set(): break

                wait_secs = (loop_minutes * 60) if cycle_ok else RETRY_WAIT
                self.log(f"⏳ รอ {wait_secs // 60} นาทีก่อนรอบถัดไป...")
                self._sleep_interruptible(wait_secs)

        except OSError as e:
            if "cacert.pem" in str(e) or "certificate" in str(e).lower():
                self.log("⚠️ SSL Error — รีสตาร์ทโปรแกรมหนึ่งครั้ง")
            else:
                self.log(f"❌ Fatal OSError: {e}")
        except Exception as e:
            self.log(f"❌ Fatal Error: {type(e).__name__}: {e}")
        finally:
            if _started:
                runtime = time.time() - _session_start
                self.discord.send_stopped(runtime, _total_posts)
                self.tg.send_stopped(runtime, _total_posts)
            self._safe_quit_driver()
            self.log("🏁 Scraper หยุดทำงานสมบูรณ์")

    def stop(self):
        self._stop_event.set()
        self._resume_event.set()
