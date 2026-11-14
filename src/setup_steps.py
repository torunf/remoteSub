"""First-run setup for RemoteSub.

Checks and, where it can, installs and configures everything the app depends on:
chiaki-ng, the PS5 pairing in chiaki-ng, chiaki-ng's settings, Ollama, the TranslateGemma model,
OCR and (on macOS) permissions. Each step fills in the machine-specific config values
(paths, console name, Ollama URL, model). Platform details live in platform_win/platform_mac.
"""
import json
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk

import ocr_engine

if sys.platform == "darwin":
    import platform_mac as plat
else:
    import platform_win as plat

PAIRING_HELP = (
    "PS5'i chiaki-ng ile bir kez eşleştirmek gerekiyor:\n"
    "1. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Uzaktan Oynatmayı Etkinleştir.\n"
    "2. Açılan chiaki-ng penceresinde PS5'ine tıkla. PSN hesabınla giriş yapman istenir; "
    "giriş yapınca hesap kimliği kendiliğinden dolar.\n"
    "3. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Cihazı Bağla. Çıkan 8 haneli kodu chiaki-ng'ye gir.\n"
    "Eşleşme bitince bu pencere kendiliğinden devam eder."
)


# ---------------------------------------------------------------- Ollama and the model

def _normalize_host(host):
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
    setting = plat.ollama_host_setting()
    for url in (_normalize_host(setting) if setting else None, "http://127.0.0.1:11434"):
        if url and ollama_version(url):
            return url
    return None


def start_ollama():
    plat.launch_ollama()
    for _ in range(60):
        url = find_ollama_url()
        if url:
            return url
        time.sleep(1)
    return None


def choose_model():
    # translategemma:12b is clearly better in Turkish; 4b fits machines with less (video) memory.
    return "translategemma:12b" if plat.model_memory_gb() >= plat.LARGE_MODEL_MIN_GB else "translategemma:4b"


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
    def __init__(self, title, check, fix, required=True):
        self.title, self.check, self.fix = title, check, fix
        # Optional steps show their state but never keep the user from starting the app.
        self.required = required


def ready(steps, results):
    """Every required step passed."""
    return all(ok for step, (ok, _) in zip(steps, results) if step.required)


def build_steps(cfg, app_dir):
    """Each check returns (ok, detail) and records what it finds in cfg; each fix may raise."""

    def check_chiaki():
        path = plat.find_chiaki()
        if path:
            cfg["chiaki_path"] = path
            return True, "Kurulu"
        return False, "Kurulu değil"

    def fix_chiaki(report):
        plat.install_chiaki(report)
        if not plat.find_chiaki():
            raise RuntimeError("chiaki-ng kurulamadı")

    def check_pairing():
        hosts = plat.chiaki_hosts()
        if not hosts:
            return False, "PS5 eşleştirilmedi"
        if cfg.get("ps5_nickname") not in [h["nickname"] for h in hosts]:
            cfg["ps5_nickname"] = hosts[0]["nickname"]
        return True, cfg["ps5_nickname"]

    def fix_pairing(report):
        if not plat.chiaki_running():
            plat.apply_chiaki_settings(None)  # decoder default before the first stream
            plat.launch_chiaki(cfg["chiaki_path"])
        report(PAIRING_HELP, None, help_text=True)
        while not plat.chiaki_hosts():
            time.sleep(2)

    def check_chiaki_settings():
        if not cfg.get("ps5_nickname"):
            return False, "Önce PS5 eşleştirilmeli"
        if plat.chiaki_settings_ok(cfg["ps5_nickname"]):
            return True, plat.CHIAKI_SETTINGS_SUMMARY
        return False, "Ayarlanmadı"

    def fix_chiaki_settings(report):
        if not cfg.get("ps5_nickname"):
            raise RuntimeError("Önce PS5 eşleştirilmeli")
        if plat.chiaki_running():
            report("chiaki-ng ayarlar için kapatılıyor…", None)
            plat.close_chiaki()
        plat.apply_chiaki_settings(cfg["ps5_nickname"])
        if not plat.chiaki_settings_ok(cfg["ps5_nickname"]):
            raise RuntimeError("chiaki-ng ayarları yazılamadı")

    def check_ollama():
        url = find_ollama_url()
        if url:
            cfg["ollama_url"] = url
            return True, f"Çalışıyor ({url.split('//')[1]})"
        return False, "Kurulu, çalışmıyor" if plat.ollama_installed() else "Kurulu değil"

    def fix_ollama(report):
        if not plat.ollama_installed():
            plat.install_ollama(report)
            if not plat.ollama_installed():
                raise RuntimeError("Ollama kurulamadı")
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
        return True, ocr_engine.describe(app_dir)

    def fix_permissions(report):
        plat.request_permissions(report)

    steps = [
        Step("chiaki-ng", check_chiaki, fix_chiaki),
        Step("PS5 eşleştirme", check_pairing, fix_pairing),
        Step("chiaki-ng ayarları", check_chiaki_settings, fix_chiaki_settings),
        Step("Ollama", check_ollama, fix_ollama),
        Step("Çeviri modeli", check_model, fix_model),
        Step("OCR", check_ocr, None),
    ]
    if plat.NEEDS_PERMISSIONS:
        # Not required: macOS 27 reported "no access" even after it was granted, which locked people
        # out of the app. A missing permission still shows up when capturing.
        steps.append(Step("İzinler", plat.permissions_ok, fix_permissions, required=False))
    return steps


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
        all_ok = ready(self.steps, results)
        self.done_btn.configure(state="normal" if all_ok and not self.busy else "disabled")
        fixable = any(not ok and step.fix for step, (ok, _) in zip(self.steps, results))
        self.all_btn.configure(state="normal" if fixable and not self.busy else "disabled")

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
                    if ready(self.steps, self.results):
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
        self.on_done(ready(self.steps, self.results))
