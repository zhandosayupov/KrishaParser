"""Скачивание фото объявлений напрямую с фотосервера krisha (в карточки не заходим).

Адрес фото: <папка объявления>/<номер>-<размер>.jpg. Номера идут не подряд,
и на сервере остаются старые удалённые фото. Поэтому:
  1) от номера первого фото идём вверх, пока не встретим несколько пропусков подряд;
  2) если фото меньше, чем заявлено в объявлении, идём вниз;
  3) берём самые свежие (с наибольшими номерами) — столько, сколько заявлено.
"""
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


_local = threading.local()        # у каждого потока своё соединение с фотосервером
_stop = threading.Event()         # поднимается, когда сервер начал отказывать — все потоки сворачиваются


def _session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
        _local.session.headers["Referer"] = "https://krisha.kz/"
    return _local.session


def fetch_listing(listing_id, photo_url, photo_count, c, root):
    """Работа одного потока: найти и скачать фото одного объявления.

    Возвращает (id, photo_url, [(номер, путь)], ошибка). В базу не пишет — это делает главный поток.
    """
    if _stop.is_set():
        return listing_id, photo_url, [], "остановлено"
    m = re.search(r"^(.*)/(\d+)-full\.\w+$", photo_url)
    if not m:
        return listing_id, photo_url, [], None
    folder, first = m.group(1), int(m.group(2))
    session, saved = _session(), []
    try:
        numbers = find_photo_numbers(session, folder, first, photo_count or 1, c)
        numbers = pick_evenly(numbers, first, c["max_per_listing"])
        out_dir = Path(root) / c["dir"] / str(listing_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        for n in numbers:
            file = out_dir / f"{n}.jpg"
            if not file.exists():                 # уже скачано раньше (например, другим конфигом)
                resp = session.get(f"{folder}/{n}-{c['size']}.jpg", timeout=30)
                if resp.status_code in (403, 429):
                    raise RuntimeError(f"фотосервер ответил {resp.status_code}")
                if resp.status_code != 200:
                    continue
                file.write_bytes(resp.content)
                _pause(c)
            saved.append((n, file.relative_to(root).as_posix()))
    except (requests.RequestException, RuntimeError) as e:
        _stop.set()
        return listing_id, photo_url, [], str(e)
    return listing_id, photo_url, saved, None


def download_photos(cfg, con, root, log=print, limit=None):
    """Качает фото для уникальных квартир, у которых их ещё нет, в несколько потоков.

    Порядок: сначала квартиры с наибольшей скидкой к аналогам. Можно прерывать и продолжать.
    """
    c = cfg["photos"]
    con.executescript(SCHEMA)
    rows = con.execute(
        "SELECT l.id, l.photo_url, l.photo_count FROM listings l "
        "JOIN metrics m ON m.listing_id = l.id "
        "LEFT JOIN benefit b ON b.listing_id = l.id "
        "LEFT JOIN photo_status s ON s.listing_id = l.id "
        "WHERE l.active = 1 AND m.is_primary = 1 AND l.photo_url IS NOT NULL "
        "AND (s.listing_id IS NULL OR s.photo_url <> l.photo_url) "
        "ORDER BY b.raw_pct IS NULL, b.raw_pct DESC").fetchall()
    if limit:
        rows = rows[:limit]
    threads = c.get("threads", 1)
    log(f"Объявлений без скачанных фото: {len(rows)}, потоков: {threads}")

    _stop.clear()
    total = done = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        jobs = [pool.submit(fetch_listing, r["id"], r["photo_url"], r["photo_count"], c, root)
                for r in rows]
        for job in as_completed(jobs):
            listing_id, photo_url, saved, error = job.result()
            if error:
                if error != "остановлено":
                    log(f"СТОП на объявлении {listing_id}: {error}. Прогресс сохранён, запустите позже.")
                continue
            con.execute("DELETE FROM photos WHERE listing_id = ?", (listing_id,))
            con.executemany("INSERT OR REPLACE INTO photos VALUES (?, ?, ?)",
                            [(listing_id, n, path) for n, path in saved])
            con.execute("INSERT OR REPLACE INTO photo_status VALUES (?, ?, ?, datetime('now', 'localtime'))",
                        (listing_id, photo_url, len(saved)))
            con.commit()
            total += len(saved)
            done += 1
            if done % 50 == 0 or done == len(rows):
                log(f"  {done}/{len(rows)} объявлений, фото: {total}")
    return total
