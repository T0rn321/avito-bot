"""Анализ объявления и продавца: перекуп, лот, редкость и популярность пластинки.

Здесь только чистые функции без сети: страницы и ответы Discogs передаются снаружи.
Благодаря этому всё проверяется тестами без доступа к Авито.

ВАЖНО: оценка «перекуп» это эвристика (набор признаков с весами), а не статистика.
Она подсказывает, на что посмотреть, и никого не исключает.
"""
from __future__ import annotations

import difflib
import math
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- регулярки

VINYL_RE = re.compile(
    r"винил|пластинк|грампластин|\blp\b|\bep\b|vinyl|\b(?:7|10|12)\s*[\"”'″]", re.I
)

LOT_RE = re.compile(
    r"\bлот\b|лотом|коллекци|все\s+(?:вместе|сразу|одним|пластинки)|"
    r"только\s+(?:все|всё|вместе|комплект\w*|оптом|целиком)|"
    r"(?:одним|единым)\s+лот|целиком|отдам\s+(?:все|всё)|продам\s+(?:все|всё)|оптом",
    re.I,
)
COUNT_RE = re.compile(r"(\d{1,3})\s*(?:шт\w*|пластин\w*|винил\w*|lp|диск\w*|альбом\w*)", re.I)

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B06]")
CAPS_RE = re.compile(r"\b[А-ЯЁA-Z]{4,}\b")
CAPS_IGNORE = {"VINYL", "MINT", "SEALED", "NEAR", "GOOD", "PROMO", "JAPAN", "STEREO"}
BULLET_RE = re.compile(r"[✔✅▪•➡⭐❗]|^\s*[-–—]\s+\S", re.M)

# (регэксп, баллы, подпись) — признаки профессионального продавца в тексте
RESELLER_PHRASES = [
    (r"в\s+наличии", 14, "«в наличии»"),
    (r"под\s+заказ|на\s+заказ", 14, "«под заказ»"),
    (r"оптом|\bопт\b", 12, "оптом"),
    (r"магазин|ассортимент|каталог", 10, "магазин/каталог"),
    (r"смотрите\s+(?:другие|мои|все)\s+объявлен|другие\s+(?:мои\s+)?(?:объявлен|пластинк)|"
     r"ещё\s+больше\s+пластинок|много\s+других\s+пластинок", 10, "отсылка к другим объявлениям"),
    (r"работаем|график\s+работы|звоните\s+с\s+\d", 8, "«работаем/график»"),
    (r"гарантия|гарантируем|отзывы", 6, "гарантии/отзывы"),
    (r"оригинал|подлинн|аутентичн|original\s+press|first\s+press|первый\s+(?:тираж|пресс)",
     5, "акцент на «оригинал»"),
    (r"торг\s+не\s+(?:уместен|предусмотрен)|без\s+торга|цена\s+(?:окончательная|фиксированная)",
     3, "цена без торга"),
    (r"доставка\s+(?:по\s+(?:россии|всей)|в\s+любой)|отправ\w+\s+(?:по\s+россии|сдэк|cdek)",
     4, "доставка по России"),
]
# признаки частного продавца (минус баллы)
PRIVATE_PHRASES = [
    (r"досталась|достались|по\s+наследству|от\s+(?:дедушки|бабушки|отца|папы|деда|мамы)|остались\s+от",
     -20, "«досталось от родных»"),
    (r"освобожда\w+\s+место|переезд|мешает|не\s+нужн\w+|не\s+слуша\w+|лежат\s+без\s+дела|избавля\w+|надоел",
     -15, "избавляется от ненужного"),
    (r"отдам\s+(?:даром|бесплатно)|\bдаром\b|\bбесплатно\b", -10, "отдаёт бесплатно"),
]
EXPERT_RE = re.compile(
    r"\b(?:nm|vg\+?|mint|goldmine|discogs|ean|pressing)\b|near\s+mint|very\s+good|"
    r"матриц|каталожн|штрихкод|\bcat\.?\s*#",
    re.I,
)

