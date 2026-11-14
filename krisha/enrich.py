"""Расчёты поверх собранных данных: дубли, класс отделки, аналоги, скидка.

Всё складывается в таблицу metrics и пересчитывается целиком при каждом запуске.
Пока класс отделки берётся со слов продавца; позже его заменит оценка по фото.
"""
from collections import defaultdict
from statistics import median

SCHEMA = """
DROP TABLE IF EXISTS metrics;
CREATE TABLE metrics (
    listing_id   INTEGER PRIMARY KEY,
    price_m2     INTEGER,      -- цена за м²
    finish       TEXT,         -- rough (черновая) / finished (с ремонтом) / needs_repair / unknown
    dup_group    INTEGER,      -- номер группы дублей (NULL = дублей нет)
    is_primary   INTEGER,      -- 1 = показываем это объявление, 0 = копия подороже
    dup_count    INTEGER,      -- сколько объявлений в группе
    comp_level   TEXT,         -- с кем сравнили: complex+finish / complex / nearby / district / none
    comp_count   INTEGER,      -- сколько аналогов нашлось
    comp_m2      INTEGER,      -- медиана цены за м² у аналогов
    discount_pct REAL,         -- на сколько % дешевле аналогов (минус = дороже)
    discount_tg  INTEGER       -- то же в тенге на всю квартиру
);
"""

ROUGH_WORDS = ("чернов", "предчистов", "white box", "вайт бокс", "без отделки", "без ремонта")


def finish_class(row):
    """Класс отделки со слов продавца (поле 'состояние' и текст)."""
    cond = (row["condition"] or "").lower()
    text = (row["description"] or "").lower()
    if cond in ("черновая отделка", "свободная планировка"):
        return "rough"
    if cond == "требует ремонта":
        return "needs_repair"
    if cond in ("свежий ремонт", "не новый, но аккуратный ремонт"):
        return "finished"
    if any(w in text for w in ROUGH_WORDS):
        return "rough"
    if row["furniture"] in ("полностью", "частично"):
        return "finished"       # мебель в черновой квартире не ставят
    return "unknown"


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
    min_count = cfg["comps"]["min_count"]
    tol = cfg["comps"]["area_tolerance"]
    edges = cfg["buckets"]["year_edges"]

    rows = con.execute(
        "SELECT * FROM listings WHERE active = 1 AND is_layout = 0 "
        "AND price > 0 AND area > 0").fetchall()
    dups = find_duplicates(rows)

    # Рабочие записи: только то, что нужно для сравнения.
    items = []
    for r in rows:
        group, primary, count = dups.get(r["id"], (None, 1, 1))
        items.append({
            "id": r["id"], "area": r["area"], "complex": r["complex"],
            "district": r["district"], "house_type": r["house_type"],
            "ybucket": year_bucket(r["year"], edges),
            "m2": r["price"] / r["area"], "finish": finish_class(r),
            "group": group, "primary": primary, "dup_count": count,
        })
    pool = [x for x in items if x["primary"]]     # копии в статистике не участвуют

    def comps_for(x):
        """Ищет аналоги от самого точного набора к самому грубому."""
        others = [y for y in pool if y["id"] != x["id"]]
        near_area = lambda y: abs(y["area"] - x["area"]) <= tol * x["area"]
        levels = []
        if x["complex"]:
            same = [y for y in others if y["complex"] == x["complex"]]
            if x["finish"] != "unknown":
                levels.append(("complex+finish", [y for y in same if y["finish"] == x["finish"]]))
            levels.append(("complex", same))
        if x["district"]:
            area_ok = [y for y in others if y["district"] == x["district"] and near_area(y)]
            levels.append(("nearby", [y for y in area_ok
                                      if y["house_type"] == x["house_type"]
                                      and y["ybucket"] == x["ybucket"]
                                      and y["finish"] == x["finish"]]))
            levels.append(("district", area_ok))
        for name, found in levels:
            if len(found) >= min_count:
                return name, found
        return "none", []

    con.executescript(SCHEMA)
    for x in items:
        level, found = comps_for(x)
        comp_m2 = median(y["m2"] for y in found) if found else None
        pct = (comp_m2 - x["m2"]) / comp_m2 * 100 if comp_m2 else None
        tg = (comp_m2 - x["m2"]) * x["area"] if comp_m2 else None
        con.execute(
            "INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (x["id"], round(x["m2"]), x["finish"], x["group"], x["primary"], x["dup_count"],
             level, len(found), round(comp_m2) if comp_m2 else None,
             round(pct, 1) if pct is not None else None, round(tg) if tg is not None else None))
    con.commit()

    summary = {
        "квартир": len(items),
        "копий скрыто": sum(1 for x in items if not x["primary"]),
        "без аналогов": con.execute("SELECT COUNT(*) FROM metrics WHERE comp_level = 'none'").fetchone()[0],
    }
    log(f"Расчёты готовы: {summary}")
    return summary
