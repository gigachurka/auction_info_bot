import asyncio
import logging
import aiohttp
import io
import os
import re
import sys
import tempfile
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ContentType, BufferedInputFile, BotCommand, FSInputFile
from aiogram.methods import SetMyCommands

from config import TOKEN, SERVICE_FEE_USD
from parser import get_lot_data
from iaai_parser import get_iaai_lot_data  # noqa: F401 — standalone fallback/debug
from customs import calculate_total_customs, parse_engine_cc, parse_year
from shipping import calculate_shipping, guess_vehicle_type, select_destination_port
from chrome_utils import ensure_patched_driver, get_chrome_major_version
from car_finder import (
    FIND_BUTTON_TEXT,
    car_finder_router,
    close_sessions,
    configure_car_finder,
    init_catalog_db,
    parse_iaai_lot,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Создайте файл .env (см. .env.example) "
        "и укажите BOT_TOKEN=<токен от @BotFather>."
    )

bot = Bot(token=TOKEN)
dp = Dispatcher()
dp.include_router(car_finder_router)
executor = ThreadPoolExecutor(max_workers=2)

MAIN_KEYBOARD = types.ReplyKeyboardMarkup(
    keyboard=[[types.KeyboardButton(text=FIND_BUTTON_TEXT)]],
    resize_keyboard=True,
)

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    """Handle /start command"""
    user_id = message.from_user.id
    logger.info(f"User {user_id} sent /start")
    await message.answer(
        "👋 Добро пожаловать! Отправьте мне ссылку или ID лота Copart или IAAI, и я покажу информацию о нем.\n\n"
        f"Также можно нажать «{FIND_BUTTON_TEXT}» и выбрать авто по марке и модели.\n\n"
        "📝 Примеры:\n"
        "- Copart: 49511256 или https://www.copart.com/lot/49511256\n"
        "- IAAI: 45338047 или https://www.iaai.com/VehicleDetail/45338047~US",
        reply_markup=MAIN_KEYBOARD,
    )

@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    """Handle /help command"""
    user_id = message.from_user.id
    logger.info(f"User {user_id} sent /help")
    await message.answer(
        "📋 Справка по боту:\n\n"
        "🔹 /start - Начать работу с ботом\n"
        "🔹 /help - Показать эту справку\n"
        "🔹 /find - Подобрать авто по марке и модели\n"
        "🔹 /restart - Перезапустить бота\n\n"
        "💡 Как использовать:\n"
        "Просто отправьте ссылку или ID лота Copart или IAAI.\n\n"
        "Примеры:\n"
        "- Copart: 49511256 или https://www.copart.com/lot/49511256\n"
        "- IAAI: 45338047 или https://www.iaai.com/VehicleDetail/45338047~US"
    )