# признаки редкости по тексту (подпись)
RARE_PATTERNS = [
    (r"первый\s+тираж|первопресс|1st\s+press|first\s+press|original\s+press", "первый тираж"),
    (r"\bpromo\b|промо\b|promotional", "промо"),
    (r"лимитирован|limited|нумерован|numbered", "лимитированное/нумерованное"),
    (r"цветн\w+\s+винил|colou?red|picture\s*disc|пикчер", "цветной/picture disc"),
    (r"запечатан|sealed|\bnew\s+sealed\b", "запечатана"),
    (r"\bobi\b|японск|\bjapan\b", "японское издание"),
    (r"\buk\s+press|английск\w+\s+(?:издан|пресс)|german\s+press|немецк\w+\s+(?:издан|пресс)",
     "импортный пресс"),
    (r"редк\w+|раритет|коллекционн|rare", "«редкая/раритет» в тексте"),
    (r"автограф|signed", "автограф"),
    (r"бутлег|bootleg|test\s+press|тест-?пресс", "бутлег/тест-пресс"),
]

# Русские написания частых артистов → как их ищут на Discogs
ALIASES = {
    "битлз": "beatles", "битлс": "beatles", "пинк флойд": "pink floyd",
    "лед зеппелин": "led zeppelin", "лед зепеллин": "led zeppelin", "квин": "queen",
    "депеш мод": "depeche mode", "металлика": "metallica", "нирвана": "nirvana",
    "роллинг стоунз": "rolling stones", "дип перпл": "deep purple", "блэк саббат": "black sabbath",
    "ac/dc": "ac dc", "эй си ди си": "ac dc", "абба": "abba",
    "бони эм": "boney m", "модерн токинг": "modern talking", "скорпионс": "scorpions",
    "дорз": "doors", "ю ту": "u2",
    "майкл джексон": "michael jackson", "дэвид боуи": "david bowie", "элтон джон": "elton john",
    "фрэнк синатра": "frank sinatra", "луи армстронг": "louis armstrong",
}
STOP_WORDS = {
    "винил", "виниловая", "виниловые", "виниловый", "пластинка", "пластинки", "пластинок",
    "грампластинка", "lp", "ep", "ссср", "ussr", "оригинал", "запечатана", "запечатанная",
    "новая", "новый", "состояние", "отличное", "идеальное", "мелодия", "стерео", "моно",
    "продам", "отдам", "куплю", "от", "и", "в", "на", "с", "по", "для", "the", "a",
    "диск", "альбом", "пресс", "издание", "тираж", "первый", "первая",
}


# ---------------------------------------------------------------- текст

def detect_lot(text: str) -> dict | None:
    """Ищет «лот / отдам всё вместе». Возвращает {'count': N|None, 'why': ...} или None."""
    m = LOT_RE.search(text or "")
    counts = [int(c) for c in COUNT_RE.findall(text or "") if 2 <= int(c) <= 500]
    if not m and not counts:
        return None
    if not m and counts and max(counts) < 3:
        return None
    return {"count": max(counts) if counts else None, "why": m.group(0).strip() if m else "несколько штук"}


def rarity_keywords(text: str, extra: list[str] | None = None) -> list[str]:
    low = (text or "").lower()
    found = [label for rx, label in RARE_PATTERNS if re.search(rx, low, re.I)]
    return found


def watch_hits(text: str, words: list[str]) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if w and w.lower() in low]


def text_points(text: str) -> tuple[int, list[tuple[int, str]]]:
    """Баллы «перекуп» по тексту объявления. Возвращает (сумма, [(баллы, причина)])."""
    text = text or ""
    low = text.lower()
    pos: list[tuple[int, str]] = []
    neg: list[tuple[int, str]] = []
    for rx, pts, label in RESELLER_PHRASES:
        if re.search(rx, low):
            pos.append((pts, label))
    for rx, pts, label in PRIVATE_PHRASES:
        if re.search(rx, low):
            neg.append((pts, label))

    if len(EMOJI_RE.findall(text)) >= 3:
        pos.append((8, "много эмодзи/оформление"))
    if text.count("!") >= 3:
        pos.append((4, "много «!»"))
    caps = [w for w in CAPS_RE.findall(text) if w not in CAPS_IGNORE]
    if len(caps) >= 3:
        pos.append((4, "КАПС"))
    if len(BULLET_RE.findall(text)) >= 4:
        pos.append((6, "списки/галочки как в шаблоне"))
    if EXPERT_RE.search(text):
        pos.append((8, "экспертные термины (грейдинг, матрицы)"))

    pos_sum = min(sum(p for p, _ in pos), 40)  # текст один не должен «решать» всё
    neg_sum = max(sum(p for p, _ in neg), -30)
    reasons = sorted(pos, reverse=True) + sorted(neg)
    return pos_sum + neg_sum, reasons


