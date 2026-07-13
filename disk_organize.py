# -*- coding: utf-8 -*-
r"""
Раскладка собранных лидов по Яндекс Диску ПОСЛЕ сбора.

Дерево:
  <base>/<отрасль>/<категория полноты контактов>/<компания>/
        ├── <компания>_карта_бизнес-процессов.docx
        └── <компания>_карта_ролей_и_контактов_пресейл.docx

Категория считается по 4 полям: email, телефон, сайт, контактное лицо (ЛПР):
  все есть       -> "есть все контактные данные"
  одного нет     -> "нет только <почты|телефона|сайта|ЛПР>"
  нескольких нет -> "нет: почты, телефона, ..."
  нет ничего     -> "нет контактов"

Файлы .docx сейчас — ВАЛИДНЫЕ ЗАГОТОВКИ (шапка + реквизиты + источники).
Когда будет готов генератор контента, подмени тела generate_dossier()/
generate_strategy() — сигнатуры и остальной конвейер не трогаются.

Диск дёргается python-коннектором connectors/yadisk_client.py (официальный
REST API, stdlib). Нужен env YANDEX_DISK_TOKEN — OAuth-токен со scope
cloud_api:disk.write (как получить — см. докстринг yadisk_client.py).

Самостоятельный запуск на готовом JSON (без повторного скрейпа):
  py C:/Users/abalb/.claude/skills/lead-finder/scripts/disk_organize.py ^
     "D:\лиды\leads_energy.json" --base disk:/Лиды
"""
import argparse
import collections
import json
import os
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

def _doc_names(dn):
    """Имена деливераблов в папке компании: карта БП + карта ролей/контактов + презентация.
    Третий элемент (презентация) ДОЛЖЕН совпадать с orchestrator._doc_names."""
    return (f"{dn}_карта_бизнес-процессов.docx",
            f"{dn}_карта_ролей_и_контактов_пресейл.docx",
            f"{dn}_презентация_Telepatt.pdf")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# короткие имена папок-отраслей (ключ _industry -> папка)
SHORT_INDUSTRY = {
    "energy": "Энергетика",
    "water": "Водоснабжение и ЖКХ",
    "construction": "Строительство",
    "processing": "Переработка",
    "opk": "ОПК",
}

# поле лида -> подпись в родительном падеже для имени папки-категории
CONTACT_FIELDS = [
    ("email", "почты"),
    ("phone", "телефона"),
    ("website", "сайта"),
    ("contact_person", "ЛПР"),
]


def _has(lead, key):
    return bool(str(lead.get(key) or "").strip())


def category_for(lead):
    """Имя папки-категории по полноте контактов."""
    missing = [label for key, label in CONTACT_FIELDS if not _has(lead, key)]
    if not missing:
        return "есть все контактные данные"
    if len(missing) == len(CONTACT_FIELDS):
        return "нет контактов"
    if len(missing) == 1:
        return "нет только " + missing[0]
    return "нет " + ", ".join(missing)   # без двоеточия — недопустимо в имени на Диске


# символы, недопустимые в имени на Яндекс Диске и в Windows
_ILLEGAL = '\\/:*?"<>|\t\r\n'


def _safe(s, maxlen=120):
    """Безопасный сегмент пути / имени файла для Диска и Windows."""
    s = str(s or "").strip().replace("«", "").replace("»", "")
    s = "".join(" " if ch in _ILLEGAL else ch for ch in s)
    s = " ".join(s.split())          # схлопнуть пробелы
    s = s.strip(" .")                # Windows не любит хвостовой пробел/точку
    if len(s) > maxlen:
        s = s[:maxlen].strip(" .")
    return s or "без_названия"


def industry_folder(lead):
    key = lead.get("_industry")
    return SHORT_INDUSTRY.get(key) or _safe(lead.get("niche") or key or "Прочее")


def _company_name(lead, dup_names):
    """Имя папки компании; одинаковые названия разводим по ИНН."""
    base = _safe(lead.get("name"))
    inn = str(lead.get("_inn") or "").strip()
    if base in dup_names and inn:
        return _safe(f"{base} (ИНН {inn})")
    return base


# ----------------------------- .docx (заготовки) -----------------------------

