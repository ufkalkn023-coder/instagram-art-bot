# Instagram Sanat Müzesi Paylaşım Otomasyonu

Bu proje, müze API'lerinden gelen eserleri hak bilgisini koruyarak seçer, güvenli biçimde indirir ve resmi Meta Instagram Graph API üzerinden yalnız editorial carousel olarak paylaşır. GitHub Actions zamanlaması, aynı checkout üzerinde önce compile ve test doğrulamasını tamamlar; yalnız ardından production bot çalışır.

## Ne yapar?

- Art Institute of Chicago, Metropolitan Museum of Art, Cleveland Museum of Art,
  Rijksmuseum, Smithsonian Institution ve Europeana kaynaklarından eser adayları toplar.
- Production publish yalnız açıkça ayarlanmış `ARTFOLIO_RIGHTS_POLICY=strict_public_domain` ile başlar; yalnız doğrulanmış public-domain/open-access eserleri kabul eder. Production dışı araçlarda geriye uyumluluk için env verilmezse `permissive` davranış korunur. Her iki mod da `is_public_domain`, `rights_status`, `rights_text`, `copyright_notice`, `credit_line`, source ve eser URL'sini telemetry/attribution olarak korur.
- Duplicate, hak-policy, kalite ve tema uyumluluğu filtrelerinden geçen görselleri güvenli biçimde indirir; HTTPS-only erişim, private-network/SSRF koruması, redirect yeniden doğrulaması, sınırlı indirme ve Pillow doğrulaması uygular.
- Chicago kaynağında yalnız açıkça public-domain eserler 1686px IIIF türevini kullanır; diğerleri API'nin meşru 843px analysis türevinde kalır. AIC IIIF istekleri süreç içinde tekilleştirilip yaklaşık saniyede bir isteğe sınırlandırılır ve `AIC-User-Agent` ile tanımlanır.
- Carousel featured eserlerini ortak 1080×1350 (4:5) editorial canvas üzerinde, tam eseri contain ederek ve tek kez compositing yaparak gösterir. Boş alan carousel boyunca ortak deterministic nötr gallery field'dır; crop, stretch, blurred clone veya metadata overlay yoktur.
- Gemini kullanılabiliyorsa caption, alt metin ve görsel metin önerisi üretir; anahtar yoksa veya istek başarısız olursa yerel fallback caption kullanır.
- History bilgisini Cloudflare R2 üzerinde tutar; Instagram publish öncesinde durable kilit kullanarak olası duplicate paylaşımları engeller.
- Kaynak hatalarını `source_health` kategorileriyle izole eder; bir adapter'ın erişim,
  rate-limit veya upstream hatası diğer beş kaynağın seçimini durdurmaz.

## Çalışma modu

Instagram feed için tek canonical ürün carousel'dir. Her production koşusu bir editorial cover ve adaptive **5–8 featured eser** üretir; toplam slide sayısı 6–9'dur. En az beş güvenli, kaliteli ve temaya uyumlu featured eser ile bunlardan farklı bir cover bulunamazsa publication başlamaz. Sekize tamamlamak için zayıf aday eklenmez.

Carousel teması doğrulanmış registry ve deterministic Theme Planner tarafından seçilir. Reel/export ve legacy history okuma altyapısı feed publisher'dan ayrıdır; feed CLI'sinde single modu, `--force-carousel`, dış image URL veya Pinterest publish seçeneği yoktur.

## Artfolio Theme Registry

Carousel tema evreni `data/carousel_themes.json` içindeki data-driven registry'de tutulur. Her kayıt stable snake_case ID, İngilizce editorial title, kontrollü theme family ve carousel format, açık primary/secondary sorgular, required/preferred/excluded relevance sinyalleri, opsiyonel seasonal aylar ve sınırlı editorial priority taşır. Required sinyaller varsayılan olarak OR grubudur; yalnız gerçekten iki kavramı birlikte gerektiren temalar küçük, backward-compatible `required_term_groups` alanıyla AND-of-OR mantığı kullanabilir. Python katmanı şemayı, enum değerlerini, sorgu dizilerini, ay aralığını ve duplicate ID'leri doğrular; bozuk registry sessizce fallback yapmadan çalışmayı durdurur. Yeni tema eklemek carousel orchestration kodunu değiştirmeyi gerektirmez.

