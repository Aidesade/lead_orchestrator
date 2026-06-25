# -*- coding: utf-8 -*-
"""
Скоринг и выбор «нужной» почты лида (для B2B-аутрича, а не поддержки).

Идея: с сайта собираются ВСЕ email с контекстом (страница + текст рядом), затем
каждый кандидат оценивается, и выбирается лучший под ТИП БИЗНЕСА:

  * ИП / микро-салон   -> личная/именная почта владельца (часто на mail.ru/
                          yandex/gmail) или почта-бренд; info@/support@ — в запас;
  * сеть / франшиза    -> почта отдела сотрудничества (franchise@/partner@/
                          b2b@/opt@/pr@/reklama@) со страниц /franchise, /partners,
                          /sotrudnichestvo; cc@/support@/zakaz@ — это поддержка, мимо.

Тип определяется автоматически по ОПФ (из Dadata), числу точек и сигналам сайта
(есть ли раздел франшизы/опта, слова «франшиза/сеть/оптом»). Если целевой почты
нет — берём общую и помечаем kind='общая', чтобы лид не терялся.

Модуль чистый (без сети): пригоден и на этапе скрейпа, и для ре-ранка после
Dadata (когда уже известны ОПФ и ФИО ЛПР) — переоценка бесплатна, в памяти.
"""
import re

# ---------------------------------------------------------------- словари ----

# почта отдела сотрудничества/закупок/маркетинга — целевая для СЕТЕЙ
COOP_KW = {
    "franchise", "franchising", "franch", "franshiza", "franchize", "fr",
    "partner", "partners", "partnership", "partnerstvo", "partnery",
    "sotrudnichestvo", "cooperation", "coop", "b2b", "opt", "optom",
    "wholesale", "dealer", "dealers", "diler", "dilery", "postavka",
    "postavshik", "postavshiki", "supplier", "supply", "zakup", "zakupki",
    "procurement", "tender", "reklama", "adv", "advert", "advertising", "ads",
    "marketing", "market", "mktg", "pr", "smm", "brand", "commerce",
    "commercial", "kommercia", "komm", "korp", "corp", "corporate", "biznes",
    "business", "razvitie", "development", "expansion",
}

# почта руководителя/собственника по должности — высокая ценность
TITLE_KW = {
    "ceo", "gd", "gendir", "gendirector", "director", "direktor", "dir",
    "owner", "vladelec", "vladeletc", "head", "boss", "founder",
    "rukovoditel", "rukovod", "upravlyayushiy", "upravlyaushiy",
}

# общие ролевые ящики — допустимы только как запас (kind='общая')
GENERIC_KW = {
    "info", "mail", "email", "office", "ofis", "contact", "contacts",
    "kontakt", "kontakty", "hello", "hi", "hey", "welcome", "zayavka",
    "zayavki", "zakaz", "zakazy", "order", "orders", "shop", "store",
    "magazin", "sale", "sales", "buy", "zapis", "zapisi", "booking", "book",
    "reception", "reservation", "salon", "beauty", "studio", "spa", "clinic",
    "klinika", "master", "admin", "administrator", "administration",
    "support", "help", "helpdesk", "care", "service", "client", "clients",
    "klient", "cc", "callcenter", "feedback", "otzyv", "otzyvy", "faq",
    "general", "main", "company", "site", "web", "fb",
    # ресторанные/сервисные ящики — это обслуживание гостей, не ЛПР
    "reservation", "reservations", "reserve", "rezerv", "rezervacia",
    "bronirovanie", "bron", "table", "tables", "stol", "stoliki", "banket",
    "banquet", "bankety", "events", "event", "catering", "delivery",
    "dostavka", "menu", "menyu", "ciao", "hostess",
}

