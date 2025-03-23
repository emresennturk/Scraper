import traceback
from flask import Flask, request, jsonify
import mysql.connector
import concurrent.futures
import queue
import threading
import google.generativeai as genai
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from dotenv import load_dotenv
import hashlib
import os
import re
import requests
import json

from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

app = Flask(__name__)

# Google Gemini API yapılandırması
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
category_model = genai.GenerativeModel("gemini-1.5-flash")

genai.configure(api_key=os.getenv("GOOGLE_API_KEY_2"))
analysis_model = genai.GenerativeModel("gemini-1.5-flash")

OLLAMA_API_URL = "http://localhost:11434/api/generate"
TRANSLATE_API_URL = "http://localhost:5001/translate"
# Global önbellek (Gemini API kotasını aşmamak için)
category_cache = {}


def create_driver():
    try:
        options = Options()
        options.headless = True
        # Gerçek bir tarayıcı User-Agent başlığı ekle
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36")
        # Bot algılama mekanizmalarını atlat
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        service = Service("C:\\Users\\comba\\OneDrive\\Masaüstü\\chromedriver-win64\\chromedriver.exe")
        return webdriver.Chrome(service=service, options=options)
    except Exception as err:
        traceback.print_exc()
        print(f'Error in create_driver: {str(err)}')
        return None


# Veritabanı bağlantı fonksiyonu
def connect_db():
    try:
        return mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
        )
    except Exception as err:
        print(f'Error in connect_db: {str(err)}')
        return None


def update_product_price(url, unique_key):
    print(f"Updating price for unique_key: {unique_key}")
    driver = create_driver()
    db = connect_db()
    if not db or not driver:
        if db:
            db.close()
        if driver:
            driver.quit()
        print("Database or driver initialization failed.")
        return None
    cursor = db.cursor()
    try:
        # Eski fiyatı al
        cursor.execute("SELECT fiyat FROM urunler WHERE uniqe_key = %s", (unique_key,))
        result = cursor.fetchone()
        old_price = result[0] if result else None
        print(f"Eski fiyat: {old_price}")

        if old_price is None:
            print("Eski fiyat bulunamadı.")
            return None

        # Yeni fiyatı çek
        driver.get(url)
        time.sleep(2)
        if "amazon.com.tr" in url:
            price_element = driver.find_element(By.CSS_SELECTOR, ".a-price-whole")
            new_price = float(price_element.text.replace('.', '').replace(',', '.'))
        elif "trendyol.com" in url:
            price_element = driver.find_element(By.CLASS_NAME, "prc-dsc")
            price_text = price_element.text.replace(" TL", "").replace(".", "").replace(",", ".")
            new_price = float(price_text)
        else:
            print("Geçersiz URL formatı.")
            return None
        print(f"Yeni fiyat: {new_price}")

        # Fiyatı güncelle
        cursor.execute("UPDATE urunler SET fiyat = %s WHERE uniqe_key = %s", (new_price, unique_key))
        db.commit()
        print("Fiyat güncellendi.")

        # Fiyat düşüşünü kontrol et
        if new_price < old_price:
            print("Fiyat düşüşü tespit edildi.")
            return {"unique_key": unique_key, "old_price": old_price, "new_price": new_price}
        print("Fiyat düşüşü yok.")
        return None
    except Exception as err:
        print(f'Error updating product price: {str(err)}')
        return None
    finally:
        cursor.close()
        db.close()
        if driver:
            driver.quit()


def update_all_prices():
    db = connect_db()
    if not db:
        return
    cursor = db.cursor()
    cursor.execute("SELECT url, uniqe_key FROM urunler")
    products = cursor.fetchall()
    price_changes = []
    for url, unique_key in products:
        change = update_product_price(url, unique_key)
        if change:
            price_changes.append(change)
    cursor.close()
    db.close()
    if price_changes:
        import requests
        try:
            response = requests.post("http://localhost:8080/api/price-drop", json=price_changes)
            print(f"Price drop notification sent: {response.status_code}")
        except Exception as err:
            print(f"Error sending price drop notification: {str(err)}")


