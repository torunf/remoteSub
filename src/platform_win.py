"""Windows side of RemoteSub: windows, capture, overlay styling, hotkeys and installing/configuring
chiaki-ng and Ollama. platform_mac.py provides the same functions for macOS."""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import tempfile
import threading
import time
import winreg

import mss
from PIL import Image

from netutil import download, github_latest_asset

APP_DIR = os.path.join(os.environ["APPDATA"], "RemoteSub")
LEGACY_APP_DIR = os.path.join(os.environ["APPDATA"], "PSAltyazi")  # the app's earlier name
SHOTS_HINT = r"%APPDATA%\RemoteSub\shots"
HOTKEY_LABEL = "Ctrl+Alt"
UI_FONT = "Segoe UI"
FONT_FILES = [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"]
NEEDS_PERMISSIONS = False

NO_WINDOW = subprocess.CREATE_NO_WINDOW
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def init_process():
    # Physical pixels everywhere: window rects, screen capture and Tk geometry must agree.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


# ---------------------------------------------------------------- chiaki-ng window

_process_names = {}


def _process_name(pid):
    if pid not in _process_names:
        name = ""
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf, size = ctypes.create_unicode_buffer(260), wt.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            kernel32.CloseHandle(h)
        _process_names[pid] = name
    return _process_names[pid]


def client_rect(hwnd):
    """Client area of a window in screen coordinates: (x, y, w, h)."""
    r = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right, r.bottom


def find_chiaki_window():
    """The largest visible chiaki.exe window, which is the stream window while playing."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _process_name(pid.value) == "chiaki.exe":
                rect = client_rect(hwnd)
                if rect and rect[2] > 100 and rect[3] > 100:
                    found.append((rect[2] * rect[3], hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return max(found)[1] if found else None


def window_valid(hwnd):
    return bool(user32.IsWindow(hwnd))


user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND


def chiaki_visible_at(hwnd, x, y):
    """Screen capture reads whatever is on top, so only read when chiaki-ng is the window at (x, y).
    Our click-through overlay is skipped by WindowFromPoint."""
    return user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), 2) == hwnd  # GA_ROOT


class Capturer:
    """Grabs a screen rectangle; created on the thread that uses it (mss is per-thread)."""

    def __init__(self):
        self._mss = mss.MSS() if hasattr(mss, "MSS") else mss.mss()

    def grab(self, hwnd, box):
        x, y, w, h = box
        shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})
        return Image.frombytes("RGB", shot.size, shot.rgb)


def grab_screen(box):
    """Everything visible in a screen rectangle (for screenshots that include the overlay)."""
    x, y, w, h = box
    with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as grabber:
        shot = grabber.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.rgb)


# ---------------------------------------------------------------- overlay window

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11


def prepare_overlay(win):
    """Make a Tk toplevel click-through, never focused and invisible to screen capture (so the
    OCR never reads our own text back). Returns the handle the other overlay functions take."""
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id())
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    set_capturable(hwnd, False)
    return hwnd


def make_floating(win):
    pass  # "-topmost" already puts it over a full-screen chiaki-ng on Windows


def set_capturable(hwnd, capturable):
    return user32.SetWindowDisplayAffinity(hwnd, 0 if capturable else WDA_EXCLUDEFROMCAPTURE)


def round_corners(hwnd, width, height, radius):
    user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius), True)


def font_px(hwnd, points):
    return round(points * (user32.GetDpiForWindow(hwnd) or 96) / 72)


# ---------------------------------------------------------------- hotkeys

def start_hotkeys(keys, on_hotkey, log):
    """Global hotkeys (Ctrl+Alt+letter). keys: {id: letter}."""
    threading.Thread(target=_hotkey_loop, args=(keys, on_hotkey, log), daemon=True).start()


def _hotkey_loop(keys, on_hotkey, log):
    """RegisterHotKey delivers to the registering thread's message loop, so this owns one."""
    mod = 0x0002 | 0x0001 | 0x4000  # Ctrl + Alt, no auto-repeat
    for hid, letter in keys.items():
        if not user32.RegisterHotKey(None, hid, mod, ord(letter.upper())):
            log(f"could not register hotkey {letter}")
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            on_hotkey(msg.wParam)


# ---------------------------------------------------------------- chiaki-ng install and settings

CHIAKI_KEY = r"Software\Chiaki\Chiaki"
UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]
CHIAKI_SETTINGS_SUMMARY = "d3d11va çözücü · otomatik bağlanma"


def _winget_available():
    try:
        return run(["winget", "--version"], timeout=30).returncode == 0
    except Exception:
        return False


def _winget_install(package_id):
    run(["winget", "install", "--id", package_id, "-e", "--silent", "--disable-interactivity",
         "--accept-package-agreements", "--accept-source-agreements"], timeout=3600)


def _uninstall_location(name_part):
    for hive, key in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, key)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                name = winreg.QueryValueEx(sub, "DisplayName")[0]
            except OSError:
                continue
            if name_part.lower() in str(name).lower():
                for value in ("InstallLocation", "DisplayIcon"):
                    try:
                        path = str(winreg.QueryValueEx(sub, value)[0]).strip('"').split(",")[0]
                    except OSError:
                        continue
                    if path:
                        return path if os.path.isdir(path) else os.path.dirname(path)
    return None


