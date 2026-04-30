"""
ssl_helper.py
─────────────
Helper สำหรับดึง CA bundle path ที่ใช้งานได้จริง
รองรับทั้งโหมด Source Code และ PyInstaller (.exe)
"""
import os
import sys

# Cache the CA bundle path to avoid repeated lookups
_ca_bundle_cache = None


def get_ca_bundle() -> str | bool:
    """
    คืนค่า path ของ CA bundle ที่ใช้งานได้จริง
    - ถ้าหาเจอ → คืน path string
    - ถ้าหาไม่เจอ → คืน True (ให้ requests ใช้ system certs)

    ลำดับความสำคัญ:
    1. SSL_CERT_FILE env var (set โดย main.py _fix_ssl_cert)
    2. REQUESTS_CA_BUNDLE env var
    3. certifi.where() ที่ไม่ใช่ _MEI temp path
    4. True (system certs fallback)
    """
    global _ca_bundle_cache

    # Return cached value if available
    if _ca_bundle_cache is not None:
        return _ca_bundle_cache

    # 1. Check env vars ก่อน (set โดย _fix_ssl_cert ใน main.py)
    for env_key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(env_key, "")
        if path and os.path.isfile(path) and "_MEI" not in path:
            _ca_bundle_cache = path
            return path

    # 2. ลอง certifi โดยตรง
    try:
        import certifi
        path = certifi.where()
        if path and os.path.isfile(path) and "_MEI" not in path:
            _ca_bundle_cache = path
            return path
    except Exception:
        pass

    # 3. สำหรับ PyInstaller: ลองหาจาก sys._MEIPASS
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        mei_pass = sys._MEIPASS
        candidates = [
            os.path.join(mei_pass, "certifi", "cacert.pem"),
            os.path.join(mei_pass, "cacert.pem"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                _ca_bundle_cache = candidate
                return candidate

    # 4. Fallback: ให้ requests ใช้ system certs
    _ca_bundle_cache = True
    return True
