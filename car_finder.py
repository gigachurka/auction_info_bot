"""FSM-модуль подбора автомобилей Copart / IAAI для Telegram-бота.

Подключается к основному боту через `car_finder_router`:

    from car_finder import car_finder_router, configure_car_finder, init_catalog_db

    dp.include_router(car_finder_router)
    configure_car_finder(
        send_lot_card=send_lot_card,       # bot.py: парсинг + отправка карточки лота
        extract_lot_id=extract_lot_id,     # bot.py: (lot_id, 'copart'|'iaai') из текста
        executor=executor,                 # ThreadPoolExecutor для Selenium
    )
    await init_catalog_db()                # в main(), до start_polling

Справочник марок/моделей — гибридный: сначала читается из PostgreSQL
(быстро, без лишних запросов к сайтам), а при отсутствии/устаревании
данных подтягивается с сайта аукциона «на лету» и дописывается в базу.
Все запросы к Copart/IAAI идут через авторизованную сессию Chrome
(Incapsula), по тому же принципу, что и parser.py.
"""

import asyncio
import atexit
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import undetected_chromedriver as uc
from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait

from chrome_utils import create_uc_driver, chrome_user_agent, get_chrome_major_version
from iaai_parser import COOKIE_FILE as IAAI_COOKIE_FILE
from iaai_parser import load_cookies as iaai_load_cookies
from iaai_parser import save_cookies as iaai_save_cookies

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константы и настройки
# ---------------------------------------------------------------------------

FIND_BUTTON_TEXT = "🔍 Подобрать авто"
MANUAL_BUTTON_TEXT = "✏️ Ввести ссылку вручную"
BACK_BUTTON_TEXT = "◀️ Назад"
CANCEL_BUTTON_TEXT = "❌ Отмена"
SEARCH_BUTTON_TEXT = "🔎 Поиск марки"

try:
    from config import DATABASE_URL as _DB_URL
except Exception:
    _DB_URL = ""
DATABASE_URL = (os.environ.get("DATABASE_URL") or _DB_URL or "").strip()
CATALOG_TTL = timedelta(days=int(os.environ.get("CATALOG_TTL_DAYS", "7") or 7))

PAGE_SIZE = 10          # марок/моделей на странице
LOTS_PAGE_SIZE = 8      # лотов на странице
LOTS_PER_SEARCH = 50    # сколько лотов запрашиваем у аукциона за раз
CATALOG_TIMEOUT = 150   # сек, на обновление справочника через Chrome
LOTS_TIMEOUT = 150      # сек, на поиск лотов через Chrome

SOURCES = {"copart": "Copart 🇺🇸", "iaai": "IAAI 🇺🇸", "both": "Copart + IAAI 🇺🇸"}

# Публичные JSON-эндпоинты, которые дёргает сам фронтенд аукционов.
# Вызываются fetch()'ем внутри браузерной сессии (иначе режет Incapsula).
COPART_SEARCH_URL = "https://www.copart.com/public/lots/search-results"
COPART_MAKES_PAGE = "https://www.copart.com/vehicleFinder"
# IAAI: старый anvis API отключён — идём через SPA-страницу поиска в DOM.
IAAI_SEARCH_PAGE = "https://www.iaai.com/Search"
IAAI_VEHICLES_PAGE = "https://www.iaai.com/vehicles"

# Минимальное количество лотов в фасете, чтобы позиция попала в справочник
# (отсекает служебные коды вроде "7tbi"/"Acur" из facet MAKE)
MIN_MAKE_FACET_COUNT = 20
MIN_MODEL_FACET_COUNT = 3

# ---------------------------------------------------------------------------
# Внедрение зависимостей из bot.py (избегаем циклического импорта)
# ---------------------------------------------------------------------------

_deps = {
    "send_lot_card": None,   # async (message, lot_id, parser_type)
    "extract_lot_id": None,  # (text) -> (lot_id|None, 'copart'|'iaai'|None)
    "executor": None,        # ThreadPoolExecutor
}


def configure_car_finder(send_lot_card=None, extract_lot_id=None, executor=None):
    """Передаёт функции основного бота в модуль. Вызывать один раз при старте."""
    if send_lot_card is not None:
        _deps["send_lot_card"] = send_lot_card
    if extract_lot_id is not None:
        _deps["extract_lot_id"] = extract_lot_id
    if executor is not None:
        _deps["executor"] = executor


# ---------------------------------------------------------------------------
# Кэш справочника: PostgreSQL (asyncpg) + fallback на память
# ---------------------------------------------------------------------------

_pool = None
_mem = {"makes": {}, "models": {}, "meta": {}}  # fallback, если БД не настроена

_DDL = """
CREATE TABLE IF NOT EXISTS car_makes (
    id         SERIAL PRIMARY KEY,
    source     TEXT NOT NULL,
    name       TEXT NOT NULL,
    query      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, name)
);
CREATE TABLE IF NOT EXISTS car_models (
    id         SERIAL PRIMARY KEY,
    source     TEXT NOT NULL,
    make_name  TEXT NOT NULL,
    name       TEXT NOT NULL,
    query      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, make_name, name)
);
CREATE TABLE IF NOT EXISTS catalog_meta (
    source       TEXT NOT NULL,
    catalog_key  TEXT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, catalog_key)
);
ALTER TABLE car_makes ADD COLUMN IF NOT EXISTS query TEXT NOT NULL DEFAULT '';
ALTER TABLE car_models ADD COLUMN IF NOT EXISTS query TEXT NOT NULL DEFAULT '';
"""


async def init_catalog_db():
    """Создаёт пул соединений и таблицы. Без DATABASE_URL работает in-memory."""
    global _pool
    if not DATABASE_URL:
        logger.warning(
            "car_finder: DATABASE_URL не задан — справочник марок/моделей "
            "будет кэшироваться только в памяти (до перезапуска)."
        )
        return
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
        async with _pool.acquire() as conn:
            await conn.execute(_DDL)
        logger.info("car_finder: PostgreSQL catalog ready.")
    except Exception as e:
        _pool = None
        logger.error(f"car_finder: не удалось подключиться к PostgreSQL ({e}). "
                     "Используется in-memory кэш.")


