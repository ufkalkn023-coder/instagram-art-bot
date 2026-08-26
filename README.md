# Instagram Sanat Müzesi Paylaşım Otomasyonu

Bu proje, doğrulanmış kamu malı/açık erişimli müze eserlerini seçer, güvenli biçimde indirir ve resmi Meta Instagram Graph API üzerinden paylaşır. GitHub Actions zamanlaması, aynı checkout üzerinde önce compile ve test doğrulamasını tamamlar; yalnız ardından production bot çalışır.

## Ne yapar?

- Art Institute of Chicago, Metropolitan Museum of Art, Cleveland Museum of Art,
  Rijksmuseum, Smithsonian Institution ve Europeana kaynaklarından eser adayları toplar.
- Yalnız doğrulanmış public-domain veya open-access hak bilgisi olan adayları kabul eder. Chicago kaynağında hibrit görsel analiz doğrudan önerilen 843px IIIF türevini, doğrulanmış public-domain final render ise 1686px türevini ve güvenli 843px fallback'i kullanır. AIC IIIF istekleri süreç içinde tekilleştirilip yaklaşık saniyede bir isteğe sınırlandırılır ve `AIC-User-Agent` ile tanımlanır.
- Duplicate, hak, kalite ve çeşitlilik filtrelerinden geçen görselleri güvenli biçimde indirir; HTTPS-only erişim, private-network/SSRF koruması, redirect yeniden doğrulaması, sınırlı indirme ve Pillow doğrulaması uygular.
- Single post'larda Instagram ile doğrudan uyumlu JPEG byte'larını ve doğal aspect ratio'yu zero-touch korur; yalnız platform uyumluluğu gerektiren kaynakları dönüştürür.
- Carousel featured eserlerini ortak 1080×1350 (4:5) editorial canvas üzerinde, tam eseri contain ederek ve tek kez compositing yaparak gösterir. Boş alan carousel boyunca ortak deterministic nötr gallery field'dır; crop, stretch, blurred clone veya metadata overlay yoktur.
- Gemini kullanılabiliyorsa caption, alt metin ve görsel metin önerisi üretir; anahtar yoksa veya istek başarısız olursa yerel fallback caption kullanır.
- History bilgisini Cloudflare R2 üzerinde tutar; Instagram publish öncesinde durable kilit kullanarak olası duplicate paylaşımları engeller.
- Kaynak hatalarını `source_health` kategorileriyle izole eder; bir adapter'ın erişim,
  rate-limit veya upstream hatası diğer beş kaynağın seçimini durdurmaz.

## Çalışma modları

Normal koşuda bot UTC saate göre çalışır:

- UTC 12:00 ve 21:00: bir editorial cover ve adaptive 3–8 featured eserden oluşan temalı carousel.
- Diğer zamanlar: tek eser paylaşımı.
- `--force-carousel`: zamanı dikkate almadan adaptive carousel çalıştırır.

Carousel teması doğrulanmış registry ve deterministic Theme Planner tarafından seçilir. Carousel için tamamlanmış 3–8 featured aday ile bunlardan farklı, hakları doğrulanmış bir cover gerekir; eksik carousel publish edilmez.

Pinterest desteği opsiyoneldir ve yalnız tek-eser akışında `--pinterest` flag’iyle çağrılır. Scheduled workflow bu flag’i vermediği için scheduled production koşuları Pinterest’e otomatik paylaşım yapmaz.

## Artfolio Theme Registry

Carousel tema evreni `data/carousel_themes.json` içindeki data-driven registry'de tutulur. Her kayıt stable snake_case ID, İngilizce editorial title, kontrollü theme family ve carousel format, açık primary/secondary sorgular, required/preferred/excluded relevance sinyalleri, opsiyonel seasonal aylar ve sınırlı editorial priority taşır. Required sinyaller varsayılan olarak OR grubudur; yalnız gerçekten iki kavramı birlikte gerektiren temalar küçük, backward-compatible `required_term_groups` alanıyla AND-of-OR mantığı kullanabilir. Python katmanı şemayı, enum değerlerini, sorgu dizilerini, ay aralığını ve duplicate ID'leri doğrular; bozuk registry sessizce fallback yapmadan çalışmayı durdurur. Yeni tema eklemek carousel orchestration kodunu değiştirmeyi gerektirmez.

