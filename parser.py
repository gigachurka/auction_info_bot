import time
import random
import re
import json
import logging
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from chrome_utils import create_uc_driver, chrome_user_agent, get_chrome_major_version

logger = logging.getLogger(__name__)

def normalize_copart_photo_url(src: str) -> str:
    """Convert Copart thumbnail URL to full-size URL."""
    # Remove intermediate suffixes that create dead URLs like _hrs_ful.jpg
    src = src.replace('_hrs_ful.jpg', '_ful.jpg')
    src = src.replace('_hrs_ful.jpeg', '_ful.jpeg')
    src = src.replace('_hrs_ful.png', '_ful.png')
    src = src.replace('_hrs.jpg', '_ful.jpg')
    src = src.replace('_hrs.jpeg', '_ful.jpeg')
    src = src.replace('_hrs.png', '_ful.png')
    # If already has proper _ful suffix, keep as-is
    if '_ful.jpg' in src or '_ful.jpeg' in src or '_ful.png' in src:
        return src
    # Handle _vthb (vehicle thumbnail) before _thb to avoid partial match
    src = src.replace('_vthb.jpg', '_ful.jpg')
    src = src.replace('_vthb.jpeg', '_ful.jpeg')
    src = src.replace('_vthb.png', '_ful.png')
    # Handle regular _thb
    src = src.replace('_thb.jpg', '_ful.jpg')
    src = src.replace('_thb.jpeg', '_ful.jpeg')
    src = src.replace('_thb.png', '_ful.png')
    # If still no _ful, add it before extension
    if '_ful.' not in src:
        if '.jpg' in src:
            src = src.replace('.jpg', '_ful.jpg')
        elif '.jpeg' in src:
            src = src.replace('.jpeg', '_ful.jpeg')
        elif '.png' in src:
            src = src.replace('.png', '_ful.png')
    return src


def _img_base_key(url: str) -> str:
    """Имя файла картинки без размерного суффикса и расширения (уникальный ключ снимка)."""
    fname = url.split('?')[0].split('/')[-1]
    return re.sub(r'(_(?:ful|hrs|thb|ths|vful|vhrs|vthb|vths|hd))?\.(jpg|jpeg|png)$', '', fname, flags=re.IGNORECASE)


def _size_rank(url: str) -> int:
    """Приоритет размера: чем меньше число, тем выше качество (для выбора лучшего)."""
    u = url.lower()
    if '_ful.' in u or '_vful.' in u:
        return 0
    if '_hrs.' in u or '_vhrs.' in u:
        return 1
    if '_hd.' in u:
        return 2
    if '_vthb.' in u or '_thb.' in u:
        return 3
    if '_ths.' in u or '_vths.' in u:
        return 4
    return 5


def collect_copart_image_urls(obj, found: list):
    """Рекурсивно собирает все URL картинок Copart из JSON-структуры."""
    if isinstance(obj, dict):
        for v in obj.values():
            collect_copart_image_urls(v, found)
    elif isinstance(obj, list):
        for v in obj:
            collect_copart_image_urls(v, found)
    elif isinstance(obj, str):
        if 'cs.copart.com' in obj and 'ids-c-prod' in obj and re.search(r'\.(jpg|jpeg|png)', obj, re.IGNORECASE):
            found.append(obj)


def dedupe_best_size(urls: list) -> list:
    """Группирует URL по снимку и оставляет лучший доступный размер, сохраняя порядок."""
    best = {}
    order = []
    for u in urls:
        key = _img_base_key(u)
        if key not in best:
            best[key] = u
            order.append(key)
        elif _size_rank(u) < _size_rank(best[key]):
            best[key] = u
    return [best[k] for k in order]