@dp.message(Command("restart"))
async def restart(message: types.Message):
    """Истинный перезапуск: заменяем процесс новым экземпляром этого же
    скрипта (os.execv). Работает и под systemd, и при ручном запуске —
    бот поднимается сам, без внешнего супервизора."""
    try:
        logger.info(f"User {message.from_user.id} sent /restart")
        await message.answer("🔄 Бот перезапускается, подождите пару секунд...")
    except Exception as e:
        logger.error(f"Error in restart handler: {e}")

    async def _do_restart():
        await asyncio.sleep(1)  # дать ответу уйти в Telegram
        try:
            await dp.stop_polling()
        except Exception as e:
            logger.warning(f"stop_polling failed: {e}")
        try:
            await bot.session.close()
        except Exception as e:
            logger.warning(f"session close failed: {e}")
        # Закрыть Chrome-сессии пула — иначе останутся процессы-сироты
        try:
            close_sessions()
        except Exception as e:
            logger.warning(f"close_sessions failed: {e}")
        logger.info("Re-executing process for restart...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    asyncio.create_task(_do_restart())


async def download_photo(session: aiohttp.ClientSession, url: str) -> bytes | None:
    """Download photo from URL via shared session; returns bytes or None.

    Some Copart images (например, табличка VIN) не имеют _ful версии,
    но доступны в других размерах (_hrs, _thb, _ths). Перебираем варианты.
    """
    urls_to_try = [url]

    # Определяем расширение и базу URL для подстановки суффиксов Copart
    match = re.search(r'(_(?:ful|hrs|thb|ths))(\.(?:jpg|jpeg|png))$', url, re.IGNORECASE)
    if match:
        ext = match.group(2)
        base = url[:match.start()]
        # Порядок: сначала качество повыше, потом миниатюры
        for suffix in ('_ful', '_hrs', '_thb', '_ths'):
            candidate = f"{base}{suffix}{ext}"
            if candidate not in urls_to_try:
                urls_to_try.append(candidate)
        # На крайний случай — без суффикса
        urls_to_try.append(f"{base}{ext}")

    for try_url in urls_to_try:
        try:
            async with session.get(try_url) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.warning(f"Failed to download photo, status: {response.status} ({try_url})")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout downloading photo: {try_url}")
        except Exception as e:
            logger.error(f"Error downloading photo: {e} ({try_url})")
    return None


def sanitize_photo(raw_bytes: bytes) -> bytes | None:
    """Приводит изображение к валидному для Telegram JPEG.

    Лечит PHOTO_INVALID_DIMENSIONS: Telegram требует соотношение сторон <= 20:1
    и сумму сторон <= 10000. Возвращает None, если файл не читается как картинка.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception:
        return None
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w < 1 or h < 1:
            return None
        ratio = max(w, h) / min(w, h)
        if ratio > 20:
            # центральный кроп до 20:1
            if w > h:
                nw = h * 20
                img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            else:
                nh = w * 20
                img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        w, h = img.size
        scale = min(1.0, 10000.0 / max(w, h), 10000.0 / (w + h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def extract_lot_id(text: str) -> tuple:
    """Извлекает ID лота и тип парсера из текста или ссылки.
    Возвращает (lot_id, parser_type) где parser_type это 'copart' или 'iaai'"""
    text = text.strip()
    
    # Если это ссылка IAAI, извлекаем ID
    if "iaai.com" in text.lower() and "vehicledetail" in text.lower():
        match = re.search(r'VehicleDetail/(\d+)', text, re.IGNORECASE)
        if match:
            return (match.group(1), 'iaai')
    
    # Если это ссылка Copart, извлекаем ID
    if "copart.com" in text.lower() and "/lot/" in text.lower():
        match = re.search(r'/lot/(\d+)', text, re.IGNORECASE)
        if match:
            return (match.group(1), 'copart')
    
    # Если это просто число, по умолчанию используем Copart
    if text.isdigit():
        return (text, 'copart')
    
    return (None, None)


def detect_parser_type(text: str) -> str:
    """Определяет тип парсера по тексту или ссылке"""
    text = text.strip().lower()
    if "iaai.com" in text:
        return 'iaai'
    elif "copart.com" in text:
        return 'copart'
    return 'copart'  # По умолчанию


async def send_lot_card(message: types.Message, lot_id: str, parser_type: str):
    """Парсит лот и отправляет пользователю полную карточку (фото + характеристики).

    Используется и обработчиком прямых ссылок (handle_lot), и модулем подбора
    car_finder после выбора конкретного лота.
    """
    await message.answer(f"Ищу информацию на {parser_type.upper()}...")

    # Run synchronous parser in thread pool with timeout
    loop = asyncio.get_event_loop()
    try:
        if parser_type == 'iaai':
            data = await asyncio.wait_for(
                loop.run_in_executor(executor, parse_iaai_lot, lot_id),
                timeout=180.0  # переиспользуемая сессия; холодный старт ~1 мин
            )
        else:  # copart
            data = await asyncio.wait_for(
                loop.run_in_executor(executor, get_lot_data, lot_id),
                timeout=180.0  # Chrome launch + Incapsula wait
            )
    except asyncio.TimeoutError:
        logger.error(f"Parser timeout for lot {lot_id}")
        await message.answer(
            "⏱ Превышено время ожидания (Chrome/сайт не ответили). "
            "Попробуйте снова через минуту."
        )
        return

    if "error" in data:
        logger.error(f"Parser error for lot {lot_id}: {data['error']}")
        await message.answer(f"❌ Ошибка: {data['error']}")
        return

    logger.info(f"Successfully parsed lot {lot_id}: {data.get('title', 'N/A')}")

    try:
        photos = data.get("photos", [])
        temp_files = []
        if photos:
            try:
                downloaded_photos = []
                # Параллельная загрузка фото одной сессией (до 8 одновременно)
                sem = asyncio.Semaphore(8)
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async def _dl(u):
                        async with sem:
                            return await download_photo(session, u)
                    raw_list = await asyncio.gather(*[_dl(u) for u in photos])

                for i, raw_bytes in enumerate(raw_list):
                    if raw_bytes:
                        # Нормализуем картинку — иначе Telegram роняет всю
                        # медиагруппу с PHOTO_INVALID_DIMENSIONS
                        clean = sanitize_photo(raw_bytes)
                        if clean is None:
                            logger.warning(f"Photo {i+1}: не читается как изображение, пропускаю")
                            continue

                        # Save to temp file — FSInputFile is more reliable than BufferedInputFile for media groups
                        tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                        tmp.write(clean)
                        tmp.close()
                        temp_files.append(tmp.name)

                        input_file = FSInputFile(tmp.name, filename=f"photo_{i}.jpg")
                        downloaded_photos.append(types.InputMediaPhoto(media=input_file))
                        logger.info(f"Successfully downloaded photo {i+1}")
                    else:
                        logger.warning(f"Failed to download photo {i+1}")

                logger.info(f"Photos: {len(photos)} URLs found, {len(downloaded_photos)} downloaded successfully")

                if downloaded_photos:
                    total = len(downloaded_photos)

                    # Telegram принимает не больше 10 фото в медиагруппе
                    batch_size = 10

                    for batch_start in range(0, total, batch_size):
                        batch = downloaded_photos[batch_start:batch_start + batch_size]

                        await message.answer_media_group(batch)
                        logger.info(f"Sent batch of {len(batch)} photos ({batch_start+1}-{min(batch_start+batch_size, total)})")

                        # Pause between batches to prevent Telegram from dropping files
                        if batch_start + batch_size < total:
                            await asyncio.sleep(1.5)
                else:
                    logger.warning("No photos could be downloaded")
                    await message.answer("Не удалось скачать фотографии")

            except Exception as e:
                logger.error(f"Error sending photos: {e}", exc_info=True)
                await message.answer(f"Не удалось отправить фото: {str(e)}")
            finally:
                # Clean up temp files
                for path in temp_files:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

        # Then send text information based on parser type
        if parser_type == 'iaai':
            text = format_iaai_data(data)
        else:  # copart
            text = format_copart_data(data)

        await message.answer(text)
        logger.info("Message sent successfully")

    except Exception as e:
        logger.error(f"Error in send_lot_ca  rd: {e}", exc_info=True)
        await message.answer("Произошла ошибка. Попробуйте снова.")


@dp.message()
async def handle_lot(message: types.Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    # Пока активен FSM-диалог car_finder (или нажата его кнопка) —
    # отдаём апдейт роутеру модуля подбора.
    if text == FIND_BUTTON_TEXT or await state.get_state() is not None:
        raise SkipHandler()
    try:
        logger.info(f"User {message.from_user.id} sent: {text}")

        # Извлекаем ID лота и тип парсера из текста или ссылки
        lot_id, parser_type = extract_lot_id(text)
        if not lot_id:
            await message.answer("❌ Неверный формат. Отправьте ID лота (например: 49511256) или ссылку Copart/IAAI")
            return

        logger.info(f"Extracted lot ID: {lot_id}, parser type: {parser_type}")
        await send_lot_card(message, lot_id, parser_type)

    except Exception as e:
        logger.error(f"Error in handle_lot: {e}", exc_info=True)
        await message.answer("Произошла ошибка. Попробуйте снова.")


# Словарь перевода повреждений на русский
DAMAGE_TRANSLATIONS = {
    "normal wear": "Нормальный износ",
    "mechanical": "Механические повреждения",
    "front end": "Передняя часть",
    "rear end": "Задняя часть",
    "side": "Боковое повреждение",
    "undercarriage": "Днище",
    "all over": "Повреждения по всему кузову",
    "burn": "Пожар",
    "biohazard": "Биологическая опасность",
    "electrical": "Электрические повреждения",
    "engine damage": "Повреждение двигателя",
    "flood": "Затопление",
    "hail": "Град",
    "partial/ime": "Частичное/IME",
    "rejected repair": "Отклоненный ремонт",
    "rollover": "Переворот",
    "salt water": "Соленая вода",
    "stripped": "Разобран",
    "total burnout": "Полное выгорание",
    "unknown": "Неизвестно",
    "vandalism": "Вандализм",
    "vandalized": "Вандализм",
    "frame damage": "Повреждение рамы",
    "minor dent/scratches": "Незначительные вмятины/царапины",
    "missing/altered vin": "Отсутствующий/измененный VIN",
    "water/flood": "Вода/Затопление",
    "roof": "Крыша",
    "left front": "Левая передняя часть",
    "right front": "Правая передняя часть",
    "left rear": "Левая задняя часть",
    "right rear": "Правая задняя часть",
    "left side": "Левая сторона",
    "right side": "Правая сторона",
    "suspension": "Подвеска",
    "transmission": "Трансмиссия",
    "none": "Нет",
    "damage history": "История повреждений",
}


def translate_damage(text: str) -> str:
    """Переводит описание повреждения на русский"""
    text_lower = text.lower()
    clean = re.sub(r"^(Primary|Secondary|Loss):\s*", "", text, flags=re.IGNORECASE).strip()
    for eng, rus in DAMAGE_TRANSLATIONS.items():
        if eng in clean.lower():
            prefix = ""
            if text_lower.startswith("primary:"):
                prefix = "Primary: "
            elif text_lower.startswith("secondary:"):
                prefix = "Secondary: "
            elif text_lower.startswith("loss:"):
                prefix = "Loss: "
            return f"{prefix}{rus}"
    return text


def parse_price_value(price_str: str) -> float:
    """Извлекает числовое значение из строки цены"""
    try:
        clean = price_str.replace("$", "").replace(",", "").replace("~", "").strip()
        return float(clean)
    except (ValueError, AttributeError):
        return 0.0


def build_delivery_block(car_price: float, duty_usd: float,
                         location: str, title: str, year: int, engine_cc: int) -> str:
    """Формирует один вариант доставки с автоматическим выбором порта.

    Порт выбирается по правилам заказчика:
    - С 2021 года и объём ≥2000 см³ → Поти (Грузия)
    - До 2020 года (включительно) или объём <2000 см³ → Клайпеда (Литва)

    Возвращает готовый текстовый блок с таможней, доставкой и итогом.
    """
    vehicle_type = guess_vehicle_type(title)
    type_ru = {"Car": "легковой", "SUV": "внедорожник",
               "Pickup": "пикап", "Motorcycle": "мотоцикл"}.get(vehicle_type, "авто")

    dest = select_destination_port(year, engine_cc)
    dest_name = "Клайпеду (Литва) 🇱🇹" if dest == "klaipeda" else "Поти (Грузия) 🇬🇪"

    ship = calculate_shipping(location, vehicle_type, dest)
    block = f"📦 Доставка через {dest_name}\n"
    if engine_cc > 0:
        block += f"🛃 Таможня (налог) РБ: ~{duty_usd:,.0f}$\n"
    else:
        block += "🛃 Таможня (налог) РБ: нет данных о двигателе — уточнит менеджер\n"
    if ship["found"] and ship["total_usd"] is not None:
        total = car_price + duty_usd + ship["total_usd"] + SERVICE_FEE_USD
        total_label = "🇧🇾 Итого под ключ" if engine_cc > 0 else "🇧🇾 Итого под ключ (без таможни)"
        block += (
            f"🚢 Доставка ({type_ru}): ~{ship['total_usd']:,.0f}$ "
            f"(авто до порта ~{ship['land_usd']:,.0f}$ + море ~{ship['sea_usd']:,.0f}$)\n"
            f"{total_label}: ~{total:,.0f}$\n"
        )
    else:
        base = car_price + duty_usd + SERVICE_FEE_USD
        block += (
            f"🚢 Доставка: уточняется у менеджера\n"
            f"🇧🇾 Итого (без доставки): ~{base:,.0f}$\n"
        )
    return block


def format_copart_data(data: dict) -> str:
    """Форматирует данные Copart по шаблону заказчика"""
    title = data.get("title", "Не найдено")
    vin = data.get("vin", "Не найдено")
    odometer = data.get("odometer", "Не найдено")
    current_bid = data.get("current_bid", "Не найдено")
    estimated_value = data.get("estimated_value", "Не найдено")
    sale_date = data.get("sale_date", "Не найдено")
    location = data.get("location", "Не найдено")
    transmission = data.get("transmission", "Не найдено")
    drive_type = data.get("drive_type", "Не найдено")
    engine = data.get("engine", "Не найдено")
    keys = data.get("keys", "Не найдено")
    title_code = data.get("title_code", "Не найдено")
    damages = data.get("damages", [])

    car_price = parse_price_value(current_bid)

    # Расчёт: таможня + доставка (порт выбирается по году и объёму двигателя)
    customs = calculate_total_customs(car_price, engine, title, location)
    engine_cc = parse_engine_cc(engine)
    year = parse_year(title)
    delivery_block = build_delivery_block(car_price, customs["duty_usd"], location, title, year, engine_cc)

    if damages:
        damages_text = "🔧 Повреждения:\n"
        for d in damages:
            damages_text += f"- {translate_damage(d)}\n"
    else:
        damages_text = "🔧 Повреждения:\n- Нет данных\n"

    text = (
        f"🏎 {title}\n\n"
        f"🆔 VIN: {vin}\n\n"
        f"📏 Пробег: {odometer}\n"
        f"💰 Текущая ставка: {current_bid}\n"
        f"📊  Оценочная стоимость: {estimated_value}\n"
        f"📅 Дата продажи: {sale_date}\n"
        f"📍 Локация: {location}\n\n"
        f"⚙️ Трансмиссия: {transmission}\n"
        f"🔄 Привод: {drive_type}\n\n"
        f"🔧 Двигатель: {engine}\n\n"
        f"🔑 Ключи: {keys}\n"
        f"📋 Код титула: {title_code}\n\n"
        f"{damages_text}\n"
        f"{delivery_block}\n"
        f"🛠️ Ориентировочная стоимость ремонта: 1,000-2,300$\n\n"
        f"Для покупки авто необходимо:\n"
        f"1. Написать менеджеру для бронирования автомобиля\n"
        f"2. Заключить договор и оплатить услуги компании\n"
        f"3. Произвести оплату автомобиля и его доставки\n"
        f"Доставка 3-6 месяцев\n\n"
        f"Обращайтесь для покупки авто или консультации:\n"
        f"+375(29)2356060 Роман\n"
        f"Telegram: @svoeavtoby\n"
    )
    return text


def format_iaai_data(data: dict) -> str:
    """Форматирует данные IAAI по шаблону заказчика"""
    title = data.get("title", "Не найдено")
    vin = data.get("vin", "Не найдено")
    odometer = data.get("odometer", "Не найдено")
    current_bid = data.get("buy_now_price", "Не найдено")
    if current_bid == "Не найдено":
        current_bid = data.get("actual_cash_value", "Не найдено")
    estimated_value = data.get("actual_cash_value", "Не найдено")
    sale_date = data.get("auction_date_time", "Не найдено")
    location = data.get("vehicle_location", "Не найдено")
    transmission = data.get("transmission", "Не найдено")
    drive_type = data.get("drive_line", "Не найдено")
    engine = data.get("engine", "Не найдено")
    keys = data.get("key", "Не найдено")
    title_doc = data.get("title_doc", "Не найдено")
    title_doc_brand = data.get("title_doc_brand", "Не найдено")
    title_code = f"{title_doc} {title_doc_brand}" if title_doc != "Не найдено" or title_doc_brand != "Не найдено" else "Не найдено"
    damages = data.get("damages", [])

    car_price = parse_price_value(current_bid)

    # Расчёт: таможня + доставка (порт выбирается по году и объёму двигателя)
    customs = calculate_total_customs(car_price, engine, title, location)
    engine_cc = parse_engine_cc(engine)
    year = parse_year(title)
    delivery_block = build_delivery_block(car_price, customs["duty_usd"], location, title, year, engine_cc)

    if damages:
        damages_text = "🔧 Повреждения:\n"
        for d in damages:
            damages_text += f"- {translate_damage(d)}\n"
    else:
        damages_text = "🔧 Повреждения:\n- Нет данных\n"

    text = (
        f"🏎 {title}\n\n"
        f"🆔 VIN: {vin}\n\n"
        f"📏 Пробег: {odometer}\n"
        f"💰 Текущая ставка: {current_bid}\n"
        f"📊 Оценочная стоимость: {estimated_value}\n"
        f"📅 Дата продажи: {sale_date}\n"
        f"📍 Локация: {location}\n\n"
        f"⚙️ Трансмиссия: {transmission}\n"
        f"🔄 Привод: {drive_type}\n\n"
        f"🔧 Двигатель: {engine}\n\n"
        f"🔑 Ключи: {keys}\n"
        f"📋 Код титула: {title_code}\n\n"
        f"{damages_text}\n"
        f"{delivery_block}\n"
        f"🛠️ Ориентировочная стоимость ремонта: 1,000-2,300$\n\n"
        f"Для покупки авто необходимо:\n"
        f"1. Написать менеджеру для бронирования автомобиля\n"
        f"2. Заключить договор и оплатить услуги компании\n"
        f"3. Произвести оплату автомобиля и его доставки\n"
        f"Доставка 3-6 месяцев\n\n"
        f"Обращайтесь для покупки авто или консультации:\n"
        f"+375(29)2356060 Роман\n"
        f"Telegram: @svoeavtoby\n"
    )
    return text


# Передаём модулю подбора функции основного бота (без циклического импорта)
configure_car_finder(
    send_lot_card=send_lot_card,
    extract_lot_id=extract_lot_id,
    executor=executor,
)


async def main():
    logger.info("Starting bot...")

    # Подключаемся к PostgreSQL для кэша справочника марок/моделей car_finder
    try:
        await init_catalog_db()
    except Exception as e:
        logger.warning(f"Catalog DB init failed (car_finder будет без БД-кэша): {e}")

    # Прогрев chromedriver при старте (скачивание не должно блокировать парсинг лота)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            executor,
            lambda: ensure_patched_driver(version_main=get_chrome_major_version()),
        )
        logger.info("Chromedriver pre-warmed.")
    except Exception as e:
        logger.warning(f"Chromedriver pre-warm failed (will retry on first parse): {e}")

    # Set up bot commands
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Показать справку"),
        BotCommand(command="find", description="Подобрать авто по марке и модели"),
        BotCommand(command="restart", description="Перезапустить бота"),
    ]
    await bot(SetMyCommands(commands=commands))
    logger.info("Bot commands set.")

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot polling error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)