async def close_catalog_db():
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None


def _norm_item(item):
    """Приводит элемент справочника к виду {'name': str, 'query': str}."""
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        return {"name": name, "query": str(item.get("query") or "")}
    return {"name": str(item).strip(), "query": ""}


async def _db_get_makes(source):
    """Возвращает список {'name','query'} марок из кэша."""
    if _pool is None:
        return sorted(_mem["makes"].get(source, {}).values(),
                      key=lambda x: x["name"])
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, query FROM car_makes WHERE source=$1 ORDER BY name",
            source)
    return [{"name": r["name"], "query": r["query"]} for r in rows]


async def _db_put_makes(source, items):
    items = [_norm_item(i) for i in items]
    items = [i for i in items if i["name"]]
    if not items:
        return
    if _pool is None:
        _mem["makes"].setdefault(source, {}).update(
            {i["name"]: i for i in items})
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO car_makes (source, name, query) VALUES ($1, $2, $3) "
            "ON CONFLICT (source, name) "
            "DO UPDATE SET updated_at = now(), query = EXCLUDED.query",
            [(source, i["name"], i["query"]) for i in items],
        )


async def _db_get_models(source, make_name):
    if _pool is None:
        return sorted(_mem["models"].get((source, make_name), {}).values(),
                      key=lambda x: x["name"])
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, query FROM car_models WHERE source=$1 AND make_name=$2 "
            "ORDER BY name", source, make_name)
    return [{"name": r["name"], "query": r["query"]} for r in rows]


async def _db_put_models(source, make_name, items):
    items = [_norm_item(i) for i in items]
    items = [i for i in items if i["name"]]
    if not items:
        return
    if _pool is None:
        _mem["models"].setdefault((source, make_name), {}).update(
            {i["name"]: i for i in items})
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO car_models (source, make_name, name, query) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (source, make_name, name) "
            "DO UPDATE SET updated_at = now(), query = EXCLUDED.query",
            [(source, make_name, i["name"], i["query"]) for i in items],
        )


async def _db_get_meta(source, key):
    if _pool is None:
        return _mem["meta"].get((source, key))
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT refreshed_at FROM catalog_meta WHERE source=$1 AND catalog_key=$2",
            source, key)
    if row is None:
        return None
    ts = row["refreshed_at"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


async def _db_set_meta(source, key):
    if _pool is None:
        _mem["meta"][(source, key)] = datetime.now(timezone.utc)
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO catalog_meta (source, catalog_key) VALUES ($1, $2) "
            "ON CONFLICT (source, catalog_key) DO UPDATE SET refreshed_at = now()",
            source, key)


def _is_stale(ts):
    if ts is None:
        return True
    return datetime.now(timezone.utc) - ts > CATALOG_TTL


# ---------------------------------------------------------------------------
# Браузерная сессия и fetch JSON внутри неё (обход Incapsula)
# ---------------------------------------------------------------------------

_FETCH_JS = """
var url = arguments[0], method = arguments[1], body = arguments[2];
var cb = arguments[arguments.length - 1];
fetch(url, {
    method: method,
    credentials: 'include',
    headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined
}).then(function (r) {
    return r.text().then(function (t) {
        try { cb(JSON.parse(t)); }
        catch (e) { cb({__status: r.status, __text: t.slice(0, 300)}); }
    });
}).catch(function (e) { cb({__error: String(e)}); });
"""


# Постоянные профили Chrome: Incapsula привязывает доверие к fingerprint'у
# браузера, и чистый профиль каждый раз получает капчу заново.
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "chrome_profiles")


def _new_driver(headless=True, profile_name=None):
    chrome_version = get_chrome_major_version()
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=en-US")
    options.add_argument(f"--user-agent={chrome_user_agent(chrome_version)}")
    user_data_dir = None
    if profile_name:
        user_data_dir = os.path.join(_PROFILE_DIR, profile_name)
        os.makedirs(user_data_dir, exist_ok=True)
    return create_uc_driver(options=options, headless=headless,
                            version_main=chrome_version,
                            user_data_dir=user_data_dir)


def _open_session(host, use_cookies=False):
    """Открывает Chrome на сайте аукциона и ждёт авто-прохождения Incapsula."""
    driver = _new_driver(headless=True, profile_name="copart")
    try:
        driver.get(f"https://{host}/")
        time.sleep(random.uniform(2, 4))
        if use_cookies and os.path.exists(IAAI_COOKIE_FILE):
            iaai_load_cookies(driver)
            driver.refresh()
            time.sleep(random.uniform(2, 3))
        # JS-челлендж Incapsula решается сам — ждём до 60с
        for _ in range(60):
            page = driver.page_source
            if len(page) > 5000 and "_Incapsula_Resource" not in page[:3000]:
                break
            time.sleep(1)
        page = driver.page_source
        if "Access Denied" in page or (
            "_Incapsula_Resource" in page and len(page) < 5000
        ):
            logger.warning(f"car_finder: {host} заблокировал сессию (Incapsula)")
            driver.quit()
            return None
        return driver
    except Exception as e:
        logger.error(f"car_finder: не удалось открыть сессию {host}: {e}")
        try:
            driver.quit()
        except Exception:
            pass
        return None


def _page_is_captcha(src):
    return ("_Incapsula_Resource" in src and len(src) < 2000) \
        or "h-captcha" in src.lower() or len(src) < 2000


def _iaai_profile_warmed():
    """Профиль IAAI уже проходил проверку — куки/fingerprint на диске."""
    return os.path.exists(IAAI_COOKIE_FILE)


