"""Точка входа.

    python run.py           — всё сразу: собрать, посчитать, построить отчёт
    python run.py collect   — только собрать объявления в базу
    python run.py enrich    — дубли, аналоги и скидки
    python run.py photos    — скачать фото (photos 20 — только для 20 объявлений)
    python run.py vision    — оценка фото ИИ (vision 5 — только для 5 квартир)
    python run.py analyst   — вердикт ИИ по лучшим кандидатам (analyst 5 — по 5 в сценарии)
    python run.py report    — построить report.html
    python run.py stats     — краткая сводка по базе

Другой набор настроек: добавьте  --config configs/golden_square.yaml

Всё, что печатается в консоль, дописывается и в журнал data/logs/<настройки>_<команда>.log
с отметкой времени — по нему видно, чем закончился запуск в фоне.
"""
import sys
from datetime import datetime
from pathlib import Path

from krisha import db
from krisha.analyst import analyse
from krisha.benefit import compute as compute_benefit
from krisha.collector import collect
from krisha.enrich import enrich
from krisha.photos import download_photos
from krisha.report import build_report
from krisha.vision import assess
from krisha.config import load_config, ROOT


def cmd_collect(cfg, con):
    stats = collect(cfg, con)
    print("\nИтог прохода:", stats)


def cmd_stats(cfg, con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    print("Объявлений в базе:    ", q("SELECT COUNT(*) FROM listings"))
    print("  активных:           ", q("SELECT COUNT(*) FROM listings WHERE active = 1"))
    print("  конкретных квартир: ", q("SELECT COUNT(*) FROM listings WHERE active = 1 AND is_layout = 0"))
    print("  планировок застр.:  ", q("SELECT COUNT(*) FROM listings WHERE active = 1 AND is_layout = 1"))
    print("Изменений цены:       ", q("SELECT COUNT(*) FROM price_history") - q("SELECT COUNT(*) FROM listings"))


def cmd_enrich(cfg, con):
    enrich(cfg, con)
    compute_benefit(cfg, con)


def cmd_analyst(cfg, con):
    compute_benefit(cfg, con)            # выгода должна учитывать свежие оценки фото
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print("Вердиктов вынесено:", analyse(cfg, con, limit=limit))


def cmd_report(cfg, con):
    compute_benefit(cfg, con)
    build_report(cfg, con, ROOT / cfg["paths"]["report"])


def cmd_photos(cfg, con):
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    total = download_photos(cfg, con, ROOT, limit=limit)
    print("Скачано фото:", total)


def cmd_vision(cfg, con):
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print("Оценено квартир:", assess(cfg, con, ROOT, limit=limit))


def cmd_all(cfg, con):
    """Полный цикл: собрать, посчитать, построить отчёт."""
    stats = collect(cfg, con)
    if stats["found"] == 0:
        print("Ничего не собрано, отчёт не обновляю.")
        return
    enrich(cfg, con)
    compute_benefit(cfg, con)
    build_report(cfg, con, ROOT / cfg["paths"]["report"])


class Tee:
    """Дублирует вывод в файл журнала, перед каждой строкой ставит время."""

    def __init__(self, stream, file):
        self.stream, self.file, self.line_start = stream, file, True

    def write(self, text):
        self.stream.write(text)
        for part in text.splitlines(keepends=True):
            if self.line_start:
                self.file.write(datetime.now().strftime("%d.%m %H:%M:%S  "))
            self.file.write(part)
            self.line_start = part.endswith(chr(10))
        self.file.flush()

    def flush(self):
        self.stream.flush()


def start_log(config_path, name):
    """Открывает журнал запуска и направляет в него копию вывода и ошибок."""
    folder = ROOT / "data" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(config_path).stem if config_path else "config"
    file = open(folder / f"{stem}_{name}.log", "a", encoding="utf-8")
    sys.stdout, sys.stderr = Tee(sys.stdout, file), Tee(sys.stderr, file)
    print(f"=== запуск: {' '.join(sys.argv[1:]) or 'all'}")


COMMANDS = {"all": cmd_all, "collect": cmd_collect, "enrich": cmd_enrich,
            "photos": cmd_photos,
            "vision": cmd_vision, "analyst": cmd_analyst,
            "report": cmd_report, "stats": cmd_stats}

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    config_path = None
    if "--config" in args:                    # другой файл настроек: --config configs/xxx.yaml
        i = args.index("--config")
        config_path = args[i + 1]
        del args[i:i + 2]
    sys.argv = [sys.argv[0], *args]           # команды читают свои числа из sys.argv[2]
    name = args[0] if args else "all"
    if name not in COMMANDS:
        sys.exit(__doc__)
    if name != "stats":
        start_log(config_path, name)
    cfg = load_config(config_path)
    con = db.connect(ROOT / cfg["paths"]["database"])
    COMMANDS[name](cfg, con)
    print("=== готово")
