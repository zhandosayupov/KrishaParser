"""Этап оценки фото и сверки их с текстом объявления.

Для каждой уникальной квартиры: склеиваем фото в листы-коллажи (так запросов и
расхода лимита в разы меньше), отправляем ИИ вместе с текстом продавца,
получаем оценку строго по схеме и кладём в таблицу vision.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw

from .llm import ask, usage_line, LLMError, LLMUnavailable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vision (
    listing_id      INTEGER PRIMARY KEY,
    provider        TEXT,
    model           TEXT,
    created_at      TEXT,
    photos_used     INTEGER,
    finish_level    INTEGER,   -- 1 без отделки … 10 премиум (шкала в PROMPT ниже)
    confidence      TEXT,      -- low / medium / high
    wear            TEXT,      -- none / light / noticeable / heavy
    furniture       TEXT,      -- none / partial / full / not_visible
    kitchen_set     TEXT,      -- yes / no / not_visible
    appliances      TEXT,      -- список через запятую
    view            TEXT,      -- вид из окна: not_shown / ordinary / good / excellent
    staged          INTEGER,   -- 1 = рендер или дорисованная мебель
    contradictions  INTEGER,   -- сколько утверждений продавца опровергнуто фото
    trust           INTEGER,   -- доверие к объявлению, 1…5
    summary         TEXT,
    x_complex       TEXT,      -- ЖК, найденный в тексте (если в объявлении поле пустое)
    x_house_type    TEXT,      -- тип дома из текста
    x_year          INTEGER,   -- год постройки из текста
    raw             TEXT       -- полный ответ ИИ в JSON
);
"""


def ensure_table(con):
    """Создаёт таблицу vision. Таблицу старого формата (шкала 1–5) удаляет: её оценки несравнимы."""
    cols = [c[1] for c in con.execute("PRAGMA table_info(vision)")]
    if cols and "view" not in cols:
        con.execute("DROP TABLE vision")
    con.executescript(SCHEMA_SQL)


ROOMS = ["kitchen", "bathroom", "living", "bedroom", "hallway", "balcony",
         "view", "exterior", "plan", "render", "other"]
APPLIANCES = ["cooktop", "oven", "hood", "dishwasher", "fridge", "microwave",
              "washer", "boiler", "ac", "tv"]

ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["photos", "finish_level", "confidence", "wear", "furniture",
                 "furniture_class", "kitchen_set", "appliances", "rooms_not_shown",
                 "view", "staged", "mixed_flats", "claims", "text_facts", "trust", "summary"],
    "properties": {
        "photos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["n", "room"],
            "properties": {"n": {"type": "integer"}, "room": {"type": "string", "enum": ROOMS}}}},
        "finish_level": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "wear": {"type": "string", "enum": ["none", "light", "noticeable", "heavy"]},
        "furniture": {"type": "string", "enum": ["none", "partial", "full", "not_visible"]},
        "furniture_class": {"type": "string", "enum": ["economy", "mid", "premium", "na"]},
        "kitchen_set": {"type": "string", "enum": ["yes", "no", "not_visible"]},
        "appliances": {"type": "array", "items": {"type": "string", "enum": APPLIANCES}},
        "rooms_not_shown": {"type": "array", "items": {
            "type": "string", "enum": ["kitchen", "bathroom", "living"]}},
        "view": {"type": "string", "enum": ["not_shown", "ordinary", "good", "excellent"]},
        "text_facts": {"type": "object", "additionalProperties": False,
                       "required": ["complex", "house_type", "year"],
                       "properties": {
                           "complex": {"type": "string"},
                           "house_type": {"type": "string",
                                          "enum": ["монолитный", "кирпичный", "панельный", "unknown"]},
                           "year": {"type": "integer"}}},
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

