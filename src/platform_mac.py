"""macOS side of RemoteSub: the same functions as platform_win.py.

Differences from Windows worth knowing:
- The chiaki-ng window is captured by its window id (CGWindowListCreateImage), so other windows
  on top don't matter and our overlay is never in the picture.
- OCR is Apple's Vision framework (see ocr_engine.py).
- chiaki-ng's settings live in the com.chiaki.Chiaki preferences domain (Qt's QSettings).
- Screen Recording permission is required; Accessibility only for the global hotkeys.
- Everything touching AppKit runs on the main (Tk) thread; macOS 26+ enforces this.
"""
import glob
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time

import AppKit
import Quartz
from PIL import Image

from netutil import download, github_latest_asset

APP_DIR = os.path.expanduser("~/Library/Application Support/RemoteSub")
LEGACY_APP_DIR = None
SHOTS_HINT = "~/Library/Application Support/RemoteSub/shots"
HOTKEY_LABEL = "Ctrl+⌥"
UI_FONT = "Helvetica Neue"
FONT_FILES = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf"]
NEEDS_PERMISSIONS = True

TITLE_BAR = 28  # points; chiaki-ng's window bounds include the title bar unless it is full screen


def init_process():
    pass


def run(cmd, timeout=None, text=True):
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout)


# ---------------------------------------------------------------- chiaki-ng window

CHIAKI_OWNERS = {"chiaki-ng", "chiaki"}


def _window_infos(option, window_id=Quartz.kCGNullWindowID):
    return Quartz.CGWindowListCopyWindowInfo(option, window_id) or []


def _window_info(window_id):
    for info in _window_infos(Quartz.kCGWindowListOptionIncludingWindow, window_id):
        if int(info["kCGWindowNumber"]) == window_id:
            return info
    return None


def find_chiaki_window():
    """The largest on-screen chiaki-ng window (the stream window while playing), by window id."""
    best = None
    option = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for info in _window_infos(option):
        if str(info.get("kCGWindowOwnerName", "")).lower() not in CHIAKI_OWNERS:
            continue
        if info.get("kCGWindowLayer", 1) != 0:
            continue
        b = info["kCGWindowBounds"]
        area = b["Width"] * b["Height"]
        if b["Width"] > 100 and b["Height"] > 100 and (best is None or area > best[0]):
            best = (area, int(info["kCGWindowNumber"]))
    return best[1] if best else None


def window_valid(window_id):
    return _window_info(window_id) is not None


def _display_bounds():
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    out = []
    for display in (ids or [])[:count]:
        r = Quartz.CGDisplayBounds(display)
        out.append((round(r.origin.x), round(r.origin.y), round(r.size.width), round(r.size.height)))
    return out


def client_rect(window_id):
    """Content area in global points (top-left origin, the same space Tk uses)."""
    info = _window_info(window_id)
    if not info:
        return None
    b = info["kCGWindowBounds"]
    x, y, w, h = round(b["X"]), round(b["Y"]), round(b["Width"]), round(b["Height"])
    if (x, y, w, h) not in _display_bounds():  # windowed: skip the title bar
        y, h = y + TITLE_BAR, h - TITLE_BAR
    return x, y, w, h


def chiaki_visible_at(window_id, x, y):
    # We capture the window itself, so windows on top don't matter; only an off-screen
    # (minimised, other Space) window has nothing to read.
    info = _window_info(window_id)
    return bool(info and info.get("kCGWindowIsOnscreen", True))


def _to_pil(cg_image):
    width, height = Quartz.CGImageGetWidth(cg_image), Quartz.CGImageGetHeight(cg_image)
    stride = Quartz.CGImageGetBytesPerRow(cg_image)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg_image))
    return Image.frombuffer("RGBA", (width, height), bytes(data), "raw", "BGRA", stride, 1).convert("RGB")


