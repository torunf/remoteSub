"""Interface language: English, or Turkish when the system's display language is Turkish.

Strings are written in English in the code and wrapped in _(); TURKISH maps them to Turkish.
A config value "language" ("en", "tr" or "auto") overrides the system choice.
"""
import locale
import os
import subprocess
import sys

_language = "en"


def system_language():
    try:
        if sys.platform == "win32":
            import ctypes
            # Primary language id of the user's display language; 0x1F is Turkish.
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x1F:
                return "tr"
            return "en"
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=5).stdout
            first = next((l.strip(' \t",()') for l in out.splitlines() if l.strip(' \t",()')), "")
            return "tr" if first.lower().startswith("tr") else "en"
    except Exception:
        pass
    name = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    return "tr" if name.startswith("tr") or "turkish" in name else "en"


def set_language(choice=None):
    global _language
    _language = choice if choice in ("en", "tr") else system_language()
    return _language


def language():
    return _language


def _(text, **values):
    if _language == "tr":
        text = TURKISH.get(text, text)
    return text.format(**values) if values else text


TURKISH = {
    # dialogs
    "Yes": "Evet",
    "Cancel": "Vazgeç",
    "OK": "Tamam",
    # downloads
    "Downloading… {done} / {total} MB": "İndiriliyor… {done} / {total} MB",
    # OCR
    "OneOCR (Windows Snipping Tool)": "OneOCR (Windows Ekran Alıntısı Aracı)",
    "RapidOCR (no Snipping Tool)": "RapidOCR (Ekran Alıntısı Aracı yok)",
    "Apple Vision": "Apple Vision",
    # installing (both platforms)
    "An administrator password will be requested…": "Yönetici parolası istenecek…",
    "Couldn't copy {name} to /Applications": "{name} /Applications klasörüne kopyalanamadı",
    "Couldn't open the disk image": "Disk görüntüsü açılamadı",
    "No app in the disk image": "Disk görüntüsünde uygulama yok",
    "auto-connect": "otomatik bağlanma",
    "d3d11va decoder · auto-connect": "d3d11va çözücü · otomatik bağlanma",
    "chiaki-ng for macOS only exists for Apple Silicon (M1 and later)":
        "chiaki-ng'nin macOS sürümü yalnızca Apple Silicon (M1 ve sonrası) için var",
    "chiaki-ng installer not found": "chiaki-ng kurulum dosyası bulunamadı",
    "Downloading chiaki-ng…": "chiaki-ng indiriliyor…",
    "Installing chiaki-ng…": "chiaki-ng kuruluyor…",
    "Installing chiaki-ng with winget… (a permission prompt may appear)":
        "chiaki-ng winget ile kuruluyor… (izin penceresi çıkabilir)",
    "Installing chiaki-ng… (a permission prompt may appear)": "chiaki-ng kuruluyor… (izin penceresi çıkabilir)",
    "Couldn't open the Ollama archive": "Ollama arşivi açılamadı",
    "No app in the Ollama archive": "Ollama arşivinde uygulama yok",
    "Installing Ollama with winget…": "Ollama winget ile kuruluyor…",
    "Installing Ollama…": "Ollama kuruluyor…",
    # updates
    "The update package isn't signed by RemoteSub": "Güncelleme paketinin imzası RemoteSub'a ait değil",
    "The update package isn't notarized by Apple": "Güncelleme paketi Apple onayından geçmemiş",
    "Installing… (an administrator password will be requested)": "Kuruluyor… (yönetici parolası istenecek)",
    "Installation cancelled": "Kurulum iptal edildi",
    "New version {version} · Update": "Yeni sürüm {version} · Güncelle",
    "Update failed: {error}": "Güncelleme olmadı: {error}",
    # permissions (macOS)
    "No Screen Recording permission": "Ekran Kaydı izni yok",
    "Screen Recording (no Accessibility permission for hotkeys)": "Ekran Kaydı (kısayollar için Erişilebilirlik izni yok)",
    "Screen Recording · Accessibility": "Ekran Kaydı · Erişilebilirlik",
    "In System Settings → Privacy & Security → Screen & System Audio Recording, turn RemoteSub on. "
    "For the hotkeys, turn it on under Accessibility too. macOS often only notices the permission "
    "after a restart: once it is on, quit RemoteSub and open it again.":
        "Sistem Ayarları → Gizlilik ve Güvenlik → Ekran ve Sistem Sesi Kaydı'nda RemoteSub'ı aç. "
        "Kısayollar için Erişilebilirlik listesinde de aç. macOS izni çoğu zaman ancak uygulama "
        "yeniden açılınca tanır: izni verdikten sonra RemoteSub'ı kapatıp yeniden aç.",
    "Screen Recording permission not visible yet; if you granted it, reopen RemoteSub":
        "Ekran Kaydı izni henüz görünmüyor; izni verdiysen RemoteSub'ı yeniden aç",
    # setup
    "Pair the PS5 with chiaki-ng once:\n"
    "1. PS5: Settings → System → Remote Play → Enable Remote Play.\n"
    "2. In the chiaki-ng window that opens, click your PS5. Sign in with your PSN account when asked; "
    "your account ID is filled in for you.\n"
    "3. PS5: Settings → System → Remote Play → Pair Device. Enter the 8-digit code shown into chiaki-ng.\n"
    "This window carries on by itself once pairing is done.":
        "PS5'i chiaki-ng ile bir kez eşleştirmek gerekiyor:\n"
        "1. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Uzaktan Oynatmayı Etkinleştir.\n"
        "2. Açılan chiaki-ng penceresinde PS5'ine tıkla. PSN hesabınla giriş yapman istenir; "
        "giriş yapınca hesap kimliği kendiliğinden dolar.\n"
        "3. PS5: Ayarlar → Sistem → Uzaktan Oynatma → Cihazı Bağla. Çıkan 8 haneli kodu chiaki-ng'ye gir.\n"
        "Eşleşme bitince bu pencere kendiliğinden devam eder.",
    "Installed": "Kurulu",
    "Not installed": "Kurulu değil",
    "Couldn't install chiaki-ng": "chiaki-ng kurulamadı",
    "PS5 not paired": "PS5 eşleştirilmedi",
    "Pair the PS5 first": "Önce PS5 eşleştirilmeli",
    "Not set": "Ayarlanmadı",
    "Closing chiaki-ng to change its settings…": "chiaki-ng ayarlar için kapatılıyor…",
    "Couldn't write chiaki-ng's settings": "chiaki-ng ayarları yazılamadı",
    "Running ({address})": "Çalışıyor ({address})",
    "Installed, not running": "Kurulu, çalışmıyor",
    "Couldn't install Ollama": "Ollama kurulamadı",
    "Starting Ollama…": "Ollama başlatılıyor…",
    "Couldn't start Ollama": "Ollama başlatılamadı",
    "{model} (Ollama first)": "{model} (önce Ollama)",
    "{model} not downloaded": "{model} indirilmedi",
    "Install Ollama first": "Önce Ollama kurulmalı",
    "Downloading the model… {done} / {total} MB": "Model indiriliyor… {done} / {total} MB",
    "PS5 pairing": "PS5 eşleştirme",
    "chiaki-ng settings": "chiaki-ng ayarları",
    "Translation model": "Çeviri modeli",
    "Permissions": "İzinler",
    "Error: {error}": "Hata: {error}",
    "RemoteSub · Setup": "RemoteSub · Kurulum",
    "Missing pieces will be installed. The app starts once everything is ready.":
        "Eksik olanlar kurulacak. Hepsi tamamlanınca uygulama başlar.",
    "Install": "Kur",
    "Install all": "Hepsini kur",
    "Start": "Başla",
    "Done.": "Tamamlandı.",
    "Everything is ready.": "Her şey hazır.",
    # placements, backgrounds
    "Cover the region": "Bölgeyi kapla",
    "Above the region": "Bölgenin üstünde",
    "In place of the English text": "İngilizce yazının yerine",
    "Frosted glass": "Buzlu cam",
    "See-through, dimmed": "Şeffaf, karartılmış",
    "Default": "Varsayılan",
    # where a translation came from
    "dictionary": "sözlük",
    "cache": "önbellek",
    "model": "model",
    # capture loop
    "No Screen Recording permission · Setup → Permissions → Install, then reopen the app":
        "Ekran Kaydı izni yok · Kurulum → İzinler → Kur, sonra uygulamayı yeniden aç",
    "chiaki-ng window not found": "chiaki-ng penceresi bulunamadı",
    "No region chosen for this profile ({hotkey})": "Bu profil için bölge seçilmemiş ({hotkey})",
    "chiaki-ng window not visible": "chiaki-ng penceresi görünmüyor",
    "Running · OCR {ms} ms": "Çalışıyor · OCR {ms} ms",
    "Translation error: {error}": "Çeviri hatası: {error}",
    "first seen → on screen {total} ms (OCR {ocr} ms, translation {translation} ms, {origin})":
        "ilk görülme→ekranda {total} ms (OCR {ocr} ms, çeviri {translation} ms, {origin})",
    "Drag over the subtitle area  ·  Esc: cancel": "Altyazı bölgesini fareyle sürükleyerek seçin  ·  Esc: iptal",
    # main window
    "Setup not finished · continue with 'Setup'": "Kurulum tamamlanmadı · 'Kurulum' ile devam edin",
    "Ready · in game, choose the subtitle area with {hotkey}": "Hazır · oyunda {hotkey} ile altyazı bölgesini seçin",
    "Looking for the PS5…": "PS5 aranıyor…",
    "chiaki-ng is already open": "chiaki-ng zaten açık",
    "No PS5 paired in chiaki-ng · see 'Setup'": "chiaki-ng'de eşleştirilmiş PS5 yok · 'Kurulum'a bakın",
    "PS5 not found on the network, trying the last address": "PS5 ağda bulunamadı, son adres deneniyor",
    "PS5 is on": "PS5 açık",
    "PS5 is asleep, waking it": "PS5 uyku modunda, uyandırılıyor",
    "{note} · {host} · connecting": "{note} · {host} · bağlanılıyor",
    "Connection error: {error}": "Bağlantı hatası: {error}",
    "saved": "kayıtlı",
    "not chosen": "seçilmedi",
    "none · every line goes to the model": "yok · her satır modele gider",
    "System font": "Sistem yazı tipi",
    "Montserrat (built-in)": "Montserrat (yerleşik)",
    "New profile": "Yeni profil",
    "Game name:": "Oyunun adı:",
    "Delete profile": "Profili sil",
    "Delete the '{name}' profile?": "'{name}' profili silinsin mi?",
    "Delete": "Sil",
    "Font file": "Yazı tipi dosyası",
    "Font": "Yazı tipi",
    "All files": "Tümü",
    "Couldn't open the font: {error}": "Yazı tipi açılamadı: {error}",
    "This font is missing these letters: {letters}\nUse it anyway?":
        "Bu yazı tipinde şu harfler yok: {letters}\nYine de kullanılsın mı?",
    "Use": "Kullan",
    "Dictionary file (JSON)": "Sözlük dosyası (JSON)",
    "Dictionary": "Sözlük",
    "Couldn't read the file: {error}": "Dosya okunamadı: {error}",
    "Dictionary loaded: {count} lines": "Sözlük yüklendi: {count} satır",
    "chiaki-ng window not found. Start the PS5 stream first.": "chiaki-ng penceresi bulunamadı. Önce PS5 yayınını açın.",
    "Screenshot saved: {name}": "Görüntü kaydedildi: {name}",
    "Stopped": "Durduruldu",
    "Stop translating": "Çeviriyi durdur",
    "Start translating": "Çeviriyi başlat",
    "version {version}": "sürüm {version}",
    "Setup": "Kurulum",
    "Not connected": "Bağlı değil",
    "Connect": "Bağlan",
    "Game profile": "Oyun profili",
    "New": "Yeni",
    "Subtitle area": "Altyazı bölgesi",
    "Choose  ·  {hotkey}": "Seç  ·  {hotkey}",
    "Choose…": "Seç…",
    "Appearance": "Görünüm",
    "Placement": "Konum",
    "Size": "Boyut",
    "Background": "Arka plan",
    "Dimming": "Karartma",
    "{value}%": "%{value}",
    "Last translation": "Son çeviri",
    "Default profile": "Varsayılan",
}
