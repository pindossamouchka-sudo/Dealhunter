# -*- coding: utf-8 -*-
"""DealHunter TG-скан: читает публичные б/у-каналы через официальный MTProto API (Telethon).

Команды:
  python tg_scan.py --login      первый вход (спросит номер и код из Telegram)
  python tg_scan.py --list       показать каналы, в которых состоит аккаунт
  python tg_scan.py --scan       инкрементальный скан каналов из tg_channels.txt
  python tg_scan.py --scan --full  без инкремента (вытащить последние 100 постов)

Ключи берёт из .env: TG_API_ID, TG_API_HASH. Никогда не коммитятся.
"""
import os
import re
import sys
import json
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")
WORKER_SECRET = os.getenv("WORKER_SECRET", "")
SESSION_FILE = "tg_session"  # -> tg_session.session (не коммитится)
CHANNELS_FILE = BASE / "tg_channels.txt"
STATE_FILE = BASE / "tg_state.json"
LOTS_FILE = BASE / "tg_lots.json"

SCAM_WORDS = [
    "быстрый перевод", "залог", "заложу", "крипт", "сатош", "bitcoin", "btc",
    "быстро?", "срочно нужно день", "скиньте", "ради бога", "молю",
    "лутануть", "полкатора", "займ под",

]

# посты-спрос («куплю», «ищу», «сколько стоит?») — НЕ лоты
DEMAND_WORDS = [
    "куплю", "покуплю", "скуп", "ищу", "нужен ", "нужна ", "нужно ", "подскажите",
    "посоветуйте", "кто может", "кто продаст", "сколько стоит", "актуально?",
    "порекомендуйте", "ищется", "ищеться", "предложите", "отдам даром", "раздаю",
]

# «болтовня/объявления» вместо лотов: приветствия, новости, правила чата
NOISE_TITLE_WORDS = [
    "всем привет", "всем внимание", "дорогие друзья", "друзья!", "внимание",
    "объявление", "правила", "администрация", "напоминаю", "обращаюсь",
    "по наличию", "спешите", "планируется", "анонс", "опрос", "поздравля",
    "доброе утро", "добрый день", "добрый вечер", "встаем", "встаём",
    "собираемся", "а вы где", "подписывайся", " online", "разбор полётов",
    "чат знакомств", "болтаем",
]
# служебные аккуратные начала («Цена: 1», «Размер 80») — блокируем,
# только если после служебного слова нет товара
SERVICE_TITLE_WORDS = ["цена:", "размер ", "цвет "]

def _is_noise_title(title: str) -> bool:
    tl = (title or "").lower()
    # «всем привет!», «ДОРОГИЕ ДРУЗЬЯ!», «По наличию в России» и т.п.
    if any(w in tl for w in NOISE_TITLE_WORDS):
        return True
    # кончается на «!» и почти без слов — телеграфный заголовок-приветствие
    if tl.rstrip("!").strip().count(" ") <= 2 and tl.rstrip("!").count("!") >= 1 and len(tl) < 22:
        return True
    return False


def _is_service_title(title: str) -> bool:
    tl = (title or "").lower()
    return any(w in tl for w in SERVICE_TITLE_WORDS) and not PRODUCT_PAT.search(tl)

# пост должен ПОЗВОНИТЬ, что sells: продаю / отдаю / продам / цена ... / есть ...
SALE_WORDS = [
    "продам", "продаю", "продаётся", "продается", "продажа", "продаж",
    "отдам", "отдаю", "отдаётся", "отдается", "содам", "могу отдать",
    "есть в наличии", "в наличии", "забрать", "заберёшь", "заберите",
    "термин: ", "торг", "цена:", "цена ", "цена -", "по цене",
    "свежий айфон", "новый, гаранти",
]

# работа/услуги/подработка — НЕ товарные лоты
NON_ITEM_WORDS = [
    "подработк", "работа", "ваканс", "зарплат", "требуются", "требуетс",
    "услуг", "отзыв", "тестиров", "сервис", "доставка груз", "написан",
    "решен", "диплом", "курсов", "реферат", "оценк", "занятост", "смена",
    "сотрудник", "резюме", "фриланс", "заставка", "стажировк",
    "нужны люди", "ищем людей", "набираем", "на полную ставку", "оплата",
    "опыт", "установ", "настро", "автоматизац", "виндовс", "windows",
    "регион", "склад", "видеонаблюд",
    # подработка-объявления («5000р на руки», «займёт 2 часа»)
    "на руки", "займёт", "займет", "в час", "в день", "дневная",
    # «заработок/задания/схемы/реклама» и прочий не-товар
    "заплач", "заплат", "раздайте", "регистрац", "фидбек", "задани",
    "казик", "выигрыш", "бонуск", "схема", "научу", "показу как",
    "за простое", "деталью", "детали", "приложени", "референ",
    "подписк", "лайкн", "оценку за", "проголос", "ставк", "букмек",
]

def _glitch_ratio(text: str) -> float:
    """доля «космических» букв (стиль спам-обфускации). >15% — спам."""
    weird = 0
    letters = 0
    for ch in text:
        o = ord(ch)
        is_normal_letter = (
            0x0400 <= o <= 0x04FF or  # кириллица
            0x0041 <= o <= 0x005A or 0x0061 <= o <= 0x007A or  # латиница
            0x0030 <= o <= 0x0039  # цифры
        )
        if ch.isalpha():
            letters += 1
            if not is_normal_letter:
                weird += 1
    if letters == 0:
        return 0.0
    return weird / letters