def _open_iaai_session():
    """Сессия на iaai.com на постоянном профиле chrome_profiles/iaai.

    Куки и fingerprint Incapsula живут в профиле — капчу решаем один раз
    (видимое окно), дальше сессии переиспользуются, в т.ч. после рестарта.
    """
    warmed = _iaai_profile_warmed()
    driver = _new_driver(headless=warmed, profile_name="iaai")
    try:
        driver.get("https://www.iaai.com/")
        time.sleep(random.uniform(2, 3))

        # Авто-прохождение JS-челленджа Incapsula (поллинг до 60с)
        solved = False
        for _ in range(60):
            src = driver.page_source
            if not _page_is_captcha(src):
                solved = True
                break
            if "h-captcha" in src.lower():
                break  # ручная капча — ждать бесполезно
            time.sleep(1)

        if not solved:
            if warmed:
                # доверие потеряно (сменился IP/протухло) — сброс флага,
                # следующий вызов откроет видимое окно для ручного прохода
                try:
                    os.remove(IAAI_COOKIE_FILE)
                except OSError:
                    pass
                driver.quit()
                return None
            logger.info("car_finder: IAAI h-captcha — жду ручного решения (180с)")
            for _ in range(180):
                time.sleep(1)
                if not _page_is_captcha(driver.page_source):
                    solved = True
                    break
            if not solved:
                driver.quit()
                return None
            time.sleep(random.uniform(3, 5))
            # флаг «профиль прогрет» — дальше ходим headless
            iaai_save_cookies(driver)

        if "Access Denied" in driver.page_source:
            driver.quit()
            return None
        return driver
    except Exception as e:
        logger.error(f"car_finder: не удалось открыть сессию IAAI: {e}")
        try:
            driver.quit()
        except Exception:
            pass
        return None