def _capture_with_screencapturekit(window_id, box):
    """Fallback for macOS versions where CGWindowListCreateImage no longer returns images."""
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    done, out = threading.Event(), {}

    def got_content(content, error):
        try:
            window = next((w for w in (content.windows() if content else []) if w.windowID() == window_id), None)
            if window is None:
                done.set()
                return
            frame = window.frame()
            x, y, w, h = box
            config = SCK.SCStreamConfiguration.alloc().init()
            config.setSourceRect_(Quartz.CGRectMake(x - frame.origin.x, y - frame.origin.y, w, h))
            config.setWidth_(int(w * 2))
            config.setHeight_(int(h * 2))
            config.setShowsCursor_(False)
            content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)

            def got_image(image, err):
                out["image"] = image
                done.set()

            SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, got_image)
        except Exception:
            done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
    done.wait(2.0)
    return out.get("image")


class Capturer:
    def grab(self, window_id, box):
        x, y, w, h = box
        image = None
        try:
            image = Quartz.CGWindowListCreateImage(
                Quartz.CGRectMake(x, y, w, h), Quartz.kCGWindowListOptionIncludingWindow,
                window_id, Quartz.kCGWindowImageBoundsIgnoreFraming)
        except Exception:
            pass
        if image is None:
            image = _capture_with_screencapturekit(window_id, box)
        return _to_pil(image) if image is not None else Image.new("RGB", (max(1, w), max(1, h)))


def grab_screen(box):
    x, y, w, h = box
    image = Quartz.CGWindowListCreateImage(Quartz.CGRectMake(x, y, w, h), Quartz.kCGWindowListOptionOnScreenOnly,
                                           Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
    return _to_pil(image) if image is not None else Image.new("RGB", (max(1, w), max(1, h)))


# ---------------------------------------------------------------- overlay window

def _nswindow(win):
    """The NSWindow behind a Tk toplevel, found by giving it a unique title."""
    marker = f"RemoteSubOverlay-{id(win)}"
    win.title(marker)
    win.update_idletasks()
    win.update()
    for ns in AppKit.NSApp().windows():
        if ns.title() == marker:
            return ns
    return None


def prepare_overlay(win):
    ns = _nswindow(win)
    if ns is None:
        return None
    ns.setLevel_(AppKit.NSStatusWindowLevel)
    ns.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                              | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                              | AppKit.NSWindowCollectionBehaviorStationary)
    ns.setHasShadow_(False)
    ns.setOpaque_(False)
    ns.setBackgroundColor_(AppKit.NSColor.clearColor())
    ns.setIgnoresMouseEvents_(True)
    set_capturable(ns, False)
    return ns


def make_floating(win):
    """Let an interactive Tk window (the region picker) appear over a full-screen chiaki-ng."""
    ns = _nswindow(win)
    if ns is not None:
        ns.setLevel_(AppKit.NSStatusWindowLevel)
        ns.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                  | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)


def set_capturable(ns, capturable):
    if ns is None:
        return False
    ns.setSharingType_(AppKit.NSWindowSharingReadOnly if capturable else AppKit.NSWindowSharingNone)
    return True


def round_corners(ns, width, height, radius):
    if ns is None:
        return
    ns.setIgnoresMouseEvents_(True)  # Tk may reset it when the window is shown again
    view = ns.contentView()
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(radius)
    view.layer().setMasksToBounds_(True)


def font_px(ns, points):
    # Tk on macOS lays out in points and draws images at one pixel per point.
    return round(points * 96 / 72)


# ---------------------------------------------------------------- hotkeys

# macOS virtual key codes (ANSI layout); with Option held the typed character changes,
# so match key codes rather than characters.
_KEYCODES = {"T": 17, "R": 15, "H": 4, "S": 1}
_monitors = []  # keep the monitors alive


def start_hotkeys(keys, on_hotkey, log):
    """Ctrl+Option+letter through NSEvent monitors. Call on the main thread: macOS 26+ aborts the
    process when keyboard-layout APIs are used off it (which is how pynput crashed RemoteSub).
    The global monitor needs the Accessibility permission; the local one covers our own windows."""
    codes = {_KEYCODES[letter.upper()]: hid for hid, letter in keys.items()}
    wanted = AppKit.NSEventModifierFlagControl | AppKit.NSEventModifierFlagOption
    relevant = wanted | AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift

    def matches(event):
        return (event.modifierFlags() & relevant) == wanted and event.keyCode() in codes

    def on_global(event):
        if matches(event):
            on_hotkey(codes[event.keyCode()])

    def on_local(event):
        if matches(event):
            on_hotkey(codes[event.keyCode()])
            return None  # swallow it
        return event

    try:
        _monitors.append(AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, on_global))
        _monitors.append(AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, on_local))
    except Exception as e:
        log(f"hotkeys unavailable: {e}")


