"""First-run setup for RemoteSub.

Checks and, where it can, installs and configures everything the app depends on:
chiaki-ng, the PS5 pairing in chiaki-ng, Ollama, the TranslateGemma model and OCR.
Each step fills in the machine-specific config values (paths, console name, Ollama URL, model).
"""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import winreg
from tkinter import ttk

import ocr_engine

NO_WINDOW = subprocess.CREATE_NO_WINDOW
CHIAKI_KEY = r"Software\Chiaki\Chiaki"
UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"

PAIRING_HELP = (
    "PS5'i chiaki-ng ile bir kez eşleştirmek gerekiyor:\n"
    "1. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Uzaktan Oynatmayı Etkinleştir.\n"
    "2. Açılan chiaki-ng penceresinde PS5'ine tıkla. PSN hesabınla giriş yapman istenir; "
    "giriş yapınca hesap kimliği kendiliğinden dolar.\n"
    "3. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Cihazı Bağla. Çıkan 8 haneli kodu chiaki-ng'ye gir.\n"
    "Eşleşme bitince bu pencere kendiliğinden devam eder."
)


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def winget_available():
    try:
        return run(["winget", "--version"], timeout=30).returncode == 0
    except Exception:
        return False


def winget_install(package_id):
    run(["winget", "install", "--id", package_id, "-e", "--silent", "--disable-interactivity",
         "--accept-package-agreements", "--accept-source-agreements"], timeout=3600)


def download(url, path, report):
    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                report(f"İndiriliyor… {done >> 20} / {total >> 20} MB", done / total)


def github_latest_asset(repo, match):
    with urllib.request.urlopen(f"https://api.github.com/repos/{repo}/releases/latest", timeout=30) as r:
        release = json.load(r)
    for asset in release.get("assets", []):
        if match(asset["name"].lower()):
            return asset["browser_download_url"], asset["name"]
    return None, None


def run_installer(path, args=()):
    """Run a downloaded installer and wait for it (it may show its own windows and a UAC prompt)."""
    subprocess.run([path, *args])


def uninstall_location(name_part):
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


# ---------------------------------------------------------------- chiaki-ng

