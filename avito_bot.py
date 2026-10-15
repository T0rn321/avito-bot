#!/usr/bin/env python3
"""Avito -> Telegram: уведомления о новых объявлениях.

Запуск:
  python avito_bot.py --daemon   быстрый цикл на ограниченное время (GitHub Actions)
  python avito_bot.py --once     один проход
  python avito_bot.py            бесконечный цикл (ПК, старый телефон, сервер)
  python avito_bot.py --ping     проверить, что Telegram-токен и chat id верные

Что умеет:
  * несколько поисков (searches.json или команды в Telegram, см. /help)
  * фильтры: цена, слова «обязательно» / «исключить», общий чёрный список,
    свежесть объявления
  * рыночная цена (медиана) и пометка «🔥 дешевле рынка на N%»
  * не шлёт дубли, сообщает о снижении цены
  * при блокировке Авито сам пробует другие «браузерные отпечатки»
  * пауза, тихие часы (ночью копит, утром присылает), ежедневный отчёт
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:  # маскируется под настоящий браузер (TLS-отпечаток)
    from curl_cffi import requests as http

    HAVE_CURL = True
except ImportError:  # запасной вариант, блокировок будет больше
    import requests as http

    HAVE_CURL = False

BASE = Path(__file__).resolve().parent
SEARCHES_FILE = BASE / "searches.json"
STATE_FILE = BASE / "state.json"

# --- сеть и «браузер»
START_FP = os.getenv("AVITO_IMPERSONATE", "firefox")   # с чего начинать
FINGERPRINTS = ["firefox", "chrome", "safari", "edge"]  # что пробовать при блокировке
HOME = "https://www.avito.ru/"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}
BLOCK_CODES = (403, 429, 439)
SESSION_TTL = 1800            # раз в 30 минут начинаем новую сессию
BLOCK_MARKERS = (
    "доступ с вашего ip",
    "доступ ограничен",
    "проблема с ip",
    "firewall",
    "captcha",
)

# --- поведение
MAX_SEND_PER_SEARCH = 10      # максимум сообщений по одному поиску за проход
SEEN_LIMIT = 2000             # сколько объявлений помнить на один поиск
PAUSE_BETWEEN_SEARCHES = (3.0, 8.0)
DEFAULT_DEAL_PCT = 25         # «выгодно» = дешевле рынка минимум на столько %
MIN_MARKET_SAMPLES = 12       # сколько цен нужно, чтобы доверять медиане
QUEUE_LIMIT = 40              # сколько сообщений копить за тихие часы
QUEUE_FLUSH = 15              # сколько отправить утром
WARN_AFTER_FAILS = 2          # после скольких неудачных проходов предупредить
DEFAULT_INTERVAL = 60         # пауза между проходами по умолчанию, сек (меняется командой /interval)
MIN_INTERVAL = 20             # быстрее нельзя: Авито начнёт блокировать
MAX_INTERVAL = 3600
SYNC_EVERY = 600              # как часто сохранять состояние в git (только при GitHub Actions), сек
try:
    DEFAULT_TZ = float(os.getenv("BOT_TZ", "5"))   # часовой пояс (Екатеринбург = 5)
except ValueError:
    DEFAULT_TZ = 5.0

log = logging.getLogger("avito")

_sess: dict = {"obj": None, "born": 0.0, "fp": None}
_fp: dict = {"current": START_FP, "switched_from": None}
_run: dict = {"failed_fps": set(), "blocked_all": False}


class Blocked(Exception):
    """Авито не пустил (429/403/439/капча)."""


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


def tg(token: str, method: str, _timeout: int = 30, **payload) -> dict:
    r = http.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=_timeout
    )
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def send_text(token: str, chat_id: str, text: str) -> None:
    res = tg(
        token, "sendMessage", chat_id=chat_id, text=text[:4000],
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


def deliver(token, chat_id, state: dict, text: str, image: str | None, quiet: bool) -> bool:
    """Шлёт сразу, а в тихие часы кладёт в очередь. True, если реально отправлено."""
    if quiet:
        q = state.setdefault("queue", [])
        q.append({"text": text, "image": image})
        del q[:-QUEUE_LIMIT]
        return False
    send_item(token, chat_id, text, image)
    time.sleep(0.6)
    return True


def flush_queue(token: str, chat_id: str, state: dict) -> int:
    q = state.get("queue") or []
    if not q:
        return 0
    state["queue"] = []
    send_text(token, chat_id, f"🌙 Накопилось за тихие часы: {len(q)}")
    for e in q[:QUEUE_FLUSH]:
        send_item(token, chat_id, e["text"], e.get("image"))
        time.sleep(0.6)
    if len(q) > QUEUE_FLUSH:
        send_text(token, chat_id, f"…и ещё {len(q) - QUEUE_FLUSH}. Посмотреть свежее: /last")
    return min(len(q), QUEUE_FLUSH)


# ---------------------------------------------------------------- время


def local_now(settings: dict) -> datetime:
    try:
        tz = float(settings.get("tz", DEFAULT_TZ))
    except (TypeError, ValueError):
        tz = DEFAULT_TZ
    return datetime.now(timezone(timedelta(hours=tz)))


def in_quiet(settings: dict, local: datetime) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*", str(settings.get("quiet") or ""))
    if not m:
        return False
    a, b, h = int(m[1]), int(m[2]), local.hour
    if a == b:
        return False
    return a <= h < b if a < b else (h >= a or h < b)


MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_age_minutes(text: str | None, local: datetime) -> int | None:
    """Возраст объявления в минутах по подписи Авито («5 минут назад», «Вчера»...).
    None, если не поняли: такие объявления фильтр свежести не отсекает."""
    t = (text or "").lower().strip()
    if not t:
        return None
    if "только что" in t or "секунд" in t:
        return 0
    if "сегодня" in t or "вчера" in t:
        hm = re.search(r"(\d{1,2}):(\d{2})", t)
        if hm:
            day = local.date() - (timedelta(days=1) if "вчера" in t else timedelta(0))
            then = datetime(day.year, day.month, day.day, int(hm[1]), int(hm[2]), tzinfo=local.tzinfo)
            return max(0, int((local - then).total_seconds() // 60))
        return 1440 if "вчера" in t else None
    m = re.search(r"(\d+)\s*мин", t)
    if m:
        return int(m[1])
    if "минут" in t:
        return 1
    m = re.search(r"(\d+)\s*час", t)
    if m:
        return int(m[1]) * 60
    if "час" in t:
        return 60
    m = re.search(r"(\d+)\s*(?:дн|день)", t)
    if m:
        return int(m[1]) * 1440
    if "день" in t:
        return 1440
    m = re.search(r"(\d+)\s*недел", t)
    if m:
        return int(m[1]) * 10080
    if "недел" in t:
        return 10080
    m = re.search(r"(\d+)\s*месяц", t)
    if m:
        return int(m[1]) * 43200
    if "месяц" in t:
        return 43200
    m = re.search(r"(\d{1,2})\s+([а-я]+)", t)
    if m and m[2] in MONTHS:
        try:
            then = datetime(local.year, MONTHS[m[2]], int(m[1]), tzinfo=local.tzinfo)
            if then > local:
                then = then.replace(year=local.year - 1)
            return int((local - then).total_seconds() // 60)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- Авито


def _new_session(fp: str, proxy: str | None):
    """Новая сессия: заходим на главную, чтобы получить cookies, как делает браузер."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if HAVE_CURL:
        s = http.Session(impersonate=fp, proxies=proxies)
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


