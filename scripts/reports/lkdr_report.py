#!/usr/bin/env python3
"""Отчёт по покупкам из SQLite-базы LKDR."""

from __future__ import annotations

import argparse
import math
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import _config


DEFAULT_DB = "lkdr.db"
DEFAULT_DAYS = 30
MAX_TABLE_CELL_WIDTH = 75
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MARKDOWN_HEADING_RE = re.compile(r"^\s*\*\*(\d+\.\s+[^*]+)\*\*\s*$")
MARKDOWN_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


class Color:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def apply(self, value: object, code: str) -> str:
        text = str(value)
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def header(self, value: object) -> str:
        return self.apply(value, "1;34")

    def bold(self, value: object) -> str:
        return self.apply(value, "1")

    def positive(self, value: object) -> str:
        return self.apply(value, "32")

    def negative(self, value: object) -> str:
        return self.apply(value, "31")

    def warning(self, value: object) -> str:
        return self.apply(value, "33")

    def muted(self, value: object) -> str:
        return self.apply(value, "90")


COLOR = Color(False)

# Режим markdown (--format md): заголовки ##/### и GFM-таблицы вместо
# ASCII-рамок — для вывода отчёта прямо в чат AI-агента. Переключается
# в run_report; цвет при этом принудительно выключен.
FORMAT_STATE = {"md": False, "first_header": True}


def md_cell(value: object) -> str:
    """Ячейка для markdown-таблицы: без ANSI, экранированные пайпы."""
    return ANSI_RE.sub("", str(value)).replace("|", "\\|").replace("\n", " ")


@dataclass
class MutableStats:
    count: int = 0
    total: float = 0
    gross_total: float = 0
    ignored_count: int = 0
    ignored_total: float = 0
    refund_count: int = 0
    refund_total: float = 0


@dataclass
class ItemStats:
    quantity: float = 0
    total: float = 0
    purchase_receipts: set[str] = field(default_factory=set)
    refund_receipts: set[str] = field(default_factory=set)
    # receipt_key → количество позиции в этом чеке (только покупки):
    # из этих разовых порций выводится шаг целой упаковки для корзины.
    purchase_portions: dict[str, float] = field(default_factory=dict)


@dataclass
class PeriodReport:
    start: datetime
    end: datetime
    stats_by_currency: defaultdict[str, MutableStats]
    stores: defaultdict[tuple[str, str], MutableStats]
    days_total: defaultdict[tuple[str, str], MutableStats]
    refund_stores: defaultdict[tuple[str, str], MutableStats]
    items: defaultdict[tuple[str, str], ItemStats]
    # Чеки, в которых есть хотя бы одна позиция приватной категории:
    # позиции «Прочего» из таких чеков не выводятся построчно, а магазины
    # маскируются — иначе название аптеки деанонимизирует характер покупки.
    private_receipts: set[str] = field(default_factory=set)
    # receipt_key → название магазина (для маскирования магазинов).
    receipt_store: dict[str, str] = field(default_factory=dict)

    def private_stores(self) -> set[str]:
        return {
            store
            for key, store in self.receipt_store.items()
            if key in self.private_receipts
        }


CURRENCY_SYMBOLS = {
    "RUB": "₽",
    "KZT": "₸",
}

CURRENCY_NAMES = {
    "RUB": "рубли",
    "KZT": "тенге",
}

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

KAZAKHSTAN_MARKERS = (
    "казахстан",
    "kazakhstan",
    "алматы",
    "астана",
    "almaty",
    "astana",
    ".kz",
)

SERVICE_ITEM_MARKERS = (
    "аванс",
    "доставка",
    "доставк",
    "курьер",
    "упаковка заказа",
    "компенсация",
    "возврат",
    "агентское вознаграждение",
    "перевозка",
    "расходы по поручению",
    "услуги связи",
)

CATEGORY_RULES = (
    # Растительные масла — до общих правил: иначе общий маркер «масло»
    # из молочных перехватывает «масло растительное» (ломались категория,
    # срок годности и корзина: 7 дн. вместо 180).
    (
        "Бакалея",
        (
            "масло раст",
            "масло подсолн",
            "масло олив",
            "масло кукуруз",
            "масло кунжут",
        ),
    ),
    (
        "Молочные продукты",
        (
            "молоко",
            "кефир",
            "ряженка",
            "сметан",
            "йогурт",
            "творог",
            "сырок",
            "сыр",
            "сливк",
            "масло",
        ),
    ),
    (
        "Мясо и птица",
        (
            "мясо",
            "курица",
            "курин",
            "цыплён",
            "цыплен",
            "бройлер",
            "индейк",
            "говядина",
            "говяж",
            "свинина",
            "свин",
            "фарш",
            "котлет",
            "колбас",
            "сосиск",
            "ветчин",
            "буженин",
        ),
    ),
    (
        "Рыба и морепродукты",
        (
            "рыба",
            "лосос",
            "форель",
            "треск",
            "тунец",
            "кревет",
            "морепродукт",
            "икра",
        ),
    ),
    (
        "Овощи и фрукты",
        (
            "овощ",
            "фрукт",
            "картоф",
            "томат",
            "помидор",
            "огур",
            "морков",
            "лук",
            "капуст",
            # Листовые салаты: общий маркер «салат» ловил «Салат Цезарь»
            # из кулинарии (см. Готовая еда).
            "салат лист",
            "салат латук",
            "салат айсберг",
            "салат ромэн",
            "салат романо",
            "зелень",
            "яблок",
            "банан",
            "груш",
            "апельсин",
            "мандарин",
            "ягод",
            "нектарин",
            "арбуз",
            "дын",
            "персик",
            "виноград",
        ),
    ),
    (
        "Хлеб и выпечка",
        (
            "хлеб",
            "батон",
            "булочка",
            "лаваш",
            "пирог",
            "круассан",
            "выпеч",
            "медов",
        ),
    ),
    (
        "Бакалея",
        (
            "яйцо",
            "крупа",
            "греч",
            "рис",
            "макарон",
            "мука",
            "сахар",
            "соль",
            "соус",
            "спец",
            "консерв",
            # «хлоп» ловил «Хлопковый плед»; хлопья — еда, хлопок — нет.
            "хлопья",
            "мюсли",
        ),
    ),
    (
        "Напитки",
        (
            "чай",
            "кофе",
            "сок",
            "вода",
            "морс",
            "лимонад",
            "напит",
            "кисель",
        ),
    ),
    (
        "Сладости и снеки",
        (
            "шоколад",
            "печень",
            "конфет",
            "вафл",
            "морожен",
            "чипс",
            "снэк",
            "снек",
            "пирожн",
        ),
    ),
    (
        "Готовая еда",
        (
            "пицца",
            "ролл",
            "суши",
            "бургер",
            "шаурм",
            "кофейня",
            "кафе",
            "ресторан",
            "яндекс еда",
            "delivery",
            "додо",
            "пельмен",
            "воппер",
            "калифорни",
            "наггетс",
            "салат цез",
            "салат оливье",
            "салат краб",
        ),
    ),
    (
        "Дом и ремонт",
        (
            "смеситель",
            "термостат",
            "лампа",
            "светильник",
            "розетка",
            "кабель",
            "краска",
            "инструмент",
            "шуруп",
            "сантех",
            "ванн",
            "душ",
            "кухн",
            "мебель",
            "икеа",
            "леруа",
            "мусор",
            "форма для льда",
            "чехол",
            "герметик",
            "полотенц",
        ),
    ),
    (
        "Одежда и обувь",
        (
            "трусы",
            "носки",
            "футболка",
            "рубашка",
            "брюки",
            "джинсы",
            "куртка",
            "платье",
            "кроссов",
            "ботин",
            "обув",
            "одежд",
            "лонгслив",
            "omsа",
            "omsa",
        ),
    ),
    (
        "Бытовая химия",
        (
            "порошок",
            "гель для стир",
            "кондиционер для белья",
            "средство для",
            "чистящ",
            "моющ",
            "мыло",
            "шампун",
            "зубная паста",
            "дезодорант",
            "салфет",
            "бумага туалет",
            "туалетная бумага",
            "для посудомоеч",
        ),
    ),
    (
        "Аптека и здоровье",
        (
            "аптека",
            "лекар",
            "табл",
            "капс.",
            "капл",
            "мазь",
            "витамин",
            "спрей",
            "сироп",
            "бинт",
            "пластыр",
            "линз",
            "анализ",
            "крови",
            "диагност",
            "сорбент",
            "эмульс",
            "врач",
            "клиник",
            "лаборатор",
            "процедур",
        ),
    ),
    (
        "Электроника",
        (
            "телефон",
            "смартфон",
            "ноутбук",
            "планшет",
            "заряд",
            "наушник",
            "кабель usb",
            "аккумулятор",
            "батарей",
        ),
    ),
    (
        "Транспорт",
        (
            "такси",
            "метро",
            "автобус",
            "бензин",
            "топливо",
            "парков",
            "проезд",
        ),
    ),
    (
        "Связь и подписки",
        (
            "услуг связи",
            "услуги связи",
            "подписк",
        ),
    ),
    (
        "ЖКХ и услуги",
        (
            "счетчик",
            "гвс",
            "хвс",
            "мастер на час",
            "жкх",
        ),
    ),
    (
        "Косметика и гигиена",
        (
            "крем",
            "гель для умыв",
            "тампон",
            "прокладк",
            "зубн",
            "бритв",
            "презерватив",
        ),
    ),
    (
        "Аксессуары",
        (
            "чемодан",
            "зонт",
            "ремешок",
            "картридж",
            "триммер",
            "компрессор",
            "насос",
        ),
    ),
    (
        "Сервисы и комиссии",
        SERVICE_ITEM_MARKERS,
    ),
)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def money(value: float | int | None, currency: str) -> str:
    # +0.0 нормализует -0.0: без этого печатались «-0.00 ₽».
    value = float(value or 0) + 0.0
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    return f"{value:,.2f}".replace(",", " ") + f" {symbol}"