# ---------------------------------------------------------------- страницы Авито (best-effort)

def _abs(href: str) -> str:
    return "https://www.avito.ru" + href if href.startswith("/") else href


def parse_ad_page(page: str) -> dict:
    """Достаёт со страницы объявления описание и сведения о продавце.

    Разметка Авито меняется, поэтому каждое поле необязательно: что не нашлось, того нет в словаре.
    Проверить, что реально читается, можно командой /check <ссылка> в Telegram."""
    soup = BeautifulSoup(page or "", "html.parser")
    out: dict = {}

    d = soup.select_one('[data-marker="item-view/item-description"]') or soup.select_one(
        '[itemprop="description"]'
    )
    desc = d.get_text("\n", strip=True) if d else ""
    if not desc:
        meta = soup.find("meta", attrs={"property": "og:description"})
        desc = (meta.get("content") or "").strip() if meta else ""
    out["description"] = desc

    pr = soup.select_one('[itemprop="price"]')
    if pr is not None and pr.get("content", "").isdigit():
        out["price"] = int(pr["content"])

    t = soup.select_one('[data-marker="item-view/title-info"]') or soup.find("h1")
    if t:
        out["title"] = t.get_text(" ", strip=True)

    a = (
        soup.select_one('[data-marker="seller-link/link"]')
        or soup.select_one('a[href*="/user/"][href*="profile"]')
        or soup.select_one('a[href*="/brands/"]')
    )
    if a and a.get("href"):
        out["seller_url"] = _abs(a["href"])
        out["seller_name"] = a.get_text(" ", strip=True)
        out["company"] = "/brands/" in a["href"]

    text = soup.get_text(" ", strip=True)
    m = re.search(r"На\s+Авито\s+с\s+([А-Яа-яЁё]+\s+\d{4})", text)
    if m:
        out["since"] = m.group(1)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*[·•]?\s*(\d+)\s*отзыв", text)
    if m:
        out["rating"], out["reviews"] = m.group(1), int(m.group(2))
    else:
        m = re.search(r"(\d+)\s*отзыв", text)
        if m:
            out["reviews"] = int(m.group(1))
    if re.search(r"Частное\s+лицо", text):
        out["private_label"] = True
    return out


def parse_profile(page: str, parse_items_fn) -> dict:
    """Профиль продавца: сколько у него объявлений и сколько из них про винил.

    Считается по первой странице списка (это нижняя оценка). Счётчики «активные»/«завершённые»
    берутся из текста страницы, если Авито их там показывает."""
    out: dict = {"items": 0, "vinyl": 0}
    try:
        items = parse_items_fn(page)
    except Exception:
        items = []
    out["items"] = len(items)
    out["vinyl"] = sum(1 for it in items if VINYL_RE.search(it.get("title", "")))
    out["titles"] = [it.get("title", "") for it in items[:5]]

    text = BeautifulSoup(page or "", "html.parser").get_text(" ", strip=True)
    m = re.search(r"(?:Завершённые|Завершенные|Завершено|Закрытые)\s*\(?(\d+)\)?", text)
    if m:
        out["closed"] = int(m.group(1))
    m = re.search(r"(?:Активные|Объявления)\s*\(?(\d+)\)?", text)
    if m:
        out["active"] = int(m.group(1))
    return out


def seller_key(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).path.rstrip("/") or None


# ---------------------------------------------------------------- Discogs