def _get_once(url: str, fp: str, proxy: str | None) -> str:
    now = time.time()
    if _sess["obj"] is None or _sess["fp"] != fp or now - _sess["born"] > SESSION_TTL:
        _sess.update(obj=_new_session(fp, proxy), born=now, fp=fp)
    r = _sess["obj"].get(url, headers=dict(HEADERS, Referer=HOME), timeout=30)
    if r.status_code in BLOCK_CODES:
        _sess["obj"] = None  # в следующий раз начнём с чистой сессии
        raise Blocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def fetch(url: str, proxy: str | None = None) -> str:
    """Скачивает страницу. Если Авито не пускает, пробует другие «браузеры»
    (по одному разу за проход) и запоминает тот, который сработал."""
    if _run["blocked_all"]:
        raise Blocked("уже заблокировано в этом проходе")
    order = [_fp["current"]] + [f for f in FINGERPRINTS if f != _fp["current"]]
    if not HAVE_CURL:
        order = order[:1]
    last: Exception | None = None
    for i, fp in enumerate(order):
        if fp in _run["failed_fps"]:
            continue
        try:
            text = _get_once(url, fp, proxy)
        except Blocked as e:
            _run["failed_fps"].add(fp)
            last = e
            if i + 1 < len(order):
                time.sleep(random.uniform(3.0, 6.0))
            continue
        except RuntimeError:
            raise
        except Exception as e:
            if "impersonat" in str(e).lower():  # такого отпечатка нет в этой версии curl_cffi
                _run["failed_fps"].add(fp)
                continue
            raise
        if fp != _fp["current"]:
            _fp["switched_from"], _fp["current"] = _fp["current"], fp
        return text
    _run["blocked_all"] = True
    raise last or Blocked("все варианты заблокированы")


def _digits(text) -> int | None:
    d = re.sub(r"\D", "", str(text or ""))
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


# ---------------------------------------------------------------- фильтры и рынок