def find_chiaki():
    candidates = [os.path.join(os.environ.get(v, ""), "chiaki-ng", "chiaki.exe")
                  for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    candidates.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "chiaki-ng", "chiaki.exe"))
    location = uninstall_location("chiaki-ng")
    if location:
        candidates.insert(0, os.path.join(location, "chiaki.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def chiaki_running():
    out = run(["tasklist", "/FI", "IMAGENAME eq chiaki.exe", "/NH"]).stdout
    return "chiaki.exe" in out.lower()


def chiaki_hosts():
    """Consoles paired in chiaki-ng: [{"nickname", "mac": (value, type)}]."""
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
    """Are the settings from apply_chiaki_settings in place for this console?"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHIAKI_KEY + r"\settings")
        decoder = winreg.QueryValueEx(key, "hw_decoder")[0]
        auto_mac = winreg.QueryValueEx(key, "auto_connect_mac")[0]
    except OSError:
        return False
    host = next((h for h in chiaki_hosts() if h["nickname"] == nickname), None)
    return decoder == "d3d11va" and host is not None and auto_mac == host["mac"][0]


def close_chiaki():
    """Ask chiaki-ng to quit (it would otherwise keep, and later save, its old settings)."""
    run(["taskkill", "/IM", "chiaki.exe"])
    for _ in range(20):
        if not chiaki_running():
            return
        time.sleep(0.5)
    run(["taskkill", "/F", "/IM", "chiaki.exe"])
    time.sleep(1)


# ---------------------------------------------------------------- Ollama and the model

def ollama_exe(name="ollama.exe"):
    path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", name)
    return path if os.path.isfile(path) else (shutil.which(name) if name == "ollama.exe" else None)


def _ollama_host_setting():
    host = os.environ.get("OLLAMA_HOST")
    if not host:
        try:
            host = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment"), "OLLAMA_HOST")[0]
        except OSError:
            host = None
    if not host:
        return None
    host = host.replace("0.0.0.0", "127.0.0.1")
    if "://" not in host:
        host = "http://" + host
    if host.count(":") < 2:
        host += ":11434"
    return host.rstrip("/")


def ollama_version(url):
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=2) as r:
            return json.load(r).get("version")
    except Exception:
        return None


def find_ollama_url():
    for url in (_ollama_host_setting(), "http://127.0.0.1:11434"):
        if url and ollama_version(url):
            return url
    return None


def start_ollama():
    app = ollama_exe("ollama app.exe")
    if app:
        subprocess.Popen([app], creationflags=NO_WINDOW)
    elif ollama_exe():
        subprocess.Popen([ollama_exe(), "serve"], creationflags=NO_WINDOW)
    for _ in range(60):
        url = find_ollama_url()
        if url:
            return url
        time.sleep(1)
    return None


def vram_gb():
    """Largest dedicated video memory among the display adapters, in GB."""
    best = 0
    try:
        cls = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}")
    except OSError:
        return 0
    for i in range(winreg.QueryInfoKey(cls)[0]):
        try:
            value = winreg.QueryValueEx(winreg.OpenKey(cls, winreg.EnumKey(cls, i)), "HardwareInformation.qwMemorySize")[0]
        except OSError:
            continue
        if isinstance(value, bytes):
            value = int.from_bytes(value, "little")
        best = max(best, int(value))
    return best / 2 ** 30


def choose_model():
    # translategemma:12b needs ~8 GB of VRAM and is clearly better in Turkish; 4b fits smaller cards.
    return "translategemma:12b" if vram_gb() >= 10 else "translategemma:4b"


def model_installed(url, model):
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            names = {m["name"] for m in json.load(r).get("models", [])}
    except Exception:
        return False
    return model in names or f"{model}:latest" in names


def pull_model(url, model, report):
    req = urllib.request.Request(url + "/api/pull", json.dumps({"model": model, "stream": True}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        for line in r:
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(msg["error"])
            total, done = msg.get("total"), msg.get("completed")
            if total and done:
                report(f"Model indiriliyor… {done >> 20} / {total >> 20} MB", done / total)
            else:
                report(msg.get("status", ""), None)


# ---------------------------------------------------------------- steps

class Step:
    def __init__(self, title, check, fix):
        self.title, self.check, self.fix = title, check, fix


def build_steps(cfg, app_dir):
    """Each check returns (ok, detail) and records what it finds in cfg; each fix may raise."""

    def check_chiaki():
        path = find_chiaki()
        if path:
            cfg["chiaki_path"] = path
            return True, "Kurulu"
        return False, "Kurulu değil"

    def fix_chiaki(report):
        if winget_available():
            report("chiaki-ng winget ile kuruluyor… (izin penceresi çıkabilir)", None)
            winget_install("StreetPea.chiaki-ng")
        if not find_chiaki():
            report("chiaki-ng indiriliyor…", None)
            url, name = github_latest_asset("streetpea/chiaki-ng",
                                            lambda n: n.endswith(".exe") and "win" in n and "x64" in n)
            if not url:
                raise RuntimeError("chiaki-ng kurulum dosyası bulunamadı")
            path = os.path.join(tempfile.gettempdir(), name)
            download(url, path, report)
            report("chiaki-ng kurulumu açıldı, adımları tamamla…", None)
            run_installer(path)
        if not find_chiaki():
            raise RuntimeError("chiaki-ng kurulamadı")

    def check_pairing():
        hosts = chiaki_hosts()
        if not hosts:
            return False, "PS5 eşleştirilmedi"
        if cfg.get("ps5_nickname") not in [h["nickname"] for h in hosts]:
            cfg["ps5_nickname"] = hosts[0]["nickname"]
        return True, cfg["ps5_nickname"]

    def fix_pairing(report):
        if not chiaki_running():
            apply_chiaki_settings(None)  # decoder default before the first stream
            subprocess.Popen([cfg["chiaki_path"]], creationflags=NO_WINDOW)
        report(PAIRING_HELP, None, help_text=True)
        while not chiaki_hosts():
            time.sleep(2)

    def check_chiaki_settings():
        if not cfg.get("ps5_nickname"):
            return False, "Önce PS5 eşleştirilmeli"
        if chiaki_settings_ok(cfg["ps5_nickname"]):
            return True, "d3d11va çözücü · otomatik bağlanma"
        return False, "Ayarlanmadı"

    def fix_chiaki_settings(report):
        if not cfg.get("ps5_nickname"):
            raise RuntimeError("Önce PS5 eşleştirilmeli")
        if chiaki_running():
            report("chiaki-ng ayarlar için kapatılıyor…", None)
            close_chiaki()
        apply_chiaki_settings(cfg["ps5_nickname"])
        if not chiaki_settings_ok(cfg["ps5_nickname"]):
            raise RuntimeError("chiaki-ng ayarları yazılamadı")

    def check_ollama():
        url = find_ollama_url()
        if url:
            cfg["ollama_url"] = url
            return True, f"Çalışıyor ({url.split('//')[1]})"
        return False, "Kurulu, çalışmıyor" if ollama_exe() else "Kurulu değil"

    def fix_ollama(report):
        if not ollama_exe():
            if winget_available():
                report("Ollama winget ile kuruluyor…", None)
                winget_install("Ollama.Ollama")
            if not ollama_exe():
                path = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
                download(OLLAMA_SETUP_URL, path, report)
                report("Ollama kurulumu açıldı, adımları tamamla…", None)
                run_installer(path)
        report("Ollama başlatılıyor…", None)
        if not start_ollama():
            raise RuntimeError("Ollama başlatılamadı")

    def check_model():
        url = cfg.get("ollama_url") or find_ollama_url()
        if not cfg.get("model"):
            cfg["model"] = choose_model()
        if not url:
            return False, f"{cfg['model']} (önce Ollama)"
        if model_installed(url, cfg["model"]):
            return True, cfg["model"]
        return False, f"{cfg['model']} indirilmedi"

    def fix_model(report):
        url = cfg.get("ollama_url") or find_ollama_url()
        if not url:
            raise RuntimeError("Önce Ollama kurulmalı")
        pull_model(url, cfg["model"], report)

    def check_ocr():
        if ocr_engine.prepare_oneocr(os.path.join(app_dir, "oneocr")):
            return True, "OneOCR (Windows Ekran Alıntısı Aracı)"
        return True, "RapidOCR (Ekran Alıntısı Aracı yok)"

    return [
        Step("chiaki-ng", check_chiaki, fix_chiaki),
        Step("PS5 eşleştirme", check_pairing, fix_pairing),
        Step("chiaki-ng ayarları", check_chiaki_settings, fix_chiaki_settings),
        Step("Ollama", check_ollama, fix_ollama),
        Step("Çeviri modeli", check_model, fix_model),
        Step("OCR", check_ocr, None),
    ]


def run_checks(steps):
    results = []
    for step in steps:
        try:
            results.append(step.check())
        except Exception as e:
            results.append((False, f"Hata: {e}"))
    return results


# ---------------------------------------------------------------- window

class SetupWindow:
    def __init__(self, root, steps, results, on_done):
        self.steps, self.on_done = steps, on_done
        self.events = queue.Queue()
        self.busy = False
        self.win = tk.Toplevel(root)
        self.win.title("RemoteSub · Kurulum")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        f = ttk.Frame(self.win, padding=14)
        f.grid(sticky="nsew")
        ttk.Label(f, text="Eksik olanlar kurulacak. Hepsi tamamlanınca uygulama başlar.",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.rows = []
        for i, step in enumerate(steps):
            ttk.Label(f, text=step.title, width=19).grid(row=i + 1, column=0, sticky="w", pady=3)
            status = ttk.Label(f, width=38)
            status.grid(row=i + 1, column=1, sticky="w")
            button = ttk.Button(f, text="Kur", width=8, command=lambda i=i: self._fix([i]))
            button.grid(row=i + 1, column=2, padx=(8, 0))
            self.rows.append((status, button))

        n = len(steps) + 1
        self.progress = ttk.Progressbar(f, length=440, mode="determinate")
        self.progress.grid(row=n, column=0, columnspan=3, sticky="we", pady=(12, 4))
        self.message = ttk.Label(f, wraplength=440, foreground="#444")
        self.message.grid(row=n + 1, column=0, columnspan=3, sticky="w")
        self.help = ttk.Label(f, wraplength=440, justify="left")
        self.help.grid(row=n + 2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(f)
        buttons.grid(row=n + 3, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self.all_btn = ttk.Button(buttons, text="Hepsini kur", command=self._fix_all)
        self.all_btn.grid(row=0, column=0, padx=(0, 6))
        self.done_btn = ttk.Button(buttons, text="Başla", command=self._finish)
        self.done_btn.grid(row=0, column=1)

        self._show_results(results)
        self.win.after(50, self._pump)

    def _show_results(self, results):
        self.results = results
        for (ok, detail), (status, button), step in zip(results, self.rows, self.steps):
            status.configure(text=("✓  " if ok else "✗  ") + detail, foreground="#1a7f37" if ok else "#b42318")
            button.configure(state="disabled" if ok or not step.fix or self.busy else "normal")
        all_ok = all(ok for ok, _ in results)
        self.done_btn.configure(state="normal" if all_ok and not self.busy else "disabled")
        self.all_btn.configure(state="disabled" if all_ok or self.busy else "normal")

    def _fix_all(self):
        self._fix([i for i, (ok, _) in enumerate(self.results) if not ok and self.steps[i].fix])

    def _fix(self, indices):
        if self.busy:
            return
        self.busy = True
        self._show_results(self.results)
        threading.Thread(target=self._worker, args=(indices,), daemon=True).start()

    def _worker(self, indices):
        def report(text, fraction, help_text=False):
            self.events.put(("help" if help_text else "message", text, fraction))

        try:
            for i in indices:
                step = self.steps[i]
                if step.check()[0]:
                    continue
                self.events.put(("message", f"{step.title}…", None))
                step.fix(report)
                self.events.put(("help", "", None))
                self.events.put(("results", run_checks(self.steps)))
            self.events.put(("message", "Tamamlandı.", 1.0))
        except Exception as e:
            self.events.put(("message", f"Hata: {e}", None))
        finally:
            # Checks can wait on the network, so run them here rather than on the UI thread.
            self.events.put(("idle", run_checks(self.steps)))

    def _pump(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind in ("message", "help"):
                    text, fraction = args
                    (self.message if kind == "message" else self.help).configure(text=text)
                    if fraction is None:
                        if kind == "message":
                            self.progress.configure(mode="indeterminate")
                            self.progress.start(15)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=fraction * 100)
                elif kind == "results":
                    self._show_results(args[0])
                elif kind == "idle":
                    self.busy = False
                    self.progress.stop()
                    self._show_results(args[0])
                    if all(ok for ok, _ in self.results):
                        self.message.configure(text="Her şey hazır.")
                        self.progress.configure(mode="determinate", value=100)
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(50, self._pump)

    def _finish(self):
        self.win.destroy()
        self.on_done(True)

    def _close(self):
        if self.busy:
            return
        self.win.destroy()
        self.on_done(all(ok for ok, _ in self.results))
