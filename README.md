# Instagram Sanat Müzesi Paylaşım Otomasyonu

Bu proje, doğrulanmış kamu malı/açık erişimli müze eserlerini seçer, güvenli biçimde indirip 1080×1350 Instagram görseline dönüştürür ve resmi Meta Instagram Graph API üzerinden paylaşır. GitHub Actions zamanlaması, aynı checkout üzerinde önce compile ve test doğrulamasını tamamlar; yalnız ardından production bot çalışır.

## Ne yapar?

- Art Institute of Chicago, Metropolitan Museum of Art, Cleveland Museum of Art, Rijksmuseum, Smithsonian Institution ve Europeana kaynaklarından eser adayları toplar.
- Yalnız doğrulanmış public-domain veya open-access hak bilgisi olan adayları kabul eder. Chicago kaynağında doğrulanmış public-domain eserler için yüksek çözünürlüklü 1686px IIIF türevi kullanılır.
- Duplicate, hak, kalite ve çeşitlilik filtrelerinden geçen görselleri güvenli biçimde indirir; HTTPS-only erişim, private-network/SSRF koruması, redirect yeniden doğrulaması, sınırlı indirme ve Pillow doğrulaması uygular.
- Gerçek indirilen görselin boyutlarını tekrar ölçer; yatay ve panoramik eserleri kırpmadan, temiz nötr bir matte üzerinde 1080×1350 feed görseli olarak sunar.
- Gemini kullanılabiliyorsa caption, alt metin ve görsel metin önerisi üretir; anahtar yoksa veya istek başarısız olursa yerel fallback caption kullanır.
- History bilgisini Cloudflare R2 üzerinde tutar; Instagram publish öncesinde durable kilit kullanarak olası duplicate paylaşımları engeller.

## Çalışma modları

Normal koşuda bot UTC saate göre çalışır:

- UTC 18:00: sekiz eserlik, rastgele temalı carousel.
- Diğer zamanlar: tek eser paylaşımı.
- `--force-carousel`: zamanı dikkate almadan sekiz eserlik carousel çalıştırır.

Carousel teması kod içindeki tema listesinden rastgele seçilir. Carousel için tamamlanmış sekiz aday gerekir; eksik carousel publish edilmez.

Pinterest desteği opsiyoneldir ve yalnız tek-eser akışında `--pinterest` flag’iyle çağrılır. Scheduled workflow bu flag’i vermediği için scheduled production koşuları Pinterest’e otomatik paylaşım yapmaz.

## Zamanlama ve GitHub Actions

Workflow cron değeri değişmeden şudur:

```text
0 6,10,14,18 * * *
```

GitHub Actions cron ifadeleri UTC’dir. Türkiye saati UTC+3 kabul edildiğinde koşular 09:00, 13:00, 17:00 ve 21:00 TSİ’ye karşılık gelir.

Workflow manuel olarak da **Actions → Instagram Art Bot Scheduler → Run workflow** üzerinden başlatılabilir. `force_carousel` girdisi `--force-carousel` olarak iletilir. `instagram-bot` concurrency grubu ve `cancel-in-progress: false` ayarı, aktif bir koşu varken yeni koşuların publish yarışına girmemesini sağlar.

Her workflow invocation şu sırayla ilerler:

```text
dependency install
→ compile validation
→ pytest
→ production bot
```

Install, compile veya test adımı başarısız olursa production adımı çalışmaz. Production secret’ları yalnız publish adımına verilir; compile ve test adımları secret almaz.

## History ve duplicate koruması

History, Git commit/push ile değil Cloudflare R2’de saklanır. Kayıtların kısa lifecycle’ı şöyledir:

```text
PENDING → PUBLISHING → PUBLISHED
            ↘ PENDING (kesin publish hatası)
                    ↘ AMBIGUOUS
PENDING → EXPIRED
```

`PENDING` rezervasyonları güvenle stale olduğu kanıtlanırsa expire edilebilir. Instagram publish sınırından hemen önce R2’ye yazılan `PUBLISHING` state’i otomatik expire edilmez; publish sonucu belirsizse `AMBIGUOUS` da kalıcı duplicate kilididir. Bu yaklaşım, yeniden paylaşma riskini availability’ye tercih eder.