def test_price_update():
    unique_key = "test_uniquekey_1"
    url = "https://www.amazon.com.tr/test-urun-1"
    change = update_product_price(url, unique_key)
    if change:
        import requests
        response = requests.post("http://localhost:8080/api/price-drop", json=[change])
        print(f"Price drop notification sent: {response.status_code}")


@app.route('/update_product', methods=['POST'])
def update_product():
    url = request.json.get('url')
    unique_key = request.json.get('uniqueKey')
    if not url or not unique_key:
        return jsonify({"error": "Missing required parameters"}), 400

    change = update_product_price(url, unique_key)
    if change is None:
        return jsonify({"error": "Failed to update product price or no price drop detected"}), 500

    # Fiyat düşüşü varsa Java backend'e bildirim gönder
    if change:
        import requests
        try:
            response = requests.post("http://localhost:8080/api/price-drop", json=[change])
            print(f"Price drop notification sent: {response.status_code}")
            return jsonify({"success": True, "new_price": change["new_price"]})
        except Exception as err:
            print(f"Error sending price drop notification: {str(err)}")
            return jsonify(
                {"error": "Price updated but failed to notify backend", "new_price": change["new_price"]}), 500
    else:
        return jsonify({"success": True, "message": "Price updated but no price drop detected"})


def scrape_amazon(keyword):
    driver = create_driver()
    if driver is None:
        print("Driver başlatılamadı, Amazon scraping iptal edildi.")
        return []
    products = []
    try:
        driver.get("https://www.amazon.com.tr/")
        print("Amazon sayfası yüklendi.")
        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "twotabsearchtextbox"))
        )
        print("Arama kutusu bulundu.")
        search_box.send_keys(keyword)
        search_box.submit()
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.s-main-slot div.s-result-item"))
        )
        print("Arama sonuçları yüklendi.")
        entries = driver.find_elements(By.CSS_SELECTOR, "div.s-main-slot div.s-result-item")
        for entry in entries[:10]:
            product_info = {"urun_adi": None, "fiyat": None, "url": None, "resim_url": None, "satici": "Amazon"}
            try:
                product_info["urun_adi"] = entry.find_element(By.CSS_SELECTOR, "h2").text
                whole_price = entry.find_element(By.CSS_SELECTOR, ".a-price-whole").text
                product_info["fiyat"] = float(whole_price.replace('.', '').replace(',', '.'))
                product_info["url"] = "https://www.amazon.com.tr" + entry.find_element(By.CSS_SELECTOR,
                                                                                       "a.a-link-normal").get_dom_attribute(
                    "href")
                product_info["resim_url"] = entry.find_element(By.CSS_SELECTOR, "img.s-image").get_dom_attribute("src")
                product_info["hash"] = hashlib.md5(
                    f"{product_info['urun_adi']}{product_info['fiyat']}".encode()).hexdigest()
                products.append(product_info)
            except Exception as err:
                print(f'Error in scrape_amazon entry processing: {str(err)}')
    except Exception as err:
        print(f'Error in scrape_amazon: {str(err)}')
    finally:
        if driver is not None:
            driver.quit()
    return products


def scrape_trendyol(keyword):
    driver = create_driver()
    if driver is None:
        print("Driver başlatılamadı, Trendyol scraping iptal edildi.")
        return []
    products = []
    try:
        driver.get("https://www.trendyol.com")
        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "V8wbcUhU"))
        )
        search_box.send_keys(keyword)
        search_box.send_keys(Keys.RETURN)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.p-card-wrppr"))
        )
        product_elements = driver.find_elements(By.CSS_SELECTOR, "div.p-card-wrppr")
        for product in product_elements[:10]:
            product_info = {"urun_adi": None, "fiyat": None, "url": None, "resim_url": None, "satici": "Trendyol"}
            try:
                brand = product.find_element(By.CLASS_NAME, "prdct-desc-cntnr-ttl").text
                product_name = product.find_element(By.CLASS_NAME, "prdct-desc-cntnr-name").text
                description = product.find_element(By.CLASS_NAME, "product-desc-sub-text").text
                product_info["urun_adi"] = f"{brand} {product_name} {description}"
                try:
                    fiyat_element = product.find_element(By.CSS_SELECTOR, ".price-item.discounted")
                except:
                    try:
                        fiyat_element = product.find_element(By.CSS_SELECTOR, ".price-item")
                    except:
                        raise Exception("Fiyat elementi bulunamadı.")
                fiyat_text = fiyat_element.text
                fiyat_number = fiyat_text.replace(" TL", "").replace(".", "").replace(",", ".")
                product_info["fiyat"] = float(fiyat_number)
                product_info["url"] = "https://www.trendyol.com" + product.find_element(By.TAG_NAME,
                                                                                        "a").get_dom_attribute("href")
                product_info["resim_url"] = product.find_element(By.TAG_NAME, "img").get_dom_attribute("src")
                product_info["hash"] = hashlib.md5(
                    f"{product_info['urun_adi']}{product_info['fiyat']}".encode()).hexdigest()
                products.append(product_info)
            except Exception as err:
                print(f'Error in scrape_trendyol entry processing: {str(err)}')
    finally:
        if driver is not None:
            driver.quit()
    return products