# ---- название и «каталог продавца» ----

PRODUCT_PAT = re.compile(
    r"(iphone|айфон|samsung|galaxy|macbook|playstation|ps\d|airpods|наушник|"
    r"apple\s*watch|смарт[-\s]?часы|xiaomi|redmi|poco|honor|dyson|ноутбук|tablet|"
    r"ipad|планшет|монитор|видеокарта|гпу|geforce|radeon|компьютер|системник|"
    r"мотоцикл|мото\b|квадроцикл|скутер|самокат|телевизор|камера|фотоаппарат|"
    r"джинс|куртк|кроссовк|ботинк|сапоги|сапог|худи|футболк|брюк|свитер|"
    r"плать|рубашк|пальто|обув|сумк|рюкз|теплый|кофт|шапк|штан|велосипед|"
    r"bmw|mercedes|toyota|nissan|honda|audi|volkswagen|kia|hyundai|vaz|lada|"
    r"mazda|isuzu|mitsubishi|suzuki|subaru|uaz|gaz|ураль| ford|форд|шевроле|"
    r"мотовездеход|вездеход|уаз|"
    r"мазда|исузу|митсубиси|субару|сузуки|ниссан|мерседес|тойота|крайслер|пежо|рено|опель|"    r"аквари|плед|овчар|пошив|"
    r"диван|кресл|кроват|матрас|комод|шкаф|стол\b|стуль|мебель|"
    r"холодильник|стиральн|посудомоеч|микроволновк|кофемашин|мультиварк|пылесос|"
    r"кондиционер|инструмент|шуруповерт|дрел|болгарк|гайковер|сварочн|газетон|"
    r"коляск|автокресл|манеж|велокресл|"
    r"тренажер|эллипт|гантел|штанг|беговая дорож|"
    r"сноуборд|коньк|лыж|санк|удочк|спиннинг|гитар|синтезатор|аккордеон|"
    r"ковер|зеркал|люстр|светильник|принтер|роутер|приставк|триммер|газонокос|"
    r"телефон\b|аккумулятор|баллон|домкрат|генератор|компрессор|печ\b|печк|"
    r"котел|котёл|редуктор|маслостан|тачка\b|тачк|катер|лодк|мотор\b|прицеп)",
    re.IGNORECASE,
)

# строки-«описания», которые не годятся для названия (реклама, условия)
GENERIC_TITLE_WORDS = [
    "весь товар", "оригинал", "гаранти", "в наличии", "по наличию", "без торга",
    "доставка", "отправка", "самовывоз", "идеальн", "состояни", "все работает",
    "в комплект", "комплект", "всё работает", "оригинальный", "проверяйте",
    "не гарантирую", "осмотр", "проверка",
]

# болтовня в строке — такая строка точно НЕ название товара
CHATTER_WORDS = [
    "утра", "вечера", "встаем", "встаём", "спим", "собираемся",
    "а вы где", "вы где", "доброе утро", "добрый день", "добрый вечер",
    "подписыв", "жмем", "жмём", "обсужда", "болтае", "знакомств",
    "присоединя", "идем", "идём", "жду вас", "ждем вас",
]

SALE_PREFIX_RE = re.compile(
    r"^(?:прода[мюяё]|продает[сся]|продается|продаж|отда[мюяё]|реализ[у]?|"
    r"обмен(?:\w{0,3})?|срочн|уступ[а-я]{0,3}|продам)\s*", re.IGNORECASE,
)


def _is_catalog_post(text: str) -> bool:
    """пост-«каталог» магазина (много разных товаров) — не частный лот"""
    low = text.lower()
    cats = set(m.group(0).lower() for m in PRODUCT_PAT.finditer(low))
    marks = len(cats)
    iphones = set(re.findall(r"(?:iphone|айфон)\s*(\d{1,2}|1[0-8]g?|x|se)", low, re.I))
    if marks >= 3 or len(iphones) >= 3:
        return True
    # «и др.» / «много ...» / перечисления через запятую с ценами
    if low.count(",") >= 6 and marks >= 2:
        return True
    return False


# ---- распознавание товара: бренд + модель + характеристики + состояние ----
# бу-рынок = ТОЛЬКО техника и электроника (телефон/ноутбук/приставка/аудио/...).

COND_WORDS = [
    (r"как новый", "как новый"),
    (r"идеальн", "идеальное"),
    (r"отличн", "отличное"),
    (r"\bхорош[еои]", "хорошее"),
    (r"не работает|бита\b|дефект", "с дефектом"),
    (r"\bновый\b", "новый"),
]


def _battery(t: str):
    for head in ("батар", "аккум", "акб", "емкость"):
        idx = t.find(head)
        if idx == -1:
            continue
        m = re.search(r"(\d{2,3})\s*%", t[idx: idx + 30])
        if m:
            v = int(m.group(1))
            if 50 <= v <= 100:
                return f"батарея {v}%"
    return None


