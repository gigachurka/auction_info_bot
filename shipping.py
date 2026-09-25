"""Расчёт стоимости доставки авто из США в Европу (Клайпеда / Поти).

Три источника данных (см. скриншоты и Excel заказчика):

1. Желтая таблица (YELLOW_TABLE) — комбинированный прайс перевозчика:
   для площадки аукциона сразу даёт наземную часть (авто -> порт США)
   и морскую часть (порт США -> Клайпеда/Поти, отдельно седан и SUV).

2. Excel (data/usa_transportation_rate.xlsx) — точные тарифы аукцион -> порт
   по конкретным локациям Copart/IAAI (колонки Car / Motorcycle / Pickup / SUV
   и порт назначения в колонке To).

3. Морские PDF (SEA_FREIGHT) — тарифы порт США -> Клайпеда/Поти по типам авто.

Алгоритм (двухуровневый приоритет):

Уровень 1 (приоритет, Желтая таблица):
    Активен, если match_yellow_key нашёл совпадение И тип авто "Car" или "SUV".
    Наземная часть = "перевозка авто в порт" (одна для седана и SUV).
    Морская часть  = колонка Клайпеда/Поти, седан (для Car) или SUV (для SUV).

Уровень 2 (резерв, Excel + морские PDF):
    Активен, если совпадения в Желтой нет ИЛИ тип авто "Pickup"/"Motorcycle".
    1. Excel: точное совпадение строки с сайта -> порт США + наземная стоимость.
    2. Морской PDF: строка порта США -> морская стоимость.
    3. Маппинг типов для моря:
        Car            -> "1 Regular Car"
        SUV / Pickup   -> "1 Large Car"
        Motorcycle     -> "Motorcycle"
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# --- Морские профили Желтой таблицы --------------------------------------
# Значения (klaipeda_sedan, klaipeda_suv, poti_sedan, poti_suv) зависят
# только от порта отгрузки. Каждой площадке присвоен один из профилей.
YELLOW_SEA_PROFILES = {
    "HOUSTON":  (835, 835, 1180, 1180),
    "CHICAGO":  (875, 975, 1200, 1200),
    "SAV":      (695, 695, 850, 850),   # Savannah / Miami
    "NY":       (725, 725, 895, 895),   # New York / Norfolk
    "LA":       (1250, 1250, 1595, 1595),  # Los Angeles / Honolulu
    "SEATTLE":  (1725, 1725, 1600, 1600),  # Seattle / Anchorage
    "TORONTO":  (850, 950, 1150, 1300),
}

# --- Желтая таблица: ключ -> (наземная стоимость авто в порт, профиль моря) 
# Ключ сравнивается как подстрока со строкой локации с сайта (в нижнем регистре).
YELLOW_TABLE = {
    "abilene": (400, "HOUSTON"),
    "ace - carson": (200, "LA"),
    "ace - perris": (325, "LA"),
    "adamsburg": (450, "NY"),
    "adelanto": (325, "LA"),
    "adesa boston": (325, "NY"),
    "adesa great lakes": (575, "NY"),
    "adesa new jersey": (200, "NY"),
    "adesa pa": (300, "NY"),
    "adesa sioux falls": (600, "CHICAGO"),
    "adesa st. john": (1950, "TORONTO"),
    "adesa wisconsin": (300, "CHICAGO"),
    "akron-canton": (600, "NY"),
    "albany": (325, "NY"),
    "albuquerque": (650, "HOUSTON"),
    "altoona": (500, "NY"),
    "amarillo": (575, "HOUSTON"),
    "anaheim": (230, "LA"),
    "anchorage": (1950, "SEATTLE"),
    "andrews": (475, "HOUSTON"),
    "antelope": (425, "LA"),
    "appleton": (375, "CHICAGO"),
    "arizona auto auction": (400, "LA"),
    "asheville": (425, "SAV"),
    "ashland": (550, "CHICAGO"),
    "atlanta auto auction": (325, "SAV"),
    "atlanta east": (325, "SAV"),
    "atlanta north": (325, "SAV"),
    "atlanta south": (325, "SAV"),
    "atlanta west": (325, "SAV"),
    "augusta": (300, "SAV"),
    "austin": (300, "HOUSTON"),
    "avenel new jersey": (200, "NY"),
    "bakersfield": (350, "LA"),
    "baltimore": (325, "NY"),
    "bangor": (425, "NY"),
    "baton rouge": (350, "HOUSTON"),
    "bay area": (350, "LA"),
    "bel-air auto auction": (450, "NY"),
    "billings": (850, "SEATTLE"),
    "birmingham": (400, "SAV"),
    "boise": (500, "SEATTLE"),
    "boston - shirley": (425, "NY"),
    "boston-shirley": (425, "NY"),
    "bowling green": (475, "CHICAGO"),
    "bridgeport": (270, "NY"),
    "bridgeview": (200, "CHICAGO"),
    "buckhannon": (625, "NY"),
    "buffalo": (550, "NY"),
    "burlington": (525, "NY"),
    "calgary": (1450, "TORONTO"),
    "candia": (425, "NY"),
    "cartersville": (350, "SAV"),
    "casper": (1250, "SEATTLE"),
    "cedar rapids": (425, "CHICAGO"),
    "central auto auction": (250, "NY"),
    "central new jersey": (225, "NY"),
    "chambersburg": (400, "NY"),
    "charleston - sc": (325, "SAV"),
    "charleston - wv": (625, "NY"),
    "charlotte": (325, "SAV"),
    "chattanooga": (400, "SAV"),
    "chicago north": (200, "CHICAGO"),
    "chicago south": (230, "CHICAGO"),
    "chicago west": (200, "CHICAGO"),
    "china grove": (325, "SAV"),
    "cicero": (425, "CHICAGO"),
    "cincinnati": (600, "NY"),
    "clayton": (325, "SAV"),
    "clearwater": (325, "SAV"),
    "cleveland east": (550, "NY"),
    "cleveland west": (550, "NY"),
    "cleveland": (600, "NY"),
    "clewiston": (350, "SAV"),
    "clinton": (480, "SAV"),
    "colorado springs": (700, "LA"),
    "columbia mo": (420, "CHICAGO"),
    "columbia sc": (325, "SAV"),
    "columbus al": (450, "SAV"),
    "columbus oh": (600, "NY"),
    "concord": (325, "SAV"),
    "cookstown": (700, "TORONTO"),
    "corpus christi": (325, "HOUSTON"),
    "culpeper,va": (400, "NY"),
    "culpeper": (400, "NY"),
    "dallas south": (325, "HOUSTON"),
    "dallas": (325, "HOUSTON"),
    "danville": (425, "SAV"),
    "davenport": (375, "CHICAGO"),
    "dayton": (600, "NY"),
    "defuniak springs": (350, "SAV"),
    "denver south": (700, "HOUSTON"),
    "denver": (700, "HOUSTON"),
    "des moines": (425, "CHICAGO"),
    "detroit": (425, "CHICAGO"),
    "dothan": (400, "SAV"),
    "dundalk": (325, "NY"),
    "dyer": (200, "CHICAGO"),
    "earlington": (585, "SAV"),
    "east bay": (450, "LA"),
    "east nc": (450, "SAV"),
    "edmonton": (1450, "TORONTO"),
    "el paso": (475, "HOUSTON"),
    "eldridge": (375, "CHICAGO"),
    "elkton": (325, "NY"),
    "englishtown": (225, "NY"),
    "erie": (550, "NY"),
    "essex": (365, "NY"),
    "eugene": (425, "SEATTLE"),
    "exeter": (400, "NY"),
    "fairburn": (325, "SAV"),
    "fargo": (650, "CHICAGO"),
    "fayetteville": (525, "HOUSTON"),
    "flint": (475, "CHICAGO"),
    "florence": (300, "SAV"),
    "fontana": (230, "LA"),
    "fort myers": (325, "SAV"),
    "fort wayne": (350, "CHICAGO"),
    "fort worth north": (325, "HOUSTON"),
    "four oaks, nc": (250, "SAV"),
    "four oaks": (250, "SAV"),
    "fredericksburg-south": (375, "NY"),
    "fredericksburg": (375, "NY"),
    "freetown": (425, "NY"),
    "fremont": (450, "LA"),
    "fresno": (425, "LA"),
    "ft. pierce": (400, "SAV"),
    "ft. worth": (325, "HOUSTON"),
    "ft.lauderdale": (280, "SAV"),
    "gastonia": (350, "SAV"),
    "glassboro east": (250, "NY"),
    "glassboro west": (250, "NY"),
    "golden gate": (375, "LA"),
    "gr.rapids": (425, "CHICAGO"),
    "graham": (175, "SEATTLE"),
    "grand island": (750, "NY"),
    "grantville": (300, "NY"),
    "greater auto auction phoenix": (400, "LA"),
    "greensboro": (325, "SAV"),
    "greenville": (300, "SAV"),
    "greer": (325, "SAV"),
    "grenada": (475, "SAV"),
    "gulf coast": (375, "SAV"),
    "gulfport": (400, "SAV"),
    "halifax": (1200, "TORONTO"),
    "hamilton": (450, "TORONTO"),
    "hammond": (200, "CHICAGO"),
    "hampton, va": (400, "NY"),
    "hampton": (400, "TORONTO"),
    "harrisburg": (325, "NY"),
    "hartford city": (250, "CHICAGO"),
    "hartford-south": (275, "NY"),
    "hartford": (275, "NY"),
    "hatward": (1040, "CHICAGO"),
    "hayward": (450, "LA"),
    "helena": (750, "SEATTLE"),
    "high desert": (350, "LA"),
    "high point": (325, "SAV"),
    "honolulu": (1650, "LA"),
    "houston-north": (250, "HOUSTON"),
    "houston": (250, "HOUSTON"),
    "huntsville": (450, "SAV"),
    "indianapolis": (300, "CHICAGO"),
    "ionia": (425, "CHICAGO"),
    "jackson": (425, "SAV"),
    "jacksonville east": (275, "SAV"),
    "jacksonville west": (275, "SAV"),
    "jacksonville": (275, "SAV"),
    "kansas city": (600, "SAV"),
    "kincheloe": (800, "CHICAGO"),
    "knoxville": (450, "SAV"),
    "lafayette": (325, "HOUSTON"),
    "lake city": (325, "SAV"),
    "lansing": (425, "CHICAGO"),
    "las vegas": (350, "LA"),
    "laurel": (325, "NY"),
    "lexington east ky": (475, "CHICAGO"),
    "lexington sc": (350, "SAV"),
    "lexington west ky": (475, "CHICAGO"),
    "lincoln, il": (350, "CHICAGO"),
    "lincoln il": (350, "CHICAGO"),
    "lincoln, ne": (500, "CHICAGO"),
    "lincoln ne": (500, "CHICAGO"),
    "little rock": (500, "HOUSTON"),
    "london": (550, "TORONTO"),
    "long beach": (180, "LA"),
    "long island": (300, "NY"),
    "longview": (325, "HOUSTON"),
    "los angeles - adesa": (200, "LA"),
    "los angeles": (200, "LA"),
    "louisville": (475, "CHICAGO"),
    "lubbock": (500, "HOUSTON"),
    "lufkin": (325, "HOUSTON"),
    "lumberton": (325, "SAV"),
    "lyman": (475, "NY"),
    "macon": (300, "SAV"),
    "madison heights": (420, "NY"),
    "madison": (350, "CHICAGO"),
    "manchester": (400, "NY"),
    "manheim albany": (325, "NY"),
    "manheim arena illinois": (200, "CHICAGO"),
    "manheim auto auction": (280, "NY"),
    "manheim baltimore-washington": (300, "NY"),
    "manheim baltimore": (300, "NY"),
    "manheim bishop brothers": (240, "SAV"),
    "manheim california": (220, "LA"),
    "manheim carleton": (525, "NY"),
    "manheim central california": (300, "LA"),
    "manheim central florida": (275, "SAV"),
    "manheim chicago": (200, "CHICAGO"),
    "manheim cincinnati": (600, "NY"),
    "manheim colorado": (500, "CHICAGO"),
    "manheim dallas-ft worth": (325, "HOUSTON"),
    "manheim dallas": (325, "HOUSTON"),
    "manheim darlington": (275, "SAV"),
    "manheim daytona beach": (325, "SAV"),
    "manheim denver": (700, "HOUSTON"),
    "manheim detroit": (650, "NY"),
    "manheim fort lauderdale": (280, "SAV"),
    "manheim fort myers": (300, "SAV"),
    "manheim fort wayne": (350, "CHICAGO"),
    "manheim fredericksburg": (375, "NY"),
    "manheim georgia": (300, "SAV"),
    "manheim harrisonburg": (400, "NY"),
    "manheim imperial auto auction": (350, "SAV"),
    "manheim kentucky": (600, "NY"),
    "manheim lafayette": (325, "HOUSTON"),
    "manheim lakeland": (350, "SAV"),
    "manheim metro milwaukee": (275, "CHICAGO"),
    "manheim milwaukee": (275, "CHICAGO"),
    "manheim mississippi": (375, "SAV"),
    "manheim missouri": (425, "CHICAGO"),
    "manheim montreal": (500, "TORONTO"),
    "manheim nashville": (400, "SAV"),
    "manheim nevada": (325, "LA"),
    "manheim new england": (400, "NY"),
    "manheim new jersey": (200, "NY"),
    "manheim new mexico": (575, "HOUSTON"),
    "manheim new orleans": (475, "SAV"),
    "manheim new york": (300, "NY"),
    "manheim north carolina": (325, "SAV"),
    "manheim northstar minnesota": (425, "CHICAGO"),
    "manheim ohio": (450, "NY"),
    "manheim oklahoma city": (400, "HOUSTON"),
    "manheim orlando": (300, "SAV"),
    "manheim oshawa": (175, "TORONTO"),
    "manheim palm beach": (300, "SAV"),
    "manheim pennsylvania": (250, "NY"),
    "manheim pensacola": (375, "SAV"),
    "manheim philadelphia": (270, "NY"),
    "manheim phoenix": (400, "LA"),
    "manheim pittsburg": (450, "NY"),
    "manheim riverside": (225, "LA"),
    "manheim san antonio": (350, "HOUSTON"),
    "manheim san diego": (215, "LA"),
    "manheim san francisco bay": (350, "LA"),
    "manheim seattle": (225, "SEATTLE"),
    "manheim skyline auto auction": (150, "NY"),
    "manheim southern california": (225, "LA"),
    "manheim st louis": (525, "SAV"),
    "manheim st. pete": (300, "SAV"),
    "manheim statesville": (300, "SAV"),
    "manheim tampa": (290, "SAV"),
    "manheim tennessee": (500, "SAV"),
    "manheim texas hobby": (150, "HOUSTON"),
    "manheim toronto": (425, "TORONTO"),
    "manheim tucson": (450, "LA"),
    "manheim utah": (425, "LA"),
    "manheim virginia (fredericksburg)": (375, "NY"),
    "martinez": (450, "LA"),
    "mcallen": (400, "HOUSTON"),
    "mebane": (325, "SAV"),
    "memphis": (500, "SAV"),
    "mentone": (250, "LA"),
    "metro dc": (325, "NY"),
    "miami central": (400, "SAV"),
    "miami north": (400, "SAV"),
    "miami south": (400, "SAV"),
    "miami": (400, "SAV"),
    "middletown": (325, "NY"),
    "milwaukee": (275, "CHICAGO"),
    "minneapolis /st. paul": (450, "CHICAGO"),
    "minneapolis /st.paul": (450, "CHICAGO"),
    "minneapolis north": (450, "CHICAGO"),
    "minneapolis": (450, "CHICAGO"),
    "missoula": (650, "SEATTLE"),
    "mobile": (450, "SAV"),
    "mocksville": (325, "SAV"),
    "moncton": (1275, "TORONTO"),
    "montgomery": (450, "SAV"),
    "monticello": (275, "NY"),
    "montreal": (500, "TORONTO"),
    "napa": (450, "LA"),
    "nashville": (450, "SAV"),
    "national auto dealers exchange": (170, "NY"),
    "new castle": (300, "NY"),
    "new orleans": (475, "SAV"),
    "newburgh": (275, "NY"),
    "north boston": (425, "NY"),
    "north charleston - sc": (325, "SAV"),
    "north charleston": (325, "SAV"),
    "north hollywood": (230, "LA"),
    "north seattle": (225, "SEATTLE"),
    "northern new jersey": (210, "NY"),
    "northern virginia": (325, "NY"),
    "ocala": (325, "SAV"),
    "ogden": (600, "LA"),
    "oklahoma city": (450, "HOUSTON"),
    "omaha": (500, "CHICAGO"),
    "orlando north": (300, "SAV"),
    "orlando south": (300, "SAV"),
    "orlando": (300, "SAV"),
    "ottawa": (475, "TORONTO"),
    "paducah": (525, "CHICAGO"),
    "pasco": (400, "SEATTLE"),
    "pensacola": (425, "SAV"),
    "peoria": (350, "CHICAGO"),
    "permian basin": (500, "HOUSTON"),
    "philadelphia east": (270, "NY"),
    "philadelphia": (270, "NY"),
    "phoenix": (400, "LA"),
    "pittsburgh south": (500, "NY"),
    "pittsburg": (450, "NY"),
    "port murray": (225, "NY"),
    "portage": (350, "CHICAGO"),
    "portland - gorham": (475, "NY"),
    "portland north": (300, "SEATTLE"),
    "portland south": (300, "SEATTLE"),
    "portland west": (300, "SEATTLE"),
    "portland": (275, "SEATTLE"),
    "providence": (400, "NY"),
    "pulaski": (425, "NY"),
    "punta gorda": (350, "SAV"),
    "puyallup": (225, "SEATTLE"),
    "quebec city": (625, "TORONTO"),
    "quebec": (600, "TORONTO"),
    "raleigh": (325, "SAV"),
    "rancho cucamonga": (230, "LA"),
    "rapid city": (1100, "CHICAGO"),
    "redding": (700, "LA"),
    "regina": (1350, "TORONTO"),
    "reno": (600, "LA"),
    "richmond": (400, "NY"),
    "riverside": (230, "LA"),
    "roanoke": (475, "NY"),
    "rochester": (500, "NY"),
    "rosedale": (375, "NY"),
    "rutland": (500, "NY"),
    "sacramento": (450, "LA"),
    "salisbury": (350, "NY"),
    "salt lake city": (700, "LA"),
    "san antonio": (350, "HOUSTON"),
    "san bernardino": (250, "LA"),
    "san diego": (250, "LA"),
    "san jose": (450, "LA"),
    "sarasota": (300, "SAV"),
    "savannah": (150, "SAV"),
    "sayreville": (200, "NY"),
    "scranton": (300, "NY"),
    "seaford": (375, "NY"),
    "seattle": (225, "SEATTLE"),
    "shady spring, wv": (625, "NY"),
    "shady spring": (625, "NY"),
    "shreveport": (365, "HOUSTON"),
    "sikeston": (475, "CHICAGO"),
    "sioux falls": (600, "CHICAGO"),
    "so sacramento": (450, "LA"),
    "somerville": (210, "NY"),
    "south bend": (300, "CHICAGO"),
    "south boston": (425, "NY"),
    "southern illinois": (575, "SAV"),
    "southern new jersey": (250, "NY"),
    "spanaway": (225, "SEATTLE"),
    "spartanburg": (325, "SAV"),
    "spokane": (400, "SEATTLE"),
    "springfield": (600, "SAV"),
    "st. cloud": (450, "CHICAGO"),
    "st. john's": (1950, "TORONTO"),
    "st. louis, il": (575, "SAV"),
    "st. louis il": (575, "SAV"),
    "st. louis, mo": (575, "SAV"),
    "st. louis mo": (575, "SAV"),
    "stockton": (450, "LA"),
    "sudbury": (475, "TORONTO"),
    "suffolk": (400, "NY"),
    "sun valley": (230, "LA"),
    "syracuse": (375, "NY"),
    "tallahassee": (325, "SAV"),
    "tampa south": (325, "SAV"),
    "tampa": (325, "SAV"),
    "tanner": (475, "SAV"),
    "taunton": (425, "NY"),
    "templeton": (425, "NY"),
    "tidewater": (350, "NY"),
    "tifton": (300, "SAV"),
    "toronto": (425, "TORONTO"),
    "total resource auc centrl penn": (300, "NY"),
    "trenton": (250, "NY"),
    "tucson": (450, "LA"),
    "tulsa": (525, "HOUSTON"),
    "vallejo": (450, "LA"),
    "van nuys": (230, "LA"),
    "vancouver": (1950, "TORONTO"),
    "waco": (375, "HOUSTON"),
    "walton": (475, "CHICAGO"),
    "washingtondc": (325, "NY"),
    "washington dc": (325, "NY"),
    "wayland": (425, "CHICAGO"),
    "webster": (450, "NY"),
    "west palm beach": (400, "SAV"),
    "west warren": (425, "NY"),
    "western colorado": (900, "LA"),
    "wheeling": (200, "CHICAGO"),
    "wichita": (600, "HOUSTON"),
    "wilmington": (400, "SAV"),
    "windham": (500, "NY"),
    "winnipeg": (1300, "TORONTO"),
    "york haven": (325, "NY"),
    "york springs": (325, "NY"),
}

# Ключи, отсортированные от самых длинных к коротким — чтобы при подстрочном
# совпадении предпочесть наиболее конкретное название площадки.
_YELLOW_KEYS_SORTED = sorted(YELLOW_TABLE.keys(), key=len, reverse=True)

# --- Морские тарифы (порт США -> Клайпеда / Поти), из PDF KLAIPEDA/POTI ----
# Значения: regular (1 Regular Car), large (1 Large Car), moto (Motorcycle).
SEA_FREIGHT = {
    "klaipeda": {
        "CHICAGO":          {"regular": 850,  "large": 950,  "moto": 350},
        "HOUSTON":          {"regular": 750,  "large": 850,  "moto": 325},
        "LOS ANGELES":      {"regular": 1200, "large": 1450, "moto": 500},
        "MIAMI":            {"regular": 750,  "large": 850,  "moto": 300},
        "NEWARK":           {"regular": 700,  "large": 800,  "moto": 300},
        "NORFOLK":          {"regular": 675,  "large": 775,  "moto": 300},
        "PORT OF HONOLULU": {"regular": 2250, "large": 2450, "moto": 1125},
        "SAVANNAH":         {"regular": 675,  "large": 775,  "moto": 300},
        "SEATTLE":          {"regular": 1625, "large": 1800, "moto": 725},
        "TORONTO":          {"regular": 850,  "large": 950,  "moto": 325},
    },
    "poti": {
        "CHICAGO":          {"regular": 950,  "large": 1125, "moto": 400},
        "HOUSTON":          {"regular": 975,  "large": 1125, "moto": 400},
        "LOS ANGELES":      {"regular": 1475, "large": 1650, "moto": 575},
        "MIAMI":            {"regular": 825,  "large": 975,  "moto": 375},
        "NEWARK":           {"regular": 800,  "large": 950,  "moto": 375},
        "NORFOLK":          {"regular": 725,  "large": 850,  "moto": 350},
        "PORT OF HONOLULU": {"regular": 2200, "large": 2500, "moto": 1200},
        "SAVANNAH":         {"regular": 700,  "large": 850,  "moto": 350},
        "SEATTLE":          {"regular": 1350, "large": 1550, "moto": 625},
        "TORONTO":          {"regular": 1150, "large": 1300, "moto": 500},
    },
}

# Соответствие названий портов из Excel (колонка To) названиям в морских PDF.
PORT_NAME_MAP = {
    "chicago": "CHICAGO",
    "houston": "HOUSTON",
    "los angeles": "LOS ANGELES",
    "miami": "MIAMI",
    "new york": "NEWARK",
    "newark": "NEWARK",
    "norfolk": "NORFOLK",
    "honolulu": "PORT OF HONOLULU",
    "port of honolulu": "PORT OF HONOLULU",
    "savannah": "SAVANNAH",
    "seattle": "SEATTLE",
    "toronto": "TORONTO",
}

_EXCEL_PATH = os.path.join(os.path.dirname(__file__), "data", "usa_transportation_rate.xlsx")

# Excel-таблица аукцион -> порт. Загружается лениво при первом обращении.
# Структура: { normalized_from: {"raw": str, "to": str,
#              "car": int|None, "moto": int|None, "pickup": int|None, "suv": int|None} }
_excel_table = None
# Индекс по названию города (часть строки From до первого " - "):
#   { normalized_city: entry }  + отсортированный по длине список ключей.
_excel_city_index = None
_excel_city_keys_sorted = None


# --- Нормализация и матчинг -----------------------------------------------

def _normalize(s: str) -> str:
    """Приводит строку к единому виду для сравнения.

    - нижний регистр;
    - тире/дефисы разных видов -> обычный дефис;
    - апострофы/кавычки -> обычный апостроф;
    - схлопывание пробелов.
    """
    if not s:
        return ""
    text = str(s).lower()
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2012", "-")
    text = text.replace("`", "'").replace("\u2019", "'")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_yellow_key(auction_string: str):
    """Ищет ключ Желтой таблицы как подстроку строки локации с сайта.

    Возвращает найденный ключ (str) или None. Предпочитает самый длинный
    (наиболее конкретный) ключ, чтобы «Manheim Toronto» не совпал как «Toronto».
    """
    norm = _normalize(auction_string)
    if not norm:
        return None
    for key in _YELLOW_KEYS_SORTED:
        if key in norm:
            return key
    return None


# --- Загрузка Excel --------------------------------------------------------

def _parse_price(value):
    """Число из ячейки Excel: int, либо None для пустых / 'Call for price'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text or "call" in text.lower():
        return None
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group(0)) if m else None