# ящики/домены, которые НИКОГДА не берём (служебные/мусорные)
JUNK_LOCAL = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "donot-reply", "mailer-daemon", "mailerdaemon", "postmaster", "abuse",
    "root", "webmaster", "hostmaster", "example", "test", "testing", "your",
    "youremail", "your-email", "yourname", "name", "user", "username", "demo",
    "sample", "sentry", "wixpress", "nospam", "no-spam", "spam",
}
JUNK_DOMAIN = (
    "example.", "sentry.", "wixpress.", "wix.com", "schema.org", "w3.org",
    "sentry.io", "googlemail.com", "domain.com", "site.ru", "mail.example",
    "email.com", "test.", "localhost", "yourdomain", "company.com",
    "googleapis", "gstatic", "cloudflare", "jsdelivr", "bootstrapcdn",
    "fontawesome", "jquery", "ваш-сайт", "адрес",
)
# куски, выдающие «email» из картинки/трекера/верстки
JUNK_SUBSTR = ("@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
               ".bmp", "@sentry", "@example")

FREE_PROVIDERS = {
    "mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru", "yandex.ru",
    "ya.ru", "yandex.com", "yandex.by", "gmail.com", "googlemail.com",
    "rambler.ru", "icloud.com", "outlook.com", "hotmail.com", "live.ru",
    "mail.com", "protonmail.com", "proton.me", "yahoo.com", "vk.com",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", re.I)
TAGS_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)

# слаги страниц сотрудничества (для краулера и для пометки page_kind)
COOP_PATHS = (
    "/franchise", "/franshiza", "/franchising", "/franchayzing",
    "/partners", "/partnery", "/partnyoram", "/partnership", "/dlya-partnerov",
    "/sotrudnichestvo", "/cooperation", "/b2b", "/opt", "/optom",
    "/wholesale", "/dealers", "/dilery", "/postavshikam", "/postavshchikam",
    "/reklama", "/advertising", "/marketing", "/vacancies", "/career",
)
CONTACT_PATHS = ("/contacts", "/kontakty", "/contact", "/kontakt", "/contacts.html")
ABOUT_PATHS = ("/about", "/o-nas", "/o-kompanii", "/company")

# маркеры-«поддержка» в тексте рядом с почтой -> штраф
SUPPORT_CTX = re.compile(
    r"поддержк|клиентск|по вопросам заказ|по заказам|служба заботы|"
    r"горячая лини|жалоб|претензи|техническ|техподдержк|вопросы по записи",
    re.I)
# маркеры-«сотрудничество» в тексте рядом с почтой -> буст для коопа
COOP_CTX = re.compile(
    r"сотрудничеств|партн[её]р|франш|франчайз|оптов|оптом|дилер|поставщик|"
    r"коммерческ|реклам|маркетинг|по вопросам сотруднич|b2b|развити",
    re.I)
# сигналы «это сеть/франшиза» на сайте
CHAIN_SIGNAL = re.compile(
    r"франш|франчайз|наша сеть|сеть салон|сеть студи|оптов|оптом|"
    r"стать партн[её]ром|открыть по франш|дилер", re.I)


# ----------------------------------------------------- транслитерация ФИО ----

_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# неоднозначные буквы -> варианты (разные схемы транслита на сайтах)
_TR_ALT = {"ц": ("c", "ts"), "х": ("h", "kh"), "й": ("y", "i", "j"),
           "ю": ("yu", "iu", "ju"), "я": ("ya", "ia", "ja"), "ж": ("zh", "j"),
           "щ": ("sch", "shch"), "ё": ("e", "yo", "jo"), "ы": ("y", "i")}


def _translit(token, alt=False):
    out = [""]
    for ch in token.lower():
        variants = _TR_ALT.get(ch) if alt else None
        if variants:
            out = [p + v for p in out for v in variants]
        else:
            r = _TR.get(ch, ch if ch.isalnum() else "")
            out = [p + r for p in out]
        if len(out) > 8:              # не раздувать комбинации
            out = out[:8]
    return {o for o in out if o}


def fio_tokens(fio):
    """ФИО (рус/лат) -> множество латинских токенов фамилии/имени (len>=4) для
    матчинга с локал-партом почты. Отчество тоже берём, но как слабый сигнал."""
    if not fio:
        return set(), set()
    parts = [p for p in re.split(r"[\s,]+", fio.strip()) if len(p) >= 3]
    strong, weak = set(), set()       # strong: фамилия+имя; weak: отчество
    for i, p in enumerate(parts):
        toks = _translit(p) | _translit(p, alt=True)
        toks = {t for t in toks if len(t) >= 4}
        if i < 2:
            strong |= toks
        else:
            weak |= toks
    return strong, weak


