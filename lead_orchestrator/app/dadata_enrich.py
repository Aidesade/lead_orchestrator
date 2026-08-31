# -*- coding: utf-8 -*-
"""
Обогащение лидов руководителем (ЛПР) из ЕГРЮЛ/ЕГРИП через Dadata.

Бесплатный тариф (10 000 запросов/сутки) полностью отдаёт то, что нам нужно:
data.management.name + data.management.post (ООО) и data.fio/name (ИП).

Стратегия точности (цель — 99% по идентификации ЛПР):
  Tier A (~99%): у лида уже есть ИНН (собран с карточки 2ГИС) -> findById по ИНН ->
                 authoritative карточка ФНС. Однозначно.
  Tier B (~85-95%): ИНН нет -> suggest по «название + регион», принимаем ТОЛЬКО
                 единственного действующего кандидата с бьюти-ОКВЭД и сильным
                 совпадением имени. Иначе -> на ручную проверку.
  Tier C (review): имя+город не дают однозначного кандидата (франшизы, общие
                 названия, ИП без бренда) -> в worklist на ручную верификацию.

Ключ: переменная окружения DADATA_TOKEN или аргумент token.
Документация: https://dadata.ru/api/find-party/  https://dadata.ru/api/suggest/party/
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"
BEAUTY_OKVED_PREFIXES = ("96.02", "96.04", "96.09")  # парикмахерские/космет., физкульт.-оздоров., прочие персональные


class DadataError(RuntimeError):
    pass


class DadataQuotaError(DadataError):
    """Фатально: исчерпан суточный лимит или неверный ключ — прерывать прогон."""
    pass


class DadataClient:
    def __init__(self, token=None, pause=0.05, retries=4):
        self.token = token or os.environ.get("DADATA_TOKEN") or ""
        if not self.token:
            raise DadataError("Не задан DADATA_TOKEN (переменная окружения или token=...)")
        self.pause = pause
        self.retries = retries

    def _post(self, endpoint, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{API}/{endpoint}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Token {self.token}")
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    time.sleep(self.pause)
                    return json.loads(r.read().decode("utf-8")).get("suggestions", [])
            except urllib.error.HTTPError as e:
                if e.code == 429:            # rate limit — подождать и повторить
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if e.code in (401, 403):
                    raise DadataQuotaError(f"{e.code}: исчерпан суточный лимит или неверный ключ")
                raise DadataError(f"HTTP {e.code}: {e.read()[:200]}")
            except urllib.error.URLError as e:
                time.sleep(1.0 * (attempt + 1))
                last = e
        raise DadataError(f"Сеть/лимит после {self.retries} попыток: {last if 'last' in dir() else '429'}")

    def find_by_inn(self, inn, kpp=None):
        """Точный поиск по ИНН. Возвращает data-карточку (или None)."""
        payload = {"query": str(inn), "count": 5}
        if kpp:
            payload["kpp"] = str(kpp)
        sugg = self._post("findById/party", payload)
        if not sugg:
            return None
        # при нескольких филиалах берём ГОЛОВНУЮ (branch_type=MAIN) либо первую
        for s in sugg:
            if (s.get("data") or {}).get("branch_type") == "MAIN":
                return s["data"]
        return sugg[0].get("data")

    def suggest(self, name, region=None, city=None, status=("ACTIVE",), count=10):
        """Поиск по названию с фильтром по региону/городу и статусу. Возвращает [data,...]."""
        payload = {"query": name, "count": count}
        if status:
            payload["status"] = list(status)
        locs = {}
        if region:
            locs["region"] = region
        if city:
            locs["city"] = city
        if locs:
            payload["locations"] = [locs]
        return [s.get("data") for s in self._post("suggest/party", payload) if s.get("data")]


# ---------- извлечение ЛПР из data-карточки ----------

def extract_lpr(data):
    """Из data-карточки Dadata собрать поля ЛПР. Универсально для ЮЛ и ИП."""
    if not data:
        return {}
    typ = data.get("type")  # LEGAL | INDIVIDUAL
    name = data.get("name") or {}
    state = data.get("state") or {}
    mgmt = data.get("management") or {}
    out = {
        "inn": data.get("inn", ""),
        "ogrn": data.get("ogrn", ""),
        "type": typ or "",
        "opf": (data.get("opf") or {}).get("short", ""),
        "legal_name": name.get("short_with_opf") or name.get("full_with_opf") or "",
        "okved": data.get("okved", ""),
        "okved_name": (data.get("okveds") or [{}])[0].get("name", "") if data.get("okveds") else "",
        "state_status": state.get("status", ""),
        "address": (data.get("address") or {}).get("unrestricted_value", ""),
    }
    # доходы по данным ФНС (≈ выручка) — бесплатный fallback к точной выручке
    # из ГИР БО (revenue_enrich). Для ИП обычно null.
    fin = data.get("finance") or {}
    out["revenue"] = fin.get("income")
    out["revenue_year"] = fin.get("year")
    if typ == "INDIVIDUAL":
        fio = data.get("fio") or {}
        full = " ".join(x for x in (fio.get("surname"), fio.get("name"),
                                    fio.get("patronymic")) if x).strip()
        out["lpr_fio"] = full or name.get("full", "")
        out["lpr_post"] = "Индивидуальный предприниматель"
    else:
        out["lpr_fio"] = mgmt.get("name", "")
        out["lpr_post"] = mgmt.get("post", "") or "Руководитель"
    return out


# ---------- скоринг совпадения 2ГИС-лид <-> кандидат Dadata ----------

_STOP = re.compile(r"\b(ооо|оао|зао|пао|ип|студия|салон|центр|клиника|сеть|"
                   r"красоты|маникюра|парикмахерская|барбершоп|spa|спа)\b", re.I)


def _norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"[«»\"'(),.\-/]", " ", s)
    s = _STOP.sub(" ", s)
    return [t for t in s.split() if len(t) > 2]


def name_overlap(a, b):
    ta, tb = set(_norm_name(a)), set(_norm_name(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_beauty(data):
    ok = data.get("okved") or ""
    if any(ok.startswith(p) for p in BEAUTY_OKVED_PREFIXES):
        return True
    for o in (data.get("okveds") or []):
        if any((o.get("code") or "").startswith(p) for p in BEAUTY_OKVED_PREFIXES):
            return True
    return False


# ---------- оркестратор: лид -> ЛПР с тирами достоверности ----------

FEDERAL_CITY = {"Москва", "Санкт-Петербург", "Севастополь"}


def _locations(city):
    if not city:
        return None, None
    if city in FEDERAL_CITY:
        return city, None       # region
    return None, city           # city


def _apply(lead, lpr, tier, conf, src, reason=""):
    """Записать ЛПР-поля в лид (с сохранением доказательной базы)."""
    if lpr:
        lead["contact_person"] = lpr.get("lpr_fio", "")
        lead["_lpr_post"] = lpr.get("lpr_post", "")
        lead["_inn"] = lead.get("_inn") or lpr.get("inn", "")
        lead["_ogrn"] = lead.get("_ogrn") or lpr.get("ogrn", "")
        lead["_legal_name"] = lpr.get("legal_name", "")
        lead["_opf"] = lpr.get("opf", "")
        lead["_type"] = lpr.get("type", "")
        lead["_okved"] = lpr.get("okved", "")
        lead["_state_status"] = lpr.get("state_status", "")
        # доходы из Dadata как fallback (ГИР БО потом перетрёт точной выручкой)
        if lpr.get("revenue") is not None and not lead.get("_revenue"):
            lead["_revenue"] = lpr["revenue"]
            lead["_revenue_year"] = str(lpr.get("revenue_year") or "")
            lead["_revenue_src"] = "dadata"
    lead["_match_tier"] = tier            # A / B / C
    lead["_match_confidence"] = round(conf, 2)
    lead["_match_source"] = src
    lead["_review_reason"] = reason


def resolve_lead(client, lead):
    """Определить ЛПР для одного лида. Стратегия:
      есть ИНН -> findById (Tier A, кросс-валидация против ложной атрибуции);
      нет ИНН  -> suggest по названию+регион, принять только единственного
                  действующего бьюти-кандидата (Tier B), иначе на ручную (Tier C).
    """
    region, city = _locations(lead.get("_city"))
    key = lead.get("_inn") or lead.get("_ogrn")   # findById принимает и ИНН, и ОГРН

    # --- путь по ИНН/ОГРН (с сайта/Checko/2ГИС) ---
    if key:
        data = client.find_by_inn(key)
        if not data:
            return _apply(lead, None, "C", 0.0, lead.get("_inn_src", "inn"),
                          "ИНН/ОГРН не найден в ЕГРЮЛ/ЕГРИП")
        lpr = extract_lpr(data)
        active = lpr.get("state_status") == "ACTIVE"
        beauty = is_beauty(data)
        nm = name_overlap(lead.get("name"), lpr.get("legal_name"))
        src = lead.get("_inn_src", "inn")
        region_ok = bool(lead.get("_city")
                         and lead["_city"] in (lpr.get("address") or ""))
        # ИНН из явной карточки/2ГИС-itin = надёжная привязка -> 0.99.
        # ИНН с сайта-футера (может быть «чужим»: конструктор/агентство) и по
        # телефону Checko (телефон могут делить фирмы) -> требуем подтверждение
        # бьюти-ОКВЭД, региона или названия, иначе на ручную проверку.
        needs_val = str(src).startswith(("site", "checko"))
        if not active:
            return _apply(lead, lpr, "C", 0.3, src,
                          f"организация не действует ({lpr.get('state_status')})")
        if (needs_val or lead.get("_inn_ambiguous")) and not (beauty or region_ok or nm >= 0.34):
            why = ("телефон делят несколько фирм — не подтверждено"
                   if lead.get("_inn_ambiguous")
                   else "ИНН не подтверждён бьюти-ОКВЭД/регионом (возможна чужая атрибуция)")
            return _apply(lead, lpr, "C", 0.5, src, why)
        conf = 0.99 if not needs_val else (0.97 if beauty else (0.92 if region_ok else 0.9))
        return _apply(lead, lpr, "A", conf, src, "")

    # --- путь по названию (для ООО работает, для ИП почти нет) ---
    try:
        cands = client.suggest(lead.get("name", ""), region=region, city=city,
                               status=("ACTIVE",), count=10)
    except DadataError:
        raise
    scored = []
    for c in cands:
        nm = max(name_overlap(lead.get("name"), (c.get("name") or {}).get("short_with_opf")),
                 name_overlap(lead.get("name"), (c.get("name") or {}).get("full")))
        if is_beauty(c) and nm >= 0.5:
            scored.append((nm, c))
    scored.sort(key=lambda x: -x[0])
    if len(scored) == 1 or (len(scored) >= 2 and scored[0][0] - scored[1][0] >= 0.34):
        nm, data = scored[0]
        lpr = extract_lpr(data)
        return _apply(lead, lpr, "B", min(0.93, 0.6 + nm * 0.3),
                      "dadata:name", "")
    reason = ("несколько кандидатов с близким именем" if len(scored) >= 2
              else "нет однозначного кандидата по названию (вероятно ИП)")
    return _apply(lead, None, "C", 0.0, "dadata:name", reason)


def enrich_leads(client, leads, log=print):
    """Обогатить все лиды ЛПР. Возвращает статистику по тирам."""
    stats = {"A": 0, "B": 0, "C": 0, "filled": 0}
    n = len(leads)
    for i, lead in enumerate(leads, 1):
        try:
            resolve_lead(client, lead)
        except DadataQuotaError as e:
            log(f"[stop] Dadata лимит/доступ: {e}")
            break
        except DadataError as e:
            # транзиентная сеть — пометить лид и продолжить, не ронять прогон
            _apply(lead, None, "C", 0.0, lead.get("_inn_src", "dadata"),
                   "Dadata недоступен (сеть) — повторить позже")
            log(f"  [{i}] сеть Dadata, пропуск лида: {e}")
        t = lead.get("_match_tier", "C")
        stats[t] = stats.get(t, 0) + 1
        if lead.get("contact_person"):
            stats["filled"] += 1
        if i % 25 == 0 or i == n:
            log(f"  [{i}/{n}] ЛПР заполнено {stats['filled']} "
                f"| A={stats['A']} B={stats['B']} C={stats['C']}")
    return stats


if __name__ == "__main__":
    # дымовой тест клиента: py dadata_enrich.py 7707083893
    import sys
    inn = sys.argv[1] if len(sys.argv) > 1 else "7707083893"
    c = DadataClient()
    d = c.find_by_inn(inn)
    print(json.dumps(extract_lpr(d), ensure_ascii=False, indent=2))