def _storage(t: str):
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d{3,4})\s*(?:гб|gb)?", t, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        # «8/10» — это состояние, не память
        if 3 <= a <= 32 and b in (64, 128, 256, 512, 1024):
            return f"{a}/{b}GB"
    m = re.search(r"\b(\d{1,2})\s*га\s*/\s*(\d{3,4})\s*(?:гб|gb)", t, re.I)
    if m and int(m.group(1)) <= 32 and int(m.group(2)) in (64, 128, 256, 512, 1024):
        return f"{m.group(1)}/{m.group(2)}GB"
    m = re.search(r"\b(\d{1,4})\s*(гб|gb|тб|tb)\b", t, re.I)
    if m:
        v, u = int(m.group(1)), m.group(2).lower()
        if u in ("тб", "tb") and 1 <= v <= 4:
            return f"{v}TB"
        if u in ("гб", "gb") and 16 <= v <= 1024:
            return f"{v}GB"
    return None


def _phone_storage(t: str):
    """для телефонов пишут просто «256» без «гб» — ловим и это"""
    if re.search(r"\b(iphone|айфон|redmi|редми|poco|поко|xiaomi)", t, re.I):
        m = re.search(r"\b(64|128|256|512)\b(?!\s*\d)(?!\s*(?:руб|₽|rub))", t)
        if m:
            return f"{m.group(1)}GB"
    return None


def _condition(t: str):
    m = re.search(r"состояние\s*[:\- ]?\s*(\d(?:[.,]\d)?)(?!\s*\d|%)(?:\s*/\s*10)?", t)
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            v = 0
        if 1 <= v <= 10:
            s = str(v).rstrip("0").rstrip(".")
            return f"состояние {s}/10"
    m = re.search(r"(\d(?:[.,]\d)?)\s*/\s*10(?!\d)", t)
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            v = 0
        if 1 <= v <= 10:
            s = str(v).rstrip("0").rstrip(".")
            return f"состояние {s}/10"
    for pat, rep in COND_WORDS:
        if re.search(pat, t):
            return rep
    if re.search(r"гаранти\w*", t):
        return "на гарантии"
    return None


def _join(parts):
    return ", ".join(p for p in parts if p)


PHONE_VARIANT = {"pro max": "Pro Max", "pro": "Pro", "max": "Max", "plus": "Plus", "mini": "Mini",
                 "про макс": "Pro Max", "про": "Pro", "макс": "Max", "плюс": "Plus", "мини": "Mini",
                 "про max": "Pro Max", "pro макс": "Pro Max"}
PHONE_BRAND = {"iphone": "iPhone", "айфон": "iPhone",
               "xiaomi": "Xiaomi", "сяоми": "Xiaomi",
               "redmi": "Redmi", "редми": "Redmi",
               "poco": "Poco", "поко": "Poco",
               "honor": "Honor", "хонор": "Honor",
               "huawei": "Huawei", "хуавей": "Huawei"}

