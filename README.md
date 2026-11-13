# RemoteSub

PS5 oyunlarındaki İngilizce altyazıları bilgisayarda Türkçeye çevirip oyun görüntüsünün üstünde gösterir.

```
PS5 ──ev ağı──> chiaki-ng ──ekran──> RemoteSub
                                      ├─ OCR: altyazı bölgesini okur (OneOCR / RapidOCR)
                                      ├─ Çeviri: TranslateGemma, Ollama ile yerelde
                                      └─ Çeviriyi orijinal altyazının üstüne çizer
```

Her şey bilgisayarda çalışır; altyazı metni internete gönderilmez.

## Kurulum (Windows)

1. [Releases](../../releases) sayfasından `RemoteSub-Setup-<sürüm>.exe` dosyasını indirip çalıştır.
   Kurulum, bilgisayarda yoksa **chiaki-ng**'yi de kurar.
2. Kurulum bitince RemoteSub kurulum penceresiyle açılır ve kalan adımları yürütür:
   - **PS5 eşleştirme**: chiaki-ng'de PSN ile giriş ve PS5'teki 8 haneli kod (bir kez).
   - **chiaki-ng ayarları**: `d3d11va` görüntü çözücü ve PS5'e otomatik bağlanma.
   - **Ollama** ve **çeviri modeli**: ekran kartı belleği 10 GB ve üzeriyse `translategemma:12b`, değilse `translategemma:4b`.
   - **OCR**: Windows Ekran Alıntısı Aracı'nın OneOCR'ı; yoksa paketteki RapidOCR.

## Kullanım

- RemoteSub açılınca chiaki-ng'yi başlatır, chiaki-ng PS5'e kendiliğinden bağlanır.
- Her oyun için **Yeni** ile profil oluştur ve altyazının çıktığı bölgeyi seç. Bölge, oyun
  görüntüsüne göre oransal kaydedilir; pencere taşınsa da büyütülse de doğru kalır.

| Kısayol | |
|---|---|
| Ctrl+Alt+T | Çeviriyi başlat / durdur |
| Ctrl+Alt+R | Altyazı bölgesini seç |
| Ctrl+Alt+H | Çeviriyi gizle / göster |
| Ctrl+Alt+S | Çeviri dahil ekran görüntüsü (`%APPDATA%\RemoteSub\shots`) |

Ayarlar, profiller ve log: `%APPDATA%\RemoteSub`. Log'da her çevirinin süresi yazar
(`ilk görülme→ekranda … ms (OCR … ms, çeviri … ms)`).

## Geliştirme

```powershell
pip install -r requirements.txt
python src\remotesub.py            # kaynaktan çalıştır
.\scripts\build_windows.ps1 -Version 0.1.0   # dist\RemoteSub-Setup-0.1.0.exe
```

Derleme için Inno Setup 6 gerekir. `v*` etiketi gönderilince GitHub Actions kurulum dosyasını
derleyip sürüm olarak yayınlar.

| Dosya | |
|---|---|
| `src/remotesub.py` | Arayüz, chiaki-ng pencere takibi, OCR → çeviri döngüsü, çeviri katmanı |
| `src/setup_steps.py` | Kurulum adımları ve kurulum penceresi |
| `src/ocr_engine.py` | OneOCR / RapidOCR seçimi |
| `installer/windows/RemoteSub.iss` | Tek dosyalık kurulum (chiaki-ng kurulumu içinde) |

## Notlar

- **chiaki-ng** ([streetpea/chiaki-ng](https://github.com/streetpea/chiaki-ng), AGPL-3.0) ayrı bir
  program olarak, değiştirilmeden kurulur. RemoteSub onun penceresini okur.
- chiaki-ng 1.9.9'da `chiaki stream` komutu bağlanmadan ana ekranda kalıyordu; bu yüzden
  RemoteSub chiaki-ng'nin kendi otomatik bağlanma ayarını (`auto_connect_mac`) kullanır.
- chiaki-ng'nin otomatik çözücü seçimi (Vulkan) çift ekran kartlı bir bilgisayarda bozuk, mavi
  ve titreyen görüntü verdi; `d3d11va` bu yüzden varsayılan yapılır.
- OneOCR Microsoft'a aittir ve pakete konmaz; çalışırken Windows'un Ekran Alıntısı Aracı'ndan kopyalanır.
