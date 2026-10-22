# Teknik referans

[README’ye dön](../README.md)

Kurulum, araçlar ve operasyon kurallarının ayrıntıları bu belgede toplanmıştır.
Varsayılan otomatik akış ile isteğe bağlı editoryal ve legacy araçlar ilgili
başlıklarda ayrılır. Yayın izinlerinin güncel prosedürü [takvim rehberindedir](STAGE-B-SCHEDULE.md).

- [Otomatik içerik](#automatic-content)
- [Editoryal araçlar](#editorial-tools)
- [Insights ve analiz](#insights)
- [Hazır içerik kuyruğu](#feed-queue)
- [Yayın geçmişi ve tekrar koruması](#publication)
- [Kurulum](#setup)
- [Ortam değişkenleri](#configuration)
- [Yerel panel ve takvim](#dashboard)

Bu proje, müze API'lerinden gelen eserleri hak bilgisini koruyarak seçer, güvenli biçimde indirir ve resmi Meta Instagram Graph API üzerinden sırayla editorial carousel ve tekli görsel gönderisi olarak paylaşır. GitHub Actions zamanlaması, aynı checkout üzerinde önce compile ve test doğrulamasını tamamlar; yalnız ardından production bot çalışır.

<a id="automatic-content"></a>

## Düzenli paylaşım: otomatik editoryal içerik

Normal Feed akışının varsayılanı `automatic` modudur. Yayın zamanı geldiğinde
uygun hazır editorial paket varsa kullanılır; yoksa mevcut müze seçimi, hak ve
güvenli indirme akışı yeni eserleri edinir ve Museum Journal tasarımında işler.
İnsan tarafından hazırlanmış stok veya gönderi başına onay gerekmez. Tekli ve
carousel sırası, seçilen altı saatlik yayın dilimleri, exact-SHA sürüm izni, tekrar
koruması ve belirsiz yayın kilitleri korunur.

Otomatik tekli bir tam eser sayfasıdır; carousel seçilen 5–8 eserin tam
sayfalarından oluşur. Eski ayrı kapak eklenmez. Varsayılan model
`gemini-3.8-flash` olur. Her yeni otomatik paket için kaynak görseller ve müze
metadata'sıyla tek, en fazla 60 saniyelik Gemini çağrısı yapılır (thinking Medium,
4096 output token). Önceki acquisition caption çağrısı bu modda atlanır.
İngilizce editoryal başlık, kısa anlatı ve her eser için görsel gözlem yeni tasarıma
ve açıklamaya taşınır; kaynak başlığı, sanatçı, tarih ve müze credits olarak korunur.
Tarihsel niyet ve sembolizm türetilmez. Gözlemler model yorumudur; bağımsız insan
incelemesi anlamına gelmez. Gemini kapalı, erişilemez veya yanıtı geçersizse
`AUTOMATIC_EDITORIAL_AI_REQUIRED` / `AUTOMATIC_EDITORIAL_AI_UNAVAILABLE_OR_INVALID`
ile o deneme yayınlamadan durur; ham malzeme listesi anlatı yerine kullanılmaz.
Sonraki uygun zamanlanmış deneme yeniden üretir. Eksik/bozuk hak, görsel, state
veya kuyruk hatası da yayınlamayı durdurur; bozuk kuyruk sessizce atlanmaz.

GitHub'da `ARTFOLIO_GEMINI_ENABLED=true` ve kayıtlı `GOOGLE_GEMINI_API_KEY` gerekir.
`Verify Gemini editorial generation` manuel workflow'u `VERIFY_GEMINI` ile iki
sınırlı model çağrısı yapar; gerçek müze kaynaklarından tekli/carousel önizlemeleri
üretir ve üç günlük artifact bırakır. Instagram veya R2 credential'ı almaz ve
gönderi oluşturmaz. Yerel karşılığı:

```bash
python scripts/verify_gemini.py --output /path/new-preview --format both
```

Otomatik çıktının `artfolio-story-delivery-v3` kaydı makine doğrulamasını ifade
eder. Geriye uyumlu `reviewed_revision`/`reviewed_digest` alanları bu üretimin
sürümünü ve doğrulanan içeriğini bağlar; insan onayı anlamına gelmez. İnsan
onaylı v2 paketlerin sözleşmesi değişmez ve mevcut pilotlar otomatik onaylanmaz.

Yalnız önceden incelenmiş projelerle çalışmak için `main.py --content-mode editorial`
seçilebilir. Bu modda uygun onaylı stok yoksa
`EDITORIAL_REVIEW_REQUIRED` ile durulur. Aşağıdaki hazırlama ve masa komutları
bu isteğe bağlı insan incelemesi yoludur; zamanlanmış otomatik yayın için şart değildir.

```bash
# Önce masada görselleri ve açıklamayı inceleyip her projeyi ayrı onaylayın.
python scripts/review_editorial_workspace.py serve --workspace data/editorial-workspace
# Yerel, salt okunur stok kontrolü; bu örnekte sıra single ile başlar.
python scripts/prepare_feed_queue.py --directory data/editorial-queue --target 4 \
  --first-format single --editorial-workspace data/editorial-workspace --check-editorial
# Yalnız yeterli onaylı stok varsa yeni yerel kuyruk oluşturur.
python scripts/prepare_feed_queue.py --directory data/editorial-queue --target 4 \
  --first-format single --editorial-workspace data/editorial-workspace
```

`--check-editorial` seçilenleri, yeniden inceleme isteyenleri ve eksik gönderi
türlerini listeler. Hazırlama 3–5 dönüşümlü slotu yalnız güncel onaylı v2 projelerden
doldurur; aynı eseri tekrar seçmez. Sonraki değişiklik onayı geçersiz kılar.
Geçerli legacy/v1 paketler yerinde korunur; normal yayında seçilmez. Legacy stok
yeni biçime otomatik çevrilmez veya silinmez; varsa stok kontrolü geçiş gerektiğini
belirtir. `--status` genel envanterdir, editoryal uygunluk onayı değildir.

Canlı sisteme kurulum ayrıca açık `--r2 --skip-keychain` ve süreçte sağlanmış R2
yetkileri gerektirir. R2 kurulumunda `--first-format auto` sonraki türü kayıtlı
geçmişten/schedule durumundan alır. Bu komut Instagram'a yayın yapmaz. Normal
workflow, açık yerel kuyruk verilmediyse private R2 kuyruğunu kontrol eder.
Mevcut exact-SHA, kaynak hakkı, TTL, tekrar koruması, yayın yetkisi ve belirsiz sonuç
kilitleri sürer. Henüz onaylanmamış pilotlar otomatik onaylanmaz.

Yayın workflow'u `automatic` modu sabitler. `Check editorial Feed stock` workflow'u
yalnız private stok kontrolü yapar; manuel kontrol `CHECK_EDITORIAL_FEED` ister.
Hazır editorial stok yokken `ON_DEMAND_GENERATION_AVAILABLE` bildirir ve acquisition/R2
yazısı yapmaz; içerik uygun yayın koşusunda üretilir. Onaylı kuyruk yerel CLI üzerinden açık `--r2` ile kurulur; workspace
yerel mutlak proje yollarını tuttuğundan klasörü kopyalamak GitHub runner'ına
taşımak için yeterli değildir. Workflow yerel dosyaları otomatik taşımaz.
Mevcut deployment onayı olmadan yayın başlatılmaz; hazır stok zorunlu değildir.

Eski hazırlama/karışık v1 akışı yalnız açık `--content-mode legacy` veya
`ARTFOLIO_FEED_CONTENT_MODE=legacy` ile kullanılabilir; CLI seçimi ortamdan önceliklidir.
Eski acquisition dry-run örneklerine `--content-mode legacy` ekleyin. Editoryal
önizleme için yukarıdaki masayı kullanın. Aşağıdaki eski 6–9 eserli carousel,
AI önerisi ve otomatik refill açıklamaları bu uyumluluk yoluna aittir.

<a id="editorial-tools"></a>

## Esere özel editoryal taslak

Kaynakları açık bir bakış açısı, kısa giriş ve esere özel gözlemlerle hazırlayın:

```bash
python scripts/preview_editorial_story.py \
  --manifest data/editorial-sources/sources.json \
  --brief data/editorial-brief.json --output data/editorial-review
```

Brief örneği (`artwork_id`, manifestteki gerçek canonical kimliktir):

```json
{
  "narrative": "single_study",
  "source_ids": ["cleveland_173665"],
  "title": "An orchid, a distant forest",
  "angle": "The change in scale between a suspended blossom and a distant forest.",
  "lead": "The pale blossom fills the upper half of the painting. Below it, the forest recedes into smaller, darker marks.",
  "notes": [{
    "id": "flower-centre", "artwork_id": "cleveland_173665", "kind": "visual_note",
    "heading": "The dark centre",
    "text": "Pale petals surround a darker, densely marked centre.",
    "focus": [0.36, 0.19, 0.94, 0.59]
  }]
}
```

`index.html`, `draft.json`, `caption.txt`, kaynak kopyaları/SHA-256 ve 1080×1350
JPEG sayfalar oluşur. `artwork_led` tam eseri büyük gösterir; `journal` başlık ve
kısa gözleme yer açar; `detail_study` yalnız açıklanmış uygun bir crop varsa
oluşur ve tam eseri de içerir. Görsel renkleri ve tam eser oranı korunur.
Sayfalar içeriğe göre 1–10 adettir; zorunlu teaser/CTA/kapanış eklenmez.
Okunabilir alana sığmayan metin kesilmez, çıktı reddedilir.

`museum_quote` gerçek kaynak açıklamasından birebir alıntı, `catalogue_fact`
ise `field` (`title`, `artist`, `date`, `medium`) ile eşleşen kayıtlı değerdir.
`visual_note` insanın doğrulayacağı görsel gözlemdir; tarihsel doğruluk veya
sanatçı niyeti otomatik onaylanmaz. Detayda heading, metin ve normalize
`[left,top,right,bottom]` focus gerekir; düşük çözünürlük, boş veya tekrar eden
kırpmalar reddedilir. Kalıp İngilizce övgü/hook/CTA ve kayıtlı başlık tekrarı
engellenir; bu sınırlı kontrol tüm klişeleri veya yanlış iddiaları yakalayamaz.
`--history` isteğe bağlı en fazla 100 başlık içeren JSON listesidir.

`comparison` iki kaynak, `comparison_aspect` (composition/gesture/light/medium/date)
ve `comparison_rationale` gerektirir. Her iki eserin seçilen yöne ait farklı
dayanağı bulunmalıdır; aynı sanatçı bilgisi yeterli değildir. `thematic_selection`
en az üç kaynak ve her eser için gözlem gerektirir.

Yeni hedef klasör zorunludur; mevcut çıktı değiştirilmez. Bu araç yerel tasarım
incelemesidir, mevcut `StoryDelivery`/editör projesi değildir. AI, müze veya
Instagram çağrısı, yayın onayı, export ya da kuyruk kaydı oluşturmaz. Otomatik
yayın akışı bu değişiklikle farklılaşmaz.

## Editoryal pilot, editör ve onaylı teslim

Beş gerçek eseri dört taslakta deneyen örnek seri
`data/editorial_pilot_series.json` içindedir. Manifest, örnekteki beş canonical kimliğin
hakları açık yerel kaynaklarını içermelidir; görsel dosyaları manifestin altında kalır.

```bash
python scripts/build_editorial_pilot.py --manifest data/pilot-sources/sources.json \
  --series data/editorial_pilot_series.json --output data/editorial-pilot
# İsteğe bağlı: --history history.json --now 2026-10-18T12:00:00+03:00
python scripts/preview_story.py serve --project data/editorial-pilot/story-02/project
```

Pilot, beş kaynağı tam birer kez kullanır. Dört inceleme projesi, tasarım
alternatifleri ve `rhythm.html`/`rhythm.json` oluşturur. Geçmiş verilmezse ritim
boş örnek geçmişe dayanır; mevcut hesabın gerçek yayın sırası olduğu iddia edilmez.
Özgün görseller proje içinde saklanır. Pilot kendiliğinden onaylanmaz.

Tek bir brief'i de mevcut sürümlü editöre taşıyabilirsiniz:

```bash
python scripts/preview_story.py create-editorial \
  --manifest data/editorial-sources/sources.json --brief data/editorial-brief.json \
  --theme-id flowers_in_painting --theme-title "Flowers in Painting" \
  --style journal --output data/editorial-project
python scripts/preview_story.py serve --project data/editorial-project
```

Editörde başlık, anlatım açısı, giriş, gözlem, detay başlığı/alanı ve düzen aynı
ekranda değişir. Detay fareyle veya klavyeyle seçilir. Katalog/müze alıntıları
kaynakla eşleşmek zorundadır; editörde salt okunur gösterilir. Açıklamanın teslim
edilecek tamamı önizlemede görünür. Kaydetmek onay vermez; her değişiklik sürümü
artırıp önceki onayı geçersiz kılar. Hatalı kayıt önceki sürümü korur. Aynı yerel
Host/Origin, oturum tokenı, JSON/boyut ve eski sürüm kontrolleri uygulanır.

Birden fazla projeyi gerçek kapaklarıyla yan yana incelemek için:

```bash
python scripts/review_editorial_workspace.py create \
  --projects data/editorial-pilot/story-01/project data/editorial-pilot/story-02/project \
             data/editorial-pilot/story-03/project data/editorial-pilot/story-04/project \
  --dossier data/editorial_pilot_dossier.json --output data/editorial-workspace
python scripts/review_editorial_workspace.py serve --workspace data/editorial-workspace
```

Akış masası 1–12 açıkça seçilmiş proje alır. Kapak sırası yalnız tarayıcıdaki
önizlemede değişir; kayıtlı projeleri veya yayın kuyruğunu değiştirmez. Statik
çıktı oluşturulduğu anı, yerel sunucu ise son kaydedilmiş sürümleri gösterir.
Dosya isteğe bağlıdır: tek soru ve 3–5 projeye ait farklı katkılar içerir;
okuma sırası yayın planı veya toplu onay değildir.

Her proje, diğer seçili projelerin anlatım açısı, girişi ve görsel gözlemleriyle
karşılaştırılır. İsteğe bağlı `--history-project PATH` tekrar edilebilir; toplam
bağlam en fazla 100 projedir. Sözcük örtüşmesi ve kayıtlı görsel motifler açıklanır;
bu sınırlı uyarı anlamı kesin değerlendirmez, otomatik metin değiştirmez ve
onayı engellemez. Geçmiş verilmezse gerçek hesap geçmişi taranmaz.

Kaynak paneli katalog bilgisini, doğrudan müze alıntısını ve editoryal gözlemi
ayrı gösterir; alıntının müze metnindeki yakın bağlamı ve mevcut kaynak bağlantısı
korunur. Son kaydedilen değişiklikten sonra önceki sürümün gerçek görselleri ve
değişen metinler karşılaştırılabilir. Kayıtlı öncülü olmayan eski projelerde
önceki sürüm varsayılmaz. Yerel editörde inceleme kutusunu işaretleyip verilen
onay, gösterilen sürüm ve doğrulanmış dosya özetiyle eşleşir; kaydedilmemiş
değişiklik varken onay kapalıdır. Onay canlı yayın başlatmaz.

Gerçek projelerden tekrar bir ritim planı üretmek için:

```bash
python scripts/plan_editorial_projects.py --history history.json \
  --projects data/editorial-pilot/story-01/project data/editorial-pilot/story-02/project \
             data/editorial-pilot/story-03/project data/editorial-pilot/story-04/project \
  --output data/editorial-rhythm
```

Mevcut geçmiş planlayıcısınıncadence politikasının yayın aralığı alt sınırı ve tekli/carousel dönüşümü korunur.
Gerçek sayfa sayısı gönderi türünü belirler; aynı kaynak birden fazla projede
planlanamaz. Uygun proje olmayan slotlar ve döneme sığmayan projeler açıkça listelenir.
Bu araç kuyruk önceliğini değiştirmez, yayın tarihi garanti etmez veya onay vermez.

Görseller ve açıklama incelendikten sonra mevcut açık onay ve export komutları:

```bash
python scripts/preview_story.py approve --project data/editorial-project --expected-revision 1
python scripts/preview_story.py export --project data/editorial-project --output data/editorial-export
```

`artfolio-editorial-story-v2` projeleri açık `artfolio-story-delivery-v2` üretir:
tek sayfa/tek kaynak `single`,2–10sayfa `carousel` olur. Her kaynak tam eser
sayfası içerir; zorunlu kapak/kapanış yoktur. Eski v1 plan/teslim kuralları ve
renderer'ı korunur. Onay; güncel planı, kaynak metadata ve dosyalarını, renderer/font
bağımlılıklarını, sayfaları ve görülen açıklamayı bağlar. Sonraki değişiklikler
yeniden inceleme gerektirir. Normal paket, hikâye metadata'sıyla bu kontrolleri atlayamaz.

Mevcut `prepare_feed_queue.py --story-project` onaylı projeyi artık kendi gönderi
türündeki ilk uygun slota yerleştirir; tekrar verilebilir. Tüm projeler hazırlık
öncesinde doğrulanır, kaynakları diğer slotlardan dışlanır ve hazırlık sırasında
onay değişirse işlem reddedilir. Örneğin dört onaylı pilot proje için:

```bash
python scripts/prepare_feed_queue.py --directory data/editorial-queue --target 4 \
  --first-format single --skip-keychain \
  --story-project data/editorial-pilot/story-01/project \
  --story-project data/editorial-pilot/story-02/project \
  --story-project data/editorial-pilot/story-03/project \
  --story-project data/editorial-pilot/story-04/project
```

Bu komut `--r2` verilmeden yalnız yerel kuyruk hazırlar. Varsayılan editoryal modda
eksik slotlar yeni onay bekler; yalnız açık legacy modunda mevcut eser hazırlayıcısı
kullanılır. Canlı tüketimde güncel hak, korunan eser,
TTL,cadence politikasının yayın aralığı ve exact-SHA yayın yetkisi kontrolleri sürer. İncelenmiş tekli içerik de
aynı dosya özeti, yayın sınırı, belirsiz sonuç kilidi ve makbuz/uzlaştırma korumalarını
kullanır. Pilot veya editör açılması canlı kuyruğa kayıt ya da yayın izni değildir.

## Yerel editoryal plan ve hikâye önerileri

İki veya dört haftalık tema fikirleri için mevcut, salt okunur yayın geçmişi
dosyasını kullanın. Her çıktıda yeni bir hedef klasör gerekir:

```bash
python scripts/plan_editorial.py --history history.json --weeks 2 \
  --seed editorial-review --output data/editorial-plan
```

`index.html` ve `plan.json` oluşturulur. Gerçek son başarılı yayın sonrası en az
Cadence politikası ve carousel/tekli dönüşümü korunur. Temalar mevcut registry/planner'dan
gelir. Geçersiz, çelişkili veya gelecekteki yayın geçmişinde plan reddedilir.
Bu yalnız fikir takvimidir: hazırlanmış eser, mevcut kuyruk sırası, uygunluk
onayı veya kesin yayın tarihi değildir. `--now` saat dilimli zamanla tekrarlanabilir
önizleme sağlar; üretim zamanlayıcısı değiştirilmez.

Mevcut hakları doğrulanmış yerel görsel manifestinden incelenecek hikâye taslakları:

```bash
python scripts/suggest_stories.py --manifest data/editorial-sources/sources.json \
  --maximum 3 --output data/story-suggestions
python scripts/preview_story.py serve --project data/story-suggestions/story-01
```

Yeni klasörde bir HTML öneri listesi, `suggestions.json` ve mevcut editörle
açılabilen sürümlü projeler oluşur. En fazla altı alternatif; kaynak sayısına göre
tek eser, metadata temelli karşılaştırma ve seçki önerilir. Hak, dosya, kalite,
başlık geçmişi ve en fazla on sayfa kontrolleri korunur; `--history` isteğe bağlı
son 100 başlık JSON listesidir. Aynı eser farklı alternatiflerde bulunabilir.
Mevcut çıktının üzerine yazılmaz; başarısız üretim hedefe yarım proje yerleştirmez.
Görsel detaylar insan incelemesi gerektirir. Araç AI, müze veya Instagram çağrısı,
editoryal onay, export, kuyruğa ekleme ya da üretim izni oluşturmaz.

## Ne yapar?

- Art Institute of Chicago, Metropolitan Museum of Art, Cleveland Museum of Art,
  Rijksmuseum, Smithsonian Institution ve Europeana kaynaklarından eser adayları toplar.
- Production publish yalnız açıkça ayarlanmış `ARTFOLIO_RIGHTS_POLICY=strict_public_domain` ile başlar; yalnız doğrulanmış public-domain/open-access eserleri kabul eder. Production dışı araçlarda geriye uyumluluk için env verilmezse `permissive` davranış korunur. Her iki mod da `is_public_domain`, `rights_status`, `rights_text`, `copyright_notice`, `credit_line`, source ve eser URL'sini telemetry/attribution olarak korur.
- Duplicate, hak-policy, kalite ve tema uyumluluğu filtrelerinden geçen görselleri güvenli biçimde indirir; HTTPS-only erişim, private-network/SSRF koruması, redirect yeniden doğrulaması, sınırlı indirme ve Pillow doğrulaması uygular.
- Chicago kaynağında yalnız açıkça public-domain eserler 1686px IIIF türevini kullanır; diğerleri API'nin meşru 843px analysis türevinde kalır. AIC IIIF istekleri süreç içinde tekilleştirilip yaklaşık saniyede bir isteğe sınırlandırılır ve `AIC-User-Agent` ile tanımlanır.
- Carousel featured eserlerini ortak 1080×1350 (4:5) editorial canvas üzerinde, tam eseri contain ederek ve tek kez compositing yaparak gösterir. Boş alan carousel boyunca ortak deterministic nötr gallery field'dır; crop, stretch, blurred clone veya metadata overlay yoktur.
- Tekli gönderide tek eserin oranını ve tamamını korur; yalnız gerekli Instagram uyumluluk dönüşümünü uygular. Caption kaynak başlık, sanatçı, tarih ve müze bilgisini korur; eksik bilgi uydurulmaz.
- Gemini kullanılabiliyorsa caption, alt metin ve görsel metin önerisi üretir; anahtar yoksa veya istek başarısız olursa yerel fallback caption kullanır.
- History bilgisini Cloudflare R2 üzerinde tutar; Instagram publish öncesinde durable kilit kullanarak olası duplicate paylaşımları engeller.
- Kaynak hatalarını `source_health` kategorileriyle izole eder; bir adapter'ın erişim,
  rate-limit veya upstream hatası diğer beş kaynağın seçimini durdurmaz.

## Çalışma modu

Instagram feed `--mode auto` ile son başarıyla kesinleşmiş Feed publication'a göre **carousel → single → carousel** sırasını izler. Başarılı Feed kaydı yoksa carousel ile başlar. Başarısız, belirsiz veya yalnız rezerve edilmiş denemeler sırayı ilerletmez; Reel kayıtları sıralamaya katılmaz. History okunamazsa publish başlamaz. Her iki format aynı tek kullanımlık production izni, durable reservation, pre-publish CAS ve receipt akışını kullanır.

Tekli gönderi, güvenli biçimde doğrulanan adaylar arasından Instagram görsel koşullarını sağlayan ilk eseri seçer. Uygun olmayan görseller sınırlı aday havuzunda atlanır; eser kırpılmaz veya çerçeve eklenmez. Havuzda uygun görsel yoksa rezervasyon ve Instagram isteği yapılmadan çalışma sonlanır.

Carousel koşusu bir editorial cover ve adaptive **5–8 featured eser** üretir; toplam slide sayısı 6–9'dur. En az beş güvenli, kaliteli ve temaya uyumlu featured eser ile bunlardan farklı bir cover bulunamazsa publication başlamaz. Sekize tamamlamak için zayıf aday eklenmez. Tekli koşu, mevcut güvenli tek eser seçicisiyle yalnız bir görsel seçer ve tek Instagram image container yayımlar.

Editorial kapak 1080×1350 ölçüsünde Source Serif başlık ve Source Sans bilgi satırları
kullanır. Başlığın özgün harf düzeni korunur; metin alt bölgede, kırık beyaz renkte ve
yerel karartmayla sunulur. Başlık/alt başlık/bilgi satırları kesilmeden boyutlandırılır;
okunabilir alanı aşan metin açık hatayla durur. Kısa alt başlık tema ve sayıları tekrar
etmez; eser/sanatçı/koleksiyon sayıları ayrı factual satırda kalır. Fontlar ve SIL OFL
lisansları `assets/fonts/source-serif/` ve `assets/fonts/source-sans/` altında paketlidir.
Yeni AI caption girişlerinin hedefi 50–80 kelimedir; metadata, credits ve factual
fallback korunur. Bu bir üretim hedefidir, model çıktısına zorunlu kelime sınırı değildir.
Mühürlenmiş hazır kuyruk paketlerinin görselleri/metinleri sonradan değiştirilmez;
yeni stil yalnız bu kodla yeniden hazırlanan içerikte görünür.

Carousel teması doğrulanmış registry ve deterministic Theme Planner tarafından seçilir. Reel/export ve legacy history okuma altyapısı feed publisher'dan ayrıdır. CLI `carousel`, `single` ve `auto` modlarını destekler; geriye uyumluluk için açık mod verilmezse carousel çalışır.

## Artfolio Theme Registry

Carousel tema evreni `data/carousel_themes.json` içindeki data-driven registry'de tutulur. Her kayıt stable snake_case ID, İngilizce editorial title, kontrollü theme family ve carousel format, açık primary/secondary sorgular, required/preferred/excluded relevance sinyalleri, opsiyonel seasonal aylar ve sınırlı editorial priority taşır. Required sinyaller varsayılan olarak OR grubudur; yalnız gerçekten iki kavramı birlikte gerektiren temalar küçük, backward-compatible `required_term_groups` alanıyla AND-of-OR mantığı kullanabilir. Python katmanı şemayı, enum değerlerini, sorgu dizilerini, ay aralığını ve duplicate ID'leri doğrular; bozuk registry sessizce fallback yapmadan çalışmayı durdurur. Yeni tema eklemek carousel orchestration kodunu değiştirmeyi gerektirmez.

Carousel acquisition registry sorgularını güç sırasıyla ve sınırlı çalıştırır: en fazla üç primary, gerektiğinde iki secondary sorgu; sorgu başına adapter başına 16 aday ve tema başına en fazla 20 adapter çağrısı. Her logical sorgudan sonra pool yeniden değerlendirilir ve yeterli headroom oluştuğunda kalan sorgular çağrılmaz. Aynı canonical eser farklı sorgulardan gelirse tek aday kalır, fakat primary/secondary türü, sıra ve matched-query provenance'ı idempotent biçimde birleştirilir. Featured eserler ile editorial cover aynı typed acquisition pool'unu kullanır; cover nihai 5–8 featured ID'yi dışlar ve kendi secure-download ile cover-score kontrollerinden ayrıca geçer.

Metadata matching Unicode NFKC/casefold, punctuation ve whitespace normalizasyonu ile exact token/phrase boundary kullanır. Artist adı, müze adı ve credit/copyright metni tematik kanıt sayılmaz. Exact required-term eşleşmesi güçlü kanıttır ancak tek qualification yolu değildir: güçlü primary-query provenance ile title/description/classification/medium alanlarında en az iki destekleyici mevcut sinyal birlikte yeterli olabilir. Museum search sonucu tek başına yeterli değildir. Preferred sinyaller sınırlı bonus verir; excluded sinyaller güvenilir descriptive alanda bulunduğunda aday reddedilir. `theme_relevance_score` ayrı bir 0–100 ölçüdür ve varsayılan geçiş eşiği 50'dir. Carousel sıralamasında relevance baskın, teknik `quality_score` önemli ama ikincildir; yüksek kalite düşük relevance'ı kurtaramaz. `quality_score` yalnız eserin teknik/metadata kullanılabilirliğini ölçmeye devam eder.

Tema kanıtı `METADATA` veya `HYBRID` olarak tanımlanır. Subject/iconography gibi `METADATA` temaları mevcut metadata ve query provenance politikasını korur. Renk ve ışık odaklı `HYBRID` temalar ise düşük eşikli metadata/query prequalification sonrasında, yalnız secure validation ile zaten indirilmiş dosyadan çıkarılan bounded luminance, contrast ve coarse color sinyallerini final relevance'a ekler. Pixel kanıtı yalnız gerçekten ölçebildiği görsel özelliği destekler; örneğin parlaklık “winter”, renk ise “candle” veya “woman reading” semantiğinin yerine geçmez. Final geçiş eşiği her iki modda da aynıdır.

Secure validation'dan geçen en güçlü en fazla 24 finalist için candidate score'dan ayrı, açıklanabilir bir carousel set score hesaplanır. Bounded optimizer 5–8 arasındaki her uygulanabilir cardinality için en iyi seti değerlendirir; artist/müze/region dominance sınırlarını korurken period, medium, orientation, luminance ve kaba renk çeşitliliğini bounded guardrail olarak kullanır. Candidate quality/editorial score, güvene göre kademeli learned engagement score ile harmanlanır; set düzeyinde tema/format/count/hook/slot ve artwork-set feature tahmini ayrıca değerlendirilir. Sonuç inclusion eşiğinin altına düştüğünde büyüme durur. Böylece zayıf veya redundant tail sırf sekize tamamlamak için eklenmez.

Set seçimi tamamlandıktan sonra seçilen 5–8 featured eser ayrı bir deterministic sequencing aşamasından geçer. Caption numaraları, `carousel_01.jpg`–`carousel_NN.jpg` artifact sırası ve history `featured_position=1..N` değerleri bu nihai sırayı izler; padding, placeholder veya duplicate slide üretilmez. `CHRONOLOGICAL` format bilinen tarihleri artan sıraya dizer ve bilinmeyen tarihleri stable ID sırasıyla sona koyar; diğer formatlarda opener/theme establishment, adjacent visual rhythm, güçlü eser dağılımı ve closure birlikte puanlanır.

Availability preflight image indirmeden canonical/history duplicate, merkezi rights policy, format target, pre-download metadata/image prerequisites ve relevance kapılarını bu sırayla sayar. `estimated_safe_pool`, son relevance kapısından geçen fakat henüz secure image validation'dan geçmemiş eser sayısıdır. Absolute construction minimum altı unique eserdir: en az beş featured ve bunlardan farklı bir cover. Altı ile on bir aday arası publish edilebilir ama dar pool, 12 aday rahat operational headroom sayılır. `minimum_candidate_target` yalnız preferred acquisition headroom/early-stop hedefidir. Pipeline minimum altı adayla construction aşamasına geçebilir; bounded acquisition ve en fazla 40 secure validation denemesi mümkün olduğunda 24 finalistlik optimizer headroom'unu toplamaya devam eder. Optimizer her boyutta en az bir cover-eligible ID'yi featured set dışında bırakır.

Attempt sıralaması editorial score'un anlamını değiştirmeden ayrı bir feasibility katmanı uygular. Mevcut adapter/credential/circuit-breaker kapasitesi ile `theme_feasibility.json` içindeki tema başına son 12 gerçek acquisition sonucu, 30 günlük half-life ve üç effective örnekte doygunlaşan confidence ile değerlendirilir. Tek başarısızlık sınırlı bir ceza verir; tekrar eden güncel başarısızlıklar en fazla `-6`, güçlü güncel headroom en fazla `+3`, hiç aktif compatible source bulunmaması `-8` etkiler ve toplam feasibility adjustment `[-8, +3]` aralığında kalır. Bu bounded skor gözlemlenebilir kalır; ancak o anda hiç aktif compatible source'u olmayan tema yalnız mevcut run için attempt dışıdır. Yeni temalar nötrdür; eski sonuçlar nötre decay eder ve hiçbir tema kalıcı olarak elenmez. Production state ayrı, ETag-conditional R2 objesinde tutulur; local ortam atomik dosya fallback'i kullanır. Okuma/yazma veya corruption hatası cold start'a düşer ve publishing'i engellemez. Objede credential, query metni, response body veya exception dump saklanmaz.

İlk planner teması yeterli değilse feasibility ile açıklanabilir deterministic sıradaki sonraki tema denenir; en fazla beş METADATA teması değerlendirilir. Beşinin tamamı başarısız olursa aynı acquisition, rights, quality, secure-image, optimizer ve cover yolunu kullanan tek `Artfolio Selection` fallback'i theme relevance kapısı olmadan denenir. Bu nötr fallback ortak subject, period, medium, region veya çoklu museum iddiası kurmaz. Yalnız tamamlanmış 1 cover + 5–8 featured planının kimliği publication state'e yazılır; feasibility telemetry ayrı media bucket nesnesinde kalır.

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

Yeni `main` sürümünün mevcut sürekli üretim iznine aktarımı için `Release reviewed
editorial Feed` workflow'u önce `inspect`, ardından aynı SHA ve dönen generation
ile açık `activate` işlemi sağlar. Aktivasyon `DEPLOY_REVIEWED_EDITORIAL_FEED`
onayı ister. Önceden duraklatılmış veya çözülmemiş bir yayını otomatik açmaz;
varsayılan olarak mevcut cadence politikasını korur. Açık `cadence=six_hourly_utc17`
seçimi altı saatlik politikaya geçişi aynı CAS içinde yeni sürüm iznine bağlar. Proje onayı, stok kurulumu ve Instagram
yayını yapmaz; production anahtarları yalnız GitHub'daki operatör adımında kalır.

Nominal Feed saatleri Türkiye saatiyle **03:17 / 09:17 / 15:17 / 21:17**'dir.
GitHub gecikmelerini karşılamak için uygunluk saatte bir kontrol edilir:

```text
Feed:     17 * * * *
```

GitHub Actions cron ifadeleri UTC’dir; yeni düzen `six_hourly_utc17` sürekli çalışma
izni ve control schema v2 gerektirir. Eski `daily_utc1717` / `hourly_utc17`
izinlerinin 48 saatlik politikası geriye uyumluluk için korunur. Cron tek başına
kalıcı izni değiştirmez. Yeni altı saatlik permit bir sonraki nominal saate kadar
geçerlidir; aynı aralıkta yalnız bir deneme yapılır. İlk saatten sonra tamamlanan
gecikmiş yayın, peş peşe telafi yayını yapmamak için sonraki uygunluğu gerçek
tamamlanma +6 saat olarak belirler. Eski bir saatlik permit kayıtlarının hesabı
korunur. GitHub koşuyu geciktirebilir; yayın dakikası garanti edilmez.
Her koşunun Actions özeti, receipt ve tüketilmiş izin kanıtıyla Instagram'a yayın
yapılıp yapılmadığını açıkça gösterir; yeşil çalışma tek başına yayın kanıtı değildir.
Yeni geniş permit yazılmadan önce yerel Insights dahil tüm state okuyucuları uyumlu
sürüme geçirilmelidir. Experiment metadata’sındaki dört legacy zaman bucket’ı korunur; bunlar publication izni veya cron sıklığı değildir.

Scheduled invocation açıkça `python main.py --mode auto` çalıştırır. Scheduled production varsayılan olarak kapalıdır; repository Actions variable `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED` tam olarak `true` olduğunda publish job’ı çalışabilir. Feed publication ayrıca tek slotluk durable Stage B permit veya açıkça verilmiş sürekli çalışma izni ve onaylanan cadence politikasına uygun yayın kanıtı gerektirir. Değişkenin eksik olması veya farklı bir değer taşıması fail-closed davranır. Tek slot modu her denemeden sonra duraklar. `enable-continuous` ile aynı main sürümüne verilen kalıcı izinde normal başarılı yayınlardan sonra bot duraklamaz; izin verilen zaman penceresindeki uygunluk koşusunda sıradaki formatı seçer. Cooldown dolmamışsa içerik üretmeden başarılı biçimde atlar. Belirsiz yayın, eksik receipt veya çözülmemiş cleanup durumunda yeni paylaşım yapmaz; aynı slotta tekrar denemez. Yeni main sürümü için çalışma izni yeniden verilmelidir.

Workflow manuel olarak da **Actions → Instagram Art Bot Scheduler → Run workflow** üzerinden başlatılabilir. Manuel yol da `auto` sırasını izler; `confirm_publish` alanı tam olarak `PUBLISH_TO_INSTAGRAM` olmalıdır. Operatör her yeni publication izni için rastgele bir UUIDv4 üretir, repository Actions variable `ARTFOLIO_MANUAL_AUTHORIZATION_ID` olarak ayarlar, aynı anda `ARTFOLIO_MANUAL_AUTHORIZATION_ISSUED_AT` değerini UTC ISO-8601 zaman damgası olarak ayarlar ve UUID'yi `authorization_id` dispatch alanına girer. Reel manuel dispatch'i aynı iki variable'ı ve kendi `PUBLISH_REEL_TO_INSTAGRAM` onayını kullanır. Tek bir UUID, Feed veya Reel tarafında yalnız bir reservation CAS yazısında tüketilebilir; ikinci dispatch aynı UUID'yi yeniden kullanamaz. İzin verildikten sonra iki variable yeniden güncellenmeden başka manuel publication yapılamaz.

Run attempt yalnız `1` olabilir. Workflow, GitHub Actions run API'sinden repository, event, commit, run ID, attempt ve değişmez `created_at` değerini doğrular; Actions token'ı yalnız `actions: read` ve `contents: read` yetkilerine sahiptir. Manuel izin veriliş zamanı ve run oluşturulma zamanı, reservation anında en fazla 60 dakika eski olabilir; run izin verilmeden önce oluşturulmuşsa veya sırada gecikmişse `PRODUCTION_AUTHORIZATION_EXPIRED` ile durur. Scheduled Feed ve Reel event'leri kendi schedule variable'larına bağlıdır ve `schedule:<run_id>` iznini bir kez tüketir. `instagram-bot` concurrency grubu aynı anda tek publish job'ına izin verir; tek kullanım kuralı ayrıca durable state CAS ile uygulanır. Scheduler ve workflow'ları etkinleştirmek ayrı rollout kararı gerektirir.

Job timeout’u **45 dakika**dır. Dokuz child ve bir parent container’lı carousel’in bounded Instagram status polling süresine acquisition/render payı bırakır; 2 saatlik stale `PENDING` eşiğinin altında kalır.

Her workflow invocation şu sırayla ilerler:

```text
dependency install
→ lightweight read-only readiness check (scheduled runs)
→ compile validation
→ pytest
→ read-only Feed production preflight
→ production bot
```

Compile adımı `main.py`, `src`, `scripts` ve `tests` kapsamını; test adımı hızlı olan full `pytest -q` suite’ini çalıştırır. Install, compile, test veya preflight başarısız olursa production adımı çalışmaz. Instagram ve içerik üretim secret’ları yalnız preflight ve publish adımlarına verilir; readiness kontrolü salt okunur R2 state/receipt ve sahipli media envanterini okur. Compile ve test adımları secret almaz. Preflight strict rights/config ve public HTTPS URL biçimini, ayrı durable-state bucket yapılandırmasını, `publication_safety_state.v2.json` ve `publication_receipts.v2.json` nesnelerinin varlığını, şemalarını ve GET yanıtlarındaki expiration metadata'sını doğrular; Instagram hesap kimliğini GET ile okur. Eksik veya bozuk v2 state production'ı durdurur; eski `posted_history.json` otomatik fallback değildir. Preflight canlı `PENDING`, `PUBLISHING`, `AMBIGUOUS`, eksik receipt ve çözülmemiş cleanup queue durumunu durdurur. Eski ama hâlâ `PENDING` olan kayıt operatör uzlaştırması gerektirir; durable `EXPIRED` durumuna geçmiş ve medyası temizlenmiş kayıt engel değildir. Reservation'dan hemen önce authoritative state ve receipt tekrar okunur, sahipli R2 media prefix'leri salt okunur listelenir; `EXPIRED` veya bilinmeyen publication ID altında kalan media anomali sayılır, normal `PUBLISHED` media retention kabul edilir. İzin tüketimi ve reservation aynı state CAS yazısındadır. Bucket lifecycle kuralları deployment sırasında ayrıca denetlenir; scoped runtime credential ile bucket konfigürasyonu okunmaz. Preflight R2 write veya Instagram publish yetkisini kanıtlamaz.

Receipt `occurred_at` alanı boş (`null`) olabilir; mevcut olduğunda timezone zorunludur. Kabul edilen biçim `YYYY-MM-DDTHH:MM:SS` (isteğe bağlı 1–6 haneli kesir) ve ardından `Z`, `+HH:MM`/`-HH:MM` veya recovery ledger'daki `+HHMM`/`-HHMM` offset'idir. Geçersiz tarih/saat, hatalı offset, timezone'suz değer ve sonda ek veri reddedilir. Orijinal receipt metni ve digest değiştirilmez. CI'daki Python 3.10 testleri, production preflight'in bu legacy offset'i okumasını ayrıca doğrular.

Bu kod yalnız yeni workflow revizyonlarını fence eder. Eski GitHub Actions run'ları kendi eski commit/workflow koduyla yeniden çalışabilir; özellikle V2 publication secret'ını kullanan canary run'ları, bu PR merge edilse bile yeni run-attempt ve authorization kontrollerini otomatik olarak kazanmaz. Eski run'ların rerun yeteneği ayrı operatör kontrolüyle kapatılmadan rollout yetkisi verilmemelidir.

### Reel candidate, portfolio ve handoff katmanı

Reel üretimi Instagram feed publisher'ından ayrıdır. `reel_candidate_acquisition`, altı
müze adapter'ından hakları doğrulanmış ve güvenli görseli olan adayları toplar;
`reel_selector` teknik ve editoryal kabul kararlarını, `reel_portfolio` ise batch içi
artist/müze/region çeşitliliğini uygular. `reel_batch_candidates` ve `reel_handoff`,
yerel Remotion tüketicisine required metadata, kaynak kimliği ve doğrulanmış asset
bilgisi taşıyan atomik bir handoff üretir. Bu katman Instagram'a publish etmez ve
feed history lifecycle'ını mutate etmez.

<a id="insights"></a>

### Instagram Insights collector

Cloud collector her saatin 43. dakikasında çalışır; tamamlanmış hedef yaş
ölçümleri yeniden sorgulanmaz. Böylece günlük koşular arasındaki boşluk yüzünden
1h/6h ölçüm pencerelerinin kapanması riski azaltılır. GitHub zamanlaması yine
gecikebilir; geçmişte kapanmış pencereler bugünkü verilerle doldurulmaz.
Yerel saatlik collector ek bir toplama yoludur. `--health-check`, collector
profilindeki ayrı state-bucket yapılandırması dahil erişimi ve son başarıyı
kontrol eder; eksik yapılandırma giderilmeden yerel toplama başlamaz.
[GitHub schedule gecikme açıklaması](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

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
cd /path/to/instagram-art-bot
python3 scripts/install_insights_launchd.py configure-keychain
python3 scripts/collect_insights.py --check-secrets
python3 scripts/collect_insights.py --health-check
python3 scripts/install_insights_launchd.py install
```

`configure-keychain`, collector profiline media bucket değişkenleri ile ayrı state bucket'ın üç değişkenini yükler ve her eksik değer için macOS Keychain’in gizli giriş prompt’unu açar; değer komut satırı argümanına, shell history’ye veya log’a girmez. Mevcut process environment değerleri interaktif çalıştırmada önceliklidir, fakat LaunchAgent yalnız `com.artfolio.instagram-insights.*` kayıtlarını yükler. Installer idempotent olarak `~/Library/LaunchAgents/com.artfolio.instagram-insights.plist` dosyasını günceller ve user LaunchAgent’ı yeniden yükler. Her login/reboot sonrasında ve en fazla saatte bir tek-seferlik collector çalışır. Dönen operasyon log’ları `~/Library/Logs/Artfolio/instagram-insights.log` altında 1 MiB + üç backup ile sınırlıdır.

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

Insights workflow'u her saat UTC 43. dakikada (`43 * * * *`) çalışır. Analytics hataları publishing state'ini veya history lifecycle'ını etkileyemez. Facebook Login modu için token’da `instagram_basic`, `instagram_manage_insights` ve `pages_read_engagement` izinleri bulunmalıdır.

Feed koşusu R2 history ile bütün `insights/YYYY-MM.json` partition'larını best-effort okur. Aynı publication için sırasıyla 72h, 168h ve 24h snapshot'ı seçilir; 1h/6h yalnız telemetry'dir. Share/save/comment/like oranları ve reach signal, account baseline'ına göre winsorize edilmiş bounded log normalization'dan geçer. Düşük reach, eksik metrik, 24h provisional maturity ve 180 günlük recency half-life effective weight'i azaltır. Artist, artist group, region, style/period, semantic family, museum/source, visual özellik, theme/format/count, cover/hook, slot/weekday ve preceding-post distance feature'ları global ortalamaya sample-size-aware shrink edilir. Model confidence sıfırdan kademeli büyür; cold start mevcut quality/editorial sıralamasını korur. Yaklaşık %10 seeded exploration yalnız normal teknik/kalite gate'lerinden geçmiş novelty adaylarına ayrılır.

`engagement_rates_v3`, carousel ve tekli gönderiler için ayrı modeller kurar. Her format kendi observation, normalization ve confidence değerlerini kullanır; tekli gönderi sonucu carousel sıralamasını değiştirmez. `publication_format` özelliği carousel'in editoryal `carousel_format` alanından ayrıdır. Audit ve doctor toplam kullanılabilir Feed verisini ve format bazında confidence/etkiyi gösterir; toplam confidence yalnız tanı amaçlıdır. Mevcut temporal backtest carousel kapsamını korur; tekli gönderilere özel temporal değerlendirme henüz eklenmedi.

Learning funnel'ını Instagram veya R2 verisini değiştirmeden incelemek için:

```console
python3 scripts/install_insights_launchd.py configure-audit-keychain
python3 scripts/install_insights_launchd.py audit-status
python3 scripts/audit_engagement_learning.py
python3 scripts/audit_engagement_learning.py --verbose
```

`configure-audit-keychain`, yalnız `com.artfolio.engagement-audit.*` altında media R2 alanlarını ve ayrı state bucket'ın üç değişkenini güvenli Keychain prompt’larıyla kurar. Audit state credential'ı yalnız okuma yetkili olmalıdır; analytics partition erişimi media bucket'ta kalır. `audit-status` değerleri göstermeden yalnız `AVAILABLE`/`MISSING` durumunu raporlar. Audit bu profilden collector namespace’ine fallback yapmaz ve Instagram credential’ı yüklemez. Her iki yerel komutta da mevcut process environment değerleri seçilen Keychain profilinden önce gelir; environment değişkenleri credential rolü metadata’sı taşımadığından doğru rolü sağlamak çağıranın sorumluluğundadır.

Komut publication/media identity eşleşmelerini, 24h/72h/168h coverage'ını, exclusion nedenlerini, seçilen slotları, reach dağılımını, maturity durumunu, effective observation toplamını ve global confidence'ı raporlar. `--verbose` publication kimliklerini hash'leyerek her observation weight faktörünü gösterir. Production R2’den okurken audit yalnız `GetObject` ve `ListObjectsV2` yollarını kullanır; `PutObject`, `DeleteObject` veya başka bir write yolu çağırmaz. `--history` ve `--snapshots` birlikte yerel dosya gösterdiğinde Keychain’e erişmez.

Her kesinleşen carousel `selection_model_version`, `engagement_model_version`, `carousel_theme`, `carousel_format`, `featured_count`, `cover_variant`, `caption_hook_type`, `publish_slot`, `exploration_selected`, `learned_score`, `engagement_confidence`, `quality_component`, `engagement_component`, `diversity_component`, `exploration_component` ve varsa `preceding_post_distance_minutes` alanlarını taşır. Eski publication kayıtlarında bu alanların bulunmaması geçerlidir.

### Eksik ölçüm ve aynı yaşta format karşılaştırması

```console
python3 scripts/report_feed_analytics.py
python3 scripts/report_feed_analytics.py --json --output /tmp/feed-analytics.json
python3 scripts/report_feed_analytics.py --history /path/history.json --snapshots /path/snapshots.json
```

Komut yalnız kayıtlı history ve Insights verisini okur; Instagram sorgusu veya R2 yazısı yapmaz. Uzak veri için ayrı `engagement-audit` profilini kullanır. İki yerel girdi verildiğinde Keychain'e erişmez. Çıktı dosyası zaten varsa üzerine yazmaz.

1h/6h/24h/72h/168h pencerelerindeki complete, partial, unavailable, missed, due ve pending durumları nedenleriyle gösterilir. Kaçırılan ölçüm penceresi sayısı, etkilenen gönderi sayısından ayrıdır. Kapanmış bir pencerenin geçmiş verisi bugünkü Insights ile yeniden üretilemez. Geç etiketlenmiş ölçümler complete sayılmaz; sonraki geç ölçüm önceki geçerli ölçümü düşürmez.

Karşılaştırma her hedef yaş ve format için ayrı observation, coverage, reach ve engagement oranlarını gösterir. Her metriğin paydası yalnız o metriği taşıyan gönderileri içerir; eksik değer sıfıra çevrilmez. Varsayılan asgari örnek sayısı format başına 5'tir (`--minimum-cohort-size`). Gerçek ölçüm yaşları açıklanır; yaş aralıkları ayrışırsa karşılaştırma uyarılır. Sonuçlar gözlemseldir, otomatik kazanan veya nedensellik iddiası üretilmez.

<a id="feed-queue"></a>

### Hazır içerik kuyruğu — yerel ve private R2

```console
python3 scripts/prepare_feed_queue.py --directory /path/new-feed-queue --target 3 \
  --editorial-workspace /path/editorial-workspace
python3 scripts/prepare_feed_queue.py --directory /path/new-feed-queue --status
```

Hazırlama tekli/carousel dönüşümüyle 3–5 paket oluşturur; her eser en fazla bir pakette kullanılır. Her pakette görseller, açıklama, eser/kaynak ve hak bilgileri, SHA-256 digesti ve en fazla 14 günlük son kullanma zamanı bulunur (`--ttl-hours`). Varsayılan mod mevcut onaylı v2 proje sayfalarını kullanır; reservation, staging, yayınlama, history yazısı veya model isteği yapmaz. Eksik onaylı stokta durur. Yalnız açık `--content-mode legacy` ile eski kaynak indirme/Gemini hazırlama ve read-only `engagement-audit` profili kullanımı korunur. Var olan manifestin üzerine yazılmaz.

`main.py --prepared-queue /path/new-feed-queue` mevcut yetkili publication akışında yerel kuyruğu seçer. Bu seçenek kendi başına yayınlama izni vermez. Format, süre, görsel digest/uyumluluk, kayıtlı confirmed hak bilgileri ve güncel duplicate koruması reservation öncesinde tekrar denetlenir. Paket yalnız doğrulanmış publication receipt sonrasında CONSUMED olur; belirsiz sonuç QUARANTINED olur ve otomatik yeniden kullanılmaz. Boş veya uygun editoryal paket bulunmayan kuyruk `EDITORIAL_REVIEW_REQUIRED` ile durur; yalnız açık legacy modunda acquisition'a döner. Bozuk manifest ve eşzamanlı sahiplik çatışması koşuyu durdurur.

Yerel pilot Mac/Linux üzerinde `fcntl` kilidi ve atomik manifest kullanır. Actions runner'ları için private durable-state bucket'ında `feed-queue/v1/` altında kalıcı R2 kuyruk desteği vardır. JPEG'ler UUID kapsamındaki immutable nesnelerdir; manifest create-only/ETag CAS ile kurulur ve sahiplenilir. Kaybedilen veya sonucu belirsiz sahiplik yazısı yayın başlatmaz ve otomatik tekrar edilmez. CLAIMED paketler süre dolmasıyla yeniden açılmaz. Tamamlanmış veya tamamen eskimiş partiler arşivlenerek yenilenebilir; geçerli READY ve tüm CLAIMED paketler yenilemeyi durdurur. Eski paketler ve asset'ler operatör incelemesi için korunur; otomatik silme yoktur.

```bash
# Açık state-writer environment credential'larıyla private R2'ye yazılır;
# yalnız incelenmiş projelerden paket hazırlar; Instagram publication yapmaz.
python3 scripts/prepare_feed_queue.py --directory /path/new-feed-queue --target 3 --r2 --skip-keychain \
  --editorial-workspace /path/editorial-workspace
# Yalnız R2 manifest GET; mutation yapmaz.
python3 scripts/prepare_feed_queue.py --directory /path/status-cache --r2 --status
```

`Check editorial Feed stock` workflow'u `main` üzerinden manuel `CHECK_EDITORIAL_FEED` ile yalnız stok kontrolü yapar. Instagram publish token'ı veya içerik üretim anahtarı almaz. Düzenli editoryal Feed varsayılan olarak private R2 kuyruğunu kullanır; açık yerel kuyruk seçilebilir. Legacy modda R2 tüketimi ayrıca `ARTFOLIO_FEED_QUEUE_ENABLED=true` veya `--prepared-queue-r2` ister. R2 tüketimi, mevcut adapter'larla bounded yeni müze sorgusunda **exact canonical artwork ID ve confirmed rights** ister. Hak doğrulaması rastgele keşif sayfalarını atlayıp ilk sonuçları kullanır; Rijksmuseum'da kayıtlı eser numarası resmi `objectNumber` alanında aranır. Eser yeniden bulunamazsa paket elenir; kayıtlı hak bilgisi güncel onay yerine geçmez. Bu konservatif kontrol geçerli paketleri de eleyebilir. Uygun editoryal stok yoksa yeni inceleme beklenir; okunamayan state veya CAS çatışması durdurur. Queue kurulumu, doldurulması ve activation ayrı üretim işlemleridir; kodun mevcut olması etkin olduğu anlamına gelmez.

Zamanlanmış stok kontrolü `ARTFOLIO_FEED_QUEUE_REFILL_ENABLED=true` ister; varsayılan kapalıdır. `ARTFOLIO_FEED_QUEUE_ENABLED` ve `ARTFOLIO_PRODUCTION_SCHEDULE_ENABLED` da `true` olduğunda aynı workflow günlük 04:37 UTC / 07:37 İstanbul kontrolü yapar. Kontrol kuyruğu doldurmaz; uygun onaylı girdi yoksa inceleme ihtiyacını bildirir. Legacy stok yeni biçime ait hazır içerik sayılmaz ve yerinde korunur. Yerelden açık `--refill --editorial-workspace ...` hazırlamada hem öncesinde hem yükleme öncesinde plan durumu, onaylı tam SHA ve sonraki format denetlenir. Duraklatma, SHA/format değişimi, bozuk state veya eşzamanlı yeni parti kurulumu yüklemeyi durdurur. Yayın izni oluşturulmaz; yayın state/receipt'leri değiştirilmez, mevcut paketler otomatik yeniden açılmaz veya silinmez.

```bash
# Salt okunur uygunluk kontrolü; acquisition veya R2 yazısı yapmaz.
python3 scripts/prepare_feed_queue.py --directory /path/status-cache --r2 --skip-keychain --check-refill --expected-sha EXACT_MAIN_SHA
# Aynı kontrollere bağlı yenileme; yeni ve boş bir yerel hazırlama dizini kullanın.
python3 scripts/prepare_feed_queue.py --directory /path/new-feed-queue --r2 --skip-keychain --refill --expected-sha EXACT_MAIN_SHA \
  --editorial-workspace /path/editorial-workspace
```

Plan ve kuyruk manifesti ayrı R2 nesneleridir; son plan kontrolü ile kurulum tek atomik işlem değildir. Bu aralıkta duraklatma/onay değişirse özel bir parti kurulabilir. Paket tüketimindeki mevcut yayın yetkisi ve hak kontrolleri yine uygulanır; yenileme kendi başına yayın başlatmaz.

Hazırlama, carousel'in gerçekten seçilen registry temasıyla sonraki tekli gönderiyi eşleştirir; aynı partide önceki eserleri ve carousel temalarını dışlar. Thematic tekli bulunamazsa normal tekliye döner ve `editorial_pair.status=unmatched` kaydeder. Single ile başlayan veya bilinmeyen tema kullanan paket için ilişki uydurulmaz. Pair kimliği reservation'dan confirmed publication'a ve reconciliation'a taşınır.

`ARTFOLIO_CAPTION_EXPERIMENT_ENABLED=true` yalnız yeni carousel hazırlığında `caption_hook_v1` deneyini açar. İki mevcut hook türü (`question`, `visual_detail`) seed ile atanır; deterministic factual body, cover ve credits korunur. Atama, gerçekten üretilen caption hook'u ile birlikte reservation'a kaydedilir. Tekli gönderiler bu deney kapsamına girmez. Prepared paketin deneyi sonradan değiştirilmez. Workflow'da `caption_experiment` manuel input'u vardır; default false.

```bash
python3 scripts/report_feed_analytics.py --history /path/history.json --snapshots /path/snapshots.json --experiments
python3 scripts/report_feed_status.py --r2 --expected-sha EXACT_MAIN_SHA
```

Deney raporu aynı carousel formatı/tema/cover/ölçüm yaşı için metric-specific coverage, örnek sayısı, ortalama, gözlenen aralık ve standart hata verir. Her iki kolda en az 5 ilgili ölçüm olmadığında kanıt yetersizdir. Sonuçlar observational'dır; otomatik winner/promotion yoktur.

Birleşik durum raporu salt okunurdur: son Feed başarısı, sonraki fırsat, hazır/eskimiş/sahiplenilmiş paket sayıları ve kimliği doğrulanmış karşılaştırılabilir analitik ölçümünün güncelliği gösterilir. Hazır sayısı manifest süre kontrolüdür; yayın öncesi byte/hak/history doğrulaması yine gereklidir. `ARTFOLIO_FEED_STATUS_ENABLED=true` repository variable'ı, Feed workflow sonunda `--notify` çalıştırır: yalnız ayrı `feed-status/v1/notifications.json` dedup nesnesine CAS yazar ve yeni engel, toparlanma veya yeni başarılı yayını Actions job summary'sinde gösterir. Aynı durum ve normal bekleme tekrar bildirilmez. Yeni harici bildirim kanalı veya otomasyon oluşturulmaz.

<a id="publication"></a>

## History ve duplicate koruması

History, Git commit/push ile değil Cloudflare R2’de saklanır. Kayıtların kısa lifecycle’ı şöyledir:

```text
PENDING → PUBLISHING → PUBLISHED
            ↘ EXPIRED (media_publish öncesi durma veya kesin 4xx reddi)
            ↘ AMBIGUOUS (sonuç kanıtlanamıyorsa)
PENDING → EXPIRED
```

`PENDING`, hiçbir irreversible `media_publish` isteğinin gönderilmediği anlamına gelir. Tek eser creation container ID'si veya carousel parent + child container ID'leri hazır olduktan sonra, `media_publish` çağrısından hemen önce bütün publication unit tek conditional R2 yazısıyla `PUBLISHING` olur. Bu yazı başarısızsa publish isteği gönderilmez. `media_publish` timeout, connection reset, malformed response veya 5xx sonucu otomatik tekrar edilmez; unit `AMBIGUOUS` kalır. Kesin 4xx reddi `EXPIRED`, başarılı response media ID'si ise önce durable receipt, ardından `PUBLISHED` olarak yazılır.

Yeni staging media objeleri yalnız `images/publications/<publication_id>/<timestamp>_<uuid>.<suffix>` altında oluşturulur. Tek eser ve carousel, history reservation'ın döndürdüğü aynı durable `publication_id` değerini kullanır. Upload sonucu exact object key + public URL taşıyan immutable bir handle olarak korunur. Public HEAD sonucu kaydedilir; anonymous GET asıl erişim kontrolüdür. GET doğrulaması başarısızsa yalnız o exact obje silinmeye çalışılır; Meta çağrılmadan önce yarım kalan carousel staging'i yalnız o invocation'ın tamamlanmış handle'larını geri alır.

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

Her production başlangıcı yeni seçimden önce en fazla 20 unresolved publication unit'i ve son 30 günlük pencereyi inceler. `PUBLISHING` unit'lerinde aktif 45 dakikalık workflow ile çakışmamak için 50 dakikalık grace kullanılır. Unit başına yalnız parent/creation container için bir logical status lookup yapılır; reconciliation GET çağrısı 10 saniyelik timeout ile en fazla iki HTTP attempt kullanır. `PUBLISHED` container status'u publication'ı kanıtlar; media ID'si olmadan unit yine `AMBIGUOUS` kalır. `FINISHED`, `ERROR` ve `EXPIRED` status'ları olası önceki `media_publish` çağrısının başarısızlığını tek başına kanıtlamaz; bunlar da `AMBIGUOUS` kalır. Unresolved unit'in kendi canonical ID'leri karantinada kalırken farklı eserler sonraki koşularda seçilebilir.

Manuel reconciliation bütün geçmişten en fazla 100 unresolved unit'i inceler; yeni eser seçmez, container oluşturmaz veya `media_publish` çağırmaz:

```bash
python3 main.py --reconcile-publications
```

Yeni carousel kayıtları mevcut artwork history şemasını koruyarak `theme_id`, `theme_family`, `carousel_format`, rights/source telemetry ve compact experiment metadata taşır. Aynı `publication_id` altındaki 6–9 artwork satırı planner için tek slot olarak okunur. Eski `theme` alanları ve metadata'sız legacy publication kayıtları yüklenmeye devam eder.

<a id="setup"></a>

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
python main.py --dry-run --mode single
python main.py --dry-run --mode auto

# Production config ve taze yayın izni zorunludur
python main.py --mode carousel
python main.py --mode single
python main.py --mode auto

# Network/acquisition başlatmadan yalnız required production config'i doğrular
python main.py --validate-production-config

# Instagram/R2 GET kontrolleriyle Feed production hazırlığını read-only doğrular
# --preflight-carousel eski adı da desteklenir
python main.py --preflight-feed

# Yeni içerik üretmeden unresolved publication lifecycle durumunu uzlaştırır
python3 main.py --reconcile-publications

# Yalnız v2 state/receipt GET ile unresolved state sayısını gösterir
python3 main.py --preview-publication-reconciliation

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

<a id="configuration"></a>

## Ortam değişkenleri

| Variable | Gerekli mi? | Amaç |
| --- | --- | --- |
| `INSTAGRAM_ACCOUNT_ID` | Production publish için gerekli | Instagram Business/Creator account ID |
| `INSTAGRAM_ACCESS_TOKEN` | Production publish ve Insights için gerekli | Meta Graph API erişim token’ı; feed/Reel workflow'ları bunu `INSTAGRAM_PUBLICATION_ACCESS_TOKEN`, Insights workflow'u ise `INSTAGRAM_INSIGHTS_ACCESS_TOKEN` repository secret'ından alır |
| `INSTAGRAM_GRAPH_API_VERSION` | Opsiyonel | Ortak Graph API sürümünü override eder (varsayılan `v22.0`) |
| `ARTFOLIO_RIGHTS_POLICY` | Production publish için gerekli | Production'da yalnız `strict_public_domain`; production dışı çağrılarda env yoksa geriye uyumlu `permissive` default |
| `ARTFOLIO_REELS_ROOT` | Opsiyonel | Varsayılan sibling konumunda değilse Artfolio Reels repo yolu |
| `CLOUDFLARE_R2_ACCOUNT_ID` | Production state ve media için gerekli | R2 account ID |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | Media için gerekli | Media bucket access key |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | Media için gerekli | Media bucket secret key |
| `CLOUDFLARE_R2_BUCKET_NAME` | Media için gerekli | Expiring media bucket |
| `CLOUDFLARE_STATE_R2_BUCKET_NAME` | Production için gerekli | Ayrı private, silme lifecycle kuralı olmayan durable-state bucket |
| `CLOUDFLARE_STATE_R2_ACCESS_KEY_ID` | Production için gerekli | State bucket'a scoped access key; media key'den farklı |
| `CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY` | Production için gerekli | State bucket'a scoped secret key |
| `CLOUDFLARE_R2_CONTROL_PLANE_ACCESS_KEY_ID` / `CLOUDFLARE_R2_CONTROL_PLANE_SECRET_ACCESS_KEY` | Yalnız `control-plane-s3` bootstrap audit seçeneğinde gerekli | Ayrı bucket lifecycle okuma credential'ı; runtime workflow'larına verilmez |
| `CLOUDFLARE_R2_PUBLIC_URL` | R2 media upload kullanılıyorsa gerekli | Instagram’ın erişeceği R2 public URL tabanı |
| `GOOGLE_GEMINI_API_KEY` | Yeni automatic paket için gerekli | Gemini üretimi; yeni automatic paketlerde zorunlu, legacy modda fallback kullanılabilir |
| `ARTFOLIO_GEMINI_ENABLED` | Feed Actions için açık opt-in | Repository variable tam `true` olduğunda Feed yayınlama/hazırlama adımları Gemini secret'ını alır. Eksik veya `false` olduğunda yeni automatic paket üretimi durur; legacy modda fallback kullanılır ve secret silinmez. Yerel CLI kendi environment ayarlarını kullanır. |
| `SMITHSONIAN_API_KEY` | Opsiyonel | Smithsonian Open Access adapter'ını etkinleştirir |
| `EUROPEANA_API_KEY` | Opsiyonel | Europeana adapter'ını etkinleştirir |
| `ARTFOLIO_SELECTION_SEED` | Opsiyonel | Seçim RNG’si için açık seed |
| `GITHUB_RUN_ID` | GitHub tarafından otomatik | Açık seed yoksa GitHub koşusunun seçim seed’i |

Gerçek R2 conditional-write doğrulaması normal testlerden ayrıdır ve credentials
varlığıyla kendiliğinden çalışmaz. Mimari, güvenlik sınırları ve açık opt-in
komutu için [Cloudflare R2 concurrency verification](r2-integration-verification.md)
belgesine bakın.

Instagram publish ile ayrı durable R2 safety state ve receipt ledger zorunludur. Temporary media cleanup yalnız media bucket'ın uygulamaya ait `images/publications/<id>/` veya `reels/publications/<id>/` anahtarlarına erişir. `CLOUDFLARE_R2_PUBLIC_URL`, carousel media upload akışında gerekir.

Production startup’ta Instagram, media R2 ve ayrı state R2 değişkenleri **required** kabul edilir; eksik adlar secret değerleri yazdırılmadan tek tanıda raporlanır. State ve media bucket/credential değerleri aynı olamaz. Gemini legacy modda optional, yeni automatic paket üretiminde zorunludur; Rijksmuseum Data Services adapter’ı anahtarsız çalışır.

Recovery bootstrap, forensic artifact dizininden yalnız yerel candidate üretir:

```bash
python3 scripts/bootstrap_publication_recovery.py --evidence-dir /path/to/carousel-history-recovery --output-dir /private/tmp/artfolio-recovery-candidate
```

Varsayılan mod R2'ye yazmaz. `--output-dir` yalnız yerel dosya üretir. Üretim yüklemesi ayrı yetki, `--write --target-bucket <durable-state-bucket> --confirm-production-write I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP` ve ayrıca `--lifecycle-audit-source` gerektirir; bu komut normal geliştirme/CI doğrulamasında çalıştırılmaz. `dashboard` seçeneğinde operatör hedef bucket'ın lifecycle kurallarını Cloudflare Dashboard'da denetleyip nesne silme/expiration kuralı bulunmadığını doğruladıktan sonra `--confirm-dashboard-lifecycle I_VERIFIED_NO_OBJECT_EXPIRATION_FOR_<durable-state-bucket>` verir. `control-plane-s3` seçeneğinde yalnız ayrı `CLOUDFLARE_R2_CONTROL_PLANE_*` credential'ları kullanılarak lifecycle API'si okunur; scoped state key'i bu audit için kullanılmaz. Default 7 günlük incomplete-multipart-abort kuralı kabul edilir; nesne expiration kuralı bootstrap'ı engeller. Audit ayrıca sonraki operasyonlarda tekrarlanmalıdır: nesnelerde `x-amz-expiration` görülmemesi, bucket kuralının güvenli olduğunu tek başına kanıtlamaz. Bootstrap iki nesnenin de yokluğunu doğrular, receipt nesnesini önce, safety nesnesini en son create-only koşuluyla oluşturur. Herhangi bir belirsiz veya kısmi sonuçta otomatik tekrar denenmez; operatör iki nesneyi okuyup durumu incelemelidir. Kaynak forensic JSON, eski `data/posted_history.json` veya örnek şemalar doğrudan production'a yüklenmez.

İlk v2 recovery rollout sırası (tarihsel): feed ve Reel schedule değişkenlerini kapalı tut; feed ve Reel GitHub workflow'larını devre dışı bırakıp queued/in-progress job kalmadığını doğrula. Schedule değişkeni manuel dispatch'i engellemez. GitHub rerun'ları ilk koşunun commit SHA'sını kullandığı için eski workflow'lara verilen `INSTAGRAM_ACCESS_TOKEN` repository secret'ını bootstrap öncesinde kaldır; önce v2 feed/Reel için `INSTAGRAM_ACCESS_TOKEN_V2` ve Insights için `INSTAGRAM_INSIGHTS_ACCESS_TOKEN` repository secret'larını güvenli şekilde hazırla. Eski token'ı almış job kalmadığını yeniden doğrula. Ayrı private durable-state bucket'ı silme lifecycle kuralı olmadan ve ayrı scoped credential'larla hazırla, v2 kodu `main` üzerine ayrı yetkiyle merge et, forensic bootstrap için ayrı yetki al, iki nesneyi doğrula, read-only preflight ve izole R2 testlerini tamamla. Yalnız bundan sonra workflow'ları yeniden açmak ve schedule/canary için ayrı operatör kararı verilir. Eski SHA rerun'ları eski secret yokken Instagram'a yazamaz; kısmi dağıtımda yeni kod eksik v2 state ile durur. Stale v2 PENDING lock yalnız açık reconciliation/expiry CAS ile kaldırılır ve v2 feed worker'ı reservation ID ile fence edilir.

Güncel publication namespace cutover tamamlanmadan `INSTAGRAM_ACCESS_TOKEN_V2` secret binding'i kaldırılmaz. Cutover sonrasında Feed/Reel yalnız `INSTAGRAM_PUBLICATION_ACCESS_TOKEN` kullanır; Insights ayrı `INSTAGRAM_INSIGHTS_ACCESS_TOKEN` kullanmaya devam eder. Eski workflow snapshot'larının yeniden çalıştırılması, eski secret binding'i kaldırılarak credential seviyesinde engellenir.

Normal outbound isteklerin tümü bounded timeout kullanır. R2 connect/read sınırları 10/30 saniyedir; SDK-level retry kapalıdır. Media upload yalnız transient network, rate-limit ve 5xx hatalarında en fazla üç loglanan uygulama denemesi yapar; permanent 4xx/config hataları hemen durur. Gemini isteği 60 saniye ve tek attempt ile sınırlıdır; legacy modda hata halinde template fallback kullanılır, yeni automatic pakette yayın yapılmaz. Instagram yalnız transient container/status hatalarını üç bounded attempt ile tekrarlar; publish sınırındaki belirsiz sonuç otomatik retry edilmez.

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

## Stage B scheduled Feed hardening

Feed supports explicitly approved continuous operation and legacy operator-reviewed
single-slot permits. Hourly checks (`17 * * * *`) use an explicitly approved
`six_hourly_utc17` policy for the exact main SHA (control schema v2). A receipt-backed
success completed in the first hour anchors the next opportunity to its nominal
slot +6 hours. Each new six-hour admission window lasts until the next slot;
delayed starts can still publish once. Manual/unlinked or later completions use
completion +6 hours to prevent catch-up bursts. Historical one-hour permits retain
their deadline derivation. Legacy schema v1 approvals keep their 48-hour rule.
Safe continuous outcomes keep scheduling enabled. Clean definitive failures retain
a minimum one-hour backoff for six-hour permits (24 hours for legacy permits);
the next attempt still requires a new regular slot. Ambiguous, incomplete or dirty
state blocks publication.
Waiting checks skip expensive validation and publishing. Manual Feed authorization
and Reel behavior remain compatible; missing Stage B control still fails closed.

Use `python scripts/manage_feed_schedule.py status --expected-sha EXACT_MAIN_SHA`
for a read-only status showing the last success, next opportunity and current reason.

See [Stage B control and operator procedures](STAGE-B-SCHEDULE.md) for the CAS
operator tool, cadence approval, migration and emergency stop.
Turning the scheduler variable off does **not** stop a job already running. No production
migration, scheduler enablement, workflow enablement or Instagram publication is
authorized by this change.

## Artfolio 2.0 yerel editoryal önizleme

`src/editorial_v2.py`, üretim tema kimliğinden ayrı `public_title`,
`editorial_angle` ve `headline_evidence` taslakları oluşturur. Bilinmeyen eser,
doğrulanamayan müze alıntısı, düşük güven, başarısız görsel kontrol, jenerik ve
son 100 başlığa benzer öneriler elenir. Görsel ve sanat tarihi doğruluğu insan incelemesi
gerektirir; modelin kendi değerlendirmesi doğruluk sertifikası değildir.

Üç Pillow kapak düzeni `src/editorial_design.py` içinde bulunur: Museum Journal,
Artwork First ve Detail Study. Odak kutusu yoksa Detail Study, **FULL ARTWORK**
etiketli tam eser alternatifi üretir. Yazı sığmazsa okunabilirliği azaltarak
devam etmek yerine hata verir.

Gemini kapalıyken gerçek yakın planları karşılaştırmak için `--detail-focus`
ile JSON odak dosyası verilebilir: `{"cleveland_123": [0.2, 0.1, 0.7, 0.6]}`.
Koordinatlar tam görsel üzerinde normalize edilmiş sol/üst/sağ/alt değerlerdir;
kimlik, sınırlar ve alan doğrulanır. Bu kırpmalar `preview_focus` olarak kaydedilir,
AI seçimi veya başlık kanıtı olarak kullanılmaz. Odak dosyası olmadan tam eser
alternatifi korunur.
`--detail-focus`, görselleri önceden inceleyebilmek için `--manifest` gerektirir;
yeni eser indirme seçeneğiyle birlikte kullanılamaz.

30 kullanım hakkı doğrulanmış Cleveland eseriyle 90 kapaklık yerel galeri:

```bash
python scripts/preview_editorial_v2.py \
  --acquire-cleveland data/editorial-sources \
  --output data/editorial-gallery --count 30
```

Var olan indirilen görsellerle tekrar üretim için `--acquire-cleveland` yerine
`--manifest data/editorial-sources/sources.json` kullanılır. Galeri dizini boş
olmalıdır. Manifest `artworks` listesinde `artwork` (NormalizedArtwork metadata)
ve `image_path` içerir; görseller manifest dizini içinde kalmalıdır. Çıktılar
`index.html`, `report.json` ve 1080×1350 JPEG kapaklardır. Müze kaynağı, hak
durumu, AI/fallback sayısı, kontrol gerekçeleri ve kırpma temeli kaydedilir.

Gemini varsayılan olarak **kapalıdır**; kaynak eser başlığı açıkça factual
fallback olarak gösterilir. Ayrı bir canlı doğrulama için açık `--use-gemini`,
pozitif `--max-ai-calls` (en fazla 50) ve `GOOGLE_GEMINI_API_KEY` gerekir.
Önizleme modeli `gemini-3.8-flash`, thinking Medium; istemci 60 saniye zaman
sınırı ve tek deneme kullanır. Görsel/metadata/model/protokol değişince yerel
önbellek anahtarı değişir. `--history` isteğe bağlı JSON başlık listesi alır;
üretim geçmişini okumaz veya yazmaz. Bu CLI Instagram/R2 yayını yapmaz.

Kapak/başlık prototipi, aşağıdaki kalıcı hikâye sistemiyle birlikte kullanılabilir.
Canlı editoryal model doğrulaması ve tasarım performansından öğrenme ayrı teslimatlardır.


## Artfolio yerel hikâye sistemi

`StoryPlan`, benzersiz eserleri ve sıralı sayfaları ayrı tutar. Tek eser incelemesi
(1 eser), karşılaştırma (2 eser) ve seçki (3–8 eser) desteklenir. Kapak, tam eser,
gerçek detay, karşılaştırma, kaynak alıntısı ve kapanış sayfaları 1080×1350 üretilir.
Kaynak açıklaması varsa müze notu yalnızca birebir alıntı içerir; açıklama yoksa
bu sayfa eklenmez. Başlıklar canlı AI kullanılmadan kaynak metadata'sından gelir.

```bash
python scripts/preview_story.py create \
  --manifest data/editorial-sources/sources.json \
  --output data/story-one --narrative single_study \
  --artwork-ids cleveland_123 \
  --detail-focus data/detail-focus.json

python scripts/preview_story.py serve --project data/story-one --port 38129
```

`--artwork-ids` manifestteki gerçek kimliklerle doldurulur; verilmezse ilk 1/2/5
eser seçilir. `--detail-focus` isteğe bağlıdır; önceki önizlemeyle aynı JSON biçimini
kullanır. Verilmezse yerel kenar/doku ölçümleri en fazla 3 kırpma önerir. Bunlar
`local_geometry` olarak işaretlenir ve görsel inceleme gerektirir; nesne tanıma veya
sanat tarihi kanıtı değildir. Uygun çözünürlük/doku yoksa detay eklenmez. Detail Study
kapağı mevcut gerçek detaydan üretilir; detay yoksa tam eser alternatifi kaydedilir.
`--history` son 100 başlığın JSON listesini, `--style-history` geçmiş kapak stillerini
alır. Üretim geçmişi okunmaz veya değiştirilmez.

Yerel editörde kapak başlığı/stili, sayfa metni, sırası ve detay koordinatları
kaydedilebilir. Kaynaklar sabittir. Hak, görsel dosyası, çözünürlük, yinelenen kırpma,
alıntı, başlık tekrarı veya okunabilir metin kapasitesi kontrolü başarısızsa kayıt
reddedilir ve son geçerli taslak korunur. Kaydetme, sürüm kontrolü ve süreçler arası
kilitle korunur; tam sürüm tek atomik işaretçiyle değiştirilir. HTTP sunucusu yalnızca
127.0.0.1 üzerinde çalışır ve yazma işlemleri origin/host/oturum kontrolünden geçer.

Detay sayfalarında özgün eser önizlemesi üzerinde fare veya dokunmayla yeni bir
alan seçilebilir. Seçim çerçevesi, dört sayısal koordinat ve kırpılmış önizleme
birlikte güncellenir; koordinatlar klavyeyle de düzenlenebilir. İptal edilen veya
geçersiz alanlı sürükleme önceki seçimi korur. Detay alanı eserin %8–%85’ini
kapsamalıdır; sınırlar önizleme ve sunucuda aynı kaynaktan alınır. Geçersiz
koordinatlar önizlemede işaretlenir ve kayıt engellenir; çözünürlük ve içerik kontrolleri kayıt sırasında
yeniden uygulanır. Görsel önizlemeler yönü düzeltilmiş küçük JPEG'ler olarak
HTML'e gömülür; TIFF kaynaklar da çevrimdışı görüntülenir, özgün dosyalar değişmez.
Statik galeride alan denenebilir; kalıcı kayıt için yerel editör sunucusu gerekir.
Eski projelerde yeni kontrolleri görmek için `render --project ...` çalıştırın;
bu işlem yeni sürüm oluşturur ve varsa önceki editoryal onayı geçersiz kılar.

Proje `sources/`, `pages/`, `project.json`, `report.json`, `index.html` ve geçmiş
sürümler için `.revisions/` içerir. Son üç dosya `current` üzerinden aynı sürüme
bağlıdır; taşırken sembolik bağlantıları koruyun. Görsel, kaynak metadata'sı, font,
çizim kodu veya sayfa değişince önbellek geçersizleşir. Yalnızca değişen sayfalar
tekrar çizilir; `render --project ...` mevcut planı yeniden doğrular ve kaydeder.
Bozuk önbellek görselleri yeniden üretilir. İlk üretim geçici dizinde hazırlanır;
başarısızsa düzeltilmiş planla yeniden denenebilir. Dolu dizinin üzerine yazılmaz.

Paket benzersiz `artwork_ids` ile sıralı sayfa varlıklarını ayrı taşır. Yerel taslak
her zaman `publication_ready=false` olarak kalır; editoryal onay ayrı bir kayıtla
incelenen proje sürümüne, kaynak metadata/görsellerine ve sıralı sayfa byte'larına
bağlanır. Düzenleme veya dosya değişikliği bu onayı geçersiz kılar.

### Eski v1 hikâyeyi uyumluluk kuyruğuna hazırlama

```bash
# İncelenen project.json revision değerini kullanın.
python scripts/preview_story.py approve --project data/story-one --expected-revision 3
# Taşınabilir JPEG'ler ve kaynak/sayfa eşlemesi; yayın veya R2 yazısı yapmaz.
python scripts/preview_story.py export --project data/story-one --output data/story-one-export
# Hikâye sonraki carousel slotunu doldurur; diğer slotlar mevcut hazırlama yolunu kullanır.
python scripts/prepare_feed_queue.py --directory data/reviewed-feed --first-format carousel \
  --story-project data/story-one --target 3 --content-mode legacy
```

`--story-project` tekrarlanabilir; carousel slot sayısını aşamaz ve aynı eser iki
pakette kullanılamaz. Normal hazırlama, sonraki hikâyeler için ayrılmış eserleri de
hariç tutar. Bütün hikâyeler acquisition öncesi doğrulanır. Bu seçenek status/refill
modlarında kullanılamaz. Diğer slotların hazırlanması mevcut müze/Gemini akışını
çalıştırabilir; hikâye onayı ve export hiçbir model isteği yapmaz.

Hikâye açık `content_kind=story` ve `artfolio-story-delivery-v1` sözleşmesi taşır:
1–8 benzersiz kaynak, 3–10 sıralı JPEG sayfası. Yerel taslakta 11–12 sayfa varsa
export reddedilir; sayfalar otomatik kesilmez. Legacy carousel'in 6–9 ayrı eser
sözleşmesi korunur. Tek eser incelemesi de **carousel gönderisidir**; tekli/carousel
dönüşümü değişmez. Kapak, detay ve tam eser aynı kaynağa referans verebilir.

Mevcut private R2 install/claim yolları hikâye türünü ve sayfa hash/sırasını korur;
fresh source-rights, TTL, kaynak duplicate ve ownership kontrolleri uygulanır.
Üretim runner'ı rezervasyon öncesi doğrulanmış özel medya kopyaları alır. Her kaynak
bir kez kilitlenir; yayın sınırında sayfa container sayısı ayrıca doğrulanır.
Reservation'dan itibaren story delivery değiştirilemez. Belirsiz sonuçta otomatik
tekrar olmaz. Onay/export, production izni değildir: mevcut exact-SHA onayı,
cooldown, safety CAS, receipt ve preflight kapıları yayın için yine zorunludur.

Publication ve receipt, benzersiz kaynak üyeliğini ve ayrı sayfa eşlemesini korur.
Insights bir parent gönderi için bir ölçüm alır; detay sayfaları örnek sayısını
çoğaltmaz. `report_feed_analytics.py --json` içindeki `story_cohorts`, aynı yaş
pencerelerinde anlatı/kapak/başlık kaynağına göre betimleyici sonuçları ve ölçüm
kapsamını gösterir; otomatik kazanan ilan etmez. Öğrenme bağlamı bu özellikleri ve
sayfa sayısını taşır. Canlı model kalitesi ve gerçek story yayın başarısı yerel
testlerden çıkarılamaz; canlı rollout ayrı operasyonel doğrulama gerektirir.

### Salt okunur Feed durum panosu

Her çalıştırmada yeni bir HTML çıktı yolu seçin; var olan çıktıların üzerine yazılmaz.
R2 modu mevcut çağıran ortam ayarlarını kullanır, açık SHA ister ve yalnızca okuma
çağrıları yapar:

```bash
python scripts/report_feed_dashboard.py --r2 --expected-sha "$GITHUB_SHA" \
  --output /tmp/feed-dashboard-r2.html
```

Çevrimdışı kullanımda schedule, history ve snapshots JSON dosyalarını verin. Kuyruk
için yerel `queue.json` dizini veya önceden alınmış, bounded queue report JSON'u
kullanılabilir; hikâye projesi her onay için tekrarlanabilir. `--now` zaman dilimi
taşımalıdır ve rapor zamanını sabitler:

```bash
python scripts/report_feed_dashboard.py --schedule schedule.json --history history.json \
  --snapshots snapshots.json --queue-report queue-report.json \
  --story-project data/story-one --now 2026-10-11T12:00:00+03:00 \
  --output /tmp/feed-dashboard-local.html
```

Pano bir anlık görüntüdür. En erken uygunluk zamanı yayın zamanı garantisi vermez;
Insights grupları gözlemseldir ve küçük örneklemler ayrıca işaretlenir.

<a id="dashboard"></a>

### Güncellenebilir yerel panel ve içerik takvimi

Panel ölçüm güncelliği ile ölçüm kapsamını ayrı gösterir. Son kayıt güncel olsa
bile geçmişte kaçırılan veya kullanılamayan pencere varsa kapsam eksik görünür.
Vadesi gelen/kısmi ölçümler, henüz zamanı gelmeyen pencereler ve tamamlanan
ölçümler ayrılır. Kapsam tüm kayıtlı yayınları içerir; bu gösterge bildirim
göndermez ve yayın iznini değiştirmez.

Paneldeki **Bu içerik neden seçildi?** bölümü, doğrulanmış yayınların ve hazır
paketlerin kayıtlı kalite/editoryal, öğrenme, çeşitlilik ve keşif katkılarını gösterir.
Kaydedilmemiş puan yeniden hesaplanmaz. Hazır paket başarılı yayın sayılmaz.
**Deney ilerlemesi**, aynı tema/hedef yaş/sabit karşı faktörde her kolun gözlem,
eksik ölçüm ve minimum örnekleme ulaşmak için gereken ek gözlem sayısını gösterir.
Atama farkı görünürdür; ekran yeni atama yapmaz, süre veya kazanan tahmin etmez.

`--feasibility data/theme_feasibility.json` ile isteğe bağlı mevcut yerel gözlem
dosyası eklenebilir. Kaynak raporu yalnız saklanan tema denemelerini kapsar:
toplam metadata hunisi ve müze bazında katılım/hata gözlemleri ayrı gösterilir.
Müze başına indirme verimi bu kayıtlardan çıkarılamaz. Bu seçenek yeni müze/R2
çağrısı başlatmaz; dosya sunucu yenilemesinde yeniden okunur.

Aynı girdi seçenekleriyle salt okunur yerel sunucuyu açın:

```bash
python scripts/serve_feed_dashboard.py --r2 --expected-sha EXACT_APPROVED_MAIN_SHA --port 38143
# Veya ağ/Keychain erişimi olmadan yerel JSON/queue dosyalarıyla:
python scripts/serve_feed_dashboard.py --schedule schedule.json --history history.json \
  --snapshots snapshots.json --queue /path/feed-queue --port 38143
```

Tarayıcıda `http://127.0.0.1:38143/` adresini açıp **Verileri yenile** düğmesini
kullanın. Sunucu yalnız loopback üzerinde dinler; yenileme origin/host/oturum
kontrolünden geçer ve en sık 5 saniyede bir yapılır. Hata olursa mevcut rapor
korunur ve yeni deneme için açıklama gösterilir. R2 erişim anahtarları tarayıcıya
aktarılmaz; panel queue, publication, receipt veya analytics verisi yazmaz.
R2 ortam ayarları çağıran süreçte açıkça bulunmalıdır; otomatik profil fallback'i yoktur.

Takvim, manifest sırasını ve tekli/carousel dönüşümünü korur. Görseller ilk paket
JPEG'inin digest kontrolünden geçirilmiş küçük, gömülü önizlemeleridir; kaynaklar
ve özel R2 bağlantıları sunulmaz. Görsel yoksa yer tutucu gösterilir. Tarihler,
önceki paketlerin belirtilen alt sınırda başarıyla tamamlandığı varsayımına bağlıdır;
yayın saati garantisi vermez. Duraklatma/sürüm uyuşmazlığı, belirsiz sahiplik,
karantina, format veya son kullanma sorunu olan paketlere tarih atanmaz.

Hikâye tablosu gerçek kayıtlı anlatım/kapak gruplarını aynı hedef yaşta karşılaştırır.
Her parent bir gözlemdir; sayfalar bağımsız örnek sayılmaz. Kapsama, gözlem sayısı,
kaydetme/beğeni oranı ve yetersiz örneklem açıkça gösterilir. Eksik metrik sıfır
olarak doldurulmaz ve otomatik kazanan seçilmez. Statik HTML çıktısı çalışmaya
devam eder; yenileme düğmesi yalnız yerel sunucu üzerinden etkinleşir.

### Kuyruk tükenmeden erken stok hazırlama

Legacy modda stok hazırlama `--check-refill --early` ve `--refill --early` kullanır.
Normal editoryal mod yeni onaylı girdi yokken yalnız inceleme ihtiyacını bildirir.
En fazla iki geçerli READY paket kaldığında üç yeni paket hazırlanır; üç veya
fazla READY varken hazırlama yapılmaz. Mevcut tüm kaynak kimlikleri dışlanır,
format sırası hazır kuyruğun sonundan devam eder. Yeni parti oluşturulurken
exact-SHA üretim onayı, mevcut sıradaki format ve manifest ETag yeniden doğrulanır.

Eski manifest önce değişmez arşive alınır. Geçerli READY kayıtları byte düzeyinde
korunarak yeni üç paket sonlarına eklenir; toplam en fazla beş paket olur.
CLAIMED, belirsiz yayın sonucu, bozuk kayıt veya eşzamanlı manifest değişikliği
sessizce onarılmaz. Koşullu yazma çatışması/belirsizliği tekrar denenmez. Hazırlama
yayın yetkisi vermez; tüketimdeki güncel hak, geçmiş, TTL vecadence politikasının yayın aralığı kontrolleri sürer.
Eski `--refill` davranışı `--early` olmadan yalnız tükenmiş/eskimiş partiyi değiştirir.

### Haftalık otomatik rapor

Yayın sayıları ve format ölçümleri son yedi günü kapsar. Kapak/açıklama deneyleri
ise rapor zamanına kadar doğrulanmış tüm kayıtlı yayınlardan birikir; haftalık
pencere deney örneklerini sıfırlamaz. Tema, hedef yaş ve sabit karşı faktör ayrı
tutulur; her kolda en az beş gözlem gerekir ve otomatik kazanan seçilmez.

`Weekly Feed report` her pazartesi06:23UTC (09:23Türkiye) salt okunur rapor üretir.
Son yedi günün `[başlangıç,bitiş)` yayınları, aynı hedef yaştaki gerçek metrikler,
ölçüm kapsamı ve mevcut kuyruk/sistem durumu ayrı gösterilir. Story parent'ı bir
örnektir; eksik veri sıfır değildir. Tam geçmişteki kimlik çakışmaları hafta
filtrelenmeden elenir. Yeni yayınların henüz vadesi gelmeyen ölçümleri beklemededir.

```bash
python scripts/report_feed_weekly.py --r2 --expected-sha EXACT_APPROVED_MAIN_SHA --output /tmp/weekly-feed/report.md
# Çevrimdışı: --r2 yerine --schedule schedule.json --history history.json --snapshots snapshots.json
```

Komut mevcut girdilerin veya çıktının üzerine yazmadan Markdown ve aynı isimli
JSON dosyası oluşturur; görsel indirmez, Instagram/Gemini çağırmaz, R2 verisi
mutasyona uğratmaz. Rapor Actions özetine eklenir ve30gün indirilebilir artifact
olarak tutulur; harici mesaj/bildirim kanalı açılmaz. Artifact saklama sözleşmesi:
[GitHub workflow artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data).

### Yayın ve hazır stok arasında çeşitlilik

`ARTFOLIO_FEED_VARIETY_ENABLED=true`, yalnız seçim puanında sınırlı tekrar baskısını
açar. Gerçek yayınlar ve kuyrukta bekleyen/aynı partide hazırlanan içeriklerden
oluşan son12 grup birlikte değerlendirilir. Sanatçı4, müze2, dönem1, renk1 puan/grup; toplam etki en
fazla−12'dir. Her carousel/story kaynak grubu bir kez sayılır; çok sayfalı içerik
etkiyi çoğaltmaz. Bilinmeyen metadata tarafsızdır; yeni renk verisi yalnız mevcut
indirilmiş görselden çıkarılır, ek indirme/AI yoktur.

Tekli ve carousel adayları bu bağlamı kullanır; ham kalite, tema uygunluğu, hak ve
minimum kabul eşikleri değişmez. Sonuç `feed_variety_adjustment` alanında görünür.
Hazır içerik yalnız seçim bağlamıdır, başarılı yayın geçmişine yazılmaz. Daha önce
mühürlenmiş paket/inceleme onayı değiştirilmez. Bayrak kapalıyken önceki sıralama
korunur; üretim aktivasyonu yalnız onaylanan yeni main sürümüyle yapılır.

### Tek değişkenli kapak ve açıklama deneyleri

Yeni hazırlanan legacy carousel için `ARTFOLIO_CAPTION_EXPERIMENT_ENABLED=true`
mevcut iki açıklama girişini (`question`, `visual_detail`) sabit `editorial` kapakla
dener. `ARTFOLIO_COVER_EXPERIMENT_ENABLED=true` aynı kaynak/crop/yazı düzeni üzerinde
`editorial` alt gradyanı ile `editorial_panel` koyu metin panelini karşılaştırır;
kapak deneyinde açıklama her iki kolda aynı factual `visual_detail` girişini kullanır.
İki bayrak açıkken seed bir aile seçer: tek gönderide iki değişken birlikte değişmez.
Atama deterministiktir; kendiliğinden kazanan seçimi veya modele terfi yapılmaz.

Tipli `controlled_cover_experiment` ve mevcut `controlled_experiment` birbirini
dışlar; atama gerçek tema, render edilen kapak ve teslim edilen açıklamayla
uyuşmalıdır. İncelenmiş story'lere uygulanmaz ve mühürlenmiş hazır paketler sonradan
farklı bir kola geçirilmez. Gemini kapalı çalışır; deney için AI çağrısı gerekmez.

`report_feed_analytics.py --experiments` eski `comparisons` açıklama alanını korur;
kapak kolları ayrı `cover_comparisons` içindedir. Haftalık rapor da bu iki aileyi
ayrı sunar. Karşılaştırmalar tema, aynı hedef yaş ve sabit karşı faktöre göre ayrılır;
her metrik için kol başına gözlem/kapsama gösterilir, eksik metrik sıfır yapılmaz.
Minimum beş gözlem yoksa kanıt yetersizdir; farklı eserler/zaman/kitle nedensel
bir A/B sonucu çıkarmayı sınırlar. Üretimde aktivasyon yalnız doğrulanmış yeni
exact-main onayıyla ve açık bayraklarla yapılır.