def _city_from_frm(normalized_frm: str) -> str:
    """Название города из строки Excel From ("dallas south - texas - copart" -> "dallas south")."""
    return normalized_frm.split(" - ", 1)[0].strip()


def _load_excel():
    """Читает Excel аукцион->порт в память (один раз) и строит индекс по городам."""
    global _excel_table, _excel_city_index, _excel_city_keys_sorted
    if _excel_table is not None:
        return _excel_table

    table = {}
    city_index = {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(_EXCEL_PATH, data_only=True)
        ws = wb.worksheets[0]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue  # заголовок: Auction | From | To | Car | Motorcycle | Pickup | SUV
            if not row or len(row) < 7:
                continue
            frm, to = row[1], row[2]
            if not frm or not to:
                continue
            norm_frm = _normalize(frm)
            entry = {
                "raw": str(frm).strip(),
                "to": str(to).strip(),
                "car": _parse_price(row[3]),
                "moto": _parse_price(row[4]),
                "pickup": _parse_price(row[5]),
                "suv": _parse_price(row[6]),
            }
            table[norm_frm] = entry
            # Индекс по городу: первая запись выигрывает (Copart/IAAI обычно совпадают).
            city = _city_from_frm(norm_frm)
            if city and city not in city_index:
                city_index[city] = entry
        logger.info(f"Загружена Excel-таблица тарифов: {len(table)} локаций, {len(city_index)} городов")
    except FileNotFoundError:
        logger.warning(f"Excel с тарифами не найден: {_EXCEL_PATH}")
    except Exception as e:
        logger.warning(f"Не удалось загрузить Excel с тарифами: {e}")

    _excel_table = table
    _excel_city_index = city_index
    _excel_city_keys_sorted = sorted(city_index.keys(), key=len, reverse=True)
    return _excel_table


def _match_excel(auction_string: str):
    """Совпадение локации в Excel.

    1) Точное совпадение по нормализованной строке.
    2) Иначе — по названию города как подстроке (для форматов вида
       "TX - DALLAS SOUTH", отличных от ключа Excel). Предпочитается самый
       длинный город, чтобы "dallas south" победил "dallas".
    """
    table = _load_excel()
    norm = _normalize(auction_string)
    exact = table.get(norm)
    if exact:
        return exact
    if not norm:
        return None
    for city in _excel_city_keys_sorted:
        if city in norm:
            return _excel_city_index[city]
    return None


# --- Определение типа авто -------------------------------------------------

_PICKUP_KEYWORDS = (
    "pickup", "pick up", "pick-up", "f-150", "f150", "f-250", "f250", "f-350",
    "silverado", "sierra", "tundra", "tacoma", "ram 1500", "ram 2500", "ram 3500",
    "ranger", "colorado", "canyon", "frontier", "titan", "ridgeline", "gladiator",
)
_SUV_KEYWORDS = (
    "suv", "crossover", "explorer", "tahoe", "suburban", "escalade", "highlander",
    "4runner", "pathfinder", "rav4", "cr-v", "crv", "cx-5", "rogue", "equinox",
    "traverse", "expedition", "wrangler", "cherokee", "durango", "pilot", "sorento",
    "santa fe", "tucson", "outback", "forester", "telluride", "palisade", "murano",
    "edge", "escape", "bronco", "blazer", "trailblazer", "gx", "lx", "qx", "mdx",
    "rx 350", "x5", "x3", "gle", "glc", "q5", "q7",
)
_MOTO_KEYWORDS = (
    "motorcycle", "harley", "harley-davidson", "scooter", "moped",
    "kawasaki", "ducati", "triumph motorcycle",
)


def guess_vehicle_type(title: str, body_style: str = "") -> str:
    """Грубая оценка типа авто по заголовку / типу кузова.

    Возвращает "Car" (по умолчанию), "SUV", "Pickup" или "Motorcycle".
    """
    text = f"{title or ''} {body_style or ''}".lower()
    if any(k in text for k in _MOTO_KEYWORDS):
        return "Motorcycle"
    if any(k in text for k in _PICKUP_KEYWORDS):
        return "Pickup"
    if any(k in text for k in _SUV_KEYWORDS):
        return "SUV"
    return "Car"


# --- Основной расчёт -------------------------------------------------------

def _yellow_sea_cost(profile: str, destination: str, vehicle_type: str):
    """Морская часть из Желтой таблицы по профилю порта и типу авто."""
    kl_sedan, kl_suv, poti_sedan, poti_suv = YELLOW_SEA_PROFILES[profile]
    is_suv = vehicle_type == "SUV"
    if destination == "poti":
        return poti_suv if is_suv else poti_sedan
    return kl_suv if is_suv else kl_sedan


def select_destination_port(year: int, engine_cc: int) -> str:
    """Выбирает порт назначения по году и объёму двигателя (санкции РБ).

    Правила заказчика:
    - С 2021 года + объём ≥2000 см³ (2.0 л и выше) → Поти (Грузия)
    - До 2020 года (включая 2020), ЛЮБОЙ объём → Клайпеда (Литва)
    - Любой год, объём <2000 см³ → Клайпеда (Литва)
    """
    if year >= 2021 and engine_cc >= 2000:
        return "poti"
    return "klaipeda"


def calculate_shipping(auction_string: str, vehicle_type: str = "Car",
                       destination: str = "klaipeda") -> dict:
    """Считает стоимость доставки авто из США до Клайпеды/Поти.

    Параметры:
        auction_string — строка локации с сайта ("ABILENE - Texas - Copart").
        vehicle_type   — "Car" | "SUV" | "Pickup" | "Motorcycle".
        destination    — "klaipeda" (по умолчанию) или "poti".

    Возвращает словарь:
        {
          "found": bool,           # удалось ли посчитать
          "method": "yellow"|"excel"|None,
          "vehicle_type": str,
          "destination": str,
          "port": str|None,        # порт отгрузки в США (для метода excel)
          "land_usd": int|None,    # аукцион -> порт США
          "sea_usd": int|None,     # порт США -> Клайпеда/Поти
          "total_usd": int|None,   # land + sea
          "note": str,             # пояснение (например, "Call for price")
        }
    """
    destination = (destination or "klaipeda").lower()
    if destination not in ("klaipeda", "poti"):
        destination = "klaipeda"
    vehicle_type = vehicle_type if vehicle_type in ("Car", "SUV", "Pickup", "Motorcycle") else "Car"

    result = {
        "found": False, "method": None, "vehicle_type": vehicle_type,
        "destination": destination, "port": None,
        "land_usd": None, "sea_usd": None, "total_usd": None, "note": "",
    }

    # --- Уровень 1: Желтая таблица (только Car / SUV) ---------------------
    if vehicle_type in ("Car", "SUV"):
        key = match_yellow_key(auction_string)
        if key:
            land, profile = YELLOW_TABLE[key]
            sea = _yellow_sea_cost(profile, destination, vehicle_type)
            result.update({
                "found": True, "method": "yellow", "matched_key": key,
                "land_usd": land, "sea_usd": sea, "total_usd": land + sea,
            })
            return result

    # --- Уровень 2: Excel + морские PDF ----------------------------------
    row = _match_excel(auction_string)
    if not row:
        result["note"] = "Локация не найдена ни в Желтой таблице, ни в Excel"
        return result

    # Наземная стоимость по типу авто (SUV/Pickup часто пустые -> берём Car).
    type_key = {"Car": "car", "SUV": "suv", "Pickup": "pickup", "Motorcycle": "moto"}[vehicle_type]
    land = row.get(type_key)
    if land is None:
        land = row.get("car")

    port = PORT_NAME_MAP.get(_normalize(row["to"]))
    result["port"] = row["to"]

    if land is None:
        result["note"] = f"Тариф аукцион->порт уточняется (Call for price), порт {row['to']}"
        result["found"] = False
        return result

    if not port or port not in SEA_FREIGHT[destination]:
        # Нет морского тарифа для этого порта (напр. Anchorage) — только наземная часть.
        result.update({"method": "excel", "land_usd": land, "found": False,
                        "note": f"Нет морского тарифа для порта {row['to']}"})
        return result

    sea_col = {"Car": "regular", "SUV": "large", "Pickup": "large", "Motorcycle": "moto"}[vehicle_type]
    sea = SEA_FREIGHT[destination][port][sea_col]

    result.update({
        "found": True, "method": "excel",
        "land_usd": land, "sea_usd": sea, "total_usd": land + sea,
    })
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tests = [
        ("ABILENE - Texas - Copart", "Car", "klaipeda"),
        ("ABILENE - Texas - Copart", "SUV", "klaipeda"),
        ("ABILENE - Texas - Copart", "Pickup", "klaipeda"),
        ("ADESA Boston - Massachusetts - Copart", "Car", "klaipeda"),
        ("ACE - Carson - California - IAAI", "Car", "poti"),
        ("ALBUQUERQUE - New Mexico - Copart", "Car", "klaipeda"),
        ("HOUSTON-NORTH - Texas - IAAI", "Car", "klaipeda"),
        ("Manheim Toronto - Ontario - Copart", "SUV", "klaipeda"),
        ("ANCHORAGE - Alaska - Copart", "Car", "klaipeda"),
        ("SOME UNKNOWN PLACE - Nowhere", "Car", "klaipeda"),
    ]
    for loc, vt, dest in tests:
        r = calculate_shipping(loc, vt, dest)
        print(f"\n{loc} | {vt} | {dest}")
        print(f"  method={r['method']} land={r['land_usd']} sea={r['sea_usd']} "
              f"total={r['total_usd']} note={r['note']}")