Carousel acquisition registry sorgularını güç sırasıyla ve sınırlı çalıştırır: en fazla üç primary, gerektiğinde iki secondary sorgu; sorgu başına adapter başına 16 aday ve tema başına en fazla 20 adapter çağrısı. Her logical sorgudan sonra pool yeniden değerlendirilir ve yeterli headroom oluştuğunda kalan sorgular çağrılmaz. Aynı canonical eser farklı sorgulardan gelirse tek aday kalır, fakat primary/secondary türü, sıra ve matched-query provenance'ı idempotent biçimde birleştirilir. Featured eserler ile editorial cover aynı typed acquisition pool'unu kullanır; cover nihai 5–8 featured ID'yi dışlar ve kendi secure-download ile cover-score kontrollerinden ayrıca geçer.

Metadata matching Unicode NFKC/casefold, punctuation ve whitespace normalizasyonu ile exact token/phrase boundary kullanır. Artist adı, müze adı ve credit/copyright metni tematik kanıt sayılmaz. Exact required-term eşleşmesi güçlü kanıttır ancak tek qualification yolu değildir: güçlü primary-query provenance ile title/description/classification/medium alanlarında en az iki destekleyici mevcut sinyal birlikte yeterli olabilir. Museum search sonucu tek başına yeterli değildir. Preferred sinyaller sınırlı bonus verir; excluded sinyaller güvenilir descriptive alanda bulunduğunda aday reddedilir. `theme_relevance_score` ayrı bir 0–100 ölçüdür ve varsayılan geçiş eşiği 50'dir. Carousel sıralamasında relevance baskın, teknik `quality_score` önemli ama ikincildir; yüksek kalite düşük relevance'ı kurtaramaz. `quality_score` yalnız eserin teknik/metadata kullanılabilirliğini ölçmeye devam eder.

Tema kanıtı `METADATA` veya `HYBRID` olarak tanımlanır. Subject/iconography gibi `METADATA` temaları mevcut metadata ve query provenance politikasını korur. Renk ve ışık odaklı `HYBRID` temalar ise düşük eşikli metadata/query prequalification sonrasında, yalnız secure validation ile zaten indirilmiş dosyadan çıkarılan bounded luminance, contrast ve coarse color sinyallerini final relevance'a ekler. Pixel kanıtı yalnız gerçekten ölçebildiği görsel özelliği destekler; örneğin parlaklık “winter”, renk ise “candle” veya “woman reading” semantiğinin yerine geçmez. Final geçiş eşiği her iki modda da aynıdır.

Secure validation'dan geçen en güçlü en fazla 24 finalist için candidate score'dan ayrı, açıklanabilir bir carousel set score hesaplanır. Bounded optimizer 5–8 arasındaki her uygulanabilir cardinality için en iyi seti değerlendirir; artist/müze/region dominance sınırlarını korurken period, medium, orientation, luminance ve kaba renk çeşitliliğini bounded guardrail olarak kullanır. Candidate quality/editorial score, güvene göre kademeli learned engagement score ile harmanlanır; set düzeyinde tema/format/count/hook/slot ve artwork-set feature tahmini ayrıca değerlendirilir. Sonuç inclusion eşiğinin altına düştüğünde büyüme durur. Böylece zayıf veya redundant tail sırf sekize tamamlamak için eklenmez.

Set seçimi tamamlandıktan sonra seçilen 5–8 featured eser ayrı bir deterministic sequencing aşamasından geçer. Caption numaraları, `carousel_01.jpg`–`carousel_NN.jpg` artifact sırası ve history `featured_position=1..N` değerleri bu nihai sırayı izler; padding, placeholder veya duplicate slide üretilmez. `CHRONOLOGICAL` format bilinen tarihleri artan sıraya dizer ve bilinmeyen tarihleri stable ID sırasıyla sona koyar; diğer formatlarda opener/theme establishment, adjacent visual rhythm, güçlü eser dağılımı ve closure birlikte puanlanır.

