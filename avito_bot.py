#!/usr/bin/env python3
"""Avito -> Telegram: уведомления о новых объявлениях.

Запуск:
  python avito_bot.py --once     один проход (GitHub Actions, cron)
  python avito_bot.py            бесконечный цикл (ПК, старый телефон, сервер)
  python avito_bot.py --ping     проверить, что Telegram-токен и chat id верные

Что умеет:
  * несколько поисков в searches.json (или командами в самом Telegram)
  * фильтры: мин/макс цена, слова "обязательно" и "исключить"
  * не шлёт дубли (state.json), сообщает о снижении цены
  * при первом запуске нового поиска ничего не спамит, только запоминает
  * если Авито блокирует IP, пишет об этом в Telegram, а не молчит
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:  # маскируется под настоящий браузер (TLS-отпечаток)
    from curl_cffi import requests as http

    HAVE_CURL = True
except ImportError:  # запасной вариант, блокировок будет больше
    import requests as http

    HAVE_CURL = False

# Какой браузер изображаем. По тесту diagnose.py у автора проекта прошёл firefox;
# если Авито перестанет пускать, запустите diagnose.py и смените значение в .env
IMPERSONATE = os.getenv("AVITO_IMPERSONATE", "firefox")
HOME = "https://www.avito.ru/"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}
BLOCK_CODES = (403, 429, 439)
SESSION_TTL = 1800            # раз в 30 минут начинаем "новую сессию"
_sess: dict = {"obj": None, "born": 0.0}

BASE = Path(__file__).resolve().parent
SEARCHES_FILE = BASE / "searches.json"
STATE_FILE = BASE / "state.json"

MAX_SEND_PER_SEARCH = 10      # максимум сообщений по одному поиску за проход
SEEN_LIMIT = 2000             # сколько id помнить на один поиск
PAUSE_BETWEEN_SEARCHES = (3.0, 8.0)
BLOCK_MARKERS = (
    "доступ с вашего ip",
    "доступ ограничен",
    "проблема с ip",
    "firewall",
    "captcha",
)

log = logging.getLogger("avito")


class Blocked(Exception):
    """Авито не пустил (429/403/капча)."""


# ---------------------------------------------------------------- файлы


def load_env() -> None:
    """Подхватывает переменные из файла .env (если он есть)."""
    env = BASE / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- Telegram


def tg(token: str, method: str, **payload) -> dict:
    r = http.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30
    )
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def send_text(token: str, chat_id: str, text: str) -> None:
    res = tg(
        token, "sendMessage", chat_id=chat_id, text=text,
        parse_mode="HTML", disable_web_page_preview=True,
    )
    if not res.get("ok"):
        log.warning("sendMessage: %s", res.get("description"))


def send_item(token: str, chat_id: str, text: str, image: str | None) -> None:
    if image:
        res = tg(
            token, "sendPhoto", chat_id=chat_id, photo=image,
            caption=text[:1020], parse_mode="HTML",
        )
        if res.get("ok"):
            return
    send_text(token, chat_id, text)


# ---------------------------------------------------------------- Авито


def _new_session(proxy: str | None):
    """Новая сессия: заходим на главную, чтобы получить cookies, как делает браузер."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if HAVE_CURL:
        s = http.Session(impersonate=IMPERSONATE, proxies=proxies)
    else:
        s = http.Session()
        if proxies:
            s.proxies.update(proxies)
    try:
        s.get(HOME, headers=HEADERS, timeout=30)
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:  # не критично, поиск всё равно попробуем
        log.warning("главная страница не открылась: %s", e)
    return s


def fetch(url: str, proxy: str | None = None) -> str:
    now = time.time()
    if _sess["obj"] is None or now - _sess["born"] > SESSION_TTL:
        _sess["obj"], _sess["born"] = _new_session(proxy), now
    r = _sess["obj"].get(url, headers=dict(HEADERS, Referer=HOME), timeout=30)
    if r.status_code in BLOCK_CODES:
        _sess["obj"] = None  # в следующий раз начнём с чистой сессии
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def _digits(text: str | None) -> int | None:
    d = re.sub(r"\D", "", text or "")
    return int(d) if d else None