def passes(item: dict, s: dict, settings: dict | None = None, local: datetime | None = None) -> bool:
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
    if any(w.lower() in title for w in (settings or {}).get("bans", []) if w):
        return False
    max_age = s.get("max_age_hours")
    if max_age and local is not None:
        age = parse_age_minutes(item.get("date"), local)
        if age is not None and age > float(max_age) * 60:
            return False
    return True


def market_stats(prices: dict) -> tuple[int, int] | None:
    """Медиана цен по поиску и число объявлений в выборке (None, если данных мало)."""
    vals = [p for p in prices.values() if isinstance(p, int) and p > 0]
    if len(vals) < MIN_MARKET_SAMPLES:
        return None
    return int(statistics.median(vals)), len(vals)


def deal_percent(item: dict, market: tuple[int, int] | None, s: dict) -> int | None:
    """На сколько % объявление дешевле рынка (None, если не «выгодное»)."""
    thr = s.get("deal_pct", DEFAULT_DEAL_PCT)
    if not thr or not market or not item["price"]:
        return None
    pct = round((1 - item["price"] / market[0]) * 100)
    return pct if pct >= thr else None


def fmt_price(p: int | None) -> str:
    return "цена не указана" if p is None else f"{p:,}".replace(",", " ") + " ₽"


