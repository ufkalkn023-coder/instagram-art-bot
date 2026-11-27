# Artfolio · Instagram Art Bot

Müze koleksiyonlarından eserler seçer, Instagram gönderileri hazırlar ve otomatik paylaşır.

## Başlangıç

Python 3.10 ile geliştirme ortamını kurun:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
```

Instagram, Gemini ve Cloudflare R2 ayarları için [yapılandırma rehberine](docs/TECHNICAL-REFERENCE.md#configuration) bakın. Anahtarları repoya koymayın.

## Komutlar

```bash
# Instagram'a göndermeden önizle
python main.py --dry-run --content-mode legacy --mode auto

# Yayın yapılandırmasını kontrol et
python main.py --validate-production-config
python main.py --preflight-feed

# Yayın takvimini gör
python scripts/manage_feed_schedule.py status
```

## Otomatik paylaşım

Hedef saatler Türkiye saatiyle **03:17, 09:17, 15:17 ve 21:17**. GitHub Actions çalışmayı gecikmeli başlatabilir; yayın için aktif sürüm onayı ve güvenlik kontrolleri gerekir.

## Belgeler

- [Ayrıntılı kurulum, mimari ve kullanım](docs/TECHNICAL-REFERENCE.md)
- [Yayın takvimi ve işletim](docs/STAGE-B-SCHEDULE.md)
- [Instagram Insights](docs/TECHNICAL-REFERENCE.md#insights)
