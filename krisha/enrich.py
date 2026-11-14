"""Первичные расчёты: копии одной квартиры, цена за м², пометки «срочно» и «торг».

Сравнение с аналогами и выгода считаются в benefit.py.
"""
import re
from collections import defaultdict

from .db import load_flats

SCHEMA = """
DROP TABLE IF EXISTS metrics;
CREATE TABLE metrics (
    listing_id   INTEGER PRIMARY KEY,
    price_m2     INTEGER,      -- цена за м²
    dup_group    INTEGER,      -- номер группы копий (NULL = копий нет)
    is_primary   INTEGER,      -- 1 = показываем это объявление, 0 = копия подороже
    dup_count    INTEGER,      -- сколько объявлений в группе
    urgent       INTEGER,      -- 1 = в тексте есть «срочно»
    bargain      INTEGER       -- 1 = в тексте есть «торг» (и нет «без торга»)
);
"""

_URGENT = re.compile(r"срочн", re.I)
_BARGAIN = re.compile(r"торг", re.I)
_NO_BARGAIN = re.compile(r"без\s+торга|торга\s+нет|не\s+торгу|торг\s+не\s*уместен", re.I)


def text_flags(text):
    """(срочно, торг) по тексту объявления. Текст в списке обрезан, так что пропуски возможны."""
    text = text or ""
    return (1 if _URGENT.search(text) else 0,
            1 if _BARGAIN.search(text) and not _NO_BARGAIN.search(text) else 0)


def year_bucket(year, edges):
    """Номер корзины года постройки по границам из конфига."""
    if year is None:
        return None
    return sum(1 for e in edges if year >= e)


def find_duplicates(rows, price_tolerance=0.07):
    """Одна квартира в нескольких объявлениях: тот же дом, комнаты, площадь, этаж
    и близкая цена (в пределах 7%). Разница в цене больше — считаем разными квартирами.

    Возвращает {id: (номер группы, главный ли, размер группы)}. Главным считаем самое дешёвое.
    """
    by_key = defaultdict(list)
    for r in rows:
        house = r["complex"] or r["address"]
        if not house or r["floor"] is None or r["area"] is None:
            continue
        by_key[(house, r["rooms"], round(r["area"], 1), r["floor"], r["floors"])].append(r)

    clusters = []
    for members in by_key.values():
        members.sort(key=lambda r: (r["price"], r["id"]))
        current = [members[0]]
        for r in members[1:]:
            if r["price"] <= current[0]["price"] * (1 + price_tolerance):
                current.append(r)
            else:
                clusters.append(current)
                current = [r]
        clusters.append(current)

    result = {}
    for n, members in enumerate((c for c in clusters if len(c) > 1), start=1):
        for i, r in enumerate(members):
            result[r["id"]] = (n, 1 if i == 0 else 0, len(members))
    return result


def enrich(cfg, con, log=print):
    flats = load_flats(con)
    dups = find_duplicates(flats)
    con.executescript(SCHEMA)
    for r in flats:
        group, primary, count = dups.get(r["id"], (None, 1, 1))
        urgent, bargain = text_flags(r["description"])
        con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?)",
                    (r["id"], round(r["price"] / r["area"]), group, primary, count, urgent, bargain))
    con.commit()
    hidden = sum(1 for v in dups.values() if not v[1])
    log(f"Квартир: {len(flats)}, копий скрыто: {hidden}")