def build_text(
    item: dict, search_name: str, old_price: int | None = None, head: str | None = None,
    market: tuple[int, int] | None = None, pct: int | None = None,
) -> str:
    deal_head = f"🔥 <b>Выгодно: на {pct}% дешевле рынка</b>" if pct else None
    head = head or (
        "📉 <b>Цена снизилась</b>" if old_price else (deal_head or "🆕 <b>Новое объявление</b>")
    )
    price = fmt_price(item["price"])
    if old_price:
        price = f"{fmt_price(old_price)} → <b>{price}</b>"
    lines = [head, html.escape(item["title"]), f"💰 {price}"]
    if market:
        lines.append(f"📊 Рынок ~{fmt_price(market[0])} (по {market[1]} объявл.)")
    if pct and head != deal_head:
        lines.append(f"🔥 На {pct}% дешевле рынка")
    if item["place"]:
        lines.append(f"📍 {html.escape(item['place'])}")
    if item["date"]:
        lines.append(f"🕒 {html.escape(item['date'])}")
    lines.append(f"🔎 {html.escape(search_name)}")
    lines.append(f'<a href="{html.escape(item["url"], quote=True)}">Открыть на Авито</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------- проверка одного поиска


def check_search(
    s: dict, cfg: dict, state: dict, token: str, chat_id: str,
    proxy: str | None, quiet: bool, local: datetime,
) -> dict:
    """Проверяет поиск. Возвращает счётчики: sent / new / deals."""
    name = s.get("name") or s["url"]
    settings = cfg.get("settings", {})
    items = parse_items(fetch(s["url"], proxy))
    first_run = s["url"] not in state["seen"]
    seen: dict = state["seen"].setdefault(s["url"], {})

    candidates: list[tuple[dict, int | None]] = []
    for it in items:
        old = seen.get(it["id"], "missing")
        if old == "missing":
            if not first_run:
                candidates.append((it, None))
        elif it["price"] and old and it["price"] < old:
            candidates.append((it, old))
        seen[it["id"]] = it["price"]

    while len(seen) > SEEN_LIMIT:  # старые id, выпавшие за лимит, забываем
        seen.pop(next(iter(seen)))

    if first_run:
        send_text(
            token, chat_id,
            f"✅ Поиск «{html.escape(name)}» запомнил ({len(items)} объявлений на странице). "
            "Дальше пришлю только новые.",
        )
        return {"sent": 0, "new": 0, "deals": 0, "items": len(items)}

    market = market_stats(seen)
    ready: list[tuple[dict, int | None, int | None]] = []
    for it, old in candidates:
        if not passes(it, s, settings, local):
            continue
        pct = deal_percent(it, market, s)
        if s.get("only_deals") and s.get("deal_pct", DEFAULT_DEAL_PCT) and pct is None:
            continue
        ready.append((it, old, pct))

    ready.reverse()  # старые сначала, самое свежее последним
    sent = 0
    for it, old, pct in ready[:MAX_SEND_PER_SEARCH]:
        text = build_text(it, name, old, market=market, pct=pct)
        sent += deliver(token, chat_id, state, text, it["image"], quiet)
    extra = len(ready) - MAX_SEND_PER_SEARCH
    if extra > 0 and not quiet:
        send_text(token, chat_id, f"…и ещё {extra} по поиску «{html.escape(name)}», открой выдачу.")
    log.info("%s: на странице %d, подходящих %d, отправлено %d", name, len(items), len(ready), sent)
    return {"sent": sent, "new": len(ready), "deals": sum(1 for r in ready if r[2]), "items": len(items)}


# ---------------------------------------------------------------- команды из Telegram

HELP = (
    "<b>Поиски</b>\n"
    "/list — список поисков и их настройки\n"
    "/last [N] — последние объявления по всем поискам\n"
    "/add Название | ссылка | макс.цена — добавить поиск\n"
    "/del N — удалить поиск\n"
    "/set N параметр значение — настроить поиск\n\n"
    "<b>Общее</b>\n"
    "/ban слово, /unban слово, /bans — чёрный список слов (для всех поисков)\n"
    "/pause, /resume — пауза\n"
    "/quiet 23-8 или /quiet off — тихие часы (ночью копит, утром присылает)\n"
    "/tz 5 — ваш часовой пояс (UTC+5)\n"
    "/report 9 или /report off — час ежедневного отчёта\n"
    "/interval 60 — как часто проверять Авито (секунды, меньше = быстрее, но рискованнее)\n"
    "/status — состояние бота\n\n"
    "Ссылку на поиск берите с Авито после выбора города и фильтров, "
    "с сортировкой «по дате»."
)

SET_HELP = (
    "Формат: /set N параметр значение, где N — номер из /list.\n"
    "Параметры:\n"
    "max 3000 — макс. цена (0 — убрать)\n"
    "min 500 — мин. цена\n"
    "age 2 — только объявления свежее 2 часов (0 — убрать)\n"
    "deal 30 — «выгодно», если дешевле рынка на 30% (off — отключить, default — 25%)\n"
    "only on — слать только выгодные (off — все)\n"
    "inc слово1, слово2 — в названии должно быть хоть одно (- убрать)\n"
    "exc слово1, слово2 — в названии не должно быть (- убрать)"
)

_OFF = ("-", "off", "выкл", "0", "нет")
_ON = ("on", "вкл", "да", "1")


def _num(val: str) -> float | None:
    return float(val.replace(",", ".")) if re.fullmatch(r"\d+(?:[.,]\d+)?", val) else None


def _words(val: str) -> list[str]:
    return [w.strip() for w in val.split(",") if w.strip()]


def apply_set(s: dict, key: str, val: str) -> str | None:
    """Применяет /set к поиску. Возвращает ответ пользователю или None, если не поняли."""
    v = val.strip().lower()
    if key in ("max", "min"):
        field = "max_price" if key == "max" else "min_price"
        if v in _OFF:
            s.pop(field, None)
            return f"{field} убрана"
        if re.fullmatch(r"\d+", v):
            s[field] = int(v)
            return f"{field} = {v} ₽"
    elif key == "age":
        if v in _OFF:
            s.pop("max_age_hours", None)
            return "Фильтр свежести выключен"
        n = _num(v)
        if n:
            s["max_age_hours"] = n
            return f"Только объявления свежее {v} ч"
    elif key == "deal":
        if v in _OFF:
            s["deal_pct"] = 0
            return "Пометка «выгодно» выключена"
        if v in ("default", "по умолчанию"):
            s.pop("deal_pct", None)
            return f"Порог «выгодно» по умолчанию: {DEFAULT_DEAL_PCT}%"
        if re.fullmatch(r"\d{1,2}", v) and 1 <= int(v) <= 95:
            s["deal_pct"] = int(v)
            return f"«Выгодно» = дешевле рынка минимум на {v}%"
    elif key == "only":
        if v in _ON:
            s["only_deals"] = True
            return "Буду слать только выгодные (дешевле рынка)"
        if v in _OFF:
            s.pop("only_deals", None)
            return "Буду слать все подходящие"
    elif key in ("inc", "exc"):
        field = "include" if key == "inc" else "exclude"
        if v in _OFF:
            s.pop(field, None)
            return f"{field} очищен"
        words = _words(val)
        if words:
            s[field] = words
            return f"{field}: {', '.join(words)}"
    return None


def describe(s: dict) -> str:
    bits = []
    if s.get("min_price"):
        bits.append(f"от {s['min_price']} ₽")
    if s.get("max_price"):
        bits.append(f"до {s['max_price']} ₽")
    if s.get("max_age_hours"):
        bits.append(f"свежее {s['max_age_hours']:g} ч")
    d = s.get("deal_pct", DEFAULT_DEAL_PCT)
    bits.append(f"выгодно от {d}%" if d else "без пометки «выгодно»")
    if s.get("only_deals") and d:
        bits.append("только выгодные")
    if s.get("include"):
        bits.append("слова: " + ", ".join(s["include"]))
    if s.get("exclude"):
        bits.append("без: " + ", ".join(s["exclude"]))
    return ", ".join(bits)


def _hhmm(ts: float, local: datetime) -> str:
    return datetime.fromtimestamp(ts, local.tzinfo).strftime("%H:%M")


def status_text(cfg: dict, state: dict, local: datetime) -> str:
    st = cfg.get("settings", {})
    stats = state.get("stats", {})
    quiet = st.get("quiet")
    rh = st.get("report_hour", 9)
    lines = [
        "<b>Состояние бота</b>",
        "⏸ На паузе" if st.get("paused") else "▶️ Работает",
        f"🔎 Поисков: {len(cfg.get('searches', []))}",
        f"⏱ Интервал проверки: {st.get('interval') or DEFAULT_INTERVAL} сек",
        f"🦊 «Браузер» для Авито: {_fp['current']}",
        f"🌙 Тихие часы: {quiet if quiet else 'выкл'} (ваше время сейчас {local:%H:%M}, UTC{local.utcoffset().total_seconds() / 3600:+g})",
        f"📅 Отчёт: {f'в {rh}:00' if rh is not None else 'выкл'}",
        f"🚫 Слов в чёрном списке: {len(st.get('bans', []))}",
    ]
    if state.get("last_ok"):
        lines.append(f"✅ Последняя удачная проверка: {_hhmm(state['last_ok'], local)}")
    if state.get("cooldown_until", 0) > time.time():
        lines.append(f"⚠️ Авито не пускает, следующая попытка после {_hhmm(state['cooldown_until'], local)}")
    if state.get("queue"):
        lines.append(f"📥 В очереди на утро: {len(state['queue'])}")
    if stats:
        lines.append(
            f"За период: проверок {stats.get('checked', 0)}, новых {stats.get('new', 0)}, "
            f"отправлено {stats.get('sent', 0)}, блокировок {stats.get('blocked', 0)}"
        )
    return "\n".join(lines)


def handle_commands(
    token: str, chat_id: str, cfg: dict, state: dict, proxy: str | None = None, wait: int = 0
) -> bool:
    """Читает новые сообщения боту. Возвращает True, если конфигурация изменилась.

    wait > 0: «длинный опрос» Telegram. Запрос висит до wait секунд и возвращается сразу,
    как только пришла команда, поэтому бот отвечает мгновенно, а ожидание не тратит CPU."""
    data = tg(token, "getUpdates", _timeout=wait + 15,
              offset=state.get("offset", 0), timeout=wait)
    changed = False
    st = cfg.setdefault("settings", {})
    searches = cfg.setdefault("searches", [])
    local = local_now(st)

    def say(text: str) -> None:
        send_text(token, chat_id, text)

    for upd in data.get("result", []):
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # чужие сообщения игнорируем
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.split("@")[0].lower(), rest.strip()

        if cmd in ("/start", "/help"):
            say(HELP)

        elif cmd == "/list":
            if not searches:
                say("Поисков нет. Добавь: /add Название | ссылка")
                continue
            rows = []
            for i, s in enumerate(searches, 1):
                link = html.escape(s["url"], quote=True)
                rows.append(
                    f'{i}. <a href="{link}">{html.escape(s.get("name", "Поиск"))}</a>'
                    f" — {html.escape(describe(s))}"
                )
            say("\n".join(rows))

        elif cmd == "/add":
            parts = [p.strip() for p in rest.split("|")]
            if len(parts) < 2 or urlparse(parts[1]).netloc.split(":")[0].split(".")[-2:] != ["avito", "ru"]:
                say("Формат: /add Название | ссылка с avito.ru | макс.цена")
                continue
            new = {"name": parts[0] or "Поиск", "url": parts[1]}
            if len(parts) > 2 and _digits(parts[2]):
                new["max_price"] = _digits(parts[2])
            searches.append(new)
            changed = True
            say(f"Добавил «{html.escape(new['name'])}». Первый проход запомнит текущие объявления.")

        elif cmd == "/del":
            n = _digits(rest)
            if n and 1 <= n <= len(searches):
                gone = searches.pop(n - 1)
                state["seen"].pop(gone["url"], None)
                changed = True
                say(f"Удалил «{html.escape(gone.get('name', ''))}».")
            else:
                say("Укажи номер из /list, например: /del 2")

        elif cmd == "/set":
            parts = rest.split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= len(searches):
                say(SET_HELP)
                continue
            answer = apply_set(searches[int(parts[0]) - 1], parts[1].lower(), parts[2])
            if answer is None:
                say(SET_HELP)
            else:
                changed = True
                say("✅ " + html.escape(answer))

        elif cmd == "/ban":
            w = rest.lower()
            if not w:
                say("Формат: /ban слово (или фраза)")
            elif w in st.setdefault("bans", []):
                say("Это слово уже в чёрном списке.")
            else:
                st["bans"].append(w)
                changed = True
                say(f"🚫 Добавил «{html.escape(w)}». Такие объявления больше не придут.")

        elif cmd == "/unban":
            w = rest.lower()
            if w in st.get("bans", []):
                st["bans"].remove(w)
                changed = True
                say(f"Убрал «{html.escape(w)}» из чёрного списка.")
            else:
                say("Такого слова нет в чёрном списке, смотри /bans")

        elif cmd == "/bans":
            bans = st.get("bans", [])
            say("🚫 Чёрный список: " + ", ".join(html.escape(b) for b in bans) if bans else "Чёрный список пуст. Добавить: /ban слово")

        elif cmd in ("/pause", "/resume"):
            st["paused"] = cmd == "/pause"
            changed = True
            say("⏸ Пауза. Продолжить: /resume" if st["paused"] else "▶️ Снова работаю.")

        elif cmd == "/quiet":
            m = re.fullmatch(r"(\d{1,2})\s*[-–]\s*(\d{1,2})", rest)
            if rest.lower() in _OFF:
                st.pop("quiet", None)
                changed = True
                say("Тихие часы выключены.")
            elif m and int(m[1]) < 24 and int(m[2]) < 24 and m[1] != m[2]:
                st["quiet"] = f"{int(m[1])}-{int(m[2])}"
                changed = True
                say(f"🌙 Тихие часы {st['quiet']} (по вашему времени). В это время копится до {QUEUE_LIMIT} объявлений, утром придут.")
            else:
                say("Формат: /quiet 23-8 (с 23:00 до 8:00) или /quiet off")

        elif cmd == "/tz":
            try:
                tz = float(rest.replace(",", ".").replace("+", ""))
                assert -12 <= tz <= 14
            except (ValueError, AssertionError):
                say("Формат: /tz 5 (часовой пояс относительно UTC; Екатеринбург = 5, Москва = 3)")
                continue
            st["tz"] = tz
            changed = True
            local = local_now(st)
            say(f"Часовой пояс UTC{tz:+g}. Сейчас у вас {local:%H:%M}.")

        elif cmd == "/report":
            if rest.lower() in _OFF and rest != "0":
                st["report_hour"] = None
                changed = True
                say("Ежедневный отчёт выключен.")
            elif re.fullmatch(r"\d{1,2}", rest) and int(rest) < 24:
                st["report_hour"] = int(rest)
                changed = True
                say(f"📅 Отчёт каждый день после {int(rest)}:00 по вашему времени.")
            else:
                say("Формат: /report 9 или /report off")

        elif cmd == "/interval":
            n = _digits(rest)
            if n and MIN_INTERVAL <= n <= MAX_INTERVAL:
                st["interval"] = n
                changed = True
                say(f"⏱ Проверяю примерно раз в {n} сек. Чем чаще, тем быстрее уведомления, "
                    "но выше риск блокировки Авито.")
            elif rest.lower() in ("default", "по умолчанию"):
                st.pop("interval", None)
                changed = True
                say(f"⏱ Вернул стандартный интервал: {DEFAULT_INTERVAL} сек.")
            else:
                say(f"Формат: /interval 60 (секунд, от {MIN_INTERVAL} до {MAX_INTERVAL}) или /interval default")

        elif cmd == "/status":
            say(status_text(cfg, state, local))

        elif cmd == "/last":
            n = min(_digits(rest) or 5, 10)
            if not searches:
                say("Поисков нет. Добавь: /add Название | ссылка")
                continue
            if state.get("cooldown_until", 0) > time.time():
                say(f"Авито сейчас не пускает, попробую после {_hhmm(state['cooldown_until'], local)}.")
                continue
            for k, s in enumerate(searches):
                name = s.get("name") or s["url"]
                if k:
                    time.sleep(random.uniform(*PAUSE_BETWEEN_SEARCHES))
                try:
                    found = parse_items(fetch(s["url"], proxy))
                except Blocked:
                    say(f"⚠️ Авито сейчас не пускает, поиск «{html.escape(name)}» пропущен.")
                    continue
                except Exception as e:
                    say(f"Ошибка по поиску «{html.escape(name)}»: {html.escape(str(e))}")
                    continue
                market = market_stats(state["seen"].get(s["url"], {}))
                shown = [it for it in found if passes(it, s, st, local)][:n]
                if not shown:
                    say(f"По поиску «{html.escape(name)}» ничего не нашлось.")
                for it in reversed(shown):
                    text_ = build_text(
                        it, name, head="🕘 <b>Последнее объявление</b>",
                        market=market, pct=deal_percent(it, market, s),
                    )
                    send_item(token, chat_id, text_, it["image"])
                    time.sleep(0.6)
    return changed


# ---------------------------------------------------------------- отчёт


def new_stats() -> dict:
    return {"since": time.time(), "checked": 0, "new": 0, "sent": 0, "deals": 0, "blocked": 0, "errors": 0}


def send_report(token: str, chat_id: str, cfg: dict, state: dict, local: datetime) -> None:
    stats = state.get("stats") or new_stats()
    lines = [
        "📊 <b>Отчёт: бот работает</b>",
        f"С {datetime.fromtimestamp(stats['since'], local.tzinfo):%d.%m %H:%M}: "
        f"проверок {stats['checked']}, новых {stats['new']}, "
        f"отправлено {stats['sent']} (выгодных {stats['deals']}).",
        f"Блокировок Авито: {stats['blocked']}, ошибок: {stats['errors']}.",
    ]
    rows = []
    for s in cfg.get("searches", []):
        mk = market_stats(state["seen"].get(s["url"], {}))
        if mk:
            rows.append(f"• {html.escape(s.get('name', 'Поиск'))} — ~{fmt_price(mk[0])} (по {mk[1]} объявл.)")
    if rows:
        lines += ["Рынок (медиана цены):"] + rows
    send_text(token, chat_id, "\n".join(lines))
    state["stats"] = new_stats()


# ---------------------------------------------------------------- основной цикл


def run_cycle(token: str, chat_id: str, proxy: str | None) -> bool:
    """Один проход. Возвращает True, если Авито не пустил ни по одному поиску."""
    cfg = load_json(SEARCHES_FILE, {"searches": []})
    state = load_json(STATE_FILE, {})
    state.setdefault("seen", {})
    state.setdefault("queue", [])
    state.setdefault("stats", new_stats())
    cfg.setdefault("settings", {})
    st = cfg["settings"]

    _run["failed_fps"], _run["blocked_all"] = set(), False
    _fp["current"], _fp["switched_from"] = state.get("fp") or START_FP, None

    try:
        if handle_commands(token, chat_id, cfg, state, proxy):
            save_json(SEARCHES_FILE, cfg)
    except Exception as e:  # команды не должны ронять мониторинг
        log.warning("команды: %s", e)

    local = local_now(st)
    quiet = in_quiet(st, local)

    if not quiet:
        state["stats"]["sent"] += flush_queue(token, chat_id, state)
        rh = st.get("report_hour", 9)
        today = local.date().isoformat()
        if state.get("last_report_date") is None:
            state["last_report_date"] = today        # первый день: отчёт завтра
        elif rh is not None and local.hour >= rh and state["last_report_date"] != today:
            send_report(token, chat_id, cfg, state, local)
            state["last_report_date"] = today

    if st.get("paused"):
        log.info("пауза")
        save_json(STATE_FILE, state)
        return False

    now = time.time()
    if now < state.get("cooldown_until", 0):
        log.info("Авито не пускал, жду до %s", _hhmm(state["cooldown_until"], local))
        save_json(STATE_FILE, state)
        return True

    ok = blocked = 0
    stats = state["stats"]
    for i, s in enumerate(cfg.get("searches", [])):
        if i:
            time.sleep(random.uniform(*PAUSE_BETWEEN_SEARCHES))
        try:
            res = check_search(s, cfg, state, token, chat_id, proxy, quiet, local)
            ok += 1
            stats["checked"] += 1
            stats["new"] += res["new"]
            stats["sent"] += res["sent"]
            stats["deals"] += res["deals"]
        except Blocked as e:
            blocked += 1
            stats["blocked"] += 1
            log.warning("%s: заблокировано (%s)", s.get("name"), e)
        except Exception as e:
            stats["errors"] += 1
            log.error("%s: %s", s.get("name"), e)

    if _fp["switched_from"]:
        send_text(
            token, chat_id,
            f"ℹ️ Авито перестал пускать как «{_fp['switched_from']}», переключился на «{_fp['current']}». "
            "Дальше работаю так.",
        )
    state["fp"] = _fp["current"]

    all_blocked = blocked > 0 and ok == 0
    if all_blocked:
        state["fails"] = state.get("fails", 0) + 1
        state["cooldown_until"] = now + min(15 * 2 ** (state["fails"] - 1), 120) * 60
        if state["fails"] >= WARN_AFTER_FAILS and not state.get("warned"):
            send_text(
                token, chat_id,
                "⚠️ Авито не пускает бота (блокировка по IP), хотя я перебрал разные «браузеры». "
                "Облачные серверы он режет чаще всего. Варианты: запустить бота на домашнем "
                "компьютере/старом телефоне с мобильным интернетом или добавить прокси (PROXY_URL). "
                "Попытки продолжаются с растущими паузами.",
            )
            state["warned"] = True
    elif ok:
        if state.get("warned"):
            send_text(token, chat_id, "✅ Авито снова отвечает, мониторинг работает.")
        state.update(fails=0, warned=False, cooldown_until=0, last_ok=now)

    save_json(STATE_FILE, state)
    return all_blocked


# ---------------------------------------------------------------- быстрый цикл (daemon)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)


_sync_sig: dict = {"last": None}


def _state_signature() -> str:
    """Отпечаток «важной» части состояния: без счётчиков, которые меняются каждый проход."""
    st = load_json(STATE_FILE, {})
    for k in ("stats", "last_ok"):
        st.pop(k, None)
    cfg = load_json(SEARCHES_FILE, {})
    return json.dumps([st, cfg], sort_keys=True, ensure_ascii=False)


def sync_git(force: bool = False) -> None:
    """Сохраняет state.json и searches.json в репозиторий (только внутри GitHub Actions).

    Без force коммитит лишь когда изменилось что-то важное (новые объявления, команды),
    поэтому история репозитория не засоряется каждую минуту."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    try:
        sig = _state_signature()
        if not force and sig == _sync_sig["last"]:
            return
        _git("config", "user.name", "avito-bot")
        _git("config", "user.email", "avito-bot@users.noreply.github.com")
        _git("add", "state.json", "searches.json")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            _sync_sig["last"] = sig
            return
        _git("commit", "-m", "update state [skip ci]")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":  # detached HEAD
            branch = os.getenv("GITHUB_REF_NAME") or "main"
        for attempt in range(3):
            _git("pull", "--rebase", "--autostash", "origin", branch)
            res = _git("push", "origin", f"HEAD:{branch}")
            if res.returncode == 0:
                _sync_sig["last"] = sig
                log.info("состояние сохранено в репозиторий")
                return
            log.warning("push не удался (попытка %d): %s", attempt + 1, res.stderr.strip()[:200])
            time.sleep(3 + attempt * 3)
    except Exception as e:  # сохранение не должно ронять бота
        log.warning("сохранение состояния: %s", e)


def wait_commands(token: str, chat_id: str, proxy: str | None, wait: int) -> bool:
    """Ждёт до wait секунд, отвечая на команды сразу. True, если настройки изменились."""
    cfg = load_json(SEARCHES_FILE, {"searches": []})
    state = load_json(STATE_FILE, {})
    state.setdefault("seen", {})
    state.setdefault("queue", [])
    state.setdefault("stats", new_stats())
    before = state.get("offset", 0)
    t0 = time.time()
    try:
        changed = handle_commands(token, chat_id, cfg, state, proxy, wait=wait)
    except Exception as e:
        log.warning("команды: %s", e)
        time.sleep(min(wait, 5))  # не крутимся вхолостую, если Telegram недоступен
        return False
    if changed:
        save_json(SEARCHES_FILE, cfg)
    if changed or state.get("offset", 0) != before:
        save_json(STATE_FILE, state)
    if time.time() - t0 < 1 and wait > 1 and not changed:
        time.sleep(1)  # защита от мгновенных пустых ответов
    return changed


def daemon(token: str, chat_id: str, proxy: str | None, duration: float | None,
           interval_default: int) -> None:
    """Проходы по Авито с короткой паузой; между ними слушает команды Telegram.

    duration — сколько секунд работать (для GitHub Actions: лимит задачи 6 часов),
    None — бесконечно. Интервал считается от НАЧАЛА прохода, поэтому длительность
    самого прохода не прибавляется к задержке."""
    stop = {"flag": False}

    def _stop(signum, frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    deadline = time.time() + duration if duration else float("inf")
    last_sync = time.time()
    _sync_sig["last"] = _state_signature()
    log.info("цикл запущен, интервал по умолчанию %d сек, работаю %s", interval_default,
             f"{duration / 3600:.1f} ч" if duration else "без ограничения")

    while not stop["flag"] and time.time() < deadline:
        started = time.time()
        try:
            run_cycle(token, chat_id, proxy)
        except Exception as e:
            log.error("сбой прохода: %s", e)

        st = load_json(SEARCHES_FILE, {}).get("settings", {})
        interval = st.get("interval") or interval_default
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL, int(interval)))
        next_at = min(started + interval * random.uniform(0.85, 1.15), deadline)

        while not stop["flag"]:
            left = next_at - time.time()
            if left <= 0:
                break
            if wait_commands(token, chat_id, proxy, wait=int(min(25, max(1, left)))):
                break  # настройки поменяли, сразу новый проход

        if time.time() - last_sync >= SYNC_EVERY:
            sync_git()
            last_sync = time.time()

    log.info("цикл завершён, сохраняю состояние")
    sync_git(force=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--ping", action="store_true", help="отправить тестовое сообщение")
    ap.add_argument("--daemon", action="store_true",
                    help="быстрый цикл на ограниченное время (см. --duration), для GitHub Actions")
    ap.add_argument("--duration", type=int, default=20400,
                    help="сколько секунд работать в режиме --daemon (по умолчанию 20400 = 5 ч 40 мин)")
    ap.add_argument("--sync", action="store_true", help="сохранить state.json в git и выйти (GitHub Actions)")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"пауза между проходами, сек (по умолчанию {DEFAULT_INTERVAL}; меняется командой /interval)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()

    if args.sync:
        sync_git(force=True)
        return 0

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

    daemon(token, chat_id, proxy, args.duration if args.daemon else None, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
