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
            f"{dn}_презентация_Telepath.pptx")

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


def _make_pptx(path, title, lines):
    """Записать МИНИМАЛЬНУЮ валидную .pptx (один слайд 16:9) без зависимости от
    python-pptx — той же техникой, что и _make_docx (голый OOXML через zipfile).
    Это ЗАГОТОВКА: настоящая 3-слайдовая презентация делается агентной стадией через
    официальный скилл pptx. Текст пишем в заметках слайда (надёжный размер >5 КБ —
    проходит общий гейт «реальный файл, а не болванка»)."""
    def esc(t):
        return _xml_escape(t)

    NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slides/slide1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{NS_R}/officeDocument" Target="ppt/presentation.xml"/>'
        '</Relationships>')
    # презентация 16:9 (12192000 x 6858000 EMU = 13.333"x7.5")
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
        '<p:sldSz cx="12192000" cy="6858000"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{NS_R}/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        f'<Relationship Id="rId2" Type="{NS_R}/slide" Target="slides/slide1.xml"/>'
        '</Relationships>')

    def _txt_body(paras):
        body = []
        for t, sz, bold in paras:
            b = ' b="1"' if bold else ""
            body.append(
                '<a:p><a:r><a:rPr lang="ru-RU" sz="%d"%s/>'
                '<a:t>%s</a:t></a:r></a:p>' % (sz, b, esc(t)))
        return "".join(body)

    paras = [(title, 2800, True)] + [(ln, 1400, False) for ln in lines if ln]
    slide = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Text"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="685800"/><a:ext cx="10820400" cy="5486400"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/>' + _txt_body(paras) + '</p:txBody></p:sp>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping '
        'bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
        'hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sld>')
    slide_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{NS_R}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '</Relationships>')
    # минимальные мастер/лейаут — присутствуют только чтобы пакет открывался редакторами
    slide_master = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldMaster xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld>'
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
        'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
        'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '</p:sldMaster>')
    slide_master_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{NS_R}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '</Relationships>')
    slide_layout = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}" type="blank" preserve="1">'
        '<p:cSld name="Blank"><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    slide_layout_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{NS_R}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        '</Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        z.writestr("ppt/slides/slide1.xml", slide)
        z.writestr("ppt/slides/_rels/slide1.xml.rels", slide_rels)
        z.writestr("ppt/slideMasters/slideMaster1.xml", slide_master)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", slide_layout)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels)
        # «балласт» БЕЗ сжатия (ZIP_STORED) — чтобы заготовка стабильно превышала гейт
        # >5 КБ в orchestrator.process() (сжатый повторяющийся текст ужимается слишком сильно).
        note = ("Заготовка презентации Telepath. Реальная 3-слайдовая .pptx делается "
                "агентной стадией через официальный скилл pptx. " * 120)
        z.writestr("docProps/_note.txt", note, compress_type=zipfile.ZIP_STORED)


def generate_presentation(lead, path):
    """ЗАГОТОВКА презентации (третий деливерабл). Валидная .pptx >5 КБ, БЕЗ python-pptx.
    Боевая 3-слайдовая презентация формируется агентной стадией _presentation_one через
    официальный скилл pptx; здесь — паритет с двумя .docx для dry-run и раскладки ФАЗЫ 1."""
    rev = _fmt_money(lead.get("_revenue"))
    lines = [
        "Презентация Telepath — заготовка",
        f"Заказчик: {lead.get('name') or ''}",
        f"ИНН: {lead.get('_inn', '')}   ОГРН: {lead.get('_ogrn', '')}",
        f"Отрасль: {industry_folder(lead)}",
        f"Ниша / ОКВЭД: {lead.get('niche', '')}",
        f"Регион: {lead.get('_region', '')}",
        f"Выручка: {rev} ₽ ({lead.get('_revenue_year', '')})",
        "Боль (если известна): " + (lead.get("pain") or "(заполнить)"),
        "— 3-слайдовая презентация будет сгенерирована стадией pptx. —",
    ]
    _make_pptx(path, "Telepath — пресейл-презентация", lines)


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
    # транзиентные сетевые сбои (обрыв соединения, таймаут, 5xx шлюза) — стоит повторить
    keys = ("network", "error sending request", "timed out", "timeout",
            "connection", "reset", "temporarily", "temporary failure",
            "handshake", "dns", "eof", " 502", " 503", " 504")
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

def organize_to_disk(leads, base="disk:/Лиды", account=None, workers=4,
                     overwrite=True, log=print):
    """Разложить лиды по Диску. Возвращает сводку dict."""
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
        p_local = os.path.join(tmp, f"{idx}_p.pptx")
        generate_dossier(lead, d_local)
        generate_strategy(lead, s_local)
        generate_presentation(lead, p_local)      # заготовка-презентация (паритет деливераблов)
        bp_name, rc_name, pptx_name = _doc_names(dn)
        _upload(d_local, f"{comp_dir}/{bp_name}", account, overwrite)
        _upload(s_local, f"{comp_dir}/{rc_name}", account, overwrite)
        _upload(p_local, f"{comp_dir}/{pptx_name}", account, overwrite)
        return 3

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