def get_lot_data(lot_id: str, max_retries: int = 3):
    """
    Parser using undetected_chromedriver with improved anti-detection
    """
    url = f"https://www.copart.com/lot/{lot_id}"
    chrome_version = get_chrome_major_version()
    
    for attempt in range(max_retries):
        driver = None
        # Headless на 1-й попытке; если Incapsula не проходит — пробуем с окном
        use_headless = attempt == 0
        try:
            logger.info(f"Copart lot {lot_id}: attempt {attempt + 1}/{max_retries} (headless={use_headless})")
            logger.info(f"Copart lot {lot_id}: initializing Chrome driver...")
            
            options = uc.ChromeOptions()
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--lang=en-US")
            options.add_argument(f"--user-agent={chrome_user_agent(chrome_version)}")
            
            driver = create_uc_driver(
                options=options,
                headless=use_headless,
                version_main=chrome_version,
            )
            logger.info(f"Copart lot {lot_id}: driver created")

            # Сначала главная — иногда помогает пройти Incapsula до лота
            logger.info(f"Copart lot {lot_id}: warming up on homepage...")
            driver.get("https://www.copart.com/")
            time.sleep(random.uniform(2, 4))

            logger.info(f"Copart lot {lot_id}: navigating to {url}")
            driver.get(url)
            logger.info(f"Copart lot {lot_id}: navigation completed")

            # Ждём либо контент лота, либо таймаут Incapsula
            logger.info(f"Copart lot {lot_id}: waiting for page load...")
            try:
                WebDriverWait(driver, 45).until(
                    lambda d: len(d.page_source) > 5000
                    and "_Incapsula_Resource" not in d.page_source[:3000]
                )
                logger.info(f"Copart lot {lot_id}: page loaded ({len(driver.page_source)} bytes)")
            except Exception as e:
                logger.warning(f"Copart lot {lot_id}: page load wait failed: {e}")

            # Короткая пауза на Angular / bid websocket
            time.sleep(random.uniform(3, 5))
            
            logger.info(f"Copart lot {lot_id}: checking for blocking...")
            page_source = driver.page_source

            if "Access Denied" in page_source or "Error 16" in page_source:
                logger.warning(f"Copart lot {lot_id}: Access Denied detected")
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(3, 5))
                    continue
                return {"error": "Copart заблокировал запрос (Access Denied)"}
            
            if "_Incapsula_Resource" in page_source and len(page_source) < 5000:
                logger.warning(f"Copart lot {lot_id}: Incapsula challenge not resolved (len={len(page_source)})")
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(3, 5))
                    continue
                return {"error": "Incapsula не пройден. Включите VPN (или укажите PROXY_URL в .env) и попробуйте снова"}
            
            # Parse HTML with BeautifulSoup
            logger.info(f"Copart lot {lot_id}: parsing HTML...")
            soup = BeautifulSoup(page_source, 'html.parser')

            # Extract title first (needed for other extractions)
            # Try <title> tag first for full page title with details
            title_elem = soup.find('title')
            if title_elem:
                title = title_elem.get_text(strip=True)
                logger.info(f"Copart lot {lot_id}: title extracted: {title[:50]}...")
            else:
                # Fallback to h1
                title_elem = soup.find('h1')
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    logger.info(f"Copart lot {lot_id}: title extracted from h1: {title[:50]}...")
                else:
                    logger.error(f"Copart lot {lot_id}: title not found")
                    return {"error": "Не найден заголовок лота"}
            
            # Проверяем, что попали именно на страницу лота, а не на главную/ошибку
            if "Online Car Auctions" in title or "Used, Salvage & Wholesale" in title:
                logger.warning(f"Copart lot {lot_id}: landed on homepage instead of lot page")
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(3, 5))
                    continue
                return {"error": "Лот не найден или недоступен (Copart показал главную страницу). Попробуйте другой ID."}
            
            # Extract data from title
            # Title format: "2019 TESLA MODEL 3    | Run and Drive | May 07, 2026 | CT - HARTFORD SPRINGFIELD | Copart"
            title_parts = [part.strip() for part in title.split('|')]
            
            # Initialize variables
            sale_date = "Не найдено"
            location = "Не найдено"
            
            # Extract sale date from title (3rd part)
            if len(title_parts) >= 3:
                sale_date = title_parts[2].strip()
            
            # Extract location from title (4th part)
            if len(title_parts) >= 4:
                location = title_parts[3].strip()
                # Remove "Copart" if present
                location = location.replace('Copart', '').strip()
            
            # Extract VIN - try multiple approaches
            logger.info(f"Copart lot {lot_id}: extracting VIN...")
            vin = "Не найдено"
            try:
                # Try to find VIN in text content
                vin = driver.execute_script("""
                    // Try to find VIN in the page - look for 17 char alphanumeric
                    var elements = document.querySelectorAll('*');
                    for (var i = 0; i < elements.length; i++) {
                        var text = elements[i].textContent || elements[i].innerText;
                        if (text) {
                            var match = text.match(/\\b[A-Z0-9*]{17}\\b/);
                            if (match) {
                                return match[0];
                            }
                        }
                    }
                    return null;
                """)
                if not vin:
                    # Try DOM selector
                    try:
                        vin_elem = driver.find_element(By.CSS_SELECTOR, "[data-uname='lotdetailvinvalue']")
                        vin = vin_elem.text.strip()
                    except:
                        # Try regex on page source
                        try:
                            vin_match = re.search(r'[A-Z0-9*]{17}', page_source)
                            if vin_match:
                                vin = vin_match.group(0)
                        except:
                            pass
            except:
                try:
                    vin_match = re.search(r'[A-Z0-9]{17}', page_source)
                    if vin_match:
                        vin = vin_match.group(0)
                except:
                    pass
            
            # Extract Sale Date - already extracted from title above
            # Extract Location - already extracted from title above
            
            # Helper function to extract value by label
            def extract_by_label(label_text):
                try:
                    labels = soup.find_all('label', class_='lot-details-information-label')
                    for label in labels:
                        if label_text.lower() in label.get_text().lower():
                            value_div = label.find_next_sibling('div', class_='lot-details-information-value')
                            if not value_div:
                                value_div = label.find_next_sibling('span', class_='lot-details-information-value')
                            if value_div:
                                return value_div.get_text(strip=True)
                except:
                    pass
                return "Не найдено"
            
            # Extract all fields using BeautifulSoup (only fields from characteristics table)
            title_code = extract_by_label("Title code")
            odometer = extract_by_label("Odometer")
            transmission = extract_by_label("Transmission")
            drive_type = extract_by_label("Drivetrain")
            fuel_type = extract_by_label("Fuel")
            engine = extract_by_label("Engine type")
            if engine == "Не найдено":
                engine = extract_by_label("Engine")
            if engine == "Не найдено":
                # Fallback: элемент с data-uname, содержащий "engine" (новая вёрстка)
                try:
                    eng_el = soup.find(attrs={"data-uname": re.compile(r"engine", re.I)})
                    if eng_el:
                        engine = eng_el.get_text(strip=True)
                except Exception:
                    pass
            if engine == "Не найдено":
                # Fallback: значение с литражом "X.XL" в таблице характеристик
                try:
                    for val in soup.select(".lot-details-information-value"):
                        t = val.get_text(strip=True)
                        if re.search(r'\d+(?:\.\d+)?\s*L\b', t):
                            engine = t
                            break
                except Exception:
                    pass
            cylinder = extract_by_label("Cylinders")
            exterior_color = extract_by_label("Color")
            keys = extract_by_label("Has key")
            vehicle_type = extract_by_label("Vehicle type")
            body_style = extract_by_label("Body style")
            notes = extract_by_label("Notes")
            
            title_elem = soup.find('h1')
            if not title_elem:
                # Try alternative selectors
                title_elem = soup.find('title')
                if title_elem:
                    title = title_elem.get_text(strip=True)
                else:
                    return {"error": "Не найден заголовок лота"}
            else:
                title = title_elem.get_text(strip=True)
            
            # Filter out CAPTCHA and cookie consent text from title
            title_lower = title.lower()
            if any(phrase in title_lower for phrase in ['captcha', 'challenge', 'verify', 'robot', 'human', 'incapsula', 'cookie', 'consent', 'access denied']):
                # Try to extract actual car title from page title
                page_title = soup.find('title')
                if page_title:
                    title = page_title.get_text(strip=True)
                    # Remove common suffixes
                    for suffix in [' | Copart', ' - Copart', ' COPART']:
                        if suffix in title:
                            title = title.replace(suffix, '')
            
            # Extract damages - try multiple selectors
            damages = []
            
            # Filter out cookie consent and CAPTCHA text
            def is_valid_damage_text(text):
                text_lower = text.lower()
                # Filter out cookie consent text
                if any(phrase in text_lower for phrase in ['cookie', 'consent', 'personal data', 'advertising', 'legitimate interest', 'vendor', 'one trust', 'optanon', 'manage your data', 'how can i change', 'what if i don\'t', 'legitimate interest', 'vendor preferences', 'confirm our vendors', 'consent management']):
                    return False
                # Filter out CAPTCHA text
                if any(phrase in text_lower for phrase in ['captcha', 'challenge', 'verify', 'robot', 'human', 'incapsula']):
                    return False
                # Filter out labels only
                if text_lower in ['primary damage:', 'secondary damage:', 'damage:', 'view damage']:
                    return False
                return True
            
            # Method 1: Look for damage values near damage labels
            damage_labels = soup.find_all(text=lambda x: x and 'primary damage' in str(x).lower())
            for label in damage_labels:
                parent = label.parent
                if parent:
                    # Get next sibling or parent's parent to find the value
                    next_sibling = parent.find_next_sibling()
                    if next_sibling:
                        text = next_sibling.get_text(strip=True)
                        if text and is_valid_damage_text(text) and len(text) > 5:
                            damages.append(f"Primary: {text}")
            
            damage_labels = soup.find_all(text=lambda x: x and 'secondary damage' in str(x).lower())
            for label in damage_labels:
                parent = label.parent
                if parent:
                    next_sibling = parent.find_next_sibling()
                    if next_sibling:
                        text = next_sibling.get_text(strip=True)
                        if text and is_valid_damage_text(text) and len(text) > 5:
                            damages.append(f"Secondary: {text}")
            
            # Method 2: data-uname attribute
            if not damages:
                damage_elements = soup.find_all(attrs={"data-uname": "damage-information"})
                for elem in damage_elements:
                    text = elem.get_text(strip=True)
                    if text and text.lower() not in ['view damage', 'damage'] and is_valid_damage_text(text):
                        damages.append(text)
            
            # Method 3: Look for damage-related text with actual values
            if not damages:
                # Find all elements containing "damage" or "Damage"
                for elem in soup.find_all(text=lambda x: x and 'damage' in str(x).lower()):
                    parent = elem.parent
                    if parent:
                        text = parent.get_text(strip=True)
                        # Filter out button labels, short non-descriptive text, and cookie consent
                        if text and len(text) > 15 and len(text) < 200 and text.lower() not in ['view damage', 'damage', 'primary damage:', 'secondary damage:'] and is_valid_damage_text(text):
                            if text not in damages:
                                damages.append(text)
            
            # Extract photos
            logger.info(f"Copart lot {lot_id}: extracting photos...")
            photos = []

            # Method 0 (primary): официальный image API Copart через авторизованную сессию браузера.
            # Возвращает ТОЧНЫЕ URL всех снимков (включая табличку VIN), без угадывания суффиксов.
            try:
                logger.info(f"Copart lot {lot_id}: calling image API...")
                api_json = driver.execute_async_script("""
                    var lotId = arguments[0];
                    var callback = arguments[arguments.length - 1];
                    fetch('https://www.copart.com/public/data/lotdetails/solr/lotImages/' + lotId, {
                        headers: {'Accept': 'application/json'},
                        credentials: 'include'
                    })
                    .then(function(r){ return r.json(); })
                    .then(function(data){ callback(data); })
                    .catch(function(e){ callback(null); });
                """, lot_id)
                if api_json:
                    api_urls = []
                    collect_copart_image_urls(api_json, api_urls)
                    photos = dedupe_best_size(api_urls)
                    logger.info(f"Copart lot {lot_id}: image API returned {len(photos)} photos")
            except Exception as e:
                logger.warning(f"Copart image API failed for lot {lot_id}: {e}")

            # Method 1: Look for img tags with specific classes or attributes
            img_tags = soup.find_all('img') if not photos else []
            for img in img_tags:
                src = img.get('src') or img.get('data-src')
                if src:
                    # Make absolute URL
                    if src.startswith('//'):
                        src = 'https:' + src
                    elif src.startswith('/'):
                        src = 'https://www.copart.com' + src
                    
                    # Filter for copart URLs only
                    if 'copart.com' in src and 'ids-c-prod' in src:
                        src = normalize_copart_photo_url(src)
                        if src not in photos:
                            photos.append(src)
            
            # Method 2: Look for photo gallery containers
            if not photos:
                photo_containers = soup.find_all(class_=lambda x: x and 'photo' in str(x).lower())
                for container in photo_containers:
                    imgs = container.find_all('img')
                    for img in imgs:
                        src = img.get('src') or img.get('data-src')
                        if src and 'copart' in src.lower():
                            if src.startswith('//'):
                                src = 'https:' + src
                            elif src.startswith('/'):
                                src = 'https://www.copart.com' + src
                            src = normalize_copart_photo_url(src)
                            if src not in photos:
                                photos.append(src)
            
            # Method 3: Extract from page source via regex (catches JSON/script loaded photos)
            if not photos:
                page_source = driver.page_source
                regex_urls = re.findall(r'https?://cs\.copart\.com/[^"\s<>]+', page_source)
                for src in regex_urls:
                    if 'ids-c-prod' in src:
                        src = src.split('"')[0].split("'")[0]
                        src = normalize_copart_photo_url(src)
                        if src not in photos:
                            photos.append(src)
            
            logger.info(f"Copart lot {lot_id}: found {len(photos)} unique photo URLs")
            for i, p in enumerate(photos[:15]):
                logger.info(f"  Photo {i+1}: {p}")
            
            try:
                driver.quit()
            except:
                pass
            
            # Extract current bid price and estimated retail value
            current_bid = "Не найдено"
            estimated_value = "Не найдено"
            
            # Method 1: Extract from cachedSolrLotDetailsStr JSON object (most reliable)
            try:
                # Look for currentBid in the JSON data directly (handle escaped quotes in JSON string)
                # The JSON is escaped as \"currentBid\":4550
                current_bid_match = re.search(r'\\"currentBid\\"[:\s]+(-?\d+\.?\d*)', page_source)
                if not current_bid_match:
                    # Try without escaped quotes
                    current_bid_match = re.search(r'"currentBid"[:\s]+(-?\d+\.?\d*)', page_source)
                if not current_bid_match:
                    # Try without quotes at all
                    current_bid_match = re.search(r'currentBid[:\s]+(-?\d+\.?\d*)', page_source)
                
                if current_bid_match:
                    bid_value = float(current_bid_match.group(1))
                    if bid_value > 0:
                        current_bid = f"${bid_value:,.0f}"
                    else:
                        # If currentBid is 0, check if this lot has bids at all
                        has_bid_match = re.search(r'\\"hasBid\\"[:\s]+(true|false)', page_source)
                        if not has_bid_match:
                            has_bid_match = re.search(r'"hasBid"[:\s]+(true|false)', page_source)
                        if has_bid_match:
                            has_bid = has_bid_match.group(1)
                            if has_bid == "false":
                                current_bid = "Нет ставок"
                            else:
                                # Has bids but cached data shows 0 - might need live data
                                # Try to find other bid fields
                                other_bid_fields = ['buyTodayBid', 'hb', 'ahb']
                                for field in other_bid_fields:
                                    field_match = re.search(f'{field}[:\\s]+(-?\\d+\\.?\\d*)', page_source)
                                    if field_match:
                                        bid_val = float(field_match.group(1))
                                        if bid_val > 0:
                                            current_bid = f"${bid_val:,.0f}"
                                            break
                                if current_bid == "Не найдено":
                                    current_bid = "Нет ставок"
                        else:
                            current_bid = "Нет ставок"
                
                # Look for estimated value (la field)
                est_value_match = re.search(r'\\"la\\"[:\s]+(-?\d+\.?\d*)', page_source)
                if not est_value_match:
                    est_value_match = re.search(r'"la"[:\s]+(-?\d+\.?\d*)', page_source)
                if not est_value_match:
                    est_value_match = re.search(r'la[:\s]+(-?\d+\.?\d*)', page_source)
                
                if est_value_match:
                    est_value = float(est_value_match.group(1))
                    if est_value > 0:
                        estimated_value = f"${est_value:,.0f}"
                
                # If la field is -1 or not found, try other estimated value fields
                if estimated_value == "Не найдено":
                    alt_est_fields = ['lotPlugAcv', 'orr', 'rc']
                    for field in alt_est_fields:
                        # Try with escaped quotes
                        field_match = re.search(f'\\"{field}\\"[:\\s]+(-?\\d+\\.?\\d*)', page_source)
                        if not field_match:
                            # Try without escaped quotes
                            field_match = re.search(f'"{field}"[:\\s]+(-?\\d+\\.?\\d*)', page_source)
                        if not field_match:
                            # Try without quotes
                            field_match = re.search(f'{field}[:\\s]+(-?\\d+\\.?\\d*)', page_source)
                        if field_match:
                            est_val = float(field_match.group(1))
                            if est_val > 0:
                                estimated_value = f"${est_val:,.0f}"
                                break
            except Exception as e:
                # Log the error for debugging
                pass
            
            # Method 2: Try to get current bid from DOM using Selenium (for live data)
            # Disabled to prevent urllib3 connection errors
            # The JSON extraction is sufficient for most cases
            pass
            
            # Method 2: Fallback to regex if JSON extraction fails
            if estimated_value == "Не найдено":
                est_value_match = re.search(r'estimated\s+retail\s+value[^$]*\$[\d,]+', page_source, re.IGNORECASE)
                if est_value_match:
                    price_match = re.search(r'\$[\d,]+', est_value_match.group(0))
                    if price_match:
                        estimated_value = price_match.group(0)
            
            return {
                "title": title,
                "vin": vin,
                "damages": damages,
                "photos": photos,
                "current_bid": current_bid,
                "estimated_value": estimated_value,
                "odometer": odometer,
                "sale_date": sale_date,
                "location": location,
                "transmission": transmission,
                "drive_type": drive_type,
                "fuel_type": fuel_type,
                "engine": engine,
                "cylinder": cylinder,
                "exterior_color": exterior_color,
                "keys": keys,
                "title_code": title_code,
                "vehicle_type": vehicle_type,
                "body_style": body_style,
                "notes": notes
            }
            
        except Exception as e:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            err = str(e)
            logger.warning(f"Copart lot {lot_id}: attempt failed: {err[:200]}")
            if attempt < max_retries - 1:
                time.sleep(random.uniform(3, 5))
                continue
            if "invalid session" in err.lower() or "disconnected" in err.lower():
                return {"error": "Copart закрыл сессию. Включите VPN (или PROXY_URL в .env) и попробуйте снова"}
            return {"error": f"Selenium error: {err}"}
    
    return {"error": "Превышено количество попыток"}