## Kurulum ve yerel kullanım

Fresh checkout için:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest
```

Önemli CLI seçenekleri:

```bash
# Yerel çıktı üretir, publish mutasyonlarını yapmaz
python main.py --dry-run

# Sekiz eserlik carousel çalıştırır
python main.py --force-carousel

# Tek-eser paylaşımından sonra Pinterest cross-post ister
python main.py --pinterest

# Tek-eser Instagram media URL'sini verilen public URL ile override eder
python main.py --image-url https://example.com/artwork.jpg
```

`--image-url` müze seçimini, indirmeyi veya yerel görsel işlemesini bypass etmez; tek-eser publish aşamasında R2 upload yerine kullanılacak public Instagram media URL’sini override eder. Carousel akışı kendi görsellerini R2’ye yükler.

### Dry-run sözleşmesi

`--dry-run` history okuyabilir; müze ve görsel GET istekleri yapabilir; güvenli görsel doğrulaması, Gemini/fallback caption üretimi ve yerel görüntü oluşturmayı çalıştırabilir.

Dry-run şunları yapmaz:

- R2 history mutate/reserve/confirm etmez veya stale recovery yazısı yapmaz.
- Production media’yı R2’ye yüklemez.
- Instagram container oluşturmaz ya da publish etmez.
- Pinterest’e publish etmez.

Dry-run strict offline değildir: `GOOGLE_GEMINI_API_KEY` varsa Gemini’ye dış inference isteği yapabilir.

### Remotion Reel handoff export

Reel planning is an explicit, local boundary; it does not run selection, Gemini,
Remotion, or publishing. Export a serialized, already-normalized artwork plus
its already-validated local image with:

```bash
python -m src.reel_handoff selected-artwork.json --local-image data/raw_artwork.jpg
```

The exporter accepts only the existing `CONFIRMED_PUBLIC_DOMAIN` rights
decision, measures the local asset against the validated dimensions, and writes
an ignored package under `output/reel-handoffs/`. The resulting JSON can be
passed independently to the Remotion planner.

### Automatic Reel candidate acquisition and batch boundary

Before a normal batch, the Art Bot automatically reuses valid existing local
handoffs and acquires only missing capacity from the active museum adapters.
Every fresh candidate must pass the existing confirmed-public-domain decision,
secure image downloader, required handoff metadata, and Reel geometry hard
gates before the existing handoff exporter writes it. Remotion supplies only
the already-produced canonical-ID exclusion set; Art Bot does not read the
production ledger. No Gemini, publishing, or feed history is used during
acquisition.

The default safe pool is `24` (`REEL_CANDIDATE_POOL_SIZE`) and the per-run
examination/download budget is `80` (`REEL_ACQUISITION_MAX_ATTEMPTS`). Both
are validated: pool size must be at least `REEL_BATCH_CANDIDATE_LIMIT`, and
the attempt limit must be at least the pool size. A compact ignored manifest is
written to `output/reel-selection/acquisition.json`; a shortfall is reported
but does not prevent the existing batch shortfall behavior.

For acquisition-only diagnostics:

```bash
python -m src.reel_candidate_acquisition
```

The Art Bot candidate boundary then consumes that local, rights-safe handoff
pool, applies the approved Reel pre-selector and portfolio ordering, and
reuses the existing handoff exporter:

```bash
python -m src.reel_batch_candidates
```

`REEL_SELECTION_TARGET` remains the single target setting (default `4`).
`REEL_BATCH_CANDIDATE_LIMIT` controls queue depth (default `8`, minimum the
target). The compact JSON stdout is the stable cross-repository boundary used
by the Remotion-owned batch runner; it does not fetch museum data or write
publishing/Reel-history state.

## Ortam değişkenleri

| Variable | Gerekli mi? | Amaç |
| --- | --- | --- |
| `INSTAGRAM_ACCOUNT_ID` | Production publish için gerekli | Instagram Business/Creator account ID |
| `INSTAGRAM_ACCESS_TOKEN` | Production publish için gerekli | Meta Graph API erişim token’ı |
| `CLOUDFLARE_R2_ACCOUNT_ID` | Production history ve R2 media için gerekli | R2 account ID |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | Production history ve R2 media için gerekli | R2 access key |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | Production history ve R2 media için gerekli | R2 secret key |
| `CLOUDFLARE_R2_BUCKET_NAME` | Production history ve R2 media için gerekli | History ve media bucket’ı |
| `CLOUDFLARE_R2_PUBLIC_URL` | R2 media upload kullanılıyorsa gerekli | Instagram’ın erişeceği R2 public URL tabanı |
| `GOOGLE_GEMINI_API_KEY` | Opsiyonel | Gemini caption/alt-text üretimi; yoksa fallback kullanılır |
| `RIJKSMUSEUM_API_KEY` | Opsiyonel | Rijksmuseum adapter’ını etkinleştirir; yoksa bu kaynak atlanır |
| `SMITHSONIAN_API_KEY` | Opsiyonel | Smithsonian Institution Open Access API’sini etkinleştirir; yoksa bu kaynak atlanır |
| `EUROPEANA_API_KEY` | Opsiyonel | Europeana API’sini etkinleştirir; yoksa bu kaynak atlanır |
| `PINTEREST_APP_ID` | Opsiyonel | Pinterest OAuth app ID; dört Pinterest değeri birlikte gerekir |
| `PINTEREST_APP_SECRET` | Opsiyonel | Pinterest OAuth app secret |
| `PINTEREST_REFRESH_TOKEN` | Opsiyonel | Pinterest refresh token |
| `PINTEREST_BOARD_ID` | Opsiyonel | Hedef Pinterest board ID |
| `PUBLIC_IMAGE_URL` | Opsiyonel, yalnız tek-eser | R2 upload yerine kullanılacak varsayılan public media URL |
| `ARTFOLIO_SELECTION_SEED` | Opsiyonel | Seçim RNG’si için açık seed |
| `GITHUB_RUN_ID` | GitHub tarafından otomatik | Açık seed yoksa GitHub koşusunun seçim seed’i |

Instagram publish ile R2-backed history zorunludur. `CLOUDFLARE_R2_PUBLIC_URL`, carousel ve normal R2 media upload akışında gerekir; tek-eser akışında `--image-url` veya `PUBLIC_IMAGE_URL` verilirse upload yerine bu URL kullanılır. Pinterest flag’i kullanılmadığında Pinterest credentials gerekli değildir.

## Selection reproducibility

Seçim seed önceliği şöyledir:

```text
ARTFOLIO_SELECTION_SEED
→ GITHUB_RUN_ID
→ local invocation entropy
```

Örneğin `ARTFOLIO_SELECTION_SEED=test-123`, aynı history, config ve API response/dataset altında müze candidate-pool rastgeleliğini ve tek-eser serendipity seçimini tekrar üretmeye yardımcı olur. Bu tüm botu deterministic yapmaz: carousel teması, content type, grid tone ve image border bu seed kapsamının dışındadır.

## Geliştirici doğrulaması

```bash
pytest -q
python3 -m compileall -q main.py src tests
```

`pytest.ini` repo kökünü import path’e eklediği için `PYTHONPATH=.` ayarlamak gerekmez.

## Mimari özeti

```text
GitHub Actions
→ selection
→ rights / duplicate / quality filters
→ secure image validation
→ Gemini or fallback caption
→ image processing
→ R2 reservation / media upload
→ Instagram publishing
→ history confirmation
→ optional Pinterest (single post only)
```

## Dosya yapısı

```text
.
├── .github/workflows/instagram_bot.yml  # Schedule, validation gate, production run
├── src/                                 # Selection, history, processing and API adapters
├── tests/                               # Networkless unit/regression suite
├── config.py                            # Runtime constants
├── main.py                              # CLI orchestration
├── requirements.txt                     # Runtime dependencies
├── pytest.ini                           # Pytest import-path configuration
└── README.md
```
