# -*- coding: utf-8 -*-
"""Проверка прямого и косвенного государственного участия по официальным данным.

Основной источник — актуальная PDF-выписка с egrul.nalog.ru. Для АО, чьи
акционеры в ЕГРЮЛ не раскрываются, можно подключить текущий документ
Росимущества через STATE_ROSIM_URL. Локальный файл запрещён: строгий режим
требует HTTPS, свежий Last-Modified и совпадение по валидному ИНН.
"""
from __future__ import annotations

import email.utils
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from inn_util import valid_inn

EGRUL_BASE = "https://egrul.nalog.ru"
DEFAULT_TTL_H = 24.0
DEFAULT_MAX_DEPTH = 6
MAX_DOWNLOAD = 5 * 1024 * 1024
_USER_AGENT = "Mozilla/5.0 (compatible; CIT-RT-LeadGen/1.0; +https://cit-rt.ru)"


class StateOwnershipSourceError(RuntimeError):
    """Официальный источник недоступен или вернул непригодные данные."""


class StateOwnershipDeadline(TimeoutError):
    """Общий лимит строгого добора истёк; новые сетевые вызовы запрещены."""


def _remaining(deadline, fallback):
    if deadline is None:
        return float(fallback)
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise StateOwnershipDeadline("истёк общий лимит проверки госучастия")
    return min(float(fallback), remaining)


@dataclass(frozen=True)
class Owner:
    name: str
    share: Decimal
    inn: str = ""
    kind: str = "legal"                  # public | legal | person | foreign


@dataclass(frozen=True)
class Entity:
    inn: str
    name: str
    kind: str                             # llc | ao | public | other
    owners: tuple[Owner, ...]
    source_url: str
    owners_complete: bool = True
    structure_valid: bool = True
    source_date: str = ""
    source_sha256: str = ""
    source_artifact: str = ""
    region: str = ""                      # субъект РФ юрадреса из выписки ("" = не извлечён)


@dataclass(frozen=True)
class RosimEvidence:
    name: str
    share: Decimal
    source_url: str
    row: str = ""
    inn: str = ""
    published_at: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class OwnershipResult:
    verified: bool
    share: Decimal                        # доказанная нижняя граница
    direct_share: Decimal
    indirect_share: Decimal
    complete: bool
    source_urls: tuple[str, ...]
    trace: tuple[str, ...]
    reasons: tuple[str, ...]
    region: str = ""                      # субъект РФ юрадреса компании по выписке ЕГРЮЛ


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _decimal(value):
    if isinstance(value, Decimal):
        return value
    text = str(value or "").replace("\xa0", "").replace(" ", "").replace("%", "")
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _flat(value):
    return " ".join(str(value or "").replace("\r", "\n").split())


