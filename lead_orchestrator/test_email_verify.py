# -*- coding: utf-8 -*-
r"""
Офлайн-тест email_verify — без сети, без реального SMTP.

Главный контракт, который нельзя потерять: «не смогли проверить» НЕ равно «ящика
нет». У самого Reacher `invalid` возвращается и при отказе сервера по адресату, и
при неудачном подключении — при закрытом порте 25 это пометило бы несуществующими
все адреса разом. Здесь такой случай обязан оставаться unknown.

Запуск: py test_email_verify.py
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
import email_verify as EV

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK ] {name}")
    else:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        _fails.append(name)


class FakeSMTP:
    """SMTP-сервер, отвечающий по RFC: RCPT без транзакции -> 503."""

    def __init__(self, host="", port=0, timeout=10):
        self.log, self.txn = [("connect", host, port)], False
        self.known = {"ivanov@dom.ru"}
        self.mode = "normal"                       # normal | catch_all | policy | greylist

    def connect(self, host, port=0):
        self.log.append(("connect", host, port))
        return 220, b"ready"

    def helo(self, name=""):
        self.log.append(("helo", name))
        return 250, b"ok"

    def ehlo(self, name=""):
        self.log.append(("ehlo", name))
        return 250, b"mail.dom.ru"

    def has_extn(self, name):
        return False                               # STARTTLS не предлагаем

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
        if self.mode == "catch_all":
            return 250, b"ok"
        if self.mode == "policy":
            return 550, b"5.7.1 Client host blocked"
        if self.mode == "greylist" and not addr.startswith("zz"):
            return 450, b"4.7.1 Greylisted, try again later"
        if addr in self.known:
            return 250, b"Accepted"
        if addr == "full@dom.ru":
            return 552, b"5.2.2 Mailbox full, over quota"
        if addr == "off@dom.ru":
            return 550, b"5.2.1 Mailbox disabled, account inactive"
        return 550, b"5.1.1 Mailbox does not exist"

    def rset(self):
        self.txn = False
        self.log.append(("rset",))
        return 250, b"ok"

    def quit(self):
        self.log.append(("quit",))
        return 221, b"bye"


def _mx(host="mail.dom.ru"):
    return lambda name, rtype, timeout=5: (
        ([{"type": 15, "data": f"10 {host}."}], True) if rtype == "MX" else ([], True))


def _with_smtp(factory):
    """Подменить smtplib.SMTP и переменные окружения на время проверки."""
    import os
    import smtplib
    saved = (smtplib.SMTP, os.environ.get("EMAIL_GUESS_HELO"),
             os.environ.get("EMAIL_GUESS_MAIL_FROM"))
    smtplib.SMTP = factory
    os.environ["EMAIL_GUESS_HELO"] = "mail.citrt.ru"
    os.environ["EMAIL_GUESS_MAIL_FROM"] = "verify@citrt.ru"
    return saved


def _restore(saved):
    import os
    import smtplib
    smtplib.SMTP, helo, mail_from = saved
    for key, val in (("EMAIL_GUESS_HELO", helo), ("EMAIL_GUESS_MAIL_FROM", mail_from)):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


# --------------------------------------------------------------- синтаксис ----
def test_syntax():
    ok = EV.check_syntax("Ivanov.A@Company.RU")
    check("валидный адрес разобран",
          ok["is_valid_syntax"] and ok["domain"] == "company.ru"
          and ok["username"] == "Ivanov.A", str(ok))
    for bad in ("", "no-at-sign", "@dom.ru", "user@", "user@dom", "a..b@dom.ru",
                "user@@dom.ru", "user name@dom.ru"):
        check(f"отвергнут некорректный адрес: {bad!r}",
              not EV.check_syntax(bad)["is_valid_syntax"])


def test_misc():
    check("ролевой ящик распознан", EV.check_misc("info@dom.ru")["is_role_account"])
    check("русский ролевой ящик распознан",
          EV.check_misc("priemnaya@dom.ru")["is_role_account"])
    check("личный ящик не ролевой", not EV.check_misc("ivanov@dom.ru")["is_role_account"])
    check("одноразовый домен распознан",
          EV.check_misc("a@mailinator.com")["is_disposable"])
    check("публичный сервис помечен", EV.check_misc("a@mail.ru")["is_b2c"])


# ------------------------------------------------------------------- SMTP ----
def test_smtp_session():
    made = []

    def factory(*a, **kw):
        srv = FakeSMTP(*a, **kw)
        made.append(srv)
        return srv

    saved = _with_smtp(factory)
    try:
        rows = EV.check_smtp_many("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"],
                                  mx_hosts=["mail.dom.ru"])
        check("существующий ящик подтверждён", rows["ivanov@dom.ru"]["is_deliverable"])
        check("несуществующий отклонён", not rows["petrov@dom.ru"]["is_deliverable"])
        check("подключение зафиксировано", rows["ivanov@dom.ru"]["can_connect_smtp"])
        check("сессия одна на домен", len(made) == 1, str(len(made)))
        check("RSET не рвёт транзакцию",
              not any(c[0] == "rset" for c in made[0].log), str(made[0].log))
        check("catch-all проверен двумя случайными адресами",
              sum(1 for c in made[0].log if c[0] == "rcpt" and c[1].startswith("zz")) == 2,
              str(made[0].log))
        check("DATA не отправляется",
              not any(c[0] == "data" for c in made[0].log), str(made[0].log))
        check("сессия закрыта", made[0].log[-1][0] == "quit", str(made[0].log))

        made.clear()
        rows = EV.check_smtp_many("dom.ru", ["full@dom.ru", "off@dom.ru"],
                                  mx_hosts=["mail.dom.ru"])
        check("переполненный ящик распознан", rows["full@dom.ru"]["has_full_inbox"],
              str(rows["full@dom.ru"]))
        check("отключённый ящик распознан", rows["off@dom.ru"]["is_disabled"],
              str(rows["off@dom.ru"]))

        made.clear()

        def catchall(*a, **kw):
            srv = FakeSMTP(*a, **kw)
            srv.mode = "catch_all"
            made.append(srv)
            return srv

        _restore(saved)
        saved = _with_smtp(catchall)
        rows = EV.check_smtp_many("dom.ru", ["ivanov@dom.ru"], mx_hosts=["mail.dom.ru"])
        check("catch-all распознан", rows["ivanov@dom.ru"]["is_catch_all"])
        check("на catch-all кандидаты не проверяются",
              sum(1 for c in made[0].log if c[0] == "rcpt") == 2, str(made[0].log))

        _restore(saved)

        def policy(*a, **kw):
            srv = FakeSMTP(*a, **kw)
            srv.mode = "policy"
            return srv

        saved = _with_smtp(policy)
        rows = EV.check_smtp_many("dom.ru", ["ivanov@dom.ru"], mx_hosts=["mail.dom.ru"])
        check("отказ по политике помечен отдельно",
              rows["ivanov@dom.ru"]["policy_blocked"]
              and not rows["ivanov@dom.ru"]["is_deliverable"], str(rows["ivanov@dom.ru"]))

        _restore(saved)

        # Лимит адресов на домен: дефолт 5 (вежливость к чужому серверу при
        # переборе гипотез), но при сверке готового списка его поднимают — иначе
        # хвост домена молча остаётся неопрошенным.
        import os as _os
        made.clear()
        saved = _with_smtp(factory)
        many = [f"user{i}@dom.ru" for i in range(8)]
        rows = EV.check_smtp_many("dom.ru", many, mx_hosts=["mail.dom.ru"])
        probed = sum(1 for c in made[0].log if c[0] == "rcpt" and not c[1].startswith("zz"))
        check("по умолчанию не больше 5 адресов на домен", probed == 5, str(probed))
        check("остальные помечены как непроверенные",
              not rows.get("user7@dom.ru", {"checked": False})["checked"]
              if "user7@dom.ru" in rows else True, str(list(rows)))

        made.clear()
        _os.environ["EMAIL_VERIFY_MAX_PER_DOMAIN"] = "8"
        try:
            EV.check_smtp_many("dom.ru", many, mx_hosts=["mail.dom.ru"])
            probed = sum(1 for c in made[0].log if c[0] == "rcpt" and not c[1].startswith("zz"))
            check("лимит поднимается переменной окружения", probed == 8, str(probed))
        finally:
            _os.environ.pop("EMAIL_VERIFY_MAX_PER_DOMAIN", None)
        _restore(saved)

        # Временный отказ обрывает цикл, чтобы не давить на чужой сервер. Адреса,
        # до которых очередь не дошла, ОБЯЗАНЫ остаться непроверенными: раньше им
        # доставалось checked=True, выставленный скопом до цикла, и _reachable
        # объявлял их несуществующими. На живом tatar.ru так был «похоронен»
        # реальный ящик — сервер ответил 450 на первый адрес, а второй, который
        # никто не спрашивал, получил вердикт «такого ящика нет».
        def greylist(*a, **kw):
            srv = FakeSMTP(*a, **kw)
            srv.mode = "greylist"
            return srv

        saved = _with_smtp(greylist)
        rows = EV.check_smtp_many("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"],
                                  mx_hosts=["mail.dom.ru"])
        first, second = rows["ivanov@dom.ru"], rows["petrov@dom.ru"]
        check("временный отказ: первый адрес не проверен", not first["checked"],
              str(first))
        check("временный отказ: остальные адреса тоже не проверены",
              not second["checked"], str(second))
        state, why = EV._reachable(second, EV.check_misc("petrov@dom.ru"),
                                   {"_state": True, "accepts_mail": True})
        check("непроверенный адрес не выдаётся за несуществующий",
              state == "unknown", f"{state}: {why}")
        check("у непроверенного адреса объяснена причина",
              "прервана" in second["reason"], str(second))

        _restore(saved)
        saved = _with_smtp(lambda *a, **kw: (_ for _ in ()).throw(OSError("timed out")))
        rows = EV.check_smtp_many("dom.ru", ["ivanov@dom.ru"], mx_hosts=["mail.dom.ru"])
        check("недоступный сервер: проверка не состоялась",
              not rows["ivanov@dom.ru"]["checked"]
              and not rows["ivanov@dom.ru"]["can_connect_smtp"], str(rows["ivanov@dom.ru"]))
    finally:
        _restore(saved)


def test_no_credentials():
    import os
    saved = {k: os.environ.get(k) for k in ("EMAIL_GUESS_HELO", "EMAIL_GUESS_MAIL_FROM")}
    import smtplib
    saved_smtp = smtplib.SMTP
    try:
        for k in saved:
            os.environ.pop(k, None)
        smtplib.SMTP = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("без своего HELO/MAIL FROM соединение открывать нельзя"))
        rows = EV.check_smtp_many("dom.ru", ["a@dom.ru"], mx_hosts=["mail.dom.ru"])
        check("без EMAIL_GUESS_HELO/MAIL_FROM проба не делается",
              not rows["a@dom.ru"]["checked"] and "EMAIL_GUESS_HELO" in rows["a@dom.ru"]["reason"],
              str(rows["a@dom.ru"]))
    finally:
        smtplib.SMTP = saved_smtp
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val


# ------------------------------------------------------- итоговый вердикт ----
def test_reachable():
    """_reachable — сердце модуля: здесь Reacher склеивает разные случаи, мы нет."""
    mx_ok, mx_no = {"_state": True}, {"_state": False}
    plain = {"is_disposable": False, "is_role_account": False, "is_b2c": False}
    role = dict(plain, is_role_account=True)

    def smtp(**kw):
        base = {"can_connect_smtp": True, "is_deliverable": False, "is_catch_all": False,
                "has_full_inbox": False, "is_disabled": False, "policy_blocked": False,
                "checked": True, "reason": ""}
        base.update(kw)
        return base

    check("ящик есть -> safe",
          EV._reachable(smtp(is_deliverable=True), plain, mx_ok)[0] == "safe")
    check("ролевой ящик -> risky",
          EV._reachable(smtp(is_deliverable=True), role, mx_ok)[0] == "risky")
    check("catch-all + принят -> risky",
          EV._reachable(smtp(is_deliverable=True, is_catch_all=True), plain, mx_ok)[0] == "risky")
    check("отказ по адресату -> invalid",
          EV._reachable(smtp(), plain, mx_ok)[0] == "invalid")
    check("ящик отключён -> invalid",
          EV._reachable(smtp(is_disabled=True), plain, mx_ok)[0] == "invalid")
    check("домен не принимает почту -> invalid",
          EV._reachable(smtp(), plain, mx_no)[0] == "invalid")

    # ключевые отличия от оригинального Reacher
    check("нет коннекта -> unknown, а НЕ invalid",
          EV._reachable(smtp(can_connect_smtp=False, checked=False), plain, mx_ok)[0]
          == "unknown")
    check("отказ по политике -> unknown, а НЕ invalid",
          EV._reachable(smtp(policy_blocked=True), plain, mx_ok)[0] == "unknown")
    check("catch-all без ответа по адресу -> unknown",
          EV._reachable(smtp(is_catch_all=True), plain, mx_ok)[0] == "unknown")


def test_to_verdict():
    def result(state, **smtp_kw):
        smtp = {"can_connect_smtp": True, "is_deliverable": False, "is_catch_all": False,
                "policy_blocked": False}
        smtp.update(smtp_kw)
        return {"is_reachable": state, "smtp": smtp, "mx": {"accepts_mail": True}}

    check("safe -> ok", EV.to_verdict(result("safe", is_deliverable=True)) == "ok")
    check("risky с доставкой -> ok",
          EV.to_verdict(result("risky", is_deliverable=True)) == "ok")
    check("risky на catch-all -> unknown",
          EV.to_verdict(result("risky", is_deliverable=True, is_catch_all=True)) == "unknown")
    check("invalid с ответом сервера -> no", EV.to_verdict(result("invalid")) == "no")
    check("invalid без коннекта -> unknown (порт 25 закрыт, а не ящика нет)",
          EV.to_verdict(result("invalid", can_connect_smtp=False)) == "unknown")
    check("invalid по политике -> unknown",
          EV.to_verdict(result("invalid", policy_blocked=True)) == "unknown")
    check("unknown -> unknown", EV.to_verdict(result("unknown")) == "unknown")


# --------------------------------------------------------------- сборка ----
def test_check_emails():
    saved_doh = EG._doh
    saved = _with_smtp(lambda *a, **kw: FakeSMTP(*a, **kw))
    try:
        EG._doh = _mx("mail.dom.ru")
        res = EV.check_emails(["ivanov@dom.ru", "petrov@dom.ru", "кривой-адрес"])
        by_input = {r["input"]: r for r in res}
        check("результат на каждый адрес", len(res) == 3, str(len(res)))
        check("формат совместим с Reacher",
              all(k in res[0] for k in ("input", "is_reachable", "syntax", "mx", "smtp", "misc")),
              str(list(res[0])))
        check("живой ящик -> safe", by_input["ivanov@dom.ru"]["is_reachable"] == "safe",
              str(by_input["ivanov@dom.ru"]["reason"]))
        check("мёртвый ящик -> invalid", by_input["petrov@dom.ru"]["is_reachable"] == "invalid")
        check("кривой адрес -> invalid без обращения к сети",
              by_input["кривой-адрес"]["is_reachable"] == "invalid")
        check("проставлено доверие провайдеру", by_input["ivanov@dom.ru"]["trust"] in
              ("full", "negative_only", "medium", "none", "unknown"))

        # Exchange Online: сессию не открываем вовсе
        _restore(saved)
        saved = _with_smtp(lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("для Exchange Online соединение открывать не нужно")))
        EG._doh = _mx("dom-ru.mail.protection.outlook.com")
        res = EV.check_emails(["ivanov@dom.ru"])
        check("для Exchange Online SMTP не открывается",
              res[0]["is_reachable"] == "unknown" and not res[0]["smtp"]["checked"],
              str(res[0]["smtp"]["reason"]))

        # публичный сервис — личные ящики не проверяем
        EG._doh = _mx("mx.yandex.net")
        res = EV.check_emails(["someone@mail.ru"])
        check("публичный сервис не проверяется",
              not res[0]["smtp"]["checked"] and res[0]["misc"]["is_b2c"], str(res[0]))
    finally:
        EG._doh = saved_doh
        _restore(saved)


def test_guess_integration():
    """email_guess должен пользоваться этим же ядром, а не своей копией SMTP."""
    saved_doh = EG._doh
    saved = _with_smtp(lambda *a, **kw: FakeSMTP(*a, **kw))
    try:
        EG._doh = _mx("mx.yandex.net")
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"])
        check("email_guess получил вердикты через email_verify",
              got["verdicts"]["ivanov@dom.ru"]["verdict"] == "ok"
              and got["verdicts"]["petrov@dom.ru"]["verdict"] == "no", str(got["verdicts"]))
        check("вердикту Яндекса доверяем",
              got["verdicts"]["ivanov@dom.ru"]["trusted"], str(got["verdicts"]))
        check("старая обёртка smtp_probe_domain работает",
              EG.smtp_probe_domain("mail.dom.ru", ["ivanov@dom.ru"])["ivanov@dom.ru"] == "ok")

        # сервер незнакомого провайдера, отклонивший часть адресов, доказал
        # избирательность — его «принято» становится весомым
        EG._doh = _mx("mail.dom.ru")               # «свой сервер» -> доверие medium
        got = EG.verify_addresses("dom.ru", ["ivanov@dom.ru", "petrov@dom.ru"])
        check("избирательность сервера повышает вес «принято»",
              got["trust"] == "full" and got["verdicts"]["ivanov@dom.ru"]["trusted"],
              str(got["trust"]) + " " + str(got["verdicts"]["ivanov@dom.ru"]))
        got_all_ok = EG.verify_addresses("dom.ru", ["ivanov@dom.ru"])
        check("без единого отказа доверие не повышается",
              got_all_ok["trust"] == "medium"
              and not got_all_ok["verdicts"]["ivanov@dom.ru"]["trusted"],
              str(got_all_ok["trust"]))
    finally:
        EG._doh = saved_doh
        _restore(saved)


def main():
    for test in (test_syntax, test_misc, test_smtp_session, test_no_credentials,
                 test_reachable, test_to_verdict, test_check_emails, test_guess_integration):
        print(f"\n--- {test.__name__} ---")
        test()
    print()
    if _fails:
        print(f"test_email_verify: ПРОВАЛЕНО {len(_fails)}: {', '.join(_fails)}")
        return 1
    print("test_email_verify: OK — проверка ящика без отправки письма; «не смогли "
          "проверить» не выдаётся за «ящика нет»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
