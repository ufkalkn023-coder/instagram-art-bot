# Artfolio · Instagram Art Bot

Müze koleksiyonlarından hakları doğrulanmış eserleri seçer, görsel ve açıklama
hazırlar, Instagram’da tekli gönderi ve carousel olarak dönüşümlü paylaşır.

## Nasıl çalışır?

- **İçerik:** Müze kaynakları ve Gemini 3.8 Flash ile otomatik hazırlanır. İnsan onaylı hazır içerik kullanmak da mümkündür.
- **Yayın:** Resmî Instagram Graph API üzerinden yapılır. Geçmiş ve tekrar koruması Cloudflare R2’de tutulur.
- **Ölçüm:** Instagram Insights verileri ayrı bir akışta toplanır.

## Kurulum

Python **3.10** kullanın. Geliştirme ortamı, yayın bağımlılıklarını da içerir:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
```

Canlı kullanım için Instagram, Gemini ve Cloudflare R2 ayarları gerekir.
Değişkenlerin tamamı [yapılandırma referansında](docs/TECHNICAL-REFERENCE.md#configuration).
Gerçek anahtarları repoya eklemeyin.

## Sık kullanılan komutlar

```bash
# Yerel görsel denemesi; Instagram’a yayın yapmaz
python main.py --dry-run --content-mode legacy --mode auto

# Üretim yapılandırmasını ve yayın hazırlığını kontrol et
python main.py --validate-production-config
python main.py --preflight-feed

# Yayın takvimini ve son başarılı gönderiyi gör
python scripts/manage_feed_schedule.py status

# Test ve lint
python -m pytest -q
python -m ruff check main.py src scripts tests
```

Önizleme, editör ve hazır içerik komutları [teknik referansta](docs/TECHNICAL-REFERENCE.md#editorial-tools).

## Otomatik yayın

Hedef saatler Türkiye saatiyle **03:17 · 09:17 · 15:17 · 21:17**.
GitHub Actions uygunluğu saatte bir kontrol eder; aynı altı saatlik dilimde
bir denemeye izin verir. Gecikmiş yayınlar peş peşe telafi edilmez.

Yayın için etkin schedule ayarı, kullanılan sürüme verilmiş üretim izni ve
geçerli yayın durumu gerekir. Yeni otomatik içerik üretiminde Gemini erişimi zorunludur.
GitHub gecikmeleri gerçek yayın saatini kaydırabilir.

Yayın sonucunu **Actions özetindeki doğrulanmış Instagram kaydından** kontrol edin.
Yetkilendirme, sürüm geçişi ve durdurma adımları [yayın takvimi rehberinde](docs/STAGE-B-SCHEDULE.md).

## Belgeler

| İhtiyaç | Rehber |
| --- | --- |
| Ayarlar, editör, içerik kuyruğu ve mimari | [Teknik referans](docs/TECHNICAL-REFERENCE.md) |
| Üretim izinleri ve yayın takvimi | [Takvim ve operasyon](docs/STAGE-B-SCHEDULE.md) |
| Insights kurulumu ve analiz | [Insights referansı](docs/TECHNICAL-REFERENCE.md#insights) |
| R2 eşzamanlılık doğrulaması | [R2 doğrulama rehberi](docs/r2-integration-verification.md) |
