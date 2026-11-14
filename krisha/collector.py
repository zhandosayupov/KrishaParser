"""Сборщик: проходит страницы списка krisha.kz и складывает объявления в базу.

Правила вежливости: один поток, случайные паузы, при блокировке — стоп.
Проверку на бота не обходим: сохраняем прогресс и выходим.

Сайт показывает не больше 1000 страниц на один поиск (около 20 000 объявлений).
Если объявлений больше, поиск автоматически делится на части по цене.
"""
import random
import time

import requests

from . import db
from .config import search_params, build_list_url
from .parse import parse_list_page, total_count, is_blocked, BlockedError

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept-Language": "ru,en;q=0.8",
}
SITE_LIMIT = 19000          # больше этого в один поиск не помещается — делим по цене
PRICE_FROM, PRICE_TO = "das[price][from]", "das[price][to]"


def collect(cfg, con, log=print):
    """Один проход по всем страницам списка. Возвращает сводку прохода."""
    c = cfg["collector"]
    base, params = search_params(cfg)
    started = db.now()
    stats = {"pages": 0, "found": 0, "new": 0, "price_changed": 0, "status": "ok"}
    seen_ids = set()

    session = requests.Session()
    session.headers.update(HEADERS)

    def fetch(page_params, page):
        resp = session.get(build_list_url(base, page_params, page), timeout=c["timeout_sec"])
        if is_blocked(resp.text, resp.status_code):
            raise BlockedError(f"страница {page}: код {resp.status_code}")
        resp.raise_for_status()
        stats["pages"] += 1
        time.sleep(random.uniform(c["pause_min_sec"], c["pause_max_sec"]))
        return resp.text

    def save(html):
        """Записывает объявления со страницы. Возвращает (всего на странице, новых для прохода)."""
        items = parse_list_page(html)
        fresh = [it for it in items if it["id"] not in seen_ids]
        for it in fresh:
            seen_ids.add(it["id"])
            result = db.upsert_listing(con, it, started)
            if result in stats:
                stats[result] += 1
        con.commit()
        stats["found"] = len(seen_ids)
        return len(items), len(fresh)

    def walk(lo, hi):
        """Собирает объявления с ценой от lo до hi; если их слишком много — делит диапазон пополам."""
        part = [(k, v) for k, v in params if k not in (PRICE_FROM, PRICE_TO)]
        part = [(PRICE_FROM, str(lo)), (PRICE_TO, str(hi))] + part
        html = fetch(part, 1)
        total = total_count(html) or 0
        if total > SITE_LIMIT and hi - lo > 1_000_000:
            mid = (lo + hi) // 2
            log(f"Цена {lo / 1e6:.0f}–{hi / 1e6:.0f} млн: {total} объявлений, делю на части")
            walk(lo, mid)
            walk(mid + 1, hi)
            return
        log(f"Цена {lo / 1e6:.1f}–{hi / 1e6:.1f} млн: {total} объявлений")
        page = 1
        while True:
            on_page, fresh = save(html)
            if page == 1 or page % 25 == 0:
                log(f"  стр. {page}: всего собрано {len(seen_ids)}")
            # Конец: пустая страница, либо на ней только уже виденные (закреплённые) объявления.
            if not on_page or not fresh or page >= c["max_pages"]:
                break
            page += 1
            html = fetch(part, page)

    try:
        given = dict(params)
        # Без верхней границы цены берём заведомо большую.
        walk(int(given.get(PRICE_FROM, 0)), int(given.get(PRICE_TO, 10_000_000_000)))
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
    return stats
