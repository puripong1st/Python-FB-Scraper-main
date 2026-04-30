"""
ui/app.py
━━━━━━━━━
ScraperApp — หน้าต่างหลัก CustomTkinter (Modern UI Redesign)
แบ่งเป็น Header | Left (Tabs) | Right (Log)
"""

import threading
import time
import json
import os
from queue import Queue, Empty
from datetime import datetime

import customtkinter as ctk

from database import DatabaseManager
from notifiers import DiscordNotifier, TelegramNotifier, TelegramListener
from ai_analyzer import ClaudeAnalyzer
from sheets_manager import GoogleSheetsManager
from scraper import FacebookScraper
from ui.widgets import KeywordTagInput


# ── Color tokens ─────────────────────────────────────────────────────────────
C = {
    "bg":           "#0a0e1a",   # พื้นหลังหลัก
    "surface":      "#111827",   # card/panel
    "surface2":     "#1a2236",   # nested card
    "border":       "#1f2d45",   # เส้นขอบ
    "accent":       "#3b82f6",   # น้ำเงิน primary
    "accent_dark":  "#1d4ed8",   # hover
    "accent_glow":  "#60a5fa",   # label/icon
    "success":      "#22c55e",
    "warning":      "#f59e0b",
    "danger":       "#ef4444",
    "danger_dark":  "#b91c1c",
    "text":         "#e2e8f0",
    "text_muted":   "#64748b",
    "text_dim":     "#334155",
    "orange":       "#f97316",
    "orange_dark":  "#c2410c",
    "purple":       "#8b5cf6",
    "purple_dark":  "#6d28d9",
    "green_dark":   "#15803d",
}


