# Mac kurulumu

Bu yerel uyarlama mevcut Chrome profilindeki sekmeleri eklenti üzerinden kullanır.
Python/MCP/eklenti çekirdeği zaten platformdan büyük ölçüde bağımsızdı; önceki
kurulum ve anahtar saklama yardımcıları Windows içindi.

1. `./Install-Mac.command` çalıştır (Python ortamını uv kurar; uv önceden kurulu olmalı).
2. Anahtar için `uv run python scripts/setup_macos.py --set-key vercel` çalıştır.
   Alternatif: MCP sunucusuna `AI_GATEWAY_API_KEY` veya `TYPESAFE_API_KEY` ortam değişkeni ver.
3. Chrome'da `chrome://extensions` aç, Developer mode etkinleştir, Load unpacked
   ile bu reponun `extension` klasörünü seç.
4. Üretilen `mcp-config.json` içindeki sunucuyu MCP istemcisine ekle. Codex için
   komut `.venv/bin/python`, tek argüman bu reponun `chrome_mcp.py` tam yoludur.
5. MCP araçları yüklendiğinde `jev_browser_connect` çağır; `connected=true`
   dönmeden tarayıcı bağlantısını çalışıyor sayma.

Kurucu Chrome açmaz, aktif sekmeyi değiştirmez ve Codex/Grok genel ayarlarını yazmaz.
Mac anahtarı `config/macos-key.json` içinde 0600, dizin 0700 izinleriyle tutulur.
Bu dosya şifreli Keychain kaydı değildir; Git dışında tutulur. Windows DPAPI yolu korunur.
Köprü anahtarı ile eklenti yerel yapılandırması da Mac'te 0600 izinlidir.

## Arka planda sekme kullanımı

Yeni sayfa için `jev_browser_open_tab(url, active=false)` kullanılır; varsayılan budur.
`jev_browser_run` artık `capture_final=false` varsayılanıyla çalışır.
Eski otomatik sonuç ekran görüntüsü `chrome.tabs.update(active=true)` çağırdığı için
kullanıcının izlediği sekmeyi değiştirebiliyordu. Görsel kanıt gerekiyorsa
`capture_final=true` açıkça seçilebilir; bu hedef sekmeyi aktif eder.

Varsayılan sonuç doğrulaması URL ve DOM metni üzerinden yapılır. Modelin DONE
kararı tek başına başarı değildir. Sayfa JavaScript'inin açtığı pencereler,
oturum açma ekranları ve tarayıcı istemleri için tam görünmezlik garantisi yoktur.
Aynı hedef sekmeyi kullanıcı ve ajan eşzamanlı düzenlememeli.

Vercel adaptörü güncel evaluation protokolünü kullanır; güven değeri native
`providerMetadata.typesafe.confidence` alanından gelir. Eksik güven eylemi engeller.

Kurulum sonrası bağlantıyı `jev_browser_connect` ile, görev sonucunu son URL ve
DOM metniyle doğrula. Arka plan davranışını kullanılan Chrome profilinde ayrıca
kontrol et. Otomatik testler ücretli API çağırmaz.