Availability preflight image indirmeden canonical/history duplicate, merkezi rights policy, format target, pre-download metadata/image prerequisites ve relevance kapılarını bu sırayla sayar. `estimated_safe_pool`, son relevance kapısından geçen fakat henüz secure image validation'dan geçmemiş eser sayısıdır. Absolute construction minimum altı unique eserdir: en az beş featured ve bunlardan farklı bir cover. Altı ile on bir aday arası publish edilebilir ama dar pool, 12 aday rahat operational headroom sayılır. `minimum_candidate_target` yalnız preferred acquisition headroom/early-stop hedefidir. Pipeline minimum altı adayla construction aşamasına geçebilir; bounded acquisition ve en fazla 40 secure validation denemesi mümkün olduğunda 24 finalistlik optimizer headroom'unu toplamaya devam eder. Optimizer her boyutta en az bir cover-eligible ID'yi featured set dışında bırakır.

Attempt sıralaması editorial score'un anlamını değiştirmeden ayrı bir feasibility katmanı uygular. Mevcut adapter/credential/circuit-breaker kapasitesi ile `theme_feasibility.json` içindeki tema başına son 12 gerçek acquisition sonucu, 30 günlük half-life ve üç effective örnekte doygunlaşan confidence ile değerlendirilir. Tek başarısızlık sınırlı bir ceza verir; tekrar eden güncel başarısızlıklar en fazla `-6`, güçlü güncel headroom en fazla `+3`, hiç aktif compatible source bulunmaması `-8` etkiler ve toplam feasibility adjustment `[-8, +3]` aralığında kalır. Bu bounded skor gözlemlenebilir kalır; ancak o anda hiç aktif compatible source'u olmayan tema yalnız mevcut run için attempt dışıdır. Yeni temalar nötrdür; eski sonuçlar nötre decay eder ve hiçbir tema kalıcı olarak elenmez. Production state ayrı, ETag-conditional R2 objesinde tutulur; local ortam atomik dosya fallback'i kullanır. Okuma/yazma veya corruption hatası cold start'a düşer ve publishing'i engellemez. Objede credential, query metni, response body veya exception dump saklanmaz.

İlk planner teması yeterli değilse feasibility ile açıklanabilir deterministic sıradaki sonraki tema denenir; en fazla beş METADATA teması değerlendirilir. Beşinin tamamı başarısız olursa aynı acquisition, rights, quality, secure-image, optimizer ve cover yolunu kullanan tek `Artfolio Selection` fallback'i theme relevance kapısı olmadan denenir. Bu nötr fallback ortak subject, period, medium, region veya çoklu museum iddiası kurmaz. Yalnız tamamlanmış 1 cover + 5–8 featured planının kimliği publication history'ye yazılır; feasibility telemetry `posted_history.json` ile birleşmez.

Theme Planner aynı selection-run seed'i kullanarak global random state'i değiştirmeden deterministic serendipity üretir. Son carousel publication slot'larında aynı theme ID, family ve format tekrarları yalnız küçük, bounded anti-spam guardrail'larıdır; güçlü learned engagement kanıtını ana amaç olarak bastırmaz. Artwork sayısı fatigue'i çoğaltmaz: aynı `publication_id` altındaki bir cover ve 5–8 featured kayıt history'de tek tema slotudur. Eski kayıtlar okunmaya devam eder.

### Format-specific carousel contracts

Format policy, temanın istediği bilinçli benzerliği istenmeyen tekrardan ayırır:

