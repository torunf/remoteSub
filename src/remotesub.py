"""RemoteSub: translates game subtitles shown in the chiaki-ng window.

Reads a per-game subtitle region of the chiaki-ng stream with OCR, translates new lines
with TranslateGemma (Ollama) and shows the translation in a click-through overlay.
Regions are stored as fractions of the 16:9 video area, so they survive moving or
resizing the chiaki-ng window.

Hotkeys: Ctrl+Alt (Ctrl+Option on macOS) + T start/stop, R select region, H hide/show overlay,
S screenshot with the overlay. Platform specifics live in platform_win.py and platform_mac.py.
"""
import difflib
import json
import os
import queue
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import filedialog, simpledialog, ttk

import sv_ttk
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import dialogs
from i18n import _
import i18n
import setup_steps
import updater
from dictionary import Dictionary
from ocr_engine import OcrEngine

try:
    from _version import VERSION  # written by the build scripts
except ImportError:
    VERSION = "0.0.0-dev"

if sys.platform == "darwin":
    import platform_mac as plat
else:
    import platform_win as plat

plat.init_process()

APP_DIR = plat.APP_DIR
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "log.txt")
SHOTS_DIR = os.path.join(APP_DIR, "shots")
DICTIONARY_DIR = os.path.join(APP_DIR, "dictionaries")
FONT_DIR = os.path.join(APP_DIR, "fonts")
# While this file exists, the next translation of a multi-line subtitle is snapshotted.
SNAP_MULTILINE_FLAG = os.path.join(APP_DIR, "snap_multiline.flag")

DEFAULT_PROFILE = {"region": None, "font_size": 20, "placement": "cover", "font_file": None,
                   "background": "blur", "dim_percent": 55}
# Machine-specific values start empty; the setup steps (setup_steps.py) fill them in.
DEFAULT_CONFIG = {
    "ollama_url": "",
    "model": "",                 # chosen from the GPU's memory on first run
    "source": "en",
    "target": "tr",
    "chiaki_path": "",
    "ps5_nickname": "",          # the console paired in chiaki-ng
    "ps5_host": "",              # refreshed by discovery at every connect
    "auto_connect": True,        # start the chiaki-ng stream when RemoteSub opens
    "chiaki_extra_args": [],     # extra chiaki-ng arguments, e.g. ["--dualsense"]
    "last_profile": "",          # first profile, named in the interface language
    "profiles": {},
    "language": "auto",          # "en", "tr" or "auto" (the system's display language)
}
LANG_NAMES = {"en": "English", "tr": "Turkish", "de": "German", "fr": "French", "es": "Spanish",
              "it": "Italian", "ja": "Japanese", "ko": "Korean", "ru": "Russian", "zh": "Chinese"}
# Labels are looked up when shown, after the interface language is chosen.
BACKGROUNDS = {"blur": "Frosted glass", "dim": "See-through, dimmed"}
MUTED = "#9aa0a6"  # secondary text on the dark theme
PLACEMENTS = {"cover": "Cover the region", "above": "Above the region", "match": "In place of the English text"}

VIDEO_ASPECT = 16 / 9
READ_INTERVAL = 0.14     # seconds between OCR reads of the region
CONFIRM_READS = 2        # a new line must read the same this many times before it is translated
EMPTY_READS_TO_HIDE = 2  # blank reads before the overlay is hidden
SAME_TEXT_RATIO = 0.9    # OCR jitter tolerance when comparing lines

HOTKEY_TOGGLE, HOTKEY_REGION, HOTKEY_HIDE, HOTKEY_SNAP = 1, 2, 3, 4


def log(line):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def load_config():
    legacy = plat.LEGACY_APP_DIR and os.path.join(plat.LEGACY_APP_DIR, "config.json")
    os.makedirs(APP_DIR, exist_ok=True)
    if legacy and not os.path.exists(CONFIG_PATH) and os.path.exists(legacy):
        shutil.copy2(legacy, CONFIG_PATH)  # profiles from the app's earlier name
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    i18n.set_language(cfg.get("language"))
    if not cfg["profiles"]:
        cfg["profiles"] = {_("Default profile"): dict(DEFAULT_PROFILE)}
    if cfg["last_profile"] not in cfg["profiles"]:
        cfg["last_profile"] = next(iter(cfg["profiles"]))
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- PS5 discovery and chiaki-ng launch

def discover_ps5(nickname, known_host, timeout=1.5):
    """Find the console on the LAN with the PlayStation discovery protocol.

    Returns (host, status) where status is "on" or "standby", or (None, None)."""
    msg = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
    targets = {"255.255.255.255"}
    if known_host:
        targets.add(known_host.rsplit(".", 1)[0] + ".255")  # subnet broadcast, in case of several adapters
        targets.add(known_host)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        s.bind(("", 0))
        for target in targets:
            try:
                s.sendto(msg, (target, 9302))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            lines = data.decode(errors="replace").splitlines()
            fields = dict(l.split(":", 1) for l in lines[1:] if ":" in l)
            if fields.get("host-name") == nickname or not nickname:
                return addr[0], "on" if " 200 " in lines[0] else "standby"
    return None, None


# ---------------------------------------------------------------- chiaki-ng window geometry

def video_rect(client):
    """chiaki-ng letterboxes the 16:9 stream inside its client area."""
    x, y, w, h = client
    if w / h > VIDEO_ASPECT:
        vw = round(h * VIDEO_ASPECT)
        return x + (w - vw) // 2, y, vw, h
    vh = round(w / VIDEO_ASPECT)
    return x, y + (h - vh) // 2, w, vh


