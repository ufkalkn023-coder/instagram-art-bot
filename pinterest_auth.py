import urllib.parse
import requests
import base64
import sys

print("="*50)
print("📌 PINTEREST OAUTH2 TOKEN OLUŞTURUCU")
print("="*50)
print("1. https://developers.pinterest.com/apps/ adresine gidin ve bir App oluşturun.")
print("2. App ayarlarından 'Redirect URIs' kısmına 'https://localhost/' ekleyin.")
print("3. App ID ve App Secret değerlerinizi kopyalayın.\n")

app_id = input("PINTEREST_APP_ID girin: ").strip()
app_secret = input("PINTEREST_APP_SECRET girin: ").strip()

redirect_uri = "https://localhost/"
scope = "boards:read,boards:write,pins:read,pins:write"

auth_url = f"https://www.pinterest.com/oauth/?client_id={app_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code&scope={scope}"

print("\n" + "-"*50)
print("Lütfen aşağıdaki linki tarayıcınızda açın ve Pinterest'e giriş yapıp izin verin:")
print(auth_url)
print("-" * 50)
print("\nİzin verdikten sonra tarayıcınız sizi boş/hata veren bir 'localhost' sayfasına yönlendirecek.")
print("Lütfen o sayfanın URL'sini (adres çubuğundaki her şeyi) kopyalayın.\n")

redirected_url = input("Yönlendirilen tam URL'yi yapıştırın: ").strip()

try:
    # Extract the 'code' parameter from the URL
    parsed_url = urllib.parse.urlparse(redirected_url)
    params = urllib.parse.parse_qs(parsed_url.query)
    auth_code = params.get('code', [None])[0]
    
    if not auth_code:
        print("HATA: URL'nin içinde 'code' parametresi bulunamadı.")
        sys.exit(1)
        
    print("\nYetki kodu alındı, Refresh Token oluşturuluyor...")
    
    auth_str = base64.b64encode(f"{app_id}:{app_secret}".encode('utf-8')).decode('utf-8')
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri
    }
    
    res = requests.post("https://api.pinterest.com/v5/oauth/token", headers=headers, data=data)
    
    if res.status_code == 200:
        tokens = res.json()
        print("\n✅ İŞLEM BAŞARILI! Lütfen aşağıdaki değerleri GitHub Secrets'a ekleyin:\n")
        print("PINTEREST_APP_ID:", app_id)
        print("PINTEREST_APP_SECRET:", app_secret)
        print("PINTEREST_REFRESH_TOKEN:", tokens.get("refresh_token"))
        print("\nNOT: PINTEREST_BOARD_ID değerini ise Pinterest'te oluşturduğunuz panonun URL'sinden alabilirsiniz.")
    else:
        print(f"\n❌ HATA OLUŞTU: HTTP {res.status_code}")
        print(res.text)

except Exception as e:
    print(f"Bir hata oluştu: {e}")
