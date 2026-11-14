"""Расчёт выгоды с учётом ремонта, мебели и техники.

Идея: приводим каждую квартиру к одному состоянию — «готова к жизни» (хороший ремонт).
    цена «под ключ» = цена + стоимость ремонта до хорошего − стоимость мебели и техники
Потом сравниваем цену «под ключ» за м² с медианой такой же цены у аналогов.
Так черновая квартира перестаёт выглядеть выгодной только потому, что она без ремонта.

Уровень отделки берём из оценки по фото (ИИ), а если её нет — со слов продавца.
Тарифы ремонта и стоимость обстановки задаются в config.yaml (блок benefit).
"""
import json
from statistics import median

from .enrich import year_bucket
from .vision import SCHEMA_SQL as VISION_SQL

SCHEMA = """
DROP TABLE IF EXISTS benefit;
CREATE TABLE benefit (
    listing_id    INTEGER PRIMARY KEY,
    level         INTEGER,   -- уровень отделки 1…5
    level_source  TEXT,      -- photo (по фото, ИИ) / seller (со слов продавца)
    scenario      TEXT,      -- renovate (под ремонт, уровень 1–3) / ready (готовая, 4–5)
    reno_cost     INTEGER,   -- во сколько обойдётся ремонт до хорошего, тг (минус = ремонт лучше хорошего)
    equip_value   INTEGER,   -- сколько стоят мебель и техника, которые остаются, тг
    ready_price   INTEGER,   -- цена «под ключ», тг
    ready_m2      INTEGER,   -- она же за м²
    comp_level    TEXT,      -- с кем сравнили: complex / nearby / district / none
    comp_count    INTEGER,
    comp_ready_m2 INTEGER,   -- медиана цены «под ключ» за м² у аналогов
    benefit_tg    INTEGER,   -- выгода на всю квартиру, тг (минус = переплата)
    benefit_pct   REAL       -- выгода в % от цены «под ключ» у аналогов
);
"""

# Состояние со слов продавца -> уровень отделки.
SELLER_LEVEL = {
    "черновая отделка": 1, "свободная планировка": 1, "требует ремонта": 2,
    "не новый, но аккуратный ремонт": 3, "свежий ремонт": 4,
}
SELLER_FURNITURE = {"полностью": "full", "частично": "partial", "нет": "none"}


def describe(row, vision, b):
    """Уровень отделки, стоимость ремонта и обстановки для одной квартиры."""
    if vision:
        level, source = vision["finish_level"], "photo"
        furniture = vision["furniture"]
        furniture_class = json.loads(vision["raw"]).get("furniture_class", "mid")   # есть только в полном ответе
        factor = b["furniture_class_factor"].get(furniture_class, 1.0)
        kitchen = vision["kitchen_set"] == "yes"
        appliances = len([a for a in (vision["appliances"] or "").split(",") if a.strip()])
    else:
        level, source = SELLER_LEVEL.get(row["condition"]), "seller"
        furniture = SELLER_FURNITURE.get(row["furniture"], "none")
        factor = b["furniture_class_factor"]["economy"]   # слова продавца не проверены — считаем по минимуму
        kitchen, appliances = False, 0
    if level is None:
        return None
    reno = b["renovation_per_m2"][level] * row["area"]
    equip = (b["furniture_value"].get(furniture, 0) * factor
             + (b["kitchen_set_value"] if kitchen else 0)
             + b["appliance_value"] * appliances)
    ready = row["price"] + reno - equip
    return {"level": level, "source": source,
            "scenario": "ready" if level >= 4 else "renovate",
            "reno": round(reno), "equip": round(equip),
            "ready": round(ready), "ready_m2": ready / row["area"]}


def compute(cfg, con, log=print):
    b = cfg["benefit"]
    min_count, tol = cfg["comps"]["min_count"], cfg["comps"]["area_tolerance"]
    edges = cfg["buckets"]["year_edges"]
    con.executescript(VISION_SQL)

    rows = con.execute(
        "SELECT l.*, m.is_primary FROM listings l JOIN metrics m ON m.listing_id = l.id "
        "WHERE l.active = 1").fetchall()
    visions = {v["listing_id"]: v for v in con.execute("SELECT * FROM vision")}

    items = []
    for r in rows:
        d = describe(r, visions.get(r["id"]), b)
        if d:
            d.update(id=r["id"], area=r["area"], complex=r["complex"], district=r["district"],
                     house_type=r["house_type"], ybucket=year_bucket(r["year"], edges),
                     primary=r["is_primary"])
            items.append(d)
    pool = [x for x in items if x["primary"]]

    def comps_for(x):
        others = [y for y in pool if y["id"] != x["id"]]
        levels = []
        if x["complex"]:
            levels.append(("complex", [y for y in others if y["complex"] == x["complex"]]))
        if x["district"]:
            area_ok = [y for y in others if y["district"] == x["district"]
                       and abs(y["area"] - x["area"]) <= tol * x["area"]]
            levels.append(("nearby", [y for y in area_ok if y["house_type"] == x["house_type"]
                                      and y["ybucket"] == x["ybucket"]]))
            levels.append(("district", area_ok))
        for name, found in levels:
            if len(found) >= min_count:
                return name, found
        return "none", []

    con.executescript(SCHEMA)
    for x in items:
        level, found = comps_for(x)
        comp = median(y["ready_m2"] for y in found) if found else None
        tg = (comp - x["ready_m2"]) * x["area"] if comp else None
        pct = (comp - x["ready_m2"]) / comp * 100 if comp else None
        con.execute("INSERT INTO benefit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            x["id"], x["level"], x["source"], x["scenario"], x["reno"], x["equip"],
            x["ready"], round(x["ready_m2"]), level, len(found),
            round(comp) if comp else None,
            round(tg) if tg is not None else None, round(pct, 1) if pct is not None else None))
    con.commit()
    by_photo = sum(1 for x in items if x["source"] == "photo")
    log(f"Выгода посчитана для {len(items)} квартир (по фото: {by_photo}, со слов продавца: "
        f"{len(items) - by_photo}); без уровня отделки осталось {len(rows) - len(items)}")