class ScraperApp(ctk.CTk):
    SETTINGS_FILE = "scraper_settings.json"
    PAGES_FILE    = "scraper_pages.json"
    KEYWORDS_FILE = "scraper_keywords.json"

    def __init__(self):
        super().__init__()
        self.title("FB News Scraper")
        self.geometry("1440x880")
        self.minsize(1100, 660)
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=C["bg"])

        self._db: DatabaseManager              = DatabaseManager()
        self._scraper: FacebookScraper | None  = None
        self._scraper_thread: threading.Thread | None = None
        self._log_queue: Queue                 = Queue()
        self._session_start_time: float | None = None
        self._posts_found_total: int           = 0
        self._cycle_count: int                 = 0
        self._is_running: bool                 = False

        self._build_ui()
        self._load_settings()
        self._load_pages()
        self._load_keywords()
        self._poll_log_queue()
        self._update_timer()

    # ─────────────────────────────────────────────────────────────────────────
    # BUILD UI
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_left_panel()
        self._build_right_panel()

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color="#0d1628", height=64, corner_radius=0)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(1, weight=1)

        # Logo + title
        logo_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        logo_frame.grid(row=0, column=0, padx=(18, 0), pady=12, sticky="w")

        ctk.CTkLabel(
            logo_frame, text="📘",
            font=ctk.CTkFont(size=22),
        ).pack(side="left", padx=(0, 8))

        title_frame = ctk.CTkFrame(logo_frame, fg_color="transparent")
        title_frame.pack(side="left")
        ctk.CTkLabel(
            title_frame, text="FB News Scraper",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=C["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_frame, text="Discord · Telegram · Claude AI",
            font=ctk.CTkFont(size=10),
            text_color=C["text_muted"],
        ).pack(anchor="w")

        # Status pill
        self._status_frame = ctk.CTkFrame(hdr, fg_color="#1a2236", corner_radius=20, height=32)
        self._status_frame.grid(row=0, column=1, padx=16, pady=16, sticky="w")
        self._status_frame.grid_propagate(False)

        self._status_dot = ctk.CTkLabel(
            self._status_frame, text="⬤",
            font=ctk.CTkFont(size=10),
            text_color=C["danger"], width=20,
        )
        self._status_dot.pack(side="left", padx=(10, 2), pady=6)

        self._status_lbl = ctk.CTkLabel(
            self._status_frame, text="หยุดทำงาน",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=C["text_muted"],
        )
        self._status_lbl.pack(side="left", padx=(0, 10), pady=6)

        # Stats chips
        stats = ctk.CTkFrame(hdr, fg_color="transparent")
        stats.grid(row=0, column=1, padx=(160, 0), pady=16, sticky="w")

        self._timer_lbl = self._make_stat_chip(stats, "⏱", "00:00:00")
        self._posts_lbl = self._make_stat_chip(stats, "📰", "0 โพสต์")
        self._cycle_lbl = self._make_stat_chip(stats, "🔄", "รอบ 0")

        # Action buttons
        btn_bar = ctk.CTkFrame(hdr, fg_color="transparent")
        btn_bar.grid(row=0, column=2, padx=(0, 14), pady=12, sticky="e")

        self.start_btn = self._hdr_btn(btn_bar, "▶  Start",     C["accent"],  C["accent_dark"], self._on_start)
        self.start_btn.grid(row=0, column=0, padx=(0, 6))

        self.stop_btn = self._hdr_btn(btn_bar, "⏹  Stop",      C["danger"],  C["danger_dark"], self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(0, 6))

        self.resume_btn = self._hdr_btn(btn_bar, "▶▶ Resume",   C["orange"],  C["orange_dark"], self._on_resume, state="disabled", width=120)
        self.resume_btn.grid(row=0, column=2, padx=(0, 6))

        self.hide_btn = self._hdr_btn(btn_bar, "🙈 ซ่อน",      C["purple"],  C["purple_dark"], self._on_hide_browser, state="disabled", width=100)
        self.hide_btn.grid(row=0, column=3, padx=(0, 6))

        self.save_btn = self._hdr_btn(btn_bar, "💾 บันทึก",    C["green_dark"], "#166534", self._save_settings, width=100)
        self.save_btn.grid(row=0, column=4)

    def _make_stat_chip(self, parent, icon: str, text: str) -> ctk.CTkLabel:
        chip = ctk.CTkFrame(parent, fg_color="#1a2236", corner_radius=12, height=28)
        chip.pack(side="left", padx=(0, 8))
        chip.pack_propagate(False)
        ctk.CTkLabel(chip, text=icon, font=ctk.CTkFont(size=10), width=18).pack(side="left", padx=(8, 2))
        lbl = ctk.CTkLabel(chip, text=text, font=ctk.CTkFont(size=10, weight="bold"),
                           text_color=C["accent_glow"])
        lbl.pack(side="left", padx=(0, 10))
        return lbl

    def _hdr_btn(self, parent, text, fg, hover, cmd, state="normal", width=110):
        return ctk.CTkButton(
            parent, text=text, width=width, height=36,
            fg_color=fg, hover_color=hover,
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=8, command=cmd, state=state,
        )

    # ── Left Panel (Tabbed Settings) ──────────────────────────────────────────

    def _build_left_panel(self):
        left = ctk.CTkFrame(self, fg_color=C["surface"], width=520, corner_radius=0)
        left.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        left.grid_propagate(False)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self._tabs = ctk.CTkTabview(
            left,
            fg_color=C["surface"],
            segmented_button_fg_color="#0d1628",
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["accent_dark"],
            segmented_button_unselected_color="#0d1628",
            segmented_button_unselected_hover_color=C["surface2"],
            text_color=C["text"],
            text_color_disabled=C["text_muted"],
            corner_radius=0,
            border_width=0,
        )
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

        self._tabs.add("🔐  บัญชี")
        self._tabs.add("🎯  เพจ & คำค้น")
        self._tabs.add("📡  แจ้งเตือน")
        self._tabs.add("🤖  AI & Sheets")

        self._build_tab_account()
        self._build_tab_pages()
        self._build_tab_notify()
        self._build_tab_ai()

    def _card(self, parent, title: str = None) -> ctk.CTkFrame:
        """สร้าง card พร้อม optional title"""
        wrap = ctk.CTkFrame(parent, fg_color=C["surface2"], corner_radius=10,
                            border_width=1, border_color=C["border"])
        wrap.pack(fill="x", padx=14, pady=(0, 10))

        if title:
            ctk.CTkLabel(
                wrap, text=title,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=C["accent_glow"],
            ).pack(anchor="w", padx=14, pady=(10, 6))

        inner = ctk.CTkFrame(wrap, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=(0, 12))
        return inner

    def _label(self, parent, text: str, muted: bool = False):
        ctk.CTkLabel(
            parent, text=text,
            font=ctk.CTkFont(size=11, weight="bold" if not muted else "normal"),
            text_color=C["text"] if not muted else C["text_muted"],
        ).pack(anchor="w", pady=(0, 3))

    def _entry(self, parent, var, placeholder="", show="", height=36) -> ctk.CTkEntry:
        e = ctk.CTkEntry(
            parent, textvariable=var, placeholder_text=placeholder,
            show=show, height=height,
            fg_color="#0d1628", border_color=C["border"],
            text_color=C["text"], placeholder_text_color=C["text_dim"],
        )
        e.pack(fill="x", pady=(0, 8))
        return e

    def _divider(self, parent):
        ctk.CTkFrame(parent, height=1, fg_color=C["border"]).pack(fill="x", pady=6)

    # Tab 1 — บัญชี & Timeframe
    def _build_tab_account(self):
        tab = self._tabs.tab("🔐  บัญชี")
        tab.configure(fg_color=C["surface"])

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["border"])
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)

        # Facebook credentials
        f1 = self._card(scroll, "🔐  Facebook Login")
        self._label(f1, "Email / ชื่อผู้ใช้")
        self.email_var = ctk.StringVar()
        self._entry(f1, self.email_var, "your@email.com")

        self._label(f1, "Password")
        self.pass_var = ctk.StringVar()
        self._entry(f1, self.pass_var, "รหัสผ่าน", show="●")

        ctk.CTkLabel(
            f1, text="⚠️  แนะนำใช้บัญชีสำรอง ไม่ใช่บัญชีหลัก",
            font=ctk.CTkFont(size=10), text_color=C["warning"],
        ).pack(anchor="w")

        # Timeframe
        f2 = self._card(scroll, "⏱️  ตั้งเวลา")
        row = ctk.CTkFrame(f2, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 2), weight=1)

        left_col = ctk.CTkFrame(row, fg_color="transparent")
        left_col.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._label(left_col, "📅  ดึงย้อนหลัง (ชม.)")
        self.hours_var = ctk.StringVar(value="6")
        ctk.CTkEntry(left_col, textvariable=self.hours_var, height=36,
                     fg_color="#0d1628", border_color=C["border"], text_color=C["text"]).pack(fill="x")

        mid = ctk.CTkFrame(row, fg_color=C["border"], width=1)
        mid.grid(row=0, column=1, sticky="ns", padx=4)

        right_col = ctk.CTkFrame(row, fg_color="transparent")
        right_col.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        self._label(right_col, "🔄  วนลูปทุก (นาที)")
        self.loop_var = ctk.StringVar(value="30")
        ctk.CTkEntry(right_col, textvariable=self.loop_var, height=36,
                     fg_color="#0d1628", border_color=C["border"], text_color=C["text"]).pack(fill="x")

        ctk.CTkLabel(
            f2, text="💡  แนะนำ: ดึงย้อนหลัง 6 ชม. | วนลูปทุก 30 นาที",
            font=ctk.CTkFont(size=10), text_color=C["text_muted"],
        ).pack(anchor="w", pady=(6, 0))

    # Tab 2 — เพจ & คำค้น
    def _build_tab_pages(self):
        tab = self._tabs.tab("🎯  เพจ & คำค้น")
        tab.configure(fg_color=C["surface"])

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["border"])
        scroll.pack(fill="both", expand=True)

        # Pages
        pg_hdr = ctk.CTkFrame(scroll, fg_color=C["surface2"], corner_radius=10,
                               border_width=1, border_color=C["border"])
        pg_hdr.pack(fill="x", padx=14, pady=(0, 10))

        ph = ctk.CTkFrame(pg_hdr, fg_color="transparent")
        ph.pack(fill="x", padx=14, pady=(10, 6))
        ph.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ph, text="🎯  URL เพจเป้าหมาย  (แต่ละบรรทัด = 1 เพจ)",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=C["accent_glow"]).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(ph, text="💾 บันทึกเพจ", width=110, height=28,
                      fg_color=C["accent"], hover_color=C["accent_dark"],
                      font=ctk.CTkFont(size=10, weight="bold"),
                      corner_radius=6,
                      command=self._save_pages).grid(row=0, column=1, sticky="e")

        self.pages_textbox = ctk.CTkTextbox(
            pg_hdr, height=120, fg_color="#0d1628",
            text_color=C["text"], border_color=C["border"], border_width=1,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.pages_textbox.pack(fill="x", padx=14, pady=(0, 12))
        self.pages_textbox.insert("1.0", "https://www.facebook.com/BBCnewsThai\nhttps://www.facebook.com/voathai")

        # Keywords
        kw_hdr = ctk.CTkFrame(scroll, fg_color=C["surface2"], corner_radius=10,
                               border_width=1, border_color=C["border"])
        kw_hdr.pack(fill="x", padx=14, pady=(0, 10))

        kh = ctk.CTkFrame(kw_hdr, fg_color="transparent")
        kh.pack(fill="x", padx=14, pady=(10, 6))
        kh.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(kh, text="🔑  Keywords  (กด Enter | คั่นด้วย ,)",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=C["accent_glow"]).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(kh, text="💾 บันทึก Keywords", width=130, height=28,
                      fg_color=C["accent"], hover_color=C["accent_dark"],
                      font=ctk.CTkFont(size=10, weight="bold"),
                      corner_radius=6,
                      command=self._save_keywords).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            kw_hdr, text="  รองรับ #แฮชแท็ก  |  ถ้าเว้นว่าง = แจ้งทุกโพสต์",
            font=ctk.CTkFont(size=10), text_color=C["text_muted"],
        ).pack(anchor="w", padx=14, pady=(0, 4))

        self.keywords_widget = KeywordTagInput(
            kw_hdr,
            defaults=["เพื่อไทย", "แพทองธาร", "ทักษิณ", "เศรษฐา"],
        )
        self.keywords_widget.pack(fill="x", padx=8, pady=(0, 12))

    # Tab 3 — แจ้งเตือน
    def _build_tab_notify(self):
        tab = self._tabs.tab("📡  แจ้งเตือน")
        tab.configure(fg_color=C["surface"])

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["border"])
        scroll.pack(fill="both", expand=True)

        # Discord
        dc = self._card(scroll, "💬  Discord Webhook")

        dc_row = ctk.CTkFrame(dc, fg_color="transparent")
        dc_row.pack(fill="x")
        dc_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(dc_row, text="Webhook URL",
                     font=ctk.CTkFont(size=11), text_color=C["text_muted"]).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(dc_row, text="🧪 ทดสอบ", width=86, height=26,
                      fg_color=C["surface"], hover_color=C["surface2"],
                      border_width=1, border_color=C["border"],
                      font=ctk.CTkFont(size=10), corner_radius=6,
                      command=self._test_discord).grid(row=0, column=1, sticky="e")

        self.webhook_var = ctk.StringVar()
        ctk.CTkEntry(dc, textvariable=self.webhook_var,
                     placeholder_text="https://discord.com/api/webhooks/...",
                     height=36, fg_color="#0d1628",
                     border_color=C["border"], text_color=C["text"],
                     placeholder_text_color=C["text_dim"]).pack(fill="x", pady=(4, 0))

        # Telegram
        tg = self._card(scroll, "✈️  Telegram Bot")

        tg_hdr = ctk.CTkFrame(tg, fg_color="transparent")
        tg_hdr.pack(fill="x")
        tg_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tg_hdr, text="Bot Token & Chat ID",
                     font=ctk.CTkFont(size=11), text_color=C["text_muted"]).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(tg_hdr, text="🧪 ทดสอบ", width=86, height=26,
                      fg_color=C["surface"], hover_color=C["surface2"],
                      border_width=1, border_color=C["border"],
                      font=ctk.CTkFont(size=10), corner_radius=6,
                      command=self._test_telegram).grid(row=0, column=1, sticky="e")

        tg_fields = ctk.CTkFrame(tg, fg_color="transparent")
        tg_fields.pack(fill="x", pady=(6, 0))
        tg_fields.grid_columnconfigure((0, 1), weight=1)

        left_f = ctk.CTkFrame(tg_fields, fg_color="transparent")
        left_f.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(left_f, text="Bot Token",
                     font=ctk.CTkFont(size=10), text_color=C["text_muted"]).pack(anchor="w", pady=(0, 2))
        self.tg_token_var = ctk.StringVar()
        ctk.CTkEntry(left_f, textvariable=self.tg_token_var,
                     placeholder_text="123456789:ABCdef...", height=36,
                     fg_color="#0d1628", border_color=C["border"],
                     text_color=C["text"], placeholder_text_color=C["text_dim"]).pack(fill="x")

        right_f = ctk.CTkFrame(tg_fields, fg_color="transparent")
        right_f.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(right_f, text="Chat ID",
                     font=ctk.CTkFont(size=10), text_color=C["text_muted"]).pack(anchor="w", pady=(0, 2))
        self.tg_chatid_var = ctk.StringVar()
        ctk.CTkEntry(right_f, textvariable=self.tg_chatid_var,
                     placeholder_text="-100123456789", height=36,
                     fg_color="#0d1628", border_color=C["border"],
                     text_color=C["text"], placeholder_text_color=C["text_dim"]).pack(fill="x")

        # Tips
        tip = self._card(scroll)
        tips = [
            ("💡", "Discord: Server Settings → Integrations → Webhooks → New Webhook"),
            ("💡", "Telegram: ส่ง /newbot ให้ @BotFather เพื่อรับ Bot Token"),
            ("💡", "Telegram Chat ID: ใช้ @userinfobot หรือ @getmyid_bot ช่วยได้"),
        ]
        for icon, txt in tips:
            r = ctk.CTkFrame(tip, fg_color="transparent")
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(r, text=icon, width=20).pack(side="left")
            ctk.CTkLabel(r, text=txt, font=ctk.CTkFont(size=10),
                         text_color=C["text_muted"], wraplength=360, justify="left").pack(side="left", padx=4)

    # Tab 4 — AI & Sheets
    def _build_tab_ai(self):
        tab = self._tabs.tab("🤖  AI & Sheets")
        tab.configure(fg_color=C["surface"])

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["border"])
        scroll.pack(fill="both", expand=True)

        # API Keys
        keys = self._card(scroll, "🔑  API Keys")

        ctk.CTkLabel(keys, text="Claude API Key",
                     font=ctk.CTkFont(size=11), text_color=C["text_muted"]).pack(anchor="w", pady=(0, 2))
        self.claude_key_var = ctk.StringVar()
        ctk.CTkEntry(keys, textvariable=self.claude_key_var, show="●",
                     placeholder_text="sk-ant-api03-...", height=36,
                     fg_color="#0d1628", border_color=C["border"],
                     text_color=C["text"], placeholder_text_color=C["text_dim"]).pack(fill="x", pady=(0, 8))

        row = ctk.CTkFrame(keys, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1)

        lf = ctk.CTkFrame(row, fg_color="transparent")
        lf.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(lf, text="ชื่อ Google Sheet",
                     font=ctk.CTkFont(size=11), text_color=C["text_muted"]).pack(anchor="w", pady=(0, 2))
        self.sheet_name_var = ctk.StringVar()
        ctk.CTkEntry(lf, textvariable=self.sheet_name_var,
                     placeholder_text="ชื่อไฟล์ใน Google Drive", height=36,
                     fg_color="#0d1628", border_color=C["border"],
                     text_color=C["text"], placeholder_text_color=C["text_dim"]).pack(fill="x")

        rf = ctk.CTkFrame(row, fg_color="transparent")
        rf.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(rf, text="Path ไฟล์ Service Account (.json)",
                     font=ctk.CTkFont(size=11), text_color=C["text_muted"]).pack(anchor="w", pady=(0, 2))
        self.sa_path_var = ctk.StringVar()
        ctk.CTkEntry(rf, textvariable=self.sa_path_var,
                     placeholder_text="C:/path/credentials.json", height=36,
                     fg_color="#0d1628", border_color=C["border"],
                     text_color=C["text"], placeholder_text_color=C["text_dim"]).pack(fill="x")

        # AI Prompt
        prompt_wrap = ctk.CTkFrame(scroll, fg_color=C["surface2"], corner_radius=10,
                                   border_width=1, border_color=C["border"])
        prompt_wrap.pack(fill="x", padx=14, pady=(0, 10))

        ph2 = ctk.CTkFrame(prompt_wrap, fg_color="transparent")
        ph2.pack(fill="x", padx=14, pady=(10, 4))
        ph2.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ph2, text="🤖  AI Prompt  (Claude จะตอบกลับเป็น JSON)",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=C["accent_glow"]).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            prompt_wrap,
            text="  Claude ต้องตอบกลับเป็น JSON: {is_target, score 1-10, persons[], reason}",
            font=ctk.CTkFont(size=10), text_color=C["text_muted"],
        ).pack(anchor="w", padx=14, pady=(0, 4))

        self.prompt_textbox = ctk.CTkTextbox(
            prompt_wrap, height=200,
            fg_color="#0d1628", text_color=C["text"],
            border_color=C["border"], border_width=1,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.prompt_textbox.pack(fill="x", padx=14, pady=(0, 12))
        self.prompt_textbox.insert("1.0", (
            'คุณคือผู้ช่วยบรรณาธิการข่าวการเมืองไทย จงวิเคราะห์ข่าวต่อไปนี้ว่าเกี่ยวข้องกับ "พรรคเพื่อไทย" หรือไม่\n'
            'จงตอบกลับมาเป็นรูปแบบ JSON เท่านั้น:\n'
            '{\n'
            '  "is_target": true หรือ false,\n'
            '  "score": 1-10,\n'
            '  "persons": ["ชื่อบุคคล"],\n'
            '  "reason": "สรุปเหตุผลสั้นๆ"\n'
            '}'
        ))

    # ── Right Panel (Log) ─────────────────────────────────────────────────────

    def _build_right_panel(self):
        right = ctk.CTkFrame(self, fg_color=C["surface"], corner_radius=0)
        right.grid(row=1, column=1, sticky="nsew", padx=0, pady=0)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        # Log header
        log_hdr = ctk.CTkFrame(right, fg_color=C["surface2"], height=46, corner_radius=0)
        log_hdr.grid(row=0, column=0, sticky="ew")
        log_hdr.grid_propagate(False)
        log_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_hdr, text="📋  Activity Log",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=C["text"],
        ).grid(row=0, column=0, padx=16, pady=10, sticky="w")

        btn_row = ctk.CTkFrame(log_hdr, fg_color="transparent")
        btn_row.grid(row=0, column=1, padx=12, pady=8, sticky="e")

        ctk.CTkButton(
            btn_row, text="🗑  ล้าง", width=80, height=30,
            fg_color=C["surface"], hover_color=C["border"],
            border_width=1, border_color=C["border"],
            font=ctk.CTkFont(size=11), corner_radius=6,
            command=self._clear_log,
        ).pack(side="left", padx=(0, 6))

        # Log textbox
        self.log_textbox = ctk.CTkTextbox(
            right, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=C["bg"], text_color=C["text"],
            border_width=0, corner_radius=0,
        )
        self.log_textbox.grid(row=1, column=0, sticky="nsew")

        # Tag colors for log
        self.log_textbox._textbox.tag_configure("success", foreground="#22c55e")
        self.log_textbox._textbox.tag_configure("error",   foreground="#ef4444")
        self.log_textbox._textbox.tag_configure("warn",    foreground="#f59e0b")
        self.log_textbox._textbox.tag_configure("info",    foreground="#60a5fa")
        self.log_textbox._textbox.tag_configure("dim",     foreground="#475569")
        self.log_textbox._textbox.tag_configure("post",    foreground="#a78bfa")

    # ─────────────────────────────────────────────────────────────────────────
    # Settings persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _save_settings(self):
        settings = {
            "email":      self.email_var.get(),
            "password":   self.pass_var.get(),
            "hours":      self.hours_var.get(),
            "loop":       self.loop_var.get(),
            "webhook":    self.webhook_var.get(),
            "tg_token":   self.tg_token_var.get(),
            "tg_chatid":  self.tg_chatid_var.get(),
            "claude_key": self.claude_key_var.get(),
            "sheet_name": self.sheet_name_var.get(),
            "sa_path":    self.sa_path_var.get(),
            "ai_prompt":  self.prompt_textbox.get("1.0", "end").strip(),
        }
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก Settings เรียบร้อย → {self.SETTINGS_FILE}", tag="success")
            # flash save button
            self.save_btn.configure(text="✅ บันทึกแล้ว", fg_color=C["success"])
            self.after(2000, lambda: self.save_btn.configure(text="💾 บันทึก", fg_color=C["green_dark"]))
        except Exception as e:
            self._show_error(f"⚠️ บันทึก Settings ไม่สำเร็จ: {e}")

    def _load_settings(self):
        if not os.path.exists(self.SETTINGS_FILE):
            return
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.email_var.set(s.get("email", ""))
            self.pass_var.set(s.get("password", ""))
            self.hours_var.set(s.get("hours", "6"))
            self.loop_var.set(s.get("loop", "30"))
            self.webhook_var.set(s.get("webhook", ""))
            self.tg_token_var.set(s.get("tg_token", ""))
            self.tg_chatid_var.set(s.get("tg_chatid", ""))
            self.claude_key_var.set(s.get("claude_key", ""))
            self.sheet_name_var.set(s.get("sheet_name", ""))
            self.sa_path_var.set(s.get("sa_path", ""))
            saved_prompt = s.get("ai_prompt", "")
            if saved_prompt:
                self.prompt_textbox.delete("1.0", "end")
                self.prompt_textbox.insert("1.0", saved_prompt)
            self._log(f"🔄 โหลด Settings สำเร็จ ← {self.SETTINGS_FILE}", tag="dim")
        except Exception as e:
            self._log(f"⚠️ โหลด Settings ไม่สำเร็จ: {e}", tag="warn")

    def _save_pages(self):
        pages = [u.strip() for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]
        try:
            with open(self.PAGES_FILE, "w", encoding="utf-8") as f:
                json.dump({"pages": pages}, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก {len(pages)} เพจ → {self.PAGES_FILE}", tag="success")
        except Exception as e:
            self._show_error(f"⚠️ บันทึกเพจไม่สำเร็จ: {e}")

    def _load_pages(self):
        if not os.path.exists(self.PAGES_FILE):
            return
        try:
            with open(self.PAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            pages = data.get("pages", [])
            if pages:
                self.pages_textbox.delete("1.0", "end")
                self.pages_textbox.insert("1.0", "\n".join(pages))
            self._log(f"🔄 โหลด {len(pages)} เพจ ← {self.PAGES_FILE}", tag="dim")
        except Exception as e:
            self._log(f"⚠️ โหลดเพจไม่สำเร็จ: {e}", tag="warn")

    def _save_keywords(self):
        kws = self.keywords_widget.get_keywords()
        try:
            with open(self.KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump({"keywords": kws}, f, ensure_ascii=False, indent=4)
            self._log(f"💾 บันทึก {len(kws)} keywords → {self.KEYWORDS_FILE}", tag="success")
        except Exception as e:
            self._show_error(f"⚠️ บันทึก Keywords ไม่สำเร็จ: {e}")

    def _load_keywords(self):
        if not os.path.exists(self.KEYWORDS_FILE):
            return
        try:
            with open(self.KEYWORDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            kws = data.get("keywords", [])
            if kws:
                self.keywords_widget.set_keywords(kws)
            self._log(f"🔄 โหลด {len(kws)} keywords ← {self.KEYWORDS_FILE}", tag="dim")
        except Exception as e:
            self._log(f"⚠️ โหลด Keywords ไม่สำเร็จ: {e}", tag="warn")

    # ─────────────────────────────────────────────────────────────────────────
    # Timer & Stats
    # ─────────────────────────────────────────────────────────────────────────

    def _update_timer(self):
        if self._session_start_time is not None:
            elapsed = int(time.time() - self._session_start_time)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            self._timer_lbl.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
        else:
            self._timer_lbl.configure(text="00:00:00")
        self.after(1000, self._update_timer)

    def _update_stats(self, posts_delta: int = 0, cycle_delta: int = 0):
        self._posts_found_total += posts_delta
        self._cycle_count       += cycle_delta
        self._posts_lbl.configure(text=f"{self._posts_found_total} โพสต์")
        self._cycle_lbl.configure(text=f"รอบ {self._cycle_count}")

    # ─────────────────────────────────────────────────────────────────────────
    # Test buttons
    # ─────────────────────────────────────────────────────────────────────────

    def _test_discord(self):
        webhook = self.webhook_var.get().strip()
        if not webhook:
            self._show_error("⚠️ กรุณากรอก Webhook URL ก่อนทดสอบ")
            return
        def _do():
            try:
                d = DiscordNotifier(webhook)
                d.send_start(
                    page_count=len([u for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]),
                    keyword_count=len(self.keywords_widget.get_keywords()),
                    loop_min=int(self.loop_var.get() or "30"),
                    hours_back=int(self.hours_var.get() or "6"),
                )
                self._log("✅ ส่ง Test Discord สำเร็จ — ตรวจสอบใน Discord Channel", tag="success")
            except Exception as e:
                self._log(f"❌ Test Discord ล้มเหลว: {e}", tag="error")
        threading.Thread(target=_do, daemon=True).start()

    def _test_telegram(self):
        tg_token  = self.tg_token_var.get().strip()
        tg_chatid = self.tg_chatid_var.get().strip()
        if not tg_token or not tg_chatid:
            self._show_error("⚠️ กรุณากรอก Bot Token และ Chat ID ก่อนทดสอบ")
            return
        def _do():
            try:
                tg = TelegramNotifier(tg_token, tg_chatid)
                tg.send_start(
                    page_count=len([u for u in self.pages_textbox.get("1.0", "end").splitlines() if u.strip()]),
                    keyword_count=len(self.keywords_widget.get_keywords()),
                    loop_min=int(self.loop_var.get() or "30"),
                    hours_back=int(self.hours_var.get() or "6"),
                )
                self._log("✅ ส่ง Test Telegram สำเร็จ — ตรวจสอบใน Telegram Chat", tag="success")
            except Exception as e:
                self._log(f"❌ Test Telegram ล้มเหลว: {e}", tag="error")
        threading.Thread(target=_do, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Log
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, message: str, tag: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put((f"[{ts}] {message}", tag))

    def _poll_log_queue(self):
        # Map emoji prefixes to tags
        TAG_MAP = {
            "✅": "success", "🟢": "success", "💾": "success",
            "❌": "error",   "🚨": "error",
            "⚠️": "warn",   "⏩": "warn",
            "🔎": "info",   "🔍": "info",  "🔄": "info",
            "📨": "post",   "🎯": "post",   "📌": "post",
        }
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple):
                    msg, tag = item
                else:
                    msg, tag = item, ""

                # Auto-detect tag from emoji if not provided
                if not tag:
                    for emoji, t in TAG_MAP.items():
                        if emoji in msg:
                            tag = t
                            break

                self.log_textbox.configure(state="normal")
                tb = self.log_textbox._textbox
                tb.insert("end", msg + "\n", tag if tag else "")
                self.log_textbox.see("end")
                self.log_textbox.configure(state="disabled")
        except Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Controls
    # ─────────────────────────────────────────────────────────────────────────

    def _on_start(self):
        email     = self.email_var.get().strip()
        password  = self.pass_var.get().strip()
        pages_raw = self.pages_textbox.get("1.0", "end").strip()
        webhook   = self.webhook_var.get().strip()
        tg_token  = self.tg_token_var.get().strip()
        tg_chatid = self.tg_chatid_var.get().strip()

        if tg_token:
            self._tg_listener = TelegramListener(tg_token)
            self._tg_listener.start()

        try:
            hours    = int(self.hours_var.get().strip())
            loop_min = int(self.loop_var.get().strip())
        except ValueError:
            self._show_error("⚠️ กรุณากรอกตัวเลขในช่อง Timeframe และ Loop")
            return

        if not email or not password:
            self._show_error("⚠️ กรุณากรอก Email และ Password ในแท็บ 🔐 บัญชี")
            return

        page_urls = [u.strip() for u in pages_raw.splitlines() if u.strip()]
        if not page_urls:
            self._show_error("⚠️ กรุณากรอก URL เพจอย่างน้อย 1 เพจ ในแท็บ 🎯 เพจ & คำค้น")
            return

        if not webhook and not (tg_token and tg_chatid):
            self._show_error("⚠️ กรุณากรอก Discord Webhook หรือ Telegram Bot+Chat ID ในแท็บ 📡 แจ้งเตือน")
            return

        keywords       = self.keywords_widget.get_keywords()
        discord        = DiscordNotifier(webhook)
        tg             = TelegramNotifier(tg_token, tg_chatid)
        claude_key     = self.claude_key_var.get().strip()
        ai_prompt      = self.prompt_textbox.get("1.0", "end").strip()
        sheet_name     = self.sheet_name_var.get().strip()
        sa_path        = self.sa_path_var.get().strip()
        ai_analyzer    = ClaudeAnalyzer(claude_key, ai_prompt, self._log) if claude_key else None
        sheets_manager = GoogleSheetsManager(sa_path, sheet_name, self._log) if sa_path and sheet_name else None

        self._scraper = FacebookScraper(
            self._log, self._db, discord, tg,
            ai_analyzer=ai_analyzer,
            sheets_manager=sheets_manager,
            on_cookies_saved=self._enable_hide_btn,
        )

        # Update UI state
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")
        self._set_status("running")
        self._session_start_time = time.time()
        self._posts_found_total  = 0
        self._cycle_count        = 0
        self._is_running         = True

        self._log(f"🚀 เริ่มทำงาน | {len(page_urls)} เพจ | Keywords: {keywords or 'ทั้งหมด'} | Loop: {loop_min}m", tag="success")

        self._scraper_thread = threading.Thread(
            target=self._scraper.run,
            args=(email, password, page_urls, keywords, hours, loop_min),
            daemon=True,
            name="ScraperThread",
        )
        self._scraper_thread.start()
        self.after(500, self._check_thread_alive)

    def _on_stop(self):
        if self._scraper:
            self._scraper.stop()
        if hasattr(self, "_tg_listener"):
            self._tg_listener.stop()
        self._log("🛑 ส่งสัญญาณหยุด Scraper แล้ว...", tag="warn")
        self._reset_ui()

    def _on_resume(self):
        if self._scraper:
            self._scraper.resume()
            self._log("▶️ กด Resume — Scraper กลับมาทำงานแล้ว", tag="info")
            self._set_status("running")

    def _enable_hide_btn(self):
        self.hide_btn.configure(state="normal")
        self._log("🍪 Cookies บันทึกแล้ว — สามารถซ่อน Browser ได้", tag="success")

    def _on_hide_browser(self):
        drv = self._scraper.driver if self._scraper else None
        if drv:
            try:
                drv.set_window_position(-2000, -2000)
                self._log("🙈 ซ่อน Browser ไปทำงานเบื้องหลังแล้ว", tag="info")
                self.hide_btn.configure(text="👁 แสดง", command=self._on_show_browser)
            except Exception as e:
                self._log(f"⚠️ ซ่อน Browser ไม่สำเร็จ: {e}", tag="warn")

    def _on_show_browser(self):
        drv = self._scraper.driver if self._scraper else None
        if drv:
            try:
                drv.set_window_position(0, 0)
                drv.maximize_window()
                self._log("👁 แสดง Browser กลับมาแล้ว", tag="info")
                self.hide_btn.configure(text="🙈 ซ่อน", command=self._on_hide_browser)
            except Exception as e:
                self._log(f"⚠️ แสดง Browser ไม่สำเร็จ: {e}", tag="warn")

    def _check_thread_alive(self):
        if self._scraper_thread and not self._scraper_thread.is_alive():
            self._reset_ui()
            self._log("✅ Scraper Thread สิ้นสุดการทำงาน", tag="dim")
        else:
            self.after(500, self._check_thread_alive)

    def _set_status(self, state: str):
        if state == "running":
            self._status_dot.configure(text_color=C["success"])
            self._status_lbl.configure(text="กำลังทำงาน", text_color=C["success"])
        elif state == "paused":
            self._status_dot.configure(text_color=C["warning"])
            self._status_lbl.configure(text="รอ Resume", text_color=C["warning"])
        else:
            self._status_dot.configure(text_color=C["danger"])
            self._status_lbl.configure(text="หยุดทำงาน", text_color=C["text_muted"])

    def _reset_ui(self):
        self._is_running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.hide_btn.configure(state="disabled", text="🙈 ซ่อน", command=self._on_hide_browser)
        self._session_start_time = None
        self._set_status("stopped")

    def _show_error(self, msg: str):
        self._log(msg, tag="error")
        dialog = ctk.CTkToplevel(self)
        dialog.title("ข้อผิดพลาด")
        dialog.geometry("400x140")
        dialog.configure(fg_color=C["surface"])
        dialog.grab_set()
        dialog.lift()

        ctk.CTkLabel(
            dialog, text=msg, wraplength=360,
            font=ctk.CTkFont(size=12), text_color=C["warning"],
        ).pack(pady=(24, 12), padx=20)

        ctk.CTkButton(
            dialog, text="ตกลง", width=100, height=34,
            fg_color=C["accent"], hover_color=C["accent_dark"],
            corner_radius=8, command=dialog.destroy,
        ).pack()

    def on_close(self):
        if self._scraper:
            self._scraper.stop()
        self._db.close()
        self.destroy()
