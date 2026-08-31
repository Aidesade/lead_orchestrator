# -*- coding: utf-8 -*-
"""
Checko API: поиск компании ПО ТЕЛЕФОНУ -> ИНН (главный рычаг: телефон есть у 100%
лидов, а Dadata реверс по телефону не умеет).

Намеренно схемо-независимо: от поиска нам нужен только ИНН, а его извлекает
inn_util.find_requisites по контрольной сумме из ЛЮБОГО JSON-ответа — поэтому код
не ломается, даже если Checko поменяет имена полей. Сам ИНН -> ЛПР резолвит Dadata
(findById, free 10k/день), чтобы беречь квоту Checko (free 100 запросов/день).

Телефон в ЕГРИП — регистрационный (что ИП дал ФНС), может не совпасть с рабочим
из 2ГИС -> hit-rate неполный (~30-60%); это нормально, остаток идёт дальше по
конвейеру (сайт -> Dadata -> обзвон).

Ключ: env CHECKO_TOKEN или token=. Endpoint: https://api.checko.ru/v2/search
CLI-калибровка на реальном ключе:  py checko_enrich.py 74991234567 --dump
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from inn_util import find_requisites

BASE = "https://api.checko.ru/v2"


class CheckoError(RuntimeError):
    pass


def checko_keys():
    """Ключи Checko ПО ПОРЯДКУ: CHECKO_TOKEN (можно несколько через запятую), затем запасные
    CHECKO_TOKEN_ALT / _2 / _3 — на случай исчерпанного лимита или 403. Дедуп, порядок сохранён.
    Единый источник для обоих клиентов (checko_enrich.CheckoClient и source_checko.CheckoSearch)."""
    out = []
    for var in ("CHECKO_TOKEN", "CHECKO_TOKEN_ALT", "CHECKO_TOKEN_2", "CHECKO_TOKEN_3"):
        v = (os.environ.get(var) or "").strip()
        if v:
            out += [k.strip() for k in v.split(",") if k.strip()]
    return list(dict.fromkeys(out))


class CheckoClient:
    def __init__(self, token=None, pause=0.2, retries=3):
        self.keys = [token.strip()] if token else checko_keys()
        if not self.keys:
            raise CheckoError("Не задан CHECKO_TOKEN (env или token=...)")
        self._ki = 0                              # индекс текущего ключа (запоминаем рабочий)
        self.pause = pause
        self.retries = retries

    @property
    def token(self):
        return self.keys[self._ki]

    def _get(self, method, params):
        tried = 0
        while tried < len(self.keys):
            url = f"{BASE}/{method}?" + urllib.parse.urlencode(dict(params, key=self.token))
            for attempt in range(self.retries):
                try:
                    req = urllib.request.Request(url, headers={"Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        time.sleep(self.pause)
                        return json.loads(r.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        return {}
                    if e.code == 429:                 # транзиентный троттл — подождать и повторить
                        time.sleep(1.5 * (attempt + 1)); continue
                    if e.code in (401, 403):          # ключ невалиден/исчерпан — на следующий
                        break
                    raise CheckoError(f"HTTP {e.code}: {e.read()[:200]}")
                except urllib.error.URLError:
                    time.sleep(1.0 * (attempt + 1))
            tried += 1                                # текущий ключ не сработал -> следующий
            if tried < len(self.keys):
                self._ki = (self._ki + 1) % len(self.keys)
                print(f"[checko] ключ исчерпан/невалиден — переключаюсь на запасной #{self._ki + 1}")
        raise CheckoError("Checko: все ключи исчерпаны/недоступны (лимит или 403)")

    def company(self, inn, dump=False):
        """ИНН -> карточка организации ЕГРЮЛ (включая блок 'Контакты'). {} если нет."""
        resp = self._get("company", {"inn": re.sub(r"\D", "", inn or "")})
        if dump:
            print(json.dumps(resp, ensure_ascii=False, indent=1)[:2500])
        return resp.get("data") or {}

    def search_phone(self, phone, dump=False):
        """Телефон -> список ИНН (валидных по к.с.), лучший первым. [] если нет."""
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) < 10:
            return []
        # Checko search принимает query; тип запроса определяется автоматически.
        resp = self._get("search", {"query": digits})
        if dump:
            print(json.dumps(resp, ensure_ascii=False, indent=1)[:2000])
        return find_requisites(resp).get("inns", [])


def checko_phone_pass(client, leads, cap=100, log=print):
    """Pass 0: проставить _inn лидам по телефону через Checko. cap — суточный лимит
    бесплатного тарифа (100). Возвращает статистику."""
    targets = [l for l in leads if l.get("phone") and not l.get("_inn")]
    used = hit = ambiguous = 0
    log(f"=== Checko byphone: кандидатов {len(targets)} | лимит {cap} запросов ===")
    for l in targets:
        if used >= cap:
            log(f"  [stop] достигнут лимит Checko {cap}/день — остаток уйдёт на сайт/Dadata")
            break
        used += 1
        try:
            inns = client.search_phone(l["phone"])
        except CheckoError as e:
            log(f"  [stop] {e}"); break
        if inns:
            l["_inn"] = inns[0]
            l["_inn_src"] = "checko:phone"
            if len(inns) > 1:
                l["_inn_ambiguous"] = True   # телефон делят несколько фирм
                ambiguous += 1
            hit += 1
        if used % 25 == 0:
            log(f"  Checko: {used} запросов, ИНН найден {hit}")
    log(f"=== Checko: ИНН по телефону {hit}/{used} (неоднозначных {ambiguous}) ===")
    return {"used": used, "hits": hit, "ambiguous": ambiguous}


# --- контакты по ИНН (метод /company -> блок 'Контакты') --------------------
# операторские/ЭДО-адреса (роутинг диадок/тензор/сбис/такском) — не контакт ЛПР
_OPERATOR_MAIL = re.compile(
    r"@(?:[\w.-]*\.)?(tensor\.ru|diadoc|sbis\.ru|taxcom|kontur|edisoft|esphere|ofd|astralnalog)",
    re.I)
_COOP = ("zakup", "tender", "torg", "postavshik", "postavchik", "partner",
         "sotrudnich", "opt", "sale", "b2b", "prodazh")
_GENERAL = ("info", "office", "mail", "priem", "secretar", "kancel", "kanc",
            "general", "company", "reception", "post", "obsh")
# Служебные ящики: письмо попадёт в техподдержку, в робота или в системный
# алиас, а не человеку, который принимает решения. Проверяется ПЕРВЫМ, иначе
# support@ не попадает ни в один список ниже и уходит в «личная» — то есть
# получает приоритет ВЫШЕ, чем info@, и вытесняет реальный контакт компании.
# Отдельный список, а не email_finder.GENERIC_KW: тот шире и включает info,
# office, mail — как раз те адреса, которые нам нужны.
_SERVICE = ("support", "help", "hotline", "noreply", "no-reply", "donotreply",
            "webmaster", "postmaster", "hostmaster", "abuse", "security",
            "sysadmin", "service", "servis", "robot", "notif", "alert",
            "billing", "podderzhka", "tehpodderzhka",
            # Каналы, куда деловое предложение адресовать нельзя в принципе.
            # Найдено на боевой рассылке: письмо ушло на corruption@ — ящик для
            # сообщений о коррупции. Формально адрес «именной» по структуре, по
            # сути — комплаенс-канал, и коммерческое предложение там неуместно.
            "corrupt", "compliance", "komplaens", "antifraud", "whistle",
            "ethics", "etika", "pretenz", "claim", "zhalob", "jalob",
            # кадры: там читают резюме, а не предложения поставщиков
            "vacan", "rabota", "career", "karyer", "resume", "personal")
# Короткие имена сравниваются ЦЕЛИКОМ: как подстроки они ловят чужое —
# «it» сидит внутри «vitaly», «pr» внутри «prodazhi», «test» внутри «testov».
# Сравнивается локальная часть, поэтому писать их с «@» бессмысленно.
_SERVICE_EXACT = ("it", "it-support", "tech", "admin", "administrator", "root",
                  "bot", "test", "edo", "sys", "smtp", "mailer",
                  "hr", "kadry", "legal", "jurist", "urist")
_COOP_EXACT = ("pr",)


def _classify_email(addr):
    lp = (addr or "").split("@", 1)[0].lower()
    if lp in _SERVICE_EXACT or any(k in lp for k in _SERVICE):
        return "служебная", False
    if lp in _COOP_EXACT or any(k in lp for k in _COOP):
        return "сотрудничество", True
    if any(lp.startswith(k) or k in lp for k in _GENERAL):
        return "общая", False
    return "личная", True  # похоже на именную/личную


def _best_email(emails):
    cand = [e for e in (emails or []) if e and "@" in e and not _OPERATOR_MAIL.search(e)]
    if not cand:
        return None, "", False
    # приоритет: сотрудничество > личная > общая > служебная.
    # Служебная — последняя осознанно: её берём, только если у компании вообще
    # больше ничего нет, и в отчёт она идёт с пометкой «не контактная».
    pr = {"сотрудничество": 0, "личная": 1, "общая": 2, "служебная": 3}
    scored = [(pr[_classify_email(e)[0]], e) for e in cand]
    scored.sort()
    best = scored[0][1]
    kind, is_target = _classify_email(best)
    return best, kind, is_target


def extract_company_contacts(data):
    """data (/company) -> {'phone','emails','website','email','email_kind',
    'email_is_target','ceo'}; пустые поля опускаются."""
    k = data.get("Контакты") or {}
    phones = [p for p in (k.get("Тел") or []) if p]
    emails = k.get("Емэйл") or []
    site = k.get("ВебСайт") or ""
    email, kind, is_target = _best_email(emails)
    ruk = data.get("Руковод")
    if isinstance(ruk, list):
        ruk = ruk[0] if ruk else {}
    ceo = (ruk or {}).get("ФИО", "") if isinstance(ruk, dict) else ""
    return {
        "phone": ", ".join(phones[:2]),
        "website": site,
        "email": email or "",
        "email_kind": kind,
        "email_is_target": is_target,
        "ceo": ceo,
    }


def checko_contacts_pass(client, leads, cap=100, only_missing=True, log=print):
    """Заполнить лидам website/phone/email (+ ЛПР, если пуст) по ИНН через /company.
    only_missing: брать только лиды без сайта/телефона/почты. cap — суточный free-лимит."""
    def need(l):
        if not l.get("_inn"):
            return False
        if not only_missing:
            return True
        return not (l.get("website") or l.get("phone") or l.get("email"))
    todo = [l for l in leads if need(l)]
    log(f"=== Checko контакты: кандидатов {len(todo)} | лимит {cap} ===")
    used = site = phone = mail = 0
    for l in todo:
        if used >= cap:
            log(f"  [stop] лимит Checko {cap}/день"); break
        used += 1
        try:
            data = client.company(l["_inn"])
        except CheckoError as e:
            log(f"  [stop] {e}"); break
        if not data:
            continue
        c = extract_company_contacts(data)
        if c["website"] and not l.get("website"):
            l["website"] = c["website"]; site += 1
        if c["phone"] and not l.get("phone"):
            l["phone"] = c["phone"]; phone += 1
        if c["email"] and not l.get("email"):
            l["email"] = c["email"]; l["_email_kind"] = c["email_kind"]
            l["_email_is_target"] = c["email_is_target"]
            l["_email_src"] = "Checko (/company)"; mail += 1
        if c["ceo"] and not l.get("contact_person"):
            l["contact_person"] = c["ceo"]
        if used % 20 == 0:
            log(f"  Checko: {used} запросов | сайт {site} тел {phone} email {mail}")
    log(f"=== Checko контакты: {used} запросов | сайт {site} | тел {phone} | email {mail} ===")
    return {"used": used, "site": site, "phone": phone, "email": mail}


if __name__ == "__main__":
    import sys
    c = CheckoClient()
    if "--company" in sys.argv:
        inn = sys.argv[1]
        print(json.dumps(extract_company_contacts(c.company(inn, dump="--dump" in sys.argv)),
                         ensure_ascii=False, indent=1))
    else:
        phone = sys.argv[1] if len(sys.argv) > 1 else "74951234567"
        print("ИНН по телефону:", c.search_phone(phone, dump="--dump" in sys.argv))