Carousel acquisition registry sorgularını güç sırasıyla ve sınırlı çalıştırır: en fazla üç primary, gerektiğinde iki secondary sorgu; sorgu başına adapter başına 16 aday ve tema başına en fazla 20 adapter çağrısı. Her logical sorgudan sonra pool yeniden değerlendirilir ve yeterli headroom oluştuğunda kalan sorgular çağrılmaz. Aynı canonical eser farklı sorgulardan gelirse tek aday kalır, fakat primary/secondary türü, sıra ve matched-query provenance'ı idempotent biçimde birleştirilir. Featured eserler ile editorial cover aynı typed acquisition pool'unu kullanır; cover nihai 3–8 featured ID'yi dışlar ve kendi secure-download ile cover-score kontrollerinden ayrıca geçer.

Metadata matching Unicode NFKC/casefold, punctuation ve whitespace normalizasyonu ile exact token/phrase boundary kullanır. Artist adı, müze adı ve credit/copyright metni tematik kanıt sayılmaz. Required sinyaller hard gate'tir, preferred sinyaller sınırlı bonus verir, excluded sinyaller güvenilir descriptive alanda bulunduğunda aday reddedilir. `theme_relevance_score` ayrı bir 0–100 ölçüdür ve varsayılan geçiş eşiği 60'tır. Carousel sıralamasında relevance baskın, teknik `quality_score` önemli ama ikincildir; yüksek kalite düşük relevance'ı kurtaramaz. `quality_score` yalnız eserin teknik/metadata kullanılabilirliğini ölçmeye devam eder.

Tema kanıtı `METADATA` veya `HYBRID` olarak tanımlanır. Subject/iconography gibi `METADATA` temaları mevcut metadata ve query provenance politikasını korur. Renk ve ışık odaklı `HYBRID` temalar ise düşük eşikli metadata/query prequalification sonrasında, yalnız secure validation ile zaten indirilmiş dosyadan çıkarılan bounded luminance, contrast ve coarse color sinyallerini final relevance'a ekler. Pixel kanıtı yalnız gerçekten ölçebildiği görsel özelliği destekler; örneğin parlaklık “winter”, renk ise “candle” veya “woman reading” semantiğinin yerine geçmez. Final geçiş eşiği her iki modda da aynıdır.

Secure validation'dan geçen en güçlü en fazla 24 finalist için candidate score'dan ayrı, açıklanabilir bir carousel set score hesaplanır. Bounded optimizer 3–8 arasındaki her uygulanabilir cardinality için en iyi seti değerlendirir; artist/müze/region dominance sınırlarını korurken period, medium, orientation, luminance ve kaba renk çeşitliliğini soft sinyal olarak kullanır. Normalized editorial utility mevcut ortalama individual strength, diversity bonusları, format adjustment'ları ve redundancy cezalarından oluşur. Minimum üçlüden sonraki her boyut için en az değer katan eserin mevcut `individual_strength` değeri, o eseri çıkarmanın set score'a etkisinin iki katıyla (±5 puanda bounded) ayarlanır; relaxed hard-cap profili dört puan ceza alır. Sonuç 70 inclusion eşiğinin altına düştüğünde büyüme durur. Böylece zayıf veya redundant tail sırf sekize tamamlamak için eklenmez; çeşitliliğe anlamlı katkı veren orta güçte bir eser eklenebilir. Pillow ile zaten indirilmiş dosyadan çıkarılan hafif görsel özellikler tekrar download gerektirmez. Format policy, örneğin `REGIONAL` için ortak region'ı, `PERIOD_FOCUS` için ortak period'u ve `COLOR_STUDY` için hedef renk benzerliğini gereksiz redundancy saymaz.

Set seçimi tamamlandıktan sonra seçilen 3–8 featured eser ayrı bir deterministic sequencing aşamasından geçer. Caption numaraları, `carousel_01.jpg`–`carousel_NN.jpg` artifact sırası ve history `featured_position=1..N` değerleri bu nihai sırayı izler; padding, placeholder veya duplicate slide üretilmez. `CHRONOLOGICAL` format bilinen tarihleri artan sıraya dizer ve bilinmeyen tarihleri stable ID sırasıyla sona koyar; diğer formatlarda opener/theme establishment, adjacent visual rhythm, güçlü eser dağılımı ve closure birlikte puanlanır.

