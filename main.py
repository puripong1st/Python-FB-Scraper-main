"""
main.py
━━━━━━━
Entry point — รันโปรแกรมจากไฟล์นี้ไฟล์เดียว
"""
import os
import sys


def _fix_ssl_cert():
    """
    แก้ปัญหา SSL/certifi ทั้งในโหมด Source Code และ PyInstaller (.exe)
    
    ปัญหา: PyInstaller แตกไฟล์ไปที่ _MEIxxxxxx (temp) ซึ่งจะถูกลบหลัง exe เปิด
    วิธีแก้: คัดลอก cacert.pem ไปไว้ข้างๆ exe แบบถาวร แล้ว patch certifi.where()
    """
    try:
        import certifi
        import shutil

        stable_cert = None

        if getattr(sys, "frozen", False):
            # ── โหมด PyInstaller exe ──────────────────────────────────────────
            exe_dir = os.path.dirname(sys.executable)
            stable_cert = os.path.join(exe_dir, "cacert.pem")

            # คัดลอกจาก bundle ครั้งแรก
            if not os.path.isfile(stable_cert):
                mei_pass = getattr(sys, "_MEIPASS", "")
                candidates = [
                    os.path.join(mei_pass, "certifi", "cacert.pem"),
                    os.path.join(mei_pass, "cacert.pem"),
                ]
                for src in candidates:
                    if os.path.isfile(src):
                        try:
                            shutil.copy2(src, stable_cert)
                            break
                        except Exception:
                            pass

            # Fallback: ดึงจาก package ใน bundle
            if not os.path.isfile(stable_cert):
                try:
                    raw_path = certifi.where()
                    if os.path.isfile(raw_path):
                        shutil.copy2(raw_path, stable_cert)
                except Exception:
                    pass

        else:
            # ── โหมด Source Code ──────────────────────────────────────────────
            raw_path = certifi.where()

            # ตรวจว่า path ชี้ไปที่ _MEI temp หรือไม่
            if os.path.isfile(raw_path) and "_MEI" not in raw_path:
                stable_cert = raw_path
            else:
                # หา path จริงของ package
                import importlib.util
                spec = importlib.util.find_spec("certifi")
                if spec and spec.origin:
                    pkg_dir = os.path.dirname(spec.origin)
                    candidate = os.path.join(pkg_dir, "cacert.pem")
                    if os.path.isfile(candidate):
                        stable_cert = candidate

        # ── Apply patch ───────────────────────────────────────────────────────
        if stable_cert and os.path.isfile(stable_cert):
            # Monkey-patch certifi.where() ให้ทุก library ที่ใช้ certifi ได้ path ที่ถูก
            certifi.where = lambda: stable_cert
            os.environ["SSL_CERT_FILE"]      = stable_cert
            os.environ["REQUESTS_CA_BUNDLE"] = stable_cert
            print(f"[SSL] ✅ cert path: {stable_cert}")
        else:
            print("[SSL] ⚠️ ไม่พบ cacert.pem — ใช้ system certs แทน")

    except Exception as e:
        print(f"[SSL] _fix_ssl_cert warning: {e}")


# ต้องรันก่อน import อื่นๆ ทั้งหมด
_fix_ssl_cert()

from ui.app import ScraperApp


if __name__ == "__main__":
    app = ScraperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
