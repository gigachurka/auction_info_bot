import time
import random
import re
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import json
import os
from chrome_utils import create_uc_driver, chrome_user_agent, get_chrome_major_version

COOKIE_FILE = "iaai_cookies.json"

# Постоянный профиль Chrome — куки/fingerprint Incapsula переживают рестарты.
# Тот же каталог, что у пуловой сессии car_finder: доверие общее.
IAAI_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chrome_profiles", "iaai")

def save_cookies(driver):
    """Save cookies to file for session persistence"""
    try:
        cookies = driver.get_cookies()
        with open(COOKIE_FILE, 'w') as f:
            json.dump(cookies, f)
        print("Cookies saved successfully")
        return True
    except Exception as e:
        print(f"Error saving cookies: {e}")
        return False

def load_cookies(driver):
    """Load cookies from file for session persistence"""
    try:
        if not os.path.exists(COOKIE_FILE):
            return False
        with open(COOKIE_FILE, 'r') as f:
            cookies = json.load(f)
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except:
                pass
        print("Cookies loaded successfully")
        return True
    except Exception as e:
        print(f"Error loading cookies: {e}")
        return False

def get_iaai_lot_data(lot_id: str, max_retries: int = 3,
                      force_manual_solve: bool = False, shared_driver=None):
    """
    Parser for IAAI using undetected_chromedriver with JavaScript extraction.

    shared_driver — уже авторизованная сессия (пул car_finder): навигация
    и парсинг идут на ней, браузер не создаётся и не закрывается.
    Без него — session persistence: первый запуск требует ручного решения
    CAPTCHA (non-headless), дальше — сохранённые куки (headless).
    """
    # Extract lot ID and suffix from URL if needed
    original_url = lot_id
    if "iaai.com/VehicleDetail/" in lot_id:
        match = re.search(r'VehicleDetail/(\d+)(~[A-Z]+)?', lot_id)
        if match:
            lot_id = match.group(1)
            suffix = match.group(2) or "~US"  # Add ~US by default
            url = f"https://www.iaai.com/VehicleDetail/{lot_id}{suffix}"
        else:
            url = lot_id
    else:
        url = f"https://www.iaai.com/VehicleDetail/{lot_id}~US"  # Add ~US by default

    for attempt in range(max_retries):
        driver = shared_driver
        try:
            cookies_exist = (os.path.exists(COOKIE_FILE)
                             and not force_manual_solve
                             and shared_driver is None)
            chrome_version = get_chrome_major_version()

            if driver is None:
                use_headless = bool(cookies_exist)

                options = uc.ChromeOptions()
                options.add_argument("--window-size=1920,1080")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")
                options.add_argument("--lang=ru")
                options.add_argument(f"--user-agent={chrome_user_agent(chrome_version)}")

                if use_headless:
                    print("Using saved cookies - headless mode")
                else:
                    print("No cookies found - opening browser for manual CAPTCHA solve")

                os.makedirs(IAAI_PROFILE_DIR, exist_ok=True)
                driver = create_uc_driver(
                    options=options,
                    headless=use_headless,
                    version_main=chrome_version,
                    user_data_dir=IAAI_PROFILE_DIR,
                )

            wait = WebDriverWait(driver, 30)

            # Visit lot page directly FIRST (cookies need domain to be loaded)
            driver.get(url)

            # Load cookies after visiting the page (domain must match)
            if cookies_exist:
                load_cookies(driver)
                # Refresh to apply cookies
                driver.refresh()
                time.sleep(random.uniform(2, 3))
            
            # Wait for page to load and JavaScript to execute
            time.sleep(random.uniform(5, 8))
            
            # Simulate human-like scrolling and mouse movements
            try:
                for _ in range(3):
                    driver.execute_script(f"window.scrollTo(0, {random.randint(100, 500)});")
                    time.sleep(random.uniform(0.5, 1.5))
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(random.uniform(1, 2))
            except:
                pass
            
            # Wait additional time for data to load
            time.sleep(random.uniform(3, 5))
            
            # Wait for page to be fully loaded
            try:
                print(f"IAAI lot {lot_id}: waiting for page load...")
                WebDriverWait(driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(5)  # Increased wait time
                print(f"IAAI lot {lot_id}: page loaded")
            except Exception as e:
                print(f"IAAI lot {lot_id}: page load wait failed: {e}")
            
            # Additional check for Access Denied after page load
            page_source = driver.page_source
            if "Access Denied" in page_source:
                print("Access Denied detected after page load")
                if attempt < max_retries - 1:
                    # If cookies exist, try deleting them and retry
                    if cookies_exist:
                        try:
                            os.remove(COOKIE_FILE)
                            print("Deleted cookies due to Access Denied")
                        except:
                            pass
                    time.sleep(random.uniform(15, 20))
                    continue
                return {"error": "IAAI заблокировал запрос (Access Denied) - возможно IP заблокирован"}
            
            # Check for blocking
            page_source = driver.page_source
            
            # Check for "Vehicle not found" error
            if "Vehicle not found" in page_source or "Vehicle details are not found" in page_source or "not found" in page_source.lower():
                print("Vehicle not found error detected")
                # Try with original URL if it was different
                if original_url != url:
                    print(f"Retrying with original URL: {original_url}")
                    driver.get(original_url)
                    time.sleep(random.uniform(5, 8))
                    page_source = driver.page_source
                    # Check again
                    if "Vehicle not found" in page_source or "Vehicle details are not found" in page_source or "not found" in page_source.lower():
                        return {"error": "Транспортное средство не найдено - возможно неверный lot ID или лот удален"}
                else:
                    return {"error": "Транспортное средство не найдено - возможно неверный lot ID или лот удален"}
            
            if "Access Denied" in page_source:
                if attempt < max_retries - 1:
                    # If cookies exist, try deleting them and retry
                    if cookies_exist:
                        try:
                            os.remove(COOKIE_FILE)
                            print("Deleted cookies due to Access Denied")
                        except:
                            pass
                    time.sleep(random.uniform(10, 15))
                    continue
                return {"error": "IAAI заблокирован запрос (Access Denied)"}
            
            # Check for Incapsula challenge or hCaptcha
            is_captcha_page = ("_Incapsula_Resource" in page_source and len(page_source) < 2000) or "h-captcha" in page_source.lower()

            if is_captcha_page and shared_driver is not None:
                # Пуловая сессия словила капчу — возвращаем ошибку,
                # car_finder пересоздаст сессию (возможно, с ручным проходом)
                return {"error": "IAAI captcha на общей сессии — сессия будет пересоздана"}

            if is_captcha_page:
                if not cookies_exist:
                    print("CAPTCHA challenge detected - please solve it manually in the browser")
                    print("Waiting for manual CAPTCHA solve (up to 180 seconds)...")
                    # Wait for manual CAPTCHA solve
                    for i in range(180):
                        time.sleep(1)
                        page_source = driver.page_source
                        # Check if CAPTCHA is solved (both conditions must be false)
                        is_solved = not ("_Incapsula_Resource" in page_source and len(page_source) < 2000) and "h-captcha" not in page_source.lower()
                        if is_solved:
                            print("CAPTCHA solved successfully!")
                            # Wait a bit after solving to ensure session is established
                            time.sleep(random.uniform(3, 5))
                            # Save cookies after successful solve
                            save_cookies(driver)
                            # Check current URL and redirect if needed
                            current_url = driver.current_url
                            if current_url != url:
                                print(f"Redirected from {current_url} to {url}")
                                driver.get(url)
                                time.sleep(random.uniform(5, 8))
                                # Re-check for CAPTCHA after redirect
                                page_source = driver.page_source
                                is_captcha_after_redirect = ("_Incapsula_Resource" in page_source and len(page_source) < 2000) or "h-captcha" in page_source.lower()
                                if is_captcha_after_redirect:
                                    print("CAPTCHA appeared again after redirect - waiting for solve...")
                                    for j in range(60):
                                        time.sleep(1)
                                        page_source = driver.page_source
                                        is_solved_again = not ("_Incapsula_Resource" in page_source and len(page_source) < 2000) and "h-captcha" not in page_source.lower()
                                        if is_solved_again:
                                            print("CAPTCHA solved after redirect!")
                                            time.sleep(random.uniform(3, 5))
                                            save_cookies(driver)
                                            break
                                        if j % 10 == 0:
                                            print(f"Still waiting for CAPTCHA solve after redirect... ({j}/60 seconds)")
                                    # Check one more time
                                    page_source = driver.page_source
                                    is_final_captcha = ("_Incapsula_Resource" in page_source and len(page_source) < 2000) or "h-captcha" in page_source.lower()
                                    if is_final_captcha:
                                        return {"error": "CAPTCHA keeps appearing - IAAI may be blocking your IP"}
                            # Additional wait after everything
                            time.sleep(random.uniform(3, 5))
                            break
                        if i % 15 == 0:
                            print(f"Still waiting for CAPTCHA solve... ({i}/180 seconds)")
                    
                    page_source = driver.page_source
                    
                    # Final check
                    is_final_captcha = ("_Incapsula_Resource" in page_source and len(page_source) < 2000) or "h-captcha" in page_source.lower()
                    if is_final_captcha:
                        return {"error": "CAPTCHA not solved in time - please solve it faster or increase wait time"}
                else:
                    # Cookies exist but still got CAPTCHA - cookies might be expired
                    print("Cookies expired, got CAPTCHA again")
                    if attempt < max_retries - 1:
                        # Delete expired cookies and retry without cookies
                        try:
                            os.remove(COOKIE_FILE)
                            print("Deleted expired cookies")
                        except:
                            pass
                        time.sleep(random.uniform(5, 8))
                        continue
                    return {"error": "Cookies expired - requires manual CAPTCHA solve again"}
            
            # Debug: Check if data elements exist on the page
            try:
                label_count = driver.execute_script("return document.querySelectorAll('.data-list__label').length;")
                value_count = driver.execute_script("return document.querySelectorAll('.data-list__value').length;")
                print(f"Debug: Found {label_count} labels and {value_count} values on page")
                
                # Print first few labels for debugging
                if label_count > 0:
                    first_labels = driver.execute_script("""
                        var labels = document.querySelectorAll('.data-list__label');
                        var result = [];
                        for (var i = 0; i < Math.min(labels.length, 5); i++) {
                            result.push(labels[i].textContent);
                        }
                        return result;
                    """)
                    print(f"Debug: First labels: {first_labels}")
            except Exception as e:
                print(f"Debug error: {e}")
            
            # Use JavaScript to extract data from the page
            def extract_field(label_text):
                try:
                    label_text_lower = label_text.lower()
                    script = f"""
                    var labels = document.querySelectorAll('.data-list__label');
                    for (var i = 0; i < labels.length; i++) {{
                        if (labels[i].textContent.toLowerCase().includes('{label_text_lower}')) {{
                            // Find the parent li element
                            var parentLi = labels[i].closest('.data-list__item');
                            if (parentLi) {{
                                // Find the value span within the same li
                                var valueSpan = parentLi.querySelector('.data-list__value');
                                if (valueSpan) {{
                                    return valueSpan.textContent.trim();
                                }}
                            }}
                            // Fallback: try next sibling approach
                            var sibling = labels[i].nextElementSibling;
                            while (sibling) {{
                                if (sibling.classList && sibling.classList.contains('data-list__value')) {{
                                    return sibling.textContent.trim();
                                }}
                                sibling = sibling.nextElementSibling;
                            }}
                        }}
                    }}
                    return null;
                    """
                    result = driver.execute_script(script)
                    return result if result else "Не найдено"
                except Exception as e:
                    print(f"Error extracting field '{label_text}': {e}")
                    return "Не найдено"
            
            # Extract primary info using JavaScript
            stock_number = extract_field("Stock #")
            selling_branch = extract_field("Selling Branch")
            vin = extract_field("VIN")
            loss = extract_field("Loss")
            primary_damage = extract_field("Primary Damage")
            secondary_damage = extract_field("Secondary Damage")
            title_doc = extract_field("Title/Sale Doc")
            start_code = extract_field("Start Code")
            key = extract_field("Key")
            odometer = extract_field("Odometer")
            airbags = extract_field("Airbags")
            
            # Extract vehicle description fields using JavaScript
            vehicle = extract_field("Vehicle")
            body_style = extract_field("Body Style")
            engine = extract_field("Engine")
            transmission = extract_field("Transmission")
            drive_line = extract_field("Drive Line Type")
            fuel_type = extract_field("Fuel Type")
            cylinders = extract_field("Cylinders")
            restraint_system = extract_field("Restraint System")
            exterior_interior = extract_field("Exterior/Interior")
            options = extract_field("Options")
            manufactured_in = extract_field("Manufactured In")
            vehicle_class = extract_field("Vehicle Class")
            model = extract_field("Model")
            series = extract_field("Series")
            
            # Extract additional sale info fields
            vehicle_location = extract_field("Vehicle Location")
            auction_date_time = extract_field("Auction Date and Time")
            lane_run = extract_field("Lane/Run #")
            aisle_stall = extract_field("Aisle/Stall")
            actual_cash_value = extract_field("Actual Cash Value")
            estimated_repair_cost = extract_field("Estimated Repair Cost")
            seller = extract_field("Seller")
            seller_type = extract_field("Seller Type")
            title_doc_brand = extract_field("Title/Sale Doc Brand")
            buy_now_price = extract_field("Buy Now Price")
            # Extract title from page
            title = driver.execute_script("return document.title;")
            if not title:
                title = f"{vehicle} {model}" if vehicle != "Не найдено" and model != "Не найдено" else "IAAI Vehicle"
            
            # Extract photos using JavaScript
            photos = []
            try:
                photo_script = """
                var photos = [];
                // все контейнеры с миниатюрами (не только первый ряд)
                var containers = document.querySelectorAll(
                    '[id^="spacedthumbs"], .vehicle-image__thumbs, #thumbGallery');
                containers.forEach(function(c){
                    c.querySelectorAll('img').forEach(function(img){
                        var src = img.src || img.getAttribute('data-src');
                        if (src && src.indexOf('vis.iaai.com') !== -1) {
                            src = src.replace(/&width=\\d+&height=\\d+/, '');
                            src = src.replace(/width=\\d+&height=\\d+/, '');
                            if (photos.indexOf(src) === -1) photos.push(src);
                        }
                    });
                });
                return photos;
                """
                photos = driver.execute_script(photo_script)
                if not photos:
                    # Fallback: look for all IAAI images
                    fallback_script = """
                    var photos = [];
                    var imgs = document.querySelectorAll('img');
                    for (var i = 0; i < imgs.length; i++) {
                        var src = imgs[i].src || imgs[i].getAttribute('data-src');
                        if (src && src.includes('vis.iaai.com')) {
                            src = src.replace(/&width=\\d+&height=\\d+/, '');
                            src = src.replace(/width=\\d+&height=\\d+/, '');
                            if (photos.indexOf(src) === -1) {
                                photos.push(src);
                            }
                        }
                    }
                    return photos;
                    """
                    photos = driver.execute_script(fallback_script)
            except:
                photos = []

            # Дополняем недостающие фото перебором imageKeys: формат
            # vis.iaai.com/resizer?imageKeys=<id>~SID~I<n>. Миниатюры на странице
            # подгружаются лениво, поэтому в DOM часто только часть кадров.
            try:
                key_m = None
                for p in photos:
                    m = re.search(r'imageKeys=([^&~]+~[^~&]+~I)(\d+)', p)
                    if m:
                        key_m = (p[:m.start()], m.group(1), p[m.end():])
                        break
                if key_m and len(photos) < 15:
                    prefix, key_base, suffix = key_m
                    have = set()
                    for p in photos:
                        mm = re.search(r'~I(\d+)', p)
                        if mm:
                            have.add(int(mm.group(1)))
                    for n in range(1, 21):
                        if n not in have:
                            photos.append(
                                f"{prefix}imageKeys={key_base}{n}{suffix}")
            except Exception as e:
                print(f"IAAI photo enumeration failed: {e}")
            
            # Save cookies after successful data extraction
            if not cookies_exist and shared_driver is None:
                save_cookies(driver)

            if shared_driver is None:
                try:
                    driver.quit()
                except:
                    pass
            
            # Compile damages
            damages = []
            if primary_damage != "Не найдено":
                damages.append(f"Primary: {primary_damage}")
            if secondary_damage != "Не найдено":
                damages.append(f"Secondary: {secondary_damage}")
            if loss != "Не найдено":
                damages.append(f"Loss: {loss}")
            
            return {
                "title": title,
                "stock_number": stock_number,
                "selling_branch": selling_branch,
                "vin": vin,
                "loss": loss,
                "primary_damage": primary_damage,
                "secondary_damage": secondary_damage,
                "title_doc": title_doc,
                "start_code": start_code,
                "key": key,
                "odometer": odometer,
                "airbags": airbags,
                "vehicle": vehicle,
                "body_style": body_style,
                "engine": engine,
                "transmission": transmission,
                "drive_line": drive_line,
                "fuel_type": fuel_type,
                "cylinders": cylinders,
                "restraint_system": restraint_system,
                "exterior_interior": exterior_interior,
                "options": options,
                "manufactured_in": manufactured_in,
                "vehicle_class": vehicle_class,
                "model": model,
                "series": series,
                "vehicle_location": vehicle_location,
                "auction_date_time": auction_date_time,
                "lane_run": lane_run,
                "aisle_stall": aisle_stall,
                "actual_cash_value": actual_cash_value,
                "estimated_repair_cost": estimated_repair_cost,
                "seller": seller,
                "seller_type": seller_type,
                "title_doc_brand": title_doc_brand,
                "buy_now_price": buy_now_price,
                "damages": damages,
                "photos": photos
            }
            
        except Exception as e:
            if driver is not None and driver is not shared_driver:
                try:
                    driver.quit()
                except:
                    pass
            if shared_driver is not None:
                # общую сессию не пересоздаём здесь — это делает car_finder
                return {"error": f"Selenium error: {str(e)}"}
            if attempt < max_retries - 1:
                time.sleep(random.uniform(3, 5))
                continue
            return {"error": f"Selenium error: {str(e)}"}
    
    return {"error": "Превышено количество попыток"}