_STOP_BRAND = re.compile(
    r"\b(ооо|оао|зао|пао|нао|ао|ип|студия|студи|салон|салоны|центр|клиника|"
    r"клиника|сеть|красоты|красота|маникюра|ногтев|парикмахерск|барбершоп|"
    r"barbershop|beauty|spa|спа|nails|nail|hair|studio|by)\b", re.I)


def brand_tokens(name, website=""):
    """Название компании + домен -> латинские токены бренда (len>=4) для
    распознавания почты-бренда (`sodanails@`, `luxe-hair@`)."""
    toks = set()
    base = _STOP_BRAND.sub(" ", (name or "").lower())
    for w in re.split(r"[^a-zа-яё0-9]+", base):
        if len(w) < 4:
            continue
        if re.search(r"[а-яё]", w):
            toks |= {t for t in _translit(w) if len(t) >= 4}
        else:
            toks.add(w)
    # second-level домена (sodanails.ru -> sodanails)
    m = re.search(r"([a-z0-9\-]+)\.[a-z]{2,}(?:/|$)", (website or "").lower())
    if m and len(m.group(1)) >= 4:
        toks.add(m.group(1).replace("-", ""))
    return {t for t in toks if t not in ("www", "site", "home")}


# --------------------------------------------------- разбор email со страницы ----

def _deobfuscate(text):
    """Раскрыть «почта (собака) domain точка ru» и подобное."""
    t = text
    t = re.sub(r"\s*\(?\s*(?:at|собака|dog|@)\s*\)?\s*", "@",
               t, flags=re.I) if False else t   # осторожно: только узкие формы ниже
    t = re.sub(r"\s*[\(\[]\s*(?:at|собака|dog)\s*[\)\]]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s+(?:at|собака)\s+", "@", t, flags=re.I)
    t = re.sub(r"\s*[\(\[]\s*(?:dot|точка)\s*[\)\]]\s*", ".", t, flags=re.I)
    t = re.sub(r"\s+(?:dot|точка)\s+", ".", t, flags=re.I)
    t = t.replace("&#64;", "@").replace("&#46;", ".")
    return t