def _xml_escape(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _make_docx(path, paragraphs):
    """Записать минимальный валидный .docx. paragraphs: список (текст, bold)."""
    body = []
    for text, bold in paragraphs:
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        body.append(
            '<w:p><w:r>{rpr}<w:t xml:space="preserve">{t}</w:t></w:r></w:p>'
            .format(rpr=rpr, t=_xml_escape(text)))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(body) + '<w:sectPr/></w:body></w:document>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def _fmt_money(v):
    try:
        return f"{int(v):,}".replace(",", " ")
    except Exception:
        return str(v or "")


def generate_dossier(lead, path):
    """ЗАГОТОВКА досье. Подмени телом из своего генератора, сигнатуру сохрани."""
    rev = _fmt_money(lead.get("_revenue"))
    P = [
        ("Досье компании", True),
        (lead.get("name") or "", True),
        ("", False),
        ("Реквизиты", True),
        (f"ИНН: {lead.get('_inn', '')}", False),
        (f"ОГРН: {lead.get('_ogrn', '')}", False),
        (f"Отрасль: {industry_folder(lead)}", False),
        (f"Ниша / ОКВЭД: {lead.get('niche', '')}", False),
        (f"Регион: {lead.get('_region', '')}", False),
        (f"Адрес: {lead.get('_address', '')}", False),
        (f"Выручка: {rev} ₽ ({lead.get('_revenue_year', '')}) "
         f"— {lead.get('_revenue_source_name', '')}", False),
        ("", False),
        ("Контакты", True),
        (f"Контактное лицо: {lead.get('contact_person', '')} "
         f"— {lead.get('_lpr_post', '')}", False),
        (f"Телефон: {lead.get('phone', '')}", False),
        (f"Email: {lead.get('email', '')}", False),
        (f"Сайт: {lead.get('website', '')}", False),
        ("", False),
        ("Источники", True),
        (f"RusProfile: {lead.get('_rusprofile_url', '')}", False),
        (f"Источник выручки: {lead.get('_revenue_source_url', '')}", False),
        ("", False),
        ("— Документ-заготовка. Содержательное досье будет сгенерировано позже. —", False),
    ]
    _make_docx(path, P)


def generate_strategy(lead, path):
    """ЗАГОТОВКА стратегии коммуникации. Подмени телом из своего генератора."""
    P = [
        ("Стратегия коммуникации", True),
        (lead.get("name") or "", True),
        (f"ЛПР: {lead.get('contact_person', '')} — {lead.get('_lpr_post', '')}", False),
        ("", False),
        ("Боль / потребность", True),
        (lead.get("pain") or "(заполнить)", False),
        ("", False),
        ("Предлагаемый оффер", True),
        (lead.get("offer") or "(заполнить)", False),
        ("", False),
        ("Канал захода", True), ("(заполнить)", False),
        ("Первое касание", True), ("(заполнить)", False),
        ("Сценарий разговора", True), ("(заполнить)", False),
        ("Следующий шаг / дата", True), ("(заполнить)", False),
        ("", False),
        ("— Документ-заготовка. —", False),
    ]
    _make_docx(path, P)


# Транслит для заготовки-PDF: базовые шрифты PDF (Helvetica/WinAnsi) кириллицу не несут,
# а тащить сюда шрифтовый файл ради болванки незачем — модуль намеренно stdlib-only.
# Настоящий one-pager кириллицу рендерит нормально (там Chromium + Google Fonts).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "«": '"', "»": '"', "—": "-", "–": "-", "₽": "RUB", "№": "No",
}


def _ascii(text):
    """Кириллица -> латиница; всё, что не влезло в ASCII, отбрасываем."""
    out = []
    for ch in str(text or ""):
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.upper() if ch.isupper() and t else t)
        elif ord(ch) < 128:
            out.append(ch)
    return "".join(out)


def _make_pdf(path, title, lines):
    """Записать МИНИМАЛЬНЫЙ валидный PDF (одна страница A4) без зависимостей — голыми
    объектами PDF, как _make_docx пишет голый OOXML.
    Это ЗАГОТОВКА: настоящий one-pager делает стадия Kimi (orchestrator._onepager_one).
    Размер держим в коридоре гейтов: >5 КБ (иначе примут за пустышку) и заведомо
    < REAL_PDF_MIN=60 КБ (иначе резюм посчитает болванку готовым деливераблом)."""
    def esc(t):
        return _ascii(t).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    # Текст страницы: заголовок + строки.
    parts = ["BT", "/F1 16 Tf", "56 780 Td", f"({esc(title)}) Tj", "/F1 11 Tf", "0 -28 Td"]
    for ln in lines:
        if ln:
            parts.append(f"({esc(ln)}) Tj")
            parts.append("0 -18 Td")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1", "replace")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    # Балласт комментарием (в PDF допустим где угодно): гейт «реальный файл» — строго >5 КБ,
    # а сам PDF из пары строк текста весит меньше килобайта.
    buf += b"% " + (b"zagotovka one-pager Telepatt - budet perezapisan stadiey Kimi. " * 120) + b"\n"
    xref_at = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()

    with open(path, "wb") as f:
        f.write(buf)