def region_to_screen(region, video):
    vx, vy, vw, vh = video
    fx, fy, fw, fh = region
    return round(vx + fx * vw), round(vy + fy * vh), max(1, round(fw * vw)), max(1, round(fh * vh))


# ---------------------------------------------------------------- OCR and translation

def extract_text(ocr_lines):
    """Subtitle lines joined into one sentence, the number of lines, and the box around them.

    ocr_lines: [(text, (x0, y0, x1, y1))] in image pixels; the box is None when nothing is kept."""
    lines, boxes = [], []
    for raw, box in ocr_lines:
        text = " ".join(raw.split())
        if sum(c.isalpha() for c in text) >= 2:
            lines.append(text)
            boxes.append(box)
    if not boxes:
        return "", 0, None
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    return " ".join(lines), len(lines), union


def looks_turkish(text):
    return any(c in "ğĞışŞİ" for c in text)


def similar(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SAME_TEXT_RATIO


class Translator:
    def __init__(self, cfg):
        self.url = cfg["ollama_url"].rstrip("/") + "/api/generate"
        self.model = cfg["model"]
        self.source, self.target = cfg["source"], cfg["target"]
        self.cache = {}
        self.dictionaries = {}  # file name -> Dictionary, loaded on first use

    def _generate(self, payload):
        req = urllib.request.Request(self.url, json.dumps(payload).encode("utf-8"),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r).get("response", "").strip()

    def warm_up(self):
        try:
            self._generate({"model": self.model, "keep_alive": "30m"})
        except Exception as e:
            log(f"warm-up failed: {e}")

    def dictionary(self, name):
        """The profile's dictionary, reloaded when its file changes (it may be growing while we play)."""
        if not name:
            return None
        path = os.path.join(DICTIONARY_DIR, name)
        cached = self.dictionaries.get(name)
        now = time.monotonic()
        if cached and now < cached["next_check"]:
            return cached["dictionary"]
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if cached and cached["mtime"] == mtime:
            cached["next_check"] = now + 30
            return cached["dictionary"]
        try:
            dictionary = Dictionary(path)
            log(f"dictionary {name}: {len(dictionary)} lines")
        except Exception as e:
            log(f"dictionary {name} unavailable: {e}")
            dictionary = cached["dictionary"] if cached else None
        self.dictionaries[name] = {"dictionary": dictionary, "mtime": mtime, "next_check": now + 30}
        return dictionary

    def translate(self, text, dictionary_name=None):
        """(translation, where it came from): "dictionary", "cache" or "model"."""
        dictionary = self.dictionary(dictionary_name)
        if dictionary is not None:
            found = dictionary.lookup(text)
            if found:
                return found, "dictionary"
        if text in self.cache:
            return self.cache[text], "cache"
        src = LANG_NAMES.get(self.source, self.source)
        tgt = LANG_NAMES.get(self.target, self.target)
        # TranslateGemma is trained on this exact prompt shape (note the two blank lines).
        prompt = (
            f"You are a professional {src} ({self.source}) to {tgt} ({self.target}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original {src} text "
            f"while adhering to {tgt} grammar, vocabulary, and cultural sensitivities.\n"
            f"Produce only the {tgt} translation, without any additional explanations or commentary. "
            f"Please translate the following {src} text into {tgt}:\n\n\n{text}"
        )
        # Cap output so a runaway generation can't append invented sentences.
        max_tokens = 24 + 4 * len(text.split())

        def run(temperature):
            return self._generate({
                "model": self.model, "prompt": prompt, "stream": False, "keep_alive": "30m",
                "options": {"temperature": temperature, "num_predict": max_tokens},
            })

        out = run(0)
        # A translation far longer than its source means the model added sentences; retry once.
        if len(out) > 1.8 * len(text) + 15:
            out = min(out, run(0.4), key=len)
        self.cache[text] = out
        return out, "model"


# ---------------------------------------------------------------- capture → OCR → translate loop

class Pipeline(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.active = threading.Event()
        self.paused = threading.Event()
        self.translator = Translator(app.cfg)

    def post(self, *msg):
        self.app.events.put(msg)

    def run(self):
        threading.Thread(target=self.translator.warm_up, daemon=True).start()
        ocr = OcrEngine(APP_DIR, log)
        log(f"OCR engine: {ocr.name}")
        capturer = plat.Capturer()
        hwnd, next_find = None, 0.0
        shown_src = ""            # source text of the translation currently on screen
        candidate, candidate_reads, first_seen = "", 0, 0.0
        empty_reads = 0
        last_box = None
        allowed, next_permission_check = False, 0.0

        while True:
            if not self.active.is_set():
                if shown_src:
                    self.post("hide")
                    shown_src = ""
                time.sleep(0.2)
                continue
            if self.paused.is_set():  # a snapshot is making the overlay visible to capture
                time.sleep(0.05)
                continue
            cycle_start = time.perf_counter()

            # Without Screen Recording access macOS shows its prompt on every capture attempt.
            if cycle_start >= next_permission_check:
                allowed, next_permission_check = plat.capture_allowed(), cycle_start + 10.0
            if not allowed:
                self.post("status", _("No Screen Recording permission · Setup → Permissions → Install, then reopen the app"))
                time.sleep(1.0)
                continue

            if hwnd is None or not plat.window_valid(hwnd) or cycle_start >= next_find:
                hwnd, next_find = plat.find_chiaki_window(), cycle_start + 1.0
            profile = self.app.current_profile()
            if hwnd is None:
                self.post("status", _("chiaki-ng window not found"))
                time.sleep(0.5)
                continue
            if not profile.get("region"):
                self.post("status", _("No region chosen for this profile ({hotkey})", hotkey=hotkey_label("R")))
                time.sleep(0.5)
                continue
            client = plat.client_rect(hwnd)
            if not client or client[2] < 100 or client[3] < 100:
                time.sleep(0.5)
                continue
            box = region_to_screen(profile["region"], video_rect(client))
            if box != last_box:
                last_box = box
                self.post("move", box)

            x, y, w, h = box
            if not plat.chiaki_visible_at(hwnd, x + w // 2, y + h // 2):
                # Nothing of chiaki-ng to read there; don't translate the taskbar or a browser.
                self.post("status", _("chiaki-ng window not visible"))
                if shown_src:
                    self.post("hide")
                    shown_src = ""
                candidate, candidate_reads = "", 0
                time.sleep(0.3)
                continue
            frame = capturer.grab(hwnd, box)
            t_ocr = time.perf_counter()
            text, n_lines, text_box = extract_text(ocr.read(frame))
            ocr_ms = (time.perf_counter() - t_ocr) * 1000
            self.post("status", _("Running · OCR {ms} ms", ms=round(ocr_ms)))

            if not text or looks_turkish(text):
                empty_reads += 1
                candidate, candidate_reads = "", 0
                if shown_src and empty_reads >= EMPTY_READS_TO_HIDE:
                    self.post("hide")
                    shown_src = ""
            else:
                empty_reads = 0
                if shown_src and similar(text, shown_src):
                    candidate, candidate_reads = "", 0
                elif candidate and similar(text, candidate):
                    candidate, candidate_reads = text, candidate_reads + 1
                else:
                    candidate, candidate_reads, first_seen = text, 1, t_ocr

                if candidate_reads >= CONFIRM_READS:
                    t_tr = time.perf_counter()
                    try:
                        translated, origin = self.translator.translate(candidate, profile.get("dictionary"))
                    except Exception as e:
                        self.post("status", _("Translation error: {error}", error=e))
                        log(f"translation error: {e}")
                        time.sleep(1)
                        continue
                    done = time.perf_counter()
                    timing = _("first seen → on screen {total} ms (OCR {ocr} ms, translation {translation} ms, {origin})",
                               total=round(1000 * (done - first_seen)), ocr=round(ocr_ms),
                               translation=round(1000 * (done - t_tr)), origin=_(origin))
                    # Where the English text sits, in screen pixels relative to the region (the
                    # frame can be at a higher resolution than the screen, e.g. Retina).
                    text_rel = None
                    if text_box:
                        sx, sy = frame.width / w, frame.height / h
                        text_rel = (text_box[0] / sx, text_box[1] / sy,
                                    (text_box[2] - text_box[0]) / sx, (text_box[3] - text_box[1]) / sy)
                    self.post("show", candidate, translated, timing, n_lines, frame, text_rel)
                    log(f"{timing} | {candidate!r} -> {translated!r}")
                    shown_src, candidate, candidate_reads = candidate, "", 0

            time.sleep(max(0.0, READ_INTERVAL - (time.perf_counter() - cycle_start)))


# ---------------------------------------------------------------- windows: overlay, region picker

TEXT_COLOR = (246, 244, 238)
_fonts = {}


def subtitle_font(px, font_file=None):
    """The chosen font file, else the platform's UI font (semibold when it is a variable font)."""
    key = (px, font_file)
    if key not in _fonts:
        for path in ([font_file] if font_file else []) + plat.FONT_FILES:
            if not path or not os.path.exists(path):
                continue
            try:
                font = ImageFont.truetype(path, px)
            except OSError:
                continue
            if path != font_file:
                try:
                    font.set_variation_by_name("Semibold")
                except Exception:
                    pass
            _fonts[key] = font
            break
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def wrap_balanced(text, font, max_width):
    """Wrap to as few lines as fit, then even out their lengths like TV subtitles."""
    def wrap(width):
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if not cur or font.getlength(trial) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        return lines + [cur] if cur else lines

    lines = wrap(max_width)
    lo, hi = 0, max_width
    while hi - lo > 4:  # narrowest width that still needs the same number of lines
        mid = (lo + hi) / 2
        if len(wrap(mid)) <= len(lines):
            hi = mid
        else:
            lo = mid
    return wrap(hi)


def layout_subtitle(text, width, font_px, font_file=None, tight=False):
    """(font, padding x, padding y, line height, lines) for text set at font_px inside width."""
    font = subtitle_font(font_px, font_file)
    pad_x = round(font_px * (0.45 if tight else 1.1))
    pad_y = round(font_px * (0.2 if tight else 0.45))
    line_h = round(font_px * 1.3)
    return font, pad_x, pad_y, line_h, wrap_balanced(text, font, max(10, width - 2 * pad_x))


def render_subtitle(text, width, min_height, font_px, background, font_file=None, tight=False,
                    bg_mode="blur", dim_percent=55):
    """RGBA image: text with a soft shadow on either a blurred, darkened copy of the game frame
    ("blur") or the live game showing through, darkened by dim_percent ("dim"). Corners are
    rounded in the alpha channel."""
    font, pad_x, pad_y, line_h, lines = layout_subtitle(text, width, font_px, font_file, tight)
    height = max(min_height, line_h * len(lines) + 2 * pad_y)
    size = (width, height)

    if bg_mode == "dim":
        img = Image.new("RGBA", size, (8, 8, 12, round(255 * min(max(dim_percent, 0), 100) / 100)))
    elif background is not None:
        # Blur at quarter size: cheap, and strong enough that the original text is unreadable.
        small = background.convert("RGB").resize((max(1, width // 4), max(1, height // 4)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(5))
        img = Image.blend(small.resize(size, Image.BILINEAR), Image.new("RGB", size, (12, 12, 16)), 0.42)
        img = img.convert("RGBA")
    else:
        img = Image.new("RGBA", size, (24, 24, 28, 255))

    top = (height - line_h * len(lines)) // 2
    offset = max(1, font_px // 14)
    shadow = Image.new("L", size, 0)
    sd = ImageDraw.Draw(shadow)
    for i, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2
        sd.text((x + offset, top + i * line_h + offset), line, font=font, fill=210)
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, font_px // 9)))
    img = Image.alpha_composite(img, Image.merge("RGBA", (*Image.new("RGB", size).split(), shadow)))

    text_layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    for i, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2
        draw.text((x, top + i * line_h), line, font=font, fill=(*TEXT_COLOR, 255))
    img = Image.alpha_composite(img, text_layer)

    corners = Image.new("L", size, 0)
    ImageDraw.Draw(corners).rounded_rectangle((0, 0, width - 1, height - 1), max(8, font_px // 2), fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), corners))
    return img


def fit_over_text(text, region, text_rel, font_px, font_file, frame, bg_mode="blur", dim_percent=55):
    """Place the translation over the English text's own box: (image, x, y).

    The font size stays as chosen. Turkish usually runs longer than the English, so the box first
    widens around its centre (never past the region); if the text still doesn't fit in the
    English text's height, it grows upwards from the same bottom line."""
    rx, ry, rw, rh = region
    tx, ty, tw, th = text_rel
    margin_x, margin_y = round(font_px * 0.5), round(font_px * 0.25)
    box_w, box_h = min(round(tw) + 2 * margin_x, rw), round(th) + 2 * margin_y
    centre_x, bottom = rx + tx + tw / 2, ry + ty + th + margin_y

    def needed_height(width):
        _, _, pad_y, line_h, lines = layout_subtitle(text, width, font_px, font_file, tight=True)
        return line_h * len(lines) + 2 * pad_y

    step = max(16, box_w // 10)
    width = box_w
    while needed_height(width) > box_h and width < rw:
        width = min(width + step, rw)
    height = max(box_h, needed_height(width))
    left = round(min(max(centre_x - width / 2, rx), rx + rw - width))

    background = None
    if frame is not None:
        sx, sy = frame.width / rw, frame.height / rh
        crop = ((left - rx) * sx, max(0, (bottom - height - ry) * sy), (left - rx + width) * sx, (bottom - ry) * sy)
        background = frame.crop(tuple(round(v) for v in crop))
    img = render_subtitle(text, width, box_h, font_px, background, font_file, tight=True,
                          bg_mode=bg_mode, dim_percent=dim_percent)
    return img, left, round(bottom - img.height)


class Overlay:
    def __init__(self, root):
        # Click-through, never focused, and kept out of screen capture so the OCR never reads
        # our own text back (platform_win: layered Tk window, platform_mac: native NSPanel).
        self.window = plat.OverlayWindow(root)
        self.background = None
        self.text_rel = None
        self.box = None
        self.text = ""
        self.hidden_by_user = False
        self.font_size, self.placement, self.font_file = 20, "cover", None
        self.bg_mode, self.dim_percent = "blur", 55

    def set_capturable(self, capturable):
        return self.window.set_capturable(capturable)

    def configure(self, font_size, placement, font_file=None, bg_mode="blur", dim_percent=55):
        self.font_size, self.placement, self.font_file = font_size, placement, font_file
        self.bg_mode, self.dim_percent = bg_mode, dim_percent
        self._place()

    def move(self, box):
        self.box = box
        self._place()

    def show(self, text, background=None, text_rel=None):
        self.text, self.background, self.text_rel = text, background, text_rel
        self._place()

    def hide(self):
        self.text = ""
        self.window.hide()

    def toggle_user_hidden(self):
        self.hidden_by_user = not self.hidden_by_user
        self._place()

    def _place(self):
        if not self.text or not self.box or self.hidden_by_user:
            self.window.hide()
            return
        x, y, w, h = self.box
        font_px = self.window.font_px(self.font_size)
        if self.placement == "match" and self.text_rel:
            img, left, top = fit_over_text(self.text, self.box, self.text_rel, font_px, self.font_file,
                                           self.background, self.bg_mode, self.dim_percent)
        else:
            # In cover mode the box is at least as tall as the region, so the original can't peek out.
            img = render_subtitle(self.text, w, h if self.placement == "cover" else 0, font_px,
                                  self.background, self.font_file, bg_mode=self.bg_mode,
                                  dim_percent=self.dim_percent)
            left = x
            top = y + h - img.height if self.placement == "cover" else y - img.height - 6
        # Per-pixel alpha (rounded corners, a see-through background) needs platform drawing.
        self.window.show(img, left, max(0, top))


class RegionPicker:
    """Dims the video area of the chiaki-ng window and lets the user drag the subtitle region."""

    def __init__(self, root, video, current, on_done):
        self.video, self.on_done = video, on_done
        vx, vy, vw, vh = video
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.45)
        self.win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.canvas = tk.Canvas(self.win, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(vw // 2, 40, fill="white", font=(plat.UI_FONT, 18, "bold"),
                                text=_("Drag over the subtitle area  ·  Esc: cancel"))
        if current:
            fx, fy, fw, fh = current
            self.canvas.create_rectangle(fx * vw, fy * vh, (fx + fw) * vw, (fy + fh) * vh,
                                         outline="#888888", width=2, dash=(6, 4))
        self.rect, self.start = None, None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        plat.make_floating(self.win)
        self.win.focus_force()
        self.win.grab_set()

    def _press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffd400", width=3)

    def _drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        _, _, vw, vh = self.video
        x0, x1 = sorted((self.start[0], e.x))
        y0, y1 = sorted((self.start[1], e.y))
        self.win.destroy()
        if x1 - x0 < 20 or y1 - y0 < 10:
            return
        clamp = lambda v: min(1.0, max(0.0, v))
        self.on_done([clamp(x0 / vw), clamp(y0 / vh), clamp((x1 - x0) / vw), clamp((y1 - y0) / vh)])


# ---------------------------------------------------------------- control panel

HOTKEY_LETTERS = {HOTKEY_TOGGLE: "T", HOTKEY_REGION: "R", HOTKEY_HIDE: "H", HOTKEY_SNAP: "S"}


def hotkey_label(letter):
    return f"{plat.HOTKEY_LABEL}+{letter}"


class App:
    def __init__(self, force_setup=False):
        self.cfg = load_config()
        self.events = queue.Queue()
        self.root = tk.Tk()
        self.root.title(f"RemoteSub {VERSION}")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        sv_ttk.set_theme("dark")
        # sv_ttk applies its colours on <<ThemeChanged>>, which never arrived with Tk 8.6.15 and left
        # the window half light; apply them directly.
        self.root.tk.call("configure_colors")
        style = ttk.Style()
        style.configure("Title.TLabel", font=(plat.UI_FONT, 16, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Translation.TLabel", font=(plat.UI_FONT, 11, "bold"))
        style.configure("Big.Accent.TButton", font=(plat.UI_FONT, 11, "bold"), padding=(10, 8))
        style.configure("Big.TButton", font=(plat.UI_FONT, 11, "bold"), padding=(10, 8))
        self.overlay = Overlay(self.root)
        self.pipeline = None  # started once setup is complete
        self._build_ui()
        plat.dark_title_bar(self.root)
        self._apply_profile()
        plat.start_hotkeys(HOTKEY_LETTERS, lambda hid: self.events.put(("hotkey", hid)), log)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.after(30, self._pump)
        # The installer starts us with --setup so the first run always walks through setup.
        # After an update the package opens us with --setup again; only the first run needs it.
        self.open_setup(show_if_ready=force_setup and not self.cfg.get("setup_completed"))

    # setup
    def open_setup(self, show_if_ready=True):
        """Check dependencies in the background; show the setup window if anything is missing."""
        self.status_var.set("Kontrol ediliyor…")
        self.setup_btn.configure(state="disabled")

        def check():
            steps = setup_steps.build_steps(self.cfg, APP_DIR)
            self.events.put(("setup_checked", steps, setup_steps.run_checks(steps), show_if_ready))

        threading.Thread(target=check, daemon=True).start()

    def _setup_checked(self, steps, results, show_if_ready):
        save_config(self.cfg)  # the checks record paths, console name, Ollama URL and model
        if setup_steps.ready(steps, results) and not show_if_ready:
            self._setup_done(True)
        else:
            setup_steps.SetupWindow(self.root, steps, results, self._setup_done)

    def _setup_done(self, ready):
        save_config(self.cfg)
        self.setup_btn.configure(state="normal")
        if not ready:
            self.status_var.set(_("Setup not finished · continue with 'Setup'"))
            return
        if not self.cfg.get("setup_completed"):
            self.cfg["setup_completed"] = True
            save_config(self.cfg)
        if self.pipeline is None:
            self.check_for_update()
            self.pipeline = Pipeline(self)
            self.pipeline.start()
            self.toggle_btn.configure(state="normal")
            self.connect_btn.configure(state="normal")
            if self.current_profile().get("region"):
                self.set_active(True)
            else:
                self.status_var.set(_("Ready · in game, choose the subtitle area with {hotkey}", hotkey=hotkey_label("R")))
            if self.cfg.get("auto_connect") and "--no-connect" not in sys.argv:
                self.connect()

    # updates
    def check_for_update(self):
        """Look for a newer release now and every few hours; offer it with a button."""
        if not plat.SUPPORTS_UPDATE:
            return

        def check():
            try:
                update = updater.find_update(VERSION, plat.UPDATE_ASSET_SUFFIX)
            except Exception as e:
                log(f"update check failed: {e}")
                return
            if update:
                self.events.put(("update_found", update))

        threading.Thread(target=check, daemon=True).start()
        self.root.after(updater.CHECK_INTERVAL_MS, self.check_for_update)

    def _show_update(self, update):
        self.update = update
        self.update_btn.configure(text=_("New version {version} · Update", version=update["version"]), state="normal")
        self.update_btn.grid()

    def install_update(self):
        version = self.update["version"]
        self.update_btn.configure(state="disabled")

        def work():
            report = lambda text, fraction=None: self.events.put(("update_status", text))
            try:
                path = updater.download_update(self.update, report)
                plat.install_update(path, report)
            except Exception as e:
                log(f"update failed: {e}")
                self.events.put(("update_failed", str(e)))
                return
            log(f"updated to {version}")
            self.events.put(("update_installed",))

        threading.Thread(target=work, daemon=True).start()

    # PS5 connection
    def connect(self):
        """Find the PS5 and start chiaki-ng straight into the stream (skipping its main window)."""
        self.connect_btn.configure(state="disabled")
        self.conn_var.set(_("Looking for the PS5…"))
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        cfg = self.cfg
        try:
            if plat.chiaki_running():
                self.events.put(("conn", _("chiaki-ng is already open")))
                return
            if not cfg.get("ps5_nickname"):
                self.events.put(("conn", _("No PS5 paired in chiaki-ng · see 'Setup'")))
                return
            host, status = discover_ps5(cfg["ps5_nickname"], cfg["ps5_host"])
            if host is None:
                # Discovery can miss a sleeping console; try the last known address anyway.
                host, note = cfg["ps5_host"], _("PS5 not found on the network, trying the last address")
            else:
                note = _("PS5 is on") if status == "on" else _("PS5 is asleep, waking it")
                if host != cfg["ps5_host"]:
                    cfg["ps5_host"] = host
                    save_config(cfg)
            self.events.put(("conn", _("{note} · {host} · connecting", note=note, host=host)))
            # chiaki-ng 1.9.9's `stream` command stalls on its host list without connecting, so
            # start it normally and let its own auto-connect (settings/auto_connect_mac, set to
            # this console) open the stream once discovery finds the PS5.
            plat.apply_chiaki_settings(cfg["ps5_nickname"])
            plat.launch_chiaki(cfg["chiaki_path"], cfg.get("chiaki_extra_args", []))
        except Exception as e:
            log(f"connect failed: {e}")
            self.events.put(("conn", _("Connection error: {error}", error=e)))
        finally:
            self.events.put(("conn_done",))

    # profiles
    def current_profile(self):
        return dict(self.cfg["profiles"][self.cfg["last_profile"]])

    def _update_profile(self, **changes):
        self.cfg["profiles"][self.cfg["last_profile"]].update(changes)
        save_config(self.cfg)
        self._apply_profile()

    def _apply_profile(self):
        p = self.current_profile()
        self.profile_box["values"] = list(self.cfg["profiles"])
        self.profile_var.set(self.cfg["last_profile"])
        self.font_var.set(p.get("font_size", 20))
        self.placement_var.set(_(PLACEMENTS[p.get("placement", "cover")]))
        self.region_var.set(_("saved") if p.get("region") else _("not chosen"))
        self.dictionary_var.set(p["dictionary"] if p.get("dictionary") else _("none · every line goes to the model"))
        font_file = self.profile_font_path(p)
        self.typeface_var.set(os.path.splitext(os.path.basename(font_file))[0] if font_file else _("System font"))
        self.background_var.set(_(BACKGROUNDS[p.get("background", "blur")]))
        self.dim_var.set(p.get("dim_percent", 55))
        self.dim_label.configure(text=_("{value}%", value=p.get("dim_percent", 55)))
        self.dim_scale.state(["!disabled"] if p.get("background") == "dim" else ["disabled"])
        self.overlay.configure(p.get("font_size", 20), p.get("placement", "cover"), font_file,
                               p.get("background", "blur"), p.get("dim_percent", 55))

    def _select_profile(self, _=None):
        self.cfg["last_profile"] = self.profile_var.get()
        save_config(self.cfg)
        self.overlay.hide()
        self._apply_profile()

    def _new_profile(self):
        name = simpledialog.askstring(_("New profile"), _("Game name:"), parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name not in self.cfg["profiles"]:
            self.cfg["profiles"][name] = dict(DEFAULT_PROFILE)
        self.cfg["last_profile"] = name
        save_config(self.cfg)
        self._apply_profile()
        self.pick_region()

    def _delete_profile(self):
        if len(self.cfg["profiles"]) == 1:
            return
        name = self.cfg["last_profile"]
        if self.ask(_("Delete profile"), _("Delete the '{name}' profile?", name=name), yes=_("Delete")):
            del self.cfg["profiles"][name]
            self.cfg["last_profile"] = next(iter(self.cfg["profiles"]))
            save_config(self.cfg)
            self._apply_profile()

    def profile_font_path(self, profile):
        name = profile.get("font_file")
        path = os.path.join(FONT_DIR, name) if name else None
        return path if path and os.path.exists(path) else None

    def choose_font(self):
        """Use a font file (.ttf/.otf) for this profile's subtitles, e.g. the game's own typeface."""
        path = filedialog.askopenfilename(parent=self.root, title=_("Font file"),
                                          filetypes=[(_("Font"), "*.ttf *.otf *.ttc"), (_("All files"), "*.*")])
        if not path:
            return
        try:
            font = ImageFont.truetype(path, 24)
        except OSError as e:
            self.inform(_("Font"), _("Couldn't open the font: {error}", error=e))
            return
        # The translation is Turkish: warn when the font can't draw its letters.
        missing = [c for c in "ğĞışŞİçÇöÖüÜ" if font.getmask(c).getbbox() is None]
        if missing and not self.ask(
                _("Font"), _("This font is missing these letters: {letters}\nUse it anyway?", letters=" ".join(missing)),
                yes=_("Use")):
            return
        os.makedirs(FONT_DIR, exist_ok=True)
        name = os.path.basename(path)
        if os.path.abspath(path) != os.path.abspath(os.path.join(FONT_DIR, name)):
            shutil.copy2(path, os.path.join(FONT_DIR, name))
        self._update_profile(font_file=name)

    def reset_font(self):
        self._update_profile(font_file=None)

    def ask(self, title, message, yes=_("Yes")):
        return dialogs.ask(self.root, title, message, yes=yes, dark_title_bar=plat.dark_title_bar)

    def inform(self, title, message):
        dialogs.inform(self.root, title, message, dark_title_bar=plat.dark_title_bar)

    def choose_dictionary(self):
        """Attach a pre-translated dictionary (JSON: source line -> translation) to this profile."""
        path = filedialog.askopenfilename(parent=self.root, title=_("Dictionary file (JSON)"),
                                          filetypes=[("JSON", "*.json"), (_("All files"), "*.*")])
        if not path:
            return
        try:
            count = len(Dictionary(path))
        except Exception as e:
            self.inform(_("Dictionary"), _("Couldn't read the file: {error}", error=e))
            return
        os.makedirs(DICTIONARY_DIR, exist_ok=True)
        name = os.path.basename(path)
        target = os.path.join(DICTIONARY_DIR, name)
        if os.path.abspath(path) != os.path.abspath(target):
            shutil.copy2(path, target)
        if self.pipeline:
            self.pipeline.translator.dictionaries.pop(name, None)  # reload a replaced file
        self._update_profile(dictionary=name)
        self.status_var.set(_("Dictionary loaded: {count} lines", count=count))

    # actions
    def pick_region(self):
        hwnd = plat.find_chiaki_window()
        if not hwnd:
            self.inform("RemoteSub", _("chiaki-ng window not found. Start the PS5 stream first."))
            return
        video = video_rect(plat.client_rect(hwnd))
        RegionPicker(self.root, video, self.current_profile().get("region"), self._region_chosen)

    def _region_chosen(self, region):
        self._update_profile(region=region)
        self.set_active(True)

    def snapshot(self):
        """Save a screenshot of the chiaki-ng window with the translation overlay included."""
        hwnd = plat.find_chiaki_window()
        if not hwnd or self.pipeline is None:
            return
        # Pause OCR while the overlay is capturable, so it never reads our own text.
        self.pipeline.paused.set()
        self.overlay.set_capturable(True)

        def grab():
            try:
                image = plat.grab_screen(plat.client_rect(hwnd))
                os.makedirs(SHOTS_DIR, exist_ok=True)
                path = os.path.join(SHOTS_DIR, time.strftime("%Y%m%d-%H%M%S") + ".png")
                image.save(path)
                log(f"snapshot saved: {path}")
                self.status_var.set(_("Screenshot saved: {name}", name=os.path.basename(path)))
            finally:
                self.overlay.set_capturable(False)
                self.pipeline.paused.clear()

        # Give the compositor a moment to include the overlay again before capturing.
        self.root.after(150, grab)

    def set_active(self, active):
        if self.pipeline is None:
            return
        if active:
            self.pipeline.active.set()
        else:
            self.pipeline.active.clear()
            self.overlay.hide()
            self.status_var.set(_("Stopped"))
        label = _("Stop translating") if active else _("Start translating")
        self.toggle_btn.configure(text=f"{label}  ·  {hotkey_label('T')}",
                                  style="Big.TButton" if active else "Big.Accent.TButton")

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="we")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="RemoteSub", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=_("version {version}", version=VERSION), style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        self.setup_btn = ttk.Button(header, text=_("Setup"), command=self.open_setup)
        self.setup_btn.grid(row=0, column=1, rowspan=2, sticky="e")
        self.update_btn = ttk.Button(outer, style="Accent.TButton", command=self.install_update)
        self.update_btn.grid(row=1, column=0, sticky="we", pady=(12, 0))
        self.update_btn.grid_remove()  # shown when a newer release is found

        # connection
        conn = ttk.LabelFrame(outer, text="PS5", padding=12)
        conn.grid(row=2, column=0, sticky="we", pady=(14, 0))
        conn.columnconfigure(0, weight=1)
        self.conn_var = tk.StringVar(value=_("Not connected"))
        ttk.Label(conn, textvariable=self.conn_var, style="Muted.TLabel", wraplength=260).grid(
            row=0, column=0, sticky="w")
        self.connect_btn = ttk.Button(conn, text=_("Connect"), command=self.connect, state="disabled")
        self.connect_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # profile: game, region, dictionary
        prof = ttk.LabelFrame(outer, text=_("Game profile"), padding=12)
        prof.grid(row=3, column=0, sticky="we", pady=(12, 0))
        prof.columnconfigure(1, weight=1)
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(prof, textvariable=self.profile_var, state="readonly")
        self.profile_box.grid(row=0, column=0, columnspan=2, sticky="we")
        self.profile_box.bind("<<ComboboxSelected>>", self._select_profile)
        ttk.Button(prof, text=_("New"), command=self._new_profile).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(prof, text=_("Delete"), command=self._delete_profile).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(prof, text=_("Subtitle area")).grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.region_var = tk.StringVar()
        ttk.Label(prof, textvariable=self.region_var, style="Muted.TLabel").grid(
            row=1, column=1, sticky="w", padx=8, pady=(12, 0))
        ttk.Button(prof, text=_("Choose  ·  {hotkey}", hotkey=hotkey_label("R")), command=self.pick_region).grid(
            row=1, column=2, columnspan=2, sticky="we", pady=(12, 0))

        ttk.Label(prof, text=_("Dictionary")).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.dictionary_var = tk.StringVar()
        ttk.Label(prof, textvariable=self.dictionary_var, style="Muted.TLabel", wraplength=170).grid(
            row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Button(prof, text=_("Choose…"), command=self.choose_dictionary).grid(
            row=2, column=2, columnspan=2, sticky="we", pady=(8, 0))

        # appearance of the translation
        look = ttk.LabelFrame(outer, text=_("Appearance"), padding=12)
        look.grid(row=4, column=0, sticky="we", pady=(12, 0))
        look.columnconfigure(1, weight=1)
        ttk.Label(look, text=_("Placement")).grid(row=0, column=0, sticky="w")
        self.placement_var = tk.StringVar()
        placement = ttk.Combobox(look, textvariable=self.placement_var, state="readonly",
                                 values=[_(v) for v in PLACEMENTS.values()])
        placement.grid(row=0, column=1, columnspan=3, sticky="we", padx=(8, 0))
        placement.bind("<<ComboboxSelected>>", lambda e: self._update_profile(
            placement=next(k for k, v in PLACEMENTS.items() if _(v) == self.placement_var.get())))

        ttk.Label(look, text=_("Size")).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.font_var = tk.IntVar()
        size = ttk.Spinbox(look, from_=10, to=60, textvariable=self.font_var, width=6,
                           command=lambda: self._update_profile(font_size=self.font_var.get()))
        size.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        size.bind("<Return>", lambda e: self._update_profile(font_size=self.font_var.get()))
        size.bind("<FocusOut>", lambda e: self._update_profile(font_size=self.font_var.get()))

        ttk.Label(look, text=_("Font")).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.typeface_var = tk.StringVar()
        ttk.Label(look, textvariable=self.typeface_var, style="Muted.TLabel", wraplength=150).grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(look, text=_("Choose…"), command=self.choose_font).grid(row=2, column=2, pady=(8, 0))
        ttk.Button(look, text=_("Default"), command=self.reset_font).grid(row=2, column=3, padx=(6, 0), pady=(8, 0))

        ttk.Label(look, text=_("Background")).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.background_var = tk.StringVar()
        background = ttk.Combobox(look, textvariable=self.background_var, state="readonly",
                                  values=[_(v) for v in BACKGROUNDS.values()])
        background.grid(row=3, column=1, columnspan=3, sticky="we", padx=(8, 0), pady=(8, 0))
        background.bind("<<ComboboxSelected>>", lambda e: self._update_profile(
            background=next(k for k, v in BACKGROUNDS.items() if _(v) == self.background_var.get())))

        ttk.Label(look, text=_("Dimming")).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.dim_var = tk.IntVar(value=55)
        self.dim_scale = ttk.Scale(look, from_=0, to=90, variable=self.dim_var,
                                   command=lambda v: self.dim_label.configure(text=_("{value}%", value=round(float(v)))))
        self.dim_scale.grid(row=4, column=1, columnspan=2, sticky="we", padx=(8, 0), pady=(8, 0))
        # Re-render only once the slider is released, not on every pixel of the drag.
        self.dim_scale.bind("<ButtonRelease-1>", lambda e: self._update_profile(dim_percent=round(self.dim_var.get())))
        self.dim_label = ttk.Label(look, text=_("{value}%", value=55), style="Muted.TLabel", width=4)
        self.dim_label.grid(row=4, column=3, sticky="e", pady=(8, 0))

        self.toggle_btn = ttk.Button(outer, state="disabled", style="Big.Accent.TButton",
                                     command=lambda: self.set_active(not self.pipeline.active.is_set()))
        self.toggle_btn.grid(row=5, column=0, sticky="we", pady=(16, 0))

        # what was read and translated last
        last = ttk.LabelFrame(outer, text=_("Last translation"), padding=12)
        last.grid(row=6, column=0, sticky="we", pady=(14, 0))
        last.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value=_("Stopped"))
        ttk.Label(last, textvariable=self.status_var, style="Muted.TLabel", wraplength=380).grid(
            row=0, column=0, sticky="w")
        self.src_var, self.dst_var, self.timing_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(last, textvariable=self.src_var, style="Muted.TLabel", wraplength=380).grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(last, textvariable=self.dst_var, style="Translation.TLabel", wraplength=380).grid(
            row=2, column=0, sticky="w", pady=(2, 0))
        ttk.Label(last, textvariable=self.timing_var, style="Muted.TLabel", wraplength=380).grid(
            row=3, column=0, sticky="w", pady=(6, 0))
        self.toggle_btn.configure(text=f"{_('Start translating')}  ·  {hotkey_label('T')}")

    def _pump(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == "status" and self.pipeline and self.pipeline.active.is_set():
                    self.status_var.set(args[0])
                elif kind == "setup_checked":
                    self._setup_checked(*args)
                elif kind == "move":
                    self.overlay.move(args[0])
                elif kind == "show":
                    src, dst, timing, n_lines, frame, text_rel = args
                    self.overlay.show(dst, frame, text_rel)
                    self.src_var.set(src)
                    self.dst_var.set(dst)
                    self.timing_var.set(timing)
                    if n_lines >= 2 and os.path.exists(SNAP_MULTILINE_FLAG):
                        os.remove(SNAP_MULTILINE_FLAG)
                        self.root.after(100, self.snapshot)
                elif kind == "hide":
                    self.overlay.hide()
                elif kind == "conn":
                    self.conn_var.set(args[0])
                elif kind == "update_found":
                    self._show_update(args[0])
                elif kind == "update_status":
                    self.status_var.set(args[0])
                elif kind == "update_failed":
                    self.status_var.set(_("Update failed: {error}", error=args[0]))
                    self.update_btn.configure(state="normal")
                elif kind == "update_installed":
                    plat.relaunch_after_update()
                    self.root.destroy()
                    return
                elif kind == "conn_done":
                    self.connect_btn.configure(state="normal")
                elif kind == "hotkey":
                    if args[0] == HOTKEY_TOGGLE and self.pipeline:
                        self.set_active(not self.pipeline.active.is_set())
                    elif args[0] == HOTKEY_REGION:
                        self.pick_region()
                    elif args[0] == HOTKEY_HIDE:
                        self.overlay.toggle_user_hidden()
                    elif args[0] == HOTKEY_SNAP:
                        self.snapshot()
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App(force_setup="--setup" in sys.argv).run()