- `THEMATIC_COLLECTION` ortak tema relevance'ını korurken artist, museum, region ve görsel çeşitliliği mevcut genel kurallarla dengeler.
- `COMPARATIVE` ortak ekseni hard relevance gate olarak tutar; period, region, artist, medium ve gerçek görsel özelliklerde kontrastı, varsa registry'deki tercih edilen comparison dimension'ı daha güçlü ödüllendirir.
- `CHRONOLOGICAL` bilinen tarihleri tercih eder; güvenli biçimde parse edilen exact, approximate, range ve century tarihleriyle zaman aralığını ve dağılımı set seçimi sırasında ödüllendirir, sonra bilinenleri artan ve bilinmeyenleri stable sırada dizer.
- `MONOGRAPHIC` 5–8 featured eser ile ayrı cover'ın typed artist target/alias kimliğine exact canonical eşleşmesini zorunlu kılar. Aynı artist redundancy değildir; museum, tarih, medium ve görsel farklılıklar soft hedeflerdir.
- `MUSEUM_SPOTLIGHT` 5–8 featured eser ile ayrı cover'ın typed museum target/alias kimliğine exact canonical eşleşmesini zorunlu kılar ve mümkünse acquisition'ı registry'deki adapter source'larına daraltır. Aynı museum redundancy değildir; artist, period, region, medium ve görsel çeşitlilik aranır.

`REGIONAL`, `PERIOD_FOCUS`, `MEDIUM_FOCUS`, `COLOR_STUDY`, `LIGHT_STUDY`, `ICONOGRAPHIC` ve `VISUAL_PATTERN` formatlarında temanın tanımlayıcı ortak boyutu kendi başına redundancy cezası üretmez. `REGIONAL`, `PERIOD_FOCUS` ve `MEDIUM_FOCUS` target eşleşmeleri soft relevance tercihidir; target uyuşmazlığı tek başına production hard reject değildir. `MONOGRAPHIC` artist ve `MUSEUM_SPOTLIGHT` museum kimlikleri hard kalır. Artist ve museum adları yalnız açık metadata ile canonical eşleştirilir; description veya credit içinde geçen bir ad target kimliği sayılmaz.

## Zamanlama ve GitHub Actions

Workflow her gün dört UTC slotunda carousel çalıştırır:

```text
carousel: 0 5,10,15,20 * * *
```

GitHub Actions cron ifadeleri UTC’dir. Slotlar `slot_1=05:00`, `slot_2=10:00`, `slot_3=15:00`, `slot_4=20:00` olarak experiment metadata'sına yazılır.

Scheduled invocation açıkça `python main.py --mode carousel` çalıştırır. Scheduled production varsayılan olarak kapalıdır; yalnız repository Actions variable `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED` tam olarak `true` olduğunda publish job’ı çalışır. Değişkenin eksik olması veya farklı bir değer taşıması fail-closed davranır.

Workflow manuel olarak da **Actions → Instagram Art Bot Scheduler → Run workflow** üzerinden başlatılabilir. Manuel yol da yalnız carousel yayınlar ve gerçek Instagram paylaşımını onaylamak için `confirm_publish` alanına tam olarak `PUBLISH_TO_INSTAGRAM` yazılmasını gerektirir. Manuel çalıştırma scheduled-production variable’ından bağımsızdır. `instagram-bot` concurrency grubu aynı anda tek publish job’ına izin verir; `cancel-in-progress: false` aktif publish’i yarıda kesmez.

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

Yerel Mac credential’ları launchd plist’ine veya repo dosyalarına yazılmaz. Yerel operasyonlar iki ayrı Keychain profili kullanır:

- **Collector:** `com.artfolio.instagram-insights.*`; Instagram account/token ile dört R2 alanını içerir ve kullandığı R2 credential’ı Object Read & Write yetkili olmalıdır.
- **Engagement audit:** `com.artfolio.engagement-audit.*`; yalnız dört R2 alanını içerir, Instagram credential’ı içermez ve kullandığı R2 credential’ı Object Read Only olmalıdır.

Collector için bir kerelik Keychain kurulumu, güvenli durum kontrolü ve LaunchAgent kurulumu:

