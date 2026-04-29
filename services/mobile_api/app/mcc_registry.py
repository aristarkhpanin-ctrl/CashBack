"""MCC → category metadata for the mobile UI.

Provides a Russian-language ``category_name`` and a stable ``icon`` slug
that the mobile client expands into a CDN URL via the
``cdn_base_url`` from settings.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MccInfo:
    name: str
    icon: str
    terms_summary: str


# Hand-picked subset covering the common merchant categories used by the
# TX simulator + RFM features. Unknown codes fall back to a generic entry.
MCC_CATALOG: dict[str, MccInfo] = {
    "5411": MccInfo("Продукты",          "groceries",  "Кэшбэк начисляется на покупки в супермаркетах."),
    "5499": MccInfo("Бакалея",           "groceries",  "Покупки в продуктовых лавках и онлайн-магазинах."),
    "5812": MccInfo("Рестораны",         "restaurant", "Кэшбэк на оплату счёта в ресторанах и кафе."),
    "5814": MccInfo("Фастфуд",           "fastfood",   "Покупки в сетях быстрого питания."),
    "5541": MccInfo("АЗС",               "fuel",       "Заправка на автозаправочных станциях."),
    "5912": MccInfo("Аптеки",            "pharmacy",   "Покупка лекарств в аптечных сетях."),
    "5311": MccInfo("Универмаги",        "department", "Покупки в крупных универмагах."),
    "5651": MccInfo("Одежда",            "apparel",    "Магазины одежды для всей семьи."),
    "5732": MccInfo("Электроника",       "electronics","Цифровая техника и электроника."),
    "5942": MccInfo("Книги",             "book",       "Книги и канцелярские товары."),
    "5921": MccInfo("Алкоголь",          "alcohol",    "Только при наличии согласия на категорию."),
    "5462": MccInfo("Пекарни",           "bakery",     "Хлеб, выпечка и десерты."),
    "4111": MccInfo("Транспорт",         "transit",    "Городской общественный транспорт."),
    "4121": MccInfo("Такси",             "taxi",       "Поездки на такси через приложения."),
    "4131": MccInfo("Автобусы",          "bus",        "Междугородние автобусные перевозки."),
    "4814": MccInfo("Связь",             "telecom",    "Услуги мобильной и стационарной связи."),
    "4829": MccInfo("Денежные переводы", "transfer",   "Переводы средств между картами."),
    "4900": MccInfo("ЖКХ",               "utilities",  "Оплата коммунальных услуг."),
    "7832": MccInfo("Кино",              "cinema",     "Билеты в кинотеатры."),
    "7011": MccInfo("Отели",             "hotel",      "Бронирование гостиниц."),
}

DEFAULT_INFO = MccInfo(
    "Покупки",
    "default",
    "Кэшбэк действует на товары и услуги указанной категории.",
)

assert all(len(info.terms_summary) <= 140 for info in MCC_CATALOG.values()), \
    "terms_summary must be ≤140 chars for the mobile UI"
assert len(DEFAULT_INFO.terms_summary) <= 140


def lookup(mcc_code: str) -> MccInfo:
    return MCC_CATALOG.get(str(mcc_code).strip(), DEFAULT_INFO)


def icon_url(cdn_base: str, mcc_code: str) -> str:
    return f"{cdn_base.rstrip('/')}/{lookup(mcc_code).icon}.png"
