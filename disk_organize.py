# -*- coding: utf-8 -*-
r"""
Раскладка собранных лидов по Яндекс Диску ПОСЛЕ сбора.

Дерево:
  <base>/<отрасль>/<категория полноты контактов>/<компания>/
        ├── досье_компании_<компания>.docx          (карта бизнес-процессов, агент-1)
        └── контакты_и_точки_входа_<компания>.docx  (контакты/роли/филиалы, агент-2)

Категория считается по 4 полям: email, телефон, сайт, контактное лицо (ЛПР):
  все есть       -> "есть все контактные данные"
  одного нет     -> "нет только <почты|телефона|сайта|ЛПР>"
  нескольких нет -> "нет: почты, телефона, ..."
  нет ничего     -> "нет контактов"

Файлы .docx сейчас — ВАЛИДНЫЕ ЗАГОТОВКИ (шапка + реквизиты + источники).
Содержательные документы рендерят агенты (company_research_agent — карта процессов,
contact_research_agent — контакты); заготовки generate_dossier()/generate_contacts()
используются для --dry-run и как подстраховка. generate_strategy() оставлена на
случай возврата документа стратегии.

Диск дёргается через CLI `yacli` (та же OAuth-сессия, что в скилле yacli-disk):
  yacli disk mkdir  <disk:/путь>
  yacli disk upload <локальный файл> <disk:/путь> --overwrite

Один раз нужно: `yacli login disk`.

Самостоятельный запуск на готовом JSON (без повторного скрейпа):
  py C:/Users/abalb/.claude/skills/lead-finder/scripts/disk_organize.py ^
     "D:\лиды\leads_energy.json" --base disk:/Лиды
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def _find_yacli():
    """Найти бинарь yacli: env YACLI_BIN -> PATH -> стандартная установка Windows."""
    env = os.environ.get("YACLI_BIN")
    if env:
        return env
    found = shutil.which("yacli")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    cand = os.path.join(local, "Programs", "yacli", "bin", "yacli.exe")
    if os.path.exists(cand):
        return cand
    return "yacli"   # последний шанс — вдруг есть в PATH среды запуска


YACLI = _find_yacli()

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


def generate_contacts(lead, path):
    """ЗАГОТОВКА контактов и точек входа. Содержательный отчёт рендерит агент-2."""
    P = [
        ("Контакты и точки входа", True),
        (lead.get("name") or "", True),
        (f"ИНН: {lead.get('_inn', '')} · ОГРН: {lead.get('_ogrn', '')}", False),
        ("", False),
        ("Ключевые точки входа", True),
        (f"Известный ЛПР: {lead.get('contact_person', '')} "
         f"— {lead.get('_lpr_post', '')}", False),
        ("", False),
        ("Контакты", True),
        (f"Телефон: {lead.get('phone', '')}", False),
        (f"Email: {lead.get('email', '')}", False),
        (f"Сайт: {lead.get('website', '')}", False),
        ("", False),
        ("Филиалы", True), ("(заполнить)", False),
        ("", False),
        ("Источники", True),
        (f"RusProfile: {lead.get('_rusprofile_url', '')}", False),
        ("", False),
        ("— Документ-заготовка. Контакты будут собраны вторым агентом. —", False),
    ]
    _make_docx(path, P)


# ------------------------------- yacli (Диск) --------------------------------

def _yacli(args, account=None):
    cmd = [YACLI] + args + ["--format", "json"]
    if account:
        cmd += ["--account", account]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        raise RuntimeError(
            "Не найден CLI `yacli`. Добавь в PATH или задай переменную YACLI_BIN.")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    blob = out or err                       # на ошибке yacli печатает JSON в stderr (stdout пуст)
    try:
        data = json.loads(blob) if blob else {}
    except json.JSONDecodeError:
        data = {"ok": p.returncode == 0, "message": blob}
    return p.returncode, data


def _mkdir(path, account=None):
    rc, data = _yacli(["disk", "mkdir", path], account)
    if rc == 0 and data.get("ok", True):
        return
    blob = (str(data.get("code", "")) + " " + str(data.get("message", ""))).lower()
    # глотаем идемпотентно ТОЛЬКО «папка уже существует»; бары «409»/«exist» опасны —
    # 409 даёт и DiskPathDoesntExistsError (нет род. пути), а «exist» есть и в DoesntExists
    if "existentdirectory" in blob or "уже существ" in blob:
        return
    raise RuntimeError(f"mkdir {path}: {data.get('message') or blob or rc}")


def _upload(local, remote, account=None, overwrite=True):
    args = ["disk", "upload", local, remote]
    if overwrite:
        args.append("--overwrite")
    rc, data = _yacli(args, account)
    if rc != 0 or not data.get("ok", True):
        raise RuntimeError(f"upload {remote}: {data.get('message') or rc}")


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
        c_local = os.path.join(tmp, f"{idx}_c.docx")
        generate_dossier(lead, d_local)
        generate_contacts(lead, c_local)
        _upload(d_local, f"{comp_dir}/досье_компании_{dn}.docx", account, overwrite)
        _upload(c_local, f"{comp_dir}/контакты_и_точки_входа_{dn}.docx", account, overwrite)
        return 2

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