```console
cd /Users/ufuk/Desktop/instagram-art-bot-push
python3 scripts/install_insights_launchd.py configure-keychain
python3 scripts/collect_insights.py --check-secrets
python3 scripts/collect_insights.py --health-check
python3 scripts/install_insights_launchd.py install
```

`configure-keychain`, yalnız collector profilini yapılandırır ve her eksik değer için macOS Keychain’in gizli giriş prompt’unu açar; değer komut satırı argümanına, shell history’ye veya log’a girmez. Mevcut process environment değerleri interaktif çalıştırmada önceliklidir, fakat LaunchAgent yalnız `com.artfolio.instagram-insights.*` kayıtlarını yükler. Installer idempotent olarak `~/Library/LaunchAgents/com.artfolio.instagram-insights.plist` dosyasını günceller ve user LaunchAgent’ı yeniden yükler. Her login/reboot sonrasında ve en fazla saatte bir tek-seferlik collector çalışır. Dönen operasyon log’ları `~/Library/Logs/Artfolio/instagram-insights.log` altında 1 MiB + üç backup ile sınırlıdır.

`--health-check` yalnız GET/read işlemleriyle collector profil bütünlüğünü, Instagram media read erişimini, R2 history read erişimini, bucket yapılandırmasını ve son yerel collector başarısının tazeliğini kontrol eder. Saatlik schedule için `HEALTHY <= 2h`, `STALE > 2h` ve `CRITICAL > 6h` eşikleri kullanılır. Read-only preflight, R2 `PutObject` yetkisini kanıtlamaz; yalnız gerekli write yapılandırmasının mevcut olduğunu raporlar. Aktif collector R2 key pair'i audit profilindeki key pair ile aynıysa rol ayrımı ihlali olarak `INVALID_ROLE_COLLISION` raporlanır ve normal collector koşusu R2/Instagram mutation sınırından önce fail-closed durur.

Tüm production sağlık yüzeylerini tek read-only komutta incelemek için:

```console
python3 scripts/artfolio_doctor.py
python3 scripts/artfolio_doctor.py --json
python3 scripts/artfolio_doctor.py --quick
```

Doctor; repository/config/rights durumunu, publication lifecycle özetini, iki credential profilinin ayrımını, collector tazeliğini, LaunchAgent sözleşmesini, engagement-learning funnel ve kalibrasyonunu, R2 LIST/GET ile Instagram GET erişimini raporlar. Normal mod ayrıca her müze adapter'ında en fazla bir adaylık hafif metadata probe'u çalıştırır; `--quick` bu dış müze probe'larını tamamen atlar. JSON modu stdout'a yalnız deterministic JSON yazar. Exit code `0=HEALTHY`, `1=DEGRADED`, `2=CRITICAL` anlamına gelir.

Doctor hiçbir Instagram container/publish endpoint'ini, publication reconciliation'ı, R2 `PutObject`/`DeleteObject` yolunu, Keychain yazısını veya LaunchAgent değişikliğini çağırmaz. Credential değerleri çıktıya dahil edilmez. Collector write yetkisi sentetik write yapılmadan kanıtlanamayacağı için `CONFIGURED_PERMISSION_NOT_PROVEN` olarak bilgi amaçlı gösterilir ve tek başına hata sayılmaz.

Bir collector credential’ını daha sonra değiştirmek için `python3 scripts/install_insights_launchd.py configure-keychain --force` kullanılır. Log’da `Operation not permitted` görülürse LaunchAgent’ın kullandığı Python interpreter’a macOS Privacy & Security ayarlarından Desktop erişimi verilmelidir.

> **UYARI:** Read-only audit credential’larını collector namespace’i olan `com.artfolio.instagram-insights.*` altına KURMAYIN. Collector R2’ye conditional `PutObject` yazar ve Object Read & Write credential gerektirir.

Devre dışı bırakma ve kaldırma:

```console
python3 scripts/install_insights_launchd.py uninstall
```