def find_chiaki():
    candidates = [os.path.join(os.environ.get(v, ""), "chiaki-ng", "chiaki.exe")
                  for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    candidates.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "chiaki-ng", "chiaki.exe"))
    location = _uninstall_location("chiaki-ng")
    if location:
        candidates.insert(0, os.path.join(location, "chiaki.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def install_chiaki(report):
    if _winget_available():
        report("chiaki-ng winget ile kuruluyor… (izin penceresi çıkabilir)", None)
        _winget_install("StreetPea.chiaki-ng")
    if find_chiaki():
        return
    report("chiaki-ng indiriliyor…", None)
    url, name = github_latest_asset("streetpea/chiaki-ng",
                                    lambda n: n.endswith(".exe") and "win_x64" in n and "installer" in n)
    if not url:
        raise RuntimeError("chiaki-ng kurulum dosyası bulunamadı")
    path = os.path.join(tempfile.gettempdir(), name)
    download(url, path, report)
    report("chiaki-ng kuruluyor… (izin penceresi çıkabilir)", None)
    subprocess.run([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"])


def chiaki_running():
    return "chiaki.exe" in run(["tasklist", "/FI", "IMAGENAME eq chiaki.exe", "/NH"]).stdout.lower()


def launch_chiaki(path, extra_args=()):
    subprocess.Popen([path, *extra_args], creationflags=NO_WINDOW)


def close_chiaki():
    """Ask chiaki-ng to quit (it would otherwise keep, and later save, its old settings)."""
    run(["taskkill", "/IM", "chiaki.exe"])
    for _ in range(20):
        if not chiaki_running():
            return
        time.sleep(0.5)
    run(["taskkill", "/F", "/IM", "chiaki.exe"])
    time.sleep(1)


def chiaki_hosts():
    """Consoles paired in chiaki-ng: [{"nickname", "mac"}] (mac as stored, compared opaquely)."""
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHIAKI_KEY + r"\registered_hosts")
    except OSError:
        return []
    hosts, i = [], 1
    while True:
        try:
            sub = winreg.OpenKey(root, str(i))
        except OSError:
            break
        try:
            hosts.append({"nickname": winreg.QueryValueEx(sub, "server_nickname")[0],
                          "mac": winreg.QueryValueEx(sub, "server_mac")})
        except OSError:
            pass
        i += 1
    return hosts


def apply_chiaki_settings(nickname):
    """Settings RemoteSub relies on. Only call while chiaki-ng is closed: it keeps its own copy.

    - hw_decoder d3d11va: chiaki-ng's automatic choice (Vulkan) showed a corrupted, blue,
      flickering picture on a dual-GPU PC; d3d11va decodes on the GPU reliably on Windows.
    - auto_connect_mac: chiaki-ng connects to this console by itself once it sees it, which is
      how RemoteSub opens the stream in one step (its `stream` command stalls in 1.9.9).
    """
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, CHIAKI_KEY + r"\settings")
    try:
        decoder = winreg.QueryValueEx(key, "hw_decoder")[0]
    except OSError:
        decoder = None
    if decoder in (None, "", "auto", "vulkan"):
        winreg.SetValueEx(key, "hw_decoder", 0, winreg.REG_SZ, "d3d11va")
    host = next((h for h in chiaki_hosts() if h["nickname"] == nickname), None)
    if host:
        value, kind = host["mac"]
        winreg.SetValueEx(key, "auto_connect_mac", 0, kind, value)


def chiaki_settings_ok(nickname):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHIAKI_KEY + r"\settings")
        decoder = winreg.QueryValueEx(key, "hw_decoder")[0]
        auto_mac = winreg.QueryValueEx(key, "auto_connect_mac")[0]
    except OSError:
        return False
    host = next((h for h in chiaki_hosts() if h["nickname"] == nickname), None)
    return decoder == "d3d11va" and host is not None and auto_mac == host["mac"][0]


# ---------------------------------------------------------------- Ollama

OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"


def _ollama_exe(name="ollama.exe"):
    path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", name)
    if os.path.isfile(path):
        return path
    return shutil.which(name) if name == "ollama.exe" else None


def ollama_installed():
    return bool(_ollama_exe())


def install_ollama(report):
    if _winget_available():
        report("Ollama winget ile kuruluyor…", None)
        _winget_install("Ollama.Ollama")
    if ollama_installed():
        return
    path = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
    download(OLLAMA_SETUP_URL, path, report)
    report("Ollama kuruluyor…", None)
    subprocess.run([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])


def launch_ollama():
    app = _ollama_exe("ollama app.exe")
    if app:
        subprocess.Popen([app], creationflags=NO_WINDOW)
    elif _ollama_exe():
        subprocess.Popen([_ollama_exe(), "serve"], creationflags=NO_WINDOW)


def ollama_host_setting():
    """OLLAMA_HOST from this process or, if set after we started, from the user's environment."""
    host = os.environ.get("OLLAMA_HOST")
    if not host:
        try:
            host = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment"), "OLLAMA_HOST")[0]
        except OSError:
            host = None
    return host


def model_memory_gb():
    """Largest dedicated video memory among the display adapters, in GB."""
    best = 0
    try:
        cls = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}")
    except OSError:
        return 0
    for i in range(winreg.QueryInfoKey(cls)[0]):
        try:
            value = winreg.QueryValueEx(winreg.OpenKey(cls, winreg.EnumKey(cls, i)),
                                        "HardwareInformation.qwMemorySize")[0]
        except OSError:
            continue
        if isinstance(value, bytes):
            value = int.from_bytes(value, "little")
        best = max(best, int(value))
    return best / 2 ** 30


# 12b needs ~8 GB of VRAM; on Windows the model runs on the graphics card.
LARGE_MODEL_MIN_GB = 10


# ---------------------------------------------------------------- updates (not yet on Windows)

SUPPORTS_UPDATE = False
UPDATE_ASSET_SUFFIX = ".exe"


def install_update(path, report):
    raise NotImplementedError


def relaunch_after_update():
    pass


# ---------------------------------------------------------------- permissions (none needed)

def capture_allowed():
    return True


def permissions_ok():
    return True, ""


def request_permissions(report):
    pass
