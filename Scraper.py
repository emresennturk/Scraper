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
import hashlib
import time

app = Flask(__name__)

# Google Gemini API yapılandırması
GOOGLE_API_KEY = "AIzaSyAn1bi1bGd73huUHhOlAe4gVrSONezqmt8"
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")


def create_driver():
    try:
        options = Options()
        options.headless = True
        service = Service("C:\\Users\\comba\\OneDrive\\Masaüstü\\chromedriver-win64\\chromedriver.exe")
        return webdriver.Chrome(service=service, options=options)
    except Exception as err:
        traceback.print_exc()
        print(f'Error in create_driver: {str(err)}')
        return None


def update_product_price(url, unique_key):
    driver = create_driver()
    try:
        driver.get(url)
        time.sleep(2)

        # Amazon ürünü ise
        if "amazon.com.tr" in url:
            price_element = driver.find_element(By.CSS_SELECTOR, ".a-price-whole")
            price = float(price_element.text.replace('.', '').replace(',', '.'))

        # Trendyol ürünü ise
        elif "trendyol.com" in url:
            price_element = driver.find_element(By.CLASS_NAME, "prc-dsc")
            price_text = price_element.text.replace(" TL", "").replace(".", "").replace(",", ".")
            price = float(price_text)
        else:
            return None

        return price
    except Exception as err:
        print(f'Error updating product price: {str(err)}')
        return None
    finally:
        driver.quit()


@app.route('/update_product', methods=['POST'])
def update_product():
    url = request.json.get('url')
    unique_key = request.json.get('uniqueKey')

    if not url or not unique_key:
        return jsonify({"error": "Missing required parameters"}), 400

    new_price = update_product_price(url, unique_key)

    if new_price is None:
        return jsonify({"error": "Failed to update product price"}), 500
    db = connect_db()
    if not db:
        return jsonify({"error": "Database connection failed"}), 500

    cursor = db.cursor()
    try:
        cursor.execute(
            "UPDATE urunler SET fiyat = %s WHERE uniqe_key = %s",
            (new_price, unique_key)
        )
        db.commit()
        return jsonify({"success": True, "new_price": new_price})
    except Exception as err:
        print(f'Error updating database: {str(err)}')
        return jsonify({"error": "Database update failed"}), 500
    finally:
        cursor.close()
        db.close()


def scrape_amazon(keyword):
    driver = create_driver()
    products = []
    try:
        driver.get("https://www.amazon.com.tr/")
        search_box = driver.find_element(By.ID, "twotabsearchtextbox")
        search_box.send_keys(keyword)
        search_box.submit()
        time.sleep(1)

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
    finally:
        driver.quit()
    return products


def scrape_trendyol(keyword):
    driver = create_driver()
    products = []
    try:
        driver.get("https://www.trendyol.com")
        search_box = driver.find_element(By.CLASS_NAME, "V8wbcUhU")
        search_box.send_keys(keyword)
        search_box.send_keys(Keys.RETURN)
        time.sleep(2)

        product_elements = driver.find_elements(By.CSS_SELECTOR, "div.p-card-wrppr")

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
                product_info["url"] = "https://www.trendyol.com" + product.find_element(By.TAG_NAME,
                                                                                        "a").get_dom_attribute("href")
                product_info["resim_url"] = product.find_element(By.TAG_NAME, "img").get_dom_attribute("src")
                product_info["hash"] = hashlib.md5(
                    f"{product_info['urun_adi']}{product_info['fiyat']}".encode()).hexdigest()
                products.append(product_info)
            except Exception as err:
                print(f'Error in scrape_trendyol entry processing: {str(err)}')
    finally:
        driver.quit()
    return products


def get_category(product_name):
    prompt = f"""
    Product: {product_name}
    Categorize incoming Product entry, according to their product names but there will be only one category no subcategories and the response will be turkish
    If you see a similar product, add it to category you create before. Don't create a new category.
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as err:
        print(f'Error in get_category: {str(err)}')
        return "Uncategorized"


def connect_db():
    try:
        return mysql.connector.connect(
            host="localhost",
            user="root",
            password="emberes1617.",
            database="deneme_db",
        )
    except Exception as err:
        print(f'Error in connect_db: {str(err)}')
        return None


# Kategori işleme kuyruğu
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

            if not category_result:
                cursor.execute("INSERT INTO kategoriler (kategori_adi) VALUES (%s)", (category_name,))
                db.commit()
                category_id = cursor.lastrowid
            else:
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


# Start category processing thread
category_thread = threading.Thread(target=process_categories, daemon=True)
category_thread.start()


@app.route('/scrape', methods=['POST'])
def scrape():
    keyword = request.json.get('keyword')
    if not keyword:
        return jsonify({"error": "Please provide a keyword"}), 400

    # Concurrent execution of scrapers
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_amazon = executor.submit(scrape_amazon, keyword)
        future_trendyol = executor.submit(scrape_trendyol, keyword)

        # Wait for both scrapers to complete
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
                product["urun_id"] = urun_id
                category_queue.put(product)
    finally:
        cursor.close()
        db.close()

    return jsonify(products)


if __name__ == '__main__':
    app.run(port=5000)