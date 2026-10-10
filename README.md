# Telegram indirim takipçisi

Kişisel Telegram hesabının üye olduğu kanalları dinler, filtreden geçen mesajları
istediğin sohbete iletir. Polling yapmaz: bağlantıyı açık tutar, mesaj gelir gelmez işler.
Dahili/harici kelime ve takip edilen grup listelerini Telegram'dan yönetebilirsin
([5. bölüm](#5-ayarları-telegramdan-düzenleme)); diğer teknik ayarlar `config.json` üzerinden yapılır.

---

## Yeni: önce özel sohbetten yönetim, sonra isteğe bağlı kişisel fırsatlar

Bu depoda `private_control: true`: **komutları gruba değil, mevcut bildirim botunun
özel sohbetine yaz.** Fırsatların hedefi (`destination`), kaynaklar, grup filtreleri,
iletim zinciri ve tekrar birleştirme ayarları değişmedi. `control_chat` eski değeriyle
korunur ama bu modda gruptan komut işlenmez. **Kayıtlı Mesajlar** yedek kontrol yoludur.
Eski kurulumlarda yeni alan yoksa önceki grup kontrol davranışı korunur.

### 1. aşama — yalnızca yönetimi taşı

1. Bu değişikliği birleştir ve takipçiyi yeni sürümle yeniden başlat. Aynı bot token'ını
   kullanan eski çalışmayı kapat; tek bir `getUpdates` tüketicisi çalışmalı.
2. Mevcut `NOTIFY_BOT_TOKEN` secret'ının tanımlı olduğundan emin ol; yeni bot veya yeni
   token gerekmez. Botun gruptaki üyeliğini/yetkilerini değiştirme.
3. Botun özel sohbetini aç. Takipçi hazır olduktan sonra `/start`, ardından `/durum` gönder.
4. `/ayar`, `/ekle`, `/çıkar`, `/kaydet`, `/iptal`, `/open`, `/close`, `/kaynaklar`,
   `/kaynaktest`, `/analiz`, `/restart` aynı işlevleri özel sohbetten yerine getirir.
   `/test` denemeyi hâlâ **hedef gruba** gönderir; sonucu özelden yanıtlar.
5. Botla özel sohbet geçmişi silinmez. Eski `clean_commands` davranışı yalnızca
   kullanıcı hesabının kontrol sohbetlerinde (ör. Kayıtlı Mesajlar) geçerlidir.

Komutları sadece hesap sahibi ve `admin_user_id` listesindeki kişiler kullanabilir.
Bot, yetkisiz kişilerin mesajlarını ve gruptan gelen Bot API mesajlarını yok sayar.
Bot API long polling ayrı bir arka plan görevidir; kaynak kanallar hâlâ aynı Telethon
bağlantısıyla dinlenir. Açılıştan önce bekleyen bot komutları güvenlik için atlanır:
cevap gelmediyse bot hazır olduktan sonra tekrar `/start` gönder.

### 2. aşama — kişisel fırsatları istediğin zaman aç

**Başlangıçta kapalıdır:** `dm_enabled: false`, `dm_keywords: []`. İlk aşamayı denedikten
sonra hesap sahibi botun özel sohbetinde şu diyaloğu başlatabilir:

```text
Sen: /dmfiltre
Bot: Kişisel bildirim KAPALI. Kelimeler: (boş). Gönderim/hata sayaçları...
Sen: /dmfiltreekle
Bot: Eklenecek kelimeleri virgülle ayırarak yaz.
Sen: tcl, lg, iphone
Bot: Kaydedildi ve anında uygulandı. Grup ayarları değişmedi.
Sen: /dmfiltrecikar
Bot: Çıkarılacak kelimeleri virgülle ayırarak yaz.
Sen: lg
Bot: Kaydedildi. Kalan kelimeler: tcl, iphone.
```

Kısayollar: `/dmfiltreekle tcl, lg, iphone`, `/dmfiltrecikar lg`. Ekleme mevcut
listeyi **koruyarak** yeni kelimeleri ekler; çıkarma yalnızca yazılan tam kelime/ifadeyi
çıkarır. Büyük/küçük harf farkı yoktur. Aynı kelime ikinci kez eklenmez. Çıkarılacak
kelimelerden biri listede yoksa hiçbir kayıt değiştirilmez; yazımı düzeltip yeniden dene.
Yeni kelime eklemek kişisel bildirimleri açar. Çıkarma, kapalı filtreyi açmaz;
son kelime çıkarılırsa filtre otomatik kapanır. Zaten mevcut kelimeleri eklemek ayarı
ve açık/kapalı durumunu değiştirmez.

Bu işlemler normal grup liste menülerinden farklı olarak **hemen kaydedilir**;
ayrıca `/kaydet` gerekmez. Değer istenirken `/iptal` vazgeçer. Bekleyen bir ayar taslağı
varsa yeni ekleme/çıkarma başlatmadan önce onu bitir. `/dmfiltre` yalnızca bilgi verir,
bekleyen taslağı değiştirmez ve tek başına kelime istemez.
Ayarlar `config.json`'a ve mevcut GitHub kayıt yöntemiyle depoya yazılır; gönderilen
kayıt raporunu kontrol et. Yerel dosyaya yazma başarısızsa eski ayar geri yüklenir.
GitHub'a gönderim başarısızsa yereldeki ayar geçerlidir ama yeni Actions runner'ına
aktarılmayabilir. **Özel kelimeler gizli değildir; config ve Git geçmişinde görünür.**

| Komut | Etki |
|---|---|
| `/dmfiltre` | Mevcut kelimeler, açık/kapalı durumu, bu oturumun gönderim/hata/kuyruk sayaçları; salt okunur |
| `/dmfiltreekle [kelime, ifade]` | Kelime ekler; argüman yoksa eklenecek kelimeleri sorar |
| `/dmfiltrecikar [kelime, ifade]` | Kelime çıkarır; argüman yoksa çıkarılacak kelimeleri sorar |
| `/dmkapat` | Listeyi koruyarak özel fırsatları kapatır; henüz gönderilmeyen kuyruk kayıtları da atlanır |
| `/dmac` | Kayıtlı listeyle tekrar açar; boş listeyi açmaz |
| `/komutlar` | Her desteklenen komutu ve açıklamasını tek kez listeler |

`/start` yanıtında `/komutlar` hatırlatılır. Yardım listesi başlıksız, maddesiz ve
her satırda tek canonical komut olacak şekilde üretilir.

Bu kişisel ayarlar ve alıcı **yalnızca hesabın sahibine** aittir; başka bir admin
kişisel alıcıyı kendisine çeviremez. Çok kullanıcılı abonelik sistemi değildir.

- Listeden **herhangi biri** eşleşir. Büyük/küçük harf farkı yok; `IPHONE`/`İPHONE`
  eşleşir. Kelime sınırı kullanılır: `lg`, `bilgi` içindeki harflerden eşleşmez.
  Tam model kodu/ifade yazılabilir. En fazla 100 kayıt, her biri 100 karakter.
- Önce mevcut grup filtresi uygulanır. Hariç tutulan, gruba gönderilemeyen veya
  mevcut tekrar birleştirmesiyle yeni grup mesajı oluşturulmayan fırsata özel kopya
  gönderilmez. Bu aşama **gruba yeni gönderilen fırsatların aynasıdır**; geçmiş tarama,
  fiyat düşüşü analizi veya gruptaki rozet düzenlemelerini DM'e eşitleme yapmaz.
- Metin ve biçimlendirme entity'leri, gizli linkler, URL düğmeleri korunur. Medya
  gruptaki iletiden Telegram'ın `copyMessage` yöntemiyle kopyalanır; tekrar indirilmez.
  Sonuna yalnızca **`🎯 SANA ÖZEL`** eklenir. Bu sabit metin iPhone Kestirmeler filtresi
  için kullanılabilir; telefon otomasyonu bu PR'ın kapsamında değildir.
- Gruba gönderim/hesap kopyası temizliği tamamlandıktan sonra özel kopya ayrı,
  en fazla 100 kayıtlık kuyruğa alınır. İşçi en fazla saniyede bir gönderim yapar;
  **bu, tekrarlı alarm değildir: bir yeni fırsat = bir özel kopya.**
- Bot engellenirse, özel sohbet başlatılmamışsa veya kopyalama başarısızsa grup
  etkilenmez. Hata `/dmfiltre` sayacına ve loga yazılır. 429 yanıtlarında sınırlı bekleme
  uygulanır. Kuyruk dolarsa özel kopya atlanır ve sayılır; sınırsız bellek büyümez.
- Grup bot mesajı başarısızken kullanıcı hesabının grup kopyası kalmışsa o ileti
  okunarak özel kopya denenir. Telegram'ın kopyalayamadığı içerikler veya ek etiketle
  uzunluk sınırını aşan yedek iletiler **sessizce kesilmez**, özel gönderim hatası sayılır.
- Kuyruk ve sayaçlar bellektedir: yeniden başlatmada henüz gönderilmemiş özel
  kopyalar kaybolabilir. Yapılandırma kalıcıdır. Garantili teslim/kalıcı outbox ve
  tekrarlı alarm bu aşamada eklenmedi.

### Sorun giderme ve geri dönüş

**Önce gruba düşen açılış mesajına bak.** Takipçi artık özel komut kanalını açılış
bildiriminden *önce* başlatır ve gerçek durumu yazar:

- `✅ Özel komutlar hazır: @botadi özel sohbetine yaz` → kanal çalışıyor; o bota yaz.
  Aynı anda bot sana özelden de `🟢 Takipçi başladı; özel komutlar hazır` atar (daha önce
  en az bir kez /start yazdıysan; Telegram botun ilk mesajı atmasına izin vermez).
- `⚠️ Özel komutlar ÇALIŞMIYOR: ... webhook var / token geçersiz (401) / başka bir süreç
  kullanıyor (409)` → nedeni yazılıdır, aşağıdaki ilgili maddeye git. Çalışma sırasında
  bozulursa da aynı uyarı gruba düşer; düzelince `✅ ... toparlandı` gelir.
- `⏳ Özel komutlar henüz hazır değil` → Telegram'a 10 sn içinde ulaşılamadı; biraz bekleyip
  Kayıtlı Mesajlar'dan `/test` yaz, `🔎 Özel komut kanalı` bölümü poller durumunu gösterir.

Takipçi hazır olmadan önce yazdığın komut işlenmez (eski bir `/restart`'ın tekrar
oynamaması için) ama artık sessiz kalmaz: son 15 dakika içinde yazılmışsa bot
`⏳ ... komutu tekrar gönder` diye uyarır.

Botun özel sohbetinde `/start` yanıtlanmıyorsa, sırayla şunları kontrol et (token'ı
asla sohbete, config'e veya loglara yazma):

1. **Secret eksik veya yanlış:** GitHub → Actions → son çalıştırmanın **"Ayarları
   doğrula"** adımındaki raporda `NOTIFY_BOT_TOKEN=YOK` yazıyorsa secret tanımlı değil.
   `Settings → Secrets and variables → Actions` üzerinden ekle/güncelle ve workflow'u
   yeniden başlat. **Bu, en sık görülen nedendir.**
2. **Botta webhook var:** Logda `Özel komutlar başlatılamadı: bu botta webhook var`
   yazıyorsa botta webhook var; `getUpdates` ile çelişir ve özel komutlar hiç
   başlamaz. Webhook'u kaldır (`https://api.telegram.org/bot<TOKEN>/deleteWebhook`)
   veya webhook kullanan entegrasyonu kapat; tek alıcı olsun. Uygulama webhook'u
   kendisi silmez.
3. **İkinci bir tüketici var:** Logda `hata 409` (çakışma) yazıyorsa aynı token'la
   başka bir `getUpdates` uygulaması (eski runner, yerel servis) çalışıyor; mesajlar
   iki tüketici arasında bölüşülür. Tek örneğe indir.
4. **Token geçersiz:** Logda `hata 401` / `token geçersiz` yazıyorsa token bozuk veya
   yanlış botun token'ı; BotFather'da `/revoke` ile yenisini alıp secret'a koy.
5. **Yanlış bot:** Kayıtlı Mesajlar'dan `/test` yaz; yanıttaki `🔎 Özel komut kanalı`
   bölümünde botun kullanıcı adı (`@...`) görünür. Özel sohbette konuştuğun bot
   **aynı** olmalı.
6. **Grupta yazıyorsan:** Bu kanal grup mesajlarını tasarım gereği yok sayar (yalnızca
   özel sohbet kabul eder). BotFather'daki gizlilik (privacy) modu grupta geçerlidir;
   özel sohbette gerekmez. Komutları botun **özel sohbetine** yaz.
7. **Bot ilk mesajı atamaz:** Telegram kuralı gereği bot sana ilk özel mesajı kendisi
   atamaz. Botun özel sohbetini aç, **/start**'a bas ve botu engellemediğinden emin ol.
   Bot engellenmişse veya hiç başlatılmamışsa ne komut yanıtı ne de kişisel fırsat
   kopyası düşer.
8. **Bekleyen mesajlar atlanır:** Takipçi kapalıyken gönderdiğin komutlar açılışta
   güvenlik için atılır; takipçi hazır olduktan sonra komutu **yeniden** gönder.

BotFather'dan yapman gerekenler: yeni bot açmana gerek yok (mevcut bildirim botu yeterli);
botun kullanıcı adını yukarıdaki `/test` çıktısından doğrula. Eski grup komut düzenine
dönmek için `private_control: false` yapıp yeniden başlat; korunan `control_chat` yeniden
geçerli olur. Özel fırsatları da kapatmak istersen `dm_enabled: false` yap. `destination`
veya grup filtrelerini değiştirme.

---

## İçindekiler

1. [Sıfırdan kurulum](#1-sıfırdan-kurulum-hatırlatma-listesi)
2. [Nasıl çalışır ve sınırları](#2-nasıl-çalışır-ve-sınırları)
3. [Ayarlar: `config.json`](#3-ayarlar-configjson)
4. [Telegram komutları](#4-telegram-komutları)
5. [Ayarları Telegram'dan düzenleme](#5-ayarları-telegramdan-düzenleme)
6. [Bildirim kurulumu](#6-bildirim-kurulumu-telefona-uyarı-gelsin)
7. [İletim zinciri: korumalı kanallar](#7-iletim-zinciri-korumalı-kanallar)
8. [Gizli bağlantılar ve fiyat arama düğmeleri](#8-gizli-bağlantılar-ve-fiyat-arama-düğmeleri)
9. [Tekrar birleştirme](#9-tekrar-birleştirme-aynı-fırsat-tek-mesaj)
10. [ID'leri doğrulama](#10-idleri-doğrulama)
11. [Sorun giderme](#11-sorun-giderme)
12. [Güvenlik](#12-güvenlik)
13. [Geliştirici notları](#13-geliştirici-notları)
14. [Bu güncellemeden sonra yapılacaklar](#14-bu-güncellemeden-sonra-yapılacaklar)

---

## 1. Sıfırdan kurulum (hatırlatma listesi)

Daha önce kurduysan bu bölüm hatırlatma niyetine; toplam 5 adım, ~10 dakika.

### Adım 1 — Telegram API bilgisini al

[my.telegram.org](https://my.telegram.org) → **API development tools** → `API_ID` ve `API_HASH`.
Kaynak kanallara **kişisel hesabınla** katıl (bot hesabı kanalları okuyamaz).

### Adım 2 — Session string üret

Bilgisayar şart değil. Depodaki `session_generator_colab.ipynb` dosyasını GitHub'da açıp
**Open in Colab** ile çalıştır (notebook'u public paylaşma):

1. API ID gir → 2. API Hash gir → 3. Telefonu `+90...` biçiminde gir →
4. Gelen kodu gir → 5. İki aşamalı doğrulama varsa parolayı gir →
6. Çıkan `SESSION_STRING` değerini kopyala.

Python olan bir ortamda alternatif:

```bash
pip install -r requirements.txt
API_ID=... API_HASH=... python generate_session.py
```

> Bu değer Telegram hesabının giriş anahtarıdır. Kimseyle paylaşma.

### Adım 3 — GitHub secret'larını ekle

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Secret | Zorunlu | Ne için |
|---|---|---|
| `API_ID` | ✅ | Telegram API kimliği |
| `API_HASH` | ✅ | 32 karakterlik API hash'i |
| `SESSION_STRING` | ✅ | Adım 2'de ürettiğin oturum anahtarı |
| `GH_PAT` | ➖ | Sadece otomatik yenileme zinciri için (aşağıda) |

Biri eksikse workflow açılışta durur ve **Ayarları doğrula** adımı hangi secret'ın boş
olduğunu Türkçe olarak log'a yazar. Aynı kontrolü yerelde de çalıştırabilirsin:

```bash
python bot.py --check
```

**GH_PAT** (opsiyonel): Actions job'u en fazla ~6 saat yaşar. Bu token, job bitmeden yeni
bir çalışma başlatarak zinciri sürer. Fine-grained token oluştururken bu repo için
**Actions: Read and write** iznini ver. Eklemezsen bot yine çalışır, sadece 6 saatte bir
elle (veya schedule ile) başlatman gerekir.

### Adım 4 — `config.json`'ı düzenle

Deponun kökündeki [`config.json`](config.json) dosyasını GitHub'dan düzenle (kalem simgesi):

```json
{
  "source_chats": ["@indirimkanali", "-1001234567890"],
  "destination": -5092968106,
  "include_keywords": ["çay", "kahve", "şeker"],
  "exclude_keywords": ["çekiliş", "hediye"],
  "match_mode": "any",
  "copy_mode": "copy",
  "delivery_modes": ["forward", "copy", "media", "text", "link"],
  "max_media_mb": 25,
  "link_appendix": "smart",
  "message_link": true,
  "source_footer": true,
  "notify_media": true,
  "clean_commands": true,
  "single_message": true,
  "dedup_enabled": true,
  "dedup_window_hours": 12,
  "dedup_scan_limit": 100,
  "control_chat": -5092968106,
  "admin_user_id": 1143378073,
  "auto_restart": true,
  "notify_on_start": true
}
```

Alanların hepsi [3. bölümde](#3-ayarlar-configjson) tek tek anlatılıyor. ID bilmiyorsan
[10. bölüm](#10-idleri-doğrulama)deki `/id` komutunu kullan.

> **ID'ler tırnak içinde de yazılabilir** (`"-5092968106"`); bot sayıya çevirir.

### Adım 5 — Çalıştır ve test et

1. **Actions → Telegram indirim takipçisi → Run workflow → `main`** → çalıştır.
2. **Ayarları doğrula** adımının log'unda yeşil "yapılandırma geçerli ✅" görmelisin.
3. **Mesajları dinle** adımında `Bağlanıldı: ...` ve `Dinleniyor...` satırları gelmeli.
4. Kontrol sohbetine `/durum` yaz → bot yanıt veriyorsa komut yolu çalışıyor.
5. `/test` yaz → hedefe deneme mesajı düşmeli.
6. Telefonuna bildirim gelsin istiyorsan [6. bölümdeki](#6-bildirim-kurulumu-telefona-uyarı-gelsin) bildirim botunu mutlaka kur.

**Kurulum kontrol listesi**

- [ ] `API_ID`, `API_HASH`, `SESSION_STRING` secret'ları dolu
- [ ] `config.json` geçerli (`python bot.py --check` temiz geçiyor)
- [ ] Kaynak kanallara kişisel hesapla üye olunmuş (log'da `ÜYE DEĞİLSİN` yok)
- [ ] `/kaynaktest` her çözülmüş kaynaktan son mesajı okuyabiliyor
- [ ] `/durum` ve `/test` yanıt veriyor
- [ ] (İsteğe bağlı) Bildirim botu kurulu, `/test` bildirim gönderiyor

---

## 2. Nasıl çalışır ve sınırları

- `bot.py` çalıştığı ve kişisel hesap kaynaklara üye olduğu sürece, yapılandırılmış tüm
  kaynakların yeni Telegram mesajları aynı canlı event handler ile anlık alınır.
- **Geçmişe dönük mesajlar otomatik iletilmez.** Bot kapalıyken veya yeniden başlatılırken kaçanlar
  sonradan bildirim olarak backfill edilmez; `/analiz` ve `/kaynaktest` geçmişi yalnızca
  manuel kontrol/analiz için okur.
- Aynı başlıklı fırsatlar tek mesajda birleşir; tekrarlar gruba atılmaz, ilk mesajın en alt
  bloğuna `📌 N kere paylaşıldı: <gruplar>` notu işlenir
  ([9. bölüm](#9-tekrar-birleştirme-aynı-fırsat-tek-mesaj)).
- **GitHub Actions kalıcı sunucu değildir.** Job yaklaşık 5 saat 50 dakika çalışır, sonra
  yeniden başlatılır. Başlatmalar arasında kısa boşluklar olabilir.
- GitHub'ın scheduled workflow için resmi en kısa aralığı 5 dakikadır ve zamanlama
  yoğunlukta gecikebilir
  ([GitHub Docs](https://docs.github.com/actions/using-workflows/workflow-syntax-for-github-actions)).
  Bu yüzden "her dakika garanti tarama" Actions ile mümkün değildir.
- `GH_PAT` varsa bot, süre dolmadan ~30 dakika önce yeni bir Actions çalışması başlatır;
  `concurrency` ayarı eski job'u kapatıp yenisini devralır. Yine de token iptali, GitHub
  yoğunluğu veya hesap limitleri nedeniyle **mutlak 7/24 garanti yoktur**.
- Gerçekten kesintisiz çalışma istersen `bot.py`'yi ücretsiz bir **Oracle Cloud Free Tier
  VM**'de `systemd` servisi olarak çalıştır; Actions'ı yedek olarak bırak.

**Oracle VM ile kurulum (özet)**

1. Oracle Cloud Free Tier hesabı aç, Ubuntu VM kur.
2. Repo'yu clone et, `pip install -r requirements.txt`.
3. Secret değerlerini ortam değişkeni veya `.env` olarak tanımla (**`.env`yi GitHub'a gönderme**).
4. `deploy/telegram-filter.service` dosyasını `systemd`'ye tanıtıp servisi başlat.
5. VM yeniden başlarsa servis otomatik kalkar.

---

## 3. Ayarlar: `config.json`

| Alan | Tip | Açıklama |
|---|---|---|
| `source_chats` | liste | Dinlenen kanal/grup listesi: `@kullaniciadi` veya `-100...` ID. **Hesabın üye olmadığı kanaldan canlı mesaj gelmeyebilir; takipçi kapalıyken kaçan mesajlar geri oynatılmaz.** Erişimi `/kaynaktest` ile sınayabilirsin. |
| `destination` | sayı / `"me"` | Filtrelenen mesajların gideceği sohbet. `me` = Kayıtlı Mesajlar (**bildirim gelmez**); grup ID'si yazarsan oraya düşer. |
| `include_keywords` | liste | Aranacak kelimeler. Büyük/küçük harf farkı yoktur; `İ`/`I` doğru indirgenir. |
| `exclude_keywords` | liste | Bunlardan biri geçerse mesaj atlanır (harici filtre **açıkken**). |
| `include_enabled` | `true` / `false` | 🔎 **Dahili kelime filtresi açık mı?** `false` ise `include_keywords` yok sayılır. `/open` ve `/close` ile değiştirilir. Varsayılan `true`. |
| `exclude_enabled` | `true` / `false` | 🚫 **Harici kelime filtresi açık mı?** `false` ise hiçbir mesaj harici kelimelerle engellenmez. `/open` ve `/close` ile değiştirilir. Varsayılan `true`. |
| `match_mode` | `any` / `all` | Dahili kelimeler nasıl eşleşsin: `any` = biri yeterli, `all` = hepsi aynı mesajda. (Eski `forward_all` değeri hâlâ okunur: dahili filtre kapalı demektir; `/open dahili` yazınca `any`'ye döner.) |
| `copy_mode` | `forward` / `copy` | Eski alan. `delivery_modes` yoksa ilk denenecek yolu belirler. |
| `delivery_modes` | liste | Korumalı kanallarda sırayla denenecek iletim yolları: `forward → copy → media → text → link` ([7. bölüm](#7-iletim-zinciri-korumalı-kanallar)). |
| `max_media_mb` | sayı | `media` yolunda indirilecek en büyük medya (varsayılan 25, `0` = sınırsız). |
| `link_appendix` | `smart` / `all` / `off` | Gizli linklerin eklenme biçimi ([8. bölüm](#8-gizli-bağlantılar-ve-fiyat-arama-düğmeleri)). |
| `message_link` | `true` / `false` | Her iletinin sonuna `🔗 Mesajı Gör: <t.me linki>` ekler (nihai güvence; kapatman önerilmez). |
| `source_footer` | `true` / `false` | Bildirimin **en altına kaynak grup adını kalın** yazar. "Fırsatı Gönderen" gibi bir etiket yazılmaz, ad bir linke bağlanmaz; yalnızca hangi gruptan geldiği görünür. Varsayılan `true`. |
| `notify_media` | `true` / `false` | Bildirim botu fotoğraf/videoyu da göndersin. |
| `clean_commands` | `true` / `false` | **Komut temizliği.** `control_chat`'te yeni bir komut yazıldığında bir önceki komut ve bot yanıtı silinir; ekranda yalnızca son mesaj kalır. İndirim bildirimleri bu temizliğin **dışındadır, asla silinmez.** Varsayılan `true`. |
| `single_message` | `true` / `false` | **Tek mesaj modu.** Bildirim botu mesajı gruba attıysa, hesabın attığı kopya gruptan silinir; böylece her fırsat tek mesaj olarak kalır. Bildirim gidemezse kopya **silinmez**. Varsayılan `true`. |
| `dedup_enabled` | `true` / `false` | **Tekrar birleştirme.** Normalize ürün sorgusu aynı olan fırsatlar tek mesajda toplanır; tekrarlar gruba atılmaz, ilk mesajın son bloğuna `📌 2 kere paylaşıldı: <gruplar>` gibi **tek satırlık** not işlenir ([9. bölüm](#9-tekrar-birleştirme-aynı-fırsat-tek-mesaj)). Varsayılan `true`. |
| `dedup_window_hours` | sayı | Normalize ürün sorgusu kaç saat boyunca "aynı fırsat" sayılsın (1–72, varsayılan 12). |
| `dedup_scan_limit` | sayı | Açılışta önbelleğe alınacak son ileti sayısı (0–100, varsayılan 100; `0` = tarama yapma). |
| `control_chat` | sayı / `"me"` | Eski kontrol sohbeti; `private_control: true` iken yalnızca Kayıtlı Mesajlar yedeği aktiftir. |
| `private_control` | `true` / `false` | Komutları bildirim botunun özel sohbetinden al; grup komutlarını kapat. Bu depoda `true`; alan yoksa eski davranış. `NOTIFY_BOT_TOKEN` gerekir. |
| `dm_enabled` | `true` / `false` | Hesap sahibine kişisel fırsat kopyaları. Başlangıçta `false`. |
| `dm_keywords` | metin listesi | Özel kopya filtresi; boş liste eşleşmez. `/dmfiltreekle` / `/dmfiltrecikar` ile anında değiştirilir; grup listesinden bağımsızdır. |
| `admin_user_id` | sayı / liste | `control_chat` bir grupsa **zorunlu**: komutları yalnızca bu ID'ler çalıştırabilir. |
| `auto_restart` | `true` / `false` | `GH_PAT` varsa yenileme zincirini açar (bir sonraki açılışta geçerli). |
| `notify_on_start` | `true` / `false` | Her açılışta hedefe kısa bir "başladım" mesajı gönderir. |

Bildirim botu token'ı config'e yazılmaz; tek kaynağı `NOTIFY_BOT_TOKEN` ortam değişkeni /
GitHub Actions secret'ıdır ([6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin)).
`notify_bot_token` eski config anahtarı yok sayılır.

**Ortam değişkeni ile geçersiz kılma:** Aynı adların büyük harflisi (`SOURCE_CHATS`,
`DESTINATION`, `MATCH_MODE`, `LINK_APPENDIX`…) config.json'ın üzerine yazar. Detaylar
`.env.example` dosyasında. (Eski `APPEND_LINKS` anahtarı da çalışır: `true` → `all`,
`false` → `off`.)

---

## 4. Telegram komutları

Komutlar yalnızca `control_chat` sohbetinden **ve** `admin_user_id` listesindeki
kullanıcılardan kabul edilir. Kayıtlı Mesajlar her zaman açıktır. Yetkisiz biri komut
yazarsa bot nedenini, o kişinin kullanıcı ID'sini ve nasıl yetki alacağını yazar.

### Telegram açıklamasına kopyalanacak kısa metin

```text
Kaynak mesajlarını hedef gruba iletir.
/open — Filtre aç (sorar: dahili, harici veya ikisi).
/close — Filtre kapat (sorar: dahili, harici veya ikisi).
/ayar, /ekle, /çıkar — kelime ve kaynak listelerini yönetir.
/kaydet, /iptal — taslağı kaydet / iptal et.
/analiz — Geçmiş başlıklarını tarar; en çok geçen kelimeleri istatistik olarak verir.
/kaynaktest — Kaynaklardan son mesajı okuyarak erişimi sınar.
/durum, /kaynaklar, /test, /id, /restart, /komutlar — durum ve araçlar.
```

`/open` ve `/close` anında uygulanır ve kaydedilir; grup listesi düzenlemeleri `/kaydet`
seçilene kadar taslakta kalır, kişisel DM kelimeleri ise hemen kaydedilir. `/restart` için
`GH_PAT` gerekir. Türkçe `I`/`İ` büyük harf yazımları (`/ANALİZ`, `/Iptal` gibi) otomatik eşleşir.

### Komut temizliği (ekranda yalnızca son mesaj)

`clean_commands` açıkken (varsayılan) **eski kullanıcı-hesabı kontrol sohbeti** kendini temizler.
**Botla özel sohbette bu temizlik uygulanmaz; geçmiş korunur.**

- Yeni bir komut yazdığında **bir önceki komutun** ve **botun ona verdiği yanıtın**
  tamamı silinir; ekranda yalnızca son komut ve yanıtı kalır.
- Çok adımlı akışlarda (`/ekle` → liste seç → değer → `/kaydet`) her adım bir öncekini
  siler; `/analiz` ilerleme mesajı rapor gelince kaybolur.
- Silme yalnızca komut diyaloğuna ait mesajları kapsar. **İndirim/ilan bildirimleri bu
  kayda hiç girmez, bu yüzden asla silinmez** — tek mesaj modunun sildiği hesap kopyası
  da bu kuralın dışındadır (o, bildirim başarılı olduğunda silinir).
- Silme için hesabın o mesajları silebilmesi gerekir (kendi mesajların her zaman
  silinebilir; başka bir admin kullanıcıdan gelen komutlar için grupta yönetici olman
  gerekir). Silinemezse bot uyarı loglar, komut çalışmaya devam eder.
- Kapatmak için `config.json` → `"clean_commands": false` (veya `CLEAN_COMMANDS=false`).

### Tüm desteklenen komutlar

| Komut | Açıklama |
|---|---|
| `/komutlar` | Tüm desteklenen komutları ve açıklamalarını listeler |
| `/start` | Özel sohbet kullanımını açıklar; fırsat hedefini değiştirmez |
| `/durum` | Takipçinin durumunu, hedefini, sayaçlarını ve filtre durumunu gösterir |
| `/dmfiltre` | Kişisel kelimeleri, açık/kapalı durumunu ve sayaçlarını gösterir |
| `/dmfiltreekle` | Kişisel kelime ekler ve filtreyi açar; argümansız sorar, hemen kaydeder |
| `/dmfiltrecikar` | Kişisel kelime çıkarır; argümansız sorar, hemen kaydeder |
| `/dmac` | Kayıtlı kişisel filtreyi açar; boş listeyi açmaz |
| `/dmkapat` | Kelimeleri koruyarak kişisel fırsat gönderimini kapatır |
| `/ayar` | Grup dahili/harici kelimeleri ve kaynak listesi menüsünü gösterir |
| `/ekle` | Grup ayarlarında liste seçip kayıt ekleme taslağı başlatır |
| `/çıkar` | Grup ayarlarında liste seçip kayıt çıkarma taslağı başlatır |
| `/kaydet` | Bekleyen grup listesi taslağını kaydeder; DM değişiklikleri anında kaydedilir |
| `/iptal` | Bekleyen ekleme/çıkarma veya filtre seçimini iptal eder |
| `/open` | Grup filtresini açar; dahili, harici veya ikisi seçilebilir |
| `/close` | Grup filtresini kapatır; dahili, harici veya ikisi seçilebilir |
| `/kaynaklar` | İzlenen kaynakları ve çözülemeyenleri listeler |
| `/kaynaktest` | Her kaynağın son ham mesajını ve yerel işlem yolunu denetler; canlı event sayısını raporlar |
| `/analiz` | Kaynak geçmişindeki başlıkları analiz eder; örnek: `/analiz 100 tümü` |
| `/test` | Hedef gruba deneme mesajı gönderir; sonucu komut sohbetinde bildirir |
| `/id` | Bulunduğun sohbetin ve kullanıcının kimliğini gösterir |
| `/restart` | Yeni takipçi çalışması başlatır; `GH_PAT` gerekir |


**Tamamı virgülle ayrılmış hâli** (BotFather açıklamasına, sabitlenmiş mesaja ya da
grubun komut menüsüne doğrudan yapıştırabilirsin):

```text
/komutlar, /start, /durum, /dmfiltre, /dmfiltreekle, /dmfiltrecikar, /dmac, /dmkapat, /ayar, /ekle, /çıkar, /kaydet, /iptal, /open, /close, /kaynaklar, /kaynaktest, /analiz, /test, /id, /restart
```

#### Kaynak mesaj erişimini test etme

- `/kaynaktest` çözülmüş **tüm** kaynaklardan yalnızca en yeni mesajı okuyup erişim
  sonucunu, üyelik uyarısını, ham metin alanının birebir bütünlüğünü (uzunluk/hash),
  entity/link/medya bilgisini, yerel compose kuru denemesini ve bu çalışmada canlı
  alınan mesaj sayısını verir. Tek kaynak seçildiğinde ham mesajdan kısa bir örnek de
  gösterilir. `/kaynaktest 3`, `/kaynaktest @kanal` veya
  `/kaynaktest -100...` ile tek kaynak da seçilebilir. Çözülemeyen kaynaklar rapora
  ayrıca girer; bir kaynağın hatası diğer testleri durdurmaz.
- Bu bir duman (smoke) testidir: geçmişten bir mesaj okunması hesabın **şu anki erişimini**
  doğrular; kanala sahte mesaj atmaz. Bildirimler, kullanıcı hesabı üye olduğu ve takipçi
  çalıştığı sırada gelen yeni Telegram event'lerinden alınır. Kaynakta o çalışmada yeni
  mesaj yoksa canlı sayaç `0` kalması normaldir.
- Takipçi kapalıyken veya Actions yeniden başlarken oluşan mesajlar otomatik olarak
  sonradan iletilmez. `/analiz` geçmişi ayrıca okur ama geriye dönük bildirim göndermez.

---

## 5. Ayarları Telegram'dan düzenleme

Telegram akışını bilerek sade tuttuk: sohbetten şu üç liste düzenlenebilir, iki filtre de
`/open` ve `/close` ile bağımsız açılıp kapatılır; diğer teknik ayarlar config/kod üzerinden yönetilir.

| Menüde görünen ad | `config.json` alanı | Ne işe yarar |
|---|---|---|
| 🔎 Dahili kelimeler | `include_keywords` | Mesajda aranacak kelimeler |
| 🚫 Harici kelimeler | `exclude_keywords` | Bunlardan biri geçerse mesajı engeller |
| 📣 Grup isimleri | `source_chats` | Takip edilecek kanal/grup kullanıcı adı veya ID'si |

#### Filtreler: `/open` ve `/close`

İki filtre **birbirinden bağımsızdır**; hangisinin çalışacağına tam olarak sen karar verirsin:

| Filtre | Config alanı | Açıkken | Kapalıyken |
|---|---|---|---|
| 🔎 Dahili kelimeler | `include_enabled` | Mesajda kelimelerden en az biri geçmeli (`any`) / hepsi geçmeli (`all`) | Anahtar kelimeler yok sayılır |
| 🚫 Harici kelimeler | `exclude_enabled` | Bu kelimelerden biri geçen mesaj engellenir | Hiç engelleme yapılmaz |

```text
/open              → "Hangi filtreyi açalım?" diye sorar
/open dahili       → yalnızca dahili kelime filtresini açar
/open harici       → yalnızca harici engeli açar
/open ikisi        → ikisini birlikte açar
/close dahili      → dahili filtreyi kapatır (harici engel çalışmaya devam eder)
/close harici      → engellemeyi kapatır (dahili kelimeler aranmaya devam eder)
/close ikisi       → ikisini de kapatır: kaynaklardaki HER mesaj iletilir
```

Menüden seçim yaparken `1` (dahili), `2` (harici), `3` (ikisi) yazabilirsin; filtre
adını yazmak da yeterli. Her iki komut değişikliği **anında** uygular, `config.json`'a
yazar ve GitHub'a gönderir; yanıtta iki filtrenin güncel durumu görünür. Yanlış bir şey
yazarsan menü açık kalır, `/iptal` ile vazgeçebilirsin. Bekleyen bir liste taslağı
(`/ekle` → `/kaydet` bekleyen) varken filtre komutları çalışmaz, önce taslağı
sonuçlandırman istenir.

Diğer ayarlar (`destination`, iletim yolları, bildirimler, token'lar vb.) Telegram'dan
değiştirilemez; bunları `config.json`/kod üzerinden düzenle.

### Ekleme

1. Kontrol sohbetinde **`/ekle`** yaz.
2. Gelen menüden `1`, `2` veya `3` gönder (istersen kategori adını da yazabilirsin).
3. Bot seçtiğin listenin mevcut kayıtlarını gösterir.
4. Eklenecek kelimeleri **tek mesajda** gönder. Birden çok kaydı virgül (`,`),
   noktalı virgül (`;`) veya yeni satırla ayır:
   `kahve, şeker, süt` ya da `@kanal1, @kanal2, -1001234567890`
5. Son onayda **`/kaydet`** ile GitHub'a gönder veya **`/iptal`** ile taslağı sil.

Tek mesajda en fazla 50 kayıt işlenir. Listede zaten olan veya geçersiz kayıtlar
atlanır; onay ekranında `⚠️` satırıyla kaç kaydın neden atlandığı yazar.

```text
➕  LİSTEYE EKLE
━━━━━━━━━━━━━━━━━━━━
1. 🔎  Dahili kelimeler  ·  3 kayıt
2. 🚫  Harici kelimeler  ·  2 kayıt
3. 📣  Grup isimleri     ·  23 kayıt

Seçmek için 1, 2 veya 3 yaz.
Vazgeçmek için /iptal.
```

Grup/kanal eklerken `@kullaniciadi` veya `-1001234567890` biçiminde ID kullan. Bot
yeni kaynağı doğrulamaya çalışır; çözülemeyen kaynak taslağa alınmaz. **Kaydettikten sonra
`/kaynaktest @kullaniciadi`** (veya sadece `/kaynaktest` ile tüm kaynaklar) yazarak
hesabın kanaldan son mesajı okuyabildiğini ve üyelik durumunu kontrol et.

### Çıkarma

1. **`/çıkar`** yaz.
2. Kategoriyi seç; bot güncel listeyi numaralı olarak gösterir.
3. Çıkarmak istediğin kayıtların numaralarını veya listedeki tam değerlerini gönder;
   birden çok kayıt için virgülle ayır: `1, 3, çekiliş`
4. Onay mesajında **`/kaydet`** ya da **`/iptal`** seç.

Takip edilen kaynak listesinden son grup/kanal silinemez; botun çalışması için en az
bir kaynak kalmalıdır (bu yüzden hepsini birden çıkaran istek reddedilir).

### Taslak ve kayıt davranışı

Değer mesajını göndermek tek başına ayarı değiştirmez. Bot önce bir taslak ve iki net
seçenek gösterir:

```text
📝  DEĞİŞİKLİK TASLAĞI
━━━━━━━━━━━━━━━━━━━━
📂 Dahili kelimeler
➕ Eklenecek: kahve

Bu değişiklik henüz aktif değil ve config.json'a yazılmadı.

✅ Kaydet ve GitHub'a gönder  →  /kaydet
↩️ İptal et ve taslağı sil     →  /iptal
```

- **`/kaydet`** (veya sadece `kaydet`): değişikliği çalışan ayara uygular, `config.json`'ı
  atomik biçimde yazar ve GitHub deposuna commit/push etmeyi dener. Yanıtta GitHub sonucu görünür.
- **`/iptal`** (veya sadece `iptal`): bekleyen adımı/taslağı siler; config dosyası ve çalışan ayar değişmez.
- Bekleyen işlem 10 dakika kullanılmazsa zaman aşımına uğrar. Bot yeniden başlarsa
  tamamlanmamış taslak bellekte tutulduğu için silinir.
- Menü sırasında yanlış bir seçim yaparsan doğru seçimi tekrar gönder. Taslak onay
  aşamasındayken önce `/kaydet` veya `/iptal` ile sonuçlandır.

### Başlık kelime analizi (`/analiz`)

Hangi ürün türlerini hiç görmek istemediğini bulmak için geçmiş mesajların
**başlığı** (mesajın ilk anlamlı satırı; marka/ürün adının yazdığı yer) taranır ve
iki istatistik verilir:

1. **En çok geçen 25 kelime** (başlıkta geçen tüm kelimeler),
2. **En çok geçen 25 ilk kelime** (her başlığın ilk kelimesi).

```text
/analiz            → kaynak başına son 300 mesaj
/analiz 1000       → kaynak başına son 1000 mesaj (20–2000 arası)
/analiz tümü       → işlevsiz kelimeler ve saf sayılar da sayılsın
/analiz 500 tümü   → ikisi birlikte
```

- Yalnızca başlık okunur; açıklama, fiyat ve link satırları sayılmaz.
- Aynı başlık birden çok kanaldan gelmişse bir kez sayılır (tekrar birleştirme);
  böylece tek bir kampanya istatistiği bozmaz. Raporda kaç tekrarın
  birleştirildiği yazar.
- Varsayılan listede "ve/ile/için" gibi işlevsiz kelimeler ve saf sayılar elenir;
  hepsini görmek için `/analiz tümü` kullan.
- Harici listende olan kelimeler `🚫`, dahili listende olanlar `🔎` ile işaretlenir.
- Çıkan listeden istemediğin kelimeleri doğrudan harici listeye ekle:
  `/çıkar` → `2` (Harici kelimeler) → `bebek, oyuncak, kitap`.
- Tarama uzun sürebilir; her kaynak bitince mesaj güncellenir. Okunamayan kaynak
  olursa tarama durmaz, raporda "Okunamayan kaynak" satırı çıkar.


---

## 6. Bildirim kurulumu (telefona uyarı gelsin)

Takipçi **kendi Telegram hesabınla** gönderir; Telegram kendi gönderdiğin mesajlar için
bildirim üretmez. Bu yüzden fırsat gruba düşse bile telefonuna uyarı gelmez. Çözüm: gruba
ikinci bir gönderici olarak küçük bir bot eklemek — bildirimi onun attığı mesaj üretir.

**Bildirim biçimi (sabit düzen):** başlık → `💰Fiyat: …` (fiyatla ilgili tüm veri) →
`🔗 <ürün linki>` → `🔗 Mesajı Gör: <t.me linki>` → kaynaktan **alınmayan** satırlar ve
gizli linkler → en altta kalın kaynak adı; çoklu paylaşım notu bu son bloğu kapatır.
Başlık, fiyat ve ürün linki kaynak mesajdan **alınır** ve alındıkları satırlardan
**silinir** (kullanıcı isteği: "benim format için orijinalden veriyi al, aldıklarını da
aldığın yerden sil"); böylece aynı bilgi bildirimde iki kez görünmez. Biçim gereği
alınmayan satırlar (örn. `🗓️ 365 Günün En Düşük Fiyatı`) altta aynen korunur:
**veri kaybı yok**.

**İstisna — ürün değil, duyuru:** biçim yalnızca **ürün başlığı ve fiyat birlikte**
bulunduğunda kurulur. Kupon/duyuru paylaşımlarında (örn.
`🎟️ Hopi 200 TL ve üzeri alışverişlerde 50 TL indirim kuponu`) ikisi de olmaz; o
mesajlar biçime sokulmadan **olduğu gibi** iletilir (kullanıcı isteği: "istisnai
durumlarda bildirimleri olduğu gibi atsın"). Yardımcı kurallar:

- Kupon satırındaki tutarlar (`… kupon`, `… indirim kodu`) ve eşik ifadeleri
  (`200 TL ve üzeri kargo bedava`) **fiyat sayılmaz**; bu yüzden kupon mesajı yanlışlıkla
  ürün bildirimine dönüşmez.
- Kredi kartı/adres gibi fiyat içermeyen bilgiler başlık olarak seçilse bile fiyat
  bulunmadığı için biçim kurulmaz.

Fiyatla ilgili **tüm** veri üst fiyat satırındadır: kaynakta
`🏷️ 33 TL (3 Adet Alımda 22 TL)` yazıyorsa bildirimde
`💰Fiyat: 33 TL (3 Adet Alımda 22 TL)` olarak görünür — asıl fiyat (3'lü alımda 22 TL)
üstte kaçmaz. Fiyat satırı tümüyle tüketilir; ek bilgi aşağıda tekrar etmez.

```text
Palmolive Moments Lavanta Yağları ve Böğürtlen ile Nemlendirici Banyo ve Duş Jeli 500ml x 4 Adet

💰Fiyat: 225 TL

🔗 https://link.amazon/B02W5SjPe

🔗 Mesajı Gör: https://t.me/indirimdeal/50953

🗓️ 365 Günün En Düşük Fiyatı      ← kaynakta alınmayan satır: aynen korunur

İndirimde Al 🛒 🛍️ Hepsiburada Trendyol N11        ← kalın, etiketsiz, linksiz (son blok)
📌 2 kere paylaşıldı: İndirimde Al, FırsatZ   ← çoklu paylaşım notu bu bloğu kapatır
```

Kurallar:

- Kaynakta ürün linki yalnızca bir satırın tamamıysa (`🛒 https://…`), bir gizli
  hyperlink etiketi CTA ise (`Fırsata Git`) ya da bir satır yalnızca fiyat etiketiyse
  (`💰 Fiyat : 225 TL`) satır tümüyle düşer: bilgi üst blokta zaten vardır.
- Fiyatla ilgili ek veri varsa fiyatla birlikte üste taşınır:
  `💰 Fiyat : 107 TL / 3 adet alımda 64 TL` → üst satır
  `💰Fiyat: 107 TL / 3 adet alımda 64 TL` (altta tekrar etmez).
- Fiyat **etiketsiz** bir satırın başındaysa ve devamı fiyat içeriyorsa
  (`33 TL (3 Adet Alımda 22 TL)`) o veri de üste taşınır; başka veri içeriyorsa yalnızca
  fiyat tüketilir, kalanı gövdede kalır: `129,90 TL Stoklarla sınırlı` →
  `💰Fiyat: 129,90 TL` + gövdede `Stoklarla sınırlı`. Satırda URL varsa satır bütün
  olarak silinmez, link kaybolmaz.
- Gizli hyperlink'in görünen etiketi anlamlı bir metinse (ürün ya da kampanya adı gibi)
  dokunulmaz ve kaynak mesajdaki yazı tıklanabilir kalır.
- Ürün başlığı **veya** fiyat ayıklanamazsa gövde yeniden kurulmaz: mesaj olduğu gibi
  (temizlik + ek bağlantılar + `Mesajı Gör` ile) iletilir. Ürün linki bulunamazsa
  biçim kurulur ama link satırı hiç yazılmaz.
- Çoklu paylaşım notu **en alt bloğa**, kaynak grup adının altına eklenip mesajı
  kapatır: `📌 3 kere paylaşıldı: FırsatZ, İndirimde Al, OnuAl`; mesaj başına taşınmaz
  (bkz. 9. bölüm).
- Özet yerel metin ayıklamasıyla hazırlanır; ağa gidilmediği için bildirimde ekstra
  gecikme yaratmaz. Arama düğmeleri gövdeye karakter eklemeden inline klavyede kalır.

Bölümler (başlık/fiyat, ürün linki, `Mesajı Gör`, kaynak grup adı) arasında **tam bir
boş satır** olur; fazlası değil. Reklam/işbirliği etiketi veya WhatsApp bağlantısı
silinince geriye kalan çoklu boş satırlar otomatik olarak tek boş satıra indirilir,
baş/sondaki boş satırlar atılır. Bu sıkıştırma **yalnızca** boşluk, tab, `\r` ve satır
sonu karakterlerini alır — metin içeriği, emojiler ve entity offset'leri korunur, veri
kaybolmaz.

- Kaynak adı "Fırsatı Gönderen" gibi bir **etiketle yazılmaz** ve **hiçbir linke
  bağlanmaz**; yalnızca hangi gruptan geldiği kalın olarak görünür
  (`source_footer: false` ile tamamen kapatılabilir).
- Arama düğmeleri gövdeye karakter eklemez; ek özellik uğruna mevcut metin kısaltılmaz.
- Metinden yalnızca bağımsız `#işbirliği`, `işbirliği`, `#reklam`, `reklam` ifadeleri
  ile doğrudan WhatsApp URL'leri çıkarılır. WhatsApp'a gizlenmiş hyperlink'in görünen
  etiketi düz metin olarak korunur; başka kelime ve domain'ler silinmez.
- **Kanal tanıtımı ve hashtag satırları temizlenir:** `💚Whatsapp Önemli Fırsatlar` gibi
  mesajlaşma kanalı tanıtımları ve `#amazon #indirimalarmi` gibi yalnızca etiketlerden
  oluşan satırlar bildirime hiç girmez (kullanıcı isteği: "tertemiz görünüm"). Kurallar
  dar tutulur: satırda hem bir kanal adı (WhatsApp/Telegram) geçmeli hem de satırdaki
  **tüm kelimeler** tanıtım sözlüğünden gelmelidir. Sözlükte olmayan tek bir kelime
  satırı korur; bu yüzden `WhatsApp'tan bilgi ... 9 TL` gibi içerik satırları,
  fiyat/ürün satırları ve tüm mesaj silinmesini gerektiren durumlar olduğu gibi kalır.

**Adımlar (2 dakika)**

1. Telegram'da **@BotFather** → `/newbot` → görünen ad → `_bot` ile biten kullanıcı adı.
   BotFather sana `7123456789:AAHx...` biçiminde bir token verir (kimseyle paylaşma).
2. Fırsatların düştüğü grubu aç → **Üyeler → Üye ekle** → botun kullanıcı adını yaz ve ekle.
   Yönetici yapmana gerek yok.
3. GitHub deposunda **Settings → Secrets and variables → Actions → New repository
   secret** bölümünden `NOTIFY_BOT_TOKEN` adlı secret oluştur ve token'ı değer olarak
   gir. Yerel çalıştırmada `NOTIFY_BOT_TOKEN` ortam değişkenini kullan. Token'ı
   `config.json`a yazma veya commit etme; kod yalnızca secret/ortam değerini kullanır.
4. Daha önce bir token `config.json` veya Git geçmişine eklendiyse BotFather'da
   `/revoke` ile iptal edip yenisini secret'a koy. Bu değişiklik güncel config'ten eski
   alanı kaldırır; Git geçmişindeki eski blob ayrıca kalabilir. Workflow'u yeniden
   başlatıp gruba `/test` yaz.
   `🔔 Bot bildirimi de gönderildi
   (telefonuna düşmeli).` yazıyorsa tamamdır.

**Sorun giderme**

| `/test` yanıtı | Anlamı | Çözüm |
|---|---|---|
| `HTTP 403: bot is not a member` | Bot gruba eklenmemiş | 2. adımı tekrarla |
| `HTTP 400: chat not found` | `destination` ID'si yanlış | Gruba `/id` yaz, çıkan ID'yi kullan |
| `HTTP 401: Unauthorized` | Token bozuk/yanlış | BotFather'dan `/revoke` ile yenisini al |
| `HTTP 429` | Çok sık mesaj | Bot eşleşme başına 1 mesaj atar; kaynak sayısını azalt |

`NOTIFY_BOT_TOKEN` secret'ı tanımlı değilse takipçi çalışmaya devam eder; yalnızca bildirim
ve arama düğmeleri gönderilmez.

**Tek mesaj modu (`single_message`):** Bildirim botu mesajı gruba attıktan sonra
hesabın attığı kopya gruptan silinir; grupta yalnızca botun mesajı (telefonuna
uyarı düşen mesaj) kalır. Bildirim gönderilemezse kopya **silinmez** — mesajsız
kalmaktansa iki kopya iyidir. Kapatmak için `config.json` → `"single_message": false`.

---

## 7. İletim zinciri: korumalı kanallar

Birçok indirim kanalı **"içeriği koru"** (`noforwards`) ayarını açar. Orada `forward`
`CHAT_FORWARDS_RESTRICTED` hatası verir, `copy` de reddedilebilir. Bu yüzden tek yol
denenmez; sırayla deneyip ilk başarılı olanı kullanır:

| Sıra | Yol | Ne yapar |
|---|---|---|
| 1 | `forward` | Sunucu tarafında iletir; kaynak etiketi korunur (en ucuz yol) |
| 2 | `copy` | Mesajı hedefe yeniden gönderir |
| 3 | `media` | **Medyayı indirip sıfırdan yükler** — görsel olarak orijinaliyle aynı |
| 4 | `text` | Sadece metni gönderir, "medya iletilemedi" notu ekler |
| 5 | `link` | `t.me` bağlantı kartı gönderir |

- `delivery_modes` ile sırayı kendin belirleyebilirsin; yazmadığın yollar yedek olarak sona
  eklenir, bilinmeyen isim olursa `--check` uyarır.
- `/durum` komutu hangi yolun kaç kez işe yaradığını gösterir:
  `İletim sırası: forward → copy → media → text → link | kullanılan: forward×120, media×7`

---

## 8. Gizli bağlantılar ve fiyat arama düğmeleri

İndirim kanalları ürün linkini çoğu zaman açıkça yazmaz; üç yere gizler:

| Gizleme yolu | Örnek | Bot ne yapar |
|---|---|---|
| Metin altına gizlenmiş hyperlink | "**Fırsata Git**" yazısı görünür, link altındadır | Ürün/mağaza linkiyse sabit üst bloktaki `🔗 <link>` satırına alınır; CTA etiketi ("Fırsata Git") tüketilir, anlamlı bir etiketse kaynak mesajda tıklanabilir kalır |
| Inline buton | Yazıda link yok, butondadır | Bot bildirimi URL düğmesini korur; ürün linkiyse sabit üst bloktaki `🔗 <link>` satırında da görünür |
| Link önizlemesi | Metinde link yok, önizleme kartı var | Ürün/mağaza hedefi ürün özetine; diğer hedefler ek link listesine girer |

Bildirim botu, kaynakta aynı arama hizmetine ait bir URL/buton yoksa eksik düğmeleri şu
sırayla ekler:

1. **Google Alışveriş** — `https://www.google.com/search?udm=28&q=<ürün>&hl=tr&gl=tr`
2. **Akakçe'de ara** — `https://www.akakce.com/arama/?q=<ürün>`
3. **Cimri'de ara** — `https://www.cimri.com/arama?sort=price,asc&q=<ürün ilk iki kelime>`

Cimri arama sonuçlarında gözlenen kalıp `/arama` yolu, `q` sorgu parametresi ve
`sort=price,asc` fiyat sıralamasıdır. Örnek:
`https://www.cimri.com/arama?sort=price,asc&q=Arzum+AR5106`. Sorgudaki Türkçe karakterler
ve özel karakterler URL-encode edilir. Araştırma sırasında Cimri sayfasının doğrudan
açılması HTTP 500 döndürdüğü için sıralama davranışı canlı sayfada bağımsız doğrulanamadı;
URL kalıbı indekslenmiş Cimri arama sonuçlarıyla destekleniyor.

**Cimri Cloudflare korumalıdır.** Site, "Sorry, you have been blocked" sayfasını
Cloudflare WAF/Access kuralları üzerinden gösterir; bu kuralı site sahibi yönetir ve
bağlantı üreten taraf olarak biz engeli kaldıramayız. Cloudflare engeli tipik olarak şu
durumlarda tetiklenir:

- uzun ve yoğun URL-encode edilmiş sorgu dizeleri (WAF'ın şüpheli istek puanı yükselir),
- kısa sürede çok sayıda istek (rate limiting) veya VPN/proxy IP'si,
- "Browser Integrity Check" (Cloudflare topluluğunda VPN eklentisi, reklam engelleyici,
  kurumsal proxy ve uygulama içi tarayıcıların bu kuralı tetiklediği bildirildi),
- JavaScript/çerez çalıştırmayan tarayıcılar (Telegram'ın uygulama içi tarayıcısı dahil).

Bu yüzden Cimri düğmesinin sorgusu **ilk iki kelimeye** indirilir
(`CIMRI_QUERY_WORDS = 2`): hem URL kısalır, hem aranan dize az özel karakter taşır.
Google Alışveriş ve Akakçe tam sorguyu kullanmaya devam eder. Engellenirsen bağlantıyı
Telegram'ın içi tarayıcısı yerine normal tarayıcıda açmak, VPN'i kapatmak veya diğer iki
düğmeyi kullanmak en pratik çözümdür; engel genelde bir süre sonra kendiliğinden kalkar.
Engel bizim tarafımızdan kaldırılamaz: kuralı site sahibi yönetir ve yalnızca Ray ID ile
Cloudflare panelinden (Security → Events) görebilir.

### Kaynak formatları farklı olsa da sorgu ürün adından üretilir

Kaynakların mesaj düzeni standart değil; başta fiyat, indirim oranı veya başlık bulunabilir.
Arama sorgusu için bot mesajı satır satır, **yerel ve anlık** olarak temizler:

- Tek başına fiyat/para birimi satırları (`1.299 TL`, `₺7.499`) ve indirim yüzdesi/
  kampanya satırları (`%50 indirim`, `İndirim oranı %30`) sorgu başlığı sayılmaz.
- Ürünle aynı satırdaki fiyat ve indirim bilgileri de sorgudan çıkarılır.
- İlk kullanılabilir ürün adı satırı seçilir; örneğin
  `1.299 TL` → `%50 indirim` → `🛍️ Philips Airfryer XXL 6.2L` → ürün linki
  düzeninde üç arama da `Philips Airfryer XXL 6.2L` ile açılır.
- Baştaki emoji/semboller ve görünür ürün URL'leri sorguya katılmaz. Ürün adı bulunamazsa
  yapay/boş arama düğmesi eklenmez.

Bu ayıklama **yalnızca arama düğmelerindeki sorguyu** etkiler: gönderilen mesaj gövdesi,
fiyat, indirim oranı, biçimlendirme ve kaynak linkleri olduğu gibi kalır. Algoritma ağ
isteği yapmadığı için bildirim yoluna ek gecikme getirmez.

Kaynakta zaten Google Shopping/Akakçe/Cimri linki varsa aynı hizmet için yeni düğme
çoğaltılmaz. Kaynaktaki özel ürün butonları korunur; tanınan arama butonları da URL'leri
bozulmadan standart Google → Akakçe → Cimri sırasına alınır. Eski bir kaynaktan gelen
`marketfiyati.org.tr` linki de normal kaynak linki olarak korunur, fakat artık Market Fiyatı
arama düğmesi oluşturulmaz. Düğmeler inline klavyededir; mesaj gövdesini uzatmaz ve
4096/1024 karakter sınırına yeni karakter eklemez. Bu düğmeler bildirim botu gerektirir;
Telegram kullanıcı hesabı inline klavye gönderemez.

Temizleme gereken iletide `forward` atlanır (özgün mesaj değişmeden iletileceği için);
kopyalama/yedek zinciri temizlenmiş metin ve entity offset'leriyle çalışır. Böylece
reklam/işbirliği etiketleri, doğrudan WhatsApp URL'leri ve WhatsApp URL butonları
forward ile geri eklenmez.

Ek olarak her iletinin sonuna `🔗 Mesajı Gör: <t.me linki>` eklenir.
`link_appendix: "smart"` (varsayılan) ürün özetinde zaten gösterilen gizli hyperlink'i
ikinci kez ek listeye yazmaz; yalnızca başka türlü taşınamayan linkleri ekler. `"all"`
ürün linki özette olsa bile aynı URL'yi çoğaltmadan diğer gizli linkleri de yazar;
`"off"` ek bağlantı listesini yazmaz. Ürün linki bulunduğunda sabit üst bloktaki
`🔗 <link>` satırı, bu ek bağlantı ayarından bağımsız olarak korunur (link bir kez yazılır).

---

## 9. Tekrar birleştirme (aynı fırsat, tek mesaj)

Aynı indirim 3-5 kanal tarafından dakikalar içinde, çoğu zaman birebir aynı
başlıkla paylaşılır. Hepsini gruba atmak mesaj kalabalığı yapar; oysa tekrarlar
indirimin "gerçek ve teyitli" olduğunun işaretidir. Bu yüzden tekrar birleştirme
açıktır (varsayılan):

- **İlk kopya** her zamanki gibi gönderilir ve başlığı bellekteki listeye yazılır.
- **Sonraki aynı fırsatın kopyaları gruba ATILMAZ.** Onun yerine ilk mesajın **en alt
  bloğuna**, kaynak grup ad(lar)ının altına çoklu paylaşım notu işlenir ve mesaj orada
  kapanır (kullanıcı isteği: "2-3-4 kere paylaşıldı şu şu şu gruplarda diyerek son
  bloğa koyup kapatalım"). Not **tek satırdır**; kaç kez paylaşıldığı ve hangi
  gruplardan geldiği yazılır:

```text
Sıcak ÇAY

💰Fiyat: 5 TL

🔗 https://amzn.to/ornek

🔗 Mesajı Gör: https://t.me/firsatz/1

Kaçırılmayacak fırsat!

firsatz                                   ← kalın kaynak adı (son blok)
📌 3 kere paylaşıldı: firsatz, İndirimde Al, OnuAl   ← not bu bloğu kapatır
```

- Sayı 5'i geçince vurgu artar: `📌 N kere paylaşıldı: …` → `🚨 N kere paylaşıldı: … —
  KAÇIRMA!`. Telegram'da mesaj rengine müdahale edilemez; kalın + emoji,
  platformun sunduğu en güçlü vurgu kombinasyonudur. Çok dar medya açıklamalarında
  grup adları sığmazsa yalnızca sayı yazılır.
- Grup adları kaynaklardan toplanır (ilk kopyayı gönderen grup + sonraki kopyaları
  yakalayan gruplar); aynı grup birden fazla kez paylaşırsa adı bir kez yazılır.
- **Geriye dönük uyumluluk:** eski `✅ 2 kaynakta paylaşıldı · teyitli fırsat` /
  `🔥 3 kaynakta ...` / `🚨 N KAYNAKTA PAYLAŞILDI` rozetleri hâlâ tanınır ve sökülür;
  daha eski sürümün ikinci satırdaki `📌 Kaynaklar: ...` listesi de atlanır. Böylece
  restart sonrası açılış taraması eski bildirimleri sayar ve not yeni biçimle yazılır.
- Eşleşme mesajın ilk satırına değil, fiyat/indirim/CTA satırlarını yerel olarak
  ayıklayan ürün başlığı sorgusuna göre yapılır. Harf büyüklüğü, noktalama, kelime sırası
  ve `6.2L` / `6,2 L` gibi sayı biçimi farkları normalize edilir; böylece fiyatı başta
  yazan kaynakla ürünü başta yazan kaynak eşleşebilir. Başlıksız (salt medya) iletiler
  birleştirilmez.

**Hız ve limitler:** eşleşme bellekteki listede yapılır; her iletide geçmiş
taranmaz, yani bildirim gecikmez ve ileti başına ek API çağrısı yapılmaz
(FloodWait/429 riski yok). Tek tarama açılışta bir kez yapılır: hedeften son
`dedup_scan_limit` ileti (varsayılan 100, tek API çağrısı) okunup liste ısıtılır;
böylece yeniden başlama sonrası aynı normalize ürün sorgusu ikinci kez düşmez. Not
güncellemesi arka planda yapılır ve aynı sohbetteki düzenleme hız sınırının
altında kalır. GitHub'a ek istek atılmaz.

**Ayarlar** (`config.json`): `dedup_enabled` (varsayılan `true`),
`dedup_window_hours` (normalize ürün sorgusu kaç saat "aynı fırsat" sayılsın, 1–72,
varsayılan 12; haftalar sonra aynı ürün yine indirime girerse YENİ fırsat sayılır),
`dedup_scan_limit` (açılış taraması, 0–100, varsayılan 100; `0` = tarama yapma).
`/durum` birleştirilen tekrar ve rozet sayılarını gösterir.

---

## 10. ID'leri doğrulama

En hızlı yol: **gruba `/id` yaz.**

```text
🆔 Bu sohbetin ID'si: -5092968106
• Tür: Chat
• Senin kullanıcı ID'n: 1143378073
config.json için:
  "control_chat": -5092968106,
  "admin_user_id": 1143378073
```

`/id` da bir kontrol komutu olduğu için, komut hiç yanıt vermiyorsa mevcut `control_chat`
yanlış demektir: geçici olarak `"control_chat": "me"` yapıp Kayıtlı Mesajlar'dan `/id` ile
doğru ID'yi al.

> **Dikkat:** temel grup (`-5092968106` gibi) sonradan süpergruba dönüşürse ID `-100...`
> biçimine değişir ve config'i güncellemen gerekir.

---

## 11. Sorun giderme

| Belirti | Muhtemel neden | Çözüm |
|---|---|---|
| Workflow görünmüyor | PR `main`'e merge edilmedi veya Actions kapalı | PR'ı merge et; Settings → Actions'tan workflow'ları etkinleştir |
| Run 10-15 saniyede kırmızı | Bir secret boş (genelde `API_HASH`) | **Ayarları doğrula** adımının log'una bak |
| `Your API ID or Hash cannot be empty` | `API_ID`/`API_HASH` boş | İki secret'ı da ekle |
| `SESSION_STRING` yetkisiz | Session kesilmiş/iptal edilmiş | Colab notebook ile yeniden üret |
| `Cannot find any entity corresponding to "-5092968106"` | ID metin olarak verilmiş | Sayı olarak yaz (güncel sürüm otomatik çeviriyor) |
| Grup komutları çalışmıyor | `admin_user_id` boş veya `control_chat` yanlış | `/id` ile ID'leri doğrula; config'i güncelle |
| Mesaj geliyor ama bildirim yok | Kendi hesabın gönderiyor; Telegram bildirim üretmez | [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin) |
| Mesaj hiç gelmiyor | Kanal listede değil / hesap üye değil / kelime eşleşmiyor | `/kaynaklar` ve `/durum`a bak; log'daki `ÜYE DEĞİLSİN` uyarılarını kontrol et |
| "Fırsata Git" var ama ham link yok | Link yazının altına gizlenmiş; tıklanabilir | Yazıya dokun. Ham URL istersen `link_appendix: "all"` |
| Fotoğraf "unnamed" dosya olarak geliyor | Eski sürüm hatası | Bot'u güncelle |
| `⚠️ ... yalnızca bu oturumda geçerli` | Ayar değişikliği depoya yazılamadı | [14. bölüm](#14-bu-güncellemeden-sonra-yapılacaklar) |
| `GH_PAT` ile yenileme olmuyor | Token izni yok veya süresi dolmuş | Token'da **Actions: Read and write** olduğunu ve expiration tarihini kontrol et |
| Aynı mesaj iki kez geliyor | İki job aynı anda çalışmış | Actions concurrency ayarını ve açık run'ları kontrol et |
| Aynı fırsat iki kez gruba düştü | Başlıklar birebir aynı değil, pencere doldu veya açılış taraması dışında kaldı | `/durum` satırındaki birleştirme sayacına bak; gerekirse `dedup_window_hours` değerini artır |
| `FloodWait` / rate limit | Çok fazla forward | Kaynak sayısını ve kelime filtresini daralt |

---

## 12. Güvenlik

- `SESSION_STRING`, API hash'i, bot token'ı ve telefon kodunu kimseye gösterme; `config.json`a
  secret yazma veya credential içeren değişikliği commit etme. Bildirim botu token'ı için
  `NOTIFY_BOT_TOKEN` GitHub secret'ını kullan.
- Bot token'ı daha önce GitHub'a push edildiyse BotFather'da `/revoke` ile iptal edip yenisini
  oluştur; Git geçmişinden silmek tek başına token'ı geçersiz kılmaz.
- `SESSION_STRING`/API hash geçmişte GitHub'a push edildiyse [my.telegram.org](https://my.telegram.org)
  üzerinden API uygulamasını yenile. Session geçersiz olursa yeni session üretip
  `SESSION_STRING` secret'ını güncelle.
- Kaynak kanalların kurallarına uy; yüksek hacimli otomatik iletim Telegram rate-limit'ine
  takılabilir.

---

## 13. Geliştirici notları

```bash
python -m unittest discover -s tests -v   # birim ve uçtan uca testler
python bot.py --check                     # secret + config doğrulaması
```

`push` ve `pull_request` olaylarında **Testler** workflow'u aynı iki komutu GitHub'da da
çalıştırır. Ek bağımlılık yok, sadece `telethon` gerekir.

Dosyalar: `bot.py` (tüm mantık), `config.json` (ayarlar), `generate_session.py` ve
`session_generator_colab.ipynb` (session üretimi), `deploy/telegram-filter.service`
(systemd örneği), `tests/` (birim ve uçtan uca testler).

---

## 14. Bu güncellemeden sonra yapılacaklar

Bu sürüm üç liste düzenlemesini ve iki filtre komutunu sunar: `/ekle` veya `/çıkar` →
seçim → değer (virgülle birden çok) → `/kaydet` ya da `/iptal`; filtreler için `/open` ve
`/close`; geçmiş istatistiği için `/analiz`.
Diğer teknik ayarlar `config.json` üzerinden yönetilir. Ayrıntılar [4. bölümde](#4-telegram-komutları)
ve [5. bölümde](#5-ayarları-telegramdan-düzenleme).

Bu repodaki güncel davranış değişiklikleri:

1. **"Fırsatı Gönderen" etiketi kaldırıldı; kaynak adı artık sade ve kalın.**
   Bildirimin en altında yalnızca hangi gruptan geldiği yazılır — "Fırsatı Gönderen"
   gibi bir açıklama ve hiçbir link yok, sadece grup adı kalın olarak durur
   (`source_footer`, varsayılan `true`).
2. **Komut temizliği eklendi** (`clean_commands`, varsayılan `true`). Komut sohbetinde
   yeni bir komut yazıldığında bir önceki komut ve yanıtı silinir; ekranda yalnızca son
   mesaj kalır. İndirim bildirimleri bu temizliğin dışındadır, asla silinmez.
   Ayrıntı: [4. bölüm → Komut temizliği](#komut-temizliği-ekranda-yalnızca-son-mesaj).
3. **İşbirliği/reklam etiketleri ve WhatsApp linkleri temizlenir; eksik arama hizmetleri
   inline düğme olarak eklenir.** Hashtag'ler ve tam kelimeler hassas sınırlarla eşleşir;
   arama düğmeleri ileti gövdesini uzatmaz. Ayrıntı: [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin) ve [8. bölüm](#8-gizli-bağlantılar-ve-fiyat-arama-düğmeleri).
4. **Bildirim botu token'ı yalnızca `NOTIFY_BOT_TOKEN` secret'ından okunur.**
   Eski config alanı ve güncel `config.json` değeri kaldırıldı; geçmişteki token'ı iptal et.
5. **Tekrar rozeti tek satıra indi.** İkinci ve sonraki kopyalarda ilk mesaja işlenen rozet
   `✅ 2 kaynakta paylaşıldı · teyitli fırsat` biçiminde **tek satırdır**; kaynak adları tek
   tek yazılmaz (5 kaynak alt alta yazılınca bildirim karışıyordu). Daha önce gönderilmiş
   mesajlardaki `📌 Kaynaklar: ...` satırı okunurken yine atlanır.
   Ayrıntı: [9. bölüm](#9-tekrar-birleştirme-aynı-fırsat-tek-mesaj).
6. **Kanal tanıtımı ve hashtag satırları temizlenir.** `💚Whatsapp Önemli Fırsatlar`,
   `📲 WhatsApp grubumuz`, `#amazon #indirimalarmi` gibi satırlar bildirime hiç girmez;
   fiyat/ürün satırları ve içerik taşıyan satırlar korunur (dar kural — veri kaybı yok).
   Ayrıntı: [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin).
7. **Arama düğmeleri artık farklı kaynak formatlarında doğru ürün adını arar.** Fiyat/
   indirim satırları sorgudan ayıklanır; sıralama Google Alışveriş → Akakçe → Cimri'dir.
   Cimri araması `/arama?sort=price,asc&q=...` URL'sini kullanır ve sorguyu ilk iki
   kelimeyle sınırlar (Cloudflare koruması). İşlem yereldir, bildirim gecikmesi eklemez;
   mesaj metnine dokunmaz.
8. **Kaynak erişim smoke testi eklendi.** `/kaynaktest` her çözülmüş kanaldan tek son mesaj
   okuyarak erişimi, üyeliği ve o oturumdaki canlı event sayısını raporlar; tüm arşivi
   indirmez ve kaynaklara test mesajı göndermez.
   Ayrıntı: [4. bölüm](#4-telegram-komutları) ve [8. bölüm](#8-gizli-bağlantılar-ve-fiyat-arama-düğmeleri).
9. **Bildirim sabit düzene geçti: başlık → `💰Fiyat: …` → `🔗 <ürün linki>` → kalan
   satırlar → `Mesajı Gör` → kaynak adı.** Başlık, fiyat ve ürün linki kaynak mesajdan
   alınır ve **alındıkları satırlardan silinir**; biçim gereği alınmayan satırlar
   (örn. `🗓️ 365 Günün En Düşük Fiyatı`) altta aynen korunur. Böylece aynı bilgi iki kez
   görünmez, veri kaybı olmaz. `🔗 Ürün fırsat linki:` etiketi kaldırıldı; link satırı
   yalnızca ataç + adrestir.
   Ayrıntı: [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin).
10. **Kupon/duyuru istisnası ve fiyat satırı birleşmesi.** Sabit düzen artık yalnızca
    **ürün başlığı ve fiyat birlikte** varsa kuruluyor; kupon ve duyuru paylaşımları
    (başlık/fiyat yok) olduğu gibi iletilir. Kupon satırlarındaki tutarlar ve
    `200 TL üzeri` gibi eşikler fiyat sayılmaz. Fiyatla ilgili ek veri
    (`🏷️ 33 TL (3 Adet Alımda 22 TL)`) üst fiyat satırına taşınır, altta tekrar etmez.
    Cimri arama düğmesinin sorgusu ilk iki kelimeye indi (Cloudflare WAF bloğunu azaltmak
    için). Ayrıntı: [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin) ve
    [8. bölüm](#8-gizli-bağlantılar-ve-fiyat-arama-düğmeleri).
11. **Blok sırası son hâli ve çoklu paylaşım notu son bloğa taşındı.** Bildirim sırası
    artık başlık → `💰Fiyat` (fiyatla ilgili tüm veri) → `🔗 <ürün linki>` →
    `🔗 Mesajı Gör` → bloklara girmeyen satırlar ve gizli linkler → en altta kaynak
    grup adı. Çoklu paylaşım notu (`📌 N kere paylaşıldı: <gruplar>`) fiyat satırının
    altına değil, **bu son bloğa** eklenip mesajı kapatır; sayı ve grup adları
    kaynaklardan toplanır. Eski `✅/🔥/🚨 … kaynakta paylaşıldı` rozetleri okunup
    yeni biçime taşınır (geriye dönük uyumluluk).
    Ayrıntı: [6. bölüm](#6-bildirim-kurulumu-telefona-uyarı-gelsin) ve
    [9. bölüm](#9-tekrar-birleştirme-aynı-fırsat-tek-mesaj).

### 1. PR'ı `main`'e merge et

Actions workflow'ları **varsayılan daldan** (`main`) çalışır; güncel PR `main`'e merge
edilmeden yeni kod devreye girmez. Merge sonrasında:

**Actions → Telegram indirim takipçisi → Run workflow → `main`** ile yeni kodu bir kez elle
başlat. (Bunu yapmazsan mevcut job eski kodla 6 saate kadar devam eder.)

### 2. `GH_PAT` — sadece süresi dolduysa yenile

- **Otomatik yenileme zinciri** için `GH_PAT`'a *Actions: Read and write* izni yeterli;
  mevcut token'ın çalışması için dokunmana gerek yok.
- **Ayarların depoya yazılması** GitHub Actions'ta **ekstra bir şey gerektirmez**: workflow
  artık `GITHUB_TOKEN` kullanıyor ve job'a `contents: write` izni verilmiş durumda. Yani
  `GH_PAT`'ı sırf bu özellik için yenilemene gerek yok.
- Token'ın süresi zaten dolmuşsa yenilerken izin listesine dikkat et:
  - Actions zinciri → **Actions: Read and write**
  - VM'de çalıştırıyorsan depoya yazmak için → **Contents: Read and write**

### 3. Doğrula

Kontrol sohbetinde yeni akışı doğrula:

```text
/ayar       → üç liste ve filtre kısayolları gösterilmeli
/close      → menü sormalı: 1 dahili · 2 harici · 3 ikisi
1           → dahili filtre kapanmalı, harici engel sürmeli
/open dahili→ dahili filtre geri açılmalı (anında kaydeder)
/close ikisi→ iki filtre de kapanmalı; her mesaj iletilmeli
/open ikisi → ikisi birden açılmalı
/ekle       → üç liste seçimi, ardından mevcut liste
kahve, şeker, süt → taslakta üçü görünmeli; henüz kaydedilmemeli
/kaydet     → config.json yazılmalı ve GitHub sonucu görünmeli
/çıkar      → liste seçimi ve mevcut kayıtlar gösterilmeli
1, 3        → iki kayıt birden çıkarılmalı
/iptal      → bekleyen taslak silinmeli; ayar değişmemeli
/analiz     → iki istatistik (25 kelime + 25 ilk kelime) gelmeli
            → beğenmediğin kelimeyi /çıkar → 2 ile harici listeye ekle
```

Yanıtta **`✅ repo'ya işlendi`** yazıyorsa kalıcılık çalışıyor demektir: repo'da
`config.json` güncellenmiş olur ve Actions yeniden başlasa da ayarın kalır.

`⚠️ ... yalnızca bu oturumda geçerli` uyarısı alırsan önce **PR'ın merge edildiğinden ve
yeni bir run başlattığından** emin ol (eski kodda bu özellik yoktur); hâlâ uyarı varsa
[11. bölümdeki](#11-sorun-giderme) ilgili satıra bak.

### Hızlı özet

- [ ] Güncel PR'ı `main`'e merge et
- [ ] Actions'tan workflow'u bir kez elle başlat
- [ ] `/ayar` → sade menü geliyor mu?
- [ ] `/open` ve `/close` → menü soruyor mu? Dahili/harici filtre ayrı ayrı kapanıyor mu?
- [ ] `/ekle` → kategori seç → `a, b, c` gönder → `/kaydet` ile GitHub'a yaz
- [ ] `/çıkar` → listeyi gösteriyor mu? `1, 3` gibi çoklu çıkarma çalışıyor mu?
- [ ] `/analiz` → iki istatistik geliyor mu? Kelimeleri harici listeye ekleyebiliyor musun?
- [ ] Bildirim düzeni: başlık → `💰Fiyat` (fiyatla ilgili tüm veri) → `🔗 link`;
      başlık/fiyat/link gövdede tekrar etmiyor mu? Kaynakta kalan satır
      (örn. "365 Günün En Düşük Fiyatı") duruyor mu? Kupon paylaşımı olduğu gibi geliyor mu?
- [ ] Tek mesaj modu: gruba tek mesaj düşüyor mu (bot bildirimi), kopya siliniyor mu?
- [ ] Komut temizliği: `/durum` yaz → eski komut/yanıt silindi mi? Bildirimler duruyor mu?
- [ ] `/iptal` taslağı değiştirmeden siliyor mu?
- [ ] `GH_PAT`: süresi dolmadıysa dokunma; dolmuşsa *Actions: Read and write* ile yenile
