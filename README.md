# RemoteSub

PS5 oyunlarındaki İngilizce altyazıları bilgisayarda Türkçeye çevirip oyun görüntüsünün üstünde gösterir.
Windows ve macOS (Apple Silicon) için.

```
PS5 ──ev ağı──> chiaki-ng ──görüntü──> RemoteSub
                                        ├─ OCR: altyazı bölgesini okur
                                        ├─ Çeviri: TranslateGemma, Ollama ile yerelde
                                        └─ Çeviriyi orijinal altyazının üstüne çizer
```

Her şey bilgisayarda çalışır; altyazı metni internete gönderilmez.

## Kurulum

[Releases](../../releases) sayfasından işletim sistemine uygun dosyayı indir:

| | Dosya | Not |
|---|---|---|
| Windows 10/11 (x64) | `RemoteSub-Setup-<sürüm>.exe` | Yönetici izni ister |
| macOS 13+ (Apple Silicon) | `RemoteSub-<sürüm>.pkg` | Developer ID ile imzalı ve Apple onaylı (notarized) |

Kurulum dosyası **chiaki-ng**'yi de içinde getirir ve kurar. Kurulum bitince RemoteSub kurulum
penceresiyle açılır ve kalan adımları yürütür:

- **PS5 eşleştirme**: chiaki-ng'de PSN ile giriş ve PS5'teki 8 haneli kod (bir kez).
- **chiaki-ng ayarları**: PS5'e otomatik bağlanma; Windows'ta ayrıca `d3d11va` görüntü çözücü.
- **Ollama** ve **çeviri modeli**: yeterli bellek varsa `translategemma:12b` (Windows'ta ekran kartında
  10 GB, Mac'te 24 GB birleşik bellek), değilse `translategemma:4b`.
- **OCR**: Windows'ta Ekran Alıntısı Aracı'nın OneOCR'ı (yoksa paketteki RapidOCR), Mac'te Apple Vision.
- **İzinler** (yalnızca Mac): Ekran ve Sistem Sesi Kaydı; kısayollar için Erişilebilirlik. Bu adım
  uygulamayı başlatmayı engellemez: macOS 27 izin verilmişken bile "yok" diyebiliyor.

## Güncelleme

macOS'ta RemoteSub açılışta (ve birkaç saatte bir) yeni sürüm olup olmadığına bakar. Varsa kontrol
penceresinde **Yeni sürüm … · Güncelle** düğmesi çıkar; paket indirilir, imzasının RemoteSub'ın
Developer ID'si olduğu ve Apple onayı doğrulanır, kurulur ve uygulama yeniden açılır. Windows'ta
henüz yok; yeni sürümü Releases'tan kurmak gerekir.

## Kullanım

- RemoteSub açılınca chiaki-ng'yi başlatır, chiaki-ng PS5'e kendiliğinden bağlanır.
- Her oyun için **Yeni** ile profil oluştur ve altyazının çıktığı bölgeyi seç. Bölge, oyun
  görüntüsüne göre oransal kaydedilir; pencere taşınsa da büyütülse de doğru kalır.

| Kısayol (Windows / Mac) | |
|---|---|
| Ctrl+Alt+T / Ctrl+⌥+T | Çeviriyi başlat / durdur |
| Ctrl+Alt+R / Ctrl+⌥+R | Altyazı bölgesini seç |
| Ctrl+Alt+H / Ctrl+⌥+H | Çeviriyi gizle / göster |
| Ctrl+Alt+S / Ctrl+⌥+S | Çeviri dahil ekran görüntüsü |

Ayarlar, profiller, log ve ekran görüntüleri: Windows'ta `%APPDATA%\RemoteSub`, Mac'te
`~/Library/Application Support/RemoteSub`. Log'da her çevirinin süresi yazar
(`ilk görülme→ekranda … ms (OCR … ms, çeviri … ms)`).

## Geliştirme

```bash
pip install -r requirements.txt
python src/remotesub.py --no-connect     # kaynaktan çalıştır, PS5'e bağlanmadan
python src/remotesub.py --setup          # kurulum penceresini her durumda göster
```

| Derleme | |
|---|---|
| `scripts\build_windows.ps1 -Version 0.1.0` | `dist\RemoteSub-Setup-0.1.0.exe` (Inno Setup 6 gerekir) |
| `scripts/build_mac.sh 0.1.0` | `dist/RemoteSub-0.1.0.pkg` (Apple Silicon Mac'te) |

`v*` etiketi gönderilince GitHub Actions iki kurulum dosyasını da derleyip sürüm olarak yayınlar.

| Dosya | |
|---|---|
| `src/remotesub.py` | Arayüz, OCR → çeviri döngüsü, çeviri katmanı, bölge seçimi |
| `src/setup_steps.py` | Kurulum adımları ve kurulum penceresi |
| `src/platform_win.py`, `src/platform_mac.py` | Pencere bulma, yakalama, katman, kısayollar, chiaki-ng/Ollama kurulum ve ayarları |
| `src/ocr_engine.py` | OneOCR / RapidOCR / Apple Vision |
| `installer/windows/RemoteSub.iss` | Windows kurulumu (chiaki-ng içinde) |
| `installer/macos/scripts/postinstall` | Mac paketi kurulduktan sonra RemoteSub'ı kurulum penceresiyle açar |

## Notlar

- **chiaki-ng** ([streetpea/chiaki-ng](https://github.com/streetpea/chiaki-ng), AGPL-3.0) kurulum
  dosyalarında değiştirilmeden, ayrı bir program olarak dağıtılır; kaynak kodu o repodadır.
  RemoteSub onun penceresini okur.
- chiaki-ng 1.9.9'da `chiaki stream` komutu bağlanmadan ana ekranda kalıyordu; bu yüzden RemoteSub
  chiaki-ng'nin kendi otomatik bağlanma ayarını (`auto_connect_mac`) kullanır.
- Windows'ta chiaki-ng'nin otomatik çözücü seçimi (Vulkan) çift ekran kartlı bir bilgisayarda bozuk,
  mavi ve titreyen görüntü verdi; `d3d11va` bu yüzden varsayılan yapılır.
- Windows'ta ekran yakalandığı için altyazı bölgesinde chiaki-ng görünür olmalı. Mac'te chiaki-ng
  penceresi doğrudan yakalanır; üstünde başka pencere olması sorun değildir.
- OneOCR Microsoft'a aittir ve pakete konmaz; çalışırken Windows'un Ekran Alıntısı Aracı'ndan kopyalanır.
- macOS paketi CI'da "Developer ID: Furkan Torun" ile imzalanır, Apple'a onaylatılır ve onay pakete
  iliştirilir. Gerekli sertifika ve anahtarlar repo secret'larındadır (`MACOS_*`, `ASC_*`); bunlar yoksa
  derleme ad-hoc imzayla devam eder.