def parse_items(page: str) -> list[dict]:
    """Достаёт объявления из HTML выдачи. Бросает Blocked, если это страница блокировки."""
    soup = BeautifulSoup(page, "html.parser")
    items: list[dict] = []
    for el in soup.select('[data-marker="item"]'):
        a = el.select_one('a[data-marker="item-title"]') or el.select_one(
            'a[itemprop="url"]'
        )
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.avito.ru" + href
        item_id = el.get("data-item-id")
        if not item_id:
            m = re.search(r"_(\d{6,})(?:\?|$)", href)
            item_id = m.group(1) if m else None
        if not item_id:
            continue

        h3 = el.select_one("h3")
        title = (h3.get_text(" ", strip=True) if h3 else "") or a.get("title") or ""

        price = None
        p = el.select_one('[itemprop="price"]')
        if p is not None and p.get("content"):
            price = _digits(p["content"])
        if price is None:
            p = el.select_one('[data-marker="item-price"]')
            price = _digits(p.get_text()) if p else None

        place = el.select_one('[data-marker="item-address"]') or el.select_one(
            '[class*="geo-root"]'
        )
        date = el.select_one('[data-marker="item-date"]')
        img = el.select_one("img")
        image = None
        if img:
            image = img.get("src") or (img.get("srcset") or "").split(" ")[0] or None

        items.append(
            {
                "id": str(item_id),
                "title": title.strip(),
                "url": href.split("?")[0],
                "price": price,
                "place": place.get_text(" ", strip=True) if place else "",
                "date": date.get_text(" ", strip=True) if date else "",
                "image": image,
            }
        )

    if not items:
        low = page.lower()
        if any(m in low for m in BLOCK_MARKERS):
            raise Blocked("страница блокировки")
        if not soup.select_one('[data-marker="catalog-serp"]'):
            raise RuntimeError(
                "не нашёл ни объявлений, ни блока выдачи: возможно, Авито изменил вёрстку"
            )
    return items


def passes(item: dict, s: dict) -> bool:
    price = item["price"]
    if s.get("min_price") and (price is None or price < s["min_price"]):
        return False
    if s.get("max_price") and price is not None and price > s["max_price"]:
        return False
    title = item["title"].lower()
    inc = [w.lower() for w in s.get("include", []) if w]
    if inc and not any(w in title for w in inc):
        return False
    if any(w.lower() in title for w in s.get("exclude", []) if w):
        return False
    return True


def fmt_price(p: int | None) -> str:
    return "цена не указана" if p is None else f"{p:,}".replace(",", " ") + " ₽"


def build_text(item: dict, search_name: str, old_price: int | None = None) -> str:
    head = "📉 <b>Цена снизилась</b>" if old_price else "🆕 <b>Новое объявление</b>"
    price = fmt_price(item["price"])
    if old_price:
        price = f"{fmt_price(old_price)} → <b>{price}</b>"
    lines = [
        head,
        html.escape(item["title"]),
        f"💰 {price}",
    ]
    if item["place"]:
        lines.append(f"📍 {html.escape(item['place'])}")
    if item["date"]:
        lines.append(f"🕒 {html.escape(item['date'])}")
    lines.append(f"🔎 {html.escape(search_name)}")
    lines.append(f'<a href="{html.escape(item["url"])}">Открыть на Авито</a>')
    return "\n".join(lines)


def check_search(s: dict, state: dict, token: str, chat_id: str, proxy: str | None) -> int:
    name = s.get("name") or s["url"]
    items = parse_items(fetch(s["url"], proxy))
    first_run = s["url"] not in state["seen"]
    seen: dict = state["seen"].setdefault(s["url"], {})

    to_send: list[tuple[dict, int | None]] = []
    for it in items:
        old = seen.get(it["id"], "missing")
        if old == "missing":
            if not first_run and passes(it, s):
                to_send.append((it, None))
        elif it["price"] and old and it["price"] < old and passes(it, s):
            to_send.append((it, old))
        seen[it["id"]] = it["price"]

    # старые id, которые выпали за лимит, забываем
    while len(seen) > SEEN_LIMIT:
        seen.pop(next(iter(seen)))

    if first_run:
        send_text(
            token, chat_id,
            f"✅ Поиск «{html.escape(name)}» запомнил ({len(items)} объявлений на странице). "
            "Дальше пришлю только новые.",
        )
        return 0

    to_send.reverse()  # старые сначала, самое свежее последним
    for it, old in to_send[:MAX_SEND_PER_SEARCH]:
        send_item(token, chat_id, build_text(it, name, old), it["image"])
        time.sleep(0.6)
    extra = len(to_send) - MAX_SEND_PER_SEARCH
    if extra > 0:
        send_text(token, chat_id, f"…и ещё {extra} по поиску «{html.escape(name)}», открой выдачу.")
    log.info("%s: на странице %d, отправлено %d", name, len(items), min(len(to_send), MAX_SEND_PER_SEARCH))
    return len(to_send)


# ---------------------------------------------------------------- команды из Telegram

HELP = (
    "Команды:\n"
    "/list — список поисков\n"
    "/add Название | ссылка | макс.цена — добавить поиск (цена необязательна)\n"
    "/del N — удалить поиск по номеру из /list\n\n"
    "Ссылку бери из адресной строки Авито после того, как выставил город, "
    "категорию и фильтры. Лучше включить сортировку «по дате»."
)


