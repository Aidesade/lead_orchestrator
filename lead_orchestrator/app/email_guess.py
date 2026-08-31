# -*- coding: utf-8 -*-
r"""
email_guess — гипотезы КОРПОРАТИВНОЙ почты ключевых сотрудников компании по ФИО
и общему домену («то, что после собаки»).

Зачем отдельный модуль. На реальных данных проекта (936 адресов из D:\лиды и
D:\orq_cache) публичная почта компании почти никогда не является почтой ЛПР: из
420 пар «ФИО руководителя + опубликованный email компании» фамилию руководителя
содержат 3 адреса (0.7%). Значит прямой рабочий адрес не «находится», а
ДОСТРАИВАЕТСЯ по схеме домена — и это единственный путь к личному ящику.

Что делает:
  1. Определяет корпоративный домен лида: сначала из общей почты (info@dom.ru ->
     dom.ru — она уже проверена Фазой 1), потом из website.
  2. Собирает ключевых сотрудников: руководитель из ЕГРЮЛ (contact_person лида) +
     leadership/procurement/branches из структурного JSON находок движка.
  3. Генерирует РАНЖИРОВАННЫЕ кандидаты локал-парта по каталогу схем (ранги — из
     эмпирики выше: фамилия+инициалы слитно 22.5%, и.фамилия 9.8%, фамилия 12.3%,
     имя.фамилия 4.1% и т.д.) с несколькими профилями транслитерации.
  4. ГЛАВНЫЙ множитель точности — вывод схемы самого домена: если известен хотя бы
     один личный адрес сотрудника (petrov.a@dom.ru), схема фиксируется и выдача
     схлопывается с десятка гипотез до одной-двух. На корпусе 72.8% адресов домена
     ложатся в его доминирующую схему.
  5. Проверяет домен по MX (DNS-over-HTTPS, с фолбэком на A/AAAA — implicit MX
     RFC 5321) и опционально пробует SMTP RCPT одной сессией на домен.
  6. `verify_addresses` — существует ли ящик, БЕЗ отправки письма. Вес вердикта
     задаёт приёмник домена: Яндекс 360 отвечает честно, mail.ru «принимает» что
     угодно от незнакомых пробников (верим только отказу), Exchange Online принимает
     всё и проверяет получателя после DATA (пробу не делаем вовсе). Отдельно
     различаются catch-all, отказ по политике (550 5.7.x) и несостоявшаяся проба —
     ни то, ни другое, ни третье не означает «ящика нет».

Чего здесь НЕТ и не будет: перебора локал-партов пачками, «пробива», breach-баз,
people-search — см. person_enrich.DENY_SOURCES. Кандидаты помечаются честной
уверенностью, а не выдаются за найденный факт.

CLI:
  py email_guess.py "Руденко Сергей Александрович" avtodor-rzn.ru
  py email_guess.py --leads "D:\лиды\leads_mining.json" --out guesses.json
  py email_guess.py --leads leads.json --smtp        # + SMTP-проба (медленно)
"""
import argparse
import gzip
import io
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import email_finder as EF
import harvest_inn_site as HIS

# ============================================================================
# ТРАНСЛИТЕРАЦИЯ ФИО
# ============================================================================
# email_finder._translit даёт декартово произведение вариантов по буквам и режет
# его капом ВНУТРИ цикла — для «Хожуцкий» все 8 вариантов остаются на h, ветка kh
# гибнет целиком. Здесь вместо перебора букв — три ИМЕНОВАННЫЕ схемы, каждая
# детерминированная; кандидатов на токен максимум три, канон всегда первый.

_COMMON = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# упрощённая схема, которую любят небольшие компании и старые почтовые админы
_SIMPLE = dict(_COMMON, **{"х": "h", "ц": "c", "щ": "sch", "ж": "j"})
# загранпаспорт (ICAO Doc 9303) — так пишут в кадровом учёте и крупных холдингах
_ICAO = dict(_COMMON, **{"й": "i", "ю": "iu", "я": "ia", "ъ": "ie"})

PROFILES = ("common", "simple", "icao")
_TABLES = {"common": _COMMON, "simple": _SIMPLE, "icao": _ICAO}

# Устоявшиеся латинские написания частых русских имён. Транслитерация по буквам
# их не даёт («Александр» -> aleksandr, а в почте живёт alexander), а имён мало —
# словарь закрывает большую часть выборки.
NAME_LAT = {
    "александр": ("alexander", "alexandr", "aleksandr"),
    "александра": ("alexandra", "aleksandra"),
    "алексей": ("alexey", "aleksey", "alexei"),
    "анатолий": ("anatoly", "anatoliy"),
    "андрей": ("andrey", "andrei"),
    "артём": ("artem", "artyom"), "артем": ("artem", "artyom"),
    "василий": ("vasily", "vasiliy"),
    "виктор": ("viktor", "victor"),
    "виталий": ("vitaly", "vitaliy"),
    "владимир": ("vladimir",),
    "вячеслав": ("vyacheslav", "viacheslav"),
    "геннадий": ("gennady", "gennadiy"),
    "григорий": ("grigory", "grigoriy"),
    "дмитрий": ("dmitry", "dmitriy", "dmitri"),
    "евгений": ("evgeny", "evgeniy", "yevgeny"),
    "евгения": ("evgenia", "evgeniya"),
    "екатерина": ("ekaterina", "katerina"),
    "елена": ("elena", "helena"),
    "зоя": ("zoya",),
    "игорь": ("igor",),
    "илья": ("ilya", "ilia"),
    "ирина": ("irina",),
    "юлия": ("yulia", "julia", "yuliya"),
    "юрий": ("yury", "yuriy", "yuri"),
    "константин": ("konstantin",),
    "ксения": ("ksenia", "kseniya"),
    "лариса": ("larisa",),
    "леонид": ("leonid",),
    "любовь": ("lyubov", "liubov"),
    "людмила": ("lyudmila", "liudmila"),
    "максим": ("maxim", "maksim"),
    "марина": ("marina",),
    "мария": ("maria", "mariya"),
    "михаил": ("mikhail", "michail"),
    "надежда": ("nadezhda",),
    "наталья": ("natalya", "natalia"), "наталия": ("natalia", "nataliya"),
    "николай": ("nikolay", "nikolai"),
    "олег": ("oleg",),
    "ольга": ("olga",),
    "оксана": ("oksana",),
    "павел": ("pavel",),
    "пётр": ("petr", "pyotr"), "петр": ("petr", "pyotr"),
    "роман": ("roman",),
    "светлана": ("svetlana",),
    "сергей": ("sergey", "sergei", "serg"),
    "станислав": ("stanislav",),
    "тамара": ("tamara",),
    "татьяна": ("tatyana", "tatiana"),
    "фёдор": ("fedor", "fyodor"), "федор": ("fedor", "fyodor"),
    "эдуард": ("eduard", "edward"),
    "элла": ("ella",),
    "яна": ("yana",),
    # частые имена без особенностей написания — нужны как словарь «это имя, а не
    # фамилия»: женские имена оканчиваются так же, как фамилии
    "иван": ("ivan",), "анна": ("anna",), "антон": ("anton",), "борис": ("boris",),
    "вадим": ("vadim",), "валентина": ("valentina",), "валерий": ("valery", "valeriy"),
    "вера": ("vera",), "галина": ("galina",), "денис": ("denis",), "егор": ("egor",),
    "инна": ("inna",), "кирилл": ("kirill",), "лидия": ("lidia", "lidiya"),
    "нина": ("nina",), "полина": ("polina",), "раиса": ("raisa",), "степан": ("stepan",),
    "алла": ("alla",), "жанна": ("zhanna",), "олеся": ("olesya", "olesia"),
    "софья": ("sofya", "sofia"), "тимур": ("timur",), "тарас": ("taras",),
    "марат": ("marat",), "ринат": ("rinat",), "рустам": ("rustam",), "азат": ("azat",),
    "ильдар": ("ildar",), "айрат": ("ayrat", "airat"), "радик": ("radik",),
    "фарид": ("farid",), "руслан": ("ruslan",), "артур": ("artur", "arthur"),
}

_VOWELS = "аеёиоуыэюя"


def _apply_rules(token, profile):
    """Буквенные правила ДО посимвольной таблицы: они меняют не букву, а сочетание.
    Мягкий знак перед гласной звучит как «й»: Афанасьева -> afanasyeva."""
    t = token.lower()
    if profile != "icao":
        t = re.sub(r"[ьъ]([" + _VOWELS + r"])",
                   lambda m: "y" + _COMMON.get(m.group(1), ""), t)
    return t


def _post_rules(lat, profile):
    """Окончания, которые в почте пишут короче, чем даёт побуквенная таблица."""
    if profile == "common":
        # -ий/-ый в конце схлопывается в -y: Дмитрий -> dmitry, Юрий -> yury
        return re.sub(r"(iy|yy)$", "y", lat)
    if profile == "icao":
        return re.sub(r"iy$", "ii", lat)
    return lat


def translit(token, profile="common"):
    """Один токен ФИО -> латиница по ИМЕНОВАННОЙ схеме (без комбинаторики)."""
    table = _TABLES.get(profile, _COMMON)
    # дефис двойной фамилии сохраняем: saltykov-schedrin@ — валидный локал-парт
    out = [table.get(ch, ch if (ch.isalnum() or ch == "-") else "")
           for ch in _apply_rules(token, profile)]
    return _post_rules("".join(out), profile)


def initial_lat(ch):
    """Кириллический инициал -> латинская буква. Без этого «Иванов И.И.» терял
    все схемы с инициалами: локал-парт с кириллицей не проходит валидацию."""
    ch = (ch or "").strip().lower()
    if not ch:
        return ""
    if re.fullmatch(r"[a-z]", ch):
        return ch
    return translit(ch, "common")[:1]


def translit_variants(token, role="surname", limit=3):
    """Токен ФИО -> упорядоченные варианты латиницы, канон первым.
    role: surname | name | patronymic (для отчества хватает одного варианта)."""
    token = (token or "").strip().lower()
    if not token:
        return []
    if re.fullmatch(r"[a-z][a-z'\-]*", token):          # уже латиница — не трогаем
        return [token.replace("'", "")]
    out = []
    if role == "name":
        out.extend(NAME_LAT.get(token, ()))
    for p in PROFILES:
        # в упрощённой схеме ц->c и х->h склеиваются в чужие звуки: Цхай -> chay
        # (читается как «Чай»), Схимин -> shimin. Такой вариант — мусор, не гипотеза.
        if p == "simple" and re.search(r"цх|сх", token):
            continue
        v = translit(token, p)
        if v:
            out.append(v)
    # «кс» пишут через x: Александр -> alexandr, Алексей -> alexey
    if "кс" in token:
        out.append(translit(token, "common").replace("ks", "x"))
    # начальное «е» иногда пишут ye — вариант, но НЕ канон: Егоров чаще egorov
    if token.startswith("е"):
        out.append("ye" + translit(token, "common")[1:])
    seen, uniq = set(), []
    for v in out:
        v = re.sub(r"[^a-z0-9\-]", "", v)
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[: (1 if role == "patronymic" else limit)]


# ============================================================================
# РАЗБОР ФИО
# ============================================================================
# В contact_person Фазы 1 иногда лежит не человек, а управляющая организация —
# строить ей личную почту бессмысленно.
_ORG_MARK = re.compile(
    r"обществ[оа]\s+с\s+огранич|общество|\bооо\b|\bоао\b|\bзао\b|\bпао\b|\bао\b|"
    r"\bнао\b|\bгуп\b|\bмуп\b|\bфгуп\b|\bгбу\b|компани|корпораци|управляющ|«|\"", re.I)


def is_person(fio):
    """True, если строка похожа на ФИО человека, а не на организацию."""
    s = (fio or "").strip()
    if len(s) < 5 or _ORG_MARK.search(s):
        return False
    return len([t for t in re.split(r"[\s.]+", s) if t]) >= 2