def normalize_name(value):
    """Точное имя без кавычек/ОПФ; дополнительные слова никогда не отбрасываются."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace("Ё", "Е")
    text = re.sub(
        r"^\s*(?:ПУБЛИЧНОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО|"
        r"ОТКРЫТОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО|"
        r"ЗАКРЫТОЕ\s+АКЦИОНЕРНОЕ\s+ОБЩЕСТВО|"
        r"АКЦИОНЕРНОЕ\s+ОБЩЕСТВО|"
        r"ОБЩЕСТВО\s+С\s+ОГРАНИЧЕННОЙ\s+ОТВЕТСТВЕННОСТЬЮ|"
        r"ПАО|ОАО|ЗАО|АО|ООО)\b",
        "",
        text,
    )
    return " ".join(re.findall(r"[0-9A-ZА-Я]+", text))


def _public_entity_name(name):
    value = _flat(name).upper().replace("Ё", "Е")
    if re.match(
        r"^(?:ФЕДЕРАЛЬНОЕ\s+ГОСУДАРСТВЕННОЕ|ГОСУДАРСТВЕННОЕ|"
        r"МУНИЦИПАЛЬНОЕ)\s+(?:УНИТАРНОЕ|КАЗЕННОЕ|БЮДЖЕТНОЕ)\s+",
        value,
    ):
        return True
    if value.startswith(("ГОСУДАРСТВЕННАЯ КОРПОРАЦИЯ ",
                         "ГОСУДАРСТВЕННАЯ КОМПАНИЯ ",
                         "ПУБЛИЧНО-ПРАВОВАЯ КОМПАНИЯ ")):
        return True
    return False


def _public_owner_name(name):
    value = _flat(name).upper().replace("Ё", "Е")
    if value == "РОССИЙСКАЯ ФЕДЕРАЦИЯ":
        return True
    if value.startswith(("МУНИЦИПАЛЬНОЕ ОБРАЗОВАНИЕ ", "ГОРОД МОСКВА",
                         "ГОРОД САНКТ-ПЕТЕРБУРГ", "ГОРОД СЕВАСТОПОЛЬ")):
        return True
    if (re.match(r"^РЕСПУБЛИКА\s+[А-Я]", value)
            or re.match(r"^[А-Я -]+\s+РЕСПУБЛИКА$", value)):
        return True
    if re.match(r"^[А-Я -]+\s+(?:ОБЛАСТЬ|КРАЙ|АВТОНОМНЫЙ ОКРУГ)$", value):
        return True
    if re.match(r"^(?:МИНИСТЕРСТВО|ДЕПАРТАМЕНТ|АДМИНИСТРАЦИЯ|"
                r"КОМИТЕТ ПО УПРАВЛЕНИЮ ИМУЩЕСТВОМ)\b", value):
        return True
    return _public_entity_name(value)


def _public_level(name):
    """Уровень публичного владельца для дедупликации федеральной доли."""
    value = _flat(name).upper().replace("Ё", "Е")
    if (value == "РОССИЙСКАЯ ФЕДЕРАЦИЯ"
            or "РОССИЙСКОЙ ФЕДЕРАЦИИ" in value
            or value.startswith(("ФЕДЕРАЛЬНОЕ ", "ФЕДЕРАЛЬНАЯ ",
                                 "ФЕДЕРАЛЬНЫЙ ", "РОСИМУЩЕСТВО"))):
        return "federal"
    if value.startswith(("МУНИЦИПАЛЬНОЕ ОБРАЗОВАНИЕ ", "АДМИНИСТРАЦИЯ ГОРОДА ")):
        return "municipal"
    if value.startswith(("ГОРОД МОСКВА", "ГОРОД САНКТ-ПЕТЕРБУРГ", "ГОРОД СЕВАСТОПОЛЬ")):
        return "regional"
    if (re.match(r"^РЕСПУБЛИКА\s+[А-Я]", value)
            or re.match(r"^[А-Я -]+\s+РЕСПУБЛИКА$", value)
            or re.search(r"\bРЕСПУБЛИКИ\s+[А-Я]", value)):
        return "regional"
    if (re.match(r"^[А-Я -]+\s+(?:ОБЛАСТЬ|КРАЙ|АВТОНОМНЫЙ ОКРУГ)$", value)
            or re.search(r"\b(?:ОБЛАСТИ|КРАЯ|АВТОНОМНОГО ОКРУГА)\b", value)):
        return "regional"
    return "other"


def region_matches(value, query):
    """Регион/адрес из официального источника соответствует запросу.

    Подстрочный матч без учёта регистра и Ё: запрос «Татарстан» находит и
    «РЕСПУБЛИКА ТАТАРСТАН» из выписки, и «Республика Татарстан» из выдачи."""
    def norm(text):
        return unicodedata.normalize("NFKC", str(text or "")).upper().replace("Ё", "Е")
    needle = norm(query).strip()
    return bool(needle) and needle in norm(value)


def _extract_region(flat):
    """Субъект РФ юрадреса из плоского текста выписки; "" — не извлечён.

    Два формата: структурные строки («Субъект Российской Федерации …») и
    однострочный адрес. Обрезка на следующем нумерованном поле безопасна:
    субъект стоит в начале адреса."""
    match = re.search(
        r"Субъект Российской Федерации\s+(.{2,80}?)(?=\s+\d+\s+[А-ЯЁA-Z]|\s+Сведения\b|\s*$)",
        flat, re.I)
    if match:
        return _flat(match.group(1))
    match = re.search(
        r"(?:Место нахождения юридического лица|Адрес юридического лица)\s+"
        r"(.{5,160}?)(?=\s+\d+\s+ГРН|\s+Сведения\b|\s*$)",
        flat, re.I)
    if match:
        return _flat(match.group(1))
    return ""


def _entity_kind(name):
    value = _flat(name).upper()
    if _public_entity_name(value):
        return "public"
    if re.match(r"^(?:ПАО|ОАО|ЗАО|АО)\b", value) or "АКЦИОНЕРНОЕ ОБЩЕСТВО" in value:
        return "ao"
    if re.match(r"^ООО\b", value) or "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ" in value:
        return "llc"
    return "other"


def _extract_name(block):
    match = re.search(
        r"(?:Полное наименование|Наименование)\s+(.+?)"
        r"(?=\s+\d+\s+(?:ГРН|Страна происхождения|ИНН|Номинальная стоимость|Размер доли|Дата))",
        block,
        re.I,
    )
    if match:
        return _flat(match.group(1))
    person = re.search(
        r"Фамилия\s+Имя\s+Отчество\s+(.+?)\s+\d+\s+ИНН",
        block,
        re.I,
    )
    if person:
        return _flat(person.group(1))
    return ""


def parse_egrul_text(text, *, inn, source_url, source_date="",
                     source_sha256="", source_artifact=""):
    """Текст актуальной PDF-выписки ФНС -> юридическое лицо и текущие владельцы."""
    flat = _flat(text)
    name_match = re.search(
        r"Полное наименование на русском языке\s+(.+?)"
        r"(?=\s+\d+\s+ГРН и дата внесения в ЕГРЮЛ записи)",
        flat,
        re.I,
    )
    if not name_match:
        raise StateOwnershipSourceError("в выписке ЕГРЮЛ не найдено полное наименование")
    name = _flat(name_match.group(1))
    kind = _entity_kind(name)

    heading = "Сведения об участниках / учредителях юридического лица"
    start = flat.find(heading)
    owners = []
    complete = True
    structure_valid = True
    owner_keys = set()
    if start >= 0:
        section = flat[start + len(heading):]
        endings = (
            "Сведения об учете в налоговом органе",
            "Сведения о видах экономической деятельности",
            "Сведения о регистрации в качестве страхователя",
        )
        cuts = [section.find(marker) for marker in endings if section.find(marker) >= 0]
        section = section[:min(cuts)] if cuts else section
        blocks = re.split(
            r"\d+\s+ГРН и дата внесения в ЕГРЮЛ сведений о\s+данном лице",
            section,
            flags=re.I,
        )[1:]
        for block in blocks:
            share_match = re.search(
                r"Размер доли \(в процентах\)\s+([0-9]+(?:[,.][0-9]+)?)",
                block,
                re.I,
            )
            owner_name = _extract_name(block)
            share = _decimal(share_match.group(1)) if share_match else None
            if not owner_name or share is None:
                complete = False
                continue
            if share < Decimal("0") or share > Decimal("100"):
                structure_valid = False
                complete = False
                continue
            inn_match = re.search(r"\bИНН(?:\s+[^\d]{0,40})?\s+(\d{10,12})\b", block, re.I)
            owner_inn = inn_match.group(1) if inn_match else ""
            if _public_owner_name(owner_name):
                owner_kind = "public"
            elif len(owner_inn) == 12 or re.search(r"Фамилия\s+Имя", block, re.I):
                owner_kind = "person"
            elif re.search(r"Страна происхождения", block, re.I) and not owner_inn:
                owner_kind = "foreign"
            else:
                owner_kind = "legal"
                if len(owner_inn) != 10:
                    complete = False
            owner_key = owner_inn or normalize_name(owner_name)
            if owner_key in owner_keys:
                structure_valid = False
                complete = False
                continue
            owner_keys.add(owner_key)
            owners.append(Owner(owner_name, share, owner_inn, owner_kind))
        total = sum((item.share for item in owners), Decimal("0"))
        if total > Decimal("100"):
            structure_valid = False
        if not owners or total < Decimal("99.99") or not structure_valid:
            complete = False
    elif kind != "public":
        complete = False

    return Entity(
        inn=_digits(inn), name=name, kind=kind, owners=tuple(owners),
        source_url=source_url, owners_complete=complete,
        structure_valid=structure_valid, source_date=source_date,
        source_sha256=source_sha256, source_artifact=source_artifact,
        region=_extract_region(flat))


def _egrul_pdf_text(pdf):
    if not bytes(pdf).startswith(b"%PDF"):
        raise StateOwnershipSourceError("ЕГРЮЛ вернул не PDF-выписку")
    try:
        import pymupdf
        document = pymupdf.open(stream=pdf, filetype="pdf")
        text = "\n".join(page.get_text() for page in document)
    except Exception as exc:
        raise StateOwnershipSourceError(
            "не удалось прочитать PDF ЕГРЮЛ (нужен PyMuPDF)") from exc
    if not re.search(
            r"Выписка\s+из\s+(?:ЕГРЮЛ|Единого государственного реестра юридических лиц)",
            text, re.I):
        raise StateOwnershipSourceError("в PDF не найден заголовок выписки ЕГРЮЛ")
    return text


def _egrul_document_date(text):
    head = str(text or "")[:2500]
    patterns = (
        r"Дата\s+(?:формирования|предоставления)\s+(?:выписки|сведений)\s*[:№]?\s*"
        r"(\d{2}\.\d{2}\.\d{4})",
        r"Выписка\s+из\s+(?:Единого государственного реестра юридических лиц|ЕГРЮЛ)"
        r".{0,250}?(\d{2}\.\d{2}\.\d{4})\s*№",
    )
    for pattern in patterns:
        match = re.search(pattern, head, re.I | re.S)
        if match:
            try:
                return datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()
            except ValueError:
                break
    raise StateOwnershipSourceError("в PDF ЕГРЮЛ не найдена дата формирования выписки")


class EgrulClient:
    """Небольшой rate-limited клиент публичного WEB-интерфейса ФНС."""
    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, cache_dir=None, timeout=45, interval=1.0, ttl_h=None):
        root = Path(os.environ.get("ORQ_DATA_ROOT") or "D:/")
        default_cache = root / "orq_cache" / "state_ownership"
        self.cache_dir = Path(cache_dir or default_cache)
        self.timeout = float(timeout)
        self.interval = max(0.0, float(interval))
        self.ttl_h = float(ttl_h if ttl_h is not None else
                           os.environ.get("STATE_OWNERSHIP_CACHE_TTL_H", DEFAULT_TTL_H))

    def _wait(self, deadline=None):
        with self._lock:
            delay = self.interval - (time.monotonic() - self._last_request)
            if delay > 0:
                if delay >= _remaining(deadline, delay + 1):
                    raise StateOwnershipDeadline("лимит истёк во время rate-limit ЕГРЮЛ")
                time.sleep(delay)
            type(self)._last_request = time.monotonic()

    def _open(self, req, *, attempts=3, max_bytes=MAX_DOWNLOAD, deadline=None):
        last = None
        for attempt in range(1, attempts + 1):
            self._wait(deadline)
            try:
                # req создаётся только _request/_download_text с фиксированным EGRUL_BASE.
                timeout = _remaining(deadline, self.timeout)
                with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec B310
                    data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise StateOwnershipSourceError("ответ официального источника превышает лимит")
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt >= attempts:
                    raise StateOwnershipSourceError(
                        f"ЕГРЮЛ HTTP {exc.code} на {urllib.parse.urlsplit(req.full_url).path}") from None
            except StateOwnershipDeadline:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt >= attempts:
                    raise StateOwnershipSourceError(
                        f"ЕГРЮЛ недоступен: {getattr(exc, 'reason', None) or exc}") from None
            delay = min(2.0, 0.5 * attempt)
            if delay >= _remaining(deadline, delay + 1):
                raise StateOwnershipDeadline("лимит истёк перед повтором ЕГРЮЛ")
            time.sleep(delay)
        raise StateOwnershipSourceError(f"ЕГРЮЛ недоступен: {last}")

    def _json(self, req, *, deadline=None):
        try:
            value = json.loads(self._open(req, deadline=deadline).decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise StateOwnershipSourceError("ЕГРЮЛ вернул невалидный JSON") from exc
        if not isinstance(value, dict):
            raise StateOwnershipSourceError("ЕГРЮЛ вернул неожиданный JSON")
        if value.get("captchaRequired"):
            raise StateOwnershipSourceError("ЕГРЮЛ потребовал CAPTCHA")
        return value

    def _request(self, path):
        return urllib.request.Request(
            EGRUL_BASE + path,
            headers={"User-Agent": _USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
        )

    def _download_text(self, inn, *, deadline=None):
        form = urllib.parse.urlencode({
            "vyp3CaptchaToken": "", "page": "", "query": inn,
            "region": "", "PreventChromeAutocomplete": "",
        }).encode("ascii")
        req = urllib.request.Request(
            EGRUL_BASE + "/", data=form,
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        search_token = self._json(req, deadline=deadline).get("t")
        if not search_token:
            raise StateOwnershipSourceError("ЕГРЮЛ не вернул токен поиска")
        result = self._json(self._request(
            "/search-result/" + urllib.parse.quote(str(search_token), safe="")),
            deadline=deadline)
        rows = [row for row in (result.get("rows") or [])
                if isinstance(row, dict) and _digits(row.get("i")) == inn and row.get("k") == "ul"]
        if len(rows) != 1 or not rows[0].get("t"):
            raise StateOwnershipSourceError(f"ЕГРЮЛ не нашёл единственное юрлицо с ИНН {inn}")
        document_token = str(rows[0]["t"])
        self._json(self._request(
            "/vyp-request/" + urllib.parse.quote(document_token, safe="")), deadline=deadline)
        ready = False
        for _ in range(12):
            status = self._json(self._request(
                "/vyp-status/" + urllib.parse.quote(document_token, safe="")), deadline=deadline)
            if status.get("status") == "ready":
                ready = True
                break
            if status.get("status") in ("error", "failed"):
                break
            if 1.0 >= _remaining(deadline, 2.0):
                raise StateOwnershipDeadline("лимит истёк при подготовке выписки ЕГРЮЛ")
            time.sleep(1.0)
        if not ready:
            raise StateOwnershipSourceError("ЕГРЮЛ не подготовил выписку за отведённое время")
        source_url = EGRUL_BASE + "/vyp-download/" + urllib.parse.quote(document_token, safe="")
        pdf = self._open(self._request(
            "/vyp-download/" + urllib.parse.quote(document_token, safe="")), deadline=deadline)
        text = _egrul_pdf_text(pdf)
        return text, source_url, pdf, _egrul_document_date(text)

    def entity(self, inn, *, deadline=None):
        _remaining(deadline, self.timeout)
        inn = _digits(inn)
        if len(inn) != 10 or not valid_inn(inn):
            raise StateOwnershipSourceError(
                f"для ЕГРЮЛ нужен валидный ИНН юрлица из 10 цифр: {inn!r}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"egrul_{inn}.json"
        artifact = self.cache_dir / f"egrul_{inn}.pdf"
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age_h = (time.time() - float(cached["fetched_at"])) / 3600
                pdf = artifact.read_bytes()
                digest = hashlib.sha256(pdf).hexdigest()
                text = str(cached.get("text") or "")
                text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if (age_h <= self.ttl_h and cached.get("source_url")
                        and cached.get("document_date") and cached.get("sha256") == digest
                        and cached.get("text_sha256") == text_digest):
                    return parse_egrul_text(
                        text, inn=inn, source_url=cached["source_url"],
                        source_date=cached["document_date"], source_sha256=digest,
                        source_artifact=str(artifact))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        text, source_url, pdf, document_date = self._download_text(inn, deadline=deadline)
        digest = hashlib.sha256(pdf).hexdigest()
        parsed = parse_egrul_text(
            text, inn=inn, source_url=source_url, source_date=document_date,
            source_sha256=digest, source_artifact=str(artifact))
        payload = {
            "fetched_at": time.time(), "source_url": source_url,
            "document_date": document_date, "sha256": digest,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
        }
        pdf_fd, pdf_tmp = tempfile.mkstemp(
            prefix=artifact.name + ".", suffix=".tmp", dir=str(artifact.parent))
        try:
            with os.fdopen(pdf_fd, "wb") as fh:
                fh.write(pdf)
            os.replace(pdf_tmp, artifact)
        finally:
            if os.path.exists(pdf_tmp):
                os.unlink(pdf_tmp)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return parsed


class RosimRegistry:
    """Точный индекс из явно подключённого актуального документа Росимущества."""
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def lookup(self, name, inn):
        key = _digits(inn)
        if not valid_inn(key):
            return None
        evidence = self.rows.get(key)
        if evidence is None or _digits(evidence.inn) != key:
            return None
        expected_name = normalize_name(name)
        if not expected_name or normalize_name(evidence.name) != expected_name:
            return None
        return evidence

    @staticmethod
    def _official(url):
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        try:
            port_ok = parsed.port in (None, 443)
        except ValueError:
            port_ok = False
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and port_ok and parsed.path.lower().endswith((".xlsx", ".xlsm"))
                and (host == "rosim.gov.ru" or host.endswith(".rosim.gov.ru")))

    @classmethod
    def from_environment(cls, *, deadline=None):
        file_name = (os.environ.get("STATE_ROSIM_FILE") or "").strip()
        url = (os.environ.get("STATE_ROSIM_URL") or "").strip()
        if not file_name and not url:
            return cls()
        if file_name:
            raise StateOwnershipSourceError(
                "STATE_ROSIM_FILE запрещён в строгом режиме: локальный файл не доказывает происхождение")
        if not cls._official(url):
            raise StateOwnershipSourceError("STATE_ROSIM_URL должен вести по HTTPS на rosim.gov.ru")
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        timeout = _remaining(deadline, 60)
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(req, timeout=timeout) as response:  # nosec B310 -- URL проверен выше
                data = response.read(20 * 1024 * 1024 + 1)
                final_url = response.geturl()
                modified_raw = response.headers.get("Last-Modified") or ""
        except Exception as exc:
            raise StateOwnershipSourceError(f"документ Росимущества недоступен: {exc}") from None
        if final_url != url:
            raise StateOwnershipSourceError("документ Росимущества попытался перенаправить запрос")
        if len(data) > 20 * 1024 * 1024:
            raise StateOwnershipSourceError("документ Росимущества превышает 20 МиБ")
        try:
            modified = email.utils.parsedate_to_datetime(modified_raw)
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - modified).total_seconds() / 86400
            max_age = max(1, int(os.environ.get("STATE_ROSIM_MAX_AGE_DAYS", "45")))
            if age_days < -1 or age_days > max_age:
                raise ValueError(f"возраст {age_days:.1f} дней")
        except Exception as exc:
            raise StateOwnershipSourceError(
                f"не подтверждена актуальность XLSX Росимущества (Last-Modified): {exc}") from None
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        digest = hashlib.sha256(data).hexdigest()
        return cls(cls._parse(data, suffix, url, modified.isoformat(), digest))

    @classmethod
    def _parse(cls, data, suffix, source_url, published_at="", sha256=""):
        if suffix in (".xlsx", ".xlsm") or data.startswith(b"PK\x03\x04"):
            try:
                import openpyxl
                workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            except Exception as exc:
                raise StateOwnershipSourceError(f"XLSX Росимущества не читается: {exc}") from None
            found, ambiguous = {}, set()
            for sheet in workbook.worksheets:
                rows = list(sheet.iter_rows(values_only=False))
                header_idx = name_col = share_col = inn_col = None
                for idx, row in enumerate(rows[:40]):
                    labels = [_flat(cell.value).lower() for cell in row]
                    for col, label in enumerate(labels):
                        if "наименование" in label and name_col is None:
                            name_col = col
                        if re.search(r"\bинн\b", label) and inn_col is None:
                            inn_col = col
                        if ("доля" in label or "процент" in label) and share_col is None:
                            share_col = col
                    if name_col is not None and inn_col is not None and share_col is not None:
                        header_idx = idx
                        break
                if header_idx is None:
                    continue
                for number, row in enumerate(rows[header_idx + 1:], header_idx + 2):
                    if max(name_col, inn_col, share_col) >= len(row):
                        continue
                    name = _flat(row[name_col].value)
                    inn = _digits(row[inn_col].value)
                    share_cell = row[share_col]
                    share = _decimal(share_cell.value)
                    if share is not None and "%" in str(share_cell.number_format or ""):
                        share *= Decimal("100")
                    if (name and valid_inn(inn) and share is not None
                            and Decimal("0") <= share <= Decimal("100")):
                        evidence = RosimEvidence(name, share, source_url,
                                                 f"{sheet.title}, строка {number}", inn,
                                                 published_at, sha256)
                        previous = found.get(inn)
                        if previous and (previous.share != share
                                         or normalize_name(previous.name) != normalize_name(name)):
                            ambiguous.add(inn)
                        else:
                            found[inn] = evidence
            for inn in ambiguous:
                found.pop(inn, None)
            if not found:
                raise StateOwnershipSourceError(
                    "в XLSX Росимущества не найдены однозначные ИНН, наименования и доли")
            return found
        raise StateOwnershipSourceError(
            "поддерживается только актуальный XLSX из STATE_ROSIM_URL")


class OwnershipVerifier:
    def __init__(self, egrul=None, rosim=None, *, threshold=Decimal("25"), max_depth=None):
        self.egrul = egrul or EgrulClient()
        self.rosim = rosim if rosim is not None else RosimRegistry.from_environment()
        self.threshold = _decimal(threshold) or Decimal("25")
        self.max_depth = int(max_depth or os.environ.get(
            "STATE_OWNERSHIP_MAX_DEPTH", DEFAULT_MAX_DEPTH))
        self._memo = {}
        self._region_memo = {}            # ИНН -> регион юрадреса из его выписки

    def _resolve(self, name, inn, path, depth, deadline=None):
        _remaining(deadline, 1.0)
        inn = _digits(inn)
        if inn in path:
            return (Decimal("0"), Decimal("0"), False, True, (),
                    (f"цикл владения по ИНН {inn}",), ())
        if depth > self.max_depth:
            return (Decimal("0"), Decimal("0"), False, True, (),
                    ("превышена глубина графа владения",), ())
        if inn in self._memo:
            return self._memo[inn]

        entity = self.egrul.entity(inn, deadline=deadline)
        self._region_memo[inn] = getattr(entity, "region", "") or ""
        urls = {entity.source_url}
        trace, reasons = [], []
        if entity.source_sha256:
            provenance = ", ".join(part for part in (
                f"дата {entity.source_date}" if entity.source_date else "",
                f"sha256 {entity.source_sha256}",
                f"архив {entity.source_artifact}" if entity.source_artifact else "",
            ) if part)
            trace.append(f"ЕГРЮЛ: {entity.name} ({provenance})")
        owner_keys = [item.inn or normalize_name(item.name) for item in entity.owners]
        owner_total = sum((item.share for item in entity.owners), Decimal("0"))
        shares_valid = all(Decimal("0") <= item.share <= Decimal("100")
                           for item in entity.owners)
        if (not entity.structure_valid or not shares_valid
                or len(owner_keys) != len(set(owner_keys))
                or owner_total > Decimal("100")):
            return (Decimal("0"), Decimal("0"), False, False, tuple(trace),
                    (f"структурно некорректный граф владельцев для ИНН {inn}",),
                    tuple(urls))
        if entity.kind == "public" or _public_entity_name(entity.name):
            result = (Decimal("100"), Decimal("100"), True, True,
                      tuple(trace + [
                          f"{entity.name}: публичная организационно-правовая форма = 100%"]),
                      (), tuple(urls))
            self._memo[inn] = result
            return result

        rosim = self.rosim.lookup(entity.name or name, inn) if self.rosim else None
        federal_from_rosim = rosim.share if rosim else Decimal("0")
        if rosim:
            urls.add(rosim.source_url)
            provenance = ", ".join(part for part in (
                rosim.row,
                f"Last-Modified {rosim.published_at}" if rosim.published_at else "",
                f"sha256 {rosim.sha256}" if rosim.sha256 else "",
            ) if part)
            trace.append(f"Росимущество: {entity.name} = {rosim.share}% ({provenance})")

        # Раздел ЕГРЮЛ для АО содержит учредителей при создании, а не текущий
        # реестр акционеров. Эти доли нельзя использовать даже при полном списке.
        if entity.kind == "ao":
            if rosim:
                reasons.append(
                    "Росимущество подтверждает федеральную долю, но не полноту "
                    "регионального/муниципального владения АО")
            else:
                reasons.append(
                    "ЕГРЮЛ не раскрывает текущих акционеров АО; "
                    "точного документа Росимущества нет")
            return (
                federal_from_rosim, federal_from_rosim, False, True,
                tuple(trace), tuple(reasons), tuple(sorted(urls)),
            )

        federal_from_egrul = Decimal("0")
        nonfederal_from_egrul = Decimal("0")
        indirect = Decimal("0")
        complete = entity.owners_complete
        structure_valid = True
        for item in entity.owners:
            if item.kind == "public" or _public_owner_name(item.name):
                if _public_level(item.name) == "federal":
                    federal_from_egrul += item.share
                else:
                    nonfederal_from_egrul += item.share
                trace.append(f"ЕГРЮЛ: {item.name} — прямая доля {item.share}%")
                continue
            if item.kind in ("person", "foreign"):
                continue
            if len(_digits(item.inn)) != 10:
                complete = False
                reasons.append(f"у владельца «{item.name}» нет ИНН юрлица для обхода цепочки")
                continue
            (parent_share, _parent_direct, parent_complete, parent_valid,
             parent_trace, parent_reasons, parent_urls) = (
                self._resolve(item.name, item.inn, path + (inn,), depth + 1, deadline))
            contribution = (item.share * parent_share / Decimal("100"))
            indirect += contribution
            urls.update(parent_urls)
            trace.extend(parent_trace)
            trace.append(
                f"цепочка: {item.share}% × госдоля «{item.name}» {parent_share}% = "
                f"{contribution}%")
            if not parent_complete:
                complete = False
            if not parent_valid:
                structure_valid = False
            reasons.extend(parent_reasons)

        # Федеральное ребро может дублироваться в ЕГРЮЛ и реестре Росимущества;
        # региональные/муниципальные рёбра независимы и суммируются отдельно.
        direct = max(federal_from_rosim, federal_from_egrul) + nonfederal_from_egrul
        combined_declared = owner_total + max(
            Decimal("0"), federal_from_rosim - federal_from_egrul)
        calculated_total = direct + indirect
        if (combined_declared > Decimal("100")
                or calculated_total > Decimal("100")):
            return (
                Decimal("0"), Decimal("0"), False, False, tuple(trace),
                tuple(dict.fromkeys(reasons + [
                    "совокупная прямая/косвенная структура владения превышает 100%",
                ])),
                tuple(sorted(urls)),
            )
        total = calculated_total
        result = (total, direct, complete, structure_valid, tuple(trace),
                  tuple(dict.fromkeys(reasons)), tuple(sorted(urls)))
        # Неполный результат может зависеть от текущего пути (например, цикл),
        # поэтому его нельзя переиспользовать в другой ветке графа.
        if complete:
            self._memo[inn] = result
        return result

    def verify(self, name, inn, *, deadline=None):
        total, direct, complete, structure_valid, trace, reasons, urls = self._resolve(
            name, inn, (), 0, deadline)
        # Порог включительный: ровно 25% — госкомпания, 24.9% — нет (2026-08-19).
        verified = structure_valid and total >= self.threshold
        return OwnershipResult(
            verified=verified,
            share=total,
            direct_share=direct,
            indirect_share=max(Decimal("0"), total - direct),
            complete=complete,
            source_urls=urls,
            trace=trace,
            reasons=reasons,
            region=self._region_memo.get(_digits(inn), ""),
        )