Availability preflight image indirmeden canonical/history duplicate, confirmed rights, format target, pre-download metadata/image prerequisites ve relevance kapılarını bu sırayla sayar. `estimated_safe_pool`, son relevance kapısından geçen fakat henüz secure image validation'dan geçmemiş eser sayısıdır. Absolute construction minimum dört unique eserdir: en az üç featured ve bunlardan farklı bir cover. Dört ile on bir aday arası publish edilebilir ama dar pool, 12 aday rahat operational headroom sayılır. `minimum_candidate_target` yalnız preferred acquisition headroom/early-stop hedefidir; 24 gibi registry değerleri tek başına daha küçük ama publish edilebilir bir pool'u unavailable yapmaz. Pipeline minimum dört adayla construction aşamasına geçebilir, fakat bounded primary/secondary acquisition ve en fazla 40 secure validation denemesi mümkün olduğunda 24 finalistlik optimizer headroom'unu toplamaya devam eder. Optimizer her boyutta en az bir cover-eligible ID'yi featured set dışında bırakır; sekizli set tek cover seçeneğini tüketiyorsa uygulanabilir yedili set seçilebilir. İlk planner teması yeterli değilse planner'ın deterministic sırasındaki sonraki tema denenir; en fazla beş tema değerlendirilir. Yalnız tamamlanmış 1 cover + 3–8 featured planının teması history'ye yazılır. `MONOGRAPHIC` ve `MUSEUM_SPOTLIGHT` aynı adaptive boyut politikasını ve typed format hedeflerini kullanır; cover yine hedefe uygun, ayrı bir eserdir.

Theme Planner aynı selection-run seed'i kullanarak global random state'i değiştirmeden deterministic serendipity üretir. Son carousel publication slot'larında aynı theme ID için orta/güçlü, aynı family içindeki farklı temalar için kademeli soft fatigue ve format tekrarları için küçük, sınırlı soft fatigue uygular. Artwork sayısı fatigue'i çoğaltmaz: aynı `publication_id` altındaki bir cover ve 3–8 featured kayıt history'de tek tema slotudur. Eski dokuz satırlı yayınlar aynı şekilde okunmaya devam eder. Tema tanımındaki ay eşleşmesi yalnız küçük bir pozitif boost verir; sezon dışında hiçbir tema yasaklanmaz. Seçilen temanın base, theme fatigue, family fatigue, format fatigue, seasonal, editorial ve serendipity bileşenleri loglanır.

### Format-specific carousel contracts

Format policy, temanın istediği bilinçli benzerliği istenmeyen tekrardan ayırır:

- `THEMATIC_COLLECTION` ortak tema relevance'ını korurken artist, museum, region ve görsel çeşitliliği mevcut genel kurallarla dengeler.
- `COMPARATIVE` ortak ekseni hard relevance gate olarak tutar; period, region, artist, medium ve gerçek görsel özelliklerde kontrastı, varsa registry'deki tercih edilen comparison dimension'ı daha güçlü ödüllendirir.
- `CHRONOLOGICAL` bilinen tarihleri tercih eder; güvenli biçimde parse edilen exact, approximate, range ve century tarihleriyle zaman aralığını ve dağılımı set seçimi sırasında ödüllendirir, sonra bilinenleri artan ve bilinmeyenleri stable sırada dizer.
- `MONOGRAPHIC` 3–8 featured eser ile ayrı cover'ın typed artist target/alias kimliğine exact canonical eşleşmesini zorunlu kılar. Aynı artist redundancy değildir; museum, tarih, medium ve görsel farklılıklar soft hedeflerdir.
- `MUSEUM_SPOTLIGHT` 3–8 featured eser ile ayrı cover'ın typed museum target/alias kimliğine exact canonical eşleşmesini zorunlu kılar ve mümkünse acquisition'ı registry'deki adapter source'larına daraltır. Aynı museum redundancy değildir; artist, period, region, medium ve görsel çeşitlilik aranır.

`REGIONAL`, `PERIOD_FOCUS`, `MEDIUM_FOCUS`, `COLOR_STUDY`, `LIGHT_STUDY`, `ICONOGRAPHIC` ve `VISUAL_PATTERN` formatlarında temanın tanımlayıcı ortak boyutu kendi başına redundancy cezası üretmez. Artist ve museum adları yalnız açık metadata ile canonical eşleştirilir; description veya credit içinde geçen bir ad target kimliği sayılmaz.

## Zamanlama ve GitHub Actions

Workflow aynı sekiz UTC slotunu iki açık production moduna böler:

