import sys
import requests

def exchange_short_token_for_long_lived(app_id: str, app_secret: str, short_token: str):
    """
    Exchanges a 1-2 hour Graph API Explorer short-lived user token
    for a 60-day Long-Lived Access Token.
    """
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token
    }

    print("\n⏳ Converting short-lived access token to 60-day Long-Lived Token...")
    res = requests.get(url, params=params, timeout=15)
    data = res.json()

    if res.status_code == 200 and "access_token" in data:
        long_token = data["access_token"]
        expires_in = data.get("expires_in", 0)
        days = round(expires_in / 86400, 1)

        print("\n" + "="*60)
        print("✅ SUCCESS! 60-Day Long-Lived Access Token Generated:")
        print("="*60)
        print(f"\n{long_token}\n")
        print("="*60)
        print(f"🔑 Valid for approximately {days} days.")
        print("📋 Copy this token and add it to your GitHub Repository Secrets as INSTAGRAM_ACCESS_TOKEN.")
        print("="*60 + "\n")
    else:
        error_msg = data.get("error", {}).get("message", res.text)
        print(f"\n❌ Error generating Long-Lived Token:\n{error_msg}\n")

if __name__ == "__main__":
    print("="*60)
    print(" 🔑 Meta Graph API Long-Lived Access Token Converter")
    print("="*60)

    if len(sys.argv) == 4:
        app_id = sys.argv[1]
        app_secret = sys.argv[2]
        short_token = sys.argv[3]
    else:
        app_id = input("Meta App ID'inizi girin: ").strip()
        app_secret = input("Meta App Secret'ınızı girin: ").strip()
        short_token = input("Kısa ömürlü Access Token'ınızı girin: ").strip()

    if not app_id or not app_secret or not short_token:
        print("❌ Hata: Tüm parametreler eksiksiz girilmelidir!")
        sys.exit(1)

    exchange_short_token_for_long_lived(app_id, app_secret, short_token)
