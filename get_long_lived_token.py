import sys
import requests

GRAPH_API_VERSION = "v22.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def exchange_short_token_for_long_lived(app_id: str, app_secret: str, short_token: str) -> str:
    """
    Exchanges a 1-2 hour Graph API Explorer short-lived user token
    for a 60-day Long-Lived Access Token.
    """
    url = f"{BASE_URL}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token
    }

    print("\n⏳ Kısa ömürlü token'ı 60 günlük Long-Lived Token'a dönüştürüyorum...")
    res = requests.get(url, params=params, timeout=15)
    data = res.json()

    if res.status_code == 200 and "access_token" in data:
        long_token = data["access_token"]
        expires_in = data.get("expires_in", 0)
        days = round(expires_in / 86400, 1)

        print("\n" + "=" * 60)
        print("✅ 60 Günlük Long-Lived Token Oluşturuldu!")
        print("=" * 60)
        print(f"⏰ Geçerlilik: ~{days} gün")
        print(f"\n{long_token}\n")
        return long_token
    else:
        error_msg = data.get("error", {}).get("message", res.text)
        print(f"\n❌ Long-Lived Token oluşturulamadı:\n{error_msg}\n")
        sys.exit(1)


def get_permanent_page_token(long_lived_user_token: str) -> str:
    """
    Uses a long-lived user token to get a PERMANENT (never-expiring)
    Page Access Token. This is the recommended approach for bots.
    """
    print("\n⏳ Kalıcı Page Access Token alınıyor...")

    # Step 1: Get user's pages
    url = f"{BASE_URL}/me/accounts"
    params = {
        "access_token": long_lived_user_token
    }
    res = requests.get(url, params=params, timeout=15)
    data = res.json()

    if res.status_code != 200 or "data" not in data:
        error_msg = data.get("error", {}).get("message", res.text)
        print(f"\n❌ Sayfalar alınamadı:\n{error_msg}")
        print("💡 Token'ınızda 'pages_show_list' ve 'pages_read_engagement' izinleri olduğundan emin olun.")
        return ""

    pages = data["data"]
    if not pages:
        print("\n❌ Hesabınıza bağlı Facebook Sayfası bulunamadı!")
        print("💡 Instagram hesabınızın bir Facebook Sayfasına bağlı olması gerekiyor.")
        return ""

    # Step 2: Let user pick a page if multiple
    if len(pages) == 1:
        selected_page = pages[0]
    else:
        print(f"\n📄 {len(pages)} Facebook Sayfası bulundu:\n")
        for i, page in enumerate(pages, 1):
            print(f"  {i}. {page['name']} (ID: {page['id']})")
        print()
        while True:
            try:
                choice = int(input(f"Instagram'a bağlı sayfayı seçin (1-{len(pages)}): ").strip())
                if 1 <= choice <= len(pages):
                    selected_page = pages[choice - 1]
                    break
            except ValueError:
                pass
            print("Geçersiz seçim, tekrar deneyin.")

    page_token = selected_page["access_token"]
    page_name = selected_page["name"]
    page_id = selected_page["id"]

    # Step 3: Verify the token is permanent by debugging it
    debug_url = f"{BASE_URL}/debug_token"
    debug_params = {
        "input_token": page_token,
        "access_token": page_token
    }
    debug_res = requests.get(debug_url, params=debug_params, timeout=15)
    debug_data = debug_res.json().get("data", {})
    expires_at = debug_data.get("expires_at", 0)

    print("\n" + "=" * 60)
    print("✅ KALİCİ PAGE ACCESS TOKEN (Süresiz!)")
    print("=" * 60)
    print(f"📄 Sayfa: {page_name} (ID: {page_id})")
    if expires_at == 0:
        print("⏰ Süre: ♾️  SINÏRSIZ — Asla sona ermez!")
    else:
        print(f"⏰ Süre sonu: {expires_at}")
    print(f"\n{page_token}\n")
    print("=" * 60)

    return page_token


def verify_instagram_account(token: str):
    """Verifies that the token can access an Instagram Business account."""
    # Try to find Instagram account linked to user's pages
    url = f"{BASE_URL}/me/accounts"
    params = {"access_token": token, "fields": "id,name,instagram_business_account"}
    res = requests.get(url, params=params, timeout=15)
    data = res.json()

    if res.status_code != 200:
        return

    for page in data.get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            ig_id = ig["id"]
            print(f"\n📸 Instagram Business Account bulundu!")
            print(f"   Instagram Account ID: {ig_id}")
            print(f"   Bağlı Facebook Sayfası: {page['name']}")
            print(f"\n💡 GitHub Secrets'a eklemen gerekenler:")
            print(f"   INSTAGRAM_ACCOUNT_ID = {ig_id}")
            print(f"   INSTAGRAM_ACCESS_TOKEN = (yukarıdaki kalıcı token)")
            return

    print("\n⚠️  Instagram Business hesabı bulunamadı. Sayfanıza Instagram hesabı bağlı mı kontrol edin.")


if __name__ == "__main__":
    print("=" * 60)
    print(" 🔑 Instagram Bot — Kalıcı Access Token Oluşturucu")
    print("=" * 60)
    print()
    print("Bu araç kısa ömürlü token'ınızı SÜRESİZ kalıcı token'a çevirir.")
    print("Graph API Explorer'dan aldığınız token'ı girin.")
    print()

    if len(sys.argv) == 4:
        app_id = sys.argv[1]
        app_secret = sys.argv[2]
        short_token = sys.argv[3]
    else:
        app_id = input("Meta App ID: ").strip()
        app_secret = input("Meta App Secret: ").strip()
        short_token = input("Kısa ömürlü Access Token: ").strip()

    if not app_id or not app_secret or not short_token:
        print("❌ Hata: Tüm parametreler eksiksiz girilmelidir!")
        sys.exit(1)

    # Step 1: Short-lived → Long-lived (60 days)
    long_token = exchange_short_token_for_long_lived(app_id, app_secret, short_token)

    # Step 2: Long-lived → Permanent Page Token
    print("\n" + "-" * 60)
    print("📌 Şimdi kalıcı (süresiz) Page Token oluşturulacak...")
    print("-" * 60)

    page_token = get_permanent_page_token(long_token)

    # Step 3: Verify Instagram account access
    if page_token:
        verify_instagram_account(page_token)
        print("\n" + "=" * 60)
        print("🎯 YAPILACAKLAR:")
        print("=" * 60)
        print("1. Yukarıdaki KALİCİ token'ı kopyala")
        print("2. GitHub → Settings → Secrets → INSTAGRAM_ACCESS_TOKEN'a yapıştır")
        print("3. Artık token asla sona ermeyecek! ✅")
        print("=" * 60 + "\n")
    else:
        print("\n⚠️  Kalıcı token oluşturulamadı. 60 günlük token'ı kullanabilirsin.")
        print("GitHub Secrets'a yukarıdaki 60 günlük token'ı ekle.\n")