Mac uykudayken veya offline iken daemon/polling yapılmaz. Sonraki saatlik koşu mevcut missed-slot politikasını uygular: o anda hâlâ açık olan en yeni 1h/6h/24h/72h/7d slotunu alır, yeni fark edilen kapanmış pencereleri append-only terminal kayıtlarla işaretler ve `missed_slots` içinde yalnız o koşuda yeni fark edilenleri raporlar.

Ambiguous veya unmatched sonuçlar yazılmaz; özet yalnız örneğin `[insights] ambiguous=1` gösterir. Sadece böyle istisnai bir Reel incelendikten sonra emergency fallback kullanılabilir:

```console
python3 scripts/collect_insights.py --link LOCAL_REEL_ID INSTAGRAM_MEDIA_ID
```

Manuel eşleme otomatik eşlemeye üstün gelir; başka bir manuel eşlemeyi sessizce değiştirmez. Snapshot’lar Reel’in UTC yayın ayına göre `insights/YYYY-MM.json` içinde ETag koşullu ve append-only tutulur.

Hedef slotlar 1, 6, 24, 72 ve 168 saattir. Her slotun penceresi bir sonraki hedefe kadar açıktır; 168 saat slotu 30 güne kadar alınabilir. Gecikmiş koşu en yeni açık slotu alır. Boş Meta verisi slotu tüketmez. `views` gibi nonempty fakat learning için pozitif `reach` ve en az bir engagement metriği taşımayan yanıtlar `partial` olarak append-only saklanır; aynı pencere içinde sonraki `learning_complete` denemesi eski ham kaydı overwrite etmeden eklenir. Tüm metriklerin kalıcı Meta code 100 ile reddedilmesi açık terminal kategori olarak saklanır ve tekrar sorgulanmaz. Ham Meta metrikleri önce saklanır: `views`, `reach`, `likes`, `comments`, `saved`, `shares`, `total_interactions`, `ig_reels_video_view_total_time`, `ig_reels_avg_watch_time`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count`. Desteklenmeyen metrikler ayrı izole edilerek eksik bırakılır; sıfır uydurulmaz. `save_rate`, `share_rate`, `like_rate` ve `comment_rate` yalnız pozitif `reach` varsa `metric / reach` olarak hesaplanır.

Mevcut günlük Insights workflow'u 03:00 UTC cron’unda çalışır. Analytics hataları publishing state'ini veya history lifecycle'ını etkileyemez. Facebook Login modu için token’da `instagram_basic`, `instagram_manage_insights` ve `pages_read_engagement` izinleri bulunmalıdır.

Feed koşusu R2 history ile bütün `insights/YYYY-MM.json` partition'larını best-effort okur. Aynı publication için sırasıyla 72h, 168h ve 24h snapshot'ı seçilir; 1h/6h yalnız telemetry'dir. Share/save/comment/like oranları ve reach signal, account baseline'ına göre winsorize edilmiş bounded log normalization'dan geçer. Düşük reach, eksik metrik, 24h provisional maturity ve 180 günlük recency half-life effective weight'i azaltır. Artist, artist group, region, style/period, semantic family, museum/source, visual özellik, theme/format/count, cover/hook, slot/weekday ve preceding-post distance feature'ları global ortalamaya sample-size-aware shrink edilir. Model confidence sıfırdan kademeli büyür; cold start mevcut quality/editorial sıralamasını korur. Yaklaşık %10 seeded exploration yalnız normal teknik/kalite gate'lerinden geçmiş novelty adaylarına ayrılır.

Learning funnel'ını Instagram veya R2 verisini değiştirmeden incelemek için:

```console
python3 scripts/install_insights_launchd.py configure-audit-keychain
python3 scripts/install_insights_launchd.py audit-status
python3 scripts/audit_engagement_learning.py
python3 scripts/audit_engagement_learning.py --verbose
```

`configure-audit-keychain`, yalnız `com.artfolio.engagement-audit.*` altında `CLOUDFLARE_R2_ACCOUNT_ID`, `CLOUDFLARE_R2_ACCESS_KEY_ID`, `CLOUDFLARE_R2_SECRET_ACCESS_KEY` ve `CLOUDFLARE_R2_BUCKET_NAME` alanlarını güvenli Keychain prompt’larıyla kurar. `audit-status` değerleri göstermeden yalnız `AVAILABLE`/`MISSING` durumunu raporlar. Audit bu profilden collector namespace’ine fallback yapmaz ve Instagram credential’ı yüklemez. Her iki yerel komutta da mevcut process environment değerleri seçilen Keychain profilinden önce gelir; environment değişkenleri credential rolü metadata’sı taşımadığından doğru rolü sağlamak çağıranın sorumluluğundadır.

Komut publication/media identity eşleşmelerini, 24h/72h/168h coverage'ını, exclusion nedenlerini, seçilen slotları, reach dağılımını, maturity durumunu, effective observation toplamını ve global confidence'ı raporlar. `--verbose` publication kimliklerini hash'leyerek her observation weight faktörünü gösterir. Production R2’den okurken audit yalnız `GetObject` ve `ListObjectsV2` yollarını kullanır; `PutObject`, `DeleteObject` veya başka bir write yolu çağırmaz. `--history` ve `--snapshots` birlikte yerel dosya gösterdiğinde Keychain’e erişmez.

Her kesinleşen carousel `selection_model_version`, `engagement_model_version`, `carousel_theme`, `carousel_format`, `featured_count`, `cover_variant`, `caption_hook_type`, `publish_slot`, `exploration_selected`, `learned_score`, `engagement_confidence`, `quality_component`, `engagement_component`, `diversity_component`, `exploration_component` ve varsa `preceding_post_distance_minutes` alanlarını taşır. Eski publication kayıtlarında bu alanların bulunmaması geçerlidir.

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

Yeni carousel kayıtları mevcut artwork history şemasını koruyarak `theme_id`, `theme_family`, `carousel_format`, rights/source telemetry ve compact experiment metadata taşır. Aynı `publication_id` altındaki 6–9 artwork satırı planner için tek slot olarak okunur. Eski `theme` alanları ve metadata'sız legacy publication kayıtları yüklenmeye devam eder.

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

# Canonical production carousel'i çalıştırır (production config zorunludur)
python main.py --mode carousel

# Network/acquisition başlatmadan yalnız required production config'i doğrular
python main.py --validate-production-config

# Yeni içerik üretmeden unresolved publication lifecycle durumunu uzlaştırır
python3 main.py --reconcile-publications

# Publish etmeden yerel carousel review bundle'ları üretir
python3 scripts/qc_carousels.py --count 8
python3 scripts/qc_carousels.py --theme women_reading
python3 scripts/qc_carousels.py --format MONOGRAPHIC --count 2
python3 scripts/qc_carousels.py --seed qc-2026-08-25 --count 10 --no-gemini

```

