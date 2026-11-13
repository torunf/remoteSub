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
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

import setup_steps
from ocr_engine import OcrEngine

if sys.platform == "darwin":
    import platform_mac as plat
else:
    import platform_win as plat

plat.init_process()

APP_DIR = plat.APP_DIR
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "log.txt")
SHOTS_DIR = os.path.join(APP_DIR, "shots")
# While this file exists, the next translation of a multi-line subtitle is snapshotted.
SNAP_MULTILINE_FLAG = os.path.join(APP_DIR, "snap_multiline.flag")

DEFAULT_PROFILE = {"region": None, "font_size": 20, "placement": "cover"}
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
    "last_profile": "Varsayılan",
    "profiles": {"Varsayılan": dict(DEFAULT_PROFILE)},
}
LANG_NAMES = {"en": "English", "tr": "Turkish", "de": "German", "fr": "French", "es": "Spanish",
              "it": "Italian", "ja": "Japanese", "ko": "Korean", "ru": "Russian", "zh": "Chinese"}
PLACEMENTS = {"cover": "Orijinalin üstüne", "above": "Orijinalin yukarısına"}

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
    if not cfg["profiles"]:
        cfg["profiles"] = {"Varsayılan": dict(DEFAULT_PROFILE)}
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
    """Subtitle lines joined into one sentence, plus how many lines there were."""
    lines = []
    for line in ocr_lines:
        text = " ".join(line.split())
        if sum(c.isalpha() for c in text) >= 2:
            lines.append(text)
    return " ".join(lines), len(lines)


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

    def translate(self, text):
        if text in self.cache:
            return self.cache[text]
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
        return out


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

            if hwnd is None or not plat.window_valid(hwnd) or cycle_start >= next_find:
                hwnd, next_find = plat.find_chiaki_window(), cycle_start + 1.0
            profile = self.app.current_profile()
            if hwnd is None:
                self.post("status", "chiaki-ng penceresi bulunamadı")
                time.sleep(0.5)
                continue
            if not profile.get("region"):
                self.post("status", f"Bu profil için bölge seçilmemiş ({hotkey_label('R')})")
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
                self.post("status", "chiaki-ng penceresi görünmüyor")
                if shown_src:
                    self.post("hide")
                    shown_src = ""
                candidate, candidate_reads = "", 0
                time.sleep(0.3)
                continue
            frame = capturer.grab(hwnd, box)
            t_ocr = time.perf_counter()
            text, n_lines = extract_text(ocr.lines(frame))
            ocr_ms = (time.perf_counter() - t_ocr) * 1000
            self.post("status", f"Çalışıyor · OCR {ocr_ms:.0f} ms")

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
                        translated = self.translator.translate(candidate)
                    except Exception as e:
                        self.post("status", f"Çeviri hatası: {e}")
                        log(f"translation error: {e}")
                        time.sleep(1)
                        continue
                    done = time.perf_counter()
                    timing = (f"ilk görülme→ekranda {1000 * (done - first_seen):.0f} ms "
                              f"(OCR {ocr_ms:.0f} ms, çeviri {1000 * (done - t_tr):.0f} ms)")
                    self.post("show", candidate, translated, timing, n_lines, frame)
                    log(f"{timing} | {candidate!r} -> {translated!r}")
                    shown_src, candidate, candidate_reads = candidate, "", 0

            time.sleep(max(0.0, READ_INTERVAL - (time.perf_counter() - cycle_start)))


# ---------------------------------------------------------------- windows: overlay, region picker

TEXT_COLOR = (246, 244, 238)
_fonts = {}


def subtitle_font(px):
    if px not in _fonts:
        for path in plat.FONT_FILES:
            if os.path.exists(path):
                _fonts[px] = ImageFont.truetype(path, px)
                try:  # variable system fonts (macOS SF): use the semibold instance
                    _fonts[px].set_variation_by_name("Semibold")
                except Exception:
                    pass
                break
        else:
            _fonts[px] = ImageFont.load_default()
    return _fonts[px]


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


