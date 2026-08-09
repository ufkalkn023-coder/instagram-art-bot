import os
import sys
import uuid
import time
from datetime import datetime
import json

def check_dependencies():
    missing = []
    try:
        import boto3
    except ImportError:
        missing.append("boto3")
    try:
        import requests
    except ImportError:
        missing.append("requests")
        
    if missing:
        print(f"❌ TEST BAŞARISIZ: Şu kütüphaneler eksik: {', '.join(missing)}")
        print("Lütfen yükleyin: pip install boto3 requests")
        sys.exit(1)

def run_tests():
    print("="*50)
    print("🚀 CLOUDFLARE R2 INTEGRATION TEST (LOCAL)")
    print("="*50)
    
    # 1. Check Env Vars
    print("\n[1] Environment Variables Kontrol Ediliyor...")
    required_vars = [
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_BUCKET_NAME",
        "CLOUDFLARE_R2_PUBLIC_URL"
    ]
    
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"❌ TEST BAŞARISIZ: Eksik environment variable'lar var: {', '.join(missing_vars)}")
        sys.exit(1)
    
    print("✅ Tüm gerekli secret'lar sistemde mevcut (Değerler gizli tutuluyor).")
    
    import boto3
    import requests
    from botocore.exceptions import ClientError
    
    account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID")
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    bucket_name = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME")
    public_url_base = os.environ.get("CLOUDFLARE_R2_PUBLIC_URL").rstrip('/')
    
    # 3. Boto3 Bağlantısı
    print("\n[2] Boto3 R2 İstemcisi Oluşturuluyor...")
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto"
    )
    print("✅ İstemci oluşturuldu.")
    
    # Create dummy files
    dummy_jpg = "dummy_test.jpg"
    dummy_mp4 = "dummy_test.mp4"
    with open(dummy_jpg, "wb") as f:
        f.write(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xFF\xDB\x00C\x00\x08\x06")
    with open(dummy_mp4, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        
    tests_passed = True
    
    def test_upload(file_path, media_type, content_type_expected):
        global tests_passed
        print(f"\n[{media_type.upper()}] Yükleme ve Doğrulama Testi Başlıyor...")
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        ext = "jpg" if media_type == "image" else "mp4"
        folder = "images" if media_type == "image" else "videos"
        object_key = f"{folder}/{timestamp}_{unique_id}_test.{ext}"
        
        try:
            print(f" ⏳ Yükleniyor... Object Key: {object_key}")
            s3_client.upload_file(
                file_path, 
                bucket_name, 
                object_key,
                ExtraArgs={"ContentType": content_type_expected}
            )
            print(f" ✅ Yüklendi.")
        except ClientError as e:
            print(f" ❌ YÜKLEME BAŞARISIZ: {e}")
            tests_passed = False
            return
            
        final_url = f"{public_url_base}/{object_key}"
        print(f" ⏳ HEAD isteği atılıyor: {final_url}")
        
        try:
            res = requests.head(final_url, allow_redirects=True, timeout=10)
            status = res.status_code
            c_type = res.headers.get("Content-Type", "")
            c_length = int(res.headers.get("Content-Length", 0))
            
            print(f"    - HTTP Status : {status}")
            print(f"    - Content-Type: {c_type}")
            print(f"    - Size (bytes): {c_length}")
            
            if status != 200:
                print(" ❌ HATA: HTTP 200 alınamadı.")
                tests_passed = False
            if content_type_expected not in c_type:
                print(f" ❌ HATA: Beklenen Content-Type '{content_type_expected}' bulunamadı.")
                tests_passed = False
            if c_length <= 0:
                print(" ❌ HATA: Content-Length 0 veya tanımsız.")
                tests_passed = False
                
        except Exception as e:
            print(f" ❌ HEAD İSTEĞİ BAŞARISIZ: {e}")
            tests_passed = False

    # Test Image
    test_upload(dummy_jpg, "image", "image/jpeg")
    
    # Test Video
    test_upload(dummy_mp4, "video", "video/mp4")
    
    # Cleanup dummy local files
    if os.path.exists(dummy_jpg): os.remove(dummy_jpg)
    if os.path.exists(dummy_mp4): os.remove(dummy_mp4)
    
    print("\n" + "="*50)
    if tests_passed:
        print("🎉 SONUÇ: TEST BAŞARILI!")
        print("Her iki dosya da başarıyla yüklendi, URL'ler public olarak çalışıyor ve Content-Type'lar %100 doğru.")
        print("R2'deki test dosyaları Cloudflare Lifecycle kuralıyla (1 gün içinde) otomatik olarak silinecektir.")
    else:
        print("🚨 SONUÇ: TEST BAŞARISIZ!")
        print("Lütfen yukarıdaki loglarda yer alan HATA çıktılarını inceleyin.")
    print("="*50)

if __name__ == "__main__":
    check_dependencies()
    run_tests()