### Dry-run sözleşmesi

`--dry-run` history okuyabilir; müze ve görsel GET istekleri yapabilir; güvenli görsel doğrulaması, Gemini/fallback caption üretimi ve yerel görüntü oluşturmayı çalıştırabilir.

Dry-run şunları yapmaz:

- R2 history mutate/reserve/confirm etmez veya publication reconciliation yazısı yapmaz.
- Production media’yı R2’ye yüklemez.
- R2 staging media objesi silmez veya cleanup queue çalıştırmaz.
- Instagram container oluşturmaz ya da publish etmez.

Dry-run strict offline değildir: `GOOGLE_GEMINI_API_KEY` varsa Gemini’ye dış inference isteği yapabilir.

### Carousel QC review harness

`scripts/qc_carousels.py`, production carousel seçim, secure image validation, set optimization,
cover, sequencing, caption ve rendering yolunu çalıştırır; ancak reservation, R2 media upload,
Instagram ve Pinterest publish aşamalarına geçmez. History yalnız okunur. Her çağrı benzersiz bir
`data/qc_carousels/<timestamp>_<seed>/` dizini oluşturur; bu dizinde run manifest, browser index ve
her carousel için bir editorial cover ile seçilen 5–8 featured eserin rendered slide'ları, contact
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
| `ARTFOLIO_RIGHTS_POLICY` | Production publish için gerekli | Production'da yalnız `strict_public_domain`; production dışı çağrılarda env yoksa geriye uyumlu `permissive` default |
| `ARTFOLIO_REELS_ROOT` | Opsiyonel | Varsayılan sibling konumunda değilse Artfolio Reels repo yolu |
| `CLOUDFLARE_R2_ACCOUNT_ID` | Production history ve R2 media için gerekli | R2 account ID |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | Production history ve R2 media için gerekli | R2 access key |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | Production history ve R2 media için gerekli | R2 secret key |
| `CLOUDFLARE_R2_BUCKET_NAME` | Production history ve R2 media için gerekli | History ve media bucket’ı |
| `CLOUDFLARE_R2_PUBLIC_URL` | R2 media upload kullanılıyorsa gerekli | Instagram’ın erişeceği R2 public URL tabanı |
| `GOOGLE_GEMINI_API_KEY` | Opsiyonel | Gemini caption/alt-text üretimi; yoksa fallback kullanılır |
| `SMITHSONIAN_API_KEY` | Opsiyonel | Smithsonian Open Access adapter'ını etkinleştirir |
| `EUROPEANA_API_KEY` | Opsiyonel | Europeana adapter'ını etkinleştirir |
| `ARTFOLIO_SELECTION_SEED` | Opsiyonel | Seçim RNG’si için açık seed |
| `GITHUB_RUN_ID` | GitHub tarafından otomatik | Açık seed yoksa GitHub koşusunun seçim seed’i |