def render_subtitle(text, width, min_height, font_px, background):
    """Text with a soft shadow on a blurred, darkened copy of the game frame behind it."""
    font = subtitle_font(font_px)
    pad_x, pad_y = round(font_px * 1.1), round(font_px * 0.45)
    line_h = round(font_px * 1.3)
    lines = wrap_balanced(text, font, width - 2 * pad_x)
    height = max(min_height, line_h * len(lines) + 2 * pad_y)

    if background is not None:
        # Blur at quarter size: cheap, and strong enough that the original text is unreadable.
        small = background.convert("RGB").resize((max(1, width // 4), max(1, height // 4)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(5))
        img = small.resize((width, height), Image.BILINEAR)
        img = Image.blend(img, Image.new("RGB", img.size, (12, 12, 16)), 0.42)
    else:
        img = Image.new("RGB", (width, height), (24, 24, 28))

    shadow = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(shadow)
    top = (height - line_h * len(lines)) // 2
    offset = max(1, font_px // 14)
    for i, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2
        sd.text((x + offset, top + i * line_h + offset), line, font=font, fill=210)
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, font_px // 9)))
    img.paste((0, 0, 0), mask=shadow)

    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2
        draw.text((x, top + i * line_h), line, font=font, fill=TEXT_COLOR)
    return img


class Overlay:
    BG = "#18181c"

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.BG)
        self.label = tk.Label(self.win, bg=self.BG, bd=0, highlightthickness=0)
        self.label.pack(fill="both", expand=True)
        self.photo = None
        self.background = None
        # Click-through, never focused, and kept out of screen capture so the OCR never reads
        # our own text back.
        self.handle = plat.prepare_overlay(self.win)
        self.win.withdraw()
        self.box = None
        self.text = ""
        self.hidden_by_user = False
        self.font_size, self.placement = 20, "cover"

    def set_capturable(self, capturable):
        return plat.set_capturable(self.handle, capturable)

    def configure(self, font_size, placement):
        self.font_size, self.placement = font_size, placement
        self._place()

    def move(self, box):
        self.box = box
        self._place()

    def show(self, text, background=None):
        self.text, self.background = text, background
        self._place()

    def hide(self):
        self.text = ""
        self.win.withdraw()

    def toggle_user_hidden(self):
        self.hidden_by_user = not self.hidden_by_user
        self._place()

    def _place(self):
        if not self.text or not self.box or self.hidden_by_user:
            self.win.withdraw()
            return
        x, y, w, h = self.box
        font_px = plat.font_px(self.handle, self.font_size)
        # In cover mode the box is at least as tall as the region, so the original can't peek out.
        img = render_subtitle(self.text, w, h if self.placement == "cover" else 0, font_px, self.background)
        height = img.height
        top = y + h - height if self.placement == "cover" else y - height - 6
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)
        self.win.geometry(f"{w}x{height}+{x}+{max(0, top)}")
        radius = max(8, font_px // 2)
        self.win.deiconify()
        self.win.lift()
        plat.round_corners(self.handle, w, height, radius)


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
                                text="Altyazı bölgesini fareyle sürükleyerek seçin  ·  Esc: iptal")
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
        self.root.title("RemoteSub")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.overlay = Overlay(self.root)
        self.pipeline = None  # started once setup is complete
        self._build_ui()
        self._apply_profile()
        threading.Thread(target=plat.hotkey_loop, daemon=True,
                         args=(HOTKEY_LETTERS, lambda hid: self.events.put(("hotkey", hid)), log)).start()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.after(30, self._pump)
        # The installer starts us with --setup so the first run always walks through setup.
        self.open_setup(show_if_ready=force_setup)

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
        if all(ok for ok, _ in results) and not show_if_ready:
            self._setup_done(True)
        else:
            setup_steps.SetupWindow(self.root, steps, results, self._setup_done)

    def _setup_done(self, ready):
        save_config(self.cfg)
        self.setup_btn.configure(state="normal")
        if not ready:
            self.status_var.set("Kurulum tamamlanmadı · 'Kurulum' ile devam edin")
            return
        if self.pipeline is None:
            self.pipeline = Pipeline(self)
            self.pipeline.start()
            self.toggle_btn.configure(state="normal")
            self.connect_btn.configure(state="normal")
            if self.current_profile().get("region"):
                self.set_active(True)
            else:
                self.status_var.set(f"Hazır · oyunda {hotkey_label('R')} ile altyazı bölgesini seçin")
            if self.cfg.get("auto_connect") and "--no-connect" not in sys.argv:
                self.connect()

    # PS5 connection
    def connect(self):
        """Find the PS5 and start chiaki-ng straight into the stream (skipping its main window)."""
        self.connect_btn.configure(state="disabled")
        self.conn_var.set("PS5 aranıyor…")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        cfg = self.cfg
        try:
            if plat.chiaki_running():
                self.events.put(("conn", "chiaki-ng zaten açık"))
                return
            if not cfg.get("ps5_nickname"):
                self.events.put(("conn", "chiaki-ng'de eşleştirilmiş PS5 yok · 'Kurulum'a bakın"))
                return
            host, status = discover_ps5(cfg["ps5_nickname"], cfg["ps5_host"])
            if host is None:
                # Discovery can miss a sleeping console; try the last known address anyway.
                host, note = cfg["ps5_host"], "PS5 ağda bulunamadı, son adres deneniyor"
            else:
                note = "PS5 açık" if status == "on" else "PS5 uyku modunda, uyandırılıyor"
                if host != cfg["ps5_host"]:
                    cfg["ps5_host"] = host
                    save_config(cfg)
            self.events.put(("conn", f"{note} · {host} · bağlanılıyor"))
            # chiaki-ng 1.9.9's `stream` command stalls on its host list without connecting, so
            # start it normally and let its own auto-connect (settings/auto_connect_mac, set to
            # this console) open the stream once discovery finds the PS5.
            plat.apply_chiaki_settings(cfg["ps5_nickname"])
            plat.launch_chiaki(cfg["chiaki_path"], cfg.get("chiaki_extra_args", []))
        except Exception as e:
            log(f"connect failed: {e}")
            self.events.put(("conn", f"Bağlantı hatası: {e}"))
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
        self.placement_var.set(PLACEMENTS[p.get("placement", "cover")])
        self.region_label.configure(text="Bölge: kayıtlı" if p.get("region") else "Bölge: seçilmedi")
        self.overlay.configure(p.get("font_size", 20), p.get("placement", "cover"))

    def _select_profile(self, _=None):
        self.cfg["last_profile"] = self.profile_var.get()
        save_config(self.cfg)
        self.overlay.hide()
        self._apply_profile()

    def _new_profile(self):
        name = simpledialog.askstring("Yeni profil", "Oyunun adı:", parent=self.root)
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
        if messagebox.askyesno("Profili sil", f"'{name}' silinsin mi?", parent=self.root):
            del self.cfg["profiles"][name]
            self.cfg["last_profile"] = next(iter(self.cfg["profiles"]))
            save_config(self.cfg)
            self._apply_profile()

    # actions
    def pick_region(self):
        hwnd = plat.find_chiaki_window()
        if not hwnd:
            messagebox.showwarning("RemoteSub", "chiaki-ng penceresi bulunamadı. Önce PS5 yayınını açın.",
                                   parent=self.root)
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
                self.status_var.set(f"Görüntü kaydedildi: {os.path.basename(path)}")
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
            self.status_var.set("Durduruldu")
        label = "Durdur" if active else "Başlat"
        self.toggle_btn.configure(text=f"{label}  ({hotkey_label('T')})")

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        top.grid(sticky="we")
        self.connect_btn = ttk.Button(top, text="PS5'e bağlan", command=self.connect, state="disabled")
        self.connect_btn.grid(row=0, column=0, sticky="w")
        self.conn_var = tk.StringVar()
        ttk.Label(top, textvariable=self.conn_var, foreground="#555", wraplength=200).grid(
            row=0, column=1, sticky="w", padx=8)
        self.setup_btn = ttk.Button(top, text="Kurulum", command=self.open_setup)
        self.setup_btn.grid(row=0, column=2, sticky="e")
        top.columnconfigure(1, weight=1)

        f = ttk.Frame(self.root, padding=10)
        f.grid()
        ttk.Label(f, text="Oyun profili:").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(f, textvariable=self.profile_var, state="readonly", width=24)
        self.profile_box.grid(row=0, column=1, sticky="we", padx=4)
        self.profile_box.bind("<<ComboboxSelected>>", self._select_profile)
        ttk.Button(f, text="Yeni", width=6, command=self._new_profile).grid(row=0, column=2)
        ttk.Button(f, text="Sil", width=5, command=self._delete_profile).grid(row=0, column=3, padx=(4, 0))

        ttk.Button(f, text=f"Bölge seç  ({hotkey_label('R')})", command=self.pick_region).grid(
            row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))
        self.region_label = ttk.Label(f)
        self.region_label.grid(row=1, column=2, columnspan=2, sticky="w", padx=4, pady=(8, 0))

        self.toggle_btn = ttk.Button(f, state="disabled",
                                     command=lambda: self.set_active(not self.pipeline.active.is_set()))
        self.toggle_btn.grid(row=2, column=0, columnspan=4, sticky="we", pady=8)

        ttk.Label(f, text="Yazı boyutu:").grid(row=3, column=0, sticky="w")
        self.font_var = tk.IntVar()
        ttk.Spinbox(f, from_=10, to=60, textvariable=self.font_var, width=5,
                    command=lambda: self._update_profile(font_size=self.font_var.get())).grid(
            row=3, column=1, sticky="w", padx=4)
        ttk.Label(f, text="Konum:").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.placement_var = tk.StringVar()
        placement = ttk.Combobox(f, textvariable=self.placement_var, state="readonly", width=24,
                                 values=list(PLACEMENTS.values()))
        placement.grid(row=4, column=1, columnspan=3, sticky="w", padx=4, pady=(4, 0))
        placement.bind("<<ComboboxSelected>>", lambda e: self._update_profile(
            placement=next(k for k, v in PLACEMENTS.items() if v == self.placement_var.get())))

        ttk.Separator(f).grid(row=5, column=0, columnspan=4, sticky="we", pady=8)
        self.status_var = tk.StringVar(value="Durduruldu")
        ttk.Label(f, textvariable=self.status_var, foreground="#555").grid(row=6, column=0, columnspan=4, sticky="w")
        self.src_var, self.dst_var, self.timing_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(f, textvariable=self.src_var, wraplength=380, foreground="#777").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(f, textvariable=self.dst_var, wraplength=380, font=(plat.UI_FONT, 10, "bold")).grid(
            row=8, column=0, columnspan=4, sticky="w")
        ttk.Label(f, textvariable=self.timing_var, foreground="#555").grid(row=9, column=0, columnspan=4, sticky="w")
        self.toggle_btn.configure(text=f"Başlat  ({hotkey_label('T')})")

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
                    src, dst, timing, n_lines, frame = args
                    self.overlay.show(dst, frame)
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