def generate_presentation(lead, path):
    """ЗАГОТОВКА третьего деливерабла — валидный .pdf >5 КБ, БЕЗ зависимостей.
    Боевой one-pager делает стадия Kimi (orchestrator._onepager_one); здесь — паритет
    с двумя .docx для dry-run и раскладки ФАЗЫ 1. Текст транслитерирован (см. _ascii)."""
    rev = _fmt_money(lead.get("_revenue"))
    lines = [
        "Zagotovka. Nastoyaschiy one-pager budet sgenerirovan stadiey Kimi.",
        "",
        f"Zakazchik: {lead.get('name') or ''}",
        f"INN: {lead.get('_inn', '')}   OGRN: {lead.get('_ogrn', '')}",
        f"Otrasl: {industry_folder(lead)}",
        f"Nisha / OKVED: {lead.get('niche', '')}",
        f"Region: {lead.get('_region', '')}",
        f"Vyruchka: {rev} RUB ({lead.get('_revenue_year', '')})",
        "Bol (esli izvestna): " + (lead.get("pain") or "(zapolnit)"),
    ]
    _make_pdf(path, "Telepatt - presale one-pager (zagotovka)", lines)


# --------------------------- коннектор Диска (python) ------------------------

_YD = None   # лениво импортированный python-коннектор (connectors/yadisk_client)


def _disk_client():
    """Python-REST коннектор Диска (connectors/yadisk_client) — единственный путь
    на Диск. Нужен env YANDEX_DISK_TOKEN (как получить — докстринг yadisk_client.py);
    без токена первая же операция даст понятный RuntimeError."""
    global _YD
    if _YD is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from connectors import yadisk_client
        _YD = yadisk_client
    return _YD


def _retrying(fn, what):
    """Общий цикл ретраев python-коннектора: лок Яндекса / сетевой сбой -> backoff."""
    import time
    last = ""
    for attempt in range(5):
        try:
            fn()
            return
        except RuntimeError as e:
            blob = str(e).lower()
            if _is_locked(blob) or _is_transient(blob):
                last = str(e)
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{what}: {e}")
    raise RuntimeError(f"{what}: не удалось после ретраев (лок/сеть): {last}")


def _is_locked(blob):
    # временная блокировка Яндекс Диска: 423 DiskResourceLockedError («ресурс заблокирован»)
    return ("423" in blob) or ("locked" in blob) or ("заблокир" in blob)


def _is_transient(blob):
    # транзиентные сбои: сеть (обрыв/таймаут), троттлинг 429 и 5xx самого Диска
    # (включая 500 InternalServerError — Яндекс эпизодически отвечает им на mkdir) — повторяем
    keys = ("network", "error sending request", "timed out", "timeout",
            "connection", "reset", "temporarily", "temporary failure",
            "handshake", "dns", "eof", " 429", " 500", " 502", " 503", " 504")
    return any(k in blob for k in keys)


def _mkdir(path, account=None):
    """Идемпотентно создать один уровень папки. account оставлен в сигнатуре
    для совместимости старых вызовов (у REST-коннектора аккаунт один — по токену)."""
    _retrying(lambda: _disk_client().ensure_dir(path), f"mkdir {path}")


def _upload(local, remote, account=None, overwrite=True):
    _retrying(lambda: _disk_client().upload_file(local, remote, overwrite), f"upload {remote}")


def ensure_dir(path, account, cache):
    """Идемпотентно создать дерево disk:/A/B/C по уровням (с кэшем созданных)."""
    rest = path[len("disk:/"):] if path.startswith("disk:/") else path
    cur = "disk:"
    for part in [p for p in rest.strip("/").split("/") if p]:
        cur = cur + "/" + part
        if cur in cache:
            continue
        _mkdir(cur, account)
        cache.add(cur)


