"""Этап оценки фото и сверки их с текстом объявления.

Для каждой уникальной квартиры: склеиваем фото в листы-коллажи (так запросов и
расхода лимита в разы меньше), отправляем ИИ вместе с текстом продавца,
получаем оценку строго по схеме и кладём в таблицу vision.
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw

from .llm import ask, LLMError, LLMUnavailable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vision (
    listing_id      INTEGER PRIMARY KEY,
    provider        TEXT,
    model           TEXT,
    created_at      TEXT,
    photos_used     INTEGER,
    finish_level    INTEGER,   -- 1 черновая … 5 дизайнерский
    confidence      TEXT,      -- low / medium / high
    wear            TEXT,      -- none / light / noticeable / heavy
    furniture       TEXT,      -- none / partial / full / not_visible
    kitchen_set     TEXT,      -- yes / no / not_visible
    appliances      TEXT,      -- список через запятую
    staged          INTEGER,   -- 1 = рендер или дорисованная мебель
    contradictions  INTEGER,   -- сколько утверждений продавца опровергнуто фото
    trust           INTEGER,   -- доверие к объявлению, 1…5
    summary         TEXT,
    raw             TEXT       -- полный ответ ИИ в JSON
);
"""

ROOMS = ["kitchen", "bathroom", "living", "bedroom", "hallway", "balcony",
         "view", "exterior", "plan", "render", "other"]
APPLIANCES = ["cooktop", "oven", "hood", "dishwasher", "fridge", "microwave",
              "washer", "boiler", "ac", "tv"]

ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["photos", "finish_level", "confidence", "wear", "furniture",
                 "furniture_class", "kitchen_set", "appliances", "rooms_not_shown",
                 "staged", "mixed_flats", "claims", "trust", "summary"],
    "properties": {
        "photos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["n", "room"],
            "properties": {"n": {"type": "integer"}, "room": {"type": "string", "enum": ROOMS}}}},
        "finish_level": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "wear": {"type": "string", "enum": ["none", "light", "noticeable", "heavy"]},
        "furniture": {"type": "string", "enum": ["none", "partial", "full", "not_visible"]},
        "furniture_class": {"type": "string", "enum": ["economy", "mid", "premium", "na"]},
        "kitchen_set": {"type": "string", "enum": ["yes", "no", "not_visible"]},
        "appliances": {"type": "array", "items": {"type": "string", "enum": APPLIANCES}},
        "rooms_not_shown": {"type": "array", "items": {
            "type": "string", "enum": ["kitchen", "bathroom", "living"]}},
        "staged": {"type": "boolean"},
        "mixed_flats": {"type": "boolean"},
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["claim", "status"],
            "properties": {"claim": {"type": "string"},
                           "status": {"type": "string",
                                      "enum": ["confirmed", "contradicted", "not_visible"]}}}},
        "trust": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "summary": {"type": "string"},
    },
}

PROMPT = """Ты оцениваешь квартиру в новостройке Алматы по фотографиям из объявления о продаже.
Фото собраны в листы-коллажи; на каждом фото в углу стоит его номер.

Что известно из объявления (это слова продавца, им нельзя верить на слово):
{facts}

Текст продавца (обрезан):
«{description}»

Задачи.

1. photos — для каждого номера фото укажи, что на нём: kitchen, bathroom, living, bedroom,
   hallway, balcony, view (вид из окна), exterior (дом, двор, подъезд), plan (планировка),
   render (рендер или реклама застройщика), other.

2. finish_level — уровень отделки самой квартиры, только по тому, что видно:
   1 черновая: бетон, нет стяжки и полов, торчит проводка;
   2 предчистовая: ровные белые стены, стяжка, нет дверей и сантехники;
   3 от застройщика: дешёвый ламинат, простые обои или покраска, базовая сантехника, типовые двери;
   4 хороший ремонт: качественные полы, ровные однотонные стены, современная сантехника, продуманный свет;
   5 дизайнерский: мебель на заказ, скрытые двери и плинтусы, трековый свет, крупноформатная плитка.
   Фото exterior, plan и render в оценке не учитывай. confidence — насколько ты уверен.

3. wear — износ: none, light, noticeable (пятна, сколы, потёртый пол), heavy (плесень, трещины, отклеенные обои).

4. furniture — none / partial / full / not_visible; furniture_class — economy / mid / premium, либо na если мебели нет.
   kitchen_set — есть ли кухонный гарнитур. appliances — только техника, которую реально видно на фото.

5. rooms_not_shown — какие из kitchen, bathroom, living не показаны ни на одном фото.

6. staged — true, если интерьер на фото похож на рендер или мебель дорисована.
   mixed_flats — true, если фото явно из разных квартир (не совпадают окна, полы, отделка).

7. claims — выпиши каждое проверяемое утверждение продавца о состоянии, ремонте, мебели и технике
   (из фактов и текста) и поставь статус: confirmed (видно на фото), contradicted (фото показывают
   обратное), not_visible (на фото этого не видно — это НЕ опровержение).
   Площадь, этаж, год, число комнат, цену и расположение сюда НЕ включай: по фото они не проверяются.
   Если таких утверждений нет — верни пустой список.

8. trust — доверие к объявлению от 1 до 5: падает от опровергнутых утверждений, рендеров вместо фото,
   смешанных квартир, отсутствия ключевых комнат.

9. summary — одна-две фразы по-русски: что за квартира по факту и главное расхождение с текстом, если есть.
"""


