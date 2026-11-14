"""Сравнение с аналогами и расчёт выгоды.

Считаем двумя «линейками» и тремя способами.

Линейки:
  raw   — просто цена за м², как в объявлении (есть у всех квартир);
  ready — цена «под ключ» за м²: цена + материалы на ремонт до хорошего состояния
          − стоимость остающихся мебели и техники. Нужен уровень отделки
          (по фото от ИИ, а если фото не оценены — со слов продавца).

Способы (с кем сравниваем):
  complex  — квартиры того же ЖК;
  nearby   — окружение: тот же район, тот же тип дома, те же годы постройки, близкая площадь;
  district — весь район, близкая площадь.

Итоговая выгода — по линейке ready: самая пессимистичная из трёх оценок или их среднее
(benefit.combine в config.yaml). Отдельно считаем, насколько сам ЖК дешевле или дороже района.
"""
import json
from statistics import median

from .db import load_flats
from .enrich import year_bucket
from .vision import ensure_table

METHODS = ("complex", "nearby", "district")

SCHEMA = """
DROP TABLE IF EXISTS benefit;
CREATE TABLE benefit (
    listing_id      INTEGER PRIMARY KEY,
    -- линейка raw: на сколько % цена за м² ниже медианы (минус = дороже), и сколько аналогов
    raw_complex     REAL, n_complex  INTEGER,
    raw_nearby      REAL, n_nearby   INTEGER,
    raw_district    REAL, n_district INTEGER,
    raw_pct         REAL,      -- итог по raw (пессимистичный или средний)
    -- уровень отделки и поправки
    level           INTEGER,   -- 1…10
    level_source    TEXT,      -- photo (по фото, ИИ) / seller (со слов продавца)
    scenario        TEXT,      -- renovate (под ремонт) / ready (готовая)
    reno_cost       INTEGER,   -- материалы на ремонт до хорошего, тг (минус = ремонт лучше хорошего)
    equip_value     INTEGER,   -- стоимость остающихся мебели и нужной техники, тг
    ready_m2        INTEGER,   -- цена «под ключ» за м²
    -- линейка ready
    ready_complex   REAL,
    ready_nearby    REAL,
    ready_district  REAL,
    benefit_pct     REAL,      -- итоговая выгода, %
    benefit_tg      INTEGER,   -- она же в тенге на всю квартиру
    methods         INTEGER,   -- сколькими способами удалось сравнить
    -- сам ЖК против района
    complex_vs_district REAL   -- на сколько % медиана ЖК ниже медианы района (минус = ЖК дороже)
);
"""

# Состояние со слов продавца -> уровень отделки по шкале 1…10.
SELLER_LEVEL = {
    "свободная планировка": 1, "черновая отделка": 2, "требует ремонта": 4,
    "не новый, но аккуратный ремонт": 7, "свежий ремонт": 8,
}
SELLER_FURNITURE = {"полностью": "full", "частично": "partial", "нет": "none"}


def describe(flat, vision, b):
    """Уровень отделки и поправки (ремонт, обстановка) для одной квартиры. None — уровня нет."""
    area = flat["area"]
    size = 0 if area <= b["small_flat_max_m2"] else 1      # 0 маленькая, 1 большая
    if vision:
        level, source = vision["finish_level"], "photo"
        furniture = vision["furniture"]
        furniture_class = json.loads(vision["raw"]).get("furniture_class", "mid")
        factor = b["furniture_class_factor"].get(furniture_class, 1.0)
        kitchen = vision["kitchen_set"] == "yes"
        seen = [a.strip() for a in (vision["appliances"] or "").split(",") if a.strip()]
    else:
        level, source = SELLER_LEVEL.get(flat["condition"]), "seller"
        furniture = SELLER_FURNITURE.get(flat["furniture"], "none")
        factor = b["furniture_class_factor"]["economy"]   # слова продавца не проверены — считаем по минимуму
        kitchen, seen = False, []
    if level is None:
        return None
    reno = b["renovation_per_m2"][level] * area
    # Техника: считаем только ту, что нужна в квартире такого размера (цена 0 = не нужна).
    appliances = sum(b["appliance_prices"].get(a, [0, 0])[size] for a in seen)
    equip = (b["furniture_per_m2"].get(furniture, 0) * area * factor
             + (b["kitchen_set_value"][size] if kitchen else 0) + appliances)
    ready = flat["price"] + reno - equip
    return {"level": level, "source": source,
            "scenario": "ready" if level >= b["ready_from_level"] else "renovate",
            "reno": round(reno), "equip": round(equip), "ready_m2": ready / area}