def handle_commands(token: str, chat_id: str, cfg: dict, state: dict) -> bool:
    """Читает новые сообщения боту. Возвращает True, если searches.json изменился."""
    data = tg(token, "getUpdates", offset=state.get("offset", 0), timeout=0)
    changed = False
    for upd in data.get("result", []):
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # чужие сообщения игнорируем
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        searches = cfg.setdefault("searches", [])

        if cmd in ("/start", "/help"):
            send_text(token, chat_id, HELP)
        elif cmd == "/list":
            if not searches:
                send_text(token, chat_id, "Поисков нет. Добавь: /add Название | ссылка")
            else:
                rows = []
                for i, s in enumerate(searches, 1):
                    extra = f" (до {s['max_price']} ₽)" if s.get("max_price") else ""
                    rows.append(f"{i}. {html.escape(s.get('name', ''))}{extra}\n{html.escape(s['url'])}")
                send_text(token, chat_id, "\n\n".join(rows))
        elif cmd == "/add":
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) < 2 or urlparse(parts[1]).netloc.split(":")[0].split(".")[-2:] != ["avito", "ru"]:
                send_text(token, chat_id, "Формат: /add Название | ссылка с avito.ru | макс.цена")
                continue
            new = {"name": parts[0] or "Поиск", "url": parts[1]}
            if len(parts) > 2 and _digits(parts[2]):
                new["max_price"] = _digits(parts[2])
            searches.append(new)
            changed = True
            send_text(token, chat_id, f"Добавил «{html.escape(new['name'])}». Первый проход запомнит текущие объявления.")
        elif cmd == "/del":
            n = _digits(rest)
            if n and 1 <= n <= len(searches):
                gone = searches.pop(n - 1)
                state["seen"].pop(gone["url"], None)
                changed = True
                send_text(token, chat_id, f"Удалил «{html.escape(gone.get('name', ''))}».")
            else:
                send_text(token, chat_id, "Укажи номер из /list, например: /del 2")
    return changed


# ---------------------------------------------------------------- основной цикл


def run_cycle(token: str, chat_id: str, proxy: str | None) -> bool:
    """Один проход по всем поискам. Возвращает True, если Авито заблокировал ВСЕ запросы."""
    cfg = load_json(SEARCHES_FILE, {"searches": []})
    state = load_json(STATE_FILE, {})
    state.setdefault("seen", {})

    try:
        if handle_commands(token, chat_id, cfg, state):
            save_json(SEARCHES_FILE, cfg)
    except Exception as e:  # команды не должны ронять мониторинг
        log.warning("команды: %s", e)

    ok = blocked = 0
    for i, s in enumerate(cfg.get("searches", [])):
        if i:
            time.sleep(random.uniform(*PAUSE_BETWEEN_SEARCHES))
        try:
            check_search(s, state, token, chat_id, proxy)
            ok += 1
        except Blocked as e:
            blocked += 1
            log.warning("%s: заблокировано (%s)", s.get("name"), e)
        except Exception as e:
            log.error("%s: %s", s.get("name"), e)

    all_blocked = blocked > 0 and ok == 0
    if all_blocked:
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= 3 and not state.get("warned"):
            send_text(
                token, chat_id,
                "⚠️ Авито уже несколько проходов подряд не пускает бота (блокировка по IP). "
                "Облачные серверы он режет чаще всего. Варианты: запустить бота на домашнем "
                "компьютере/старом телефоне или добавить прокси (PROXY_URL).",
            )
            state["warned"] = True
    elif ok:
        if state.get("warned"):
            send_text(token, chat_id, "✅ Авито снова отвечает, мониторинг работает.")
        state["fails"] = 0
        state["warned"] = False

    save_json(STATE_FILE, state)
    return all_blocked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--ping", action="store_true", help="отправить тестовое сообщение")
    ap.add_argument("--interval", type=int, default=300, help="пауза между проходами, сек (по умолчанию 300)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    token, chat_id = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    proxy = os.getenv("PROXY_URL") or None
    if not token or not chat_id:
        print("Нужны переменные TG_TOKEN и TG_CHAT_ID (см. README.md, файл .env).")
        return 2

    if args.ping:
        send_text(token, chat_id, "👋 Бот на связи. Напиши /help.")
        return 0

    if args.once:
        run_cycle(token, chat_id, proxy)
        return 0

    backoff = 0
    while True:
        try:
            blocked = run_cycle(token, chat_id, proxy)
        except Exception as e:
            log.error("сбой прохода: %s", e)
            blocked = False
        backoff = min(max(backoff * 2, args.interval), 1800) if blocked else 0
        time.sleep(max(args.interval, backoff) * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    sys.exit(main())
