# Artfolio · Instagram Art Bot

Müze koleksiyonlarından eserler seçer, Instagram gönderileri hazırlar ve
otomatik paylaşır.

## Başlangıç

Python 3.10 kurulu olmalı.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
```

Instagram, Gemini ve Cloudflare R2 ayarlarını [yapılandırma rehberine](docs/TECHNICAL-REFERENCE.md#configuration) göre ekleyin. Anahtarları GitHub’a veya bu repoya koymayın.

## Komutlar

```bash
# Instagram’a göndermeden önizle
python main.py --dry-run --content-mode legacy --mode auto

# Yayın ayarlarını kontrol et
python main.py --validate-production-config
python main.py --preflight-feed

# Yayın takvimini gör
python scripts/manage_feed_schedule.py status
```

## Otomatik paylaşım

Hedef saatler Türkiye saatiyle **03:17, 09:17, 15:17 ve 21:17**. GitHub
Actions yayını gecikmeli başlatabilir; sonuç ve son başarılı gönderi Actions
özetinde görünür.

## Daha fazla bilgi

- [Ayarlar ve komutlar](docs/TECHNICAL-REFERENCE.md)
- [Yayın takvimi](docs/STAGE-B-SCHEDULE.md)
- [Instagram Insights](docs/TECHNICAL-REFERENCE.md#insights)
