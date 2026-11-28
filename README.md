# Artfolio · Instagram Art Bot

Müze koleksiyonlarından eserler seçer; yerel aylık planlama/paketleme ve
GitHub Actions tabanlı otomatik Instagram Feed/Reel akışları için araçlar sunar.

## Başlangıç

Python 3.10 kurulu olmalı.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
```

Instagram, Gemini ve Cloudflare R2 ayarlarını [yapılandırma rehberine](docs/TECHNICAL-REFERENCE.md#configuration) göre ekleyin. Anahtarları GitHub’a veya bu repoya koymayın.

## Akışlar ve giriş noktaları

### 1. Aylık carousel planı ve 30 günlük grid önizlemesi

Yerel çalışma alanı oluşturup 30 günlük 3×3 grid ve tasarım stüdyosunu açmak için:

```bash
# 30 günlük Carousel + Reels planı çalışma alanı oluştur (varsayılan: 4 carousel + 2 reel/gün)
python scripts/review_monthly_plan.py create --output data/monthly-plan

# Yerel 30 günlük grid ve inceleme stüdyosunu aç (varsayılan: http://127.0.0.1:38150/)
python scripts/review_monthly_plan.py serve --workspace data/monthly-plan
```

Doğrulanmış müze havuzundan (`pool_index.json`) haftalık/aylık taşınabilir carousel teslim paketi oluşturmak için:

```bash
python scripts/review_monthly_plan.py build-carousel-delivery \
  --pool /path/to/pool_index.json --output data/carousel-delivery
```

### 2. Meta Business Suite yerel paketi

Aylık çalışma alanında onaylanan gönderilerden Meta Business Suite için yerel paket üretmek için:

```bash
python scripts/review_monthly_plan.py export-meta \
  --workspace data/monthly-plan --output data/meta-export --split-weeks
```

Tüm gönderi seçenekleri ve tekrar defteri kuralları için [aylık stüdyo rehberine](docs/TECHNICAL-REFERENCE.md#monthly-studio) bakın.

### 3. Otomatik Feed akışı (GitHub Actions)

Otomatik Feed akışı repoda [`.github/workflows/instagram_bot.yml`](.github/workflows/instagram_bot.yml) workflow’u üzerinden tanımlıdır (`cron: "17 * * * *"`, `ARTFOLIO_FEED_CONTENT_MODE=automatic`, `python main.py --mode auto`). Nominal hedef saatler Türkiye saatiyle **03:17, 09:17, 15:17 ve 21:17** olarak tanımlanmıştır; GitHub Actions koşuyu geciktirebilir.

> Not: Workflow’un repoda tanımlı olması canlı GitHub Actions’ta etkin olduğu anlamına gelmez; canlı etkinlik repository variable’larına ve onaylı sürüm/takvim iznine bağlıdır ([içerik modları ve kuyruk kuralları](docs/TECHNICAL-REFERENCE.md#content-modes)).

Yerel kontrol ve önizleme komutları:

```bash
# Yayın ayarlarını ve canlı hazırlığı salt okunur kontrol et
python main.py --validate-production-config
python main.py --preflight-feed

# Yayın takvimi durumunu gör
python scripts/manage_feed_schedule.py status

# Instagram’a göndermeden önizle (varsayılan automatic mod Gemini anahtarı gerektirir)
python main.py --dry-run --mode auto
python main.py --dry-run --content-mode legacy --mode auto
```

### 4. İnsan onaylı editoryal masa (v2)

```bash
python scripts/review_editorial_workspace.py serve --workspace data/editorial-workspace
python scripts/prepare_feed_queue.py --directory data/editorial-queue --target 4 \
  --first-format single --editorial-workspace data/editorial-workspace --check-editorial
```

## Daha fazla bilgi

- [Ayarlar, modlar ve komutlar](docs/TECHNICAL-REFERENCE.md)
- [İçerik modları ve kuyruk kuralları](docs/TECHNICAL-REFERENCE.md#content-modes)
- [Aylık stüdyo ve Meta Business Suite paketi](docs/TECHNICAL-REFERENCE.md#monthly-studio)
- [Yayın takvimi](docs/STAGE-B-SCHEDULE.md)
- [Instagram Insights](docs/TECHNICAL-REFERENCE.md#insights)