def get_existing_categories():
    db = connect_db()
    if not db:
        print("Veritabanı bağlantısı başarısız.")
        return []
    cursor = db.cursor()
    try:
        cursor.execute("SELECT kategori_adi FROM kategoriler")
        categories = [row[0] for row in cursor.fetchall()]
        return categories
    except Exception as err:
        print(f"Veritabanı kategori sorgu hatası: {str(err)}")
        return []
    finally:
        cursor.close()
        db.close()


def get_category(product_name):
    product_name_lower = product_name.lower()
    for cached_key, category in category_cache.items():
        if cached_key in product_name_lower:
            return category
    existing_categories = get_existing_categories()
    prompt = f"""
    Product: {product_name} Gelen ürün girişini, ürün adına göre kategorilendir. Kategoriler Türkçe olacak ve 
    yalnızca bir ana kategori kullanılacak, alt kategori olmayacak. Benzer ürünler (örneğin, aynı türden ürünler) 
    aynı kategoriye atanmalı. Aşağıda mevcut kategori listesi verilmiştir. Eğer ürün mevcut kategorilerden birine 
    uyuyorsa, o kategoriyi kullan; eğer uymuyorsa, yeni bir kategori öner (her zaman tekil formda, 
    örneğin "Ayakkabılar" değil "Ayakkabı"). Mevcut kategoriler: 
{", ".join(existing_categories) if existing_categories else "Henüz kategori yok."}
    Kategoriyi belirlerken ürünün genel türünü dikkate al ve marka veya modele özgü ayrı kategoriler oluşturma. 
    Örneğin:
    - "iPhone 15 128GB" -> "Akıllı Telefon"
    - "Samsung S24 128GB" -> "Akıllı Telefon"
    - "Logitech G502 Mouse" -> "Bilgisayar Aksesuarı"
    - "Casper Excalibur Gaming Laptop" -> "Dizüstü Bilgisayar"
    - "Nike Air Max 90" -> "Ayakkabı"
    Cevap yalnızca kategori adından oluşmalı ve tekil formda olmalı.
    """
    for attempt in range(3):
        try:
            response = category_model.generate_content(prompt)  # 1 numaralı API kullanıyor
            category = response.text.strip()
            if category.endswith("lar") or category.endswith("ler"):
                category = category[:-3]
            if category not in existing_categories and category != "Uncategorized":
                db = connect_db()
                if db:
                    cursor = db.cursor()
                    try:
                        cursor.execute("INSERT INTO kategoriler (kategori_adi) VALUES (%s)", (category,))
                        db.commit()
                        print(f"Yeni kategori eklendi: {category}")
                    except Exception as err:
                        print(f"Kategori ekleme hatası: {str(err)}")
                    finally:
                        cursor.close()
                        db.close()
            key = next((word for word in product_name_lower.split() if word in category.lower()), product_name_lower)
            category_cache[key] = category
            return category
        except Exception as err:
            if "429" in str(err):
                print(f"Kategorilendirme kota aşımı, {attempt + 1}. deneme başarısız. 5 saniye bekleniyor...")
                time.sleep(5)
            else:
                print(f'Error in get_category: {str(err)}')
                return "Uncategorized"
    print("Kategorilendirme: Tüm denemeler başarısız, kota limiti aşıldı.")
    return "Uncategorized"