```text
single:   0 0,3,6,9,15,18 * * *
carousel: 0 12,21 * * *
```

GitHub Actions cron ifadeleri UTC’dir. Türkiye saati UTC+3 kabul edildiğinde koşular 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 ve ertesi gün 00:00 TSİ’ye karşılık gelir.

Scheduled invocation `--mode single` veya `--mode carousel` değerini cron lane’inden açıkça geçirir. Böylece GitHub’ın geciktirdiği bir job, başladığı wall-clock saatine bakarak yanlış moda geçmez. Yerel ve varsayılan manuel çağrılardaki legacy `auto` davranışı UTC 12:00/21:00 için carousel seçmeye devam eder.

Workflow manuel olarak da **Actions → Instagram Art Bot Scheduler → Run workflow** üzerinden başlatılabilir. `force_carousel` girdisi `--force-carousel` olarak iletilir. Yalnız bu production workflow’una ait `instagram-bot` concurrency grubu aynı anda tek publish job’ına izin verir; `cancel-in-progress: false` aktif publish’i yarıda kesmez ve yeni koşuyu bekletir (GitHub birden fazla pending koşudan en yenisini tutabilir).

Job timeout’u **45 dakika**dır. Dokuz child ve bir parent container’lı carousel’in bounded Instagram status polling süresine acquisition/render payı bırakır; 2 saatlik stale `PENDING` eşiğinin altında kalır.

Her workflow invocation şu sırayla ilerler:

```text
dependency install
→ compile validation
→ pytest
→ production configuration validation
→ production bot
```

Compile adımı `main.py`, `src`, `scripts` ve `tests` kapsamını; test adımı hızlı olan full `pytest -q` suite’ini çalıştırır. Install, compile, test veya configuration validation başarısız olursa production adımı çalışmaz. Production secret’ları yalnız config-validation ve publish adımlarına verilir; compile ve test adımları secret almaz. Uygulama aynı required-config kontrolünü history okuması veya artwork acquisition başlamadan önce tekrarlar.

### Reel candidate, portfolio ve handoff katmanı

Reel üretimi Instagram feed publisher'ından ayrıdır. `reel_candidate_acquisition`, altı
müze adapter'ından hakları doğrulanmış ve güvenli görseli olan adayları toplar;
`reel_selector` teknik ve editoryal kabul kararlarını, `reel_portfolio` ise batch içi
artist/müze/region çeşitliliğini uygular. `reel_batch_candidates` ve `reel_handoff`,
yerel Remotion tüketicisine required metadata, kaynak kimliği ve doğrulanmış asset
bilgisi taşıyan atomik bir handoff üretir. Bu katman Instagram'a publish etmez ve
feed history lifecycle'ını mutate etmez.

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
cd /Users/ufuk/Desktop/instagram-art-bot-push
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
            ↘ EXPIRED (Meta kesin olarak publish edilmediğini kanıtlarsa)
            ↘ AMBIGUOUS (sonuç kanıtlanamıyorsa)