PHONE_PAT = re.compile(r"\b(iphone|айфон)\s*(1[0-8]|2\d|[4-9]|x|se|xr)?" + r"(\s*(pro\s*max|про\s*макс|pro|max|макс|plus|плюс|mini|мини))?", re.I)
PHONE_BR_PAT = re.compile(r"\b(xiaomi|redmi|poco|сяоми|редми|поко)\s*((?:note\s?\d{1,2}|\d{1,2})[a-z]?(?:\s?(?:pro|прo|про))?)?", re.I)
HONOR_PAT = re.compile(r"\b(honor|хонор)\s*((?:magic\s?)?\d{1,2}[a-z]?|x\d)?", re.I)
PIXEL_PAT = re.compile(r"\bpixel?\s?(\d[a-z]?)?\s?(pro)?", re.I)
HUAWEI_PAT = re.compile(r"\b(huawei|хуавей)\s*(p\d{1,2}|mate\s?\d{1,2})?", re.I)
ONEPLUS_PAT = re.compile(r"\b(?:one\s*plus|ван\s*плюс)\s?(\d{1,2}[a-z]?|7t)?", re.I)
GALAXY_PAT = re.compile(r"\b(?:galaxy|самсунг|samsung)\s*(?:s|note|a|z)\s?\d{1,2}", re.I)
MACBOOK_PAT = re.compile(r"\bmacbook\s*(pro|air)?", re.I)
CHIP_PAT = re.compile(r"\bm([1-4])\s*(pro|max)?\b")
IPAD_PAT = re.compile(r"\b(ipad|айпад)\s*(pro|air|mini)?", re.I)
LAPTOP_PAT = re.compile(r"\b(ноутбук|лаптоп|ultrabook|matebook)\b", re.I)
LAPTOP_BRAND = re.compile(r"\b(asus|lenovo|hp|dell|acer|msi|honor|huawei|lg|irbis|dexxp)\b", re.I)
PC_PAT = re.compile(r"\b(компьютер|системник|системн\w*|стационарн\w*|моноблок)\b", re.I)
MONITOR_PAT = re.compile(r"\bмонитор\w*", re.I)
MONITOR_BRAND = re.compile(r"\b(asus|dell|lg|samsung|aoc|msi|philips|benq|xiaomi|xiaom)\b", re.I)
INCH_PAT = re.compile(r"\b(1[5-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-5])\s*(?:\"|дюйм)", re.I)
SSD_PAT = re.compile(r"\b(ssd|hdd|ж[её]стк\w*|внешн\w*\s*диск|накопител\w*|m\.2)\b", re.I)
PS_PAT = re.compile(r"\b(?:ps|playstation|плейстеш)\s?([3-5])(\s?(pro|slim|digital|цифр))?", re.I)
XBOX_PAT = re.compile(r"\bxbox\s?(one|series\s?s|series\s?x|\bs\b|\bx\b)?", re.I)
SWITCH_PAT = re.compile(r"\b(?:nintendo\s*)?switch\s?(lite|oled)?", re.I)
GPU_PAT = re.compile(r"\b(rtx|gtx)\s?(\d{3,4})(\s?(ti|super))?\b", re.I)
RADEON_PAT = re.compile(r"\bradeon\s?(rx\s?\d{3,4})?", re.I)
GPU_BRAND = re.compile(r"\b(asus|msi|gigabyte|zotac|evga|inno3d|palit|gainward|asrock)\b", re.I)
AIRPODS_PAT = re.compile(r"\bairpods\s?(pro)?\s?([1-3])?", re.I)
BUDS_PAT = re.compile(r"\b(?:galaxy\s?)?buds\s?(\d|pro|fe|\d\s?pro|2\s?pro)?", re.I)
HEAD_NOUN = re.compile(r"\b(наушник\w*|гарнитур\w*)", re.I)
HEAD_BRAND = re.compile(r"\b(jbl|xiaomi|honor|sennheiser|marshall|anker|edifier|qcy|jabra|sony)\b", re.I)
HEAD_CODE = re.compile(r"\b(?:wh|wf|qh|wf)[-\s]?\d{3,4}[a-z]?\b", re.I)
WATCH_PAT = re.compile(r"\bapple\s*watch\s?(s\d|se|ultra\s?2?)?", re.I)
GALWATCH_PAT = re.compile(r"\bgalaxy\s?watch\s?(\d|active\s?2?|classic)?\s?(pro)?", re.I)
SMARTWATCH_NOUN = re.compile(r"\bсмарт.?часы\b|\bчасы\b", re.I)
SMART_BRAND = re.compile(r"\b(xiaomi|huawei|honor|amazfit|samsung)\b", re.I)
TV_PAT = re.compile(r"\b(телевизор|tv)\b", re.I)
TV_BRAND = re.compile(r"\b(lg|samsung|philips|xiaomi|sony|haier|horizont|supra)\b", re.I)
DYSON_MODEL = re.compile(r"\b(v\d{1,2}|s\d{2}|sv\d{2})\b", re.I)
SCOOTER_PAT = re.compile(r"\bэлектросамокат\w*", re.I)
SCOOTER_BRAND = re.compile(r"(xiaomi|kugou?|kugoo|ninebot|niubility|likebike|hiper)", re.I)
REG_PAT = re.compile(r"\bвидеорегистратор\w*", re.I)
REG_BRAND = re.compile(r"\b(70mai|dvr|xiaomi|neoline|inspector|carcam|sho-me)\b", re.I)
DRONE_PAT = re.compile(r"\b(dji|квадрокоптер|дрон)\b", re.I)
CAMERA_NOUN = re.compile(r"\b(камер\w*|фотоаппара|зеркалк\w*|беззеркалк\w*)")
CAMERA_BRAND = re.compile(r"\b(canon|nikon|sony|fujifilm|panasonic|olympus|pentax)\b", re.I)
CAMERA_CODE = re.compile(r"\b(a[1-9] ?(?:iii|iv|v|ii|i)?|\d{3,5}d)\b", re.I)