def signed_refund(value: float, currency: str) -> str:
    """Возврат со знаком минуса везде, где он показан: возврат уменьшает
    расходы. Ноль печатается без минуса."""
    if value > 0:
        return COLOR.warning(f"-{money(value, currency)}")

    return COLOR.warning(money(value, currency))


def format_qty(value: float) -> str:
    """Человекочитаемое количество для таблиц: 2; 0.5; 0.47 — без хвоста
    значащих цифр вроде 0.4667."""
    rounded = round(value, 2)
    if rounded == int(rounded):
        return f"{int(rounded)}"

    return f"{rounded:g}"


def percent(value: float, total: float) -> str:
    if total == 0:
        return "0.0%"
    return f"{value / total * 100:.1f}%"


def percent_delta(value: float, previous: float) -> str:
    if previous == 0:
        return "н/д"
    delta = (value - previous) / previous * 100
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.1f}%"


def share_bar(value: float, total: float, width: int = 10) -> str:
    if total <= 0:
        return f"{'░' * width} 0.0%"

    ratio = max(0.0, min(value / total, 1.0))
    filled = min(width, max(1, int(ratio * width + 0.999))) if value > 0 else 0
    bar = "█" * filled + "░" * (width - filled)
    if ratio >= 0.25:
        bar = COLOR.warning(bar)
    else:
        bar = COLOR.muted(bar)
    return f"{bar} {percent(value, total)}"


def print_table(headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> None:
    rows = [tuple(truncate_cell(cell) for cell in row) for row in rows]
    if not rows:
        print(COLOR.muted("(нет данных)"))
        return

    if FORMAT_STATE["md"]:
        print("| " + " | ".join(md_cell(header) for header in headers) + " |")
        print("|" + "---|" * len(headers))
        for row in rows:
            print("| " + " | ".join(md_cell(cell) for cell in row) + " |")
        print()
        return

    widths = [visible_len(header) for header in headers]
    for row in rows:
        widths = [max(width, visible_len(cell)) for width, cell in zip(widths, row)]

    print(COLOR.bold(format_row(headers, widths)))
    print(COLOR.muted(format_row(tuple("-" * width for width in widths), widths)))
    for row in rows:
        print(format_row(row, widths))


def visible_len(value: object) -> int:
    return len(ANSI_RE.sub("", str(value)))


def truncate_cell(value: object, limit: int = MAX_TABLE_CELL_WIDTH) -> str:
    text = str(value)
    if visible_len(text) <= limit:
        return text
    if ANSI_RE.search(text):
        return text
    return text[: limit - 3].rstrip() + "..."


def pad_cell(value: object, width: int) -> str:
    text = str(value)
    return text + " " * (width - visible_len(text))


def format_row(row: tuple[object, ...], widths: list[int]) -> str:
    return "  ".join(pad_cell(cell, width) for cell, width in zip(row, widths))


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    aliases = {
        "RUR": "RUB",
        "₽": "RUB",
        "РУБ": "RUB",
        "РУБЛЬ": "RUB",
        "РУБЛИ": "RUB",
        "ТГ": "KZT",
        "₸": "KZT",
        "ТЕНГЕ": "KZT",
    }
    return aliases.get(currency, currency)


def parse_currency_rule(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Expected NAME=CURRENCY")

    name, currency = value.split("=", 1)
    name = name.strip()
    currency = normalize_currency(currency)
    if not name or not currency:
        raise argparse.ArgumentTypeError("Expected NAME=CURRENCY")
    return name, currency


def build_rule_map(rules: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name.casefold(): currency for name, currency in rules}


def require_tables(conn: sqlite3.Connection) -> None:
    required = {"receipts", "brands", "fiscal_data", "fiscal_data_items"}
    existing = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name in ({})".format(
                ",".join("?" for _ in required)
            ),
            tuple(required),
        )
    }
    missing = sorted(required - existing)
    if missing:
        raise SystemExit(f"Database schema is missing required tables: {', '.join(missing)}")


def latest_receipt_datetime(conn: sqlite3.Connection) -> datetime:
    # datetime() нормализует строки с разными офсетами (+03:00, +05:00)
    # к UTC: лексикографический max() на смешанных зонах ошибался бы.
    row = conn.execute(
        "select date_time from fiscal_data order by datetime(date_time) desc limit 1"
    ).fetchone()
    if not row or row[0] is None:
        raise SystemExit("No fiscal data found in database")
    return parse_datetime(row[0])


def earliest_receipt_datetime(conn: sqlite3.Connection) -> datetime:
    row = conn.execute(
        "select date_time from fiscal_data order by datetime(date_time) asc limit 1"
    ).fetchone()
    if not row or row[0] is None:
        raise SystemExit("No fiscal data found in database")
    return parse_datetime(row[0])


def in_reference_zone(value: datetime, reference: datetime) -> datetime:
    """Naive-время приводится к зоне reference (стенные часы не меняются):
    для смешанных баз, где часть строк без офсета."""
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)

    return value


def store_name(row: sqlite3.Row) -> str:
    return row["store"] or row["kkt_owner"] or row["fd_user"] or "Unknown"


def detect_currency(
    row: sqlite3.Row,
    store_rules: Mapping[str, str],
    receipt_rules: Mapping[str, str],
) -> str:
    receipt_key = str(row["receipt_key"]).casefold()
    if receipt_key in receipt_rules:
        return receipt_rules[receipt_key]

    store = store_name(row).casefold()
    if store in store_rules:
        return store_rules[store]

    text = " ".join(
        str(row[key] or "")
        for key in (
            "store",
            "kkt_owner",
            "fd_user",
            "user_inn",
            "retail_place",
            "retail_place_address",
        )
    ).casefold()
    if any(marker in text for marker in KAZAKHSTAN_MARKERS):
        return "KZT"

    return "RUB"


def effective_spend(row: sqlite3.Row) -> float:
    if operation_sign(row) < 0:
        return float(row["total_sum"] or 0)
    return max(float(row["total_sum"] or 0) - float(row["prepaid_sum"] or 0), 0)


def operation_sign(row: sqlite3.Row) -> int:
    operation_type = int(row["operation_type"] or 1)
    if operation_type in (2, 3):
        return -1
    return 1


def signed_effective_spend(row: sqlite3.Row) -> float:
    return operation_sign(row) * effective_spend(row)


def is_service_item(name: str) -> bool:
    normalized = name.casefold()
    return any(marker in normalized for marker in SERVICE_ITEM_MARKERS)