PENDING → EXPIRED
```

`PENDING`, hiçbir irreversible `media_publish` isteğinin gönderilmediği anlamına gelir. Tek eser creation container ID'si veya carousel parent + child container ID'leri hazır olduktan sonra, `media_publish` çağrısından hemen önce bütün publication unit tek conditional R2 yazısıyla `PUBLISHING` olur. Bu yazı başarısızsa publish isteği gönderilmez. `media_publish` timeout, connection reset, malformed response veya 5xx sonucu otomatik tekrar edilmez; unit `AMBIGUOUS` kalır. Kesin 4xx reddi `EXPIRED`, başarılı response media ID'si ise önce durable receipt, ardından `PUBLISHED` olarak yazılır.

Yeni staging media objeleri yalnız `images/publications/<publication_id>/<timestamp>_<uuid>.<suffix>` altında oluşturulur. Tek eser ve carousel, history reservation'ın döndürdüğü aynı durable `publication_id` değerini kullanır. Upload sonucu exact object key + public URL taşıyan immutable bir handle olarak korunur. Public HEAD doğrulaması başarısızsa yalnız o exact obje silinmeye çalışılır; Meta çağrılmadan önce yarım kalan carousel staging'i yalnız o invocation'ın tamamlanmış handle'larını geri alır.

Crash recovery için authoritative `EXPIRED` geçişi aynı history CAS yazısında additive `staging_media_cleanup_queue` kaydı oluşturur. Cleanup önce lifecycle kararının durable olmasını bekler, sonra yalnız exact publication prefix'ini bounded olarak listeler (en fazla 100 obje, 25 objelik sayfalar) ve yeniden ownership validation'dan geçen key'leri siler. Başarısız cleanup state'i geri açmaz; queue sonraki startup veya manuel reconciliation koşusunda tekrar denenir. Policy fail-closed'dur: `PENDING`, `PUBLISHING`, `AMBIGUOUS` ve `PUBLISHED` media tutulur; yalnız authoritative `EXPIRED` cleanup-eligible'dır. Başarılı media Pinterest'in aynı public URL'yi kullanabilmesi için bu görevde tutulur. Eski `images/<timestamp>_<uuid>.<suffix>` objeleri publication ownership kanıtı taşımadığından otomatik cleanup kapsamı dışındadır.

History iki additive görünümü birlikte korur:

```json
{
  "posted_artworks": [],
  "publications": [],
  "grid_publication_count": 0,
  "active_color_tone": "warm"
}
```

`posted_artworks` eser bazlı duplicate/lifecycle kilididir; carousel cover ve her
featured eser burada role ve `featured_position` bilgisiyle ayrı kalır.
`publications` ise kesinleşmiş her feed postunu tek logical slot olarak indeksler.
Başarılı normal finalization, artwork state'lerini `PUBLISHED` yapmayı, tek
`PublicationRecord` eklemeyi ve `grid_publication_count` değerini artırmayı aynı
exact-ETag conditional write içinde gerçekleştirir. Eski history objeleri additive
alanlar yokken okunmaya devam eder; forward-only sayaç eski satırlardan tahmin edilmez.

Her production başlangıcı yeni seçimden önce en fazla 20 unresolved publication unit'i ve son 30 günlük pencereyi inceler. `PUBLISHING` unit'lerinde aktif 45 dakikalık workflow ile çakışmamak için 50 dakikalık grace kullanılır. Unit başına yalnız parent/creation container için bir logical status lookup yapılır; reconciliation GET çağrısı 10 saniyelik timeout ile en fazla iki HTTP attempt kullanır. `PUBLISHED` container status'u publication'ı kanıtlar; `FINISHED` yalnız publish edilmeye hazır olduğunu gösterir ve `AMBIGUOUS` kalır. Unresolved unit'in kendi canonical ID'leri karantinada kalırken farklı eserler sonraki koşularda seçilebilir.

Manuel reconciliation bütün geçmişten en fazla 100 unresolved unit'i inceler; yeni eser seçmez, container oluşturmaz veya `media_publish` çağırmaz:

```bash
python3 main.py --reconcile-publications
```

Yeni carousel kayıtları mevcut artwork history şemasını koruyarak `theme_id`, `theme_family` ve `carousel_format` alanlarını taşır. Aynı `publication_id` altındaki 4–9 artwork satırı planner için tek slot olarak okunur. Eski `theme` alanları yüklenmeye devam eder; güvenle bilinmeyen legacy family veya format değerleri uydurulmaz.

### Tek-eser çeşitlilik sıralaması

Tek-eser seçimi son 12 doğrulanmış **single publication event** üzerinden orientation, artist ve visual-category tekrarını soft ve deterministic biçimde azaltır. On iki event, görünür feed çevresini kapsayacak kadar uzun, eski bir sanatçıyı veya görünümü kalıcı biçimde baskılamayacak kadar kısadır. Carousel/Reel satırları pencere kurulmadan önce elenir; aynı carousel'in dokuz history satırı tek-eser sinyallerini büyütmez. Legacy single kayıtları okunur, bulunmayan alanlar `UNKNOWN` kalır.

Finalist aşaması metadata puanına göre en güçlü 12 adayı güvenli biçimde birer kez indirir; beş geçerli recovery adayı bulunamazsa sıradaki adaylara yalnız gereken kadar devam eder. EXIF-correct display dimensions ile bounded luminance/coarse-color özellikleri aynı doğrulanmış dosyadan çıkarılır ve final sıralama bir kez hesaplanır. Böylece orientation veya visual fingerprint için ikinci download ya da Gemini/external vision çağrısı yapılmaz. Yalnız final sıralamadaki ilk beş aday single publishability recovery akışına girer.

Orientation sınıfları `PORTRAIT`, `SQUARE`, `LANDSCAPE`, `UNKNOWN` olup kare çevresinde %2 relative tolerance kullanır. Frequency penalty 2/3/4/5/6+ tekrar için sırasıyla `-0.75/-1.5/-2.5/-3.5/-4.5`; kesintisiz 2/3/4+ streak için ayrıca `-1/-2/-3` ve toplam orientation alt sınırı `-7.5` puandır. Bilinen artist tekrarı 1/2/3+ için `-1/-2.5/-4`, immediate repeat için ayrıca `-2` ve artist alt sınırı `-6` puandır. Artist kimliği mevcut Unicode, case, punctuation, whitespace ve surname-first normalizasyonunu kullanır; unknown/anonymous değerler kimlik üretmez.

Visual category, title veya artist adından tahmin edilmez. Kanıt sırası `classification → department → style_or_period → medium`; yalnız açık controlled ifadeler `LANDSCAPE`, `PORTRAITURE`, `STILL_LIFE`, `RELIGIOUS` veya `ABSTRACT` ailesi üretir, aksi halde `UNKNOWN` kalır. Fingerprint semantic family + luminance tone + coarse color'dan oluşur; orientation ayrı bileşen olduğu için fingerprint içinde tekrar puanlanmaz. Semantic frequency 1/2/3+ için `-0.5/-1/-1.5`, immediate semantic streak için en fazla `-0.75`; tone ve color 2/3/5+ için ayrı ayrı `-0.25/-0.5/-0.75`; immediate matching fingerprint için `-1` veya streak halinde `-1.5` uygulanır. Visual-category toplamı `-5` ile sınırlıdır.

Nihai tek-eser formülü: `quality_score + museum + region + orientation + artist + visual_category + discovery + seeded_serendipity`. `quality_score` yalnız intrinsic artwork quality olarak kalır. Mevcut high-quality unseen-artist discovery bonusu (`+2`) korunur; recent artist bonus almaz ve ayrıca bounded repeat penalty gördüğü için semantik açıdan iki ayrı ödül yığılmaz. Yeni single history satırları actual `published_orientation`, normalized known artist key, semantic family, tone ve coarse color değerlerini taşır.

## Kurulum ve yerel kullanım

Desteklenen production runtime **Python 3.10**'dur. Human-maintained dependency intent
`requirements.in` ve `requirements-dev.in` dosyalarında; tam transitive sürümler ve
PyPI artifact hash'leri ise üretilmiş `requirements.lock` ve
`requirements-dev.lock` dosyalarındadır. Lock dosyaları commit edilir ve elle
düzenlenmez.

Production-compatible ortam için:

```bash
python -m pip install --require-hashes -r requirements.lock
```

Geliştirme ve test ortamı için (production bağımlılıklarını da içerir):

```bash
python -m pip install --require-hashes -r requirements-dev.lock
```

Bağımlılıkları bilinçli olarak güncellemek için Python 3.10 ortamında önce ilgili
`.in` dosyasını değiştirin, ardından lock'ları yeniden üretip testleri çalıştırın:

```bash
./scripts/compile_requirements.sh
pytest -q
```

Lock compiler sürümü `requirements-dev.in` içinde pinlidir. CI aynı üretim komutunu
çalıştırıp lock dosyalarında diff oluşmadığını doğrular.

Önemli CLI seçenekleri:

```bash
# Yerel çıktı üretir, publish mutasyonlarını yapmaz
python main.py --dry-run