def _strip_tags(html):
    text = TAGS_RE.sub(" ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"[ \t]+", " ", text)


_PK_PRIORITY = {"coop": 3, "contacts": 2, "root": 1, "about": 0, "other": 0}


def dedup_candidates(cands):
    """Схлопнуть кандидатов по email (один и тот же адрес в футере каждой
    страницы). Оставляем вхождение с самой «говорящей» страницей (сотрудничество
    > контакты > корень) и самым длинным контекстом."""
    best = {}
    for c in cands or []:
        e = (c.get("email") or "").lower()
        if not e:
            continue
        rank = (_PK_PRIORITY.get(c.get("page_kind"), 0), len(c.get("context") or ""))
        cur = best.get(e)
        if cur is None or rank > cur[0]:
            best[e] = (rank, c)
    return [v[1] for v in best.values()]


def page_kind(path):
    p = (path or "").lower()
    if p in ("", "/"):
        return "root"
    if any(p.startswith(c) for c in COOP_PATHS):
        return "coop"
    if any(p.startswith(c) for c in CONTACT_PATHS):
        return "contacts"
    if any(p.startswith(c) for c in ABOUT_PATHS):
        return "about"
    return "other"


def extract_candidates(html, path="/"):
    """Из HTML страницы -> список кандидатов:
       {email, page, page_kind, context} (context — текст ±100 симв. вокруг)."""
    if not html:
        return []
    kind = page_kind(path)
    raw_emails = set(MAILTO_RE.findall(html))
    text = _deobfuscate(_strip_tags(html))
    low = text.lower()
    out = []
    seen = set()

    def add(email, ctx):
        e = email.strip().strip(".,;:").lower()
        if not e or e in seen:
            return
        seen.add(e)
        out.append({"email": e, "page": path or "/", "page_kind": kind,
                    "context": (ctx or "")[:200].lower()})

    # из видимого текста — с контекстом
    for m in EMAIL_RE.finditer(text):
        e = m.group(0)
        s, f = max(0, m.start() - 100), min(len(text), m.end() + 100)
        add(e, low[s:f])
    # из mailto — контекст ищем в тексте, иначе пусто
    for e in raw_emails:
        idx = low.find(e.lower())
        ctx = low[max(0, idx - 100):idx + 100] if idx >= 0 else ""
        add(e, ctx)
    return out


# ------------------------------------------------------------- классификация ----

def is_junk(email):
    e = email.lower()
    if any(s in e for s in JUNK_SUBSTR):
        return True
    if "@" not in e:
        return True
    local, _, domain = e.partition("@")
    base_local = re.sub(r"[._\-]?\d+$", "", local)
    if local in JUNK_LOCAL or base_local in JUNK_LOCAL:
        return True
    if any(d in domain for d in JUNK_DOMAIN):
        return True
    if len(local) > 40 or re.fullmatch(r"[0-9a-f]{16,}", local):  # хэш-трекер
        return True
    return False


def _local_parts(local):
    """Локал-парт -> (нормализованный без разделителей, список токенов)."""
    norm = re.sub(r"[._\-+]", "", local.lower())
    toks = [t for t in re.split(r"[._\-+]", local.lower()) if t]
    return norm, toks


def classify(email, brand, fio_strong, fio_weak):
    """Вернуть (class, signals) для одного email.
    class: personal_lpr | personal | title | cooperation | brand | generic | junk
    """
    if is_junk(email):
        return "junk", {}
    local = email.split("@", 1)[0].lower()
    norm, toks = _local_parts(local)
    sig = {}

    # 1) совпадение с ФИО ЛПР -> личная почта владельца (сильнейший сигнал)
    if fio_strong and any(len(t) >= 4 and (t in norm or norm in t) for t in fio_strong):
        sig["fio"] = "strong"
        return "personal_lpr", sig
    if fio_weak and any(len(t) >= 5 and t in norm for t in fio_weak):
        sig["fio"] = "weak"
        return "personal_lpr", sig

    # 2) должность руководителя в ящике (director@, ceo@, vladelec@)
    if any(t in TITLE_KW for t in toks) or norm in TITLE_KW:
        return "title", sig

    # 3) отдел сотрудничества/закупок/маркетинга
    if any(t in COOP_KW for t in toks) or norm in COOP_KW:
        return "cooperation", sig

    # 4) общий ролевой ящик
    if any(t in GENERIC_KW for t in toks) or norm in GENERIC_KW:
        return "generic", sig

    # 5) почта-бренд (локал-парт = имя компании/домен) — для ИП почти всегда владелец
    if brand and any(len(t) >= 4 and (t in norm or norm in t) for t in brand):
        sig["brand"] = True
        return "brand", sig

    # 6) имя.фамилия (два алфа-токена) -> вероятно личная
    name_like = [t for t in toks if t.isalpha() and len(t) >= 2]
    if len(name_like) >= 2 and not any(t in GENERIC_KW for t in name_like):
        sig["name_pattern"] = True
        return "personal", sig

    return "generic", sig


# --------------------------------------------------- определение типа бизнеса ----

def detect_business_type(lead, candidates=None, pages_text=""):
    """'chain' | 'micro' | 'unknown' по ОПФ, числу точек и сигналам сайта."""
    opf = (lead.get("_opf") or "").upper()
    branch = lead.get("_branch_count") or 0
    coop_page = any(c.get("page_kind") == "coop" for c in (candidates or []))
    chain_words = bool(CHAIN_SIGNAL.search(pages_text or ""))

    if opf == "ИП":
        # ИП почти всегда микро; но франшиза-ИП бывает -> сильные сигналы сети перевесят
        if coop_page or chain_words or branch >= 3:
            return "chain"
        return "micro"
    if opf in ("ООО", "АО", "ПАО", "НАО", "ЗАО", "ОАО"):
        if coop_page or chain_words or branch >= 2:
            return "chain"
        return "micro"          # одиночное ООО ведёт себя как микро (владелец рулит)
    # ОПФ неизвестен (Dadata не отрабатывал)
    if coop_page or chain_words:
        return "chain"
    return "unknown"


# ----------------------------------------------------------------- скоринг ----

# базовые веса класса по типу бизнеса
_BASE = {
    "micro":   {"personal_lpr": 1000, "personal": 720, "brand": 680,
                "title": 520, "cooperation": 320, "generic": 120},
    "chain":   {"personal_lpr": 760, "cooperation": 880, "title": 820,
                "personal": 520, "brand": 240, "generic": 130},
    "unknown": {"personal_lpr": 950, "personal": 660, "cooperation": 600,
                "title": 680, "brand": 480, "generic": 120},
}

_KIND_RU = {
    "personal_lpr": "личная ЛПР", "personal": "личная",
    "title": "руководитель", "cooperation": "сотрудничество",
    "brand": "почта компании", "generic": "общая",
}


def score_candidate(cand, btype, brand, fio_strong, fio_weak):
    cls, sig = classify(cand["email"], brand, fio_strong, fio_weak)
    if cls == "junk":
        return None
    score = _BASE[btype].get(cls, 120)
    ctx = cand.get("context", "")
    pk = cand.get("page_kind")
    domain = cand["email"].split("@", 1)[1].lower()
    is_free = domain in FREE_PROVIDERS

    # страница/контекст сотрудничества усиливают кооп/должностные ящики
    if cls in ("cooperation", "title"):
        if pk == "coop":
            score += 140 if btype == "chain" else 50
        if COOP_CTX.search(ctx):
            score += 90 if btype == "chain" else 30
    # «поддержка» рядом -> это саппорт-ящик, вниз
    if SUPPORT_CTX.search(ctx):
        score -= 120
    # домен сайта vs free-провайдер
    if is_free:
        score += 50 if btype == "micro" else (-40 if btype == "chain" else 0)
    else:
        if btype == "chain" and cls in ("cooperation", "title"):
            score += 60
    # личную почту на странице контактов/футере чуть поднимем (где она и живёт)
    if cls in ("personal_lpr", "personal", "brand") and pk in ("root", "contacts"):
        score += 20

    is_target = cls in ("personal_lpr", "personal", "title", "cooperation") or (
        cls == "brand" and btype in ("micro", "unknown"))
    return {"email": cand["email"], "class": cls, "kind": _KIND_RU[cls],
            "score": score, "is_target": is_target, "page": cand.get("page", ""),
            "signals": sig}


def pick_best(candidates, lead, fallback_generic=True, pages_text=""):
    """Выбрать лучший email. Возвращает dict или None.
    {email, kind, class, is_target, score, page} — kind по-русски для Excel."""
    cands = list(candidates or [])
    if not cands:
        return None
    brand = brand_tokens(lead.get("name", ""), lead.get("website", ""))
    fio_s, fio_w = fio_tokens(lead.get("contact_person", ""))
    btype = detect_business_type(lead, cands, pages_text)

    scored = []
    best_by_email = {}
    for c in cands:
        s = score_candidate(c, btype, brand, fio_s, fio_w)
        if not s:
            continue
        # один email мог встретиться на нескольких страницах — берём макс.балл
        prev = best_by_email.get(s["email"])
        if prev is None or s["score"] > prev["score"]:
            best_by_email[s["email"]] = s
    scored = sorted(best_by_email.values(), key=lambda x: -x["score"])
    if not scored:
        return None
    top = scored[0]
    top["business_type"] = btype
    if not top["is_target"] and not fallback_generic:
        return None
    return top


if __name__ == "__main__":
    # дымовой тест классификатора
    import json
    samples = [
        ("franchise@tanuki.ru", "Тануки", ""),
        ("cc@tanuki.ru", "Тануки", ""),
        ("kormilcev@sodanails.ru", "Soda студия маникюра", "Кормильцев Дмитрий"),
        ("info@luxe-hair.ru", "Luxe Hair", ""),
        ("sodanails@yandex.ru", "Soda студия маникюра", ""),
        ("noreply@site.ru", "X", ""),
    ]
    for email, name, fio in samples:
        b = brand_tokens(name)
        fs, fw = fio_tokens(fio)
        print(f"{email:32} -> {classify(email, b, fs, fw)}")