def categorize_item(name: str) -> str:
    normalized = name.casefold()
    for category, markers in CATEGORY_RULES:
        if any(marker in normalized for marker in markers):
            return category
    return "Прочее"


# ---------------------------------------------------------------------------
# Корзина продуктов на неделю
# ---------------------------------------------------------------------------

# Типичные сроки годности (в днях) по продуктовым категориям; уточнения —
# по ключевым словам в названии. Короткий срок → покупать чаще и меньше.
SHELF_LIFE_DAYS = {
    "Молочные продукты": 7,
    "Мясо и птица": 4,
    "Рыба и морепродукты": 3,
    "Овощи и фрукты": 10,
    "Хлеб и выпечка": 4,
    "Бакалея": 180,
    "Напитки": 90,
    "Сладости и снеки": 60,
}

SHELF_LIFE_OVERRIDES = (
    ("заморож", 120),
    ("замороз", 120),
    ("пельмен", 120),
    ("вареник", 120),
    ("морожен", 90),
    ("зелень", 3),
    ("картоф", 30),
    ("лук ", 30),
    (" лук", 30),
    ("морков", 30),
    ("капуст", 30),
    ("яйц", 25),
    ("консерв", 365),
    ("сыр ", 30),
)

# Позиция регулярная, если куплена минимум столько раз за окно корзины.
MIN_BASKET_PURCHASES = 3

# Продуктовые группы корзины: категории сворачиваются в группы, понятные
# для похода в магазин; категории вне групп попадают в «Прочее».
FOOD_GROUP_BY_CATEGORY = {
    "Овощи и фрукты": "Овощи и фрукты",
    "Молочные продукты": "Молочное",
    "Мясо и птица": "Мясо и рыба",
    "Рыба и морепродукты": "Мясо и рыба",
    "Хлеб и выпечка": "Хлебобулочное и бакалея",
    "Бакалея": "Хлебобулочное и бакалея",
}
FOOD_GROUP_OTHER = "Прочее"
FOOD_GROUP_ORDER = (
    "Овощи и фрукты",
    "Молочное",
    "Мясо и рыба",
    "Хлебобулочное и бакалея",
    FOOD_GROUP_OTHER,
)


def food_group(category: str) -> str:
    """Продуктовая группа корзины для категории товара."""
    return FOOD_GROUP_BY_CATEGORY.get(category, FOOD_GROUP_OTHER)

# Сезонные профили спроса: (название группы, маркеры подстроки в названии
# товара, множители по месяцам январь..декабрь). 1.0 — сезон спрос не меняет;
# меньше — зимой берут реже, больше — летом чаще. Маркеры применяются по
# первому совпадению, как SHELF_LIFE_OVERRIDES. Значения — типичные для
# российского климата оценки, а не данные о конкретной семье.
SEASONAL_PROFILES: tuple[tuple[str, tuple[str, ...], tuple[float, ...]], ...] = (
    (
        "Мороженое",
        ("морожен", "эскимо", "пломбир"),
        (0.35, 0.35, 0.45, 0.60, 0.90, 1.20, 1.50, 1.60, 1.50, 1.10, 0.90, 0.70),
    ),
    (
        "Прохладительные напитки",
        ("лимонад", "квас", "морс", "газир", "вода питьев"),
        (0.50, 0.50, 0.55, 0.75, 1.00, 1.20, 1.40, 1.45, 1.40, 1.10, 0.85, 0.60),
    ),
    (
        "Ягоды и летние фрукты",
        ("ягод", "клубник", "малик", "черник", "арбуз", "дын", "персик", "нектарин", "виноград"),
        (0.30, 0.30, 0.35, 0.50, 0.70, 1.10, 1.50, 1.60, 1.60, 1.20, 0.80, 0.50),
    ),
    (
        "Горячие напитки",
        ("чай", "кофе", "какао"),
        (1.30, 1.30, 1.25, 1.10, 1.00, 0.90, 0.80, 0.75, 0.80, 1.10, 1.15, 1.30),
    ),
    (
        "Мандарины и цитрусы",
        ("мандарин", "апельсин"),
        (1.30, 1.10, 0.90, 0.85, 0.85, 0.85, 0.85, 0.85, 0.90, 1.00, 1.30, 1.60),
    ),
)

@dataclass
class BasketEntry:
    name: str
    category: str
    weekly_qty: float
    weekly_sum: float
    shelf_days: int
    season_weight: float = 1.0
    # Шаг целой упаковки (см. purchase_step); None — плавающий вес.
    step: float | None = None
    purchase_count: int = 0
    window_days: float = 0.0

    @property
    def adjusted_qty(self) -> float:
        """Недельное количество с учётом сезонного множителя."""
        return self.weekly_qty * self.season_weight

    @property
    def adjusted_sum(self) -> float:
        """Недельная сумма с учётом сезонного множителя."""
        return self.weekly_sum * self.season_weight

    @property
    def cadence_days(self) -> float | None:
        """Фактический интервал между закупками по истории окна."""
        if self.purchase_count <= 0:
            return None

        return max(self.window_days / self.purchase_count, 1.0)

    @property
    def plan_qty(self) -> float:
        """Сколько брать за одну закупку: типовая разовая покупка с сезонной
        поправкой, округлённая вверх до целой упаковки (не бывает меньше
        одной продаваемой единицы)."""
        cadence = self.cadence_days or 7.0
        return round_up_to_step(self.weekly_qty * cadence / 7.0 * self.season_weight, self.step)

    @property
    def plan_qty_label(self) -> str:
        """«2» для штучных или «2×0.9» — сколько упаковок и какого размера."""
        if self.step and abs(self.step - 1.0) > 1e-9:
            return f"{round(self.plan_qty / self.step):d}×{format_qty(self.step)}"

        return format_qty(self.plan_qty)

    @property
    def plan_sum(self) -> float:
        """Примерная стоимость одной закупки по средней цене из чеков."""
        if self.weekly_qty <= 0:
            return 0.0

        return self.plan_qty * self.weekly_sum / self.weekly_qty


def seasonal_weight(name: str, month: int) -> float:
    """Сезонный множитель спроса товара в месяце 1-12; 1.0 — сезон нейтрален."""
    normalized = name.casefold()
    for _, markers, weights in SEASONAL_PROFILES:
        if any(marker in normalized for marker in markers):
            return weights[month - 1]
    return 1.0


def seasonal_month_overrides(month: int) -> list[tuple[str, float]]:
    """Сезонные группы месяца со сдвигом от нейтрали, по убыванию сдвига.

    Возвращаются и группы, которых нет в корзине: сводка показывает, что
    сейчас не в сезоне, ещё до похода в магазин.
    """
    rows = [(label, weights[month - 1]) for label, _, weights in SEASONAL_PROFILES]
    return sorted(rows, key=lambda row: abs(row[1] - 1.0), reverse=True)


def seasonal_mark(weight: float) -> str:
    """Компактная метка множителя для таблиц: ×1.6 или «—» для нейтральных."""
    return "—" if abs(weight - 1.0) < 1e-9 else f"×{weight:g}"


def shelf_life_days(name: str, category: str) -> int:
    normalized = name.casefold()
    for marker, days in SHELF_LIFE_OVERRIDES:
        if marker in normalized:
            return days

    return SHELF_LIFE_DAYS.get(category, 14)


# Точность количеств в чеках ФНС — 3 знака: до неё округляются разовые порции.
PORTION_PRECISION = 3
# Шаг упаковки меньше 0.05 не имеет смысла: это плавающий вес, а не фасовка.
MIN_PACKAGE_STEP = 0.05


def purchase_step(portions: Iterable[float]) -> float | None:
    """Шаг целой упаковки по истории разовых покупок позиции.

    Все порции целые — штучный товар (или пачки «1 кг», «5 кг»): шаг 1,
    меньше целого не продадут. Иначе шаг — наибольший общий делитель порций
    (0.9, 1.8, 2.7 → 0.9): кратные ему количества реально стоят на полке.
    Делитель мельче MIN_PACKAGE_STEP не находится — весовой товар с
    плавающим количеством, шага нет (None).
    """
    values = sorted({round(q, PORTION_PRECISION) for q in portions if q > 0})
    if not values:
        return None

    if all(value == int(value) for value in values):
        return 1.0

    scale = 10**PORTION_PRECISION
    step = math.gcd(*(round(value * scale) for value in values)) / scale
    if step >= MIN_PACKAGE_STEP:
        return step

    return None


