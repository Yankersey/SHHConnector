"""Desktop integration for macOS, Linux and Linux running inside WSL."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import webbrowser


def downloads_directory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import uuid

        identifier = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(identifier)
        path = ctypes.c_void_p()
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
        result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
        if result != 0:
            raise OSError("无法获取 Windows 下载文件夹")
        try:
            return Path(ctypes.wstring_at(path))
        finally:
            ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.ole32.CoTaskMemFree(path)
    return Path.home() / "Downloads"


def is_wsl():
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def open_workspace(url):
    # The argument crosses a Windows command interpreter. Only our own loopback
    # URL format is accepted; no shell fragments or arbitrary external URL.
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/#token=[0-9a-f]{64}", url):
        return False
    if is_wsl() and shutil.which("powershell.exe"):
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              "Start-Process -FilePath '" + url + "'"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    return webbrowser.open(url)


def reveal_local(path):
    path = Path(path).resolve()
    if is_wsl() and shutil.which("explorer.exe") and shutil.which("wslpath"):
        folder = path if path.is_dir() else path.parent
        converted = subprocess.run(["wslpath", "-w", str(folder)], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, check=True, timeout=4).stdout.strip()
        subprocess.Popen(["explorer.exe", converted], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
        return True
    if os.name == "nt":
        folder = path if path.is_dir() else path.parent
        os.startfile(str(folder))
        return True
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
        return True
    return False
