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

### Instagram Insights collector

Artfolio Reel analitiği posting akışından tamamen ayrıdır ve yalnız GET çağrıları yapar. Yerel `artfolio-reels` production history, ReelData ve `output/social` caption dosyaları okunur; son sahipli Instagram medyası içinden yalnız Reels/video kayıtları alınır. Tam veya normalize caption eşleşmesi önceliklidir. Caption dosyası yoksa artwork title + artist + üretim/yayın zamanı birlikte ikincil kanıt olabilir. Birden fazla aday asla otomatik bağlanmaz.

```console
python3 scripts/collect_insights.py
python3 scripts/collect_insights.py --dry-run
python3 scripts/collect_insights.py --check-secrets
```

Normal akışta kullanıcı Instagram permalink’i, media ID’si veya Reel eşlemesi girmez. Kullanıcı Reel’i Instagram Edits ile yayınladıktan sonra saatlik yerel koşu yeni owned Reel/video medyasını keşfeder; üretim zaman penceresindeki yerel Artfolio kayıtlarıyla önce birebir caption, sonra normalize caption, son olarak güçlü title + artist + zaman kanıtıyla global one-to-one eşleştirir. Yalnız tek ve yüksek güvenli aday R2’de `insights/media-associations.json` objesine yazılır. Kalıcı eşlemeler sonraki koşularda önceliklidir ve aynı Reel veya Instagram media ID ikinci kez bağlanamaz.

Yerel Mac için gerekli secret’lar launchd plist’ine veya repo dosyalarına yazılmaz. Bir kerelik Keychain kurulumu, güvenli tanı ve LaunchAgent kurulumu:

```console
cd /Users/ufuk/Desktop/instagram-art-bot-final
python3 scripts/install_insights_launchd.py configure-keychain
python3 scripts/collect_insights.py --check-secrets
python3 scripts/install_insights_launchd.py install
```

`configure-keychain`, her eksik değer için macOS Keychain’in gizli giriş prompt’unu açar; değer komut satırı argümanına, shell history’ye veya log’a girmez. Mevcut process environment değerleri interaktif çalıştırmada önceliklidir, fakat LaunchAgent yalnız Keychain kayıtlarını yükler. Installer idempotent olarak `~/Library/LaunchAgents/com.artfolio.instagram-insights.plist` dosyasını günceller ve user LaunchAgent’ı yeniden yükler. Her login/reboot sonrasında ve en fazla saatte bir tek-seferlik collector çalışır. Dönen operasyon log’ları `~/Library/Logs/Artfolio/instagram-insights.log` altında 1 MiB + üç backup ile sınırlıdır.

Bir credential’ı daha sonra değiştirmek için `python3 scripts/install_insights_launchd.py configure-keychain --force` kullanılır. Log’da `Operation not permitted` görülürse LaunchAgent’ın kullandığı Python interpreter’a macOS Privacy & Security ayarlarından Desktop erişimi verilmelidir.

Devre dışı bırakma ve kaldırma:

```console
python3 scripts/install_insights_launchd.py uninstall
```

Mac uykudayken veya offline iken daemon/polling yapılmaz. Sonraki saatlik koşu mevcut missed-slot politikasını uygular: o anda hâlâ açık olan en yeni 1h/6h/24h/72h/7d slotunu alır, kapanmış eski pencereleri compact `missed_slots` olarak raporlar ve Meta’yı agresif biçimde sorgulamaz.

Ambiguous veya unmatched sonuçlar yazılmaz; özet yalnız örneğin `[insights] ambiguous=1` gösterir. Sadece böyle istisnai bir Reel incelendikten sonra emergency fallback kullanılabilir:

```console
python3 scripts/collect_insights.py --link LOCAL_REEL_ID INSTAGRAM_MEDIA_ID
```

Manuel eşleme otomatik eşlemeye üstün gelir; başka bir manuel eşlemeyi sessizce değiştirmez. Snapshot’lar Reel’in UTC yayın ayına göre `insights/YYYY-MM.json` içinde ETag koşullu ve append-only tutulur.

