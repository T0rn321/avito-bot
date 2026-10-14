#!/usr/bin/env python3
"""Диагностика: какой способ запроса Авито сейчас пропускает.

Запуск (в папке бота):  python diagnose.py
Берёт первую ссылку из searches.json и пробует несколько вариантов.
Ничего никуда не отправляет, токены не нужны.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as cr
except ImportError:
    print("Нужен curl_cffi: pip install curl_cffi")
    sys.exit(1)

BASE = Path(__file__).resolve().parent
HOME = "https://www.avito.ru/"
BLOCK = ("доступ с вашего ip", "доступ ограничен", "проблема с ip", "firewall", "captcha")
COOKIES = os.getenv("AVITO_COOKIES", "").strip()

try:
    url = json.loads((BASE / "searches.json").read_text(encoding="utf-8"))["searches"][0]["url"]
except Exception as e:
    print("Не смог прочитать searches.json:", e)
    sys.exit(1)

# (название, отпечаток браузера, сначала зайти на главную?, добавить cookies?, ссылка)
VARIANTS = [
    ("chrome, сразу на поиск", "chrome", False, False, url),
    ("chrome, сначала главная", "chrome", True, False, url),
    ("chrome131, сначала главная", "chrome131", True, False, url),
    ("safari, сначала главная", "safari", True, False, url),
    ("firefox, сначала главная", "firefox", True, False, url),
    ("android chrome, сначала главная", "chrome131_android", True, False, url),
    ("android chrome, мобильный сайт m.avito.ru", "chrome131_android", True, False,
     url.replace("://www.avito.ru", "://m.avito.ru")),
]
if COOKIES:
    VARIANTS.append(("chrome + ваши cookies", "chrome", False, True, url))


def attempt(imp, warm, use_cookies, target):
    headers = {"Accept-Language": "ru-RU,ru;q=0.9"}
    if use_cookies:
        headers["Cookie"] = COOKIES
    s = cr.Session(impersonate=imp)
    if warm:
        s.get(HOME, headers=headers, timeout=30)
        time.sleep(2.5)
        headers = dict(headers, Referer=HOME)
    r = s.get(target, headers=headers, timeout=30)
    text = r.text or ""
    items = len(re.findall(r'data-marker="item"', text))
    low = text.lower()
    blocked = any(m in low for m in BLOCK)
    return r.status_code, items, blocked, len(text)


print("Ссылка:", url[:90], "...\n")
good = []
for i, (label, imp, warm, use_cookies, target) in enumerate(VARIANTS, 1):
    try:
        code, items, blocked, size = attempt(imp, warm, use_cookies, target)
        note = "блок-страница" if blocked and not items else ""
        print(f"[{i}] {label:<45} HTTP {code}, объявлений: {items} {note}")
        if code == 200 and items:
            good.append(label)
    except Exception as e:
        print(f"[{i}] {label:<45} ошибка: {str(e)[:80]}")
    time.sleep(4)

print()
if good:
    print("РАБОТАЕТ:", "; ".join(good))
else:
    print("Ни один вариант не прошёл. Пришлите этот вывод целиком.")
