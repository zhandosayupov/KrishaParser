"""Разбор страницы списка krisha.kz в обычные словари Python.

Числа (цена, площадь, этаж, год) достаём кодом из разметки страницы,
а не через ИИ: так они не искажаются.
"""
import json
import re

from bs4 import BeautifulSoup


class BlockedError(Exception):
    """Сайт показал проверку на бота вместо страницы."""


def is_blocked(html, status_code=200):
    return status_code in (403, 429) or "/.safeline/" in html[:5000]


def _num(text):
    """'54 112 000' -> 54112000; '60.8' -> 60.8; нет числа -> None."""
    if text is None:
        return None
    m = re.search(r"\d[\d\s ]*(?:[.,]\d+)?", text)
    if not m:
        return None
    s = re.sub(r"[\s ]", "", m.group()).replace(",", ".")
    return float(s) if "." in s else int(s)


def page_data(html):
    """Служебные данные страницы (window.data): общее число объявлений и т.п."""
    start = html.find('id="jsdata"')
    i = html.find("window.data = ", start)
    if start < 0 or i < 0:
        return {}
    try:
        return json.JSONDecoder().raw_decode(html, i + len("window.data = "))[0]
    except json.JSONDecodeError:
        return {}


def total_count(html):
    try:
        return int(page_data(html)["search"]["nbTotal"])
    except (KeyError, TypeError, ValueError):
        return None


# Короткие характеристики в начале текста объявления: "монолитный дом, 2023 г.п., ...".
# Каждая запись: (поле, шаблон). Что не подошло — считается началом описания.
_PREVIEW_RULES = [
    ("complex",    re.compile(r"^жил\. комплекс (.+)$")),
    ("house_type", re.compile(r"^(монолитный|кирпичный|панельный|каркасно-камышитовый|иное)(?: дом)?$")),
    ("floors",     re.compile(r"^(\d+) этаж(?:ей|а)?$")),
    ("year",       re.compile(r"^(\d{4}) г\.п\.$")),
    ("condition",  re.compile(r"^состояние: (.+)$")),
    ("ceiling",    re.compile(r"^потолки ([\d.]+)\s*м\.?$")),
    ("bathroom",   re.compile(r"^санузел (.+)$")),
    ("furniture",  re.compile(r"^меблирована (.+)$")),
    # Известные, но пока не нужные нам характеристики — просто пропускаем.
    (None,         re.compile(r"^(интернет|телефон|балкон|дверь|парковка|пол|безопасность|"
                              r"бывшее общежитие|в залоге|в приват\. общежитии|"
                              r"пластиковые окна|кухня-студия|счётчики|тихий двор|"
                              r"неугловая|улучшенная|комнаты изолированы|"
                              r"встроенная кухня|новая сантехника|кондиционер|"
                              r"площадь кухни|жилая площадь)\b.*$")),
]


def parse_preview(preview):
    """Делит текст на характеристики и свободное описание продавца."""
    out = {"complex": None, "house_type": None, "floors": None, "year": None,
           "condition": None, "ceiling": None, "bathroom": None, "furniture": None,
           "pledged": False, "description": ""}
    if not preview:
        return out
    # В этом значении есть своя запятая — временно убираем её, чтобы не разрезать.
    preview = preview.replace("не новый, но аккуратный ремонт", "не новый но аккуратный ремонт")
    parts = [p.strip() for p in preview.split(", ")]
    i = 0
    while i < len(parts):
        part = parts[i]
        if part.lower() == "без мебели":
            out["furniture"] = "нет"
            i += 1
            continue
        if part.lower() == "в залоге":      # пометка стоит первой и с заглавной буквы
            out["pledged"] = True
            i += 1
            continue
        for field, rx in _PREVIEW_RULES:
            m = rx.match(part)
            if m:
                if field in ("floors", "year"):
                    out[field] = int(m.group(1))
                elif field == "ceiling":
                    out[field] = float(m.group(1))
                elif field:
                    out[field] = m.group(1).strip().replace("не новый но", "не новый, но")
                break
        else:
            break   # первая неузнанная часть — начало описания
        i += 1
    out["description"] = ", ".join(parts[i:]).strip()
    return out


def _seller_type(card):
    owner = card.select_one(".a-card__owner")
    text = owner.get_text(" ", strip=True) if owner else ""
    if card.select_one(".a-card__complex-label") or "застройщик" in text:
        return "застройщик"
    if "Хозяин" in text:
        return "хозяин"
    if "Крыша Агент" in text:
        return "крыша агент"
    if "Руководитель" in text:
        return "компания"
    if "Специалист" in text:
        return "специалист"
    return text[:40] or None


def parse_list_page(html):
    """Возвращает список объявлений со страницы. Каждое — словарь с полями."""
    if is_blocked(html):
        raise BlockedError("вместо списка пришла проверка на бота")
    soup = BeautifulSoup(html, "lxml")
    items = []
    for card in soup.select("div.a-card[data-id]"):
        def text(sel):
            el = card.select_one(sel)
            return el.get_text(" ", strip=True) if el else None

        title = text(".a-card__title") or ""
        price_text = text(".a-card__price") or ""
        subtitle = text(".a-card__subtitle") or ""
        info = parse_preview(text(".a-card__text-preview"))

        rooms = re.search(r"(\d+)-комнатная", title)
        area = re.search(r"([\d.,]+)\s*м²", title)
        floor = re.search(r"(\d+)/(\d+)\s*этаж", title)       # "5/9 этаж"
        floor_only = re.search(r"·\s*(\d+)\s*этаж", title)    # "5 этаж"

        district, _, address = subtitle.partition(", ")
        if "р-н" not in district:           # района нет, вся строка — адрес
            district, address = None, subtitle

        image = card.select_one("a.a-card__image")
        picture = card.select_one("picture")
        complex_link = card.select_one(".a-card__complex-info a")
        stats = [s.get_text(strip=True) for s in card.select(".a-card__stats-item")]

        item = {
            "id": int(card["data-id"]),
            "uuid": card.get("data-uuid"),
            "url": f"https://krisha.kz/a/show/{card['data-id']}",
            # Объявление застройщика о планировке ("от ... тг"), а не конкретная квартира.
            "is_layout": 1 if card.get("data-is-layout") == "1" else 0,
            "rooms": int(rooms.group(1)) if rooms else None,
            "area": _num(area.group(1)) if area else None,
            "floor": int(floor.group(1)) if floor else (int(floor_only.group(1)) if floor_only else None),
            "floors": int(floor.group(2)) if floor else info["floors"],
            "price": _num(price_text),
            "district": district,
            "address": address or None,
            "complex": info["complex"] or (
                complex_link.get_text(strip=True).replace("ЖК ", "").strip("«»") if complex_link else None),
            "house_type": info["house_type"],
            "year": info["year"],
            "condition": info["condition"],
            "ceiling": info["ceiling"],
            "bathroom": info["bathroom"],
            "furniture": info["furniture"],
            "pledged": 1 if info["pledged"] else 0,
            "seller_type": _seller_type(card),
            "description": info["description"],
            "photo_count": int(image["data-nb"]) if image and image.get("data-nb", "").isdigit() else 0,
            "photo_url": picture.get("data-full-src") if picture else None,
            "posted": stats[1] if len(stats) > 1 else None,
        }
        items.append(item)
    return items