_RE_SURNAME_END = re.compile(
    r"(ов|ев|ёв|ин|ын|ский|ская|цкий|цкая|ко|ук|юк|ян|дзе|швили|"
    r"ov|ev|in|yn|sky|skiy|skaya|tsky|enko|uk|yuk|yan|dze|shvili)$", re.I)


def _looks_surname(token):
    return bool(_RE_SURNAME_END.search((token or "").lower()))


def split_fio(fio):
    """«Фамилия Имя Отчество» -> dict(surname, name, patronymic, n_ini, p_ini).

    Понимает и «Иванов И.И.» (из находок движка), и «Иван Иванов» (латиница/СМИ).
    Возвращает None, если это не человек."""
    s = re.sub(r"\s+", " ", (fio or "").strip())
    if not is_person(s):
        return None
    # форма «Иванов И.И.» / «Иванов И. И.»
    m = re.match(r"^([А-ЯЁA-Za-zа-яё\-]{2,})\s+([А-ЯЁA-Z])\.\s*(?:([А-ЯЁA-Z])\.)?$", s)
    if m:
        return {"surname": m.group(1), "name": "", "patronymic": "",
                "n_ini": m.group(2).lower(), "p_ini": (m.group(3) or "").lower()}
    parts = [p for p in s.split(" ") if p]
    # «оглы/кызы» — часть отчества, а не четвёртое слово
    if len(parts) > 3 and parts[-1].lower() in ("оглы", "кызы", "угли", "гызы"):
        parts = parts[:2] + [" ".join(parts[2:])]
    if len(parts) < 2:
        return None
    surname, name = parts[0], parts[1]
    patronymic = parts[2] if len(parts) > 2 else ""
    if not patronymic:
        # СМИ и сайты пишут «Имя Фамилия». Разворачиваем осторожно: женские имена
        # оканчиваются так же, как фамилии («Ким Татьяна»), поэтому известное имя
        # в словаре весомее окончания.
        first_name, second_name = surname.lower() in NAME_LAT, name.lower() in NAME_LAT
        if first_name and not second_name:
            surname, name = name, surname
        elif not second_name and _looks_surname(name) and not _looks_surname(surname):
            surname, name = name, surname
    return {"surname": surname, "name": name, "patronymic": patronymic,
            "n_ini": "", "p_ini": ""}


# ============================================================================
# КАТАЛОГ СХЕМ ЛОКАЛ-ПАРТА
# ============================================================================
# S — фамилия, N — имя, i — инициал имени, o — инициал отчества.
# rank — эмпирический вес по 316 персональным адресам корпоративных доменов.
SCHEMES = (
    ("S{io}",   100, "{S}{i}{o}"),      # ivanovai — доминанта РФ (Exchange/AD)
    ("i.S",      95, "{i}.{S}"),        # a.ivanov — Yandex 360 / Bitrix24
    ("S",        90, "{S}"),            # ivanov
    ("N.S",      80, "{N}.{S}"),        # anna.ivanova — холдинги, .com
    ("S.io",     70, "{S}.{i}{o}"),
    ("S{i}",     65, "{S}{i}"),
    ("iS",       60, "{i}{S}"),
    ("io.S",     58, "{i}{o}.{S}"),
    ("i_S",      55, "{i}_{S}"),
    ("S_io",     52, "{S}_{i}{o}"),
    ("S.i",      50, "{S}.{i}"),
    ("N_S",      45, "{N}_{S}"),
    ("NS",       40, "{N}{S}"),
    ("S.N",      35, "{S}.{N}"),
    ("i.o.S",    30, "{i}.{o}.{S}"),
    ("S-N",      25, "{S}-{N}"),
    ("io",       22, "{i}{o}"),         # только инициалы — коротко и коллизийно, но частотно
    ("N",        18, "{N}"),
)
_SCHEME_BY_ID = {sid: tpl for sid, _, tpl in SCHEMES}
_RANK_BY_ID = {sid: rank for sid, rank, _ in SCHEMES}

# Формы, неразличимые без сверки с ФИО: по одному лишь ivanov@ нельзя сказать,
# это «голая фамилия» или «фамилия+инициалы». Фиксировать домену конкретную схему
# в таком случае нельзя — сужаем выдачу до группы, а не до одного шаблона.
SCHEME_GROUPS = {
    "solid": ("S{io}", "S", "S{i}", "iS", "NS"),
    "initials": ("io", "N"),
}

_LOCAL_OK = re.compile(r"^[a-z0-9][a-z0-9._\-]{1,62}$")
_SEPS = (".", "_", "-")


def _with_sep(template, sep):
    """Тот же шаблон, но с разделителем, принятым на домене: {S}.{i} -> {S}-{i}.
    Домены с дефисом/подчёркиванием встречаются, и без этого модуль предлагал бы
    несуществующий адрес с точкой вместо реального."""
    if not sep or sep not in _SEPS:
        return template
    return re.sub(r"(?<=\})[._\-](?=\{)", sep, template)


def _render(template, sur, nam, ini, pini, sep=None):
    # пустой плейсхолдер обязан рушить рендер, а не молча выпадать: иначе «S{io}»
    # без инициалов даёт тот же ivanov@, что и схема «S», и сверка с ФИО ложно
    # опознаёт домену чужую схему
    for placeholder, value in (("{S}", sur), ("{N}", nam), ("{i}", ini), ("{o}", pini)):
        if placeholder in template and not value:
            return ""
    try:
        local = _with_sep(template, sep).format(S=sur, N=nam, i=ini, o=pini)
    except (KeyError, IndexError):
        return ""
    local = re.sub(r"([._\-])[._\-]+", r"\1", local).strip("._-")
    return local if _LOCAL_OK.match(local) else ""


def guess_emails(fio, domain, scheme=None, limit=10, profile=None, sep=None):
    """ФИО + домен -> ранжированные кандидаты почты.

    scheme  — id схемы из SCHEMES: если известен «почерк» домена, выдача
              схлопывается до этой схемы (плюс её ближайший вариант).
    sep     — разделитель, принятый на домене ('.', '_' или '-').
    profile — профиль транслитерации домена, если он выведен из известных адресов.
    Возвращает список dict(email, scheme, translit, score)."""
    parts = split_fio(fio)
    domain = (domain or "").strip().lower().lstrip("@")
    if not parts or not domain:
        return []

    surs = translit_variants(parts["surname"], "surname")
    nams = translit_variants(parts["name"], "name") if parts["name"] else []
    pats = translit_variants(parts["patronymic"], "patronymic") if parts["patronymic"] else []
    # инициалы: из ФИО либо уже готовые («Иванов И.И.»)
    inis = [initial_lat(parts["n_ini"])] if parts["n_ini"] else [n[:1] for n in nams[:2]]
    pinis = [initial_lat(parts["p_ini"])] if parts["p_ini"] else [p[:1] for p in pats[:1]]
    if profile:                                    # почерк домена важнее общего канона
        pref = translit(parts["surname"], profile)
        if pref in surs:
            surs = [pref] + [s for s in surs if s != pref]

    if scheme in _SCHEME_BY_ID:
        active = [(scheme, _RANK_BY_ID[scheme], _SCHEME_BY_ID[scheme])]
    elif scheme in SCHEME_GROUPS:
        active = [s for s in SCHEMES if s[0] in SCHEME_GROUPS[scheme]]
    else:
        active = list(SCHEMES)

    pini = pinis[0] if pinis else ""
    out, seen = [], set()
    for sid, rank, tpl in active:
        need_name = "{N}" in tpl or "{i}" in tpl
        need_patronymic = "{o}" in tpl
        if (need_name and not (nams or inis)) or (need_patronymic and not pinis):
            continue
        for si, sur in enumerate(surs):
            for ni, nam in enumerate(nams or [""]):
                ini = nam[:1] or (inis[0] if inis else "")
                local = _render(tpl, sur, nam, ini, pini, sep)
                if not local:
                    continue
                addr = f"{local}@{domain}"
                if addr in seen:
                    continue
                seen.add(addr)
                # вес схемы гасится «неканоничностью» транслита
                score = rank * (1.0, 0.6, 0.4)[min(si, 2)] * (1.0, 0.7, 0.5)[min(ni, 2)]
                out.append({"email": addr, "scheme": sid, "score": round(score, 1),
                            "translit": sur})
    out.sort(key=lambda x: -x["score"])
    if not out and scheme:
        # схема домена требует данных, которых нет (например отчества) — не оставлять
        # человека вовсе без гипотез, вернуться к общему каталогу
        return guess_emails(fio, domain, scheme=None, limit=limit, profile=profile, sep=sep)
    return out[:limit]


# ============================================================================
# ВЫВОД СХЕМЫ ДОМЕНА ПО ИЗВЕСТНЫМ АДРЕСАМ
# ============================================================================
# Словари email_finder собраны под B2C (салоны, рестораны) — канцелярских ящиков
# российских предприятий там нет, и priemnaya@ принималась за личную почту, из-за
# чего домену приписывалась несуществующая схема.
RU_ROLE_WORDS = {
    "priemnaya", "priemnaia", "priyomnaya", "sekretar", "secretariat", "kanc",
    "kancelyariya", "kanceljarija", "buh", "buhgalter", "buhgalteria",
    "bukhgalteria", "buhg", "kadry", "otdelkadrov", "personal", "hr", "ok",
    "otdel", "otdelprodazh", "sbyt", "snab", "snabzhenie", "zakup", "zakupka",
    "tender", "tenders", "dogovor", "dogovory", "pto", "oks", "ohrana", "sklad",
    "dispetcher", "dispatcher", "priem", "office", "ofis", "obrashenie",
    "obrasheniya", "documents", "docs", "delo", "deloproizvodstvo", "press",
    "presssluzhba", "smi", "it", "sysadmin", "servicedesk", "helpdesk",
    "jurist", "yurist", "legal", "econom", "ekonom", "plan", "finans",
}
_ROLE_WORDS = EF.GENERIC_KW | EF.COOP_KW | EF.TITLE_KW | RU_ROLE_WORDS


def is_role_mailbox(email):
    """info@/zakupki@/director@ — ролевой ящик, для вывода схемы бесполезен."""
    local = (email or "").split("@", 1)[0].lower()
    norm = re.sub(r"[._\-+]", "", local)
    toks = [t for t in re.split(r"[._\-+]", local) if t]
    return norm in _ROLE_WORDS or any(t in _ROLE_WORDS for t in toks)


def _by_form(scheme, sep=""):
    """Схема, опознанная только по ФОРМЕ локал-парта: без ФИО владельца ни профиль
    транслитерации, ни точность подтвердить нечем."""
    return {"scheme": scheme, "sep": sep, "profile": None, "exact": False}