def combine(values, how):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return min(values) if how == "min" else sum(values) / len(values)


def compute(cfg, con, log=print):
    b = cfg["benefit"]
    min_count, tol = cfg["comps"]["min_count"], cfg["comps"]["area_tolerance"]
    edges = cfg["buckets"]["year_edges"]
    ensure_table(con)

    primary = {r["listing_id"]: r["is_primary"] for r in con.execute("SELECT * FROM metrics")}
    visions = {v["listing_id"]: v for v in con.execute("SELECT * FROM vision")}

    items = []
    for f in load_flats(con):
        d = describe(f, visions.get(f["id"]), b)
        items.append({
            "id": f["id"], "area": f["area"], "complex": f["complex"], "district": f["district"],
            "house_type": f["house_type"], "ybucket": year_bucket(f["year"], edges),
            "primary": primary.get(f["id"], 1), "raw_m2": f["price"] / f["area"],
            "ready_m2": d["ready_m2"] if d else None, "d": d})
    pool = [x for x in items if x["primary"]]              # копии в сравнении не участвуют

    def groups(x):
        """Аналоги квартиры x каждым из трёх способов."""
        others = [y for y in pool if y["id"] != x["id"]]
        out = {"complex": [], "nearby": [], "district": []}
        if x["complex"]:
            out["complex"] = [y for y in others if y["complex"] == x["complex"]]
        if x["district"]:
            out["district"] = [y for y in others if y["district"] == x["district"]
                               and abs(y["area"] - x["area"]) <= tol * x["area"]]
            out["nearby"] = [y for y in out["district"] if y["house_type"] == x["house_type"]
                             and y["ybucket"] == x["ybucket"]]
        return out

    def pct_below(x, found, key):
        """На сколько % квартира x дешевле медианы аналогов по линейке key."""
        values = [y[key] for y in found if y[key] is not None]
        if x[key] is None or len(values) < min_count:
            return None, len(values)
        m = median(values)
        return (m - x[key]) / m * 100, len(values)

    # Медианы ЖК и районов — чтобы видеть, дешёвый ли сам ЖК.
    def med(key_field):
        by = {}
        for y in pool:
            if y[key_field]:
                by.setdefault(y[key_field], []).append(y["raw_m2"])
        return {k: median(v) for k, v in by.items() if len(v) >= min_count}
    complex_med, district_med = med("complex"), med("district")

    con.executescript(SCHEMA)
    for x in items:
        g = groups(x)
        raw = {m: pct_below(x, g[m], "raw_m2") for m in METHODS}
        ready = {m: pct_below(x, g[m], "ready_m2")[0] for m in METHODS}
        raw_pct = combine([raw[m][0] for m in METHODS], b["combine"])
        benefit_pct = combine(list(ready.values()), b["combine"])
        d = x["d"]
        # Выгода в тенге: процент от цены «под ключ» у аналогов, пересчитанный на площадь.
        benefit_tg = None
        if benefit_pct is not None:
            benefit_tg = x["ready_m2"] / (1 - benefit_pct / 100) * benefit_pct / 100 * x["area"]
        cvd = None
        if x["complex"] in complex_med and x["district"] in district_med:
            dm = district_med[x["district"]]
            cvd = (dm - complex_med[x["complex"]]) / dm * 100
        r1 = lambda v: None if v is None else round(v, 1)
        con.execute(
            "INSERT INTO benefit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                x["id"],
                r1(raw["complex"][0]), raw["complex"][1], r1(raw["nearby"][0]), raw["nearby"][1],
                r1(raw["district"][0]), raw["district"][1], r1(raw_pct),
                d and d["level"], d and d["source"], d and d["scenario"],
                d and d["reno"], d and d["equip"], d and round(d["ready_m2"]),
                r1(ready["complex"]), r1(ready["nearby"]), r1(ready["district"]),
                r1(benefit_pct), None if benefit_tg is None else round(benefit_tg),
                sum(1 for v in ready.values() if v is not None), r1(cvd)))
    con.commit()
    with_level = [x for x in items if x["d"]]
    by_photo = sum(1 for x in with_level if x["d"]["source"] == "photo")
    log(f"Сравнение готово: {len(items)} квартир; уровень отделки есть у {len(with_level)} "
        f"(по фото: {by_photo}, со слов продавца: {len(with_level) - by_photo})")