PROMPT = """Ты оцениваешь квартиру в Алматы по фотографиям из объявления о продаже.
Фото собраны в листы-коллажи; на каждом фото в углу стоит его номер.

Что известно из объявления (это слова продавца, им нельзя верить на слово):
{facts}

Текст продавца (обрезан):
«{description}»

Задачи.

1. photos — для каждого номера фото укажи, что на нём: kitchen, bathroom, living, bedroom,
   hallway, balcony, view (вид из окна), exterior (дом, двор, подъезд), plan (планировка),
   render (рендер или реклама застройщика), other.

2. finish_level — состояние отделки самой квартиры по шкале 1–10, только по тому, что видно:
    1 без отделки: голый бетон или кирпич, нет стяжки, перегородок может не быть;
    2 черновая: стяжка и штукатурка есть, разводка сделана, но нет полов, дверей и сантехники;
    3 предчистовая (white box): стены готовы под покраску, может стоять унитаз, жить нельзя;
    4 убитая: старый ремонт в плохом состоянии — отходят обои, ржавые трубы, разбитая плитка,
      нужен полный ремонт со сносом;
    5 «бабушкин» ремонт: старый, но целый и чистый — линолеум, советская плитка, ковры, старая
      мебель и окна; жить можно, ремонт нужен;
    6 простая отделка: чистовая от застройщика или дешёвая косметика — ламинат эконом-класса,
      простые обои или покраска, базовая сантехника, кухни обычно нет;
    7 аккуратный ремонт не первой свежести: сделан 10–15 лет назад, устарел по стилю
      (арки, многоуровневые потолки, глянец), но всё исправно и ухожено;
    8 хороший современный ремонт: свежий, ровные стены, качественные полы, современная
      сантехника и свет, вкладываться не нужно;
    9 очень хороший: виден дизайн-проект, встроенная мебель, продуманный свет, дорогая плитка;
   10 премиум: дорогие материалы везде, мебель на заказ, скрытые двери и плинтусы, техника премиум-класса.
   Фото exterior, plan и render в оценке не учитывай. confidence — насколько ты уверен.

3. wear — износ: none, light, noticeable (пятна, сколы, потёртый пол), heavy (плесень, трещины, отклеенные обои).

4. furniture — none / partial / full / not_visible; furniture_class — economy / mid / premium, либо na если мебели нет.
   kitchen_set — есть ли кухонный гарнитур. appliances — только техника, которую реально видно на фото.

5. rooms_not_shown — какие из kitchen, bathroom, living не показаны ни на одном фото.

6. view — вид из окна: not_shown (нет такого фото), ordinary (двор, соседний дом),
   good (открытый вид, зелень, панорама города), excellent (горы или парк без помех).

7. staged — true, если интерьер на фото похож на рендер или мебель дорисована.
   mixed_flats — true, если фото явно из разных квартир (не совпадают окна, полы, отделка).

8. claims — выпиши каждое проверяемое утверждение продавца о состоянии, ремонте, мебели и технике
   (из фактов и текста) и поставь статус: confirmed (видно на фото), contradicted (фото показывают
   обратное), not_visible (на фото этого не видно — это НЕ опровержение).
   Площадь, этаж, год, число комнат, цену и расположение сюда НЕ включай: по фото они не проверяются.
   Если таких утверждений нет — верни пустой список.

9. text_facts — что можно достать из ТЕКСТА продавца, если этого нет в фактах выше:
   complex — название ЖК (пустая строка, если не названо); house_type — монолитный / кирпичный /
   панельный / unknown; year — год постройки (0, если не указан). Не угадывай: только то, что написано.

10. trust — доверие к объявлению от 1 до 5: падает от опровергнутых утверждений, рендеров вместо фото,
    смешанных квартир, отсутствия ключевых комнат.

11. summary — одна-две фразы по-русски: что за квартира по факту и главное расхождение с текстом, если есть.
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
             f"ЖК: {r['complex'] or 'не указан'}, год постройки: {r['year'] or 'не указан'}, "
             f"дом: {r['house_type'] or 'не указан'}"]
    if r["condition"]:
        parts.append(f"состояние: {r['condition']}")
    if r["furniture"]:
        parts.append(f"мебель: {r['furniture']}")
    parts.append(f"цена {r['price']:,} тг".replace(",", " "))
    return "\n".join("- " + p for p in parts)


def assess(cfg, con, root, log=print, limit=None):
    """Оценивает квартиры, у которых есть фото и ещё нет оценки. Можно прерывать и продолжать."""
    stage = cfg["llm"]["vision"]
    ensure_table(con)
    rows = con.execute(
        "SELECT l.* FROM listings l JOIN metrics m ON m.listing_id = l.id "
        "LEFT JOIN benefit b ON b.listing_id = l.id "
        "WHERE l.active = 1 AND m.is_primary = 1 "
        "AND EXISTS (SELECT 1 FROM photos p WHERE p.listing_id = l.id) "
        "AND NOT EXISTS (SELECT 1 FROM vision v WHERE v.listing_id = l.id) "
        # сначала самые дешёвые относительно аналогов — в том же порядке качаются фото
        "ORDER BY b.raw_pct IS NULL, b.raw_pct DESC, l.price / l.area").fetchall()
    if limit:
        rows = rows[:limit]
    threads = stage.get("threads", 1)
    log(f"Квартир к оценке: {len(rows)} (провайдер {stage['provider']}, "
        f"модель {stage.get('model')}, потоков: {threads})")

    # Список фото читаем заранее в главном потоке: с базой работает только он.
    tasks = []
    for r in rows:
        photos = [Path(root) / p["path"] for p in con.execute(
            "SELECT path FROM photos WHERE listing_id = ? ORDER BY n", (r["id"],))]
        photos = [p for p in photos if p.exists()]
        if photos:
            tasks.append((r, photos))

    stop = threading.Event()      # поднимается, когда ИИ недоступен (нет входа, кончился лимит)

    def work(r, photos):
        """Работа одного потока: коллажи + запрос к ИИ. В базу не пишет."""
        if stop.is_set():
            return r, photos, None, "остановлено"
        try:
            sheets = make_sheets(photos, photos[0].parent / "sheets")
            prompt = PROMPT.format(facts=_facts(r), description=r["description"] or "текста нет")
            return r, photos, ask(stage, prompt, sheets, ANSWER_SCHEMA), None
        except LLMUnavailable as e:
            stop.set()
            return r, photos, None, f"СТОП: {e}"
        except LLMError as e:
            return r, photos, None, f"пропуск ({e})"

    done = 0
    pool = ThreadPoolExecutor(max_workers=threads)
    for job in as_completed([pool.submit(work, r, photos) for r, photos in tasks]):
        r, photos, a, error = job.result()
        if error:
            if error != "остановлено":
                log(f"  {r['id']}: {error}")
            continue
        contradictions = sum(1 for c in a["claims"] if c["status"] == "contradicted")
        t = a["text_facts"]
        con.execute(
            "INSERT OR REPLACE INTO vision VALUES "
            "(?,?,?,datetime('now','localtime'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], stage["provider"], stage.get("model"), len(photos), a["finish_level"],
             a["confidence"], a["wear"], a["furniture"], a["kitchen_set"],
             ", ".join(a["appliances"]), a["view"], int(a["staged"]), contradictions, a["trust"],
             a["summary"], t["complex"].strip() or None,
             None if t["house_type"] == "unknown" else t["house_type"],
             t["year"] if 1900 < t["year"] < 2035 else None,
             json.dumps(a, ensure_ascii=False)))
        con.commit()
        done += 1
        log(f"  {r['id']}: отделка {a['finish_level']}, доверие {a['trust']}, "
            f"расхождений {contradictions} — {a['summary'][:90]}")
    pool.shutdown()
    log(usage_line())
    if stop.is_set():
        log(f"Готово {done}, остальное продолжится при следующем запуске.")
    return done