def parse_local(email, fio=None):
    """Разобрать локал-парт известного адреса в схему.

    Если ФИО владельца известно — схема определяется точно (сверкой с вариантами
    транслита). Если нет — по форме: где стоят однобуквенные токены."""
    local = (email or "").split("@", 1)[0].lower()
    if not local or is_role_mailbox(email):
        return None
    toks = [t for t in re.split(r"[._\-]", local) if t]
    sep = next((s for s in _SEPS if s in local), "")

    if fio:
        parts = split_fio(fio)
        if parts:
            for prof in PROFILES:
                sur = translit(parts["surname"], prof)
                nam = translit(parts["name"], prof) if parts["name"] else ""
                ini = (nam[:1] if nam else "") or initial_lat(parts["n_ini"])
                pini = (translit(parts["patronymic"], prof)[:1] if parts["patronymic"]
                        else initial_lat(parts["p_ini"]))
                for sid, _rank, tpl in SCHEMES:
                    # разделитель берём фактический: petrov-a@ — та же схема S.i,
                    # просто домен пишет её через дефис
                    if _render(tpl, sur, nam, ini, pini, sep) == local:
                        return {"scheme": sid, "sep": sep, "profile": prof, "exact": True}

    # без ФИО — по форме токенов. Слитную форму нельзя свести к одному шаблону:
    # ivanov@ это и «фамилия», и «фамилия+инициалы» — отдаём группу схем
    if len(toks) == 1:
        t = toks[0]
        return _by_form("initials" if len(t) <= 3 and t.isalpha() else "solid")
    if len(toks) == 2:
        a, b = toks
        if len(a) <= 2 and len(b) > 2:
            return _by_form("i.S" if len(a) == 1 else "io.S", sep)
        if len(b) <= 2 and len(a) > 2:
            return _by_form("S.i" if len(b) == 1 else "S.io", sep)
        return _by_form("N.S", sep)
    if len(toks) == 3 and len(toks[0]) == 1 and len(toks[1]) == 1:
        return _by_form("i.o.S", sep)
    return None


def belongs_to(email, fio):
    """Адрес построен именно из этого ФИО? Строгая проверка — сверка со всеми
    схемами и профилями транслита.

    Нестрогий матч по токенам (email_finder.classify) здесь не годится: «Пётр»
    как имя даёт токен petr, который входит в чужую фамилию petrov — и адрес
    главного инженера приписывается директору филиала."""
    got = parse_local(email, fio)
    return bool(got and got.get("exact"))


def link_known_to_people(known, people):
    """Проставить известным адресам домена ФИО их владельцев — только так схема
    домена выводится точно, а не по форме локал-парта."""
    for item in known:
        if item.get("fio"):
            continue
        for person in people or []:
            if belongs_to(item["email"], person.get("fio")):
                item["fio"] = person["fio"]
                break
    return known


def infer_scheme(known):
    """Известные адреса домена -> его «почерк».

    known: [{'email': ..., 'fio': ...(опц.)}]. Годится и ОДИН адрес: «n.ivanov@»
    задаёт схему целиком, а слитное «ivanov@» хотя бы исключает все схемы с
    разделителями — тогда возвращается группа («solid»), а не один шаблон.
    Ролевые ящики (info@, zakupki@) в расчёт не идут: они ничего не говорят о
    том, как компания образует адреса сотрудников."""
    votes, profiles = Counter(), Counter()
    seps, examples, exact_ids = defaultdict(Counter), defaultdict(list), set()
    for item in known or []:
        email = item.get("email") if isinstance(item, dict) else item
        fio = item.get("fio") if isinstance(item, dict) else None
        got = parse_local(email, fio)
        if not got:
            continue
        sid = got["scheme"]
        votes[sid] += 3 if got.get("exact") else 1   # опознанное по ФИО весит больше
        examples[sid].append(email)
        if got.get("exact"):
            exact_ids.add(sid)
        if got.get("profile"):
            profiles[got["profile"]] += 1
        if got.get("sep"):
            seps[sid][got["sep"]] += 1
    if not votes:
        return None
    sid, weight = votes.most_common(1)[0]
    prof = profiles.most_common(1)[0][0] if profiles else None
    # разделитель — самый частый среди адресов победившей схемы: домен пишет
    # либо petrov.a@, либо petrov-a@, и гипотезы обязаны следовать его привычке
    sep = seps[sid].most_common(1)[0][0] if seps.get(sid) else None
    return {"scheme": sid, "support": weight, "profile": prof, "sep": sep,
            "exact": sid in exact_ids,      # опознана сверкой с ФИО, а не по форме адреса
            # support — вес голосов (точное опознание весит 3), поэтому число
            # адресов держим отдельно: иначе один адрес выглядит как три
            "addresses": len(examples[sid]),
            "examples": examples[sid][:3],
            "template": _with_sep(_SCHEME_BY_ID.get(sid, ""), sep)}


# ============================================================================
# ДОМЕН И ПРОВЕРКА ДОСТАВИМОСТИ
# ============================================================================
_PARKED = ("parking", "sedoparking", "afraid.org", "expired")
_PROVIDERS = (
    ("Yandex 360", ("mx.yandex.net", "mx.yandex.ru")),
    ("mail.ru для бизнеса", ("emx.mail.ru", "mxs.mail.ru")),
    ("Microsoft 365", ("mail.protection.outlook.com",)),
    ("Google Workspace", ("aspmx.l.google.com", "googlemail.com")),
    ("Timeweb", ("timeweb",)), ("Beget", ("beget",)), ("REG.RU", ("reg.ru",)),
)