# ---------------------------------------------------------------- installing apps

def _copy_app(source_app, report):
    """Copy an .app into /Applications, asking for an administrator password if needed."""
    dest = os.path.join("/Applications", os.path.basename(source_app))
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    if run(["ditto", source_app, dest]).returncode != 0:
        report("Yönetici parolası istenecek…", None)
        script = f'do shell script "rm -rf \\"{dest}\\"; ditto \\"{source_app}\\" \\"{dest}\\"" with administrator privileges'
        if run(["osascript", "-e", script]).returncode != 0:
            raise RuntimeError(f"{os.path.basename(source_app)} /Applications klasörüne kopyalanamadı")
    run(["xattr", "-dr", "com.apple.quarantine", dest])
    return dest


def _install_from_dmg(dmg_path, report):
    mount = tempfile.mkdtemp(prefix="remotesub-dmg-")
    if run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, dmg_path]).returncode != 0:
        raise RuntimeError("Disk görüntüsü açılamadı")
    try:
        apps = glob.glob(os.path.join(mount, "*.app"))
        if not apps:
            raise RuntimeError("Disk görüntüsünde uygulama yok")
        return _copy_app(apps[0], report)
    finally:
        run(["hdiutil", "detach", mount, "-quiet"])


# ---------------------------------------------------------------- chiaki-ng install and settings

CHIAKI_DOMAIN = "com.chiaki.Chiaki"  # Qt QSettings for organization "Chiaki", application "Chiaki"
CHIAKI_SETTINGS_SUMMARY = "otomatik bağlanma"


def find_chiaki():
    candidates = ["/Applications/chiaki-ng.app", os.path.expanduser("~/Applications/chiaki-ng.app")]
    candidates += sorted(glob.glob("/Applications/chiaki*.app"))
    return next((c for c in candidates if os.path.isdir(c)), None)


def install_chiaki(report):
    if platform.machine() != "arm64":
        raise RuntimeError("chiaki-ng'nin macOS sürümü yalnızca Apple Silicon (M1 ve sonrası) için var")
    report("chiaki-ng indiriliyor…", None)
    url, name = github_latest_asset("streetpea/chiaki-ng", lambda n: n.endswith(".dmg") and "macos" in n)
    if not url:
        raise RuntimeError("chiaki-ng macOS kurulum dosyası bulunamadı")
    path = os.path.join(tempfile.gettempdir(), name)
    download(url, path, report)
    report("chiaki-ng kuruluyor…", None)
    _install_from_dmg(path, report)


def chiaki_running():
    return any(run(["pgrep", "-x", name]).returncode == 0 for name in ("chiaki-ng", "chiaki"))


def launch_chiaki(path, extra_args=()):
    cmd = ["open", "-a", path]
    if extra_args:
        cmd += ["--args", *extra_args]
    subprocess.Popen(cmd)


def close_chiaki():
    run(["osascript", "-e", 'tell application "chiaki-ng" to quit'])
    for _ in range(20):
        if not chiaki_running():
            return
        time.sleep(0.5)
    run(["pkill", "-x", "chiaki-ng"])
    run(["pkill", "-x", "chiaki"])
    time.sleep(1)


def _flatten(prefs, prefix=""):
    flat = {}
    for key, value in prefs.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _chiaki_prefs():
    """chiaki-ng's settings, keys like 'registered_hosts.1.server_nickname' (Qt turns '/' into '.')."""
    out = run(["defaults", "export", CHIAKI_DOMAIN, "-"], text=False)
    if out.returncode != 0 or not out.stdout:
        return {}
    return _flatten(plistlib.loads(out.stdout))


