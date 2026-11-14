"""Аналитик: итоговый вердикт ИИ по лучшим кандидатам.

В отличие от оценки фото, аналитик видит всё сразу: цифры квартиры, аналоги,
расчёт выгоды, результат оценки фото и текст продавца. Картинки ему не нужны,
поэтому запрос короткий и дешёвый. Запускается только для лучших по выгоде квартир.
"""
import json

from .llm import ask, LLMError, LLMUnavailable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyst (
    listing_id INTEGER PRIMARY KEY,
    provider   TEXT,
    model      TEXT,
    created_at TEXT,
    basis      TEXT,     -- по каким цифрам вынесен вердикт (если цена или выгода изменились — пересчитаем)
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

LEVELS = {1: "черновая", 2: "предчистовая", 3: "от застройщика / простой ремонт",
          4: "хороший ремонт", 5: "дизайнерский"}
SCENARIOS = {"renovate": "под ремонт (купить дешевле и вложиться)",
             "ready": "готовая (можно жить без вложений)"}
COMP_LEVELS = {"complex": "тот же ЖК", "nearby": "рядом: тот же район, тип дома, годы, близкая площадь",
               "district": "тот же район, близкая площадь"}

PROMPT = """Ты помогаешь покупателю квартиры в Алматы отобрать объявления для просмотра.
Оцени одно объявление. Опирайся только на данные ниже, ничего не придумывай. Цифры уже посчитаны, не пересчитывай их.

Квартира: {rooms}-комн., {area} м², этаж {floor}/{floors}, {year} г.п., дом: {house_type}, ЖК: {complex}, {district}.
Цена: {price} млн тг ({price_m2} тг/м²). Продавец: {seller}. {pledged}{price_drop}

Сценарий: {scenario}.
Отделка: {level} (источник: {level_source}).
Расчёт: ремонт до хорошего состояния обойдётся в {reno} млн, остающиеся мебель и техника оценены в {equip} млн.
Цена «под ключ»: {ready_m2} тг/м². У аналогов ({comp_level}, {comp_count} шт.) медиана «под ключ»: {comp_ready_m2} тг/м².
Выгода: {benefit} млн тг ({benefit_pct}%). Плюс — дешевле аналогов, минус — дороже.

Оценка фото: {vision}

Текст продавца (обрезан): «{description}»

Ответь:
- verdict: look — стоит ехать смотреть; doubt — выгода есть, но данные ненадёжны или есть серьёзный риск; skip — не стоит времени.
- reason: 1–2 фразы, почему такой вердикт.
- advantage: главное преимущество одной фразой (или «нет»).
- risk: главный риск одной фразой (или «нет»).
Учитывай: мало аналогов или сравнение только по району — слабое основание; опровергнутые фото утверждения продавца,
непоказанные кухня и санузел, залог, рендеры вместо фото — поводы для doubt. Пиши по-русски, коротко.
"""

mln = lambda v: "—" if v is None else f"{v / 1e6:.1f}"


def _vision_text(v):
    if not v:
        return "фото не оценивались, уровень отделки взят со слов продавца."
    raw = json.loads(v["raw"])
    claims = "; ".join(f"«{c['claim']}» — {'подтверждено' if c['status'] == 'confirmed' else 'ОПРОВЕРГНУТО'}"
                       for c in raw["claims"] if c["status"] != "not_visible")
    parts = [v["summary"], f"Износ: {v['wear']}. Мебель: {v['furniture']}. Кухня: {v['kitchen_set']}. "
             f"Техника: {v['appliances'] or 'нет'}. Доверие к объявлению: {v['trust']}/5."]
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
            "JOIN listings l ON l.id = b.listing_id "
            "WHERE m.is_primary = 1 AND l.active = 1 AND b.scenario = ? "
            "AND b.benefit_pct >= ? AND b.level_source = 'photo' "
            "ORDER BY b.benefit_pct DESC LIMIT ?",
            (scenario, cfg["analyst"]["min_benefit_pct"], top))]
    log(f"Кандидатов для аналитика: {len(ids)} (провайдер {stage['provider']}, модель {stage.get('model')})")

    done = 0
    for id_ in ids:
        r = con.execute(
            "SELECT l.*, m.price_m2, b.*, "
            "(SELECT price FROM price_history p WHERE p.listing_id = l.id ORDER BY seen_at LIMIT 1) AS first_price "
            "FROM listings l JOIN metrics m ON m.listing_id = l.id JOIN benefit b ON b.listing_id = l.id "
            "WHERE l.id = ?", (id_,)).fetchone()
        basis = f"{r['price']}|{r['benefit_tg']}|{r['level']}"
        old = con.execute("SELECT basis FROM analyst WHERE listing_id = ?", (id_,)).fetchone()
        if old and old["basis"] == basis:
            continue
        v = con.execute("SELECT * FROM vision WHERE listing_id = ?", (id_,)).fetchone()
        drop = (r["first_price"] or 0) - r["price"]
        prompt = PROMPT.format(
            rooms=r["rooms"], area=r["area"], floor=r["floor"], floors=r["floors"],
            year=r["year"] or "?", house_type=r["house_type"] or "не указан",
            complex=r["complex"] or "не указан", district=r["district"] or "район не указан",
            price=mln(r["price"]), price_m2=r["price_m2"], seller=r["seller_type"],
            pledged="КВАРТИРА В ЗАЛОГЕ. " if r["pledged"] else "",
            price_drop=f"Цена снижена на {mln(drop)} млн с момента появления." if drop > 0 else "",
            scenario=SCENARIOS[r["scenario"]], level=f"{r['level']} — {LEVELS[r['level']]}",
            level_source="фото" if r["level_source"] == "photo" else "слова продавца",
            reno=mln(r["reno_cost"]), equip=mln(r["equip_value"]), ready_m2=r["ready_m2"],
            comp_level=COMP_LEVELS.get(r["comp_level"], "нет"), comp_count=r["comp_count"],
            comp_ready_m2=r["comp_ready_m2"], benefit=mln(r["benefit_tg"]),
            benefit_pct=r["benefit_pct"], vision=_vision_text(v),
            description=r["description"] or "текста нет")
        try:
            a = ask(stage, prompt, (), ANSWER_SCHEMA)
        except LLMUnavailable as e:
            log(f"СТОП: {e}. Готово {done}, остальное продолжится при следующем запуске.")
            return done
        except LLMError as e:
            log(f"  {id_}: пропуск ({e})")
            continue
        con.execute(
            "INSERT OR REPLACE INTO analyst VALUES (?,?,?,datetime('now','localtime'),?,?,?,?,?)",
            (id_, stage["provider"], stage.get("model"), basis,
             a["verdict"], a["reason"], a["advantage"], a["risk"]))
        con.commit()
        done += 1
        log(f"  {id_}: {a['verdict']} — {a['reason'][:100]}")
    return done