def _build_tech_title(text: str) -> str:
    """распознать товар: собрать «Бренд Модель, характеристики, состояние».
    Никакой техники в посте — вернуть '' (пост не лот)."""
    t = text.lower().replace("ё", "е")
    base = None

    # --- телефоны ---
    m = PHONE_PAT.search(t)
    if m:
        num = (m.group(2) or "").upper()
        vkey = re.sub(r"\s+", " ", (m.group(4) or "").lower())
        var = PHONE_VARIANT.get(vkey, "")
        model = " ".join(x for x in (num, var) if x)
        base = "iPhone" + (f" {model}" if model else "")
    if not base:
        m = PHONE_BR_PAT.search(t)
        if m and (m.group(2) or m.group(1)):
            b = PHONE_BRAND.get(m.group(1), m.group(1))
            model = (m.group(2) or "").strip()
            # «Xiaomi Redmi Note 12 Pro»: второй бренд уточняет модель
            if not model:
                m2 = PHONE_BR_PAT.search(t, m.end())
                if m2 and PHONE_BRAND.get(m2.group(1), "").lower() != b.lower():
                    b = PHONE_BRAND.get(m2.group(1), m2.group(1))
                    model = (m2.group(2) or "").strip()
            model = re.sub(r"\s+", " ", model).upper().replace("NOTE", "Note").replace(" PRO", " Pro").replace("ПРО", " Pro")
            base = b + (f" {model}" if model else "")
    if not base:
        m = GALAXY_PAT.search(t)
        if m:
            base = "Samsung " + re.sub(r"\s+", " ", m.group(0)).upper().replace("SAMSUNG ", "").replace("GALAXY", "Galaxy")
    if not base:
        m = HONOR_PAT.search(t)
        if m and m.group(2):
            base = "Honor " + re.sub(r"\s+", " ", m.group(2)).upper().replace("MAGIC", "Magic")
    if not base:
        m = PIXEL_PAT.search(t)
        if m:
            base = "Google Pixel " + (m.group(1) or "").upper() + (" Pro" if m.group(2) else "")
    if not base:
        m = HUAWEI_PAT.search(t)
        if m:
            base = "Huawei " + re.sub(r"\s+", " ", m.group(2) or "").upper().replace("MATE", "Mate")
    if not base:
        m = ONEPLUS_PAT.search(t)
        if m:
            base = "OnePlus " + (m.group(1) or "").upper()

    # --- компьютеры/планшеты/ТВ ---
    if not base:
        m = MACBOOK_PAT.search(t)
        if m:
            var = (m.group(1) or "").capitalize()
            chip = CHIP_PAT.search(t)
            st = _storage(t)
            bits = ["MacBook", var]
            if chip:
                bits.append((f"M{chip.group(1)}" + (f" {chip.group(2).title()}" if chip.group(2) else "")))
            if st:
                bits.append(st)
            base = " ".join(bits)
    if not base:
        m = IPAD_PAT.search(t)
        if m:
            var = (m.group(2) or "").capitalize()
            st = _storage(t)
            base = ("iPad" + (f" {var}" if var else "")) + (f" {st}" if st else "")
    if not base:
        m = LAPTOP_PAT.search(t)
        if m:
            b = LAPTOP_BRAND.search(t)
            st = _storage(t)
            base = "Ноутбук" + (f" {b.group(1).upper()}" if b else "") + (f" {st}" if st else "")
    if not base:
        m = PC_PAT.search(t)
        if m:
            base = "Компьютер" + (f" {_storage(t)}" if _storage(t) else "")
    if not base:
        m = MONITOR_PAT.search(t)
        if m:
            b = MONITOR_BRAND.search(t)
            inch = INCH_PAT.search(t)
            base = "Монитор" + (f" {b.group(1).upper()}" if b else "") + (f' {inch.group(1)}"' if inch else "")
    if not base:
        m = SSD_PAT.search(t)
        if m:
            st = _storage(t)
            noun = "SSD" if re.search(r"\bssd|m\.2", t) else ("HDD" if re.search(r"\bhdd", t) else "Жёсткий диск")
            base = noun + (f" {st}" if st else "")

    # --- консоли ---
    if not base:
        m = PS_PAT.search(t)
        if m:
            var = (m.group(3) or "").lower()
            var = {"pro": "Pro", "slim": "Slim", "digital": "Digital", "цифр": "Digital"}.get(var, "")
            base = f"PlayStation {m.group(1)}" + (f" {var}" if var else "")
    if not base:
        m = XBOX_PAT.search(t)
        if m:
            v = (m.group(1) or "").lower().replace("\u00a0", " ")
            if v.startswith("series"):
                var = f"Series {v[-1].upper()}"
            elif v == "one":
                var = "One"
            else:
                var = v.upper()
            base = "Xbox" + (f" {var}".strip() if var else "")
    if not base:
        m = SWITCH_PAT.search(t)
        if m:
            v = (m.group(1) or "").lower()
            var = {"oled": "OLED", "lite": "Lite"}.get(v, v.capitalize())
            base = "Nintendo Switch" + (f" {var}" if var else "")

    # --- видеокарты ---
    if not base:
        m = GPU_PAT.search(t)
        if m:
            gb = GPU_BRAND.search(t)
            b = gb.group(1).upper() if gb else "NVIDIA"
            var = (m.group(4) or "").upper()
            if var.lower() == "ti":
                var = "Ti"
            base = f"{b} {m.group(1).upper()} {m.group(2)}" + (f" {var}" if var else "")
    if not base:
        m = RADEON_PAT.search(t)
        if m and m.group(1):
            gb = GPU_BRAND.search(t)
            b = gb.group(1).upper() if gb else "AMD"
            base = f"{b} {m.group(1).upper()}"

    # --- аудио/часы ---
    if not base:
        m = AIRPODS_PAT.search(t)
        if m:
            gen = f" {m.group(2)}" if m.group(2) else ""
            base = ("AirPods Pro" if m.group(1) else "AirPods") + gen
    if not base:
        m = BUDS_PAT.search(t)
        if m:
            var = (m.group(1) or "").capitalize()
            base = "Buds" + (f" {var}" if var else "")
    if not base:
        m = HEAD_NOUN.search(t)
        if m:
            b = HEAD_BRAND.search(t)
            base = "Наушники" + (f" {b.group(1).upper()}" if b else "")
            code = HEAD_CODE.search(text)
            if code:
                base += f" {code.group(0).upper()}"
    if not base:
        m = WATCH_PAT.search(t)
        if m:
            var = re.sub(r"\s+", " ", (m.group(1) or "")).upper().replace("ULTRA", "Ultra").replace("SE", "SE")
            base = "Apple Watch" + (f" {var}" if var else "")
    if not base:
        m = GALWATCH_PAT.search(t)
        if m:
            var = re.sub(r"\s+", " ", (m.group(1) or "")).upper()
            pro = " Pro" if m.group(2) else ""
            base = "Samsung Galaxy Watch" + (f" {var}{pro}" if var else "")
    if not base:
        m = SMARTWATCH_NOUN.search(t)
        if m:
            b = SMART_BRAND.search(t)
            if not b and not re.search(r"смарт", t):
                # обычные наручные часы — не наша категория
                return ""
            base = "Смарт-часы" + (f" {b.group(1).title()}" if b else "")

    # --- ТВ/дом/ков: прочая техника ---
    if not base:
        m = TV_PAT.search(t)
        if m:
            b = TV_BRAND.search(t)
            inch = INCH_PAT.search(t)
            base = "Телевизор" + (f" {b.group(1).upper()}" if b else "") + (f' {inch.group(1)}"' if inch else "")
    if not base:
        if re.search(r"\bпылесос\w*", t):
            if re.search(r"\b(dyson|дайсон)", t):
                mod = DYSON_MODEL.search(t)
                base = "Пылесос Dyson" + (f" {mod.group(1).upper()}" if mod else "")
            else:
                b = re.search(r"\b(xiaomi|bosch|philips|lg|samsung|panasonic|karcher|hitachi|makita)\b", t, re.I)
                mod = re.search(r"\b([a-z]{2,4}[- ]?\d{2,4}[a-z]{0,2})\b", t)
                base = "Пылесос" + (f" {b.group(1).upper()}" if b else "") + (f" {mod.group(1).upper()}" if mod else "")
    if not base:
        m = SCOOTER_PAT.search(t)
        if m:
            b = SCOOTER_BRAND.search(t)
            base = "Электросамокат" + (f" {b.group(1).title()}" if b else "")
    if not base:
        m = REG_PAT.search(t)
        if m:
            b = REG_BRAND.search(t)
            base = "Видеорегистратор" + (f" {b.group(1).upper()}" if b else "")
    if not base:
        m = DRONE_PAT.search(t)
        if m:
            base = "Дрон"
    if not base:
        m = CAMERA_NOUN.search(t)
        if m:
            b = CAMERA_BRAND.search(t)
            code = CAMERA_CODE.search(t)
            base = "Фотокамера" + (f" {b.group(1).title()}" if b else "") + (f" {code.group(0).upper()}" if code else "")

    if not base:
        return ""

    parts = [base]
    st = _storage(t) or _phone_storage(t)
    if st and st.lower() not in base.lower():
        parts.append(st)
    bat = _battery(t)
    if bat:
        parts.append(bat)
    cond = _condition(t)
    if cond and "с дефектом" not in (cond, base):
        parts.append(cond)
    title = _join(parts)
    return title if len(title) >= 4 else ""