Gerçek R2 conditional-write doğrulaması normal testlerden ayrıdır ve credentials
varlığıyla kendiliğinden çalışmaz. Mimari, güvenlik sınırları ve açık opt-in
komutu için [Cloudflare R2 concurrency verification](docs/r2-integration-verification.md)
belgesine bakın.

Instagram publish ile R2-backed history zorunludur. `CLOUDFLARE_R2_PUBLIC_URL`, carousel media upload akışında gerekir.

Production startup’ta Instagram ve beş R2 değişkeni (`ACCOUNT_ID`, access key, secret key, bucket ve public URL) **required** kabul edilir ve eksik adlar secret değerleri yazdırılmadan tek tanıda raporlanır. Gemini template fallback sunduğu için optional kalır; Rijksmuseum Data Services adapter’ı anahtarsız çalışır.

Normal outbound isteklerin tümü bounded timeout kullanır. R2 connect/read sınırları 10/30 saniyedir; SDK-level retry kapalıdır. Media upload yalnız transient network, rate-limit ve 5xx hatalarında en fazla üç loglanan uygulama denemesi yapar; permanent 4xx/config hataları hemen durur. Gemini isteği 60 saniye ve tek attempt ile sınırlıdır; hata halinde deterministic template fallback kullanılır. Instagram yalnız transient container/status hatalarını üç bounded attempt ile tekrarlar; publish sınırındaki belirsiz sonuç otomatik retry edilmez.

## Selection reproducibility

Seçim seed önceliği şöyledir:

```text
ARTFOLIO_SELECTION_SEED
→ GITHUB_RUN_ID
→ local invocation entropy
```

Örneğin `ARTFOLIO_SELECTION_SEED=test-123`, aynı theme history, learned model ve ay girdisiyle carousel tema seçimini; aynı history, config ve API response/dataset altında müze candidate-pool ve %10 exploration kararını tekrar üretmeye yardımcı olur.

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
→ museum candidate acquisition
→ centralized rights policy / duplicate / technical / quality gates
→ theme compatibility
→ mature Insights-derived feature scores
→ confidence-blended exploitation + seeded exploration
→ bounded repetition guardrails + set optimizer
→ 5–8 featured works + editorial cover
→ secure image validation
→ Gemini or fallback caption
→ image processing
→ R2 reservation / media upload
→ Instagram publishing
→ history confirmation
→ Insights snapshots → next-run learning rebuild

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