def round_up_to_step(value: float, step: float | None) -> float:
    """Округление вверх до покупаемого количества: целых упаковок при
    известном шаге, до 0.1 — для весовых с плавающим количеством."""
    if value <= 0:
        return 0.0

    if step and step > 0:
        return math.ceil(value / step - 1e-9) * step

    return math.ceil(value * 10 - 1e-9) / 10


def cadence_label(days: float | None) -> str:
    """«раз в ~7 дн.» по фактической частоте закупок; «—» без истории."""
    return "—" if not days else f"раз в ~{round(days):d} дн."


def take_label(entry: BasketEntry, currency: str) -> str:
    """Сколько брать за одну закупку и сколько она стоит: «1 (~243 ₽)»,
    «2×0.9 (~437 ₽)». Стоимость — ориентир по средней цене из чеков,
    округлённый до целой валюты, чтобы не читалась как цена строки."""
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    cost = f"{round(entry.plan_sum):,}".replace(",", " ")
    return f"{entry.plan_qty_label} (~{cost} {symbol})"


def build_weekly_basket(
    report: PeriodReport,
    window_days: int,
    top: int = 15,
    month: int | None = None,
    private_categories: set[str] | None = None,
) -> dict[str, dict[str, list[BasketEntry]]]:
    """Корзина регулярных продуктов на неделю: {валюта: {группа: [записи]}}.

    Учитываются только продуктовые категории, купленные MIN_BASKET_PURCHASES+
    раз за окно; группировка — по продуктовым группам (см. food_group),
    срок годности остаётся колонкой. При заданном месяце (1-12) записи
    получают сезонный множитель спроса, а weekly_qty и weekly_sum остаются
    базовым средним за окно. Рекомендация «брать» — за одну закупку,
    с округлением вверх до целой упаковки (шаг выводится из разовых
    покупок, см. purchase_step). Позиции приватных категорий в корзину
    не попадают (единообразно с остальным отчётом).
    """
    weeks = max(window_days / 7.0, 1.0)
    baskets: dict[str, dict[str, list[BasketEntry]]] = {}
    for (currency, name), item in report.items.items():
        if item.total <= 0 or is_service_item(name):
            continue

        category = categorize_item(name)
        if category not in SHELF_LIFE_DAYS:
            continue

        if private_categories and category in private_categories:
            continue

        if len(item.purchase_receipts) < MIN_BASKET_PURCHASES:
            continue

        entry = BasketEntry(
            name=name,
            category=category,
            weekly_qty=item.quantity / weeks,
            weekly_sum=item.total / weeks,
            shelf_days=shelf_life_days(name, category),
            season_weight=seasonal_weight(name, month) if month is not None else 1.0,
            step=purchase_step(item.purchase_portions.values()),
            purchase_count=len(item.purchase_receipts),
            window_days=float(window_days),
        )
        groups = baskets.setdefault(currency, {})
        groups.setdefault(food_group(category), []).append(entry)

    for groups in baskets.values():
        for entries in groups.values():
            entries.sort(key=lambda entry: entry.weekly_sum, reverse=True)
            del entries[top:]

    return baskets


# Единая метка вместо названий магазинов, в чеках которых есть позиции
# приватных категорий: характер покупки не должен деанонимизироваться.
HIDDEN_STORE_LABEL = "(скрыто: приватные покупки)"


def masked_store_stats(
    report: PeriodReport,
    currency: str,
    hidden_stores: set[str],
) -> dict[str, tuple[int, float]]:
    """Статистика магазинов с маскированием: скрытые магазины сливаются
    в одну строку-заглушку, чеки и суммы при этом сохраняются."""
    merged: dict[str, tuple[int, float]] = {}
    for (item_currency, store), value in report.stores.items():
        if item_currency != currency or value.total <= 0:
            continue

        key = HIDDEN_STORE_LABEL if store in hidden_stores else store
        count, total = merged.get(key, (0, 0.0))
        merged[key] = (count + value.count, total + value.total)

    return merged


def masked_refund_stats(
    report: PeriodReport,
    currency: str,
    hidden_stores: set[str],
) -> dict[str, tuple[int, float]]:
    merged: dict[str, tuple[int, float]] = {}
    for (item_currency, store), value in report.refund_stores.items():
        if item_currency != currency or value.total <= 0:
            continue

        key = HIDDEN_STORE_LABEL if store in hidden_stores else store
        count, total = merged.get(key, (0, 0.0))
        merged[key] = (count + value.count, total + value.total)

    return merged


def only_private_receipts(item: ItemStats, private_receipts: set[str]) -> bool:
    """Позиция встречалась только в чеках с приватными покупками.

    Так из «Разбора Прочего» исчезают медикаменты с незнакомым названием:
    они не совпали ни с одним маркером словаря, но куплены в аптечном чеке.
    """
    receipts = item.purchase_receipts | item.refund_receipts
    return bool(receipts) and receipts <= private_receipts


def category_totals(item_totals: Mapping[str, float]) -> defaultdict[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for name, total in item_totals.items():
        if total > 0:
            totals[categorize_item(name)] += total
    return totals


def print_header(title: str) -> None:
    if FORMAT_STATE["md"]:
        if FORMAT_STATE["first_header"]:
            print(f"## {title}")
            FORMAT_STATE["first_header"] = False
        else:
            print(f"### {title}")
        return

    print(COLOR.header(title))
    print(COLOR.muted("-" * visible_len(title)))


def render_ai_output(text: str) -> str:
    rendered: list[str] = []
    for line in text.splitlines():
        heading = MARKDOWN_HEADING_RE.match(line)
        if heading:
            title = heading.group(1).strip()
            rendered.append(COLOR.header(title))
            rendered.append(COLOR.muted("-" * visible_len(title)))
            continue

        rendered.append(MARKDOWN_BOLD_RE.sub(r"\1", line))
    return "\n".join(rendered).strip()


def money_delta(current: float, previous: float, currency: str) -> str:
    delta = current - previous
    sign = "+" if delta > 0 else ""
    return f"{sign}{money(delta, currency)}"


def colored_expense_delta(current: float, previous: float, currency: str) -> str:
    delta = current - previous
    value = money_delta(current, previous, currency)
    if delta < 0:
        return COLOR.positive(value)
    if delta > 0:
        return COLOR.negative(value)
    return value


def colored_neutral_money_delta(current: float, previous: float, currency: str) -> str:
    delta = current - previous
    value = money_delta(current, previous, currency)
    if delta > 0:
        return COLOR.positive(value)
    if delta < 0:
        return COLOR.negative(value)
    return value


def count_delta(current: int, previous: int) -> str:
    delta = current - previous
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta}"


def insight_line(label: str, current: float, previous: float, currency: str) -> str:
    delta = current - previous
    delta_text = f"{money_delta(current, previous, currency)} ({percent_delta(current, previous)})"
    if delta < 0:
        delta_text = COLOR.positive(delta_text)
    elif delta > 0:
        delta_text = COLOR.negative(delta_text)
    return f"{label}: {money(current, currency)} против {money(previous, currency)} ({delta_text})"


def changed_rows(
    current_values: Mapping[str, float],
    previous_values: Mapping[str, float],
    limit: int,
) -> list[tuple[str, float, float]]:
    names = set(current_values) | set(previous_values)
    rows = [
        (name, current_values.get(name, 0.0), previous_values.get(name, 0.0))
        for name in names
    ]
    rows.sort(key=lambda row: abs(row[1] - row[2]), reverse=True)
    return rows[:limit]


def compact_money_delta(current: float, previous: float, currency: str) -> str:
    return f"{money(current, currency)} / было {money(previous, currency)} / изменение {money_delta(current, previous, currency)}"


def family_intro(family_context: str) -> str:
    """Вводная про семью для AI-промпта: FAMILY.md или фолбэк-формулировка."""
    if family_context:
        return (
            "Проанализируй расходы домашнего хозяйства.\n"
            "Контекст семьи (из локального файла пользователя FAMILY.md):\n"
            + family_context
        )

    return (
        "Проанализируй расходы семьи из 2 взрослых и 2 подростков.\n"
        "Семья обычно питается дома."
    )