# клин по категориям: (регэксп, минимальная правдоподобная цена):
# сравниваются ПО ПОРЯДКУ, берём Первое совпадение (сначала узкие модели)
FLOOR_RULES = [
    (r"\b(ps\s?4|playstation\s?4)\b", 4500),
    (r"\b(ps\s?5|playstation\s?5)\b", 7000),
    (r"playstation", 7000),
    (r"\bxbox\b", 4500),
    (r"\bswitch\b", 4500),
    (r"\b(macbook|макбук)\b", 14000),
    (r"\b(ноутбук|ноуттб)\b", 8000),
    (r"\b(rt[хx]|rtx)\s?\d{3,4}", 5000),
    (r"\b(gtx|gt[хx])\s?\d{3,4}", 2500),
    (r"\bradeon\b", 3000),
    (r"\b(redmi|редми)\b", 2500),
    (r"\b(poco|поко)\b", 2500),
    (r"\b(xiaomi|сяоми)\b", 2500),
    (r"\b(honor|хонор)\b", 2000),
    (r"\b(dyson|дайсон)\b", 4000),
    (r"\b(ipad|айпад)\b", 6000),
    (r"\b(iphone|айфон)\b", 3500),
    (r"\b(galaxy|самсунг)\b", 3500),
    (r"\b(samsung)\b", 3000),
    (r"\b(airpods)\b", 900),
    (r"\b(наушник\w*|гарнитур\w*)\b", 700),
    (r"\b(apple\s?watch)\b|\bwatch\b", 1600),
    (r"\bсмарт.?часы\b", 1500),
    (r"\bмонитор\w*\b", 1500),
    (r"\bтелевизор\b", 4000),
    (r"\bпылесос\w*\b", 2000),
    (r"\bэлектросамокат\w*\b", 3000),
    (r"\b(ssd|hdd)\b", 600),
    (r"\b(дрон|dji)\b", 3000),
    (r"\b(фотокамера|камер\w*|фотоаппара|зеркалк\w*)\b", 3000),
    (r"\bкомпьютер\b|системник", 5000),
    (r"\bвидеорегистратор\w*\b", 1500),
]
DEFAULT_FLOOR = 900


def floor_for(title: str) -> int:
    tl = title.lower().replace("ё", "е")
    for pat, fl in FLOOR_RULES:
        if re.search(pat, tl):
            return fl
    return DEFAULT_FLOOR

PRICE_RE = re.compile(r"(\d[\d\s\u00a0.,]{2,13})\s*(руб|₽|р\.|р(?!\w)|rub)", re.IGNORECASE)


def normalize_price(raw: str):
    """«1 800 000» -> 1800000; «m1 180000 ₽» -> 180000; «256 90000» -> 90000
    (не прирасти к цене-мусору)"""
    groups = re.split(r"[\s\u00a0]+", (raw or "").strip())
    digits = re.sub(r"[^\d]", "", (raw or "").replace(",", "").replace(".", ""))
    if not digits:
        return 0
    val = int(digits)
    if val >= 500_000:
        # «256 90000» / «m1 180000»: последняя группа — сама цена
        if len(groups) >= 2:
            tail = int(re.sub(r"[^\d]", "", groups[-1]) or 0)
            if 1000 <= tail <= 499_999:
                return tail
            # «1 800 000» — нормальное машинное написание крупной цены
            if len(groups) >= 3 and all(1 <= len(g) <= 3 for g in groups):
                return val
        return 0
    if val >= 1_000_000 and len(groups) == 2:
        # «m1 180000» из 2 групп: первая — мусорная приставка, берём вторую
        alt = int(re.sub(r"[^\d]", "", groups[1]) or 0)
        if 0 < alt < val:
            val = alt
    if val >= 1_000_000 and len(groups) >= 3 and all(1 <= len(g) <= 3 for g in groups):
        return val
    return val


