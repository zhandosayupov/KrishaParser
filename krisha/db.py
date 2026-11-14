"""База данных: один файл SQLite. Здесь таблицы и запись объявлений."""
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY,   -- номер объявления на krisha
    uuid        TEXT,
    url         TEXT,
    is_layout   INTEGER,               -- 1 = планировка от застройщика, не конкретная квартира
    rooms       INTEGER,
    area        REAL,                  -- м²
    floor       INTEGER,
    floors      INTEGER,               -- этажей в доме
    price       INTEGER,               -- тенге
    district    TEXT,
    address     TEXT,
    complex     TEXT,                  -- ЖК
    house_type  TEXT,                  -- монолитный / кирпичный / панельный
    year        INTEGER,               -- год постройки
    condition   TEXT,                  -- состояние со слов продавца
    ceiling     REAL,                  -- высота потолков, м
    bathroom    TEXT,
    furniture   TEXT,
    pledged     INTEGER,               -- 1 = в залоге
    seller_type TEXT,                  -- хозяин / крыша агент / специалист / застройщик
    description TEXT,                  -- начало текста объявления (в списке он обрезан)
    photo_count INTEGER,
    photo_url   TEXT,                  -- первое фото
    posted      TEXT,                  -- дата на сайте, как написано
    first_seen  TEXT,                  -- когда мы впервые увидели объявление
    last_seen   TEXT,                  -- когда видели в последний раз
    active      INTEGER DEFAULT 1      -- 0 = пропало из выдачи (снято или продано)
);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id  INTEGER,
    seen_at     TEXT,
    price       INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    pages       INTEGER,
    found       INTEGER,
    new         INTEGER,
    price_changed INTEGER,
    status      TEXT                   -- ok / blocked / error
);
"""

# Поля, которые обновляются при каждом проходе.
FIELDS = ["uuid", "url", "is_layout", "rooms", "area", "floor", "floors", "price",
          "district", "address", "complex", "house_type", "year", "condition",
          "ceiling", "bathroom", "furniture", "pledged", "seller_type",
          "description", "photo_count", "photo_url", "posted"]


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_listing(con, item, seen_at):
    """Сохраняет объявление. Возвращает 'new', 'price_changed' или 'seen'."""
    row = con.execute("SELECT price FROM listings WHERE id = ?", (item["id"],)).fetchone()
    values = [item.get(f) for f in FIELDS]
    if row is None:
        con.execute(
            f"INSERT INTO listings (id, {', '.join(FIELDS)}, first_seen, last_seen, active) "
            f"VALUES (?, {', '.join('?' * len(FIELDS))}, ?, ?, 1)",
            [item["id"], *values, seen_at, seen_at])
        con.execute("INSERT INTO price_history VALUES (?, ?, ?)",
                    (item["id"], seen_at, item.get("price")))
        return "new"
    con.execute(
        f"UPDATE listings SET {', '.join(f + ' = ?' for f in FIELDS)}, last_seen = ?, active = 1 "
        f"WHERE id = ?", [*values, seen_at, item["id"]])
    if row["price"] != item.get("price"):
        con.execute("INSERT INTO price_history VALUES (?, ?, ?)",
                    (item["id"], seen_at, item.get("price")))
        return "price_changed"
    return "seen"


def mark_missing_inactive(con, seen_at):
    """После ПОЛНОГО прохода: всё, что не встретилось, считаем снятым."""
    cur = con.execute("UPDATE listings SET active = 0 WHERE active = 1 AND last_seen < ?", (seen_at,))
    return cur.rowcount


def load_flats(con):
    """Активные квартиры как словари. Пустые ЖК, тип дома и год дозаполняются тем,
    что ИИ нашёл в тексте и на фото; такие поля перечислены в flat["filled"]."""
    # Колонки x_* есть только в таблице оценок нового формата.
    has_vision = "x_complex" in [c[1] for c in con.execute("PRAGMA table_info(vision)")]
    extra = {}
    if has_vision:
        for v in con.execute("SELECT listing_id, x_complex, x_house_type, x_year FROM vision"):
            extra[v["listing_id"]] = v
    flats = []
    for r in con.execute("SELECT * FROM listings WHERE active = 1 AND is_layout = 0 "
                         "AND price > 0 AND area > 0"):
        d = dict(r)
        d["filled"] = []
        v = extra.get(d["id"])
        if v:
            for field, found in (("complex", v["x_complex"]), ("house_type", v["x_house_type"]),
                                 ("year", v["x_year"])):
                if d[field] in (None, "") and found:
                    d[field] = found
                    d["filled"].append(field)
        flats.append(d)
    return flats
