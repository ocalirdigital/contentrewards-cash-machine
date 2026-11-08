"""
Tarayici ve masaustu yardimci fonksiyonlari.
Acik kaynak surumu icin kisisel veri icermez.
"""
import subprocess
import webbrowser
from pathlib import Path


def open_tiktok_upload(video_path: str = ""):
    if video_path:
        vpath = Path(video_path).resolve()
        if vpath.exists():
            subprocess.Popen(["explorer.exe", f"/select,{vpath}"])
    webbrowser.open("https://www.tiktok.com/tiktokstudio/upload")
    return True