category_queue = queue.Queue()


def process_categories():
    while True:
        product = category_queue.get()
        if product is None:
            break
        db = connect_db()
        if not db:
            category_queue.task_done()
            continue
        cursor = db.cursor()
        try:
            category_name = get_category(product["urun_adi"])
            cursor.execute("SELECT id FROM kategoriler WHERE kategori_adi = %s", (category_name,))
            category_result = cursor.fetchone()
            category_id = category_result[0]
            cursor.execute(
                "INSERT INTO urunler_kategoriler (urun_id, kategori_id) VALUES (%s, %s)",
                (product["urun_id"], category_id)
            )
            db.commit()
        except Exception as err:
            print(f'Error in process_categories: {str(err)}')
        finally:
            cursor.close()
            db.close()
            category_queue.task_done()


category_thread = threading.Thread(target=process_categories, daemon=True)
category_thread.start()

# Mevcut kuyruktan sonra ekleyin
analysis_queue = queue.Queue()


@app.route('/analyze_ollama', methods=['POST'])
def analyze_product():
    try:
        data = request.get_json()
        product_name = data.get('product_name')

        if not product_name:
            return jsonify({"error": "Product name is required"}), 400

        # Ollama API'sine İngilizce istek gönder
        prompt = f"""
        Product: {product_name}

        Analyze the advantages, disadvantages, and estimated market price of this product in turkey.
        Provide the response in the following format:

        Advantages: [Advantage 1, Advantage 2, Advantage 3]
        Disadvantages: [Disadvantage 1, Disadvantage 2]
        Estimated Price: XXXX USD

        Provide the response only in the above format, no extra explanation.
        """

        payload = {
            "model": "deepseek-r1:1.5b",
            "prompt": prompt,
            "stream": False
        }

        response = requests.post(OLLAMA_API_URL, json=payload)
        response.raise_for_status()

        raw_response = response.text
        print("Raw response:", raw_response)

        # Anlamlı metni çıkar ve temizle (İngilizce)
        english_text = extract_meaningful_text(raw_response)

        # İngilizce metni Türkçe'ye çevir
        turkish_text = translate_to_turkish(english_text)

        # JSON yanıtı döndür
        return jsonify({
            "raw_response": raw_response,
            "english_text": english_text,
            "meaningful_text": turkish_text
        })

    except requests.RequestException as e:
        return jsonify({"error": "Ollama API error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


def extract_meaningful_text(raw_response):
    try:
        # JSON'ı ayrıştır
        json_data = json.loads(raw_response)
        response_text = json_data.get("response", "")

        if not response_text:
            return "No meaningful text extracted."

        # <think> bloğunu ve gereksiz kısımları kaldır
        cleaned_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL).strip()

        # Metni formatla
        formatted_text = format_text(cleaned_text)
        return formatted_text

    except json.JSONDecodeError as e:
        return f"Text parsing error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


def format_text(text):
    # Metni satır satır ayrıştır ve gereksiz boşlukları kaldır
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    formatted_lines = []

    for line in lines:
        if line.startswith("Advantages:"):
            advantages = line.replace("Advantages:", "").strip()
            advantages_list = [item.strip() for item in advantages.strip('[]').split(',') if item.strip()]
            formatted_lines.append("Advantages: " + ", ".join(advantages_list))
        elif line.startswith("Disadvantages:"):
            disadvantages = line.replace("Disadvantages:", "").strip()
            disadvantages_list = [item.strip() for item in disadvantages.strip('[]').split(',') if item.strip()]
            formatted_lines.append("Disadvantages: " + ", ".join(disadvantages_list))
        elif line.startswith("Estimated Price:"):
            price = line.replace("Estimated Price:", "").strip()
            formatted_lines.append(f"Estimated Price: {price}")
        else:
            formatted_lines.append(line)

    return "\n".join(formatted_lines) if formatted_lines else text.strip()


def translate_to_turkish(english_text):
    try:
        payload = {
            "q": english_text,
            "source": "en",
            "target": "tr",
            "format": "text"
        }
        response = requests.post(TRANSLATE_API_URL, json=payload)
        response.raise_for_status()

        translated_data = response.json()
        translated_text = translated_data.get("translatedText", english_text)

        # Çeviriyi Türkçeye uygun şekilde formatla
        translated_text = translated_text.replace("Advantages:", "Avantajlar:")
        translated_text = translated_text.replace("Disadvantages:", "Dezavantajlar:")
        translated_text = translated_text.replace("Estimated Price:", "Tahmini Fiyat:")
        translated_text = translated_text.replace("USD", "TL")

        return translated_text

    except requests.RequestException as e:
        print(f"Translation error: {str(e)}")
        return english_text  # Hata olursa İngilizce metni döndür
    except Exception as e:
        print(f"Translation processing error: {str(e)}")
        return english_text


@app.route('/analyze_gemini', methods=['POST'])
def analyze_product_gemini():
    try:
        data = request.get_json()
        product_name = data.get('product_name')

        if not product_name:
            return jsonify({"error": "Product name is required"}), 400

        # Gemini ile analiz yap
        analysis = analyze_product_with_gemini(product_name)
        if not analysis:
            return jsonify({"error": "Analysis failed"}), 500

        # İngilizce metni Türkçe'ye çevir
        english_text = f"Advantages: {analysis['avantajlar']}\nDisadvantages: {analysis['dezavantajlar']}\nEstimated Price: {analysis['tahmini_fiyat']} TL"
        turkish_text = translate_to_turkish(english_text)

        # JSON yanıtı döndür
        return jsonify({
            "english_text": english_text,
            "meaningful_text": turkish_text
        })

    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


def analyze_product_with_gemini(product_name):
    prompt = f"""
    Product: {product_name}
    

    Analyze the advantages, disadvantages, and estimated market price of this product.
    Provide the response EXACTLY in the following format, nothing else:
    - Advantages: [High performance, Durability]
    - Disadvantages: [High price, Compatibility limitations]
    - Estimated Price: 8200 (The price will be Turkey price. It have to be turkish liras.)

    Respond in English and provide a realistic analysis.
    """
    for attempt in range(5):
        try:
            response = analysis_model.generate_content(prompt)
            response_text = response.text.strip()
            print("Raw Response:", response_text)

            avantajlar = ""
            dezavantajlar = ""
            tahmini_fiyat = 0.0

            # Avantajlar ayrıştırma
            if "- Advantages:" in response_text:
                avantajlar_start = response_text.index("- Advantages:") + len("- Advantages:")
                if "- Disadvantages:" in response_text:
                    avantajlar_end = response_text.index("- Disadvantages:")
                    avantajlar_raw = response_text[avantajlar_start:avantajlar_end].strip("[]").strip()
                    avantajlar_list = [item.strip() for item in avantajlar_raw.split(", ")]
                    avantajlar = ", ".join(avantajlar_list)
                else:
                    avantajlar_raw = response_text[avantajlar_start:].strip("[]").strip()
                    avantajlar_list = [item.strip() for item in avantajlar_raw.split(", ")]
                    avantajlar = ", ".join(avantajlar_list)

            # Dezavantajlar ayrıştırma
            if "- Disadvantages:" in response_text:
                dezavantajlar_start = response_text.index("- Disadvantages:") + len("- Disadvantages:")
                if "- Estimated Price:" in response_text:
                    dezavantajlar_end = response_text.index("- Estimated Price:")
                    dezavantajlar_raw = response_text[dezavantajlar_start:dezavantajlar_end].strip("[]").strip()
                    dezavantajlar_list = [item.strip() for item in dezavantajlar_raw.split(", ")]
                    dezavantajlar = ", ".join(dezavantajlar_list)
                else:
                    dezavantajlar_raw = response_text[dezavantajlar_start:].strip("[]").strip()
                    dezavantajlar_list = [item.strip() for item in dezavantajlar_raw.split(", ")]
                    dezavantajlar = ", ".join(dezavantajlar_list)

            # Tahmini Fiyat ayrıştırma
            if "- Estimated Price:" in response_text:
                fiyat_section = response_text.split("- Estimated Price:")[1].strip()
                fiyatlar = re.findall(r'\d+', fiyat_section)
                if fiyatlar:
                    if len(fiyatlar) == 1:  # Tek sayı varsa
                        tahmini_fiyat = float(fiyatlar[0])
                    else:  # Aralık varsa ortalamayı al
                        tahmini_fiyat = sum(map(float, fiyatlar)) / len(fiyatlar)

            return {"avantajlar": avantajlar, "dezavantajlar": dezavantajlar, "tahmini_fiyat": tahmini_fiyat}

        except Exception as err:
            if "429" in str(err):
                wait_time = (attempt + 1) * 60
                print(f"Quota exceeded, attempt {attempt + 1} failed. Waiting {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f'Error in analyze_product_with_gemini: {str(err)}')
                return None
    print("Analysis: All attempts failed, possibly quota exceeded.")
    return None


def translate_to_turkish_gemini(english_text):
    try:
        payload = {
            "q": english_text,
            "source": "en",
            "target": "tr",
            "format": "text"
        }
        response = requests.post(TRANSLATE_API_URL, json=payload)
        response.raise_for_status()

        translated_text = response.json().get("translatedText", english_text)

        # Çeviriyi Türkçeye uygun şekilde formatla
        translated_text = translated_text.replace("Advantages:", "Avantajlar:")
        translated_text = translated_text.replace("Disadvantages:", "Dezavantajlar:")
        translated_text = translated_text.replace("Estimated Price:", "Tahmini Fiyat:")

        return translated_text

    except requests.RequestException as e:
        print(f"Translation error: {str(e)}")
        return english_text  # Hata olursa İngilizce metni döndür
    except Exception as e:
        print(f"Translation processing error: {str(e)}")
        return english_text


@app.route('/scrape', methods=['POST'])
def scrape():
    keyword = request.json.get('keyword')
    if not keyword:
        return jsonify({"error": "Please provide a keyword"}), 400

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_amazon = executor.submit(scrape_amazon, keyword)
        future_trendyol = executor.submit(scrape_trendyol, keyword)
        amazon_products = future_amazon.result()
        trendyol_products = future_trendyol.result()

    products = amazon_products + trendyol_products
    db = connect_db()
    if not db:
        return jsonify({"error": "Database connection failed"}), 500

    cursor = db.cursor()
    try:
        for product in products:
            cursor.execute("SELECT id FROM urunler WHERE uniqe_key = %s", (product["hash"],))
            existing_product = cursor.fetchone()
            if not existing_product:
                cursor.execute(
                    "INSERT INTO urunler (urun_adi, fiyat, url, resim_url, uniqe_key) VALUES (%s, %s, %s, %s, %s)",
                    (product["urun_adi"], product["fiyat"], product["url"], product["resim_url"], product["hash"])
                )
                db.commit()
                urun_id = cursor.lastrowid
                product["urun_id"] = urun_id

                # Satıcı işlemleri
                cursor.execute("SELECT id FROM saticilar WHERE satici_adi = %s", (product["satici"],))
                existing_seller = cursor.fetchone()
                if not existing_seller:
                    cursor.execute("INSERT INTO saticilar (satici_adi) VALUES (%s)", (product["satici"],))
                    db.commit()
                    seller_id = cursor.lastrowid
                else:
                    seller_id = existing_seller[0]
                cursor.execute(
                    "INSERT INTO saticilar_urunler (satici_id, urun_id) VALUES (%s, %s)",
                    (seller_id, urun_id)
                )
                db.commit()

                # Kategoriye ekleme
                category_queue.put(product)
                # Analize ekleme
                analysis_queue.put(product)
    finally:
        cursor.close()
        db.close()

    return jsonify(products)


# Her gece 00:00'da calısacak zamanlayici
import schedule
import time

# Scheduler'ı oluştur
schedule.every().day.at("00:00").do(update_all_prices)

if __name__ == '__main__':
    # Zamanlayıcıyı ayrı bir thread'de çalıştır
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)


    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Flask uygulamasını çalıştır
    app.run(port=5000)
