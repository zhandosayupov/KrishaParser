"""Скачивание фото объявлений напрямую с фотосервера krisha (в карточки не заходим).

Адрес фото: <папка объявления>/<номер>-<размер>.jpg. Номера идут не подряд,
и на сервере остаются старые удалённые фото. Поэтому:
  1) от номера первого фото идём вверх, пока не встретим несколько пропусков подряд;
  2) если фото меньше, чем заявлено в объявлении, идём вниз;
  3) берём самые свежие (с наибольшими номерами) — столько, сколько заявлено.
"""
import random
import re
import time
from pathlib import Path

import requests

from .collector import HEADERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    listing_id INTEGER,
    n          INTEGER,        -- номер фото на сервере
    path       TEXT,           -- путь к файлу относительно папки проекта
    PRIMARY KEY (listing_id, n)
);
CREATE TABLE IF NOT EXISTS photo_status (
    listing_id INTEGER PRIMARY KEY,
    photo_url  TEXT,           -- по какому первому фото искали (изменилось -> ищем заново)
    found      INTEGER,
    done_at    TEXT
);
"""


def _pause(c):
    time.sleep(random.uniform(c["pause_min_sec"], c["pause_max_sec"]))


def find_photo_numbers(session, folder, first, wanted, c):
    """Возвращает номера действующих фото объявления (не больше wanted)."""
    size = c["size"]
    exists = lambda n: session.head(f"{folder}/{n}-{size}.jpg", timeout=20).status_code == 200
    found = [first]

    misses, n = 0, first + 1                      # шаг 1: вверх
    while misses < c["stop_after_misses"]:
        if exists(n):
            found.append(n)
            misses = 0
        else:
            misses += 1
        n += 1
        _pause(c)

    n = first - 1                                 # шаг 2: вниз, если не хватает
    while len(found) < wanted and n >= 1:
        if exists(n):
            found.append(n)
        n -= 1
        _pause(c)

    return sorted(found)[-wanted:]                # шаг 3: самые свежие


def pick_evenly(numbers, first, limit):
    """Если фото больше лимита — берём главное и остальные равномерно по всему набору."""
    if len(numbers) <= limit:
        return numbers
    step = (len(numbers) - 1) / (limit - 1)
    picked = {numbers[round(i * step)] for i in range(limit)}
    if first in numbers and first not in picked:
        picked.discard(max(picked))
        picked.add(first)
    return sorted(picked)


def download_photos(cfg, con, root, log=print, limit=None):
    """Качает фото для уникальных квартир, у которых их ещё нет. Можно прерывать и продолжать."""
    c = cfg["photos"]
    con.executescript(SCHEMA)
    rows = con.execute(
        "SELECT l.id, l.photo_url, l.photo_count FROM listings l "
        "JOIN metrics m ON m.listing_id = l.id "
        "LEFT JOIN photo_status s ON s.listing_id = l.id "
        "WHERE l.active = 1 AND m.is_primary = 1 AND l.photo_url IS NOT NULL "
        "AND (s.listing_id IS NULL OR s.photo_url <> l.photo_url) "
        "ORDER BY l.price / l.area").fetchall()         # сначала самые дешёвые за м²
    if limit:
        rows = rows[:limit]
    log(f"Объявлений без скачанных фото: {len(rows)}")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = "https://krisha.kz/"
    total = 0

    for i, r in enumerate(rows, start=1):
        m = re.search(r"^(.*)/(\d+)-full\.\w+$", r["photo_url"])
        if not m:
            continue
        folder, first = m.group(1), int(m.group(2))
        try:
            numbers = find_photo_numbers(session, folder, first, r["photo_count"] or 1, c)
            numbers = pick_evenly(numbers, first, c["max_per_listing"])
            out_dir = Path(root) / c["dir"] / str(r["id"])
            out_dir.mkdir(parents=True, exist_ok=True)
            con.execute("DELETE FROM photos WHERE listing_id = ?", (r["id"],))
            for n in numbers:
                resp = session.get(f"{folder}/{n}-{c['size']}.jpg", timeout=30)
                if resp.status_code in (403, 429):
                    raise RuntimeError(f"фотосервер ответил {resp.status_code}")
                if resp.status_code != 200:
                    continue
                file = out_dir / f"{n}.jpg"
                file.write_bytes(resp.content)
                con.execute("INSERT OR REPLACE INTO photos VALUES (?, ?, ?)",
                            (r["id"], n, file.relative_to(root).as_posix()))
                total += 1
                _pause(c)
        except (requests.RequestException, RuntimeError) as e:
            con.commit()
            log(f"СТОП на объявлении {r['id']}: {e}. Прогресс сохранён, запустите ещё раз позже.")
            return total
        con.execute("INSERT OR REPLACE INTO photo_status VALUES (?, ?, ?, datetime('now', 'localtime'))",
                    (r["id"], r["photo_url"], len(numbers)))
        con.commit()
        if i % 10 == 0 or i == len(rows):
            log(f"  {i}/{len(rows)} объявлений, фото скачано: {total}")
    return total