def _fetch_json(driver, url, method="GET", body=None):
    try:
        driver.set_script_timeout(60)
        data = driver.execute_async_script(_FETCH_JS, url, method, body)
        if isinstance(data, dict) and ("__error" in data or "__status" in data):
            logger.warning(f"car_finder: fetch {url} -> {data}")
            return None
        return data
    except Exception as e:
        logger.warning(f"car_finder: fetch {url} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Переиспользуемые браузерные сессии: один живой Chrome на источник
# ---------------------------------------------------------------------------

_drivers = {"copart": None, "iaai": None}
_driver_locks = {"copart": threading.Lock(), "iaai": threading.Lock()}


def _driver_alive(d):
    try:
        d.execute_script("return 1")
        return True
    except Exception:
        return False


def _new_session(source):
    if source == "iaai":
        return _open_iaai_session()
    return _open_session("www.copart.com")


def _with_driver(source, fn):
    """Выполняет fn(driver) на живой сессии источника.

    Кэшированная сессия может протухнуть (Incapsula) — тогда пересоздаём
    и повторяем один раз. Возвращает [] при полной неудаче.
    """
    lock = _driver_locks["iaai" if source == "iaai" else "copart"]
    with lock:
        for attempt in range(2):
            drv = _drivers.get(source)
            was_cached = drv is not None
            if drv is None or not _driver_alive(drv):
                drv = _new_session(source)
                _drivers[source] = drv
                was_cached = False
            if drv is None:
                return []
            result = fn(drv)
            if result or not was_cached or attempt == 1:
                return result
            logger.info(f"car_finder: {source} session stale — recreating")
            try:
                drv.quit()
            except Exception:
                pass
            _drivers[source] = None
        return []


def close_sessions():
    """Закрывает все живые Chrome-сессии пула. Публичная обёртка —
    вызывать перед перезапуском процесса, чтобы не остались сироты."""
    _close_drivers()


@atexit.register
def _close_drivers():
    for key in list(_drivers):
        try:
            if _drivers[key] is not None:
                _drivers[key].quit()
        except Exception:
            pass
        _drivers[key] = None


def _wait_dom(driver, css, timeout=30):
    """Ждёт появления элемента; быстрее фиксированного sleep при быстрой загрузке."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script(
                    f"return !!document.querySelector({css!r})"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_cards(driver, timeout=15, grace=4):
    """Ждёт карточки VehicleDetail, но выходит рано, если страница
    полностью загрузилась и `grace` секунд карточек нет — значит, для этой
    марки/категории их не будет, и ждать дальше бесполезно."""
    end = time.time() + timeout
    loaded_at = None
    while time.time() < end:
        try:
            if driver.execute_script(
                    "return !!document.querySelector(\"a[href*='VehicleDetail/']\")"):
                return True
            ready = driver.execute_script(
                "return document.readyState === 'complete'")
            if ready:
                if loaded_at is None:
                    loaded_at = time.time()
                elif time.time() - loaded_at >= grace:
                    return False
            else:
                loaded_at = None
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Парсинг ответов API (защитно: несколько вариантов структур)
# ---------------------------------------------------------------------------

def _dig(obj, *path):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


def _first(dct, keys):
    for k in keys:
        v = dct.get(k)
        if v not in (None, "", 0):
            return v
    return None


def _iter_lot_dicts(resp):
    """Вытаскивает список лотов из любого известного формата ответа."""
    if not isinstance(resp, dict):
        return []
    for path in (("data", "results", "content"), ("data", "results", "results"),
                 ("Results",), ("results",), ("content",), ("lots",)):
        items = _dig(resp, *path)
        if isinstance(items, list) and items:
            return [i for i in items if isinstance(i, dict)]
    return []


def _copart_facet(resp, code, min_count=1):
    """Справочник из facetFields по quickPickCode: [{'name', 'query'}].

    facetCounts элемент: {'columnName': 'lot_make_desc', 'count': 4108,
    'displayName': 'Acura', 'query': 'lot_make_desc:"ACURA"'}.
    """
    facets = _dig(resp, "data", "results", "facetFields") or []
    out = []
    for f in facets:
        if f.get("quickPickCode") != code:
            continue
        for c in f.get("facetCounts") or []:
            name = (c.get("displayName") or "").strip()
            if not name or "unknown" in name.lower():
                continue
            if (c.get("count") or 0) < min_count:
                continue
            out.append({"name": name, "query": c.get("query") or ""})
    return out


def _dedupe_items(items):
    """Дедупликация по имени (без учёта регистра) + сортировка по алфавиту."""
    seen = {}
    for i in items:
        i = _norm_item(i)
        if i["name"] and i["name"].upper() not in seen:
            seen[i["name"].upper()] = i
    return sorted(seen.values(), key=lambda x: x["name"].upper())


def _collect_field(resp, keys):
    """Собирает уникальные значения поля из списка лотов ответа."""
    out = set()
    for lot in _iter_lot_dicts(resp):
        v = _first(lot, keys)
        if v is not None:
            out.add(str(v).strip())
    return out


def _fmt_bid(value):
    try:
        v = float(value)
        return f"${v:,.0f}" if v > 0 else None
    except (TypeError, ValueError):
        return str(value) if value else None


def _norm_lot_copart(raw):
    """Лот Copart: ln/lotNumberStr, ld — полный заголовок, lcy — год,
    dynamicLotDetails.currentBid / bnp — цена, yn — площадка."""
    lot_id = raw.get("lotNumberStr") or raw.get("ln")
    if not lot_id:
        return None
    title = raw.get("ld") or " ".join(
        str(p) for p in (raw.get("lcy"), raw.get("mkn"), raw.get("lm")) if p)
    bid = _fmt_bid(_dig(raw, "dynamicLotDetails", "currentBid"))
    if not bid:
        bid = _fmt_bid(raw.get("bnp"))
    return {
        "id": str(lot_id),
        "title": str(title).strip()[:60] or "Без названия",
        "bid": bid,
        "location": raw.get("yn") or "",
        "source": "copart",
    }


# ---------------------------------------------------------------------------
# Загрузчики справочника и лотов (sync, выполняются в executor)
# ---------------------------------------------------------------------------

def _copart_search(driver, query="*", filters=None, page=0, size=20):
    """POST /public/lots/search-results — тело строго минимальное:
    лишние поля (sort, searchName и т.п.) дают 400 Invalid request body."""
    body = {"query": [query], "filter": filters or {}, "page": page, "size": size}
    return _fetch_json(driver, COPART_SEARCH_URL, "POST", body)


def _make_query(name):
    """Фильтр-запрос марки Copart, если не пришёл готовый facet query."""
    return f'lot_make_desc:"{name.upper()}"'


def _model_query(name):
    return f'lot_model_desc:"{name.upper()}"'


def _copart_makes_dom_fallback(driver):
    """Запасной вариант: марки — ссылки vehicle-search-make/ на /vehicleFinder."""
    items = []
    try:
        driver.get(COPART_MAKES_PAGE)
        time.sleep(random.uniform(3, 5))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for a in soup.find_all("a", href=True):
            if "vehicle-search-make/" in a["href"]:
                text = a.get_text(strip=True)
                if text and 1 < len(text) < 40:
                    items.append({"name": text, "query": _make_query(text)})
    except Exception as e:
        logger.warning(f"car_finder: copart DOM fallback failed: {e}")
    return items


def _copart_catalog(make=None):
    """Марки Copart (make=None) или модели марки.

    make — dict {'name','query'} из справочника.
    Источник: facetFields ответа /public/lots/search-results.
    """
    def work(driver):
        if make is None:
            resp = _copart_search(driver, size=1)
            items = _copart_facet(resp, "MAKE", MIN_MAKE_FACET_COUNT)
            if not items:
                items = _copart_makes_dom_fallback(driver)
            return _dedupe_items(items)

        mq = make.get("query") or _make_query(make["name"])
        resp = _copart_search(driver, filters={"MAKE": [mq]}, size=1)
        items = _copart_facet(resp, "MODL", MIN_MODEL_FACET_COUNT)
        if not items:
            # Fallback: уникальные модели из первых страниц выдачи по марке
            names = set()
            for p in range(3):
                resp = _copart_search(driver, filters={"MAKE": [mq]},
                                      page=p, size=100)
                lots = _iter_lot_dicts(resp)
                for l in lots:
                    v = l.get("lm") or l.get("lmtd")
                    if v:
                        names.add(str(v).strip().upper())
                if not lots:
                    break
            items = [{"name": n, "query": _model_query(n)} for n in names]
        return _dedupe_items(items)

    return _with_driver("copart", work)


def _copart_lots(make, model):
    """Актуальные лоты Copart по марке и модели (dicts {'name','query'})."""
    def work(driver):
        mq = make.get("query") or _make_query(make["name"])
        mdq = model.get("query") or _model_query(model["name"])
        resp = _copart_search(
            driver, filters={"MAKE": [mq], "MODL": [mdq]},
            size=LOTS_PER_SEARCH)
        lots = [l for l in map(_norm_lot_copart, _iter_lot_dicts(resp)) if l]
        if not lots:
            # Фолбэк: свободный поиск "make model"
            resp = _copart_search(
                driver, query=f"{make['name']} {model['name']}",
                size=LOTS_PER_SEARCH)
            lots = [l for l in map(_norm_lot_copart, _iter_lot_dicts(resp)) if l]
        return lots

    return _with_driver("copart", work)


def _iaai_extract_cards(driver):
    """Карточки лотов из DOM: все ссылки VehicleDetail/<id> на странице.
    Заголовок берём из текстового (не image-) анкора той же карточки."""
    raw = driver.execute_script("""
        var byId={};
        document.querySelectorAll("a[href*='VehicleDetail/']").forEach(function(a){
            var m=(a.getAttribute('href')||'').match(/VehicleDetail\\/(\\d+)/);
            if(!m) return;
            var id=m[1];
            var t=(a.textContent||'').replace(/\\s+/g,' ').trim();
            var rec=byId[id]||{id:id, title:'', text:''};
            // текстовый анкор (название авто) длиннее и не "View All Images"
            if(t && !/view all images/i.test(t) && t.length>rec.title.length)
                rec.title=t;
            var card=a.closest('tr')||a.closest('li')||a.closest('div')||a;
            var ct=(card.textContent||'').replace(/\\s+/g,' ').trim();
            if(ct.length>rec.text.length) rec.text=ct;
            byId[id]=rec;
        });
        return Object.values(byId);
    """) or []
    lots = []
    for c in raw:
        text = c.get("text") or ""
        title = c.get("title") or ""
        if not title:
            m = re.search(r"\b(19|20)\d{2}\s+[A-Z][A-Z0-9 .\-]+", text)
            title = m.group(0) if m else f"IAAI #{c['id']}"
        bid_m = re.search(r"\$[\d,]+", text)
        lots.append({
            "id": str(c["id"]),
            "title": title[:60].strip(),
            "bid": bid_m.group(0) if bid_m else None,
            "location": "",
            "source": "iaai",
        })
    return lots


def _iaai_model_match(title, model_name):
    """Проверяет, что заголовок лота соответствует выбранной модели.

    Таксономии различаются: Copart говорит "5 SERIES", IAAI пишет в тайтле
    код двигателя ("530E", "528I"). Правила:
    - точное вхождение нормализованного имени (F-150 -> F150 и т.п.);
    - BMW-стиль "N SERIES" -> коды Nxx в тайтле (5 SERIES -> 530E/528I...).
    """
    t = title.upper()
    tn = re.sub(r"[\s\-]+", "", t)
    mn = re.sub(r"[\s\-]+", "", model_name.upper())
    if mn and mn in tn:
        return True
    m = re.match(r"^(\d)\s*SERIES?$", model_name.upper())
    if m:
        return bool(re.search(rf"\b{m.group(1)}\d{{2}}[A-Z]{{0,3}}\b", t))
    return False


def _iaai_cards_match_make(lots, make_name):
    """Проверяем, что выдача действительно по нужной марке
    (hash-фильтр IAAI молча отваливается на дефолтную выдачу)."""
    if not lots:
        return False
    mn = re.sub(r"[\s\-]+", "", make_name.upper())
    hits = sum(1 for l in lots
               if mn in re.sub(r"[\s\-]+", "", l["title"].upper()))
    return hits >= max(1, len(lots) // 4)


# Основные категории Vehiclelisting, покрывающие легковые авто
IAAI_CATEGORIES = ["Cars", "SUVs", "Pick-upTrucks", "Vans", "Motorcycles"]


def _iaai_lots_via_listing(driver, make_name, model_name=None):
    """SEO-страницы /Vehiclelisting/{cat}/{make} — стабильнее SPA-поиска."""
    mk = quote(make_name.replace(" ", "-"), safe="")
    lots = []
    for cat in IAAI_CATEGORIES:
        if len(lots) >= LOTS_PER_SEARCH:
            break
        try:
            driver.get(f"https://www.iaai.com/Vehiclelisting/{cat}/{mk}")
            if not _wait_cards(driver, timeout=15, grace=4):
                continue  # страница загрузилась, но карточек нет — дальше
            page_lots = _iaai_extract_cards(driver)
            lots.extend(l for l in page_lots
                        if _iaai_cards_match_make([l], make_name))
        except Exception as e:
            logger.warning(f"car_finder: IAAI listing {cat}/{mk}: {e}")
    # дедуп по id
    seen, uniq = set(), []
    for l in lots:
        if l["id"] not in seen:
            seen.add(l["id"])
            uniq.append(l)
    if model_name:
        # строгая фильтрация по модели: чужие авто хуже, чем пустой список
        uniq = [l for l in uniq if _iaai_model_match(l["title"], model_name)]
    return uniq


def _iaai_lots(make, model):
    """Лоты IAAI по марке/модели.
    1) SPA-поиск #vehicleSearch/Makes=.../Models=... с проверкой результата
    2) фолбэк: SEO-страницы Vehiclelisting + фильтрация по имени модели."""
    def work(driver):
        mk = quote(make["name"], safe="")
        md = quote(model["name"], safe="")
        driver.get(f"{IAAI_SEARCH_PAGE}#vehicleSearch/ALL_LOTS/"
                   f"Makes={mk}/Models={md}/")
        _wait_cards(driver, timeout=18, grace=6)
        lots = _iaai_extract_cards(driver)
        # если фильтр не применился (выдача не про марку) — идём на Vehiclelisting
        if not _iaai_cards_match_make(lots, make["name"]):
            logger.info("car_finder: IAAI hash-filter не сработал — "
                        "fallback на Vehiclelisting")
            lots = _iaai_lots_via_listing(driver, make["name"],
                                          model["name"])
        else:
            # марка совпала — модель фильтруем сами (хэш-фильтр Models= ненадёжен)
            lots = [l for l in lots
                    if _iaai_model_match(l["title"], model["name"])]
        return lots[:LOTS_PER_SEARCH]

    return _with_driver("iaai", work)


def _iaai_catalog(make=None):
    """Марки/модели для IAAI. Публичного справочника у IAAI нет —
    используем универсальную таксономию Copart: её имена совместимы
    с фильтрами Makes=/Models= на iaai.com."""
    cp_make = ({"name": make["name"], "query": _make_query(make["name"])}
               if make else None)
    return _copart_catalog(cp_make)


def _fetch_catalog(source, make=None):
    """Точка входа sync-загрузчика справочника. make — dict или None."""
    try:
        if source == "copart":
            return _copart_catalog(make)
        return _iaai_catalog(make)
    except Exception as e:
        logger.error(f"car_finder: catalog fetch failed ({source}, {make}): {e}")
        return []


def _fetch_lots(source, make, model):
    try:
        if source == "copart":
            return _copart_lots(make, model)
        return _iaai_lots(make, model)
    except Exception as e:
        logger.error(f"car_finder: lots fetch failed ({source}, {make}, {model}): {e}")
        return []


# ---------------------------------------------------------------------------
# Асинхронная обёртка: кэш + lazy-обновление с защитой от гонок
# ---------------------------------------------------------------------------

_refresh_locks = {}


def _lock_for(key):
    if key not in _refresh_locks:
        _refresh_locks[key] = asyncio.Lock()
    return _refresh_locks[key]


def parse_iaai_lot(lot_id):
    """Карточка лота IAAI на пуловой сессии — без нового Chrome и капчи.
    Возвращает dict с данными или {'error': ...}."""
    from iaai_parser import get_iaai_lot_data

    def work(driver):
        res = get_iaai_lot_data(lot_id, shared_driver=driver)
        # капча/блок на общей сессии → None → _with_driver пересоздаст её
        if isinstance(res, dict) and res.get("error"):
            err = str(res["error"])
            if "captcha" in err.lower() or "Access Denied" in err:
                try:
                    os.remove(IAAI_COOKIE_FILE)
                except OSError:
                    pass
                return None
        return res

    res = _with_driver("iaai", work)
    if not res:
        return {"error": "IAAI недоступен — попробуйте позже"}
    return res


def _prewarm_session(source):
    """Фоновый прогрев браузерной сессии источника (запускается в executor).
    Пока пользователь кликает марку/модель — Chrome уже прошёл Incapsula."""
    def noop(driver):
        return [True]
    try:
        _with_driver(source, noop)
    except Exception as e:
        logger.warning(f"car_finder: prewarm {source} failed: {e}")


async def _run_blocking(func, *args, timeout=150):
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_deps["executor"], func, *args), timeout=timeout)


def _merge_items(cached, fresh):
    """Объединяет кэш и свежие позиции по имени (новые дописываются «на лету»)."""
    merged = {i["name"].upper(): i for i in cached}
    for i in fresh:
        merged[i["name"].upper()] = i
    return sorted(merged.values(), key=lambda x: x["name"].upper())


async def get_makes(source):
    """Марки: сначала из БД, при пустоте/устаревании — догрузка с сайта.
    Возвращает [{'name','query'}]. source='both' — объединение каталогов."""
    if source == "both":
        copart = await get_makes("copart")
        iaai = await _db_get_makes("iaai")   # кэш; lazy-refresh IAAI не дёргаем тут
        return _merge_items(copart, iaai)
    async with _lock_for((source, "makes")):
        cached = await _db_get_makes(source)
        fresh_ts = await _db_get_meta(source, "makes")
        if cached and not _is_stale(fresh_ts):
            return cached
        try:
            fresh = await _run_blocking(_fetch_catalog, source, None,
                                        timeout=CATALOG_TIMEOUT)
        except Exception as e:
            logger.error(f"car_finder: makes refresh failed ({source}): {e}")
            fresh = []
        if fresh:
            await _db_put_makes(source, fresh)
            await _db_set_meta(source, "makes")
            return _merge_items(cached, fresh)
        return cached


async def get_models(source, make):
    """Модели марки (make — dict {'name','query'}): кэш + lazy refresh."""
    if source == "both":
        copart = await get_models("copart", make)
        iaai = await _db_get_models("iaai", make["name"])
        return _merge_items(copart, iaai)
    key = f"models:{make['name']}"
    async with _lock_for((source, key)):
        cached = await _db_get_models(source, make["name"])
        fresh_ts = await _db_get_meta(source, key)
        if cached and not _is_stale(fresh_ts):
            return cached
        try:
            fresh = await _run_blocking(_fetch_catalog, source, make,
                                        timeout=CATALOG_TIMEOUT)
        except Exception as e:
            logger.error(
                f"car_finder: models refresh failed ({source}/{make['name']}): {e}")
            fresh = []
        if fresh:
            await _db_put_models(source, make["name"], fresh)
            await _db_set_meta(source, key)
            return _merge_items(cached, fresh)
        return cached


async def search_lots(source, make, model):
    """Лоты ищем всегда вживую — аукционный инвентарь меняется ежедневно.
    make/model — dicts {'name','query'} из справочника.
    source='both' — параллельный запрос на оба аукциона."""
    if source == "both":
        results = await asyncio.gather(
            search_lots("copart", make, model),
            search_lots("iaai", make, model),
        )
        return results[0] + results[1]
    try:
        return await _run_blocking(_fetch_lots, source, make, model,
                                   timeout=LOTS_TIMEOUT)
    except Exception as e:
        logger.error(f"car_finder: lot search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# FSM и клавиатуры
# ---------------------------------------------------------------------------

class CarFinder(StatesGroup):
    choosing_source = State()
    choosing_make = State()
    choosing_model = State()
    choosing_lot = State()
    waiting_make_query = State()
    waiting_link = State()


car_finder_router = Router(name="car_finder")


def _pager_row(page, total, page_cb):
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{page_cb}|{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="cf|noop"))
    if (page + 1) * PAGE_SIZE < total:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"{page_cb}|{page + 1}"))
    return row


def _footer_rows(include_back=True):
    rows = [[InlineKeyboardButton(text=MANUAL_BUTTON_TEXT, callback_data="cf|manual")]]
    tail = []
    if include_back:
        tail.append(InlineKeyboardButton(text=BACK_BUTTON_TEXT, callback_data="cf|back"))
    tail.append(InlineKeyboardButton(text=CANCEL_BUTTON_TEXT, callback_data="cf|cancel"))
    rows.append(tail)
    return rows


def _kb_source():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Copart 🇺🇸", callback_data="cf|src|copart"))
    b.row(InlineKeyboardButton(text="IAAI 🇺🇸", callback_data="cf|src|iaai"))
    b.row(InlineKeyboardButton(text="Copart + IAAI 🌐",
                               callback_data="cf|src|both"))
    for row in _footer_rows(include_back=False):
        b.row(*row)
    return b.as_markup()


_SOURCE_TAGS = {"copart": "CP", "iaai": "IA"}


def _kb_makes(makes_view, page):
    b = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for i, item in enumerate(makes_view[start:start + PAGE_SIZE]):
        b.button(text=item["name"], callback_data=f"cf|mki|{start + i}")
    b.adjust(2)
    b.row(*_pager_row(page, len(makes_view), "cf|mkp"))
    b.row(InlineKeyboardButton(text=SEARCH_BUTTON_TEXT, callback_data="cf|q"))
    for row in _footer_rows():
        b.row(*row)
    return b.as_markup()


def _kb_models(models, page):
    b = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for i, item in enumerate(models[start:start + PAGE_SIZE]):
        b.button(text=item["name"], callback_data=f"cf|mdi|{start + i}")
    b.adjust(2)
    b.row(*_pager_row(page, len(models), "cf|mdp"))
    for row in _footer_rows():
        b.row(*row)
    return b.as_markup()


def _lot_button_text(lot):
    title = lot["title"][:38]
    bid = f" · {lot['bid']}" if lot.get("bid") else ""
    tag = _SOURCE_TAGS.get(lot.get("source"), "")
    tag = f" [{tag}]" if tag else ""
    return f"🚗 {title} · #{lot['id']}{bid}{tag}"


def _kb_lots(lots, page):
    b = InlineKeyboardBuilder()
    start = page * LOTS_PAGE_SIZE
    for i, lot in enumerate(lots[start:start + LOTS_PAGE_SIZE]):
        b.row(InlineKeyboardButton(text=_lot_button_text(lot),
                                   callback_data=f"cf|lti|{start + i}"))
    pages = max(1, (len(lots) + LOTS_PAGE_SIZE - 1) // LOTS_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"cf|ltp|{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="cf|noop"))
    if (page + 1) * LOTS_PAGE_SIZE < len(lots):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"cf|ltp|{page + 1}"))
    b.row(*nav)
    for row in _footer_rows():
        b.row(*row)
    return b.as_markup()


async def _safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await message.answer(text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Handlers: вход в диалог
# ---------------------------------------------------------------------------

@car_finder_router.message(F.text == FIND_BUTTON_TEXT)
@car_finder_router.message(Command("find"))
async def cf_start(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(CarFinder.choosing_source)
    await message.answer(
        "🔍 Подбор автомобиля: марка → модель → лот.\n\n"
        "Шаг 1/4 — выберите аукцион:",
        reply_markup=_kb_source(),
    )


# ---------------------------------------------------------------------------
# Шаг 1 → 2: источник → список марок
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(CarFinder.choosing_source,
                                  F.data.startswith("cf|src|"))
async def cf_pick_source(callback: types.CallbackQuery, state: FSMContext):
    source = callback.data.rsplit("|", 1)[-1]
    if source not in SOURCES:
        await callback.answer("Неизвестный аукцион")
        return
    await state.update_data(source=source)
    await callback.answer()
    # фоновый прогрев браузерных сессий — пока пользователь выбирает марку
    loop = asyncio.get_running_loop()
    for s in (("copart", "iaai") if source == "both" else (source,)):
        loop.run_in_executor(_deps["executor"], _prewarm_session, s)
    await _safe_edit(callback.message,
                     f"⏳ Загружаю список марок {SOURCES[source]}...")
    makes = await get_makes(source)
    if not makes:
        await _safe_edit(
            callback.message,
            "⚠️ Не удалось загрузить список марок. "
            "Попробуйте позже или отправьте ссылку на лот вручную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_footer_rows()))
        return
    await state.update_data(makes=makes, makes_view=makes, make_page=0)
    await state.set_state(CarFinder.choosing_make)
    await _safe_edit(
        callback.message,
        f"Шаг 2/4 — выберите марку ({SOURCES[source]}):",
        reply_markup=_kb_makes(makes, 0))


@car_finder_router.callback_query(CarFinder.choosing_make,
                                  F.data.startswith("cf|mkp|"))
async def cf_makes_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    view = data.get("makes_view") or []
    await state.update_data(make_page=page)
    await callback.answer()
    await _safe_edit(callback.message, "Шаг 2/4 — выберите марку:",
                     reply_markup=_kb_makes(view, page))


# ---------------------------------------------------------------------------
# Шаг 2 → 3: марка → список моделей
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(CarFinder.choosing_make,
                                  F.data.startswith("cf|mki|"))
async def cf_pick_make(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    view = data.get("makes_view") or []
    if not (0 <= idx < len(view)):
        await callback.answer("Список устарел — выберите заново")
        return
    make = view[idx]
    source = data["source"]
    await state.update_data(make=make)
    await callback.answer()
    await _safe_edit(callback.message,
                     f"⏳ Загружаю модели {make['name']}...")
    models = await get_models(source, make)
    if not models:
        await _safe_edit(
            callback.message,
            f"⚠️ Модели для {make['name']} не найдены. "
            "Попробуйте другую марку или отправьте ссылку вручную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_footer_rows()))
        return
    await state.update_data(models=models, model_page=0)
    await state.set_state(CarFinder.choosing_model)
    await _safe_edit(callback.message, f"Шаг 3/4 — модель {make['name']}:",
                     reply_markup=_kb_models(models, 0))


@car_finder_router.callback_query(CarFinder.choosing_model,
                                  F.data.startswith("cf|mdp|"))
async def cf_models_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    models = data.get("models") or []
    make = data.get("make") or {}
    await state.update_data(model_page=page)
    await callback.answer()
    await _safe_edit(callback.message,
                     f"Шаг 3/4 — модель {make.get('name', '')}:",
                     reply_markup=_kb_models(models, page))


# ---------------------------------------------------------------------------
# Шаг 3 → 4: модель → список лотов
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(CarFinder.choosing_model,
                                  F.data.startswith("cf|mdi|"))
async def cf_pick_model(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    models = data.get("models") or []
    if not (0 <= idx < len(models)):
        await callback.answer("Список устарел — выберите заново")
        return
    model = models[idx]
    make, source = data["make"], data["source"]
    await state.update_data(model=model)
    await callback.answer()
    label = f"{make['name']} {model['name']}"
    await _safe_edit(callback.message,
                     f"⏳ Ищу актуальные лоты {label}...")
    lots = await search_lots(source, make, model)
    if not lots:
        await _safe_edit(
            callback.message,
            f"⚠️ Активных лотов {label} сейчас нет. "
            "Попробуйте другую модель или отправьте ссылку вручную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_footer_rows()))
        return
    await state.update_data(lots=lots, lot_page=0)
    await state.set_state(CarFinder.choosing_lot)
    await _safe_edit(
        callback.message,
        f"Шаг 4/4 — лоты {label} (нажмите на авто для карточки):",
        reply_markup=_kb_lots(lots, 0))


@car_finder_router.callback_query(CarFinder.choosing_lot,
                                  F.data.startswith("cf|ltp|"))
async def cf_lots_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    lots = data.get("lots") or []
    await state.update_data(lot_page=page)
    await callback.answer()
    label = " ".join(i["name"] for i in
                     (data.get("make") or {}, data.get("model") or {}) if i)
    await _safe_edit(callback.message,
                     f"Шаг 4/4 — лоты {label}:",
                     reply_markup=_kb_lots(lots, page))


# ---------------------------------------------------------------------------
# Шаг 4: конкретный лот → полная карточка через существующий парсер
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(CarFinder.choosing_lot,
                                  F.data.startswith("cf|lti|"))
async def cf_pick_lot(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.rsplit("|", 1)[-1])
    data = await state.get_data()
    lots = data.get("lots") or []
    if not (0 <= idx < len(lots)):
        await callback.answer("Список устарел — выберите заново")
        return
    lot = lots[idx]
    # В режиме "оба аукциона" источник берём из самого лота
    source = lot.get("source") or data["source"]
    await callback.answer()
    await state.clear()
    try:
        await _deps["send_lot_card"](callback.message, lot["id"], source)
    except Exception as e:
        logger.error(f"car_finder: send_lot_card failed: {e}", exc_info=True)
        await callback.message.answer("Произошла ошибка. Попробуйте снова.")


# ---------------------------------------------------------------------------
# Страховка: ручной ввод ссылки (доступен на любом шаге)
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(F.data == "cf|manual")
async def cf_manual(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CarFinder.waiting_link)
    await callback.answer()
    await _safe_edit(
        callback.message,
        "✏️ Отправьте ссылку на лот Copart/IAAI или просто ID лота.\n\n"
        "Примеры:\n"
        "• https://www.copart.com/lot/49511256\n"
        "• https://www.iaai.com/VehicleDetail/45338047~US\n"
        "• 49511256",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text=CANCEL_BUTTON_TEXT, callback_data="cf|cancel")]]))


@car_finder_router.message(CarFinder.waiting_link, F.text)
async def cf_manual_link(message: types.Message, state: FSMContext):
    text = message.text.strip()
    lot_id, parser_type = _deps["extract_lot_id"](text)
    if not lot_id:
        await message.answer(
            "❌ Не распознал ссылку/ID. Отправьте ссылку на лот Copart или IAAI "
            "(или нажмите «❌ Отмена»).")
        return
    await state.clear()
    try:
        await _deps["send_lot_card"](message, lot_id, parser_type)
    except Exception as e:
        logger.error(f"car_finder: send_lot_card failed: {e}", exc_info=True)
        await message.answer("Произошла ошибка. Попробуйте снова.")


# ---------------------------------------------------------------------------
# Поиск марки текстом
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(CarFinder.choosing_make, F.data == "cf|q")
async def cf_make_search_prompt(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CarFinder.waiting_make_query)
    await callback.answer()
    await _safe_edit(callback.message, "🔎 Введите часть названия марки:")


@car_finder_router.message(CarFinder.waiting_make_query, F.text)
async def cf_make_search(message: types.Message, state: FSMContext):
    text = message.text.strip()
    # Пользователь может сразу скинуть ссылку — обрабатываем как ручной ввод.
    lot_id, parser_type = _deps["extract_lot_id"](text)
    if lot_id:
        await state.clear()
        try:
            await _deps["send_lot_card"](message, lot_id, parser_type)
        except Exception as e:
            logger.error(f"car_finder: send_lot_card failed: {e}", exc_info=True)
            await message.answer("Произошла ошибка. Попробуйте снова.")
        return
    data = await state.get_data()
    makes = data.get("makes") or []
    q = text.upper()
    filtered = [m for m in makes if q in m["name"].upper()]
    if not filtered:
        await message.answer("Ничего не найдено. Уточните запрос "
                             "или нажмите «✏️ Ввести ссылку вручную».")
        return
    await state.update_data(makes_view=filtered, make_page=0)
    await state.set_state(CarFinder.choosing_make)
    await message.answer("Шаг 2/4 — выберите марку:",
                         reply_markup=_kb_makes(filtered, 0))


# ---------------------------------------------------------------------------
# Навигация: назад / отмена / noop
# ---------------------------------------------------------------------------

@car_finder_router.callback_query(F.data == "cf|cancel")
async def cf_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await _safe_edit(callback.message,
                     "❌ Подбор отменён. Чтобы начать заново, нажмите "
                     f"«{FIND_BUTTON_TEXT}» или /find.")


@car_finder_router.callback_query(F.data == "cf|back")
async def cf_back(callback: types.CallbackQuery, state: FSMContext):
    current = await state.get_state()
    data = await state.get_data()
    await callback.answer()
    if current in (CarFinder.choosing_make.state, CarFinder.choosing_source.state,
                   CarFinder.waiting_make_query.state, CarFinder.waiting_link.state):
        await state.set_state(CarFinder.choosing_source)
        await _safe_edit(callback.message,
                         "Шаг 1/4 — выберите аукцион:",
                         reply_markup=_kb_source())
    elif current == CarFinder.choosing_model.state:
        view = data.get("makes_view") or []
        page = data.get("make_page", 0)
        await state.set_state(CarFinder.choosing_make)
        await _safe_edit(callback.message, "Шаг 2/4 — выберите марку:",
                         reply_markup=_kb_makes(view, page))
    elif current == CarFinder.choosing_lot.state:
        models = data.get("models") or []
        page = data.get("model_page", 0)
        await state.set_state(CarFinder.choosing_model)
        await _safe_edit(
            callback.message,
            f"Шаг 3/4 — модель {(data.get('make') or {}).get('name', '')}:",
            reply_markup=_kb_models(models, page))
    else:
        await state.set_state(CarFinder.choosing_source)
        await _safe_edit(callback.message,
                         "Шаг 1/4 — выберите аукцион:",
                         reply_markup=_kb_source())


@car_finder_router.callback_query(F.data == "cf|noop")
async def cf_noop(callback: types.CallbackQuery):
    await callback.answer()


# ---------------------------------------------------------------------------
# Fallback: текст внутри диалога — пробуем распознать как ссылку на лот
# ---------------------------------------------------------------------------

@car_finder_router.message(StateFilter(CarFinder), F.text)
async def cf_fallback_text(message: types.Message, state: FSMContext):
    lot_id, parser_type = _deps["extract_lot_id"](message.text.strip())
    if lot_id:
        await state.clear()
        try:
            await _deps["send_lot_card"](message, lot_id, parser_type)
        except Exception as e:
            logger.error(f"car_finder: send_lot_card failed: {e}", exc_info=True)
            await message.answer("Произошла ошибка. Попробуйте снова.")
        return
    await message.answer(
        "Используйте кнопки меню для навигации, либо нажмите "
        f"«{MANUAL_BUTTON_TEXT}» и отправьте прямую ссылку на лот.")
