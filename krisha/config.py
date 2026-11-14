"""Чтение config.yaml и разбор ссылки поиска krisha.kz."""
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, quote

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Параметры, которые относятся к положению карты, а не к фильтрам.
MAP_ONLY_PARAMS = {"zoom", "lat", "lon", "page"}


def load_config(path=None):
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_search_url(url):
    """Превращает ссылку (с карты или из списка) в адрес списка и набор фильтров.

    Возвращает (base, params): base — адрес страницы списка без параметров,
    params — список пар (имя, значение) в исходном порядке.
    """
    parts = urlsplit(url.strip())
    path = parts.path
    if path.startswith("/map/"):
        path = path[len("/map"):]          # /map/prodazha/... -> /prodazha/...
    if not path.endswith("/"):
        path += "/"
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k not in MAP_ONLY_PARAMS]
    base = f"https://{parts.netloc or 'krisha.kz'}{path}"
    return base, params


def build_list_url(base, params, page=1):
    """Собирает адрес страницы списка с номером страницы."""
    pairs = list(params)
    if page > 1:
        pairs.append(("page", str(page)))
    # Скобки и запятые оставляем как есть — так делает сам сайт.
    query = "&".join(f"{quote(k, safe='[].')}={quote(v, safe=',.')}" for k, v in pairs)
    return f"{base}?{query}" if query else base


def polygon_from_params(params):
    """Достаёт границы поиска из параметра areas: список точек (широта, долгота)."""
    for k, v in params:
        if k == "areas" and v.startswith("p"):
            nums = [float(x) for x in v[1:].split(",") if x]
            return list(zip(nums[0::2], nums[1::2]))
    return []