def chiaki_hosts():
    prefs, hosts, i = _chiaki_prefs(), [], 1
    while f"registered_hosts.{i}.server_nickname" in prefs:
        hosts.append({"nickname": prefs[f"registered_hosts.{i}.server_nickname"],
                      "mac": prefs.get(f"registered_hosts.{i}.server_mac")})
        i += 1
    return hosts


def apply_chiaki_settings(nickname):
    """auto_connect_mac: chiaki-ng connects to this console by itself once it sees it, which is how
    RemoteSub opens the stream in one step. Only call while chiaki-ng is closed."""
    host = next((h for h in chiaki_hosts() if h["nickname"] == nickname), None)
    if not host or host["mac"] is None:
        return
    mac = host["mac"]
    if isinstance(mac, bytes):
        run(["defaults", "write", CHIAKI_DOMAIN, "settings.auto_connect_mac", "-data", mac.hex()])
    else:
        run(["defaults", "write", CHIAKI_DOMAIN, "settings.auto_connect_mac", "-string", str(mac)])


def chiaki_settings_ok(nickname):
    host = next((h for h in chiaki_hosts() if h["nickname"] == nickname), None)
    return host is not None and _chiaki_prefs().get("settings.auto_connect_mac") == host["mac"]


# ---------------------------------------------------------------- Ollama

OLLAMA_ZIP_URL = "https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_APP = "/Applications/Ollama.app"


def ollama_installed():
    return os.path.isdir(OLLAMA_APP) or bool(shutil.which("ollama"))


def install_ollama(report):
    path = os.path.join(tempfile.gettempdir(), "Ollama-darwin.zip")
    download(OLLAMA_ZIP_URL, path, report)
    report("Ollama kuruluyor…", None)
    unpacked = tempfile.mkdtemp(prefix="remotesub-ollama-")
    if run(["ditto", "-x", "-k", path, unpacked]).returncode != 0:
        raise RuntimeError("Ollama arşivi açılamadı")
    apps = glob.glob(os.path.join(unpacked, "*.app"))
    if not apps:
        raise RuntimeError("Ollama arşivinde uygulama yok")
    _copy_app(apps[0], report)


def launch_ollama():
    if os.path.isdir(OLLAMA_APP):
        subprocess.Popen(["open", "-a", OLLAMA_APP])
    elif shutil.which("ollama"):
        subprocess.Popen([shutil.which("ollama"), "serve"])


def ollama_host_setting():
    return os.environ.get("OLLAMA_HOST") or run(["launchctl", "getenv", "OLLAMA_HOST"]).stdout.strip() or None


def model_memory_gb():
    """Apple Silicon shares memory between the CPU and GPU; this is the total."""
    try:
        return int(run(["sysctl", "-n", "hw.memsize"]).stdout.strip()) / 2 ** 30
    except ValueError:
        return 0


# The 12b model (~8 GB) has to fit next to macOS, chiaki-ng and the stream in shared memory.
LARGE_MODEL_MIN_GB = 24


# ---------------------------------------------------------------- permissions

def _accessibility_trusted(prompt=False):
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
    except Exception:
        return True


def permissions_ok():
    if not Quartz.CGPreflightScreenCaptureAccess():
        return False, "Ekran Kaydı izni yok"
    if not _accessibility_trusted():
        return True, "Ekran Kaydı (kısayollar için Erişilebilirlik izni yok)"
    return True, "Ekran Kaydı · Erişilebilirlik"


def request_permissions(report):
    Quartz.CGRequestScreenCaptureAccess()
    _accessibility_trusted(prompt=True)
    run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
    report("Sistem Ayarları → Gizlilik ve Güvenlik → Ekran ve Sistem Sesi Kaydı'nda RemoteSub'ı aç. "
           "Kısayollar için Erişilebilirlik listesinde de aç. macOS izni çoğu zaman ancak uygulama "
           "yeniden açılınca tanır: izni verdikten sonra RemoteSub'ı kapatıp yeniden aç.", None, help_text=True)
    for _ in range(120):
        if Quartz.CGPreflightScreenCaptureAccess():
            return
        time.sleep(1)
    raise RuntimeError("Ekran Kaydı izni henüz görünmüyor; izni verdiysen RemoteSub'ı yeniden aç")
