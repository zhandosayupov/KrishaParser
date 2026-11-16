"""Единая точка обращения к ИИ. Остальной код знает только функцию ask().

Провайдер выбирается в config.yaml отдельно для каждого этапа:

  claude_cli         — Claude Code в режиме командной строки, вход по подписке,
                       API-ключ не нужен. Требует один раз выполнить `claude auth login`.
  openai_compatible  — любой сервис с OpenAI-совместимым API: Gemini, Groq,
                       OpenRouter, локальная Ollama. Адрес, модель и имя переменной
                       с ключом задаются в конфиге.

Чтобы добавить провайдера: написать функцию _ask_<имя>(stage, prompt, images, schema)
и вписать её в PROVIDERS внизу файла.
"""
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import requests


class LLMError(Exception):
    """ИИ не ответил или ответил не по форме."""


class LLMUnavailable(LLMError):
    """Продолжать бессмысленно: нет входа, нет ключа, исчерпан лимит."""


# Счётчик расхода за запуск. Стоимость условная: по подписке деньги не списываются,
# но по ней видно, сколько лимита съел этап.
USAGE = {"calls": 0, "tokens": 0, "usd": 0.0}


def usage_line():
    tokens = f"{USAGE['tokens']:,}".replace(",", " ")
    return f"Запросов к ИИ: {USAGE['calls']}, токенов: {tokens}, условная стоимость: ${USAGE['usd']:.2f}"


# ---------- Claude Code CLI (подписка, без API-ключа) ----------

def _find_claude(stage):
    path = stage.get("claude_path") or shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
    if not Path(path).exists():
        raise LLMUnavailable("не найден claude: укажите claude_path в config.yaml")
    return path


def _ask_claude_cli(stage, prompt, images, schema):
    # Картинки кладём прямо в запрос: так ответ приходит за один ход. Если давать Claude пути
    # к файлам, он открывает их инструментом в несколько ходов, и лимит уходит в разы быстрее.
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(Path(p).read_bytes()).decode()}}
               for p in images]
    content.append({"type": "text", "text": prompt})
    message = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    cmd = [_find_claude(stage), "-p",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", stage.get("model", "sonnet"),
           "--json-schema", json.dumps(schema, ensure_ascii=False),
           "--no-session-persistence", "--tools", "",
           # Без этих ключей каждый запрос несёт с собой большую инструкцию Claude Code, плагины
           # и подключения пользователя.
           "--system-prompt", "Ты оцениваешь объявления о продаже квартир и отвечаешь строго по заданной схеме.",
           "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", ""]
    # Убираем ключи из окружения: иначе запрос молча уйдёт на платный API, а не на подписку.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    # «Размышления» модели съедают в несколько раз больше лимита, чем сам ответ.
    # Включить: thinking_tokens в настройках этапа в config.yaml.
    env["MAX_THINKING_TOKENS"] = str(stage.get("thinking_tokens", 0))
    try:
        proc = subprocess.run(cmd, input=message + chr(10), capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=stage.get("timeout_sec", 300))
    except subprocess.TimeoutExpired:
        raise LLMError("claude не ответил за отведённое время")
    # Вывод — поток строк JSON; итог лежит в строке с type = result.
    out = None
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            out = item
    if out is None:
        raise LLMError(f"непонятный ответ claude: {(proc.stdout or proc.stderr)[:300]}")

    u = out.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["tokens"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens", "output_tokens"))
    USAGE["usd"] += out.get("total_cost_usd") or 0
    text = str(out.get("result", ""))
    if out.get("is_error"):
        low = text.lower()
        if "not logged in" in low or "/login" in low:
            raise LLMUnavailable("в Claude Code не выполнен вход. Выполните в терминале: claude auth login")
        if "limit" in low or "quota" in low:
            raise LLMUnavailable(f"лимит подписки исчерпан: {text[:200]}")
        raise LLMError(f"claude вернул ошибку: {text[:300]}")
    if isinstance(out.get("structured_output"), dict):
        return out["structured_output"]
    return _json_from_text(text)


# ---------- OpenAI-совместимые сервисы (в том числе бесплатные) ----------

def _ask_openai_compatible(stage, prompt, images, schema):
    key_env = stage.get("api_key_env", "")
    key = os.environ.get(key_env, "") if key_env else ""
    if key_env and not key:
        raise LLMUnavailable(f"не задана переменная окружения {key_env} с ключом")
    content = [{"type": "text", "text": prompt + "\n\nОтветь одним JSON-объектом строго по этой схеме:\n"
                + json.dumps(schema, ensure_ascii=False)}]
    for p in images:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    resp = requests.post(
        stage["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"} if key else {},
        json={"model": stage["model"], "messages": [{"role": "user", "content": content}],
              "response_format": {"type": "json_object"}},
        timeout=stage.get("timeout_sec", 300))
    if resp.status_code in (401, 403):
        raise LLMUnavailable(f"сервис отклонил ключ ({resp.status_code})")
    if resp.status_code == 429:
        raise LLMUnavailable("лимит запросов сервиса исчерпан, продолжите позже")
    if resp.status_code != 200:
        raise LLMError(f"сервис ответил {resp.status_code}: {resp.text[:300]}")
    return _json_from_text(resp.json()["choices"][0]["message"]["content"])


# ---------- общее ----------

def _json_from_text(text):
    """Достаёт JSON-объект из текста ответа (модели иногда оборачивают его в ```)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"в ответе нет JSON: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"битый JSON в ответе: {e}")


def check_answer(answer, schema):
    """Простая проверка формы: все обязательные поля на месте, перечисления соблюдены."""
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in answer:
            raise LLMError(f"в ответе нет поля {name}")
    for name, value in answer.items():
        allowed = props.get(name, {}).get("enum")
        if allowed and value not in allowed:
            raise LLMError(f"поле {name}: недопустимое значение {value!r}")


PROVIDERS = {
    "claude_cli": _ask_claude_cli,
    "openai_compatible": _ask_openai_compatible,
}


def ask(stage, prompt, images=(), schema=None, retries=1):
    """Задаёт вопрос ИИ и возвращает словарь по схеме.

    stage — настройки этапа из config.yaml (provider, model, ...).
    При ответе не по форме делает повтор; LLMUnavailable пробрасывает сразу.
    """
    provider = PROVIDERS.get(stage["provider"])
    if provider is None:
        raise LLMUnavailable(f"неизвестный провайдер {stage['provider']!r}; есть: {', '.join(PROVIDERS)}")
    last = None
    for _ in range(retries + 1):
        try:
            answer = provider(stage, prompt, list(images), schema or {})
            check_answer(answer, schema or {})
            return answer
        except LLMUnavailable:
            raise
        except LLMError as e:
            last = e
    raise last