def domain_of(value):
    """URL или email -> голый домен без www/схемы/пути.

    Кириллические домены (.рф) приводятся к punycode: иначе компания с реальной
    почтой на .рф оставалась вовсе без гипотез, да ещё с сообщением «нет ни почты,
    ни сайта»."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    else:
        v = urllib.parse.urlparse(HIS._norm_url(v)).netloc or v
    v = v.split("/")[0].split(":")[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    if re.search(r"[^\x00-\x7f]", v):
        try:
            v = v.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return ""
    return v if re.fullmatch(r"[a-z0-9.\-]+\.(?:xn--[a-z0-9\-]+|[a-z]{2,})", v) else ""


def lead_domain(lead):
    """Домен компании из лида: почта приоритетнее сайта — она уже подтверждена
    Фазой 1, а website иногда содержит агрегатор или мусор.
    Возвращает (домен, источник)."""
    email = (lead.get("email") or "").strip()
    dom = domain_of(email)
    if dom and dom not in EF.FREE_PROVIDERS:
        return dom, f"общая почта компании ({email})"
    site = (lead.get("website") or "").strip()
    if site and HIS.is_own_site(site):
        dom = domain_of(site)
        if dom:
            return dom, f"сайт компании ({site})"
    if dom:                                        # почта на mail.ru/yandex — личный ящик
        return dom, f"общая почта на публичном домене ({email}) — личные адреса не строим"
    return "", "домен не определён (нет ни почты, ни сайта)"


def _doh(name, rtype, timeout=5):
    """Запрос к DNS-over-HTTPS. Возвращает (записи, ответ_достоверен).

    Второе значение отличает «записей нет» от «резолвер не ответил»: SERVFAIL и
    REFUSED приходят с HTTP 200, и без разбора Status они читались бы как отказ
    домена принимать почту."""
    try:
        host = name.encode("idna").decode("ascii") if re.search(r"[^\x00-\x7f]", name) else name
        u = f"https://dns.google/resolve?name={urllib.parse.quote(host)}&type={rtype}"
        req = urllib.request.Request(u, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(1 << 20).decode("utf-8"))
    except Exception:
        return [], False
    if data.get("Status") not in (0, 3):           # 0 = NOERROR, 3 = NXDOMAIN
        return [], False
    return data.get("Answer", []) or [], True


def _mx_records(answers):
    """Ответ DoH -> (хосты MX, был ли Null MX). Запись с пустым хостом — это «0 .»,
    то есть RFC 7505: домен явно объявил, что почту не принимает."""
    hosts, null_mx = [], False
    for ans in answers:
        if ans.get("type") != 15:
            continue
        parts = (ans.get("data") or "").split()
        host = parts[-1].rstrip(".").lower() if parts else ""
        if host:
            hosts.append(host)
        else:
            null_mx = True
    return hosts, null_mx


def mail_domain_state(domain, timeout=5):
    """Домен принимает почту? MX, а при их отсутствии — A/AAAA (implicit MX по
    RFC 5321). Отличает «записей нет» от «DNS не ответил» — это разные выводы."""
    res = {"domain": domain, "mx": [], "provider": "", "accepts_mail": None,
           "via": "", "note": ""}
    if not domain:
        res["note"] = "домен не задан"
        return res
    answers, ok = _doh(domain, "MX", timeout)
    if not ok:
        res["note"] = "DNS не ответил — доставимость не проверена"
        return res
    hosts, null_mx = _mx_records(answers)
    if null_mx and not hosts:
        res.update(accepts_mail=False, via="MX",
                   note="Null MX (RFC 7505) — домен явно отказался принимать почту")
        return res
    if hosts:
        res.update(mx=hosts, accepts_mail=True, via="MX")
    else:
        a_ans, a_ok = _doh(domain, "A", timeout)
        if a_ok and any(x.get("type") == 1 for x in a_ans):
            res.update(accepts_mail=True, via="A (implicit MX)",
                       note="MX нет, почта пойдёт на A-запись — доставимость слабее")
        elif a_ok:
            res.update(accepts_mail=False, note="ни MX, ни A — почту домен не принимает")
        else:
            res["note"] = "DNS не ответил на A — доставимость не проверена"
    blob = " ".join(hosts)
    for title, marks in _PROVIDERS:
        if any(m in blob for m in marks):
            res["provider"] = title
            break
    if any(p in blob for p in _PARKED):
        res.update(accepts_mail=False, note="домен на парковке")
    return res


def smtp_probe_domain(mx_host, addresses, timeout=10, helo=None, mail_from=None):
    """Одна SMTP-сессия на домен: сначала catch-all тестом случайным адресом,
    затем RCPT по кандидатам. Возвращает {адрес: ok|no|unknown}.

    Осознанно скромно: один connect, не больше пяти RCPT, при 4xx/421 сессия
    прекращается — это проверка гипотезы, а не перебор ящиков.

    Представляться чужим доменом нельзя: приёмник проверяет SPF/PTR отправителя, и
    отказ по политике «example.com» неотличим от «такого ящика нет» — реальный
    контакт ЛПР был бы выброшен. Поэтому HELO и MAIL FROM берутся из
    EMAIL_GUESS_HELO / EMAIL_GUESS_MAIL_FROM, а без них проба не делается."""
    verdicts = {a: "unknown" for a in addresses}
    if not (mx_host and addresses):
        return verdicts
    import email_verify as EV
    domain = addresses[0].split("@", 1)[1]
    rows = EV.check_smtp_many(domain, addresses, mx_hosts=[mx_host], timeout=timeout,
                              helo=helo, mail_from=mail_from)
    for addr, row in rows.items():
        if not row.get("checked") or row.get("is_catch_all") or row.get("policy_blocked"):
            continue                               # вердикта нет — оставляем unknown
        verdicts[addr] = "ok" if row.get("is_deliverable") else "no"
    return verdicts


# ============================================================================
# ПРОВЕРКА СУЩЕСТВОВАНИЯ ЯЩИКА БЕЗ ОТПРАВКИ ПИСЬМА
# ============================================================================
# Достоверность SMTP-ответа определяем не мы, а тот, кто принимает почту домена:
#   Яндекс 360        — «сбор потерянных писем» по умолчанию ВЫКЛЮЧЕН, отказ приходит
#                       прямо на RCPT -> вердикту можно верить в обе стороны;
#   Google Workspace  — так же, но жёстче лимиты;
#   mail.ru           — антиспам-контур отвечает незнакомым пробникам «принято» на что
#                       угодно, чтобы сломать перебор -> верить можно только ОТКАЗУ;
#   Microsoft 365     — Exchange Online принимает почти всё, получателя проверяет уже
#                       после DATA -> проба бессмысленна, сессию не тратим вовсе;
#   массовый хостинг  — в cPanel/Plesk catch-all включён по умолчанию.
_TRUST = {
    "Yandex 360": "full",
    "Google Workspace": "full",
    "mail.ru для бизнеса": "negative_only",
    "Microsoft 365": "none",
    "Timeweb": "negative_only",
    "Beget": "negative_only",
    "REG.RU": "negative_only",
}


def mail_provider(mx_hosts):
    """MX-хосты -> кто держит почту домена (по этому решаем, верить ли пробе)."""
    blob = " ".join(mx_hosts or []).lower()
    for title, marks in _PROVIDERS:
        if any(m in blob for m in marks):
            return title
    return "свой сервер" if blob else ""


def smtp_trust(provider):
    """full — верим и «есть», и «нет»; negative_only — только отказу;
    none — не верим вовсе (и не пробуем)."""
    return _TRUST.get(provider, "medium")


def _classify_rcpt(code, message):
    """Ответ на RCPT -> ok | no | policy | temp.

    Отделять policy от no обязательно: 550 бывает и «нет такого ящика» (5.1.1), и
    «ваш IP мне не нравится» (5.7.1). Во втором случае об адресате не сказано ничего,
    а наивный разбор пометил бы живой контакт несуществующим."""
    text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message or "")
    if code in (250, 251):
        return "ok"
    if code in (421, 450, 451, 452, 503, 452):
        return "temp"
    if code in (550, 551, 553, 554):
        if re.search(r"5\.7\.\d", text) or re.search(
                r"policy|blocked|blacklist|spam|denied|not allowed|reputation", text, re.I):
            return "policy"
        return "no"
    return "temp"


# ============================================================================
# ОБЛАЧНЫЙ ВЕРИФИКАТОР MyEmailVerifier — ТОЛЬКО ДОБОР ПО НЕРАЗРЕШЁННОМУ
# ============================================================================
# Зачем он вообще нужен. Наша SMTP-проба идёт с рабочей машины, у которой нет ни
# PTR, ни SPF под чужой домен, поэтому приёмная сторона отвечает отказом по политике
# или молчит — и адрес остаётся `unknown`. MyEmailVerifier пробует со своей
# инфраструктуры (свои IP, PTR, репутация) и закрывает ровно этот класс адресов.
#
# Чем он НЕ является: базой контактов. Сервис не отдаёт ни ФИО, ни должностей, ни
# адресов — только отвечает «существует ли вот этот ящик». Источником данных по ЛПР
# он быть не может.
#
# Границы, которые здесь держит код, а не благие намерения:
#   * слой ВЫКЛЮЧЕН по умолчанию (EMAIL_MEV_ENABLE);
#   * работает ТОЛЬКО по адресам, которые предыдущие слои оставили `unknown`;
#   * адреса ОПК/ВПК и госсектора наружу не уходят никогда — режется ДО запроса,
#     а неопределённая отрасль трактуется как запрет (fail-closed);
#   * ключ идёт в query (так требует их API) и потому вычищается из текстов ошибок.
# ToS сервиса требует «100% opt-in» списков, а у нас расчётные гипотезы — ещё одна
# причина держать наружу минимальный поток, а не выгружать список целиком.
MEV_URL_DEF = "https://api.myemailverifier.com"
MEV_CREDITS_URL = "https://client.myemailverifier.com"

# ОКВЭД-префиксы ОПК и госуправления. Намеренная копия из source_rusprofile.INDUSTRY
# ("opk" и "government"): тот модуль импортирует undetected_chromedriver на верхнем
# уровне, и тащить браузерную зависимость в email_guess (его грузят оркестратор и
# person_enrich, в т.ч. в Docker без Chrome) ради двух списков нельзя.
_MEV_DENY_OKVED = ("25.40", "30.30", "30.11", "30.12", "30.40", "20.51",
                   "26.30", "26.51", "28.99", "84.1", "84.2", "84.3", "84.")
# Отраслевые маркеры в названии/описании ОКВЭД. Третий гейт поверх _industry и
# ОКВЭД: коды 25.40 и 28.99 лежат И в "opk", И в "processing", поэтому оборонный
# завод штатно приезжает с меткой processing и обоими первыми гейтами не ловится.
#
# ⚠️ Вторая половина списка — не «на всякий случай», а результат разбора реальной
# выгрузки «Почты руководителей филиалов и топ-менеджмента» (2026-08-07): под
# видом дорожного строительства там лежали ФНПЦ «Титан-Баррикады», ГосНИИ
# «Кристалл» и ОКБ «Новатор». Ни одного слова из первой половины в их названиях
# нет — ловятся они только по организационно-правовым маркерам оборонной науки
# (ФНПЦ/ГосНИИ/ОКБ/ЦКБ/НПО). Гейт умышленно широкий: ложное срабатывание стоит
# одного непроверенного адреса, пропуск — выгрузки контактов ОПК подрядчику в США.
_MEV_DENY_WORDS = ("оруж", "боеприпас", "оборон", "военн", "вооруж", "ракет",
                   "атомн", "ядерн", "росатом", "ростех", "спецназнач",
                   "специального назначения", "гособорон",
                   "фнпц", "госнии", "цнии", "окб ", "цкб", "нпо ", "нпц ",
                   "судоремонт", "судострои", "авиастрои", "двигателестрои",
                   "приборострои", "спецстрой", "атомстрой", "атомэнерго",
                   "баррикад", "алмаз-антей", "калашников", "уралвагон")


def _mev_env(name, default=""):
    return (os.environ.get("EMAIL_MEV_" + name) or default).strip()


def _mev_num(name, default):
    """EMAIL_MEV_<name> числом. Мусор в переменной окружения не должен ронять прогон
    посреди рассылки — молча берём дефолт."""
    try:
        return float(_mev_env(name, str(default)))
    except ValueError:
        return float(default)


def _mev_list(name, default):
    """EMAIL_MEV_<name> — список через запятую, нормализованный к нижнему регистру."""
    return [part.strip().lower() for part in _mev_env(name, default).split(",")
            if part.strip()]


def mev_policy():
    """Действующие границы слоя одной структурой.

    Дефолты живут ровно здесь: по ним решает _mev_allowed, их же печатает прекондишен
    стадии 4 в outreach — иначе лог рассказывал бы про свою копию списков."""
    return {
        "daily_limit": int(_mev_num("DAILY_LIMIT", 100)),
        "deny_industries": _mev_list("DENY_INDUSTRIES", "opk,government"),
        "deny_domains": _mev_list("DENY_DOMAINS",
                                  "gov.ru,mil.ru,mod.gov.ru,rosatom.ru,rostec.ru"),
    }


def mev_enabled():
    """Слой включён явно И есть ключ. Без ключа молча ничего не делаем."""
    return (_mev_env("ENABLE", "0").lower() not in ("0", "false", "no", "off", "нет")
            and bool(_mev_env("API_KEY")))


def _mev_safe(text):
    """Ключ уходит в query — значит попадает и в текст сетевых ошибок. Вычищаем,
    иначе он утечёт в лог прогона (D:\\orq_tmp\\run_*.log) и в ноты вердиктов."""
    out = str(text or "")
    key = _mev_env("API_KEY")
    if key:
        out = out.replace(key, "<скрыто>")
    return out[:160]


_mev_last_call = 0.0


def _mev_pace():
    """Сервис отдаёт 429 после 30 запросов в минуту — держим интервал сами."""
    global _mev_last_call
    rpm = max(1.0, _mev_num("RPM", 30))
    wait = (60.0 / rpm) - (time.monotonic() - _mev_last_call)
    if wait > 0:
        time.sleep(wait)
    _mev_last_call = time.monotonic()


def _mev_request(url, timeout, retries=2):
    """GET -> распарсенный JSON или {"_error": ...}. Ретраит 429 и 5xx с учётом
    Retry-After (образец — source_ofdata.OfDataClient._call)."""
    for attempt in range(retries + 1):
        _mev_pace()
        try:
            # Без User-Agent их WAF отвечает 403 на всё, включая запрос баланса:
            # дефолтный "Python-urllib/3.12" режется. Проверено вживую 2026-08-07.
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; lead-orchestrator/1.0)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read(1 << 20).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                try:
                    pause = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pause = 0.0
                time.sleep(max(pause, 2.0 * (attempt + 1)))
                continue
            return {"_error": f"HTTP {exc.code}"}
        except Exception as exc:                   # noqa: BLE001 — сеть/таймаут/битый JSON
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"_error": _mev_safe(exc)}
    return {"_error": "нет ответа"}


def _mev_payload(email, url, timeout):
    """Сырой ответ MyEmailVerifier. Ключ — в query: так устроен их API."""
    key = _mev_env("API_KEY")
    if not key:
        return {"_error": "не задан EMAIL_MEV_API_KEY"}
    query = urllib.parse.urlencode({"apikey": key, "email": email})
    data = _mev_request(f"{url}/api/validate_single.php?{query}", timeout)
    if not isinstance(data, dict):                 # разбор ниже ждёт объект, а не список
        return {"_error": "неожиданный ответ сервиса"}
    if data.get("status") == "error":
        # {"status":"error","error":"unauthorized","message":"User not found"}
        return {"_error": _mev_safe(data.get("message") or data.get("error"))}
    return data


# Диагнозы, означающие «ящика нет» либо «слать нельзя». Всё, чего в списке НЕТ,
# трактуется как `unknown`: у них `Invalid` покрывает и «нет адресата», и «сервер
# нас отшил», а второе об адресате не говорит ничего. Тот же урок, что в
# _classify_rcpt (550 5.1.1 против 550 5.7.x): наивный разбор вычеркнул бы живой
# контакт ЛПР навсегда — _rank_rows такие строки отбрасывает без права апелляции.
_MEV_NO = (
    "does not exist", "doesn't exist", "not exist", "no such user", "user unknown",
    "unknown user", "invalid mailbox", "mailbox not found", "recipient not found",
    "invalid domain", "domain does not exist", "invalid syntax", "syntax error",
)
# Диагнозы уровня ДОМЕНА, а не ящика. Держим отдельно и НЕ переводим в «no».
# Проверено вживую 2026-08-07: сервис отвечает «Disposable or Toxic domain (UCE)»
# на info@vozr.ru (АО ПО «Возрождение») и info@zaovad.com (АО «ВАД») — это
# опубликованные общие адреса живых дорожных подрядчиков, никакие не одноразовые.
# Там же в списке оказались tatavtodor.ru, kznvodokanal.ru, avtodorstroy.ru,
# nbssib.ru. Это оценка репутации домена по чужим блок-листам, о существовании
# ящика она не говорит НИЧЕГО, а «no» у нас необратим: _rank_rows выбрасывает
# такую строку без права апелляции. Сигнал сохраняем в ноте — решать человеку.
# Голое «Invalid email» СЮДА НЕ ВХОДИТ: это отказ уровня ящика, просто без причины.
# Его вес решает _mev_fill по приёмнику домена (soft_no), иначе мы выбросили бы и
# честные отказы Яндекса — а именно там сервис единственно и полезен.
_MEV_DOMAIN_NO = ("disposable", "toxic", "spam trap", "spamtrap")


def _mev_read(data):
    """Ответ MyEmailVerifier -> наш {verdict, trusted, note, soft_no, raw}.

    Маппинг намеренно асимметричный: `Valid` принимаем, `Invalid` — только с
    внятным диагнозом. Их `Invalid` покрывает три разных случая, и путать их нельзя:
      * «mailbox does not exist» — настоящий отказ ящику    -> no;
      * «Disposable or Toxic domain» — приговор ДОМЕНУ по чужим блок-листам,
        о ящике не сказано ничего                            -> unknown;
      * голое «Invalid email (D2)» без причины              -> soft_no.
    `soft_no` разрешает _mev_fill: он один знает приёмника домена и по тому же
    `_TRUST`, что и наша SMTP-проба, решает, приговор это или шум."""
    def flag(key):
        """Их булевы поля. ⚠️ Документация обещает строки "true"/"false", а API
        отдаёт ЧИСЛА 0/1 (проверено вживую 2026-08-07). Понимаем обе формы: разбор
        только по "true" молча считал бы catch-all домен обычным, и веер гипотез
        по нему уехал бы наружу как подтверждённые адреса."""
        return str(data.get(key) or "").strip().lower() in ("true", "1", "yes")

    status = str(data.get("Status") or "").strip().lower()
    diag = str(data.get("Diagnosis") or "").strip()
    low = diag.lower()
    note = f"MyEmailVerifier: {diag or status or 'без диагноза'}"
    soft_no = False

    if flag("catch_all") or status in ("catch all", "catch-all", "catchall"):
        return {"verdict": "unknown", "trusted": False, "raw": data, "soft_no": False,
                "note": "MyEmailVerifier: домен принимает любой адрес (catch-all)"}
    if status == "valid":
        verdict = "ok"
    elif status == "invalid":
        if any(mark in low for mark in _MEV_DOMAIN_NO):
            # Приговор ДОМЕНУ по чужим блок-листам. О ящике не сказано ничего, и
            # даже на доверенном приёмнике это не повод вычёркивать контакт.
            verdict = "unknown"
            note = (f"MyEmailVerifier забраковал ДОМЕН, а не ящик ({diag}) — "
                    f"на российских корпоративных доменах это его известная "
                    f"ошибка, проверять вручную")
        elif any(mark in low for mark in _MEV_NO):
            verdict = "no"
        else:
            # Расплывчатое «Invalid email» без указания причины. Верить ему можно
            # ровно настолько, насколько честен приёмник домена, — решает _mev_fill
            # по тому же _TRUST, что и для нашей собственной пробы.
            # Нота намеренно нейтральна: трактовку допишет _mev_fill, и склеенный
            # текст обязан читаться связно в ОБЕ стороны.
            verdict, soft_no = "unknown", True
            note = f"MyEmailVerifier отклонил адрес: {diag or 'без диагноза'}"
    else:                                          # unknown, grey-listed и всё прочее
        verdict = "unknown"
    if flag("Role_Based"):
        note += "; ролевой адрес"
    if flag("Disposable_Domain"):
        note += "; одноразовый домен"
    return {"verdict": verdict, "trusted": verdict != "unknown", "note": note,
            "soft_no": soft_no, "raw": data}


def _mev_allowed(lead, domain):
    """Можно ли выпускать адреса этой компании во внешний сервис -> (bool, причина).

    Fail-closed: не смогли определить отрасль — не выпускаем. Ошибка в эту сторону
    стоит нескольких непроверенных адресов, в обратную — выгрузки контактов ЛПР
    оборонного предприятия американскому подрядчику."""
    policy = mev_policy()
    dom = (domain or "").lower()
    if any(dom == bad or dom.endswith("." + bad) for bad in policy["deny_domains"]):
        return False, f"домен {domain} в блок-листе"
    if not isinstance(lead, dict):
        return False, "лид неизвестен — отрасль не определить"

    industry = str(lead.get("_industry") or "").strip().lower()
    if not industry:
        return False, "отрасль компании не определена"
    if industry in policy["deny_industries"]:
        return False, f"отрасль «{industry}» в блок-листе"

    # ОКВЭД Фаза 1 кладёт внутрь niche строкой «<отрасль> (ОКВЭД 25.40)»
    found = re.search(r"ОКВЭД\s+(\d{2}(?:\.\d{1,2}){0,2})", str(lead.get("niche") or ""))
    okved = found.group(1) if found else ""
    if okved and any(okved.startswith(code) for code in _MEV_DENY_OKVED):
        return False, f"ОКВЭД {okved} — оборонный или государственный"

    blob = " ".join(str(lead.get(k) or "") for k in
                    ("name", "niche", "_okved_descr")).lower()
    hit = next((w for w in _MEV_DENY_WORDS if w in blob), "")
    if hit:
        return False, f"признак оборонного/госсектора в профиле («{hit}»)"
    return True, ""


# ---- бюджет кредитов и кэш вердиктов ---------------------------------------
# Прогон рассчитан на перезапуск ТОЙ ЖЕ командой, поэтому без кэша повтор сжигал бы
# суточную квоту заново на тех же адресах. Файл общий: и кэш, и счётчик за сутки.
def _mev_state_path():
    """ORQ_DATA_ROOT/orq_cache/mev_state.json -> D:\\orq_cache -> %TEMP%.

    Намеренная копия orchestrator._work_base — ровно по той же причине, что и в
    outreach_registry: импортировать оркестратор ради четырёх строк нельзя."""
    data_root = (os.environ.get("ORQ_DATA_ROOT") or "").strip()
    root = data_root or ("D:\\" if os.path.isdir("D:\\") else tempfile.gettempdir())
    return os.path.join(root, "orq_cache", "mev_state.json")


def _mev_state():
    try:
        with open(_mev_state_path(), encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except Exception:                              # noqa: BLE001 — нет файла/битый JSON
        return {}


def _mev_save(state):
    """Атомарно: прогон могут прервать Ctrl+C, а половина JSON хуже отсутствия."""
    path = _mev_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:                              # noqa: BLE001 — кэш не критичен
        pass


def _mev_today():
    """Ключ суточного счётчика. Общий для списания и для остатка: разойдись эти два
    места в формате — квота считалась бы вечно нетронутой."""
    return time.strftime("%Y-%m-%d")


def _mev_remember(state, email, row):
    """Списать кредит с суточного счётчика и, если вердикт окончательный, положить
    его в кэш — одной операцией: вердикт без списания сжёг бы квоту молча.

    `unknown` в кэш НЕ идёт. Он означает «greylisted», «сервер отшил» или сетевой
    сбой — состояния временные, и запоминать их на месяц значит навсегда потерять
    адрес, который завтра разрешился бы. Сам сервис за `unknown` кредит не берёт
    (их FAQ), так что повторный вопрос ничего не стоит; счётчик всё равно
    инкрементим — он считает запросы, а не списания, и это честнее к их лимиту."""
    usage = state.setdefault("usage", {})
    day = _mev_today()
    usage[day] = int(usage.get(day, 0)) + 1
    if row.get("verdict") != "unknown":
        state.setdefault("verdicts", {})[email] = row


def _mev_budget_left(state):
    spent = int((state.get("usage") or {}).get(_mev_today(), 0))
    return max(0, mev_policy()["daily_limit"] - spent)


def _mev_cached(state, email):
    row = (state.get("verdicts") or {}).get(email)
    if not isinstance(row, dict) or not row.get("verdict"):
        return None                                # битую строку кэша считаем отсутствующей
    if time.time() - float(row.get("ts") or 0) > _mev_num("CACHE_TTL_D", 30) * 86400:
        return None
    return row


def mev_credits(timeout=20):
    """Остаток кредитов на счету (запрос баланса кредит не тратит) -> (int|None, текст)."""
    key = _mev_env("API_KEY")
    if not key:
        return None, "не задан EMAIL_MEV_API_KEY"
    base = _mev_env("CREDITS_URL", MEV_CREDITS_URL).rstrip("/")
    data = _mev_request(f"{base}/verifier/getcredits/{urllib.parse.quote(key)}", timeout)
    if isinstance(data, dict) and data.get("_error"):
        return None, _mev_safe(data["_error"])
    credits = data.get("credits") if isinstance(data, dict) else None
    try:
        return int(str(credits).strip()), ""
    except (TypeError, ValueError):
        return None, _mev_safe(f"неожиданный ответ: {data}")


def _mev_fill(out, lead=None, log=None):
    """Добрать облачным сервисом адреса, которые предыдущие слои не разрешили.

    Вызывается на выходе verify_addresses и НИКОГДА не переписывает уже полученный
    вердикт: трогаются только `unknown`."""
    if not mev_enabled() or out.get("catch_all"):
        return out
    still = [addr for addr, row in out["verdicts"].items()
             if row.get("verdict") == "unknown"]
    if not still:
        return out

    allowed, why = _mev_allowed(lead, out.get("domain") or "")
    if not allowed:
        if log:
            log(f"    MyEmailVerifier пропущен: {why}")
        return out

    # Вес расплывчатого отказа задаёт приёмник домена — ровно как для нашей пробы.
    # Yandex 360 и Google Workspace отвечают на RCPT честно, поэтому их «Invalid
    # email» без причины стоит принять; на mail.ru и самохостинге то же самое
    # означает лишь «нам не понравился отправитель». Считаем один раз на домен.
    trust = out.get("trust")
    if not trust:
        try:
            provider = mail_provider((mail_domain_state(out["domain"]).get("mx") or []))
            trust = smtp_trust(provider) if provider else "unknown"
        except Exception:                          # noqa: BLE001 — DNS не критичен
            trust = "unknown"

    state = _mev_state()
    url = _mev_env("URL", MEV_URL_DEF).rstrip("/")
    asked = 0
    for addr in still:
        row = _mev_cached(state, addr)
        if row is None:
            if _mev_budget_left(state) <= 0:
                # если что-то уже спросили, итоговая строка сама покажет нулевой остаток
                if log and not asked:
                    log("    MyEmailVerifier: суточная квота исчерпана — "
                        "адреса остаются непроверенными")
                break
            got = external_verify(addr, url=url, kind="myemailverifier")
            verdict, note = got["verdict"], got["note"]
            if got.get("soft_no"):
                if trust == "full":
                    verdict = "no"
                    note += " — причины не назвал, но приёмник домена отвечает честно"
                else:
                    note += (" — причины не назвал, а приёмнику этого домена такой "
                             "отказ не доказательство")
            row = {"verdict": verdict, "note": note, "ts": time.time()}
            _mev_remember(state, addr, row)
            asked += 1
        # Ноту пишем ВСЕГДА, даже для unknown: без неё оператор видит «не
        # подтверждён» и не знает, catch-all это, блок-лист домена или отказ.
        out["verdicts"][addr] = {"verdict": row["verdict"],
                                 "trusted": row["verdict"] != "unknown",
                                 "note": row["note"]}
        if row["verdict"] != "unknown":
            out["probed"] = True
    if asked:
        _mev_save(state)
        mark = f"добор MyEmailVerifier ({asked})"
        was = out.get("reason") or ""
        out["reason"] = f"{was} | {mark}" if was else mark
        if log:
            log(f"    MyEmailVerifier: проверено {asked}, "
                f"остаток квоты {_mev_budget_left(state)}")
    return out


def _external_payload(email, url, kind, timeout):
    """Сырой ответ внешнего верификатора (или None). Отдельно от разбора, чтобы
    ошибку сети было видно как ошибку, а не как «ящик не найден»."""
    if kind == "myemailverifier":
        return _mev_payload(email, url, timeout)
    helo = os.environ.get("EMAIL_GUESS_HELO", "").strip()
    mail_from = os.environ.get("EMAIL_GUESS_MAIL_FROM", "").strip()
    secret = os.environ.get("EMAIL_VERIFIER_SECRET", "").strip()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if secret:                                     # self-host с RCH__HEADER_SECRET иначе даёт 401
        headers["x-reacher-secret"] = secret
    paths = ["/v1/check_email", "/v0/check_email"] if kind == "reacher" else [
        f"/v1/{urllib.parse.quote(email)}/verification"]
    for path in paths:
        try:
            if kind == "reacher":
                body = {"to_email": email}
                if helo:
                    body["hello_name"] = helo       # дефолтный HELO часть российских MX режет
                if mail_from:
                    body["from_email"] = mail_from
                req = urllib.request.Request(
                    url + path, data=json.dumps(body).encode("utf-8"), headers=headers)
            else:
                req = urllib.request.Request(url + path, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read(1 << 20).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and path != paths[-1]:
                continue                           # старый инстанс — пробуем /v0
            return {"_error": f"HTTP {exc.code}"}
        except Exception as exc:                   # noqa: BLE001 — сервис не поднят и т.п.
            return {"_error": str(exc)[:80]}
    return None


def external_verify(email, url=None, kind=None, timeout=20):
    """Проверка через СВОЙ (self-hosted) верификатор, если он поднят.

    Поддержаны два открытых проекта, оба ходят по SMTP без отправки письма:
      * AfterShip/email-verifier (Go, MIT) — GET  {url}/v1/{email}/verification
      * Reacher / check-if-email-exists (Rust, AGPL) — POST {url}/v1/check_email
    Сюда же можно направить наш собственный `email_verify.py --serve` — формат
    ответа у него тот же.

    Третий kind — `myemailverifier` — облачный. Массово подключать облака нельзя
    (ZeroBounce, Hunter, NeverBounce так и не подключены): это выгрузка списка ЛПР
    третьей стороне. Этот вызывается только из _mev_fill, только по адресам, которые
    не разрешили предыдущие слои, и только для компаний вне блок-листа отраслей.

    Возвращает {verdict, trusted, note, raw}. Именно dict, а не строка: «не смогли
    проверить» и «ящика нет» обязаны различаться, иначе недоступный сервис молча
    вычёркивает живые контакты."""
    url = (url or os.environ.get("EMAIL_VERIFIER_URL", "")).strip().rstrip("/")
    if not url:
        return {"verdict": "unknown", "trusted": False,
                "note": "внешний верификатор не настроен (EMAIL_VERIFIER_URL)", "raw": None}
    kind = (kind or os.environ.get("EMAIL_VERIFIER_KIND", "")).strip().lower()
    if not kind:
        kind = "reacher" if "check_email" in url or "reacher" in url else "aftership"
    data = _external_payload(email, url, kind, timeout)
    if not data or data.get("_error"):
        return {"verdict": "unknown", "trusted": False,
                "note": f"верификатор недоступен ({(data or {}).get('_error', 'нет ответа')})",
                "raw": data}

    if kind == "myemailverifier":
        return _mev_read(data)                     # у облака свой формат, smtp-блока нет

    smtp = data.get("smtp") if isinstance(data.get("smtp"), dict) else {}
    if kind == "reacher":
        # разбор общий с нашим python-портом: у Reacher `invalid` означает и «нет
        # ящика», и «не смог подключиться», поэтому смотреть надо на сам smtp-блок
        import email_verify as EV
        verdict = EV.to_verdict(data)
    else:                                          # AfterShip: reachable yes|no|unknown
        state = str(data.get("reachable") or "").lower()
        verdict = {"yes": "ok", "no": "no"}.get(state, "unknown")
        if verdict == "no" and not smtp.get("host_exists", True):
            verdict = "unknown"                    # до сервера не достучались — не приговор
    if smtp.get("catch_all") or smtp.get("is_catch_all"):
        verdict = "unknown"
    note = data.get("reason") or (
        "проверено внешним верификатором"
        + (", домен catch-all" if smtp.get("is_catch_all") or smtp.get("catch_all") else ""))
    return {"verdict": verdict, "trusted": verdict != "unknown", "note": note, "raw": data}


def verify_addresses(domain, addresses, mail=None, timeout=10, log=None, lead=None):
    """Существуют ли ящики — БЕЗ отправки письма. Одна SMTP-сессия на домен.

    Возвращает dict с вердиктами и — главное — с честной оценкой их веса:
      probed      — состоялась ли проверка вообще;
      provider    — кто принимает почту домена;
      trust       — насколько его ответам можно верить (см. _TRUST);
      catch_all   — домен принимает любой адрес (тогда вердикта нет ни у кого);
      verdicts    — {адрес: {"verdict": ok|no|unknown, "trusted": bool, "note": str}}.
    Пустой/неуверенный результат — норма: 20-25% корпоративных доменов не разрешаются
    ничем, кроме реальной отправки.

    `lead` нужен последнему слою (_mev_fill): по нему решается, можно ли выпускать
    адреса этой компании во внешний сервис. Без лида облачный добор не работает."""
    out = {"domain": domain, "probed": False, "provider": "", "trust": "",
           "catch_all": None, "reason": "", "verdicts": {}}
    addresses = [a for a in (addresses or []) if "@" in a][:5]
    for addr in addresses:
        out["verdicts"][addr] = {"verdict": "unknown", "trusted": False, "note": ""}
    if not (domain and addresses):
        out["reason"] = "нечего проверять"
        return out
    if domain in EF.FREE_PROVIDERS:
        out["reason"] = "публичный почтовый сервис — это личный ящик, не проверяем"
        return out

    # внешний self-hosted верификатор умнее нашей пробы (headless-режимы, свои обходы)
    if os.environ.get("EMAIL_VERIFIER_URL", "").strip():
        out["reason"] = "self-hosted верификатор (EMAIL_VERIFIER_URL)"
        for addr in addresses:
            got = external_verify(addr)
            out["verdicts"][addr] = {k: got[k] for k in ("verdict", "trusted", "note")}
            if got["verdict"] != "unknown":
                out["probed"] = True               # хоть один ответ получен — сервис жив
        return _mev_fill(out, lead, log)

    mail = mail or mail_domain_state(domain)
    if mail.get("accepts_mail") is False:
        out["reason"] = mail.get("note") or "домен не принимает почту"
        for addr in addresses:
            out["verdicts"][addr] = {"verdict": "no", "trusted": True,
                                     "note": out["reason"]}
        return out
    mx = (mail.get("mx") or [])
    if not mx:
        out["reason"] = "у домена нет MX — проверять нечего"
        return out

    out["provider"] = mail_provider(mx)
    out["trust"] = smtp_trust(out["provider"])
    if out["trust"] == "none":
        out["reason"] = (f"{out['provider']} принимает почту на любой адрес и проверяет "
                         f"получателя уже после приёма — проба ничего не докажет")
        return _mev_fill(out, lead, log)           # именно здесь облако и полезнее всего

    # сам SMTP-диалог живёт в email_verify (Python-порт ядра Reacher) — здесь остаётся
    # только доменная логика и перевод технических флагов в вес вердикта
    import email_verify as EV
    smtp = EV.check_smtp_many(domain, addresses, mx_hosts=mx, timeout=timeout)
    first = smtp.get(addresses[0], {})
    out["catch_all"] = first.get("is_catch_all") if first.get("checked") else None
    out["probed"] = any(row.get("checked") for row in smtp.values())
    if not out["probed"]:
        out["reason"] = first.get("reason") or "проба не состоялась"
    elif out["catch_all"]:
        out["reason"] = ("домен принимает любой адрес (catch-all) — существование "
                         "конкретного ящика так не проверить")
    # Сервер, отклонивший в этой же сессии хоть один адрес, доказал, что реально
    # проверяет получателя: он не catch-all и не отвечает «принято» всем подряд.
    # Тогда его «принято» — сильное доказательство, даже если провайдер незнакомый.
    selective = any(row.get("checked") and not row.get("is_deliverable")
                    and not row.get("policy_blocked") for row in smtp.values())
    if selective and out["trust"] == "medium":
        out["trust"] = "full"
        out["reason"] = (out["reason"] or
                         "сервер отклонил другие адреса — значит проверяет получателя")
    for addr in addresses:
        row = smtp.get(addr) or {}
        if not row.get("checked") or out["catch_all"]:
            continue
        if row.get("policy_blocked"):
            out["verdicts"][addr] = {
                "verdict": "unknown", "trusted": False,
                "note": "сервер отклонил по политике (наш IP), об адресе не сказано"}
            continue
        kind = "ok" if row.get("is_deliverable") else "no"
        trusted = (out["trust"] == "full"
                   or (kind == "no" and out["trust"] in ("negative_only", "medium")))
        note = ("подтверждён почтовым сервером" if kind == "ok" else
                "сервер сообщил, что такого ящика нет")
        if row.get("is_disabled"):
            note = "ящик отключён"
        elif row.get("has_full_inbox"):
            note, kind = "ящик существует, но переполнен", "ok"
        elif kind == "ok" and not trusted:
            note = ("сервер принял адрес, но его политика приёма нам неизвестна — "
                    "слабое подтверждение"
                    if out["trust"] == "medium" else
                    f"{out['provider']} отвечает «принято» и незнакомым пробникам — "
                    f"это не доказательство")
        out["verdicts"][addr] = {"verdict": kind, "trusted": trusted, "note": note}
    if log:
        log(f"    SMTP-проверка {domain}: {out['reason'] or 'выполнена'} "
            f"(провайдер: {out['provider'] or '—'}, доверие: {out['trust'] or '—'})")
    return _mev_fill(out, lead, log)


# ============================================================================
# КЛЮЧЕВЫЕ СОТРУДНИКИ ИЗ НАХОДОК ДВИЖКА
# ============================================================================
_FINDINGS_JSON = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _findings_blocks(findings_text):
    """Структурные JSON-блоки отчёта движка. Битый блок пропускается молча: отчёт
    пишет модель, и один сломанный JSON не должен лишать нас остальных."""
    for m in _FINDINGS_JSON.finditer(findings_text or ""):
        try:
            yield json.loads(m.group(1))
        except (ValueError, TypeError):
            continue


def _dict_rows(container, key):
    """Строки-словари по ключу. И контейнер, и его элементы приходят от модели,
    поэтому всё, что не dict, отбрасывается молча."""
    rows = container.get(key) if isinstance(container, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def people_from_findings(findings_text):
    """Из отчёта deep_research_engine достать ФИО + должности сотрудников.

    Движок кладёт в конец отчёта тот же результат структурным JSON — берём
    leadership / procurement.contacts / branches.director / departments."""
    people, seen = [], set()

    def add(fio, position, source):
        fio = re.sub(r"\s+", " ", (fio or "").strip())
        if not is_person(fio) or fio.lower() in seen:
            return
        seen.add(fio.lower())
        people.append({"fio": fio, "position": (position or "").strip(),
                       "source": source or ""})

    for data in _findings_blocks(findings_text):
        for row in _dict_rows(data, "leadership"):
            add(row.get("fio"), row.get("position"), row.get("source"))
        for row in _dict_rows(data, "branches"):
            add(row.get("director"), f"директор филиала «{row.get('branch', '')}»",
                row.get("source"))
        for row in _dict_rows(data.get("procurement"), "contacts"):
            add(row.get("fio"), "закупки", row.get("source"))
    return people


# Страницы, где российские компании публикуют руководство. harvest_inn_site.PATHS
# сюда не годится: он собран под футерные реквизиты (ИНН/ОГРН), а не под персоналии.
MGMT_PATHS = (
    # порядок важен: обход обрывается по лимиту попыток, поэтому впереди страницы,
    # которые чаще всего существуют и несут и ФИО, и личные адреса
    # «/kontaktyi» и «/kontakti» — не опечатки: так «контакты» транслитерируют
    # плагины Cyr-to-Lat (WordPress) и Bitrix. На talspecstroi.ru именно по такому
    # адресу лежала страница с восемью личными адресами и схемой домена, а обход
    # с одним лишь «/kontakty» возвращал ноль людей и ноль адресов
    "/rukovodstvo", "/management", "/kontakty", "/kontaktyi", "/kontakti",
    "/contacts", "/team", "/komanda",
    "/struktura", "/administraciya", "/o-kompanii", "/structure", "/staff",
    "/sotrudniki", "/about/management", "/company/management",
)
# ФИО берём только в полной форме с отчеством — иначе в кандидаты лезут «Общество
# Ограниченной Ответственностью» и названия улиц.
_RE_FIO = re.compile(
    r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+)\s+"
    r"([А-ЯЁ][а-яё]+(?:ович|евич|ьевич|овна|евна|ична|инична|ыч))\b")
_RE_POST = re.compile(
    r"(генеральн\w+ директор|исполнительн\w+ директор|финансов\w+ директор|"
    r"технически\w+ директор|коммерчески\w+ директор|директор по \w+|директор|"
    r"первы\w+ заместител\w+|заместител\w+ [\w\s]{0,30}|главн\w+ инженер|"
    r"главн\w+ \w+|начальник [\w\s]{0,30}|руководител\w+ [\w\s]{0,30}|"
    r"председател\w+ [\w\s]{0,20}|президент|управляющи\w+)", re.I)


FETCH_MAX_BYTES = int(os.environ.get("EMAIL_GUESS_MAX_BYTES", str(2 << 20)))
SITE_BUDGET_S = float(os.environ.get("EMAIL_GUESS_SITE_BUDGET", "45"))


_PRIVATE_HOST = re.compile(
    r"^(localhost|.*\.local|.*\.internal|.*\.localdomain|"
    r"127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|169\.254\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|\[?::1\]?|\[?fe80:.*|\[?fc00:.*|\[?fd.*)$", re.I)


def _is_public_host(url):
    """Домен приходит из внешних данных (поле website лида), поэтому обращения во
    внутреннюю сеть отсекаем — как это уже делает движок deep_research."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or "@" in (parsed.netloc or ""):
        return False
    host = (parsed.hostname or "").strip(".")
    return bool(host) and not _PRIVATE_HOST.match(host)


