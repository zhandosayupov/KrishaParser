"""Аналитик: итоговый вердикт ИИ по лучшим кандидатам.

В отличие от оценки фото, аналитик видит всё сразу: цифры квартиры, сравнение с аналогами
тремя способами, расчёт выгоды, результат оценки фото и текст продавца. Картинки ему не нужны,
поэтому запрос короткий и дешёвый. Запускается только для лучших по выгоде квартир.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .db import load_flats
from .llm import ask, usage_line, LLMError, LLMUnavailable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyst (
    listing_id INTEGER PRIMARY KEY,
    provider   TEXT,
    model      TEXT,
    created_at TEXT,
    basis      TEXT,     -- по каким цифрам вынесен вердикт (изменились — пересчитаем)
    verdict    TEXT,     -- look (стоит смотреть) / doubt (сомнительно) / skip (мимо)
    reason     TEXT,
    advantage  TEXT,
    risk       TEXT
);
"""

ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "reason", "advantage", "risk"],
    "properties": {
        "verdict": {"type": "string", "enum": ["look", "doubt", "skip"]},
        "reason": {"type": "string"},
        "advantage": {"type": "string"},
        "risk": {"type": "string"},
    },
}

LEVELS = {1: "без отделки", 2: "черновая", 3: "предчистовая", 4: "убитая", 5: "«бабушкин» ремонт",
          6: "простая отделка", 7: "аккуратный не новый ремонт", 8: "хороший современный ремонт",
          9: "очень хороший ремонт", 10: "премиум"}
SCENARIOS = {"renovate": "под ремонт (купить дешевле и вложиться)",
             "ready": "готовая (можно жить без вложений)"}

PROMPT = """Ты помогаешь покупателю квартиры в Алматы отобрать объявления для просмотра.
Оцени одно объявление. Опирайся только на данные ниже, ничего не придумывай. Цифры уже посчитаны, не пересчитывай их.

Квартира: {rooms}-комн., {area} м², этаж {floor}/{floors}, {year} г.п., дом: {house_type}, ЖК: {complex}, {district}, адрес: {address}.
Цена: {price} млн тг ({price_m2} тг/м²). Продавец: {seller}. {flags}

Сценарий: {scenario}.
Отделка: {level} из 10 — {level_name} (источник: {level_source}).
Материалы на ремонт до хорошего состояния: {reno} млн. Остающиеся мебель и нужная техника: {equip} млн.
Цена «под ключ»: {ready_m2} тг/м².

Сравнение с аналогами. Процент — насколько квартира дешевле медианы (минус — дороже); «—» значит аналогов мало.
- тот же ЖК: по цене объявления {raw_complex}, «под ключ» {ready_complex} (аналогов: {n_complex});
- окружение (тот же район, тип дома и годы постройки): {raw_nearby}, «под ключ» {ready_nearby} (аналогов: {n_nearby});
- весь район: {raw_district}, «под ключ» {ready_district} (аналогов: {n_district}).
Итоговая выгода (самая осторожная оценка «под ключ»): {benefit} млн тг ({benefit_pct}%).
{complex_note}

Оценка фото: {vision}

Текст продавца (обрезан): «{description}»

Ответь:
- verdict: look — стоит ехать смотреть; doubt — выгода есть, но данные ненадёжны или есть серьёзный риск; skip — не стоит времени.
- reason: 1–2 фразы, почему такой вердикт.
- advantage: главное преимущество одной фразой (или «нет»).
- risk: главный риск одной фразой (или «нет»).
Как судить:
- если три способа сравнения сильно расходятся или сработал только один — выгода ненадёжна;
- если сам ЖК заметно дешевле района, низкая цена — свойство ЖК, а не удача;
- опровергнутые фото утверждения продавца, непоказанные кухня и санузел, залог, рендеры вместо фото — поводы для doubt;
- если адрес, ЖК, район и текст противоречат друг другу, отметь это как риск;
- «срочно» и «торг» — рычаг для торга, это плюс;
- одной непоказанной комнаты при хороших остальных данных недостаточно для doubt.
Пиши по-русски, коротко.
"""

mln = lambda v: "—" if v is None else f"{v / 1e6:.1f}"
pct = lambda v: "—" if v is None else f"{v:+.0f}%"


def _vision_text(v):
    if not v:
        return "фото не оценивались, уровень отделки взят со слов продавца."
    raw = json.loads(v["raw"])
    claims = "; ".join(f"«{c['claim']}» — {'подтверждено' if c['status'] == 'confirmed' else 'ОПРОВЕРГНУТО'}"
                       for c in raw["claims"] if c["status"] != "not_visible")
    parts = [v["summary"], f"Износ: {v['wear']}. Мебель: {v['furniture']}. Кухня: {v['kitchen_set']}. "
             f"Техника: {v['appliances'] or 'нет'}. Вид из окна: {v['view']}. "
             f"Доверие к объявлению: {v['trust']}/5."]
    if raw["rooms_not_shown"]:
        parts.append("Не показаны: " + ", ".join(raw["rooms_not_shown"]) + ".")
    if v["staged"]:
        parts.append("Фото похожи на рендер.")
    if claims:
        parts.append("Утверждения продавца: " + claims + ".")
    return " ".join(parts)


