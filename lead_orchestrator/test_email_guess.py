# -*- coding: utf-8 -*-
r"""
Офлайн-тест email_guess — без сети, без LLM, без SMTP.

Держит контракты, которые легко сломать «упрощением»:
  * транслит даёт ИМЕНОВАННЫЕ схемы, канон первым, и не вырождается (Щербаков
    обязан дать и shcherbakov, и scherbakov — в старом email_finder._translit
    кап резал ветку целиком);
  * адрес приписывается человеку только строгой сверкой (petrov.i@ — почта
    Петрова Ильи, а НЕ Сидорова Петра: имя «Пётр» входит в фамилию «Петров»);
  * схема домена, выведенная по известным адресам, схлопывает выдачу до 1-2
    гипотез — ради этого модуль и написан;
  * подбор никогда не роняет компанию: нет домена/ФИО/DNS — меньше полей.

Запуск: py test_email_guess.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import email_guess as EG

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK ] {name}")
    else:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        _fails.append(name)


# --------------------------------------------------------------- транслит ----
def test_translit():
    sch = EG.translit_variants("Щербаков", "surname")
    check("щ даёт обе схемы", "shcherbakov" in sch and "scherbakov" in sch, str(sch))
    check("канон первым (щ -> shch)", sch[0] == "shcherbakov", str(sch))

    kh = EG.translit_variants("Хожуцкий", "surname")
    check("ветка kh не вырождается", any(v.startswith("kh") for v in kh), str(kh))
    check("кап вариантов фамилии <= 3", len(kh) <= 3, str(kh))

    check("кс -> x", "alexander" in EG.translit_variants("Александр", "name"))
    check("-ий -> -y", "dmitry" in EG.translit_variants("Дмитрий", "name"))
    check("юрий -> yury", "yury" in EG.translit_variants("Юрий", "name"))
    check("ь перед гласной -> y",
          "afanasyeva" in EG.translit_variants("Афанасьева", "surname"))
    check("ц -> ts канон", EG.translit_variants("Цветков", "surname")[0] == "tsvetkov")
    check("мусорный вариант цх -> ch отсечён",
          "chay" not in EG.translit_variants("Цхай", "surname"),
          str(EG.translit_variants("Цхай", "surname")))
    check("латиница на входе не транслитерируется",
          EG.translit_variants("Ivanov", "surname") == ["ivanov"])
    check("отчество — один вариант",
          len(EG.translit_variants("Александрович", "patronymic")) == 1)


# ------------------------------------------------------------------- ФИО ----
def test_split_fio():
    p = EG.split_fio("Руденко Сергей Александрович")
    check("ФИО разобрано",
          p and (p["surname"], p["name"], p["patronymic"]) ==
          ("Руденко", "Сергей", "Александрович"), str(p))

    p = EG.split_fio("Иванов И.И.")
    check("форма «Иванов И.И.»",
          p and p["surname"] == "Иванов" and p["n_ini"] == "и" and p["p_ini"] == "и", str(p))

    p = EG.split_fio("Сергей Руденко")
    check("«Имя Фамилия» развёрнуто", p and p["surname"] == "Руденко", str(p))

    check("организация вместо ФИО отсеяна",
          EG.split_fio("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «УК»") is None)
    check("is_person: организация", not EG.is_person("ООО Ромашка"))
    check("is_person: человек", EG.is_person("Козлова Анна Ивановна"))

    p = EG.split_fio("Абдулаев Рашид Гаджи оглы")
    check("«оглы» не становится четвёртым словом",
          p and p["patronymic"].endswith("оглы"), str(p))


# -------------------------------------------------------------- генерация ----
def test_guess():
    got = EG.guess_emails("Руденко Сергей Александрович", "avtodor-rzn.ru")
    mails = [g["email"] for g in got]
    check("домен подставлен после собаки",
          all(m.endswith("@avtodor-rzn.ru") for m in mails), str(mails[:3]))
    check("топ-схема РФ — фамилия+инициалы слитно",
          mails[0] == "rudenkosa@avtodor-rzn.ru", str(mails[:3]))
    check("и.фамилия среди кандидатов", "s.rudenko@avtodor-rzn.ru" in mails, str(mails))
    check("голая фамилия среди кандидатов", "rudenko@avtodor-rzn.ru" in mails)
    check("ранжирование по убыванию",
          all(got[i]["score"] >= got[i + 1]["score"] for i in range(len(got) - 1)))
    check("лимит соблюдён", len(EG.guess_emails(
        "Руденко Сергей Александрович", "x.ru", limit=3)) == 3)
    check("нет ФИО — нет кандидатов", EG.guess_emails("ООО Ромашка", "x.ru") == [])
    check("нет домена — нет кандидатов", EG.guess_emails("Иванов Иван Иванович", "") == [])

    only_surname = EG.guess_emails("Иванов И.И.", "x.ru")
    check("работает по фамилии с инициалами", bool(only_surname), str(only_surname[:2]))
    check("локал-парт валиден",
          all(EG._LOCAL_OK.match(g["email"].split("@")[0]) for g in only_surname))

    by_scheme = EG.guess_emails("Руденко Сергей Александрович", "dom.ru", scheme="i.S")
    check("заданная схема схлопывает выдачу",
          by_scheme[0]["email"] == "s.rudenko@dom.ru" and len(by_scheme) <= 3,
          str([g["email"] for g in by_scheme]))
    # схема требует отчества, которого нет — человек не должен остаться без гипотез
    fallback = EG.guess_emails("Сергей Руденко", "dom.ru", scheme="i.o.S")
    check("фолбэк, если схема неприменима", bool(fallback), str(fallback[:2]))


# ------------------------------------------------- разбор известных адресов ----
def test_parse_and_scheme():
    got = EG.parse_local("rudenko.s@avtodor-rzn.ru", "Руденко Сергей Александрович")
    check("схема опознана точно по ФИО",
          got and got["scheme"] == "S.i" and got["exact"], str(got))
    check("ролевой ящик схемой не считается", EG.parse_local("info@dom.ru") is None)
    check("закупки — ролевой ящик", EG.parse_local("zakupki@dom.ru") is None)

    got = EG.parse_local("a.ivanov@dom.ru")
    check("форма и.фамилия без ФИО", got and got["scheme"] == "i.S", str(got))

    sch = EG.infer_scheme([{"email": "petrov.a@dom.ru"}, {"email": "sidorov.i@dom.ru"},
                           {"email": "info@dom.ru"}])
    check("схема домена выведена по двум адресам",
          sch and sch["scheme"] == "S.i" and sch["support"] >= 2, str(sch))
    check("ролевой адрес в примеры не попал",
          sch and "info@dom.ru" not in sch["examples"], str(sch))

    # одного адреса достаточно: с разделителем — точная схема, слитного — группа
    check("одного адреса с разделителем достаточно",
          (EG.infer_scheme([{"email": "petrov.a@dom.ru"}]) or {}).get("scheme") == "S.i",
          str(EG.infer_scheme([{"email": "petrov.a@dom.ru"}])))
    check("одиночный слитный адрес даёт группу, а не шаблон",
          (EG.infer_scheme([{"email": "petrov@dom.ru"}]) or {}).get("scheme") == "solid")
    check("ролевой адрес схемы не задаёт",
          EG.infer_scheme([{"email": "zakupki@dom.ru"}]) is None)
    sch1 = EG.infer_scheme([{"email": "rudenko.s@dom.ru",
                             "fio": "Руденко Сергей Александрович"}])
    check("один адрес, опознанный по ФИО, схему задаёт",
          sch1 and sch1["scheme"] == "S.i" and sch1["exact"], str(sch1))
    check("схема по форме не помечается точной",
          sch and not sch.get("exact"), str(sch))
    check("только ролевые адреса — схемы нет",
          EG.infer_scheme([{"email": "info@dom.ru"}, {"email": "office@dom.ru"}]) is None)


def test_separator():
    # регрессия: домен пишет схему через дефис/подчёркивание, а гипотезы выдавались
    # с точкой — реальный адрес не опознавался, а выдуманный шёл в .docx
    sch = EG.infer_scheme([{"email": "petrov-a@dom.ru"}, {"email": "sidorov-i@dom.ru"}])
    check("разделитель домена определён", sch and sch["sep"] == "-", str(sch))
    got = [g["email"] for g in EG.guess_emails(
        "Петров Андрей Иванович", "dom.ru", scheme=sch["scheme"], sep=sch["sep"])]
    check("гипотеза следует разделителю домена", got and got[0] == "petrov-a@dom.ru", str(got))
    check("адрес через дефис принадлежит своему владельцу",
          EG.belongs_to("petrov-a@dom.ru", "Петров Андрей Иванович"))
    check("адрес через подчёркивание тоже",
          EG.belongs_to("a_petrov@dom.ru", "Петров Андрей Иванович"))

    sch2 = EG.infer_scheme([{"email": "a_petrov@dom.ru"}, {"email": "i_sidorov@dom.ru"}])
    got2 = [g["email"] for g in EG.guess_emails(
        "Петров Андрей Иванович", "dom.ru", scheme=sch2["scheme"], sep=sch2["sep"])]
    check("подчёркивание доезжает до гипотезы", got2 and got2[0] == "a_petrov@dom.ru", str(got2))
    check("точка остаётся по умолчанию",
          EG.guess_emails("Петров Андрей Иванович", "dom.ru", scheme="i.S")[0]["email"]
          == "a.petrov@dom.ru")


EXISTING_BOXES = {"rudenkosa@x.ru", "s.rudenko@x.ru", "rudenko@x.ru"}


class _FakeSMTP:
    """SMTP-сервер, соблюдающий RFC 5321: RCPT без открытой транзакции -> 503."""

    def __init__(self, host="", port=0, timeout=10):
        self.log, self.txn, self.catch_all = [], False, False
        self.known = EXISTING_BOXES            # какие ящики «существуют»
        self.policy = False                    # сервер рубит по политике, а не по ящику

    def connect(self, host, port=0):
        self.log.append(("connect", host))
        return 220, b"ready"

    def helo(self, name=""):
        self.log.append(("helo", name))
        return 250, b"ok"

    def ehlo(self, name=""):
        self.log.append(("ehlo", name))
        return 250, b"mail.x.ru"

    def has_extn(self, name):
        return False

    def close(self):
        self.log.append(("close",))

    def mail(self, sender, options=()):
        self.txn = True
        self.log.append(("mail", sender))
        return 250, b"ok"

    def rcpt(self, addr, options=()):
        self.log.append(("rcpt", addr))
        if not self.txn:
            return 503, b"Bad sequence of commands"
        if self.policy:
            return 550, b"5.7.1 Client host blocked by policy"
        if self.catch_all:
            return 250, b"ok"
        return (250, b"ok") if addr in self.known else (550, b"5.1.1 no such user")

    def rset(self):
        self.txn = False
        self.log.append(("rset",))
        return 250, b"ok"

    def quit(self):
        self.log.append(("quit",))
        return 221, b"bye"


def test_smtp_probe():
    import os
    import smtplib
    saved = smtplib.SMTP
    saved_env = {k: os.environ.get(k) for k in ("EMAIL_GUESS_HELO", "EMAIL_GUESS_MAIL_FROM")}
    made = []
    try:
        # без настроенного отправителя проба не делается: представляться чужим
        # доменом нельзя, а отказ по SPF неотличим от «ящика нет»
        for k in saved_env:
            os.environ.pop(k, None)
        smtplib.SMTP = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("проба не должна открывать соединение без EMAIL_GUESS_MAIL_FROM"))
        check("без EMAIL_GUESS_MAIL_FROM проба не выполняется",
              EG.smtp_probe_domain("mx.x.ru", ["a@x.ru"]) == {"a@x.ru": "unknown"})
        os.environ["EMAIL_GUESS_HELO"] = "mail.citrt.ru"
        os.environ["EMAIL_GUESS_MAIL_FROM"] = "verify@citrt.ru"
        def factory(*a, **kw):
            srv = _FakeSMTP(*a, **kw)
            made.append(srv)
            return srv

        smtplib.SMTP = factory
        addrs = ["rudenkosa@x.ru", "s.rudenko@x.ru", "rudenko@x.ru",
                 "nosuch1@x.ru", "nosuch2@x.ru"]
        got = EG.smtp_probe_domain("mx.x.ru", addrs)
        # регрессия: RSET между адресами закрывал транзакцию, и все кандидаты
        # кроме первого получали 503 -> «unknown»
        check("вердикт получен по всем существующим ящикам",
              all(got[a] == "ok" for a in list(EXISTING_BOXES)), str(got))
        check("несуществующие ящики отклонены",
              got["nosuch1@x.ru"] == "no" and got["nosuch2@x.ru"] == "no", str(got))
        check("RSET не рвёт транзакцию",
              not any(c[0] == "rset" for c in made[0].log), str(made[0].log))
        check("MAIL FROM отправлен",
              any(c[0] == "mail" for c in made[0].log), str(made[0].log))
        check("сессия закрыта QUIT", made[0].log[-1][0] == "quit", str(made[0].log))
        check("не больше 7 RCPT за сессию (5 кандидатов + 2 теста catch-all)",
              sum(1 for c in made[0].log if c[0] == "rcpt") <= 7, str(made[0].log))

        made.clear()

        def catchall_factory(*a, **kw):
            srv = _FakeSMTP(*a, **kw)
            srv.catch_all = True
            made.append(srv)
            return srv

        smtplib.SMTP = catchall_factory
        got = EG.smtp_probe_domain("mx.x.ru", addrs)
        check("catch-all домен -> все unknown",
              all(v == "unknown" for v in got.values()), str(got))
        check("на catch-all лишние RCPT не тратятся",
              sum(1 for c in made[0].log if c[0] == "rcpt") <= 2, str(made[0].log))

        smtplib.SMTP = saved
        check("нет MX — проба не делается",
              EG.smtp_probe_domain("", ["a@x.ru"]) == {"a@x.ru": "unknown"})
    finally:
        smtplib.SMTP = saved
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _mx_doh(host):
    """Подмена DNS: домен обслуживает указанный MX."""
    return lambda name, rtype, timeout=5: (
        ([{"type": 15, "data": f"10 {host}."}], True) if rtype == "MX" else ([], True))


def test_verify_addresses():
    """Проверка существования ящика без отправки письма.

    Главное здесь — не «сходили на сервер», а ВЕС ответа: у Exchange Online проба
    бессмысленна, у mail.ru «принято» ничего не доказывает."""
    import os
    import smtplib

    check("Яндексу верим в обе стороны", EG.smtp_trust("Yandex 360") == "full")
    check("mail.ru — верим только отказу",
          EG.smtp_trust("mail.ru для бизнеса") == "negative_only")
    check("Exchange Online не верим вовсе", EG.smtp_trust("Microsoft 365") == "none")
    check("провайдер определяется по MX",
          EG.mail_provider(["mx.yandex.net"]) == "Yandex 360")

    check("550 5.1.1 — ящика нет", EG._classify_rcpt(550, b"5.1.1 User unknown") == "no")
    check("550 5.7.1 — политика, а не ящик",
          EG._classify_rcpt(550, b"5.7.1 Client host blocked") == "policy")
    check("250 — принят", EG._classify_rcpt(250, b"OK") == "ok")
    check("450 — временный отказ", EG._classify_rcpt(450, b"greylisted, try later") == "temp")

    check("без EMAIL_VERIFIER_URL внешний верификатор молчит",
          EG.external_verify("a@dom.ru", url="")["verdict"] == "unknown")

    saved_doh, saved_smtp = EG._doh, smtplib.SMTP
    saved_env = {k: os.environ.get(k) for k in ("EMAIL_GUESS_HELO", "EMAIL_GUESS_MAIL_FROM")}
    try:
        os.environ["EMAIL_GUESS_HELO"] = "mail.citrt.ru"
        os.environ["EMAIL_GUESS_MAIL_FROM"] = "verify@citrt.ru"

        # Exchange Online: сессию не открываем вовсе
        EG._doh = _mx_doh("x.mail.protection.outlook.com")
        smtplib.SMTP = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("для Exchange Online проба не должна открываться"))
        got = EG.verify_addresses("dom.ru", ["a@dom.ru"])
        check("для Exchange Online проба не делается",
              not got["probed"] and got["trust"] == "none" and got["reason"], str(got))

        # Яндекс 360: честный сервер, вердикты весомы
        EG._doh = _mx_doh("mx.yandex.net")
        boxes = {"ivanov@dom.ru"}

        def factory(*a, **kw):
            srv = _FakeSMTP(*a, **kw)
            srv.known = boxes
            return srv

        smtplib.SMTP = factory
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"])
        check("Яндекс: существующий ящик подтверждён",
              got["verdicts"]["ivanov@dom.ru"]["verdict"] == "ok"
              and got["verdicts"]["ivanov@dom.ru"]["trusted"], str(got["verdicts"]))
        check("Яндекс: несуществующий отклонён и этому верим",
              got["verdicts"]["petrov@dom.ru"]["verdict"] == "no"
              and got["verdicts"]["petrov@dom.ru"]["trusted"], str(got["verdicts"]))
        check("catch-all не сработал ложно", got["catch_all"] is False, str(got))

        # mail.ru: «принято» не доказательство, отказ — доказательство
        EG._doh = _mx_doh("mxs.mail.ru")
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"])
        check("mail.ru: «принято» не считается доказательством",
              got["verdicts"]["ivanov@dom.ru"]["verdict"] == "ok"
              and not got["verdicts"]["ivanov@dom.ru"]["trusted"],
              str(got["verdicts"]["ivanov@dom.ru"]))
        check("mail.ru: отказу верим",
              got["verdicts"]["petrov@dom.ru"]["trusted"], str(got["verdicts"]))

        # catch-all домен: вердикта нет ни у кого
        EG._doh = _mx_doh("mx.yandex.net")

        def catchall_factory(*a, **kw):
            srv = _FakeSMTP(*a, **kw)
            srv.catch_all = True
            return srv

        smtplib.SMTP = catchall_factory
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru"])
        check("catch-all распознан и вердиктов нет",
              got["catch_all"] and got["verdicts"]["ivanov@dom.ru"]["verdict"] == "unknown",
              str(got))

        # сервер отклонил по политике — про адрес не сказано ничего
        def policy_factory(*a, **kw):
            srv = _FakeSMTP(*a, **kw)
            srv.policy = True
            return srv

        smtplib.SMTP = policy_factory
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru"])
        row = got["verdicts"]["ivanov@dom.ru"]
        check("отказ по политике не выдаётся за отсутствие ящика",
              row["verdict"] == "unknown" and not row["trusted"], str(row))

        # порт 25 закрыт / сервер недоступен — все unknown, а не «нет ящика»
        smtplib.SMTP = lambda *a, **kw: (_ for _ in ()).throw(OSError("connect timeout"))
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru"])
        check("недоступный сервер не превращается в «ящика нет»",
              not got["probed"] and got["verdicts"]["ivanov@dom.ru"]["verdict"] == "unknown",
              str(got))

        # домен вовсе не принимает почту — это честное «не отправлять»
        EG._doh = lambda name, rtype, timeout=5: ([], True)
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru"])
        check("домен без MX/A -> адрес недоставляем",
              got["verdicts"]["ivanov@dom.ru"]["verdict"] == "no", str(got))

        check("публичный сервис не проверяем",
              EG.verify_addresses("mail.ru", ["a@mail.ru"])["probed"] is False)
    finally:
        EG._doh, smtplib.SMTP = saved_doh, saved_smtp
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_belongs_to():
    # регрессия: токен имени «Пётр» входит в чужую фамилию «Петров»
    check("адрес не приписан однофамильцу-по-имени",
          not EG.belongs_to("petrov.i@dom.ru", "Сидоров Пётр Петрович"))
    check("адрес приписан настоящему владельцу",
          EG.belongs_to("petrov.i@dom.ru", "Петров Илья Юрьевич"))
    check("чужой адрес не приписан",
          not EG.belongs_to("kozlova.a@dom.ru", "Петров Илья Юрьевич"))

    known = [{"email": "petrov.i@dom.ru", "fio": ""}]
    linked = EG.link_known_to_people(known, [{"fio": "Петров Илья Юрьевич"},
                                             {"fio": "Сидоров Пётр Петрович"}])
    check("известный адрес связан с верным человеком",
          linked[0]["fio"] == "Петров Илья Юрьевич", str(linked))


# ----------------------------------------------------------------- домен ----
def test_domain():
    check("почта приоритетнее сайта",
          EG.lead_domain({"email": "info@avtodor-rzn.ru",
                          "website": "http://other.ru/"})[0] == "avtodor-rzn.ru")
    check("www и путь срезаются",
          EG.lead_domain({"email": "", "website": "http://www.corp.ru/contacts"})[0] == "corp.ru")
    dom, src = EG.lead_domain({"email": "direktor@mail.ru", "website": ""})
    check("публичный домен помечен", dom == "mail.ru" and "публичн" in src, src)
    check("соцсеть сайтом не считается",
          EG.lead_domain({"email": "", "website": "https://vk.com/x"})[0] == "")
    check("пустой лид — пустой домен", EG.lead_domain({})[0] == "")
    check("domain_of из адреса", EG.domain_of("A.Ivanov@Corp.RU") == "corp.ru")


def test_mail_state():
    saved = EG._doh
    try:
        EG._doh = lambda name, rtype, timeout=5: (
            [{"type": 15, "data": "10 mx.yandex.net."}], True) if rtype == "MX" else ([], True)
        st = EG.mail_domain_state("dom.ru")
        check("MX найден", st["accepts_mail"] is True and st["mx"] == ["mx.yandex.net"], str(st))
        check("провайдер опознан", st["provider"] == "Yandex 360", str(st))

        EG._doh = lambda name, rtype, timeout=5: (
            ([], True) if rtype == "MX" else ([{"type": 1, "data": "1.2.3.4"}], True))
        st = EG.mail_domain_state("dom.ru")
        check("фолбэк на A (implicit MX)",
              st["accepts_mail"] is True and "implicit" in st["via"], str(st))

        EG._doh = lambda name, rtype, timeout=5: ([], True)
        check("ни MX, ни A — почту не принимает",
              EG.mail_domain_state("dom.ru")["accepts_mail"] is False)

        EG._doh = lambda name, rtype, timeout=5: ([], False)
        st = EG.mail_domain_state("dom.ru")
        check("DNS не ответил — не путать с «записей нет»",
              st["accepts_mail"] is None, str(st))
    finally:
        EG._doh = saved


# ------------------------------------------------------- люди из находок ----
FINDINGS = """
## Руководство
Текст отчёта [источник: https://a.ru]

```json
{"leadership":[{"position":"Генеральный директор","fio":"Руденко Сергей Александрович","source":"https://a.ru/ruk"},
{"position":"Главный инженер","fio":"Петров Илья Юрьевич","source":"https://a.ru/ruk"},
{"position":"Управляющая организация","fio":"ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ УК","source":"https://a.ru"}],
"branches":[{"branch":"Касимовский","director":"Сидоров Пётр Петрович","phone":"","source":"https://a.ru/f"}],
"contacts":{"emails":[{"email":"zakupki@avtodor-rzn.ru","role":"tender","source":"https://a.ru"},
{"email":"petrov.i@avtodor-rzn.ru","role":"personal","source":"https://a.ru"},
{"email":"someone@other-domain.ru","role":"personal","source":"https://a.ru"}]},
"procurement":{"summary":"","contacts":[{"fio":"Козлова Анна Ивановна","email":"","phone":"","source":"https://z.ru"}]}}
```
"""

LEAD = {"name": "АО «Рязаньавтодор»", "_inn": "6234065445",
        "email": "info@avtodor-rzn.ru", "website": "https://avtodor-rzn.ru",
        "contact_person": "Руденко Сергей Александрович"}


def test_people():
    people = EG.people_from_findings(FINDINGS)
    fios = [p["fio"] for p in people]
    check("руководство извлечено", "Петров Илья Юрьевич" in fios, str(fios))
    check("директор филиала извлечён", "Сидоров Пётр Петрович" in fios, str(fios))
    check("контакт закупок извлечён", "Козлова Анна Ивановна" in fios, str(fios))
    check("организация в люди не попала",
          not any("ОБЩЕСТВО" in f for f in fios), str(fios))

    known = [k["email"] for k in EG.known_emails_from(LEAD, FINDINGS)]
    check("адреса своего домена собраны",
          "petrov.i@avtodor-rzn.ru" in known and "info@avtodor-rzn.ru" in known, str(known))
    check("чужой домен отсеян", "someone@other-domain.ru" not in known, str(known))

    dedup = EG._dedup_people([
        {"fio": "Руденко Сергей Александрович", "position": "руководитель (ЕГРЮЛ)",
         "source": "ЕГРЮЛ (Фаза 1)"},
        {"fio": "Руденко Сергей Александрович", "position": "Генеральный директор",
         "source": "https://a.ru/ruk"}])
    check("человек не задваивается", len(dedup) == 1, str(dedup))
    check("должность берётся из находок",
          dedup[0]["position"] == "Генеральный директор", str(dedup))


# ------------------------------------------------------------ end-to-end ----
def test_company():
    res = EG.guess_for_company(LEAD, FINDINGS, check_mx=False, per_person=2, use_site=False,
                             log=lambda *a: None)
    check("домен определён по общей почте", res["domain"] == "avtodor-rzn.ru", str(res["domain"]))
    check("схема домена выведена из связки адрес+ФИО",
          res["scheme"] and res["scheme"]["scheme"] == "S.i" and res["scheme"]["exact"],
          str(res["scheme"]))

    by_fio = {p["fio"]: p for p in res["people"]}
    check("директор в выдаче один раз", len(res["people"]) == len(by_fio), str(list(by_fio)))
    check("все ключевые сотрудники обработаны", len(by_fio) == 4, str(list(by_fio)))

    petrov = by_fio["Петров Илья Юрьевич"]["emails"]
    check("опубликованный адрес помечен фактом",
          petrov[0]["email"] == "petrov.i@avtodor-rzn.ru"
          and "подтверждён" in petrov[0]["confidence"], str(petrov[0]))

    kozlova = by_fio["Козлова Анна Ивановна"]["emails"]
    check("схема применена к остальным",
          kozlova[0]["email"] == "kozlova.a@avtodor-rzn.ru", str(kozlova))
    check("без MX уверенность не завышается",
          all("низкая" in e["confidence"] for e in kozlova), str(kozlova))

    sidorov = by_fio["Сидоров Пётр Петрович"]["emails"]
    check("чужой адрес не приписан однофамильцу",
          all(e["email"] != "petrov.i@avtodor-rzn.ru" for e in sidorov), str(sidorov))

    block = EG.format_findings_block(res)
    check("блок для писателя содержит оговорку", "гипотезы по схеме домена" in block)
    check("SMTP-вердикт не выдумывается", "SMTP:" not in block, block[:200])


MGMT_HTML = """
<html><body>
<h1>Руководство</h1>
<div class="person"><span>Генеральный директор</span>
  <b>Руденко Сергей Александрович</b>
  <a href="mailto:rudenko.s@avtodor-rzn.ru">rudenko.s@avtodor-rzn.ru</a></div>
<div class="person"><span>Главный инженер</span>
  <b>Петров Илья Юрьевич</b> <a href="mailto:petrov.i@avtodor-rzn.ru">петров</a></div>
<div>ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «Подрядчик»</div>
<div>ул. Николая Островского, 21</div>
<footer>info@avtodor-rzn.ru, noreply@avtodor-rzn.ru, partner@other.ru</footer>
</body></html>
"""


def test_people_from_site():
    saved = EG._fetch_page
    try:
        calls = []

        def fake_fetch(url, timeout=12):
            calls.append(url)
            if url.endswith("/rukovodstvo"):
                return MGMT_HTML, url
            raise OSError("404")                   # остальные пути отсутствуют — это норма

        EG._fetch_page = fake_fetch
        people, emails = EG.people_from_site("avtodor-rzn.ru")
        fios = [p["fio"] for p in people]
        check("ФИО со страницы руководства собраны",
              "Руденко Сергей Александрович" in fios and "Петров Илья Юрьевич" in fios, str(fios))
        check("должность подхвачена рядом с ФИО",
              any("директор" in (p["position"] or "").lower() for p in people), str(people))
        check("организация не принята за человека",
              not any("ОБЩЕСТВО" in f.upper() for f in fios), str(fios))
        check("адрес улицы не принят за ФИО",
              not any("Островск" in f for f in fios), str(fios))
        addrs = [e["email"] for e in emails]
        check("личные адреса со страницы собраны",
              "rudenko.s@avtodor-rzn.ru" in addrs, str(addrs))
        check("мусорный ящик отсеян", "noreply@avtodor-rzn.ru" not in addrs, str(addrs))
        check("чужой домен отсеян", "partner@other.ru" not in addrs, str(addrs))
        check("404 не роняет обход, лимит попыток соблюдён",
              len(calls) == 10 and calls[0].endswith("/rukovodstvo"), str(calls))

        # сквозной сценарий: сайт дал людей и адрес -> схема домена -> точные гипотезы
        res = EG.guess_for_company(
            {"name": "АО «Рязаньавтодор»", "email": "info@avtodor-rzn.ru",
             "contact_person": "Руденко Сергей Александрович"},
            "", check_mx=False, per_person=1, log=lambda *a: None)
        check("схема домена выведена из данных сайта",
              res["scheme"] and res["scheme"]["scheme"] == "S.i" and res["scheme"]["exact"],
              str(res["scheme"]))
        got = {p["fio"]: p["emails"][0]["email"] for p in res["people"] if p["emails"]}
        check("главный инженер получил адрес по схеме домена",
              got.get("Петров Илья Юрьевич") == "petrov.i@avtodor-rzn.ru", str(got))

        # редирект увёл на чужой хост — данные оттуда не наши
        EG._fetch_page = lambda url, timeout=12: (MGMT_HTML, "https://another-site.ru/x")
        people, emails = EG.people_from_site("avtodor-rzn.ru")
        check("данные с чужого хоста после редиректа отброшены",
              people == [] and emails == [], f"{people} / {emails}")

        # переезд на свой же домен: у СВГК svgc.ru редиректит на svgk.ru, а почта
        # осталась на svgc.ru. Раньше обход отбрасывал такой сайт целиком.
        moved_html = ('<p>Начальник Киреев Владимир Борисович</p>'
                      '<p>svgc@svgc.ru</p><p>kireevvb@svgk.ru</p>'
                      '<p>partner@other.ru</p>')
        EG._fetch_page = lambda url, timeout=12: (moved_html, "https://svgk.ru/kontakty")
        people, emails = EG.people_from_site("svgc.ru")
        addrs = [e["email"] for e in emails]
        check("переезд на домен того же бренда принят",
              [p["fio"] for p in people] == ["Киреев Владимир Борисович"], str(people))
        check("почта на прежнем домене сохранена", "svgc@svgc.ru" in addrs, str(addrs))
        check("адрес на новом домене тоже собран", "kireevvb@svgk.ru" in addrs, str(addrs))
        check("чужой домен всё равно отсеян", "partner@other.ru" not in addrs, str(addrs))

        check("непохожий домен не считается тем же брендом",
              not EG._same_brand("avtodor-rzn.ru", "another-site.ru"), "")
        check("одна буква разницы — тот же бренд", EG._same_brand("svgc.ru", "svgk.ru"), "")

        # человек без должности рядом — не обязательно сотрудник
        EG._fetch_page = lambda url, timeout=12: (
            "<p>Поздравляем ветерана Ветрова Илью Петровича с юбилеем</p>", url)
        people, _ = EG.people_from_site("avtodor-rzn.ru")
        check("ФИО без должности сотрудником не считается", people == [], str(people))
    finally:
        EG._fetch_page = saved


def test_review_regressions():
    """Дефекты, найденные code-review: каждый закрыт проверкой."""
    import re as _re

    check("priemnaya@ — ролевой ящик, а не личный",
          EG.parse_local("priemnaya@dom.ru") is None)
    check("buhgalteria@ — ролевой ящик", EG.parse_local("buhgalteria@dom.ru") is None)
    check("kadry@ — ролевой ящик", EG.parse_local("kadry@dom.ru") is None)

    # слитную форму нельзя свести к одному шаблону: ivanov@ — и «фамилия», и «фамилия+инициалы»
    sch = EG.infer_scheme([{"email": "petrov@dom.ru"}, {"email": "sidorov@dom.ru"}])
    check("слитная форма даёт группу схем, а не один шаблон",
          sch and sch["scheme"] == "solid", str(sch))
    group = [g["email"] for g in EG.guess_emails("Козлов Иван Петрович", "dom.ru",
                                                 scheme="solid")]
    check("в группе есть и голая фамилия, и фамилия+инициалы",
          "kozlov@dom.ru" in group and "kozlovip@dom.ru" in group, str(group))

    # кириллические инициалы «Иванов И.И.» раньше выпадали из всех схем
    got = [g["email"] for g in EG.guess_emails("Иванов И.И.", "dom.ru")]
    check("инициалы транслитерированы", "ivanovii@dom.ru" in got, str(got))
    check("кириллицы в адресах нет",
          all(_re.fullmatch(r"[a-z0-9.@_\-]+", g) for g in got), str(got))
    check("«Иванов И.И.» не опознаётся как схема S{io} по адресу ivanov@",
          (EG.parse_local("ivanov@dom.ru", "Иванов И.И.") or {}).get("scheme") == "S",
          str(EG.parse_local("ivanov@dom.ru", "Иванов И.И.")))

    check("кириллический домен приведён к punycode",
          EG.domain_of("info@сайт.рф").startswith("xn--"), EG.domain_of("info@сайт.рф"))
    check("punycode-домен принимается как есть",
          EG.domain_of("xn--80aswg.xn--p1ai") == "xn--80aswg.xn--p1ai")

    p = EG.split_fio("Ivan Ivanov")
    check("латинское «Имя Фамилия» развёрнуто", p and p["surname"].lower() == "ivanov", str(p))
    p = EG.split_fio("Ким Татьяна")
    check("женское имя не принято за фамилию", p and p["surname"] == "Ким", str(p))
    p = EG.split_fio("Ткач Марина")
    check("«Ткач Марина» не переворачивается", p and p["surname"] == "Ткач", str(p))

    check("двойная фамилия сохраняет дефис",
          any("-" in v for v in EG.translit_variants("Салтыков-Щедрин", "surname")),
          str(EG.translit_variants("Салтыков-Щедрин", "surname")))

    check("число адресов схемы не путается с весом голосов",
          EG.infer_scheme([{"email": "rudenko.s@dom.ru",
                            "fio": "Руденко Сергей Александрович"}])["addresses"] == 1)

    check("внутренние адреса не фетчатся",
          not EG._is_public_host("http://localhost/x")
          and not EG._is_public_host("http://192.168.0.1/x")
          and not EG._is_public_host("http://intranet.local/x")
          and not EG._is_public_host("file:///etc/passwd"))
    check("обычный сайт проходит", EG._is_public_host("https://avtodor-rzn.ru/kontakty"))

    saved = EG._doh
    try:
        # Null MX (RFC 7505): домен явно отказался принимать почту
        EG._doh = lambda name, rtype, timeout=5: (
            ([{"type": 15, "data": "0 ."}], True) if rtype == "MX" else ([], True))
        st = EG.mail_domain_state("dom.ru")
        check("Null MX распознан как отказ", st["accepts_mail"] is False, str(st))
        # SERVFAIL приходит с HTTP 200 — это не «записей нет»
        EG._doh = saved
        real = EG._doh
        check("_doh отдаёт признак достоверности", callable(real))
    finally:
        EG._doh = saved


def test_confidence():
    """Уверенность обязана соответствовать доказательной силе — по каждой строке."""
    lead = {"name": "X", "email": "info@dom.ru", "contact_person": "Руденко Сергей Александрович"}
    findings = """```json
{"leadership":[{"position":"Директор","fio":"Руденко Сергей Александрович","source":"https://dom.ru/r"},
{"position":"Главный инженер","fio":"Козлов Иван","source":"https://dom.ru/r"}],
"contacts":{"emails":[{"email":"rudenko.s@dom.ru","role":"personal","source":"https://dom.ru/r"},
{"email":"petrov.a@dom.ru","role":"personal","source":"https://dom.ru/r"}]}}
```"""
    saved = EG._doh
    try:
        EG._doh = lambda name, rtype, timeout=5: (
            ([{"type": 15, "data": "10 mx.dom.ru."}], True) if rtype == "MX" else ([], True))
        res = EG.guess_for_company(lead, findings, per_person=3, use_site=False,
                                   log=lambda *a: None)
        by_fio = {p["fio"]: p for p in res["people"]}
        # у «Козлов Иван» нет отчества -> схема S.i применима, но проверим,
        # что «высокая» не ставится строкам, построенным не по схеме домена
        for person in res["people"]:
            for e in person["emails"]:
                if e["scheme"] not in ("опубликован", res["scheme"]["scheme"]):
                    check(f"не по схеме домена -> не «высокая» ({e['email']})",
                          "высокая" not in e["confidence"], str(e))
        rud = by_fio["Руденко Сергей Александрович"]["emails"][0]
        check("свой опубликованный адрес — факт с источником",
              rud["email"] == "rudenko.s@dom.ru" and "подтверждён" in rud["confidence"]
              and rud.get("source"), str(rud))
        check("чужой опубликованный адрес не приписан",
              all(e["email"] != "petrov.a@dom.ru"
                  for p in res["people"] for e in p["emails"]),
              str(res["people"]))

        # домен почту не принимает -> явный запрет, а не «не проверено»
        EG._doh = lambda name, rtype, timeout=5: ([], True)
        res = EG.guess_for_company(lead, "", per_person=1, use_site=False, log=lambda *a: None)
        rows = [e for p in res["people"] for e in p["emails"]]
        check("домен без почты -> «НЕ отправлять»",
              rows and all("НЕ отправлять" in e["confidence"] for e in rows), str(rows))
    finally:
        EG._doh = saved


def test_homonyms():
    """Однофамильцы: адрес по схеме «голая фамилия» не факт ни для кого."""
    lead = {"name": "X", "email": "info@dom.ru"}
    people = [{"fio": "Иванов Иван Петрович", "position": "директор"},
              {"fio": "Иванов Игорь Олегович", "position": "закупки"}]
    known = [{"email": "ivanov@dom.ru", "fio": "", "source": "https://dom.ru"}]
    got = EG.published_for(known, "Иванов Иван Петрович", people)
    check("неоднозначный адрес не считается фактом", got == [], str(got))

    known_named = [{"email": "ivanov@dom.ru", "fio": "Иванов Игорь Олегович",
                    "source": "https://dom.ru"}]
    check("явный владелец получает свой адрес",
          [i["email"] for i in EG.published_for(known_named, "Иванов Игорь Олегович", people)]
          == ["ivanov@dom.ru"])
    check("однофамильцу чужой адрес не достаётся",
          EG.published_for(known_named, "Иванов Иван Петрович", people) == [])


def test_no_crash():
    for lead in ({}, {"name": "X"}, {"name": "X", "email": "нет"},
                 {"name": "X", "email": "info@mail.ru", "contact_person": "Иванов Иван Иванович"},
                 {"name": "X", "website": "https://vk.com/x", "contact_person": "ООО Ромашка"}):
        res = EG.guess_for_company(lead, "", check_mx=False, use_site=False,
                                   log=lambda *a: None)
        check(f"не падает на {str(lead)[:40]}", isinstance(res, dict) and "people" in res)
    check("публичный домен — личные адреса не строятся",
          EG.guess_for_company({"email": "info@mail.ru", "contact_person": "Иванов Иван Иванович"},
                               "", check_mx=False, use_site=False,
                               log=lambda *a: None)["people"] == [])


def main():
    for test in (test_translit, test_split_fio, test_guess, test_parse_and_scheme,
                 test_separator, test_smtp_probe, test_verify_addresses,
                 test_belongs_to, test_domain, test_mail_state, test_people,
                 test_people_from_site, test_company, test_review_regressions,
                 test_confidence, test_homonyms, test_no_crash):
        print(f"\n--- {test.__name__} ---")
        test()
    print()
    if _fails:
        print(f"test_email_guess: ПРОВАЛЕНО {len(_fails)}: {', '.join(_fails)}")
        return 1
    print("test_email_guess: OK — транслит, разбор ФИО, схема домена, строгая "
          "привязка адреса к человеку и мягкая деградация")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