def make_sheets(photo_paths, out_dir, per_sheet=6, tile=(750, 470)):
    """Склеивает фото в листы 3×2 с номерами. Возвращает пути к листам."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = []
    for s in range(0, len(photo_paths), per_sheet):
        chunk = photo_paths[s:s + per_sheet]
        cols = min(3, len(chunk))
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tile[0], rows * tile[1]), "white")
        draw = ImageDraw.Draw(sheet)
        for i, path in enumerate(chunk):
            x, y = (i % cols) * tile[0], (i // cols) * tile[1]
            with Image.open(path) as im:
                sheet.paste(im.convert("RGB").resize(tile), (x, y))
            label = Path(path).stem                       # номер фото
            draw.rectangle([x, y, x + 26 + 16 * len(label), y + 44], fill="black")
            draw.text((x + 10, y + 4), label, fill="white", font_size=32)
        file = out_dir / f"sheet{len(sheets) + 1}.jpg"
        sheet.save(file, quality=82)
        sheets.append(file)
    return sheets


def _facts(r):
    parts = [f"{r['rooms']}-комнатная, {r['area']} м², этаж {r['floor']}/{r['floors']}",
             f"ЖК: {r['complex'] or 'не указан'}, {r['year'] or '?'} г.п."]
    if r["condition"]:
        parts.append(f"состояние: {r['condition']}")
    if r["furniture"]:
        parts.append(f"мебель: {r['furniture']}")
    parts.append(f"цена {r['price']:,} тг".replace(",", " "))
    return "\n".join("- " + p for p in parts)


def assess(cfg, con, root, log=print, limit=None):
    """Оценивает квартиры, у которых есть фото и ещё нет оценки. Можно прерывать и продолжать."""
    stage = cfg["llm"]["vision"]
    con.executescript(SCHEMA_SQL)
    rows = con.execute(
        "SELECT l.* FROM listings l JOIN metrics m ON m.listing_id = l.id "
        "WHERE l.active = 1 AND m.is_primary = 1 "
        "AND EXISTS (SELECT 1 FROM photos p WHERE p.listing_id = l.id) "
        "AND NOT EXISTS (SELECT 1 FROM vision v WHERE v.listing_id = l.id) "
        "ORDER BY m.discount_pct DESC").fetchall()
    if limit:
        rows = rows[:limit]
    log(f"Квартир к оценке: {len(rows)} (провайдер {stage['provider']}, модель {stage.get('model')})")

    done = 0
    for r in rows:
        photos = [Path(root) / p["path"] for p in con.execute(
            "SELECT path FROM photos WHERE listing_id = ? ORDER BY n", (r["id"],))]
        photos = [p for p in photos if p.exists()]
        if not photos:
            continue
        sheets = make_sheets(photos, photos[0].parent / "sheets")
        prompt = PROMPT.format(facts=_facts(r), description=r["description"] or "текста нет")
        try:
            a = ask(stage, prompt, sheets, ANSWER_SCHEMA)
        except LLMUnavailable as e:
            log(f"СТОП: {e}. Готово {done}, остальное продолжится при следующем запуске.")
            return done
        except LLMError as e:
            log(f"  {r['id']}: пропуск ({e})")
            continue
        contradictions = sum(1 for c in a["claims"] if c["status"] == "contradicted")
        con.execute(
            "INSERT OR REPLACE INTO vision VALUES (?,?,?,datetime('now','localtime'),?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], stage["provider"], stage.get("model"), len(photos), a["finish_level"],
             a["confidence"], a["wear"], a["furniture"], a["kitchen_set"],
             ", ".join(a["appliances"]), int(a["staged"]), contradictions, a["trust"],
             a["summary"], json.dumps(a, ensure_ascii=False)))
        con.commit()
        done += 1
        log(f"  {r['id']}: отделка {a['finish_level']}, доверие {a['trust']}, "
            f"расхождений {contradictions} — {a['summary'][:90]}")
    return done