# --------------------------------- основное ---------------------------------

def _put_stub(local, remote, account, overwrite=False):
    """Залить заготовку. По умолчанию БЕЗ перезаписи: если файл на Диске уже есть
    (в т.ч. РЕАЛЬНЫЙ документ прошлой ФАЗЫ 2 — резюм опирается на его размер),
    не трогаем и не считаем ошибкой. Возвращает 1 (залито) / 0 (пропущено)."""
    if overwrite:
        _upload(local, remote, account, True)
        return 1
    try:
        _upload(local, remote, account, False)
        return 1
    except RuntimeError as e:
        if "уже есть" in str(e).lower():
            return 0
        raise


def organize_to_disk(leads, base="disk:/Лиды", account=None, workers=4,
                     overwrite=False, log=print):
    """Разложить лиды по Диску. Возвращает сводку dict.
    overwrite=False (дефолт): заготовки НЕ перезаписывают уже существующие файлы —
    повторный полный прогон не затирает реальные документы прошлых ФАЗ 2."""
    leads = [l for l in (leads or []) if l]
    if not leads:
        return {"companies": 0, "uploaded": 0, "errors": [], "by_category": {},
                "base": base}

    name_counts = collections.Counter(_safe(l.get("name")) for l in leads)
    dup_names = {n for n, c in name_counts.items() if c > 1}

    # верхние уровни (base / отрасль / категория) создаём заранее и последовательно,
    # чтобы папка компании в потоках создавалась одним вызовом без гонок по кэшу
    cache = set()
    ensure_dir(base, account, cache)
    targets = []
    for idx, lead in enumerate(leads):
        ind = _safe(industry_folder(lead))      # все сегменты пути — через санитайзер
        cat = _safe(category_for(lead))
        comp = _company_name(lead, dup_names)
        ensure_dir(f"{base}/{ind}/{cat}", account, cache)
        targets.append((idx, lead, ind, cat, comp, f"{base}/{ind}/{cat}/{comp}"))

    by_cat = collections.Counter(t[3] for t in targets)
    tmp = tempfile.mkdtemp(prefix="leadsdisk_")
    uploaded = 0
    errors = []

    def _one(t):
        idx, lead, ind, cat, comp, comp_dir = t
        _mkdir(comp_dir, account)                 # родитель уже создан
        dn = _safe(lead.get("name"))
        d_local = os.path.join(tmp, f"{idx}_d.docx")
        s_local = os.path.join(tmp, f"{idx}_s.docx")
        p_local = os.path.join(tmp, f"{idx}_p.pdf")
        generate_dossier(lead, d_local)
        generate_strategy(lead, s_local)
        generate_presentation(lead, p_local)      # заготовка one-pager (паритет деливераблов)
        bp_name, rc_name, pdf_name = _doc_names(dn)
        n = 0
        for local, rname in ((d_local, bp_name), (s_local, rc_name), (p_local, pdf_name)):
            n += _put_stub(local, f"{comp_dir}/{rname}", account, overwrite)
        return n

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_one, t): t for t in targets}
            done = 0
            for f in as_completed(futs):
                t = futs[f]
                done += 1
                try:
                    uploaded += f.result()
                except Exception as e:
                    errors.append((t[4], str(e)))
                    log(f"  [!] {t[4]}: {e}")
                if done % 25 == 0 or done == len(targets):
                    log(f"  Диск: {done}/{len(targets)} компаний, {uploaded} файлов")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"companies": len(targets), "uploaded": uploaded, "errors": errors,
            "by_category": dict(by_cat), "base": base}


def _main():
    ap = argparse.ArgumentParser(
        description="Разложить готовый JSON лидов по Яндекс Диску")
    ap.add_argument("json", help="путь к JSON со списком лидов (из pipeline)")
    ap.add_argument("--base", default="disk:/Лиды")
    ap.add_argument("--account", default=None)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    leads = json.load(open(a.json, encoding="utf-8"))
    res = organize_to_disk(leads, base=a.base, account=a.account, workers=a.workers)
    print(f"\n[ГОТОВО] компаний: {res['companies']} | файлов: {res['uploaded']} "
          f"| ошибок: {len(res['errors'])}")
    print(f"[ГОТОВО] по категориям: {res['by_category']}")
    print(f"[ГОТОВО] корень: {res['base']}")


if __name__ == "__main__":
    _main()
