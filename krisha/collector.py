"""Сборщик: проходит страницы списка krisha.kz и складывает объявления в базу.

Правила вежливости: один поток, случайные паузы, при блокировке — стоп.
Проверку на бота не обходим: сохраняем прогресс и выходим.
"""
import random
import time

import requests

from . import db
from .config import parse_search_url, build_list_url
from .parse import parse_list_page, total_count, is_blocked, BlockedError

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept-Language": "ru,en;q=0.8",
}


def collect(cfg, con, log=print):
    """Один проход по всем страницам списка. Возвращает сводку прохода."""
    c = cfg["collector"]
    base, params = parse_search_url(cfg["search_url"])
    started = db.now()
    stats = {"pages": 0, "found": 0, "new": 0, "price_changed": 0, "status": "ok"}
    seen_ids = set()
    total = None

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        for page in range(1, c["max_pages"] + 1):
            url = build_list_url(base, params, page)
            resp = session.get(url, timeout=c["timeout_sec"])
            if is_blocked(resp.text, resp.status_code):
                raise BlockedError(f"страница {page}: код {resp.status_code}")
            resp.raise_for_status()

            items = parse_list_page(resp.text)
            if total is None:
                total = total_count(resp.text)
                log(f"Всего объявлений по фильтру: {total}")

            fresh = [it for it in items if it["id"] not in seen_ids]
            for it in fresh:
                seen_ids.add(it["id"])
                result = db.upsert_listing(con, it, started)
                if result in stats:
                    stats[result] += 1
            con.commit()
            stats["pages"] = page
            stats["found"] = len(seen_ids)
            log(f"  стр. {page}: {len(items)} на странице, новых для прохода {len(fresh)}, "
                f"всего {len(seen_ids)}")

            # Конец: пустая страница, либо на ней только уже виденные (закреплённые) объявления.
            if not items or not fresh:
                break
            time.sleep(random.uniform(c["pause_min_sec"], c["pause_max_sec"]))
    except BlockedError as e:
        stats["status"] = "blocked"
        log(f"СТОП: сайт показал проверку на бота ({e}). Прогресс сохранён, повторите позже.")
    except requests.RequestException as e:
        stats["status"] = "error"
        log(f"СТОП: ошибка сети ({e}). Прогресс сохранён.")

    # Снятыми помечаем только после полного успешного прохода.
    if stats["status"] == "ok":
        stats["gone"] = db.mark_missing_inactive(con, started)

    con.execute(
        "INSERT INTO runs (started_at, finished_at, pages, found, new, price_changed, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (started, db.now(), stats["pages"], stats["found"], stats["new"],
         stats["price_changed"], stats["status"]))
    con.commit()
    stats["total_on_site"] = total
    return stats