Hedef slotlar 1, 6, 24, 72 ve 168 saattir. Her slotun penceresi bir sonraki hedefe kadar açıktır; 168 saat slotu 30 güne kadar alınabilir. Gecikmiş koşu en yeni açık slotu alır ve önceki kapanmış pencereleri `missed_slots` olarak raporlar. Boş Meta verisi slotu tüketmez. Ham Meta metrikleri önce saklanır: `views`, `reach`, `likes`, `comments`, `saved`, `shares`, `total_interactions`, `ig_reels_video_view_total_time`, `ig_reels_avg_watch_time`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count`. Desteklenmeyen metrikler ayrı izole edilerek eksik bırakılır; sıfır uydurulmaz. `save_rate`, `share_rate`, `like_rate` ve `comment_rate` yalnız pozitif `reach` varsa `metric / reach` olarak hesaplanır.

Mevcut günlük GitHub workflow değişmeyen 03:00 UTC cron’unda çalışır. GitHub runner yerel Artfolio dosyalarını görmediği için yeni eşleme yapmaz; R2’de önceden oluşmuş association’ların ve mevcut legacy `publications` kayıtlarının due snapshot’larını bağımsız toplar. Yeni GitHub cron’u eklenmemiştir. Analytics hataları publishing state’ini veya history lifecycle’ını etkileyemez. Facebook Login modu için token’da `instagram_basic`, `instagram_manage_insights` ve `pages_read_engagement` izinleri bulunmalıdır. Henüz performans skorlaması veya eser seçimine performance etkisi yoktur.

## History ve duplicate koruması

History, Git commit/push ile değil Cloudflare R2’de saklanır. Kayıtların kısa lifecycle’ı şöyledir:

```text
PENDING → PUBLISHING → PUBLISHED
            ↘ PENDING (kesin publish hatası)
                    ↘ AMBIGUOUS
PENDING → EXPIRED
```

`PENDING` rezervasyonları güvenle stale olduğu kanıtlanırsa expire edilebilir. Instagram publish sınırından hemen önce R2’ye yazılan `PUBLISHING` state’i otomatik expire edilmez; publish sonucu belirsizse `AMBIGUOUS` da kalıcı duplicate kilididir. Bu yaklaşım, yeniden paylaşma riskini availability’ye tercih eder.

History iki ayrı kavramı additive bir şemada tutar:

```json
{
  "posted_artworks": [],
  "publications": [],
  "grid_publication_count": 0,
  "active_color_tone": "warm"
}
```

- `posted_artworks`, eser bazlı duplicate ve lifecycle kilitlerinin authoritative kaynağıdır. Carousel içindeki her eser burada ayrı kayıt olarak kalır.
- `publications`, yalnız Instagram'ın kesin media ID döndürdüğü yeni paylaşımları tutar. Tek paylaşım bir eser ID'si, carousel ise bütün child eser ID'lerini içeren tek publication kaydı üretir.
- `grid_publication_count`, bu şemanın devreye alınmasından sonra kesinleşen feed publication sayısıdır; eski `posted_artworks` kayıtlarından geriye dönük sayı veya carousel grubu tahmin edilmez.
- Eski R2 objesinde `publications` ve sayaç yoksa migration gerekmez. İlk yeni başarılı finalization mevcut `active_color_tone` değerini korur ve forward-only sayacı `1` ile başlatır.

Başarılı Instagram publish sonrasında bütün artwork kayıtlarının `PUBLISHED` yapılması, tek publication eklenmesi ve grid sayacının bir artırılması aynı ETag-korumalı R2 yazısında gerçekleşir. Bu yazı başarısız olursa artwork kayıtları durable `PUBLISHING` kilidinde kalır; `PENDING` durumuna geri alınmaz ve hata görünür biçimde üst katmana iletilir.

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

## Ortam değişkenleri

| Variable | Gerekli mi? | Amaç |
| --- | --- | --- |
| `INSTAGRAM_ACCOUNT_ID` | Production publish ve Reel discovery için gerekli | Instagram Business/Creator account ID |
| `INSTAGRAM_ACCESS_TOKEN` | Production publish için gerekli | Meta Graph API erişim token’ı |
| `INSTAGRAM_ACCESS_TOKEN` | Insights collector için gerekli | Read-only Instagram media Insights erişimi |
| `INSTAGRAM_GRAPH_API_VERSION` | Opsiyonel | Tek Graph API version kaynağını override eder (varsayılan `v22.0`) |
| `ARTFOLIO_REELS_ROOT` | Opsiyonel | Varsayılan sibling konumunda değilse Artfolio Reels repo yolu |
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