def build_ai_prompt(
    *,
    currency_label: str,
    currency: str,
    days: int,
    current_start: datetime,
    current_end: datetime,
    previous_start: datetime,
    previous_end: datetime,
    stats: MutableStats,
    previous_stats: MutableStats,
    avg_day: float,
    previous_avg_day: float,
    previous_item_totals: Mapping[str, float],
    category_rows: list[tuple[str, float, float]],
    store_changes: list[tuple[str, float, float]],
    item_changes: list[tuple[str, float, float]],
    store_rows: list[tuple[str, int, float, float]],
    item_rows: list[tuple[str, float, float]],
    service_rows: list[tuple[str, float, float]],
    recurring_rows: list[tuple[str, int, float, float, float]],
    max_item_name_chars: int = _config.DEFAULT_MAX_ITEM_NAME_CHARS,
    family_context: str = "",
) -> str:
    def lines(title: str, rows: Iterable[str]) -> str:
        body = "\n".join(f"- {row}" for row in rows)
        return f"{title}:\n{body if body else '- нет данных'}"

    def short_item(name: str) -> str:
        # В промпт названия товаров уходят уже сокращёнными — как в таблицах.
        return truncate_cell(name, max_item_name_chars)

    store_change_lines = (
        f"{short_item(name)}: {compact_money_delta(current, previous, currency)}"
        for name, current, previous in store_changes
    )
    store_lines = (
        f"{short_item(name)}: чеков {count}, {compact_money_delta(total, previous_total, currency)}, доля {percent(total, stats.total)}"
        for name, count, total, previous_total in store_rows
    )
    item_change_lines = (
        f"{short_item(name)}: {compact_money_delta(current, previous, currency)}"
        for name, current, previous in item_changes
    )
    item_lines = (
        f"{short_item(name)}: количество {quantity:.3g}, {compact_money_delta(total, previous_total, currency)}, доля {percent(total, stats.total)}"
        for name, quantity, total in item_rows
        for previous_total in (previous_item_totals.get(name, 0.0),)
    )
    service_lines = (
        f"{short_item(name)}: количество {quantity:.3g}, {compact_money_delta(total, previous_total, currency)}"
        for name, quantity, total in service_rows
        for previous_total in (previous_item_totals.get(name, 0.0),)
    )
    recurring_lines = (
        f"{short_item(name)}: покупок {purchases}, количество {quantity:.3g}, сумма {compact_money_delta(total, previous_total, currency)}, средняя цена {money(avg_unit, currency)}"
        for name, purchases, quantity, total, avg_unit in recurring_rows
        for previous_total in (previous_item_totals.get(name, 0.0),)
    )
    category_lines = (
        f"{name}: {compact_money_delta(total, previous_total, currency)}, доля {percent(total, stats.total)}"
        for name, total, previous_total in category_rows
    )

    return f"""
Ты финансовый помощник. {family_intro(family_context)}
Отчет построен по чекам ФНС, уже учтены возвраты/отмены,
дубли закрытия предоплаты интернет-магазинов и разделение валют.

Нужно дать практичное заключение на русском языке. Не пересказывай все таблицы.
Используй только данные ниже, не выдумывай доходы, долги, цели и медицинские рекомендации.
Обязательно оцени, какие категории товаров занимают наибольшую долю расходов в процентах.
Для еды используй детальные категории: молочные продукты, мясо и птица, рыба,
овощи и фрукты, хлеб и выпечка, бакалея, напитки, сладости и снеки.
Формат ответа:
Каждая строка ответа должна быть не длиннее 120 символов, включая маркеры и нумерацию.
1. Краткий вывод в 3-5 пунктов.
2. Структура категорий: какие категории занимают самые большие доли и как это изменилось.
3. Что сильнее всего повлияло на расходы.
4. Повторяющиеся покупки и бытовые привычки.
5. Что проверить вручную.
6. Рекомендации на следующий месяц.

Валюта: {currency_label} ({currency})
Текущий период: {current_start:%Y-%m-%d %H:%M} - {current_end:%Y-%m-%d %H:%M} ({days} дней)
Период сравнения: {previous_start:%Y-%m-%d %H:%M} - {previous_end:%Y-%m-%d %H:%M}

Итоги:
- чистые расходы: {compact_money_delta(stats.total, previous_stats.total, currency)}
- расходы до возвратов: {compact_money_delta(stats.gross_total, previous_stats.gross_total, currency)}
- среднее в день: {compact_money_delta(avg_day, previous_avg_day, currency)}
- покупочные чеки: {stats.count} / было {previous_stats.count} / изменение {count_delta(stats.count, previous_stats.count)}
- возвраты и отмены: {money(stats.refund_total, currency)} / было {money(previous_stats.refund_total, currency)}
- закрытие уже учтенной предоплаты: {stats.ignored_count} чеков на {money(stats.ignored_total, currency)}

{lines("Категории расходов", category_lines)}

{lines("Главные изменения по магазинам", store_change_lines)}

{lines("Главные изменения по товарам", item_change_lines)}

{lines("Топ магазинов текущего периода", store_lines)}

{lines("Топ товаров текущего периода", item_lines)}

{lines("Сервисы и комиссии", service_lines)}

{lines("Повторяющиеся покупки", recurring_lines)}
""".strip()


