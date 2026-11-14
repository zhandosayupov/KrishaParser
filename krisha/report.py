"""Сборка report.html: один файл, данные внутри, открывается двойным кликом."""
import json
from pathlib import Path

from .analyst import SCHEMA_SQL as ANALYST_SQL
from .db import load_flats
from .enrich import year_bucket
from .vision import ensure_table

TEMPLATE = Path(__file__).with_name("report_template.html")


def price_label(price, step_mln):
    # Ровно 60 млн относим к корзине 50–60, а не 60–70.
    lo = int((price - 1) // (step_mln * 1_000_000)) * step_mln
    return f"{lo}–{lo + step_mln} млн"


def year_label(year, edges):
    if year is None:
        return "год не указан"
    b = year_bucket(year, edges)
    if b == 0:
        return f"до {edges[0]}"
    if b == len(edges):
        return f"{edges[-1]}+"
    return f"{edges[b - 1]}–{edges[b] - 1}"


def _table(con, name):
    return {r["listing_id"]: dict(r) for r in con.execute(f"SELECT * FROM {name}")}


def build_report(cfg, con, out_path, log=print):
    step = cfg["buckets"]["price_step_mln"]
    edges = cfg["buckets"]["year_edges"]
    ensure_table(con)
    con.executescript(ANALYST_SQL)
    metrics, benefit = _table(con, "metrics"), _table(con, "benefit")
    vision, analyst = _table(con, "vision"), _table(con, "analyst")
    first_price = {}
    for r in con.execute("SELECT listing_id, price FROM price_history ORDER BY seen_at DESC"):
        first_price[r["listing_id"]] = r["price"]        # последним запишется самое раннее

    data = []
    for f in load_flats(con):
        id_ = f["id"]
        if id_ not in metrics:
            continue
        d = {k: f[k] for k in ("id", "url", "rooms", "area", "floor", "floors", "price", "district",
                               "address", "complex", "house_type", "year", "condition", "ceiling",
                               "furniture", "pledged", "seller_type", "description", "photo_count",
                               "photo_url", "first_seen", "filled")}
        d.update({k: metrics[id_][k] for k in ("price_m2", "is_primary", "dup_count", "urgent", "bargain")})
        d.update({k: v for k, v in benefit.get(id_, {}).items() if k != "listing_id"})
        v = vision.get(id_)
        if v:
            raw = json.loads(v["raw"])
            d.update({"finish_level": v["finish_level"], "confidence": v["confidence"], "wear": v["wear"],
                      "v_furniture": v["furniture"], "kitchen_set": v["kitchen_set"],
                      "appliances": v["appliances"], "view": v["view"], "staged": v["staged"],
                      "contradictions": v["contradictions"], "trust": v["trust"], "summary": v["summary"],
                      # в отчёт берём только подтверждённые и опровергнутые утверждения продавца
                      "claims": [c for c in raw["claims"] if c["status"] != "not_visible"],
                      "not_shown": raw["rooms_not_shown"]})
        a = analyst.get(id_)
        if a:
            d.update({k: a[k] for k in ("verdict", "reason", "advantage", "risk")})
        d["price_drop"] = max(0, first_price.get(id_, f["price"]) - f["price"])
        d["g_price"] = price_label(f["price"], step)
        d["g_year"] = year_label(f["year"], edges)
        d["g_complex"] = f["complex"] or "ЖК не указан"
        d["g_district"] = f["district"] or "район не указан"
        data.append(d)

    last_run = con.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    meta = {"generated": last_run[0] if last_run else "", "count": len(data),
            "min_comps": cfg["comps"]["min_count"], "combine": cfg["benefit"]["combine"]}
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*DATA*/[]", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("/*META*/{}", json.dumps(meta, ensure_ascii=False))
    Path(out_path).write_text(html, encoding="utf-8")
    log(f"Отчёт: {out_path} ({len(data)} объявлений)")
