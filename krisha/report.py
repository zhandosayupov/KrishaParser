"""Сборка report.html: один файл, данные внутри, открывается двойным кликом."""
import json
from pathlib import Path

from .enrich import year_bucket

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


def build_report(cfg, con, out_path, log=print):
    step = cfg["buckets"]["price_step_mln"]
    edges = cfg["buckets"]["year_edges"]
    rows = con.execute(
        "SELECT l.*, m.price_m2, m.finish, m.is_primary, m.dup_count, m.comp_level, "
        "m.comp_count, m.comp_m2, m.discount_pct, m.discount_tg, "
        "(SELECT COUNT(*) - 1 FROM price_history p WHERE p.listing_id = l.id) AS price_changes, "
        "(SELECT price FROM price_history p WHERE p.listing_id = l.id ORDER BY seen_at LIMIT 1) AS first_price "
        "FROM listings l JOIN metrics m ON m.listing_id = l.id WHERE l.active = 1").fetchall()

    data = []
    for r in rows:
        d = dict(r)
        d["g_price"] = price_label(d["price"], step)
        d["g_year"] = year_label(d["year"], edges)
        d["g_complex"] = d["complex"] or "ЖК не указан"
        d["g_district"] = d["district"] or "район не указан"
        for k in ("uuid", "is_layout", "active", "posted", "last_seen"):
            d.pop(k, None)
        data.append(d)

    last_run = con.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    meta = {"generated": last_run[0] if last_run else "", "count": len(data),
            "min_comps": cfg["comps"]["min_count"]}
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*DATA*/[]", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("/*META*/{}", json.dumps(meta, ensure_ascii=False))
    Path(out_path).write_text(html, encoding="utf-8")
    log(f"Отчёт: {out_path} ({len(data)} объявлений)")
