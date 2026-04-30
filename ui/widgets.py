"""
ui/widgets.py
━━━━━━━━━━━━━
KeywordTagInput — widget ใส่ keyword เป็น chip/tag พร้อมลบทีละอัน
"""

import tkinter as tk
import customtkinter as ctk


class KeywordTagInput(ctk.CTkFrame):
    CHIP_BG     = "#1e3a5f"
    CHIP_FG     = "#7ec8f0"
    CHIP_BTN_FG = "#5ba4cf"
    CHIP_HOVER  = "#2a4f7a"

    def __init__(self, master, defaults: list | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self._tags: list[str]               = []
        self._chip_widgets: dict[str, tk.Frame] = {}
        self._build()
        for kw in (defaults or []):
            self._add_tag(kw)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        chip_outer = ctk.CTkFrame(self, fg_color=("gray85", "#1a1a2e"), corner_radius=8)
        chip_outer.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        chip_outer.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(chip_outer, bg="#1a1a2e", bd=0, highlightthickness=0, height=46)
        self._canvas.pack(fill="x", expand=True, padx=4, pady=(4, 0))

        self._hbar = tk.Scrollbar(chip_outer, orient="horizontal", command=self._canvas.xview)
        self._hbar.pack(fill="x", padx=4, pady=(0, 4))
        self._canvas.configure(xscrollcommand=self._hbar.set)

        self._chip_area = tk.Frame(self._canvas, bg="#1a1a2e")
        self._canvas_window = self._canvas.create_window((0, 0), window=self._chip_area, anchor="nw")

        self._chip_area.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.xview_scroll(int(-e.delta / 60), "units"),
        )

        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 6))
        input_row.grid_columnconfigure(0, weight=1)

        self._entry_var = ctk.StringVar()
        self._entry = ctk.CTkEntry(
            input_row,
            textvariable=self._entry_var,
            placeholder_text="พิมพ์ keyword แล้วกด Enter หรือ ＋",
            height=34,
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._entry.bind("<Return>", lambda e: self._on_add())

        add_btn = ctk.CTkButton(
            input_row, text="＋ เพิ่ม", width=80, height=34,
            fg_color="#1877F2", hover_color="#145db8", command=self._on_add,
        )
        add_btn.grid(row=0, column=1)

        clear_btn = ctk.CTkButton(
            input_row, text="ล้างทั้งหมด", width=90, height=34,
            fg_color="gray30", hover_color="gray20", command=self._clear_all,
        )
        clear_btn.grid(row=0, column=2, padx=(6, 0))

    def _on_add(self):
        raw = self._entry_var.get().strip()
        if not raw:
            return
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        for p in parts:
            self._add_tag(p)
        self._entry_var.set("")

    def _add_tag(self, text: str):
        if not text or text in self._tags:
            return
        self._tags.append(text)
        self._render_chip(text)

    def _render_chip(self, text: str):
        chip = tk.Frame(self._chip_area, bg=self.CHIP_BG, bd=0, padx=6, pady=3)
        chip.pack(side="left", padx=3, pady=3)

        lbl = tk.Label(chip, text=text, bg=self.CHIP_BG, fg=self.CHIP_FG, font=("Segoe UI", 10))
        lbl.pack(side="left")

        def remove():
            self._remove_tag(text, chip)

        btn = tk.Label(
            chip, text=" ✕", bg=self.CHIP_BG, fg=self.CHIP_BTN_FG,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: remove())
        btn.bind("<Enter>", lambda e: btn.config(fg="#ff6b6b"))
        btn.bind("<Leave>", lambda e: btn.config(fg=self.CHIP_BTN_FG))

        self._chip_widgets[text] = chip
        self._chip_area.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.xview_moveto(1.0)

    def _remove_tag(self, text: str, chip_frame):
        if text in self._tags:
            self._tags.remove(text)
        chip_frame.destroy()
        self._chip_widgets.pop(text, None)

    def _clear_all(self):
        for chip in list(self._chip_widgets.values()):
            chip.destroy()
        self._chip_widgets.clear()
        self._tags.clear()

    def get_keywords(self) -> list:
        return list(self._tags)

    def set_keywords(self, keywords: list):
        self._clear_all()
        for kw in keywords:
            self._add_tag(kw)