def _fetch_page(url, timeout=12):
    """Страница сайта -> (html, финальный URL после редиректов).

    Свой фетч вместо harvest_inn_site._fetch по двум причинам: тело читается с
    капом (иначе gzip-бомба или 80-мегабайтная страница разворачиваются в память
    целиком), и наружу отдаётся ФАКТИЧЕСКИЙ адрес — urllib молча идёт по 302, и
    без этого ФИО с чужого сайта уходили бы со ссылкой на нашу страницу."""
    if not _is_public_host(url):
        return "", url
    req = urllib.request.Request(url, headers={
        "User-Agent": HIS.UA, "Accept-Language": "ru,en;q=0.8", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        final, ctype = r.geturl(), (r.headers.get("Content-Type") or "").lower()
        if not _is_public_host(final):             # редирект во внутреннюю сеть
            return "", final
        if ctype and "html" not in ctype and not ctype.startswith("text/"):
            return "", final                       # PDF/архив гонять через regex незачем
        raw = r.read(FETCH_MAX_BYTES + 1)[:FETCH_MAX_BYTES]
        gz = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
    if gz:
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(FETCH_MAX_BYTES)
        except OSError:
            return "", final
    m = re.search(r"charset=([\w\-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return raw.decode(enc, "replace"), final
    except LookupError:
        return raw.decode("utf-8", "replace"), final


def _edit_distance_1(a, b):
    """Строки различаются не больше чем одной правкой (вставка/замена/удаление)."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i, j = i + 1, j + 1
            continue
        diff += 1
        if diff > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return diff + (len(b) - j) + (len(a) - i) <= 1


def _same_brand(old, new):
    """Один и тот же бренд под разными доменами?

    «svgc.ru» и «svgk.ru» — да: одна аббревиатура (СВГК), разная латиница, и почта
    осталась на старом домене. «avtodor-rzn.ru» и «another-site.ru» — нет, это
    просто увод на чужой сайт, данные оттуда к нашей компании не относятся.
    Сходства домена мало для доказательства, но его ОТСУТСТВИЕ — достаточный повод
    не верить редиректу, а именно это здесь и решается."""
    a, b = old.split(".")[0], new.split(".")[0]
    if not a or not b:
        return False
    if a == b:                                     # тот же бренд в другой зоне (.ru -> .рф)
        return True
    if len(a) < 3 or len(b) < 3:                   # двухбуквенные метки слишком близки друг к другу
        return False
    if a in b or b in a:                           # svgk -> svgk-group
        return abs(len(a) - len(b)) <= 6
    return _edit_distance_1(a, b)


def people_from_site(domain, max_pages=10, timeout=12, budget_s=None, log=None):
    """Обойти страницы руководства сайта -> (люди, найденные адреса).

    Это единственный источник персоналий, когда ресёрч по компании ещё не делался:
    в JSON Фазы 1 есть только директор из ЕГРЮЛ. Личные адреса, найденные здесь же,
    задают схему домена — ради них страницы и обходятся.

    max_pages ограничивает ПОПЫТКИ, а не удачные загрузки: на сайте без раздела
    «Руководство» каждый путь отвечает 404, и счётчик по успехам не остановил бы
    обход — на прогоне в 200 компаний это тысячи лишних запросов к чужим серверам.
    budget_s — общий дедлайн по времени: socket-таймаут не спасает от сервера,
    отдающего данные по байту, и одна мёртвая компания вешала бы прогон."""
    people, emails, seen_p, seen_e, tried = [], [], set(), set(), 0
    own = domain_of(domain)
    # почту компания могла оставить на прежнем домене, даже переехав сайтом
    mail_domains = {own} if own else set()
    moved = False
    base = HIS._norm_url(domain).rstrip("/")
    deadline = time.monotonic() + (SITE_BUDGET_S if budget_s is None else budget_s)
    for path in MGMT_PATHS[:max_pages]:
        if time.monotonic() > deadline:
            break
        tried += 1
        try:
            html, final_url = _fetch_page(base + path, timeout=timeout)
        except Exception:
            continue                               # 404 на странице — норма, не сбой
        if not html:
            continue
        final_dom = domain_of(final_url)
        if own and final_dom != own:
            # Компания сменила домен сайта, а почту оставила на старом: у СВГК
            # svgc.ru редиректит на svgk.ru, и почта при этом svgc@svgc.ru.
            # Прежняя проверка отбрасывала такой сайт целиком — обход возвращал
            # ноль людей и ноль адресов. Переезд принимаем однократно и только
            # когда это тот же бренд на собственном сайте: одного is_own_site мало,
            # он пропускает и увод на сайт совершенно другой компании.
            if moved or not (final_dom and HIS.is_own_site(final_url)
                             and _same_brand(own, final_dom)):
                continue
            moved = True
            own, base = final_dom, "https://" + final_dom
            mail_domains.add(final_dom)
        text = re.sub(r"\s+", " ", EF._strip_tags(html))
        for m in _RE_FIO.finditer(text):
            fio = f"{m.group(1)} {m.group(2)} {m.group(3)}"
            if fio.lower() in seen_p or not is_person(fio):
                continue
            around = text[max(0, m.start() - 160):m.end() + 160]
            post = _RE_POST.search(around)
            # без должности рядом это не обязательно сотрудник: на страницах «о
            # компании» так же упоминают ветеранов, чиновников и журналистов —
            # строить им рабочую почту нельзя
            if not post:
                continue
            seen_p.add(fio.lower())
            people.append({"fio": fio, "position": post.group(1).strip(),
                           "source": final_url})
        for cand in EF.extract_candidates(html, path or "/"):
            email = cand["email"]
            if email in seen_e or EF.is_junk(email) or domain_of(email) not in mail_domains:
                continue
            seen_e.add(email)
            emails.append({"email": email, "fio": ""})
    if log and (people or emails):
        log(f"    сайт {domain}: людей {len(people)}, адресов {len(emails)} "
            f"(страниц опрошено {tried})")
    return people, emails


def _dedup_people(persons):
    """Один человек = одна запись: руководитель приходит и из ЕГРЮЛ (Фаза 1), и из
    находок движка. Побеждает запись с более содержательной должностью."""
    best = {}                                      # dict помнит порядок вставки
    for p in persons:
        key = re.sub(r"[^а-яёa-z]", "", (p.get("fio") or "").lower())
        cur = best.get(key)
        if cur is None:
            best[key] = dict(p)
            continue
        # должность из находок движка (у неё есть URL-источник) точнее дежурной из ЕГРЮЛ
        if p.get("position") and str(p.get("source", "")).startswith("http"):
            cur["position"], cur["source"] = p["position"], p["source"]
        elif len(p.get("position") or "") > len(cur.get("position") or ""):
            cur["position"] = p["position"]
    return list(best.values())


def known_emails_from(lead, findings_text=""):
    """Уже известные адреса на домене компании — материал для вывода схемы.
    Личные адреса ценнее: только по ним виден «почерк» домена."""
    out, seen = [], set()
    dom = lead_domain(lead)[0]

    def add(email, fio="", source=""):
        email = (email or "").strip().lower()
        # литерал \n из склеенного текста прилипает к локал-парту и рождает фантомы
        # (\nabutov@ рядом с реальным abutov@) — режем только явный литерал
        while email.startswith("\\n"):
            email = email[2:]
        if not email or "@" not in email or email in seen or EF.is_junk(email):
            return
        if dom and domain_of(email) != dom:
            return
        seen.add(email)
        out.append({"email": email, "fio": fio, "source": source})

    add(lead.get("email"), source=lead.get("_email_src") or "контакты компании (Фаза 1)")
    for data in _findings_blocks(findings_text):
        for row in _dict_rows(data.get("contacts"), "emails"):
            add(row.get("email"), source=row.get("source") or "")
        for row in _dict_rows(data.get("procurement"), "contacts"):
            add(row.get("email"), row.get("fio") or "", row.get("source") or "")
    return out


# ============================================================================
# ГЛАВНЫЙ СБОРЩИК ПО КОМПАНИИ
# ============================================================================
_CONF = {
    "published": "подтверждён (опубликован компанией)",
    "high": "высокая (адрес по схеме домена, сверенной с ФИО сотрудника)",
    "medium": "средняя (домен принимает почту, адрес по типовой схеме)",
    "weak_mx": "ниже средней (у домена нет MX, почта пойдёт на A-запись)",
    "low": "низкая (доставимость домена не проверена)",
    "rejected": "НЕ отправлять (домен почту не принимает)",
}


def _confidence(cand_scheme, domain_scheme, scheme_exact, accepts, weak_mx):
    """Уверенность считается ПО КАЖДОМУ адресу: когда схема домена человеку не
    подошла (нет отчества) и он получил кандидатов из общего каталога, выдавать их
    за «адрес по схеме домена» нельзя."""
    if accepts is False:
        return _CONF["rejected"]
    if accepts is None:
        return _CONF["low"]
    if scheme_exact and cand_scheme == domain_scheme:
        return _CONF["high"]
    if weak_mx:
        return _CONF["weak_mx"]
    return _CONF["medium"]


def _same_person(a, b):
    """Одно ли это лицо: сверяем фамилию и имя, отчество не обязательно."""
    pa, pb = split_fio(a), split_fio(b)
    if not (pa and pb):
        return False
    return (pa["surname"].lower() == pb["surname"].lower()
            and (not pa["name"] or not pb["name"]
                 or pa["name"].lower() == pb["name"].lower()))


def published_for(known, fio, persons=()):
    """Опубликованные адреса, ОДНОЗНАЧНО принадлежащие этому человеку.

    Схема «голая фамилия» одинакова у однофамильцев, поэтому совпадения шаблона
    мало: если под тот же адрес подходит второй человек из списка, фактом он не
    считается ни для кого — иначе почта закупщика Иванова уезжает директору
    Иванову с пометкой «подтверждён»."""
    out = []
    for item in known or []:
        owner = (item.get("fio") or "").strip()
        if owner:                                  # владелец известен явно — сверяем с ним
            if _same_person(owner, fio):
                out.append(item)
            continue
        if not belongs_to(item["email"], fio):
            continue
        rivals = [p for p in persons
                  if not _same_person(p.get("fio", ""), fio)
                  and belongs_to(item["email"], p.get("fio", ""))]
        if not rivals:
            out.append(item)
    return out


def _persons_for_company(lead, findings_text, domain, use_site, log):
    """Ключевые сотрудники компании из всех источников: руководитель из ЕГРЮЛ
    (Фаза 1), находки движка и страницы руководства сайта.
    Возвращает (люди, адреса с сайта, замечания)."""
    persons, site_emails, notes = [], [], []
    if lead.get("contact_person"):
        persons.append({"fio": lead["contact_person"],
                        "position": lead.get("_lpr_post") or "руководитель (ЕГРЮЛ)",
                        "source": "ЕГРЮЛ (Фаза 1)"})
    persons.extend(people_from_findings(findings_text))
    if use_site:
        try:
            site_people, site_emails = people_from_site(domain, log=log)
        except Exception as exc:                   # noqa: BLE001 — сайт лежит/антибот
            site_people, site_emails = [], []
            notes.append(f"сайт не обойдён: {str(exc)[:60]}")
        persons.extend(site_people)
    return persons, site_emails, notes


def _apply_smtp_verdicts(res, log=None, lead=None):
    """Проверить кандидатов без отправки письма и записать вердикты вместе с их весом.

    Вердикт меняет уверенность только если ему МОЖНО верить: у mail.ru «принято»
    ничего не значит, а у Exchange Online проба вообще не делается."""
    addrs = [r["email"] for p in res["people"] for r in p["emails"]
             if r["scheme"] != "опубликован"][:5]
    try:
        check = verify_addresses(res["domain"], addrs, mail=res.get("mail"), log=log,
                                 lead=lead)
    except Exception:                              # noqa: BLE001 — проба не критична
        return
    res["smtp"] = {k: check[k] for k in
                   ("probed", "provider", "trust", "catch_all", "reason")}
    judged = [v for v in check["verdicts"].values() if v["trusted"]]
    if judged and all(v["verdict"] == "no" for v in judged):
        # сервер отверг ВСЕ проверенные гипотезы — значит схема домена другая,
        # и менеджеру важно знать это прямо, а не гадать по пометкам уверенности
        res["notes"].append(
            "почтовый сервер отклонил все проверенные гипотезы — схема адресов домена "
            "не угадана; нужен реальный адрес сотрудника (сайт, закупки, визитка)")
    for person in res["people"]:
        for row in person["emails"]:
            got = check["verdicts"].get(row["email"])
            if not got:
                continue
            row["verdict"] = got["verdict"]
            row["verdict_note"] = got["note"]
            if not got["trusted"]:
                continue
            if got["verdict"] == "no":
                row["confidence"] = "НЕ отправлять (почтовый сервер: такого ящика нет)"
            elif got["verdict"] == "ok":
                row["confidence"] = "подтверждён почтовым сервером (без отправки письма)"


def guess_for_company(lead, findings_text="", people=None, check_mx=True, smtp=False,
                      per_person=3, use_site=True, log=print):
    """Лид (+ находки движка) -> гипотезы почты ключевых сотрудников.

    Никогда не бросает исключений наружу: недоступный DNS/SMTP просто снижает
    уверенность — компания не должна падать из-за подбора почты."""
    name = lead.get("name") or ""
    domain, dom_src = lead_domain(lead)
    res = {"company": name, "inn": str(lead.get("_inn") or ""), "domain": domain,
           "domain_source": dom_src, "mail": {}, "smtp": {}, "scheme": None,
           "published": [], "people": [], "notes": []}

    known = known_emails_from(lead, findings_text)
    res["published"] = [k["email"] for k in known]
    if not domain:
        res["notes"].append(dom_src)
        return res
    if domain in EF.FREE_PROVIDERS:
        res["notes"].append(
            f"домен {domain} — публичный почтовый сервис, личные адреса по схеме не строим")
        return res

    if check_mx:
        try:
            res["mail"] = mail_domain_state(domain)
        except Exception as exc:                   # noqa: BLE001 — DNS не критичен
            res["notes"].append(f"MX не проверен: {str(exc)[:60]}")
    accepts = res.get("mail", {}).get("accepts_mail")

    # кого обогащаем: переданный список либо руководитель из ЕГРЮЛ + найденные движком
    persons = list(people or [])
    if not persons:
        persons, site_emails, notes = _persons_for_company(
            lead, findings_text, domain, use_site, log)
        res["notes"].extend(notes)
        have = {k["email"] for k in known}
        known.extend(e for e in site_emails if e["email"] not in have)
        res["published"] = [k["email"] for k in known]
    named = [p for p in persons if is_person(p.get("fio"))]
    if len(named) < len(persons):
        res["notes"].append(
            f"пропущено записей, не распознанных как ФИО человека: {len(persons) - len(named)} "
            f"(управляющая организация, должность вместо имени и т.п.)")
    persons = _dedup_people(named)

    # схема домена выводится ПОСЛЕ сбора людей: связанный с ФИО адрес опознаётся
    # точно, а по одной лишь форме локал-парта — только предположительно
    res["scheme"] = infer_scheme(link_known_to_people(known, persons))
    scheme = res["scheme"] or {}
    if scheme:
        how = "сверена с ФИО" if scheme["exact"] else "по форме адресов"
        log(f"    схема домена {domain}: {scheme['scheme']} "
            f"(адресов: {scheme['addresses']}, {how})")

    scheme_id, profile, sep = scheme.get("scheme"), scheme.get("profile"), scheme.get("sep")
    scheme_exact = bool(scheme.get("exact"))
    # почта через A-запись доставляется хуже, чем через настоящий MX: у сайта-визитки
    # на конструкторе A-запись есть всегда, а ящиков нет вовсе
    weak_mx = accepts and not (res.get("mail") or {}).get("mx")
    for person in persons:
        cands = guess_emails(person["fio"], domain, scheme=scheme_id, profile=profile,
                             sep=sep, limit=per_person if scheme_id else max(per_person, 6))
        if not cands:
            continue
        # адрес, уже опубликованный компанией и построенный из ЭТОГО ФИО, — факт, не гипотеза
        found = published_for(known, person["fio"], persons)
        published = {item["email"] for item in found}
        rows = [{"email": item["email"], "scheme": "опубликован", "score": 1000,
                 "confidence": _CONF["published"], "verdict": "не требуется",
                 "source": item.get("source") or "источник не сохранён"}
                for item in found]
        for c in cands[:per_person]:
            if c["email"] in published:
                continue
            conf = _confidence(c["scheme"], scheme_id, scheme_exact, accepts, weak_mx)
            rows.append(dict(c, confidence=conf, verdict="не проверялся"))
        res["people"].append({"fio": person["fio"], "position": person.get("position", ""),
                              "source": person.get("source", ""), "emails": rows})

    if smtp and res["people"]:
        _apply_smtp_verdicts(res, log=log, lead=lead)
    return res


def format_findings_block(res):
    """Текстовый блок для писателя .docx — в одном стиле с person_enrich."""
    L = ["=== ГИПОТЕЗЫ КОРПОРАТИВНОЙ ПОЧТЫ (email_guess — по схеме домена) ==="]
    if not res.get("domain"):
        L.append(f"Домен компании не определён: {res.get('domain_source', '')}")
        return "\n".join(L)
    L.append(f"Домен: {res['domain']} (источник: {res['domain_source']})")
    mail = res.get("mail") or {}
    if mail:
        accepts = mail.get("accepts_mail")
        if accepts:
            state = "принимает почту"
        elif accepts is False:
            state = "почту НЕ принимает"
        else:                                      # None — не то же самое, что False
            state = "не проверено"
        line = f"Доставимость домена: {state}"
        if mail.get("via"):
            line += f", {mail['via']}"
        if mail.get("provider"):
            line += f", провайдер {mail['provider']}"
        if mail.get("note"):
            line += f" — {mail['note']}"
        L.append(line)
    smtp = res.get("smtp") or {}
    if smtp:
        line = ("Проверка ящиков без отправки письма: "
                + ("выполнена" if smtp.get("probed") else "не выполнена"))
        if smtp.get("provider"):
            line += f", почта на {smtp['provider']}"
        if smtp.get("catch_all"):
            line += ", домен принимает ЛЮБОЙ адрес (catch-all)"
        if smtp.get("reason"):
            line += f" — {smtp['reason']}"
        L.append(line)
    if res.get("scheme"):
        s = res["scheme"]
        L.append(f"Схема адресов домена: {s['scheme']} "
                 f"(подтверждена: {', '.join(s['examples'])})")
    else:
        L.append("Схема адресов домена не выведена — кандидаты даны по типовым схемам РФ.")
    for p in res.get("people", []):
        L.append(f"{p['fio']} — {p.get('position') or 'должность не указана'}"
                 + (f" [{p['source']}]" if p.get("source") else ""))
        for e in p["emails"]:
            line = f"  - {e['email']} [{e['confidence']}]"
            if e.get("source"):
                line += f", источник: {e['source']}"
            if e.get("verdict") in ("ok", "no", "unknown"):
                line += f", проверка: {e.get('verdict_note') or e['verdict']}"
            L.append(line)
    for n in res.get("notes", []):
        L.append(f"Замечание: {n}")
    L.append("ВАЖНО: адреса без пометки «подтверждён» — расчётные гипотезы по схеме "
             "домена, а не найденные контакты. Первое письмо дублировать на общую почту.")
    return "\n".join(L)


# ============================================================================
# CLI
# ============================================================================
def _findings_for(lead):
    """Кэш находок движка (проход roles) — оттуда берутся ключевые сотрудники."""
    cache = os.environ.get("ORQ_CACHE_DIR") or r"D:\orq_cache"
    inn = str(lead.get("_inn") or "").strip()
    if not inn:
        return ""
    for fname in (f"findings_{inn}__roles.md", f"findings_{inn}.md"):
        path = os.path.join(cache, fname)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                return ""
    return ""


def main():
    ap = argparse.ArgumentParser(
        description="Гипотезы корпоративной почты сотрудников по ФИО и домену компании")
    ap.add_argument("fio", nargs="?", help="ФИО (Фамилия Имя Отчество)")
    ap.add_argument("domain", nargs="?", help="домен компании или её общая почта")
    ap.add_argument("--leads", help="JSON лидов Фазы 1 — пакетный режим")
    ap.add_argument("--out", help="куда сохранить результат пакетного режима (JSON)")
    ap.add_argument("--no-mx", action="store_true", help="не проверять доставимость домена")
    ap.add_argument("--no-site", action="store_true",
                    help="не обходить страницы руководства сайта (быстрее, но меньше людей)")
    ap.add_argument("--smtp", action="store_true", help="+ SMTP-проба (медленно, часто unknown)")
    ap.add_argument("--per-person", type=int, default=3, help="сколько гипотез на человека (3)")
    ap.add_argument("--mev-credits", action="store_true",
                    help="остаток кредитов MyEmailVerifier (сам запрос кредит не тратит)")
    a = ap.parse_args()

    if a.mev_credits:
        left, why = mev_credits()
        if left is None:
            print(f"MyEmailVerifier: баланс не получен — {why}")
            return
        print(f"MyEmailVerifier: {left} кредитов на счету; "
              f"суточный лимит прогона {mev_policy()['daily_limit']}, "
              f"слой {'включён' if mev_enabled() else 'ВЫКЛЮЧЕН (EMAIL_MEV_ENABLE)'}")
        return

    if a.leads:
        with open(a.leads, encoding="utf-8") as fh:
            leads = json.load(fh)
        leads = leads if isinstance(leads, list) else [leads]
        out = []
        for i, lead in enumerate(leads, 1):
            print(f"\n=== [{i}/{len(leads)}] {lead.get('name', '')} ===", flush=True)
            res = guess_for_company(lead, _findings_for(lead), check_mx=not a.no_mx,
                                    smtp=a.smtp, per_person=a.per_person,
                                    use_site=not a.no_site)
            print(format_findings_block(res))
            out.append(res)
        path = a.out or os.path.splitext(a.leads)[0] + "_emails.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        total = sum(len(p["emails"]) for r in out for p in r["people"])
        print(f"\nГотово: {len(out)} компаний, {total} адресов -> {path}")
        return

    if not (a.fio and a.domain):
        ap.error("нужно указать ФИО и домен, либо --leads <json>")
    lead = {"name": "", "email": a.domain if "@" in a.domain else f"info@{a.domain}",
            "contact_person": a.fio}
    res = guess_for_company(lead, "", check_mx=not a.no_mx, smtp=a.smtp,
                            per_person=a.per_person, use_site=not a.no_site)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
