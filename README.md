# 📰 Python FB Scraper

> บอทสแกนโพสต์ Facebook อัตโนมัติ — แจ้งเตือนผ่าน Discord / Telegram — วิเคราะห์ด้วย Claude AI

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## สารบัญ

- [ภาพรวม](#-ภาพรวม)
- [ความสามารถ](#-ความสามารถ)
- [โครงสร้างโค้ด](#-โครงสร้างโค้ด)
- [การติดตั้ง](#-การติดตั้ง)
- [การตั้งค่า](#-การตั้งค่า)
- [วิธีใช้งาน](#-วิธีใช้งาน)
- [การแก้บัค](#-การแก้บัค)
- [คำถามที่พบบ่อย](#-คำถามที่พบบ่อย)

---

## 🧠 ภาพรวม

โปรแกรมนี้ใช้ **undetected-chromedriver** เพื่อเปิด Chrome แบบ stealth mode แล้วล็อกอิน Facebook ด้วย email/password ของคุณ จากนั้นวนสแกนเพจที่กำหนดทุก N นาที หากพบโพสต์ใหม่ที่มี keyword ที่ต้องการจะส่งการแจ้งเตือนไปยัง Discord และ/หรือ Telegram ทันที พร้อมตัวเลือกวิเคราะห์โพสต์ด้วย Claude AI และบันทึกลง Google Sheets

```
[GUI (CustomTkinter)]
        │
        ▼
[FacebookScraper Thread]
   ├── Chrome (undetected_chromedriver)
   ├── Login / Cookie Session
   ├── Scroll & Extract Posts
   │       └── keyword matching → new post found
   │                   ├── [DiscordNotifier] → Discord Webhook
   │                   ├── [TelegramNotifier] → Telegram Bot
   │                   ├── [ClaudeAnalyzer] → Anthropic API
   │                   └── [GoogleSheetsManager] → Google Sheets
   └── [DatabaseManager] (SQLite) — กัน duplicate
```

---

## ✨ ความสามารถ

| ฟีเจอร์ | รายละเอียด |
|---------|-----------|
| 🤖 Anti-detection | ใช้ `undetected_chromedriver` — ผ่าน bot detection ของ Facebook |
| 🍪 Session Cookies | บันทึก session เป็น JSON — ไม่ต้องล็อกอินใหม่ทุกครั้ง |
| 🔁 Auto Loop | วนสแกนทุก N นาที ตั้งเองได้ |
| ⏮️ Lookback Window | ดึงโพสต์ย้อนหลังได้สูงสุด N ชั่วโมง |
| 🔑 Keyword Filter | รองรับ hashtag และ วางหลายคำคั่นด้วย `,` |
| 💬 Discord | Rich embed พร้อมสีแยกตามเพจ + AI score |
| ✈️ Telegram | ส่งพร้อมปุ่ม inline บันทึก / ลบ / เปิดลิงก์ |
| 🤖 Claude AI | วิเคราะห์ความเกี่ยวข้อง ให้คะแนน 1-10 ระบุบุคคล |
| 📊 Google Sheets | บันทึกข่าวลง Spreadsheet อัตโนมัติ |
| 🗄️ SQLite | กัน duplicate post ทั้ง ID และ URL |
| 🚧 Obstacle Handler | ตรวจจับ Checkpoint / 2FA / CAPTCHA แล้วรอผู้ใช้แก้ไข |

---

## 🏗️ โครงสร้างโค้ด

โค้ดแบ่งเป็น 7 คลาสหลัก อธิบายแต่ละส่วนดังนี้:

---

### 1. `DatabaseManager` — จัดการฐานข้อมูล SQLite

```python
class DatabaseManager:
    DB_FILE = "scraper_data.db"
```

**หน้าที่:** เก็บประวัติโพสต์ที่แจ้งเตือนไปแล้ว ป้องกันแจ้งซ้ำ

| เมธอด | การทำงาน |
|-------|---------|
| `is_seen(post_id)` | ตรวจว่าเคอ post_id นี้เคยเห็นแล้วหรือยัง |
| `is_seen_by_url(url)` | ตรวจซ้ำด้วย URL (ครอบคลุมกรณี ID ต่างกันแต่ URL เดียวกัน) |
| `mark_seen(...)` | บันทึกโพสต์ว่าเห็นแล้ว |
| `cleanup_old_data()` | ลบข้อมูลเก่ากว่า 24 ชม. + VACUUM ลดขนาดไฟล์ |

ใช้ `threading.RLock()` เพื่อป้องกัน race condition เมื่อ scraper thread และ UI thread เข้าถึงพร้อมกัน

---

### 2. `DiscordNotifier` — ส่งการแจ้งเตือนไป Discord

```python
class DiscordNotifier:
    PAGE_COLORS = { "khaosod": 0xE53935, ... }  # สีแยกตามเพจ
```

**หน้าที่:** ส่ง Rich Embed ไปยัง Discord Webhook

| เมธอด | ส่งเมื่อไหร่ |
|-------|------------|
| `send_start(...)` | กด Start — แจ้งว่าระบบเริ่มทำงาน |
| `send_post(...)` | พบโพสต์ใหม่ที่ตรง keyword |
| `send_cycle_complete(...)` | สแกนครบรอบหนึ่ง |
| `send_obstacle(...)` | ติด Checkpoint / CAPTCHA / 2FA (พร้อม @everyone) |
| `send_stopped(...)` | กด Stop |

`_smart_truncate()` ตัดข้อความยาวอย่างฉลาด — ไม่ตัดกลางคำ

---

### 3. `TelegramNotifier` — ส่งการแจ้งเตือนไป Telegram

```python
class TelegramNotifier:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
```

**หน้าที่:** ส่งข้อความ HTML-formatted ไปยัง Telegram Bot พร้อม Inline Keyboard ให้ผู้ใช้กด "บันทึก" หรือ "ลบ" ได้จากใน Telegram โดยตรง

---

### 4. `TelegramListener` — รับคำสั่ง callback จาก Telegram

```python
class TelegramListener(threading.Thread):
```

**หน้าที่:** Thread แยกที่ polling `getUpdates` ตลอดเวลา เมื่อผู้ใช้กดปุ่ม inline บน Telegram จะจัดการ:
- `save_news` → เปลี่ยนปุ่มเป็น "✅ บันทึกแล้ว"
- `delete_news` → ลบข้อความออกจาก Chat
- `already_saved` → แจ้งว่าบันทึกไปแล้ว

---

### 5. `ClaudeAnalyzer` — วิเคราะห์ข่าวด้วย AI

```python
class ClaudeAnalyzer:
    model = "claude-3-5-sonnet-20240620"
```

**หน้าที่:** ส่งข้อความโพสต์ไปให้ Claude API วิเคราะห์ตาม system prompt ที่ผู้ใช้กำหนด แล้วรับ JSON กลับมา

```json
{
  "is_target": true,
  "score": 8,
  "persons": ["ทักษิณ", "แพทองธาร"],
  "reason": "ข่าวกล่าวถึงการประชุมพรรคเพื่อไทยโดยตรง"
}
```

ใช้เทคนิค "prefill" (`{"role": "assistant", "content": "{"}`) เพื่อบังคับให้ Claude ตอบด้วย JSON ทันที ไม่มีคำนำหน้า

---

### 6. `GoogleSheetsManager` — บันทึกลง Google Sheets

```python
class GoogleSheetsManager:
    scope = ["https://spreadsheets.google.com/feeds", ...]
```

**หน้าที่:** ใช้ Service Account ยืนยันตัวตนกับ Google API แล้ว `append_row()` ข้อมูลข่าวต่อท้าย Sheet

คอลัมน์ที่บันทึก: `ชื่อเพจ | URL | เนื้อหา | บุคคล | คะแนน AI | เหตุผล AI`

---

### 7. `FacebookScraper` — หัวใจหลักของโปรแกรม

```python
class FacebookScraper:
    SELECTORS = { "email_input": "//input[@id='email']", ... }
```

#### 7.1 Browser Lifecycle

```
_start_browser()
    │
    ├── [1] ลบ chromedriver cache เก่า (taskkill + shutil.rmtree)
    ├── [2] ตรวจเวอร์ชัน Chrome จาก EXE โดยตรง (4 path fallback)
    │         → ไม่เชื่อ Registry BLBeacon (อาจค้างเวอร์ชันเก่า)
    ├── [3] สร้าง ChromeOptions ใหม่ทุกครั้งด้วย _make_options()
    │         → ห้าม reuse object เดิมหลังส่งให้ uc.Chrome()
    └── [4] เปิด Chrome พร้อม version_main ที่ตรวจได้จริง
```

#### 7.2 Session Management (Cookies)

```
_save_cookies()  →  บันทึก JSON  →  fb_cookies.json
_load_cookies()  →  โหลด JSON   →  ใส่กลับใน Browser
```

ใช้ JSON แทน pickle — ป้องกัน arbitrary code execution (pickle สามารถ execute โค้ดได้เมื่อโหลด)

#### 7.3 Login Flow

```
login(email, password)
    │
    ├── กรอก email + password แบบ human typing (_type_human)
    │       → delay สุ่ม random.uniform() ป้องกัน bot detection
    ├── คลิกปุ่ม Login (7 selector strategies + Enter fallback)
    └── ตรวจผล → บันทึก Cookies
```

#### 7.4 Obstacle Detection

ตรวจ 5 ประเภทอัตโนมัติ:

| ประเภท | วิธีตรวจ |
|--------|---------|
| Checkpoint | URL contains `checkpoint` |
| 2FA | URL contains `two_step_verification` |
| CAPTCHA | URL contains `captcha` |
| Identity Verify | ตรวจ 3 ชั้น: URL → form action → heading text |
| Account Suspended | URL contains `suspended` / `disabled` |

Identity Verification มี **re-confirm** — ตรวจซ้ำหลัง 2 วิ ป้องกัน false positive จาก notification banner

#### 7.5 Scraping Loop

```
scrape_page(page_url, keywords, hours_back)
    │
    ├── เปิดเพจ + ตรวจ obstacle
    ├── วนซ้ำ (สูงสุด 30 scroll rounds):
    │   ├── _slow_scroll() — เลื่อนหน้าจำลองพฤติกรรมมนุษย์
    │   ├── _get_articles() — ดึง div[role='article'] ทั้งหมด
    │   ├── ตรวจ timestamp (cutoff ตาม hours_back)
    │   ├── ตรวจ duplicate (DB + seen_this_run set)
    │   ├── keyword matching
    │   ├── ถ้าตรง: แจ้ง Discord + Telegram + AI + Sheets
    │   └── ถ้าเจอโพสต์เก่าติดกัน 5 ครั้ง → หยุดสแกนเพจนี้
    └── log สรุปจำนวนโพสต์ใหม่
```

#### 7.6 Timestamp Parser

รองรับรูปแบบ Thai + English:
- `เมื่อสักครู่`, `just now` → ตอนนี้
- `3 นาที`, `2 hrs` → หักจาก now
- `เมื่อวาน`, `yesterday` → เมื่อวาน
- `data-utime` attribute → Unix timestamp ตรงๆ

---

### 8. GUI — `ScraperApp` (CustomTkinter)

หน้าตาแบ่งเป็น 2 คอลัมน์:

```
┌─────────────────────────────────────────────────────┐
│  [▶ Start] [⏹ Stop] [▶▶ Resume] [🙈 ซ่อน] [💾 บันทึก] │
│  ⬤ กำลังทำงาน    ⏱ 00:12:34                          │
├─────────────────────┬───────────────────────────────┤
│  ⚙️ การตั้งค่า      │  📋 Real-time Activity Log     │
│                     │                               │
│  🔐 Credentials     │  [15:02:38] 🔎 Chrome v147    │
│  🎯 Target Pages    │  [15:02:46] ✅ Browser เปิด   │
│  🔑 Keywords        │  [15:03:01] 🔍 สแกนเพจ...    │
│  ⏱ Timeframe        │  [15:03:45] 📨 โพสต์ใหม่!    │
│  💬 Discord         │                               │
│  ✈️ Telegram        │                               │
│  🤖 AI & Sheets     │                               │
└─────────────────────┴───────────────────────────────┘
```

การตั้งค่าทั้งหมดบันทึกลงไฟล์ `scraper_config.json` อัตโนมัติ

---

## 📦 การติดตั้ง

### ความต้องการระบบ

- **OS:** Windows 10 / 11 (64-bit)
- **Python:** 3.10 ขึ้นไป
- **Chrome:** ติดตั้งแล้ว (ไม่จำกัดเวอร์ชัน — ตรวจเวอร์ชันอัตโนมัติ)
- **RAM:** แนะนำ 4GB ขึ้นไป

### ขั้นตอนติดตั้ง

**Step 1 — Clone repo**
```bash
git clone https://github.com/puripong1st/Python-FB-Scraper.git
cd Python-FB-Scraper
```

**Step 2 — สร้าง Virtual Environment (แนะนำ)**
```bash
python -m venv venv
venv\Scripts\activate
```

**Step 3 — ติดตั้ง dependencies**
```bash
pip install -r requirements.txt
```

หากไม่มี `requirements.txt` ให้รัน:
```bash
pip install customtkinter selenium undetected-chromedriver requests anthropic gspread oauth2client
```

**Step 4 — รันโปรแกรม**
```bash
python main-gemini.py
```

---

## ⚙️ การตั้งค่า

### ส่วนที่ 1 — Facebook Credentials (จำเป็น)

กรอก Email และ Password ของ Facebook ในช่องที่กำหนด

> ⚠️ **แนะนำ:** ใช้บัญชีสำรอง ไม่ใช่บัญชีหลัก เพราะ Facebook อาจ flag บัญชีที่ login จาก automation

---

### ส่วนที่ 2 — Target Pages (จำเป็น)

วาง URL เพจที่ต้องการสแกน บรรทัดละ 1 เพจ:
```
https://www.facebook.com/BBCnewsThai
https://www.facebook.com/voathai
https://www.facebook.com/khaosod
```

---

### ส่วนที่ 3 — Keywords (ไม่บังคับ)

พิมพ์ keyword แล้วกด Enter หรือคั่นด้วย `,`
- รองรับ: `เพื่อไทย`, `#ทักษิณ`, `ประยุทธ์, ประวิตร`
- ถ้าเว้นว่าง = แจ้งทุกโพสต์ใหม่ (ไม่กรอง)

---

### ส่วนที่ 4 — Timeframe & Loop

| ช่อง | ความหมาย | ค่าแนะนำ |
|------|---------|---------|
| ดึงย้อนหลัง (ชั่วโมง) | สแกนโพสต์ที่โพสต์ภายในกี่ชั่วโมงล่าสุด | `6` |
| วนลูปทุก (นาที) | รอกี่นาทีก่อนสแกนรอบถัดไป | `30` |

---

### ส่วนที่ 5 — Discord Webhook (ไม่บังคับ)

1. เปิด Discord → Server Settings → Integrations → Webhooks → New Webhook
2. Copy Webhook URL มาวางในช่อง
3. กด **🧪 ทดสอบ** เพื่อยืนยันว่าส่งได้

---

### ส่วนที่ 6 — Telegram Bot (ไม่บังคับ)

1. ส่ง `/newbot` ให้ `@BotFather` บน Telegram → รับ **Bot Token**
2. หา Chat ID ของกลุ่มหรือ channel ที่ต้องการ (ใช้ `@userinfobot` ช่วยได้)
3. กรอก Token และ Chat ID ในช่องที่กำหนด
4. กด **🧪 ทดสอบ**

---

### ส่วนที่ 7 — AI Analysis & Google Sheets (ไม่บังคับ)

#### Claude AI

1. สมัคร Anthropic API ที่ [console.anthropic.com](https://console.anthropic.com)
2. สร้าง API Key
3. วาง Key ในช่อง **Claude API Key**

#### Google Sheets

1. ไปที่ [Google Cloud Console](https://console.cloud.google.com)
2. สร้าง Project ใหม่ → Enable **Google Sheets API** + **Google Drive API**
3. สร้าง **Service Account** → Download credentials JSON
4. เปิด Google Sheet ที่ต้องการ → Share ให้ email ของ Service Account (Editor)
5. กรอก:
   - **Path ไฟล์ .json** → path ของ credentials ที่ download มา
   - **ชื่อ Google Sheet** → ชื่อไฟล์ใน Google Drive (ไม่ใช่ URL)

#### AI Prompt

แก้ prompt ในกล่องได้ตามต้องการ — ต้องให้ Claude ตอบกลับเป็น JSON รูปแบบนี้เสมอ:
```json
{
  "is_target": true,
  "score": 1-10,
  "persons": ["ชื่อบุคคล"],
  "reason": "สรุปเหตุผล"
}
```

---

## 🚀 วิธีใช้งาน

### การเริ่มใช้งานครั้งแรก

```
1. กรอก Email + Password Facebook
2. ใส่ URL เพจที่ต้องการ
3. ใส่ Keywords
4. ตั้ง Timeframe และ Loop interval
5. กด 💾 บันทึก (ค่าจะถูกจำไว้)
6. กด ▶ Start
```

**รอบแรก** — Chrome จะเปิดขึ้นมาและล็อกอิน Facebook โดยอัตโนมัติ ถ้า Facebook ถามยืนยันตัวตน ให้แก้ไขในหน้าต่าง Browser แล้วกด **▶▶ Resume**

**หลังจาก Login สำเร็จ** — Cookies จะถูกบันทึก การเปิดครั้งถัดไปจะไม่ต้อง login ใหม่ และปุ่ม **🙈 ซ่อน Browser** จะเปิดใช้ได้

---

### การใช้งานทั่วไป

| ปุ่ม | การทำงาน |
|------|---------|
| **▶ Start** | เริ่มสแกน — เปิด Chrome ล็อกอิน แล้ววนสแกนเพจ |
| **⏹ Stop** | หยุดทำงาน — รอให้รอบปัจจุบันเสร็จก่อน |
| **▶▶ Resume** | กดหลังแก้ Checkpoint/CAPTCHA ให้ระบบทำงานต่อ |
| **🙈 ซ่อน Browser** | ซ่อนหน้าต่าง Chrome (ใช้ได้หลัง Cookies บันทึกแล้ว) |
| **💾 บันทึก** | บันทึกการตั้งค่าทั้งหมดลงไฟล์ config |
| **🗑 ล้าง Log** | เคลียร์ Log ในหน้าต่าง |

---

### ไฟล์ที่สร้างโดยโปรแกรม

| ไฟล์ | ความหมาย |
|------|---------|
| `scraper_config.json` | การตั้งค่าทั้งหมด (email, webhook, keywords ฯลฯ) |
| `fb_cookies.json` | Session cookies ของ Facebook |
| `scraper_data.db` | ฐานข้อมูล SQLite เก็บโพสต์ที่เห็นแล้ว |
| `pages.txt` | รายการ URL เพจ |
| `keywords.txt` | รายการ keyword |

---

## 🐛 การแก้บัค

### บัค 1 — ChromeDriver version ไม่ตรงกับ Chrome

**อาการ:**
```
This version of ChromeDriver only supports Chrome version 141
Current browser version is 147.0.7727.117
```

**สาเหตุ:** Registry `BLBeacon` ค้างเวอร์ชันเก่า โปรแกรมอ่านเวอร์ชัน 141 ไปดาวน์โหลด ChromeDriver 141 แต่ Chrome จริงคือ 147

**วิธีแก้ (โค้ดแก้ไขแล้ว):** ตอนนี้โปรแกรมอ่านเวอร์ชันจาก `chrome.exe` โดยตรงก่อนเสมอ ผ่าน PowerShell 4 วิธีตามลำดับ:
1. `Get-Command chrome` (ผ่าน PATH)
2. `C:\Program Files\Google\Chrome\Application\chrome.exe`
3. `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`
4. `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`

**ถ้ายังเกิดขึ้น** ให้รัน:
```bash
pip install --upgrade undetected-chromedriver
```

---

### บัค 2 — ChromeOptions reuse error

**อาการ:**
```
you cannot reuse the ChromeOptions object
```

**สาเหตุ:** `ChromeOptions` object ส่งให้ `uc.Chrome()` ไปแล้ว ห้ามนำกลับมาใช้ซ้ำใน fallback

**วิธีแก้ (โค้ดแก้ไขแล้ว):** แยก `_make_options()` เป็น helper function สร้าง object ใหม่ทุกครั้ง:
```python
def _make_options():
    opts = uc.ChromeOptions()
    opts.add_argument("--no-sandbox")
    # ...
    return opts  # object ใหม่ทุกครั้ง

# ใช้ทั้ง try และ except
self.driver = uc.Chrome(options=_make_options(), ...)
```

---

### บัค 3 — Facebook ติด Checkpoint / CAPTCHA

**อาการ:** โปรแกรมหยุดนิ่ง มีแจ้งเตือนใน Discord/Telegram ว่า "บอทติดหน้า Checkpoint"

**วิธีแก้:**
1. เปิดหน้าต่าง Chrome ที่โปรแกรมเปิดไว้
2. แก้ไขตามที่ Facebook แจ้ง (ยืนยันตัวตน/กรอก CAPTCHA)
3. กด **▶▶ Resume** บนโปรแกรม

**ป้องกันในอนาคต:**
- ใช้บัญชี Facebook ที่ active มานานและมีประวัติ
- ไม่ล็อกอินบ่อยเกินไป (loop ทุก 30 นาที ไม่ควรมีปัญหา)
- ถ้าโดนบ่อย ลองลด loop interval เป็น 60 นาที

---

### บัค 4 — Chrome ไม่เปิด / Crash ทันที

**วิธีแก้:**
```bash
# 1. ปิด Chrome และ ChromeDriver ทั้งหมดก่อน
taskkill /f /im chrome.exe
taskkill /f /im chromedriver.exe

# 2. ลบ cache ของ undetected-chromedriver
# ลบโฟลเดอร์: %APPDATA%\undetected_chromedriver

# 3. รัน pip upgrade
pip install --upgrade undetected-chromedriver selenium
```

---

### บัค 5 — ไม่พบโพสต์ / สแกนแล้วไม่มีอะไร

**สาเหตุที่เป็นไปได้:**

| สาเหตุ | วิธีตรวจ | วิธีแก้ |
|--------|---------|---------|
| Facebook เปลี่ยน HTML structure | เปิด DevTools ดู element | รายงาน issue บน GitHub |
| Keyword ไม่ตรง | ดู log ว่า "พบ N โพสต์" แต่ "0 ตรง keyword" | แก้ keyword ให้ตรง |
| ช่วงเวลาเก่าเกิน | โพสต์อาจเก่ากว่า hours_back ที่ตั้ง | เพิ่ม hours_back |
| Session หมดอายุ | Log บอกว่า redirect ไป login | ลบ `fb_cookies.json` แล้วรันใหม่ |

---

### บัค 6 — Google Sheets เชื่อมต่อไม่ได้

**Checklist:**
- [ ] Enable **Google Sheets API** และ **Google Drive API** ใน Google Cloud Console
- [ ] Share Google Sheet ให้ email ของ Service Account (สิทธิ์ Editor)
- [ ] ชื่อ Sheet ที่กรอกตรงกับชื่อไฟล์ใน Drive (case-sensitive)
- [ ] Path ของ `.json` credentials ถูกต้องและไฟล์มีอยู่จริง

---

### บัค 7 — Telegram ไม่ได้รับข้อความ

**Checklist:**
- [ ] Bot Token ถูกต้อง (ขึ้นต้นด้วยตัวเลข เช่น `123456789:ABC...`)
- [ ] Chat ID ถูกต้อง (group/channel ขึ้นต้นด้วย `-100`)
- [ ] Add bot เข้า group/channel แล้ว
- [ ] ถ้าเป็น channel: Bot ต้องเป็น Admin

---

## ❓ คำถามที่พบบ่อย

**Q: ปลอดภัยไหมที่ให้โปรแกรมรู้รหัสผ่าน Facebook?**

รหัสผ่านเก็บไว้ใน `scraper_config.json` บนเครื่องของคุณเท่านั้น ไม่ได้ส่งไปที่ใดนอกจาก Facebook โดยตรง แนะนำให้ใช้บัญชีสำรองเพื่อความปลอดภัย

**Q: ใช้บน Mac / Linux ได้ไหม?**

ยังไม่รองรับ เพราะใช้ `winreg` (Windows Registry) และ `taskkill` สำหรับตรวจ Chrome version และจัดการ process ต้องแก้ไขส่วนนี้ก่อนใช้บน OS อื่น

**Q: ทำไมใช้ undetected-chromedriver แทน Selenium ปกติ?**

Facebook ตรวจจับ Selenium ปกติได้ง่าย `undetected_chromedriver` patch ChromeDriver ให้ผ่านการตรวจจับ bot โดยซ่อน automation signature

**Q: โพสต์ที่แจ้งซ้ำ ทำอย่างไร?**

ระบบตรวจซ้ำ 2 ชั้น: ด้วย `post_id` และ `post_url` ถ้ายังเจอซ้ำ ให้รายงาน issue

**Q: ลบ fb_cookies.json แล้วเกิดอะไรขึ้น?**

โปรแกรมจะล็อกอินใหม่ด้วย email/password ในครั้งถัดไปที่กด Start

---

## 📄 License

MIT License — ใช้ได้อย่างอิสระ ทั้งส่วนตัวและเชิงพาณิชย์
