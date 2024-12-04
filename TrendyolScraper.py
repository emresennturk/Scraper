from flask import Flask, request, jsonify
import mysql.connector
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import hashlib
import concurrent.futures
import google.generativeai as genai

app = Flask(__name__)

# Gemini API yapılandırması
GOOGLE_API_KEY = "AIzaSyAn1bi1bGd73huUHhOlAe4gVrSONezqmt8"
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')


def get_category(product_name):
    prompt = f"""
    Product: {product_name}
    Categorize incoming Product entry, according to their product names but there will be only one category no subcategories and the response will be turkish
    If you see a similar product, add it to category you create before. Don't create a new category.
    """
    response = model.generate_content(prompt)
    return response.text.strip()


chrome_driver_path = "C:\\Users\\comba\\OneDrive\\Masaüstü\\chromedriver-win64\\chromedriver.exe"
service = Service(chrome_driver_path)


def create_driver():
    options = Options()
    options.headless = True
    return webdriver.Chrome(service=service, options=options)


def scrape_amazon(keyword):
    driver = create_driver()
    driver.get("https://www.amazon.com.tr/")
    search_box = driver.find_element(By.ID, "twotabsearchtextbox")
    search_box.send_keys(keyword)
    search_box.submit()
    time.sleep(1)

    entries = driver.find_elements(By.CSS_SELECTOR, "div.s-main-slot div.s-result-item")
    products = []

    for entry in entries[:10]:
        product_info = {"urun_adi": None, "fiyat": None, "url": None, "resim_url": None, "satici": "Amazon"}
        try:
            product_info["urun_adi"] = entry.find_element(By.CSS_SELECTOR, "h2").text
            whole_price = entry.find_element(By.CSS_SELECTOR, ".a-price-whole").text
            product_info["fiyat"] = float(whole_price.replace('.', '').replace(',', '.'))
            product_info["url"] = entry.find_element(By.CSS_SELECTOR, "a.a-link-normal").get_dom_attribute("href")
            product_info["resim_url"] = entry.find_element(By.CSS_SELECTOR, "img.s-image").get_dom_attribute("src")
            product_info["hash"] = hashlib.md5(
                f"{product_info['urun_adi']}{product_info['fiyat']}".encode()).hexdigest()
            products.append(product_info)
        except Exception:
            pass

    driver.quit()
    return products


def scrape_trendyol(keyword):
    driver = create_driver()
    driver.get("https://www.trendyol.com")
    search_box = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "V8wbcUhU")))
    search_box.send_keys(keyword)
    search_box.send_keys(Keys.RETURN)
    product_elements = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.p-card-wrppr")))

    products = []
    for product in product_elements[:10]:
        product_info = {"urun_adi": None, "fiyat": None, "url": None, "resim_url": None, "satici": "Trendyol"}
        try:
            brand = product.find_element(By.CLASS_NAME, "prdct-desc-cntnr-ttl").text
            product_name = product.find_element(By.CLASS_NAME, "prdct-desc-cntnr-name").text
            description = product.find_element(By.CLASS_NAME, "product-desc-sub-text").text
            product_info["urun_adi"] = f"{brand} {product_name} {description}"
            fiyat_text = product.find_element(By.CLASS_NAME, "prc-box-dscntd").text
            fiyat_number = fiyat_text.replace(" TL", "").replace(".", "").replace(",", ".")
            product_info["fiyat"] = float(fiyat_number)
            product_info["url"] = product.find_element(By.TAG_NAME, "a").get_dom_attribute("href")
            product_info["resim_url"] = product.find_element(By.TAG_NAME, "img").get_dom_attribute("src")
            product_info["hash"] = hashlib.md5(
                f"{product_info['urun_adi']}{product_info['fiyat']}".encode()).hexdigest()
            products.append(product_info)
        except Exception:
            pass

    driver.quit()
    return products


@app.route('/scrape', methods=['POST'])
def scrape():
    keyword = request.json.get('keyword')
    if not keyword:
        return jsonify({"error": "Please provide a keyword"}), 400

    # 2 scraperı da aynı anda çalıştırıyoruz
    with concurrent.futures.ThreadPoolExecutor() as executor:
        amazon_future = executor.submit(scrape_amazon, keyword)
        trendyol_future = executor.submit(scrape_trendyol, keyword)
        amazon_products = amazon_future.result()
        trendyol_products = trendyol_future.result()

    products = amazon_products + trendyol_products

    db = mysql.connector.connect(
        host="localhost",
        user="root",
        password="emberes1617.",
        database="deneme_db",
    )
    cursor = db.cursor()

    # Save products to the database
    for product in products:
        # Gemini API ile kategori belirleme
        category_name = get_category(product["urun_adi"])

        # Kategori tablosunda kontrol ve ekleme
        cursor.execute("SELECT id FROM kategoriler WHERE kategori_adi = %s", (category_name,))
        category_result = cursor.fetchone()

        if not category_result:
            cursor.execute("INSERT INTO kategoriler (kategori_adi) VALUES (%s)", (category_name,))
            db.commit()
            category_id = cursor.lastrowid
        else:
            category_id = category_result[0]

        cursor.execute("SELECT id FROM urunler WHERE uniqe_key = %s", (product["hash"],))
        existing_product = cursor.fetchone()

        if not existing_product:
            cursor.execute(
                "INSERT INTO urunler (urun_adi, fiyat, url, resim_url, uniqe_key) VALUES (%s, %s, %s, %s, %s)",
                (product["urun_adi"], product["fiyat"], product["url"], product["resim_url"], product["hash"])
            )
            db.commit()
            urun_id = cursor.lastrowid

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

            # Ürün-kategori ilişkisi ekleme
            cursor.execute(
                "INSERT INTO urunler_kategoriler (urun_id, kategori_id) VALUES (%s, %s)",
                (urun_id, category_id)
            )
            db.commit()
        else:
            cursor.execute(
                "UPDATE urunler SET fiyat = %s, resim_url = %s, urun_adi = %s WHERE uniqe_key = %s",
                (product["fiyat"], product["resim_url"], product["urun_adi"], product["hash"])
            )
            db.commit()

    cursor.close()
    db.close()

    return jsonify(products)


if __name__ == '__main__':
    app.run(port=5000)
