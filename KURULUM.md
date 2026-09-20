# Jev Browser Chrome — kolay Windows kurulumu

Bu sürüm, Jev'in mevcut ve giriş yapılmış kişisel Chrome sekmelerinde çalışması
içindir. Ayrı Chrome profili açmaz, çerez kopyalamaz ve
`chrome://inspect/#remote-debugging` kullanmaz.

## Gerekenler

- Windows 10 veya 11
- Google Chrome
- Python 3.12 veya üzeri
- TypeSafe Jev API anahtarı ya da Jev erişimli Vercel AI Gateway anahtarı
- MCP destekleyen bir ana ajan: Codex, Grok, Claude Desktop vb.

## İlk kurulum

1. GitHub'da **Code → Download ZIP** seçin ve ZIP'i normal bir klasöre çıkarın.
2. `Install-Windows.cmd` dosyasına çift tıklayın. Kurucu sanal ortamı ve tüm
   Python paketlerini hazırlar, uzantının yerel bağlantı anahtarını üretir ve
   `mcp-config.json` dosyasını oluşturur.
3. `Ayarlar.cmd` dosyasını açın. **Save/change API key** seçeneğiyle anahtarı
   girin. Yazdığınız karakterler görünmez. Anahtar düz metin kaydedilmez;
   Windows DPAPI ile yalnız mevcut Windows hesabına bağlı şekilde saklanır.
4. Chrome'da açılan `chrome://extensions` sayfasında **Geliştirici modu**nu
   açın. **Paketlenmemiş öğe yükle / Load unpacked** seçeneğini seçip projenin
   `extension` klasörünü gösterin.
5. MCP istemcinizin ayarına oluşturulan `mcp-config.json` içindeki sunucuyu
   ekleyin ve istemciyi yeniden başlatın.

Klasörü daha sonra taşırsanız `Ayarlar.cmd` içinden MCP yapılandırmasını yeniden
üretin ve istemcinizdeki yolu güncelleyin.

## Kullanım

Chrome'u ve kullanmak istediğiniz web sayfasını siz açın. Sonra ajana örneğin:

> Jev Browser kullanarak açık otel sekmesinde Design kategorisini seç ve ücretsiz
> iptal filtresini aç. Sonucu doğrula.

deyin. Ajan önce mevcut sekmeleri listeler, görevin hedefini ve izin verilen site
kökenini sınırlar, sonra Jev döngüsünü başlatır. Jev işlem ve hedefi seçer. Serbest
metin gerektiğinde ana ajan yalnız istenen alan değerini sağlar. Görev sonundaki
metin ve ekran görüntüsü ayrıca doğrulanır.

## Güncelleme

Yeni dosyaları aynı klasöre çıkarın, `Install-Windows.cmd` dosyasını yeniden
çalıştırın ve `chrome://extensions` sayfasındaki **Jev Browser Bridge** kartında
yenile simgesine basın. Uzantı sürümü değiştiğinde bu yenileme gerekir.

## Sorun giderme

- **needs_extension:** Uzantı kapalıdır veya yenilenmemiştir. Uzantıyı etkinleştirip
  kartındaki yenile simgesine basın.
- **No Jev API key is configured:** `Ayarlar.cmd` ile anahtarı kaydedin.
- **Sekme görünmüyor:** Normal bir `http://` veya `https://` sayfasını açın. Chrome
  iç ayar sayfaları bilerek listelenmez.
- **Araçlar görünmüyor:** MCP istemcisini tamamen kapatıp yeniden açın.
- **Ekran görüntüsü izni:** Uzantıyı güncelledikten sonra kartındaki yenile
  simgesine basın. Sürüm 1.0.1 veya üzeri olmalıdır.

## Doğrulanan durum

20 Eylül 2026'da gerçek Chrome üzerinde uzantı bağlantısı ve mevcut sekme
gözlemi doğrulandı. Yerel otel testinde Jev, **Design** kategorisini ve
**Free cancellation** filtresini iki eylemde uyguladı; sayfada iki eşleşen yer
göründü. Karar döngüsü 2,9 saniye sürdü. Bu tek bir yerel görev ölçümüdür ve her
web sitesi için aynı hız veya başarı garantisi değildir.

Bu proje, `browser-use/jev-ultrafast` tabanlı bağımsız bir Windows uyarlamasıdır;
değiştirilmemiş resmi bir TypeSafe Windows ürünü değildir.
