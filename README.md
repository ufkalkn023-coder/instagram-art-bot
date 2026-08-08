# 🎨 Instagram Sanat Müzesi Paylaşım Otomasyonu

X (Twitter) üzerindeki sanat müzesi hesaplarından esinlenerek hazırlanan; dünya müzelerinden (Chicago Art Institute, Met Museum vb.) kamu malı yüksek çözünürlüklü tabloları otomatik çeken, **bulanık arka plan (Blur Passe-partout)** çerçeve ile işleyen ve **Resmi Meta Instagram Graph API** kullanarak GitHub Actions üzerinde otomatik paylaşan otomasyon sistemi.

---

## 🌟 Öne Çıkan Özellikler

- **%100 Resmi Meta API**: `instagrapi` veya şifre ile giriş kullanılmaz. Meta Graph API sayesinde hesabınız engellenmez veya doğrulamaya takılmaz.
- **Estetik Bulanık Arka Plan (Blur Passe-partout)**: Görseller dikey Instagram formatına (1080x1350 / 4:5) getirilirken, tablonun kendisi arka planda hafifçe büyütülüp bulanıklaştırılır.
- **Yalın Sergi Kartı Formatı**: Ekstra yapay zeka metinleri veya `#hashtag` kalabalığı içermez. Sade sergi kartı görünümündedir:
  ```text
  🎨 [Eser Adı]
  👨‍🎨 [Sanatçı Adı]
  🗓️ [Yapım Yılı]
  🏛️ [Müze Arşivi]
  ```
- **Otomatik Geçmiş Takibi & Git Commit**: Daha önce paylaşılan eserler `data/posted_history.json` dosyasına kaydedilir ve her GitHub Actions çalışması sonunda otomatik repo'ya `git commit & push` yapılır.

---

## 🚀 Kurulum Rehberi

### 1. Gereksinimleri Yükleme ve Yerel Test (`--dry-run`)

Bilgisayarınızda test etmek için terminal açın ve proje dizininde şu komutları çalıştırın:

```bash
# Bağımlılıkları yükleyin
pip install -r requirements.txt

# Instagram'a istek atmadan yerel test gerçekleştirin
python main.py --dry-run
```

Bu işlem sonucunda `data/output_post.jpg` dosyası oluşacak ve estetik bulanık arka planlı görseli inceleyebileceksiniz.

---

### 2. Meta Access Token'ınızı 60 Günlük Token'a Çevirme

Meta Graph API Explorer'dan aldığınız varsayılan Access Token'lar 1-2 saatliktir. Bunu 60 günlük süresiz yenilenebilir token'a çevirmek için projedeki yardımcı betiği çalıştırın:

```bash
python get_long_lived_token.py
```

Sizden Meta App ID, Meta App Secret ve Kısa ömürlü Token'ınızı isteyecek ve çıktı olarak **60 Günlük Long-Lived Token** verecektir.

---

### 3. GitHub Secrets Tanımlama

Projeyi GitHub reponuza yükledikten sonra, GitHub sayfanızda:

1. **Settings** -> **Secrets and variables** -> **Actions** sekmesine gidin.
2. **New repository secret** butonuna tıklayın ve şu 2 gizli değişkeni ekleyin:

- `INSTAGRAM_ACCOUNT_ID`: Instagram İşletme / İçerik Üretici Hesap ID'niz (Meta Graph API'den alınan numerik ID).
- `INSTAGRAM_ACCESS_TOKEN`: 2. adımda oluşturduğunuz 60 Günlük Long-Lived Access Token.

---

### 4. GitHub Actions Zamanlayıcısı (Cron Schedule)

Otomasyon `.github/workflows/instagram_bot.yml` dosyası sayesinde varsayılan olarak **her gün saat 12:00 ve 21:00 (TSİ)** olmak üzere günde 2 kez çalışır.

İstediğiniz zaman GitHub reponuzun **Actions** sekmesinden **Instagram Art Bot Scheduler** -> **Run workflow** butonuna basarak manuel olarak da tetikleyebilirsiniz.

---

## 📁 Proje Dosya Yapısı

```text
.
├── .github/
│   └── workflows/
│       └── instagram_bot.yml    # GitHub Actions Otomasyon Akışı
├── data/
│   ├── output_post.jpg          # İşlenen son dikey görsel
│   └── posted_history.json      # Paylaşılan eserlerin tarihçesi
├── src/
│   ├── art_fetcher.py           # Müze API'lerinden kamu malı eser çekici
│   ├── image_processor.py       # Bulanık arka plan (Blur Passe-partout) modülü
│   ├── instagram_poster.py      # Resmi Instagram Graph API modülü
│   └── history_tracker.py       # JSON geçmiş yöneticisi
├── config.py                    # Boyut ve sabit konfigürasyonlar
├── get_long_lived_token.py      # 60 Günlük Token dönüştürücü betik
├── main.py                      # Ana orkestrasyon dosyası
├── requirements.txt             # Python bağımlılıkları (requests, Pillow)
└── README.md                    # Kurulum ve kullanım rehberi
```
