"""Статические справочники платформы (фаза 21).

Единый источник для сегментов и MCC-категорий: раньше эти данные были
захардкожены на фронте (``mockData.ts``) и дублировались в трёх местах.
Теперь фронт читает их из ``GET /reference/*``.

Имена сегментов и порядок корзин — витринные (децильная раскладка живёт в
``app.segments.SEGMENT_BUCKETS``). MCC-иконки заданы **именами** (Lucide),
не emoji: фронт мапит имя на SVG-компонент (фаза 27), стенд остаётся
единым по стилю иконографики.
"""
from __future__ import annotations

# Русские витринные имена сегментных корзин (ключи — как в SEGMENT_BUCKETS).
SEGMENT_NAMES: dict[str, str] = {
    "premium":  "Премиум",
    "mass":     "Массовый",
    "young":    "Молодежь",
    "senior":   "Средний класс",
    "business": "Бизнес",
}

# Справочник MCC: code → (человекочитаемое имя, имя Lucide-иконки).
# Набор соответствует контракту design_handoff (API.md) — 12 категорий.
MCC_CATEGORIES: list[dict[str, str]] = [
    {"code": "5411", "name": "Супермаркеты",        "icon": "shopping-cart"},
    {"code": "5912", "name": "Аптеки",              "icon": "pill"},
    {"code": "5541", "name": "АЗС",                 "icon": "fuel"},
    {"code": "5812", "name": "Рестораны",           "icon": "utensils"},
    {"code": "5999", "name": "Прочая розница",      "icon": "store"},
    {"code": "7011", "name": "Отели",               "icon": "hotel"},
    {"code": "4111", "name": "Транспорт",           "icon": "bus"},
    {"code": "5045", "name": "Электроника",         "icon": "laptop"},
    {"code": "5600", "name": "Одежда",              "icon": "shirt"},
    {"code": "7832", "name": "Кинотеатры",          "icon": "clapperboard"},
    {"code": "5251", "name": "DIY / Строительство", "icon": "hammer"},
    {"code": "5122", "name": "Косметика",           "icon": "sparkles"},
]