def clean_price(raw: str):
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return 0
    return int(re.sub(r"^0+", "", digits) or "0")


def extract_lots(text: str, channel: str, msg_id: int):
    if not text or len(text) < 8:
        return None
    low = text.lower().replace("ё", "е")
    if "запчаст" in low or "разборк" in low:
        return None
    if any(w in low for w in SCAM_WORDS):
        return None
    if any(w in low for w in DEMAND_WORDS):
        return None
    has_sale = any(w in low for w in SALE_WORDS)
    # работа/услуги/подработка — не лоты
    if any(w in low for w in NON_ITEM_WORDS):
        return None
    # «пишите в лс/личку» + нигде нет товара → анонс/болтовня, не лот
    if any(w in low for w in ("в лс", "в лc", "личку", "в личные")) and not PRODUCT_PAT.search(low):
        return None
    # обфусцированный («гламурный») текст — уловка спама, не лот
    if _glitch_ratio(text) > 0.15:
        return None
    # шум-заголовок («всем привет», «дорогие друзья», «по наличию») отсекаем только
    # если в посте нет явного «продаю/цена:» — иначе режем настоящие продажи
    first_line0 = text.strip().splitlines()[0]
    if _is_noise_title(re.sub(r"^#\w+\s*", "", first_line0).strip()[:120]) and not has_sale:
        return None
    # берём цену: сначала с валютой (по всему посту), иначе голое число:
    # приоритет — строка с товаром («Продам ... 26000»), потом любая строка
    price = 0
    for m in PRICE_RE.finditer(text):
        cand = normalize_price(m.group(1))
        if 300 <= cand <= 499_999:
            price = cand
            break
    if price == 0:
        # fallback: цена без валюты. Смотрим голые числа в конце строк
        # («iPhone 18 pro 256gb eSIM. 96000» / «стооимость 26000»);
        # предпочитаем строку с товарным словом, из неё — последнее число
        lines = [ln for ln in text.strip().splitlines() if ln.strip()][:12]
        any_line_cand = 0
        for ln in lines:
            cand = 0
            for m in re.finditer(r"(\d[\d\s\u00a0.,]{2,9})(?!\w)", ln):
                if "/" in m.group(1):
                    continue  # «16/512» — память, не цена
                v = normalize_price(m.group(1))
                if 900 <= v <= 499_999:
                    cand = v
            last = None
            if cand:
                last = cand
            elif 0:
                pass
            if not last:
                # одно последнее «похожее на цену» число строки
                ms = list(re.finditer(r"(\d[\d\s\u00a0.,]{0,9})(?!\w)", ln))
                if ms:
                    v = normalize_price(ms[-1].group(1))
                    if 900 <= v <= 499_999:
                        last = v
            if not last:
                continue
            if PRODUCT_PAT.search(ln.lower()):
                price = last
                break
            if not any_line_cand:
                any_line_cand = last
        if price == 0:
            price = any_line_cand
    if price < 300:  # отсекаем «100р» болтовню и цены-пустышки
        return None
    # каталог продавца (много разных телефонов/товаров) — не частный лот
    if _is_catalog_post(text):
        return None
    title = _build_tech_title(text)
    if not title:
        # техника не распознана — пост не наш лот (одежда/мебель/болтовня/услуги)
        return None
    tl = title.lower()
    # сверхкороткое название + копеечная цена — мусор
    if len(title) < 13 and price < 3000:
        return None
    floor = floor_for(title)
    if price < floor:
        return None
    return {
        "id": f"tg_{channel}_{msg_id}",
        "title": title,
        "price": price,
        "url": f"https://t.me/{channel}/{msg_id}",
        "channel": channel[:40],
        "excerpt": text.strip()[:160],
    }


def push_to_server(lots):
    """Отправка лотов в DealHunter (source=TG). Возвращает сколько принято."""
    if not SERVER_URL or not WORKER_SECRET:
        print("push: нет SERVER_URL/WORKER_SECRET в .env")
        return 0
    payload = [{
        "id": l["id"],
        "title": l["title"][:200],
        "source": "TG",
        "city": "Москва",
        "price": l["price"],
        "market": l["price"],
        "tags": ["барахолка"],
        "sellerName": "Автор поста",
        "sellerRating": 0,
        "sellerReviews": 0,
        "description": l["excerpt"],
        "url": l["url"],
    } for l in lots]
    saved = 0
    with httpx.Client(timeout=60) as c:
        for k in range(0, len(payload), 100):
            try:
                r = c.post(f"{SERVER_URL}/api/worker/ingest", json=payload[k:k + 100],
                           headers={"X-Worker-Secret": WORKER_SECRET})
                print(f"push: {r.status_code} {r.text[:100]}")
                if r.status_code == 200:
                    saved += r.json().get("saved", 0)
            except Exception as e:
                print(f"push: ошибка {type(e).__name__}")
    return saved