def _norm(text: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    for k, v in ALIASES.items():
        low = low.replace(k, v)
    return low


def discogs_query(title: str) -> str:
    """Название объявления → поисковая строка для Discogs (без мусорных слов)."""
    low = _norm(title)
    low = re.sub(r"[^\w\s]", " ", low)
    toks = [t for t in low.split() if t not in STOP_WORDS and not re.fullmatch(r"\d{1,4}", t)]
    return " ".join(toks[:7])


def _tokens(text: str) -> set[str]:
    low = re.sub(r"[^\w\s]", " ", _norm(text))
    return {t for t in low.split() if t not in STOP_WORDS and len(t) > 1}


def best_discogs_match(query: str, results: list[dict]) -> tuple[dict | None, float]:
    """Выбирает результат поиска Discogs, лучше всего совпавший с запросом (0..1)."""
    q = _tokens(query)
    if len(q) < 2:
        return None, 0.0
    best, best_score = None, 0.0
    for r in results:
        rt = _tokens(r.get("title", ""))
        if not rt:
            continue
        overlap = len(q & rt) / len(q)
        fuzz = difflib.SequenceMatcher(None, " ".join(sorted(q)), " ".join(sorted(rt))).ratio()
        score = 0.7 * overlap + 0.3 * fuzz
        if score > best_score:
            best, best_score = r, score
    return (best, best_score) if best_score >= 0.6 else (None, best_score)


def classify_discogs(d: dict) -> dict:
    """Популярность/редкость по данным Discogs: have = у скольких есть, want = сколько хотят."""
    have, want = int(d.get("have") or 0), int(d.get("want") or 0)
    low = d.get("lowest_usd")
    tags = []
    if want >= 300 or have >= 2000:
        tags.append("popular")
    if want >= 40 and want >= have * 0.8 and have <= 500:
        tags.append("rare")
    if low and low >= 30:
        tags.append("valuable")
    return {"tags": tags}


# ---------------------------------------------------------------- итоговая оценка

def seller_points(seller: dict | None) -> tuple[int, list[tuple[int, str]]]:
    if not seller:
        return 0, []
    pts: list[tuple[int, str]] = []
    if seller.get("company"):
        pts.append((40, "магазин/компания на Авито"))
    v = seller.get("vinyl") or 0
    if v >= 15:
        pts.append((35, f"{v}+ объявлений с винилом в профиле"))
    elif v >= 6:
        pts.append((20, f"{v} объявлений с винилом в профиле"))
    elif v >= 3:
        pts.append((8, f"{v} объявл. с винилом в профиле"))
    c = seller.get("closed") or 0
    if c >= 50:
        pts.append((20, f"{c} завершённых объявлений"))
    elif c >= 15:
        pts.append((10, f"{c} завершённых объявлений"))
    if (seller.get("reviews") or 0) >= 100:
        pts.append((8, "много отзывов (активный продавец)"))
    return sum(p for p, _ in pts), pts


def reseller_score(
    text: str, seller: dict | None = None, price: int | None = None, rub_lowest: float | None = None
) -> dict:
    """Итог: шанс перекупа в %, метка и главные причины.

    Формула: сумма баллов (профиль продавца + текст + цена относительно Discogs) переводится
    в проценты логистической кривой: 35 баллов ≈ 50%, 60 ≈ 89%, 0 ≈ 5%."""
    t_sum, t_reasons = text_points(text)
    s_sum, s_reasons = seller_points(seller)
    extra: list[tuple[int, str]] = []
    if price and rub_lowest:
        if price >= 0.8 * rub_lowest:
            extra.append((10, "цена на уровне Discogs"))
        elif price <= 0.35 * rub_lowest:
            extra.append((-8, "цена сильно ниже Discogs (продавец вряд ли знает цену)"))
    score = t_sum + s_sum + sum(p for p, _ in extra)
    pct = max(3, min(95, round(100 / (1 + math.exp(-(score - 35) / 12)))))  # 100% и 0% не бывает
    has_profile = bool(seller and (seller.get("items") or seller.get("company")))
    if not has_profile:
        pct = min(pct, 70)  # без профиля уверенность ниже
    if pct >= 55:
        label, icon = "скорее перекуп", "🔴"
    elif pct >= 25:
        label, icon = "возможно перекуп", "🟡"
    else:
        label, icon = "похоже на частника", "🟢"
    reasons = sorted(s_reasons + t_reasons + extra, key=lambda x: -abs(x[0]))
    return {
        "pct": pct, "label": label, "icon": icon, "score": score,
        "reasons": [r for _, r in reasons], "detail": reasons,
        "basis": "по профилю и тексту" if has_profile else "только по тексту, профиль не прочитан",
    }