def print_ai_summary(prompt: str, command: str, timeout: int) -> None:
    argv = shlex.split(command)
    if not argv:
        print(COLOR.warning(f"Пустая команда AI CLI: {command!r}"))
        return

    executable = shutil.which(argv[0])
    if executable is None:
        print(COLOR.warning(f"AI CLI не найден: {argv[0]}"))
        return

    print_header("AI-выводы и рекомендации")
    try:
        result = subprocess.run(
            [executable, *argv[1:]],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(COLOR.warning(f"AI CLI не ответил за {timeout} секунд"))
        return

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "неизвестная ошибка"
        print(COLOR.warning(f"AI CLI завершился с ошибкой: {message}"))
        return

    print(render_ai_output(result.stdout))
    print()


def build_period_report(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    store_rules: Mapping[str, str],
    receipt_rules: Mapping[str, str],
    private_categories: set[str] | None = None,
) -> PeriodReport:
    receipts = conn.execute(
        """
        select
            fd.receipt_key,
            fd.date_time,
            fd.operation_type,
            fd.total_sum,
            fd.prepaid_sum,
            fd.retail_place,
            fd.retail_place_address,
            fd.user as fd_user,
            fd.user_inn,
            r.kkt_owner,
            coalesce(nullif(b.name, ''), nullif(r.kkt_owner, ''), nullif(fd.user, ''), 'Unknown') as store
        from fiscal_data fd
        join receipts r on r.key = fd.receipt_key
        left join brands b on b.id = r.brand_id
        where datetime(fd.date_time) >= datetime(?) and datetime(fd.date_time) <= datetime(?)
        """,
        (start.isoformat(sep=" "), end.isoformat(sep=" ")),
    ).fetchall()

    receipt_currencies: dict[str, str] = {}
    receipt_store: dict[str, str] = {}
    private_receipts: set[str] = set()
    stats_by_currency: defaultdict[str, MutableStats] = defaultdict(MutableStats)
    stores: defaultdict[tuple[str, str], MutableStats] = defaultdict(MutableStats)
    days_total: defaultdict[tuple[str, str], MutableStats] = defaultdict(MutableStats)
    refund_stores: defaultdict[tuple[str, str], MutableStats] = defaultdict(MutableStats)

    for row in receipts:
        currency = detect_currency(row, store_rules, receipt_rules)
        receipt_currencies[row["receipt_key"]] = currency
        receipt_store[row["receipt_key"]] = store_name(row)
        stats = stats_by_currency[currency]
        unsigned_spend = effective_spend(row)
        spend = signed_effective_spend(row)
        if unsigned_spend > 0 and operation_sign(row) > 0:
            stats.count += 1
            stats.total += spend
            stats.gross_total += spend
            stores[(currency, store_name(row))].count += 1
            stores[(currency, store_name(row))].total += spend
            day = str(row["date_time"])[:10]
            days_total[(currency, day)].count += 1
            days_total[(currency, day)].total += spend
        elif unsigned_spend > 0 and operation_sign(row) < 0:
            stats.refund_count += 1
            stats.refund_total += unsigned_spend
            stats.total += spend
            stores[(currency, store_name(row))].total += spend
            refund_stores[(currency, store_name(row))].count += 1
            refund_stores[(currency, store_name(row))].total += unsigned_spend
            day = str(row["date_time"])[:10]
            days_total[(currency, day)].total += spend
        elif operation_sign(row) > 0 and float(row["prepaid_sum"] or 0) > 0:
            stats.ignored_count += 1
            stats.ignored_total += float(row["total_sum"] or 0)

    items: defaultdict[tuple[str, str], ItemStats] = defaultdict(ItemStats)
    item_rows = conn.execute(
        """
        select
            fd.receipt_key,
            fd.operation_type,
            fd.total_sum,
            fd.prepaid_sum,
            item.name,
            item.quantity,
            item.sum
        from fiscal_data_items item
        join fiscal_data fd on fd.receipt_key = item.receipt_key
        where datetime(fd.date_time) >= datetime(?) and datetime(fd.date_time) <= datetime(?)
        """,
        (start.isoformat(sep=" "), end.isoformat(sep=" ")),
    ).fetchall()

    for row in item_rows:
        if effective_spend(row) <= 0:
            continue

        if private_categories and categorize_item(row["name"]) in private_categories:
            private_receipts.add(row["receipt_key"])

        currency = receipt_currencies.get(row["receipt_key"], "RUB")
        item = items[(currency, row["name"])]
        sign = operation_sign(row)
        item.quantity += sign * float(row["quantity"] or 0)
        item.total += sign * float(row["sum"] or 0)
        if sign > 0:
            item.purchase_receipts.add(row["receipt_key"])
            receipt_key = row["receipt_key"]
            item.purchase_portions[receipt_key] = (
                item.purchase_portions.get(receipt_key, 0.0) + float(row["quantity"] or 0)
            )
        else:
            item.refund_receipts.add(row["receipt_key"])

    return PeriodReport(
        start=start,
        end=end,
        stats_by_currency=stats_by_currency,
        stores=stores,
        days_total=days_total,
        refund_stores=refund_stores,
        items=items,
        private_receipts=private_receipts,
        receipt_store=receipt_store,
    )


def run_report(
    db_path: Path,
    days: int,
    as_of: datetime | None,
    top: int,
    store_currency_rules: Iterable[tuple[str, str]],
    receipt_currency_rules: Iterable[tuple[str, str]],
    color: str,
    ai_summary: bool,
    ai_command: str,
    ai_timeout: int,
    max_item_name_chars: int = _config.DEFAULT_MAX_ITEM_NAME_CHARS,
    private_categories: list[str] | None = None,
    report_format: str = "text",
    basket_days: int = 180,
) -> None:
    if days < 1:
        raise SystemExit("--days must be at least 1")
    if top < 1:
        raise SystemExit("--top must be at least 1")
    FORMAT_STATE["md"] = report_format == "md"
    FORMAT_STATE["first_header"] = True
    COLOR.enabled = color == "always" or (color == "auto" and sys.stdout.isatty())
    if FORMAT_STATE["md"]:
        COLOR.enabled = False

    if not db_path.exists():
        raise SystemExit(
            f"База данных не найдена: {db_path}.\n"
            "Сначала соберите чеки: make parse (подробнее — docs/getting-started.md)."
        )

    # База открывается только для чтения: опечатка в --db не должна
    # создавать пустой файл, а отчёт — случайно не менять данные.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    require_tables(conn)

    latest = latest_receipt_datetime(conn)
    end = as_of or latest
    if end.tzinfo is None and latest.tzinfo is not None:
        # --as-of без зоны: считаем стенными часами в той же зоне,
        # что и данные (иначе граница периода уедет на часы).
        end = end.replace(tzinfo=latest.tzinfo)

    start = end - timedelta(days=days)
    previous_end = start
    previous_start = previous_end - timedelta(days=days)
    store_rules = build_rule_map(store_currency_rules)
    receipt_rules = build_rule_map(receipt_currency_rules)

    # Товары приватных категорий (по умолчанию аптечка/врачи/анализы) не
    # выводятся построчно: их суммы видны только на уровне категории.
    # Чеки с такими позициями дополнительно скрывают «Прочее» и магазины.
    private = set(private_categories if private_categories is not None else _config.DEFAULT_PRIVATE_CATEGORIES)

    current = build_period_report(conn, start, end, store_rules, receipt_rules, private)
    previous = build_period_report(conn, previous_start, previous_end, store_rules, receipt_rules, private)

    print_header("Отчет по покупкам")
    if not FORMAT_STATE["md"]:
        # Локальный путь машины не нужен в md-версии для чата.
        print(f"{COLOR.muted('База данных:')} {db_path}")
    print(f"{COLOR.muted('Текущий период:')} {start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M}")
    print(f"{COLOR.muted('Период сравнения:')} {previous_start:%Y-%m-%d %H:%M} - {previous_end:%Y-%m-%d %H:%M}")
    if private:
        print(
            COLOR.muted(
                f"Приватность: позиции категорий {', '.join(sorted(private))} построчно не выводятся "
                f"(суммы учтены в категориях); магазины таких чеков показаны как «{HIDDEN_STORE_LABEL}»."
            )
        )
    print()

    currencies = sorted(set(current.stats_by_currency) | set(previous.stats_by_currency))

    def short_item(name: str) -> str:
        # Только отображение (товары и магазины): агрегация и ключи
        # previous.items/previous.stores — по полным именам.
        return truncate_cell(name, max_item_name_chars)

    def hidden_item(name: str) -> bool:
        return categorize_item(name) in private

    # Маскирование магазинов: объединение приватных за оба периода, чтобы
    # название не утекало через колонку «Было» предыдущего периода.
    hidden_stores = current.private_stores() | previous.private_stores()

    for currency in currencies:
        stats = current.stats_by_currency[currency]
        previous_stats = previous.stats_by_currency[currency]
        currency_label = CURRENCY_NAMES.get(currency, currency)
        avg_receipt = stats.total / stats.count if stats.count else 0
        avg_day = stats.total / days
        previous_avg_receipt = (
            previous_stats.total / previous_stats.count if previous_stats.count else 0
        )
        previous_avg_day = previous_stats.total / days
        current_stores_masked = masked_store_stats(current, currency, hidden_stores)
        previous_stores_masked = masked_store_stats(previous, currency, hidden_stores)
        current_store_totals = {
            store: total for store, (_, total) in current_stores_masked.items()
        }
        previous_store_totals = {
            store: total for store, (_, total) in previous_stores_masked.items()
        }
        current_item_totals = {
            name: value.total
            for (item_currency, name), value in current.items.items()
            if item_currency == currency and value.total > 0
        }
        previous_item_totals = {
            name: value.total
            for (item_currency, name), value in previous.items.items()
            if item_currency == currency and value.total > 0
        }
        current_category_totals = category_totals(current_item_totals)
        previous_category_totals = category_totals(previous_item_totals)
        category_rows = sorted(
            (
                (
                    category,
                    current_category_totals.get(category, 0.0),
                    previous_category_totals.get(category, 0.0),
                )
                for category in set(current_category_totals) | set(previous_category_totals)
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        store_changes = changed_rows(current_store_totals, previous_store_totals, top)

        def public_item_totals(report: PeriodReport) -> dict[str, float]:
            # Публичные позиции для построчных списков: без приватных
            # категорий и без позиций, встречавшихся только в приватных чеках.
            return {
                name: item.total
                for (item_currency, name), item in report.items.items()
                if item_currency == currency
                and item.total > 0
                and not only_private_receipts(item, report.private_receipts)
            }

        current_public_items = public_item_totals(current)
        previous_public_items = public_item_totals(previous)
        item_changes = changed_rows(
            {
                name: total
                for name, total in current_public_items.items()
                if not is_service_item(name) and not hidden_item(name)
            },
            {
                name: total
                for name, total in previous_public_items.items()
                if not is_service_item(name) and not hidden_item(name)
            },
            top,
        )

        print_header(f"Короткий вывод ({currency_label})")
        print(insight_line("Чистые расходы", stats.total, previous_stats.total, currency))
        print(insight_line("Среднее в день", avg_day, previous_avg_day, currency))
        # Возврат уменьшает расходы — везде показывается со знаком минус.
        print(
            insight_line(
                "Возвраты и отмены",
                -stats.refund_total,
                -previous_stats.refund_total,
                currency,
            )
        )
        print()

        print_header(f"Сравнение ({currency_label})")
        print_table(
            ("Показатель", "Текущий период", "Предыдущий период", "Изменение"),
            (
                (
                    "Чистые расходы",
                    money(stats.total, currency),
                    money(previous_stats.total, currency),
                    colored_expense_delta(stats.total, previous_stats.total, currency),
                ),
                (
                    "Расходы до возвратов",
                    money(stats.gross_total, currency),
                    money(previous_stats.gross_total, currency),
                    colored_expense_delta(
                        stats.gross_total, previous_stats.gross_total, currency
                    ),
                ),
                (
                    "Покупочные чеки",
                    stats.count,
                    previous_stats.count,
                    count_delta(stats.count, previous_stats.count),
                ),
                (
                    "Средний чек",
                    money(avg_receipt, currency),
                    money(previous_avg_receipt, currency),
                    colored_expense_delta(avg_receipt, previous_avg_receipt, currency),
                ),
                (
                    "Среднее в день",
                    money(avg_day, currency),
                    money(previous_avg_day, currency),
                    colored_expense_delta(avg_day, previous_avg_day, currency),
                ),
                (
                    "Возвраты и отмены",
                    signed_refund(stats.refund_total, currency),
                    signed_refund(previous_stats.refund_total, currency),
                    colored_expense_delta(
                        -stats.refund_total, -previous_stats.refund_total, currency
                    ),
                ),
            ),
        )
        print()

        print_header(f"Категории ({currency_label})")
        print_table(
            ("Категория", "Текущий период", "Предыдущий период", "Изменение", "Доля"),
            (
                (
                    category,
                    money(total, currency),
                    money(previous_total, currency),
                    colored_expense_delta(total, previous_total, currency),
                    share_bar(total, stats.total),
                )
                for category, total, previous_total in category_rows
                if total > 0 or previous_total > 0
            ),
        )
        # Сходимость объясняем на месте, а не только в свёрнутых правилах.
        print(
            COLOR.muted(
                "Категории считаются по товарным позициям чеков и могут отличаться "
                "от итога чеков (возвраты, предоплата, сервисные строки)."
            )
        )
        print()

        other_total = current_category_totals.get("Прочее", 0.0)
        previous_other_total = previous_category_totals.get("Прочее", 0.0)
        if other_total > 0 or previous_other_total > 0:
            print_header(f"Разбор Прочего ({currency_label})")
            print(
                COLOR.muted(
                    f"Позиции, не попавшие ни в одну категорию: {money(other_total, currency)} "
                    f"({percent(other_total, stats.total)} чистых расходов; "
                    f"было {money(previous_other_total, currency)})"
                )
            )
            other_rows = sorted(
                (
                    (name, value.quantity, value.total)
                    for (item_currency, name), value in current.items.items()
                    if item_currency == currency
                    and value.total > 0
                    and categorize_item(name) == "Прочее"
                    and not only_private_receipts(value, current.private_receipts)
                ),
                key=lambda row: row[2],
                reverse=True,
            )
            top_other_rows = other_rows[:top]
            rest_total = sum(total for _, _, total in other_rows[top:])
            rest_count = max(len(other_rows) - len(top_other_rows), 0)
            print_table(
                ("Позиция", "Кол-во", "Текущий период", "Доля Прочего"),
                (
                    (
                        short_item(name) if name.strip() else "(без названия)",
                        f"{quantity:.3g}",
                        money(total, currency),
                        share_bar(total, other_total),
                    )
                    for name, quantity, total in top_other_rows
                ),
            )
            if rest_count:
                print(
                    COLOR.muted(
                        f"и ещё {rest_count} позиций на {money(rest_total, currency)} "
                        f"({percent(rest_total, other_total)} Прочего)"
                    )
                )
            print()

        print_header(f"Главные изменения ({currency_label})")
        # Таблицу изменений по магазинам не дублируем: топ магазинов ниже
        # показывает те же суммы; store_changes уходят только в AI-промпт.
        print(f"Топ-{top} изменений по товарам")
        print_table(
            ("Товар", "Текущий период", "Предыдущий период", "Изменение"),
            (
                (
                    short_item(item),
                    money(current_total, currency),
                    money(previous_total, currency),
                    colored_expense_delta(current_total, previous_total, currency),
                )
                for item, current_total, previous_total in item_changes
            ),
        )
        print()

        print_header(f"Итоги ({currency_label})")
        print_table(
            ("Показатель", "Значение"),
            (
                ("Чистые расходы", money(stats.total, currency)),
                ("Расходы до возвратов", money(stats.gross_total, currency)),
                ("Покупочные чеки", stats.count),
                ("Средний чек", money(avg_receipt, currency)),
                ("Среднее в день", money(avg_day, currency)),
            ),
        )
        print()

        print_header(f"Корректировки ({currency_label})")
        print_table(
            ("Корректировка", "Чеки", "Сумма"),
            (
                (
                    "Возвраты и отмены",
                    stats.refund_count,
                    signed_refund(stats.refund_total, currency),
                ),
                (
                    "Закрытие уже учтенной предоплаты",
                    stats.ignored_count,
                    COLOR.warning(money(stats.ignored_total, currency)),
                ),
            ),
        )
        print()

        refund_rows = sorted(
            (
                (store, count, total)
                for store, (count, total) in masked_refund_stats(
                    current, currency, hidden_stores
                ).items()
            ),
            key=lambda row: row[2],
            reverse=True,
        )[:top]
        if refund_rows:
            print(COLOR.warning(f"Топ-{top} магазинов по возвратам и отменам"))
            print_table(
                ("Магазин", "Чеки", "Сумма"),
                (
                    (short_item(store), count, signed_refund(total, currency))
                    for store, count, total in refund_rows
                ),
            )
            print()

        print_header(f"Основная разбивка ({currency_label})")

        print(f"Топ-{top} магазинов")
        store_rows = sorted(
            (
                (store, count, total, previous_store_totals.get(store, 0.0))
                for store, (count, total) in current_stores_masked.items()
            ),
            key=lambda row: row[2],
            reverse=True,
        )[:top]
        print_table(
            ("Магазин", "Чеки", "Текущий период", "Предыдущий период", "Изменение", "Доля"),
            (
                (
                    short_item(store),
                    count,
                    money(total, currency),
                    money(previous_total, currency),
                    colored_expense_delta(total, previous_total, currency),
                    share_bar(total, stats.total),
                )
                for store, count, total, previous_total in store_rows
            ),
        )
        print()

        print_header(f"Товары ({currency_label})")

        print(f"Топ-{top} товаров")
        rows = sorted(
            (
                (name, value.quantity, value.total)
                for (item_currency, name), value in current.items.items()
                if item_currency == currency
                and value.total > 0
                and not is_service_item(name)
                and not hidden_item(name)
                and not only_private_receipts(value, current.private_receipts)
            ),
            key=lambda row: row[2],
            reverse=True,
        )[:top]
        print_table(
            ("Товар", "Кол-во", "Текущий период", "Предыдущий период", "Изменение", "Доля"),
            (
                (
                    short_item(name),
                    f"{quantity:.3g}",
                    money(total, currency),
                    money(previous.items[(currency, name)].total, currency),
                    colored_expense_delta(
                        total, previous.items[(currency, name)].total, currency
                    ),
                    share_bar(total, stats.total),
                )
                for name, quantity, total in rows
            ),
        )
        print()

        print(f"Топ-{top} сервисов и комиссий")
        service_rows = sorted(
            (
                (name, value.quantity, value.total)
                for (item_currency, name), value in current.items.items()
                if item_currency == currency
                and value.total > 0
                and is_service_item(name)
                and not hidden_item(name)
                and not only_private_receipts(value, current.private_receipts)
            ),
            key=lambda row: row[2],
            reverse=True,
        )[:top]
        print_table(
            ("Строка", "Кол-во", "Текущий период", "Предыдущий период", "Изменение", "Доля"),
            (
                (
                    short_item(name),
                    f"{quantity:.3g}",
                    money(total, currency),
                    money(previous.items[(currency, name)].total, currency),
                    colored_expense_delta(
                        total, previous.items[(currency, name)].total, currency
                    ),
                    share_bar(total, stats.total),
                )
                for name, quantity, total in service_rows
            ),
        )
        print()

        print(f"Топ-{top} повторяющихся покупок")
        recurring_rows = sorted(
            (
                (
                    name,
                    len(value.purchase_receipts),
                    value.quantity,
                    value.total,
                    value.total / value.quantity if value.quantity else 0,
                )
                for (item_currency, name), value in current.items.items()
                if item_currency == currency
                and value.total > 0
                and value.quantity > 1
                and len(value.purchase_receipts) > 1
                and not is_service_item(name)
                and not hidden_item(name)
                and not only_private_receipts(value, current.private_receipts)
            ),
            key=lambda row: (row[2], row[3]),
            reverse=True,
        )[:top]
        print_table(
            (
                "Товар",
                "Покупки",
                "Кол-во",
                "Текущий период",
                "Предыдущий период",
                "Изменение",
                "Средняя цена",
            ),
            (
                (
                    short_item(name),
                    purchases,
                    f"{quantity:.3g}",
                    money(total, currency),
                    money(previous.items[(currency, name)].total, currency),
                    colored_expense_delta(
                        total, previous.items[(currency, name)].total, currency
                    ),
                    money(avg_unit, currency),
                )
                for name, purchases, quantity, total, avg_unit in recurring_rows
            ),
        )
        print()

        if ai_summary:
            prompt = build_ai_prompt(
                currency_label=currency_label,
                currency=currency,
                days=days,
                current_start=start,
                current_end=end,
                previous_start=previous_start,
                previous_end=previous_end,
                stats=stats,
                previous_stats=previous_stats,
                avg_day=avg_day,
                previous_avg_day=previous_avg_day,
                previous_item_totals=previous_item_totals,
                category_rows=category_rows,
                store_changes=store_changes,
                item_changes=item_changes,
                store_rows=store_rows,
                item_rows=rows,
                service_rows=service_rows,
                recurring_rows=recurring_rows,
                max_item_name_chars=max_item_name_chars,
                family_context=_config.load_family_context(),
            )
            print_ai_summary(prompt, ai_command, ai_timeout)

    # Корзина продуктов на неделю: по регулярным покупкам за отдельное
    # окно (по умолчанию 6 месяцев), частота — по срокам годности, объём —
    # с сезонной поправкой по месяцу конца периода. Окно клампится по
    # фактической глубине истории, иначе короткая база размазывала
    # «ориентир на неделю» до бессмысленных значений.
    if basket_days >= 7:
        earliest = in_reference_zone(earliest_receipt_datetime(conn), end)
        history_days = (end - earliest).days + 1
        window_days = min(basket_days, max(history_days, 7))
        basket_report = build_period_report(
            conn, end - timedelta(days=window_days), end, store_rules, receipt_rules, private
        )
        baskets = build_weekly_basket(
            basket_report, window_days, top, month=end.month, private_categories=private
        )
        for currency, buckets in baskets.items():
            currency_label = CURRENCY_NAMES.get(currency, currency)
            weekly_total = sum(
                entry.adjusted_sum for entries in buckets.values() for entry in entries
            )
            seasonal_active = [
                (label, weight)
                for label, weight in seasonal_month_overrides(end.month)
                if abs(weight - 1.0) > 1e-9
            ]
            print_header(f"Корзина продуктов на неделю ({currency_label})")
            window_note = f"за последние {window_days} дней"
            if window_days < basket_days:
                window_note += (
                    f" — история чеков короче окна {basket_days} дней,"
                    " среднее может быть нестабильным"
                )
            print(
                COLOR.muted(
                    f"Регулярные покупки {window_note} "
                    f"({MIN_BASKET_PURCHASES}+ чеков на позицию), сгруппированы по "
                    "продуктовым группам; «Как часто» — фактический интервал между "
                    "закупками, «Брать» — сколько взять за одну закупку с округлением "
                    "вверх до целой упаковки (шаг — типовая разовая покупка по истории "
                    "чеков), в скобках — примерная стоимость закупки по средней цене; "
                    "«~Сумма/нед» — средний расход."
                )
            )
            if seasonal_active:
                overrides = ", ".join(
                    f"{label} {seasonal_mark(weight)}" for label, weight in seasonal_active
                )
                print(
                    COLOR.muted(
                        f"Сезонность ({MONTHS_RU[end.month].lower()}): {overrides}. "
                        "Множитель корректирует количество и сумму позиции."
                    )
                )
            for group in FOOD_GROUP_ORDER:
                entries = buckets.get(group)
                if not entries:
                    continue

                if FORMAT_STATE["md"]:
                    print(f"**Позиции ({len(entries)}) · {group}**")
                else:
                    print(COLOR.header(f"Позиции ({len(entries)}) · {group}"))
                print_table(
                    ("Продукт", "Как часто", "Брать", "~Сумма/нед", "Срок годности", "Сезон"),
                    (
                        (
                            short_item(entry.name),
                            cadence_label(entry.cadence_days),
                            take_label(entry, currency),
                            money(entry.adjusted_sum, currency),
                            f"~{entry.shelf_days} дн.",
                            seasonal_mark(entry.season_weight),
                        )
                        for entry in entries
                    ),
                )
                if not FORMAT_STATE["md"]:
                    print()

            print(f"Ориентир трат в неделю по корзине: {money(weekly_total, currency)}.")
            print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Построить отчет по покупкам из SQLite-базы LKDR."
    )
    parser.add_argument("--db", default=DEFAULT_DB, type=Path, help="Путь к lkdr.db")
    parser.add_argument(
        "--format",
        choices=("text", "md"),
        default="text",
        help="Формат вывода: text — терминал с рамками; md — markdown (GFM-таблицы, без цвета), например для чата AI-агента",
    )
    parser.add_argument(
        "--days",
        default=DEFAULT_DAYS,
        type=int,
        help="Длина текущего периода в днях, предыдущий период будет такой же длины",
    )
    parser.add_argument(
        "--as-of",
        type=parse_datetime,
        help="Дата и время конца отчета в ISO-формате, по умолчанию самый свежий чек",
    )
    parser.add_argument("--top", default=10, type=int, help="Количество строк в топах")
    parser.add_argument(
        "--basket-days",
        default=180,
        type=int,
        help="Окно корзины продуктов на неделю, дней (3-6 месяцев; 0 отключает секцию)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Режим цветного вывода: auto, always или never",
    )
    parser.add_argument(
        "--currency-store",
        action="append",
        default=[],
        type=parse_currency_rule,
        metavar="STORE=CURRENCY",
        help="Принудительно указать валюту для точного названия магазина, например 'Kaspi.kz=KZT'",
    )
    parser.add_argument(
        "--currency-receipt",
        action="append",
        default=[],
        type=parse_currency_rule,
        metavar="RECEIPT_KEY=CURRENCY",
        help="Принудительно указать валюту для конкретного receipt_key",
    )
    parser.add_argument(
        "--ai-summary",
        action="store_true",
        help="Добавить AI-выводы и рекомендации через AI CLI",
    )
    parser.add_argument(
        "--ai-command",
        default=None,
        help="Команда AI CLI с аргументами (промпт — на stdin); по умолчанию ai.command из config.json, иначе codex",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="config.json с настройками отчётов (ai.command)",
    )
    parser.add_argument(
        "--ai-timeout",
        default=180,
        type=int,
        help="Сколько секунд ждать ответ Codex CLI, по умолчанию 180",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.ai_command = args.ai_command or _config.load_ai_command(args.config)
    try:
        max_item_name_chars = _config.load_max_item_name_chars(args.config)
        private_categories = _config.load_private_categories(args.config)
    except ValueError as error:
        raise SystemExit(f"{args.config}: {error}")
    run_report(
        args.db,
        args.days,
        args.as_of,
        args.top,
        args.currency_store,
        args.currency_receipt,
        args.color,
        args.ai_summary,
        args.ai_command,
        args.ai_timeout,
        max_item_name_chars,
        private_categories,
        args.format,
        args.basket_days,
    )


if __name__ == "__main__":
    main()