async def run():
    from telethon import TelegramClient

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--scan"
    if not API_ID or not API_HASH:
        print("В .env нужны TG_API_ID и TG_API_HASH (см. my.telegram.org)")
        return

    client = TelegramClient(SESSION_FILE, int(API_ID), API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERR: сессии нет — сначала выполни вход (tg_login.py)")
        await client.disconnect()
        return
    try:
        if cmd == "--login":
            me = client.get_me()
            print(f"Вход ок: {me.first_name} (@{me.username or '—'})")
            return

        if cmd == "--list":
            print("Каналы/группы, в которых ты состоишь:")
            async for d in client.iter_dialogs(limit=100):
                if d.is_channel or d.is_group:
                    username = getattr(d.entity, "username", None)
                    mark = "public" if username else "private"
                    print(f"  [{mark}] {d.name[:40]:40}  @{'б' if not username else username}")
            return

        # --scan
        if not CHANNELS_FILE.exists():
            print(f"Нет {CHANNELS_FILE.name}. Создай файл, по одному @каналу на строку.")
            print("Подсказка: python tg_scan.py --list  покажет твои каналы.")
            return
        chans = []
        for l in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            l = l.strip().lstrip("@")
            if not l or l.startswith("#"):
                continue
            chans.append(l.split()[0])  # «@имя # описание» -> имя
        if not chans:
            print("tg_channels.txt пустой.")
            return

        state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
        full = "--full" in sys.argv

        all_lots = []
        for ch in chans:
            try:
                min_id = 0 if full else state.get(ch, 0)
                msgs = await client.get_messages(ch, limit=100 if full else 50, min_id=min_id)
            except Exception as e:
                name = type(e).__name__
                if name == "FloodWaitError":
                    wait = getattr(e, "seconds", 60)
                    print(f"[{ch}] FloodWait {wait}с — ждём (правило белой зоны)")
                    time.sleep(min(wait, 300))
                    continue
                import traceback
                print(f"[{ch}] пропуск: {name}")
                traceback.print_exc()
                continue
            got = []
            for m in msgs:
                if not m.message:
                    continue
                lot = None
                try:
                    lot = extract_lots(m.message, ch, m.id)
                except Exception as e:
                    print(f"[{ch}] extract_lots ошибка {type(e).__name__}: {e} | msg={m.message[:80]!r}")
                if lot:
                    got.append(lot)
            last_id = max([m.id for m in msgs], default=None)
            if last_id and not full:
                state[ch] = max(state.get(ch, 0), last_id)
            print(f"[{ch}] постов прочитано: {len(msgs)}, лотов с ценой: {len(got)}")
            all_lots.extend(got)

        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")

        # местная витрина: чтение -> merger c прошлым (последние 300 лотов)
        old = json.loads(LOTS_FILE.read_text("utf-8")) if LOTS_FILE.exists() else []
        by_id = {l["id"]: l for l in old}
        for l in all_lots:
            by_id[l["id"]] = l
        merged = sorted(by_id.values(), key=lambda x: x["id"], reverse=True)[:300]
        LOTS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")
        print(f"\nИтого свежих лотов: {len(all_lots)}. Всего в витрине: {len(merged)}. Файл: {LOTS_FILE.name}")

        # свежие лоты отдельно (для контроля подавления мусора)
        try:
            Path("tg_fresh.json").write_text(
                json.dumps(all_lots, ensure_ascii=False, indent=1), "utf-8")
        except Exception:
            pass

        if all_lots and ("--push" in sys.argv or cmd == "--push"):
            saved = push_to_server(all_lots)
            print(f"Принято на сервер: {saved}")
            await mark_gone_tg(client, all_lots)
    finally:
        await client.disconnect()


async def mark_gone_tg(client, all_lots):
    """Пост пропал с барахолки (удалён/отредактирован в «продано») — лот реально
    ушёл. Проверяем по самому Telethon и помечаем gone на сервере."""
    if "--no-push" in sys.argv or not WORKER_SECRET:
        return
    import httpx
    try:
        r = httpx.get(
            f"{SERVER_URL}/api/worker/lots-ids",
            params={"source": "TG"},
            headers={"X-Worker-Secret": WORKER_SECRET}, timeout=60,
        )
        if r.status_code != 200:
            return
        prod_rows = r.json().get("rows", [])
    except Exception as e:
        print(f"[!] маркировка gone TG не удалась (сеть): {e}")
        return
    fresh_ids = {str(l["id"]) for l in all_lots}
    gone = []
    for row in prod_rows:
        lot_id = str(row.get("id", ""))
        if lot_id in fresh_ids:
            continue
        try:
            # id вида tg_<канал>_<msg_id>
            parts = lot_id.split("_")
            ch, mid = parts[1], parts[2]
            mid = int(mid)
        except Exception:
            continue
        try:
            live = await client.get_messages(ch, ids=[mid])
            if not live or not any(m.message for m in live):
                gone.append(lot_id)
        except Exception as e:
            print(f"[g] {ch}: {type(e).__name__}")
    if not gone:
        return
    try:
        rg = httpx.post(
            f"{SERVER_URL}/api/worker/lots-gone",
            headers={"X-Worker-Secret": WORKER_SECRET},
            json={"source": "TG", "ids": gone[:1000]}, timeout=60,
        )
        if rg.status_code == 200:
            print(f"пропавших лотов TG помечено: {rg.json().get('gone', 0)}")
    except Exception as e:
        print(f"[!] маркировка gone TG не удалась: {e}")


if __name__ == "__main__":
    print("TG-скан DealHunter — читаю только публичные каналы, по своему аккаунту.")
    import asyncio
    try:
        asyncio.run(run())
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