def analyse(cfg, con, log=print, limit=None):
    """Выносит вердикты по лучшим кандидатам каждого сценария. Готовые вердикты не пересчитывает."""
    stage = cfg["llm"]["analyst"]
    top = limit or cfg["analyst"]["top_per_scenario"]
    con.executescript(SCHEMA_SQL)

    ids = []
    for scenario in ("renovate", "ready"):
        ids += [r[0] for r in con.execute(
            "SELECT b.listing_id FROM benefit b JOIN metrics m ON m.listing_id = b.listing_id "
            "WHERE m.is_primary = 1 AND b.scenario = ? AND b.benefit_pct >= ? "
            "AND b.level_source = 'photo' ORDER BY b.benefit_pct DESC LIMIT ?",
            (scenario, cfg["analyst"]["min_benefit_pct"], top))]
    log(f"Кандидатов для аналитика: {len(ids)} (провайдер {stage['provider']}, модель {stage.get('model')})")

    flats = {f["id"]: f for f in load_flats(con)}
    # Запросы готовим заранее в главном потоке: с базой работает только он.
    tasks = []
    for id_ in ids:
        f = flats[id_]
        b = con.execute("SELECT * FROM benefit WHERE listing_id = ?", (id_,)).fetchone()
        m = con.execute("SELECT * FROM metrics WHERE listing_id = ?", (id_,)).fetchone()
        v = con.execute("SELECT * FROM vision WHERE listing_id = ?", (id_,)).fetchone()
        basis = f"{f['price']}|{b['benefit_pct']}|{b['level']}"
        old = con.execute("SELECT basis FROM analyst WHERE listing_id = ?", (id_,)).fetchone()
        if old and old["basis"] == basis:
            continue
        first = con.execute("SELECT price FROM price_history WHERE listing_id = ? ORDER BY seen_at LIMIT 1",
                            (id_,)).fetchone()
        drop = (first["price"] if first else f["price"]) - f["price"]
        flags = " ".join(filter(None, [
            "КВАРТИРА В ЗАЛОГЕ." if f["pledged"] else "",
            "В тексте есть «срочно»." if m["urgent"] else "",
            "В тексте есть «торг»." if m["bargain"] else "",
            f"Цена снижена на {mln(drop)} млн с момента появления." if drop > 0 else ""]))
        cvd = b["complex_vs_district"]
        complex_note = "" if cvd is None else (
            f"Сам ЖК в среднем {'дешевле' if cvd > 0 else 'дороже'} района на {abs(cvd):.0f}%.")
        prompt = PROMPT.format(
            rooms=f["rooms"], area=f["area"], floor=f["floor"], floors=f["floors"],
            year=f["year"] or "?", house_type=f["house_type"] or "не указан",
            complex=f["complex"] or "не указан", district=f["district"] or "район не указан",
            address=f["address"] or "не указан", price=mln(f["price"]), price_m2=m["price_m2"],
            seller=f["seller_type"], flags=flags, scenario=SCENARIOS[b["scenario"]],
            level=b["level"], level_name=LEVELS[b["level"]],
            level_source="фото" if b["level_source"] == "photo" else "слова продавца",
            reno=mln(b["reno_cost"]), equip=mln(b["equip_value"]), ready_m2=b["ready_m2"],
            raw_complex=pct(b["raw_complex"]), ready_complex=pct(b["ready_complex"]), n_complex=b["n_complex"],
            raw_nearby=pct(b["raw_nearby"]), ready_nearby=pct(b["ready_nearby"]), n_nearby=b["n_nearby"],
            raw_district=pct(b["raw_district"]), ready_district=pct(b["ready_district"]),
            n_district=b["n_district"], benefit=mln(b["benefit_tg"]), benefit_pct=b["benefit_pct"],
            complex_note=complex_note, vision=_vision_text(v),
            description=f["description"] or "текста нет")
        tasks.append((id_, basis, prompt))

    stop = threading.Event()      # поднимается, когда ИИ недоступен (нет входа, кончился лимит)

    def work(id_, basis, prompt):
        """Работа одного потока: только запрос к ИИ. В базу не пишет."""
        if stop.is_set():
            return id_, basis, None, "остановлено"
        try:
            return id_, basis, ask(stage, prompt, (), ANSWER_SCHEMA), None
        except LLMUnavailable as e:
            stop.set()
            return id_, basis, None, f"СТОП: {e}"
        except LLMError as e:
            return id_, basis, None, f"пропуск ({e})"

    done = 0
    pool = ThreadPoolExecutor(max_workers=stage.get("threads", 1))
    for job in as_completed([pool.submit(work, *t) for t in tasks]):
        id_, basis, a, error = job.result()
        if error:
            if error != "остановлено":
                log(f"  {id_}: {error}")
            continue
        con.execute(
            "INSERT OR REPLACE INTO analyst VALUES (?,?,?,datetime('now','localtime'),?,?,?,?,?)",
            (id_, stage["provider"], stage.get("model"), basis,
             a["verdict"], a["reason"], a["advantage"], a["risk"]))
        con.commit()
        done += 1
        log(f"  {id_}: {a['verdict']} — {a['reason'][:100]}")
    pool.shutdown()
    if stop.is_set():
        log(f"Готово {done}, остальное продолжится при следующем запуске.")
    log(usage_line())
    return done