# Production modunu açıkça seçer (gerekli production config zorunludur)
python main.py --mode single
python main.py --mode carousel

# Network/acquisition başlatmadan yalnız required production config'i doğrular
python main.py --validate-production-config

# Yeni içerik üretmeden unresolved publication lifecycle durumunu uzlaştırır
python3 main.py --reconcile-publications

# Adaptive 3–8 featured eserlik carousel çalıştırır
python main.py --force-carousel

# Publish etmeden yerel carousel review bundle'ları üretir
python3 scripts/qc_carousels.py --count 8
python3 scripts/qc_carousels.py --theme women_reading
python3 scripts/qc_carousels.py --format MONOGRAPHIC --count 2
python3 scripts/qc_carousels.py --seed qc-2026-08-25 --count 10 --no-gemini

# Tek-eser paylaşımından sonra Pinterest cross-post ister
python main.py --pinterest

```

Eski `--image-url` seçeneği komut satırı uyumluluğu için kabul edilir, ancak güvenli indirilen ve doğrulanan eserin yerine dışarıdan farklı byte'lar publish edilmesini önlemek için artık yok sayılır. Tek-eser ve carousel akışları kendi doğrulanmış görsellerini R2'ye yükler.

### Dry-run sözleşmesi

`--dry-run` history okuyabilir; müze ve görsel GET istekleri yapabilir; güvenli görsel doğrulaması, Gemini/fallback caption üretimi ve yerel görüntü oluşturmayı çalıştırabilir.

Dry-run şunları yapmaz:

- R2 history mutate/reserve/confirm etmez veya publication reconciliation yazısı yapmaz.
- Production media’yı R2’ye yüklemez.
- R2 staging media objesi silmez veya cleanup queue çalıştırmaz.
- Instagram container oluşturmaz ya da publish etmez.
- Pinterest’e publish etmez.

Dry-run strict offline değildir: `GOOGLE_GEMINI_API_KEY` varsa Gemini’ye dış inference isteği yapabilir.

### Carousel QC review harness

`scripts/qc_carousels.py`, production carousel seçim, secure image validation, set optimization,
cover, sequencing, caption ve rendering yolunu çalıştırır; ancak reservation, R2 media upload,
Instagram ve Pinterest publish aşamalarına geçmez. History yalnız okunur. Her çağrı benzersiz bir
`data/qc_carousels/<timestamp>_<seed>/` dizini oluşturur; bu dizinde run manifest, browser index ve
her carousel için bir editorial cover ile seçilen 3–8 featured eserin rendered slide'ları, contact
sheet, caption, JSON manifest ve seçim raporu bulunur.
`--theme` tam bir registry theme ID seçer; `--format` bir formatla sınırlar; tema verilmezse `--count`
boyunca format çeşitliliği önceliklendirilir. `--seed`, deterministic planner/sample davranışını tekrar
üretmek için kullanılabilir. `--no-gemini`, Gemini çağrısını tamamen atlar ve doğrulanmış İngilizce tema
başlığıyla deterministic yerel editorial intro/caption fallback'ını kullanır; manifest içinde
`gemini_used: false` kaydedilir.

Carousel cover ve caption içindeki sayısal/factual iddialar final sıralanmış featured setten
deterministic olarak türetilir; cover eseri bu sayılara veya `Featured Works` listesine dahil edilmez.
Tek müzeli setler müze adıyla odaklı bir seçki olarak, çok müzeli setler ise yalnız metadata eksiksizse
çoğul koleksiyon diliyle sunulur. Artist/müze sayıları ve güvenilir tarih aralığı Gemini tarafından
değiştirilemez. Registry theme title editorial başlık olarak korunur; body copy bunu her eserin formal
sınıflandırmasıymış gibi güçlendirmez. QC manifest'i `editorial_facts`, cover copy ve caption intro'yu
saklayarak görünen iddiaların seçilen setle karşılaştırılmasını sağlar.

## Ortam değişkenleri

| Variable | Gerekli mi? | Amaç |
| --- | --- | --- |
| `INSTAGRAM_ACCOUNT_ID` | Production publish için gerekli | Instagram Business/Creator account ID |
| `INSTAGRAM_ACCESS_TOKEN` | Production publish için gerekli | Meta Graph API erişim token’ı |
| `INSTAGRAM_GRAPH_API_VERSION` | Opsiyonel | Ortak Graph API sürümünü override eder (varsayılan `v22.0`) |
| `ARTFOLIO_REELS_ROOT` | Opsiyonel | Varsayılan sibling konumunda değilse Artfolio Reels repo yolu |
| `CLOUDFLARE_R2_ACCOUNT_ID` | Production history ve R2 media için gerekli | R2 account ID |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | Production history ve R2 media için gerekli | R2 access key |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | Production history ve R2 media için gerekli | R2 secret key |
| `CLOUDFLARE_R2_BUCKET_NAME` | Production history ve R2 media için gerekli | History ve media bucket’ı |
| `CLOUDFLARE_R2_PUBLIC_URL` | R2 media upload kullanılıyorsa gerekli | Instagram’ın erişeceği R2 public URL tabanı |
| `GOOGLE_GEMINI_API_KEY` | Opsiyonel | Gemini caption/alt-text üretimi; yoksa fallback kullanılır |
| `RIJKSMUSEUM_API_KEY` | Opsiyonel | Rijksmuseum adapter’ını etkinleştirir; yoksa bu kaynak atlanır |
| `SMITHSONIAN_API_KEY` | Opsiyonel | Smithsonian Open Access adapter'ını etkinleştirir |
| `EUROPEANA_API_KEY` | Opsiyonel | Europeana adapter'ını etkinleştirir |
| `PINTEREST_APP_ID` | Opsiyonel | Pinterest OAuth app ID; dört Pinterest değeri birlikte gerekir |
| `PINTEREST_APP_SECRET` | Opsiyonel | Pinterest OAuth app secret |
| `PINTEREST_REFRESH_TOKEN` | Opsiyonel | Pinterest refresh token |
| `PINTEREST_BOARD_ID` | Opsiyonel | Hedef Pinterest board ID |
| `PUBLIC_IMAGE_URL` | Deprecated | Dış media override'ı güvenlik ve eser sadakati için yok sayılır |
| `ARTFOLIO_SELECTION_SEED` | Opsiyonel | Seçim RNG’si için açık seed |
| `GITHUB_RUN_ID` | GitHub tarafından otomatik | Açık seed yoksa GitHub koşusunun seçim seed’i |

Gerçek R2 conditional-write doğrulaması normal testlerden ayrıdır ve credentials
varlığıyla kendiliğinden çalışmaz. Mimari, güvenlik sınırları ve açık opt-in
komutu için [Cloudflare R2 concurrency verification](docs/r2-integration-verification.md)
belgesine bakın.

Instagram publish ile R2-backed history zorunludur. `CLOUDFLARE_R2_PUBLIC_URL`, carousel ve tek-eser media upload akışlarında gerekir. Pinterest flag’i kullanılmadığında Pinterest credentials gerekli değildir.

Production startup’ta Instagram ve beş R2 değişkeni (`ACCOUNT_ID`, access key, secret key, bucket ve public URL) **required** kabul edilir ve eksik adlar secret değerleri yazdırılmadan tek tanıda raporlanır. Gemini template fallback sunduğu, Rijksmuseum adapter’ı anahtar yokken devre dışı kaldığı ve Pinterest scheduled workflow’ta çağrılmadığı için bu entegrasyonlar **optional** kalır; eksik/partial optional config production startup’ını durdurmaz.

Normal outbound isteklerin tümü bounded timeout kullanır. R2 connect/read sınırları 10/30 saniyedir; SDK-level retry kapalıdır. Media upload yalnız transient network, rate-limit ve 5xx hatalarında en fazla üç loglanan uygulama denemesi yapar; permanent 4xx/config hataları hemen durur. Gemini isteği 60 saniye ve tek attempt ile sınırlıdır; hata halinde deterministic template fallback kullanılır. Instagram yalnız transient container/status hatalarını üç bounded attempt ile tekrarlar; publish sınırındaki belirsiz sonuç otomatik retry edilmez.

## Selection reproducibility

Seçim seed önceliği şöyledir:

```text
ARTFOLIO_SELECTION_SEED
→ GITHUB_RUN_ID
→ local invocation entropy
```

Örneğin `ARTFOLIO_SELECTION_SEED=test-123`, aynı theme history ve ay girdisiyle carousel tema seçimini; aynı history, config ve API response/dataset altında müze candidate-pool rastgeleliğini ve tek-eser serendipity seçimini tekrar üretmeye yardımcı olur. Dış API verileri, content type, grid tone ve image border gibi seed dışındaki girdiler tüm bot koşusunu yine etkileyebilir.

## Geliştirici doğrulaması

```bash
pytest -q
python3 -m compileall -q main.py src scripts tests
ruff check main.py src scripts tests
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

Reel lane: museum acquisition → selector → portfolio → atomic handoff
Insights lane: owned media GET → association → append-only R2 snapshots
```

## Dosya yapısı

```text
.
├── .github/workflows/instagram_bot.yml  # Schedule, validation gate, production run
├── src/                                 # Selection, history, processing and API adapters
├── data/carousel_themes.json            # Validated editorial theme registry
├── tests/                               # Networkless unit/regression suite
├── config.py                            # Runtime constants
├── main.py                              # CLI orchestration
├── requirements.in                      # Human-maintained runtime dependency intent
├── requirements-dev.in                  # Human-maintained test/tooling intent
├── requirements.lock                    # Generated exact, hashed runtime graph
├── requirements-dev.lock                # Generated exact, hashed development graph
├── pytest.ini                           # Pytest import-path configuration
└── README.md
```
