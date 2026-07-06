# -*- coding: utf-8 -*-
r"""
Авторизованная сессия RusProfile через ПОСТОЯННЫЙ профиль Chrome (undetected_chromedriver).

Зачем: контакты (тел/email/сайт) на карточках RusProfile платные — у анонима
замаскированы плашкой «оформите профессиональный доступ». С залогиненным
аккаунтом (профессиональный доступ) они открываются и парсятся.

Профиль хранится в PROFILE_DIR -> логин сохраняется между запусками (cookie на
диске). Пароль НИГДЕ не хранится: пользователь логинится руками ОДИН раз в
режиме --login (включая SMS/капчу), дальше сессия переиспользуется.

Режимы:
  py rusprofile_session.py --login         # открыть окно, залогиниться вручную; ждёт и проверяет
  py rusprofile_session.py --check         # проверить, открыты ли контакты под текущим профилем
  (как модуль) RusProfileAuth().contacts_by_url("/id/123")  # спарсить контакты карточки
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import undetected_chromedriver as uc

from browser_util import chrome_major  # version_main под реально установленный Chrome

uc.Chrome.__del__ = lambda self: None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

PROFILE_DIR = r"C:\Users\abalb\.claude\skills\lead-finder\.rp_profile"
COOKIES_FILE = r"C:\Users\abalb\.claude\skills\lead-finder\.rp_cookies.json"
HOME = "https://www.rusprofile.ru/"
TEST_CARD = "https://www.rusprofile.ru/id/4464622"  # любая карточка для проверки масок
PAYWALL = "оформите профессиональный доступ"
_REAL_TEL = re.compile(r"\+7\s*\(?\d")            # реальный телефон (после +7 цифра, не ░)
_REAL_MAIL = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MASK = "░"


def log(m):
    print(m, flush=True)


class RusProfileAuth:
    def __init__(self, headless=False, profile_dir=PROFILE_DIR, offscreen=False):
        self.headless = headless
        self.offscreen = offscreen        # headed, но окно ЗА экраном (антибот проходит, окна не видно)
        self.profile_dir = profile_dir
        self.d = None

    def __enter__(self):
        opts = uc.ChromeOptions()
        opts.page_load_strategy = "eager"
        opts.add_argument(f"--user-data-dir={self.profile_dir}")
        if self.headless:
            opts.add_argument("--headless=new")
        elif self.offscreen:
            opts.add_argument("--window-position=-32000,-32000")  # окно за пределами экрана: не видно
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1320,950")
        _kw = dict(options=opts, headless=self.headless, use_subprocess=True)
        _vm = chrome_major()              # прибиваем версию uc-драйвера к Chrome
        if _vm:
            _kw["version_main"] = _vm
        self.d = uc.Chrome(**_kw)
        if self.offscreen and not self.headless:
            try:
                self.d.minimize_window()   # подстраховка: и за экраном, и свёрнуто
            except Exception:
                pass
        self.d.set_page_load_timeout(45)
        if os.path.exists(COOKIES_FILE):
            self.load_cookies()
        return self

    # ---- cookie-персистентность (вход один раз, дальше переиспользуем) ----
    def save_cookies(self, path=COOKIES_FILE):
        cookies = self.d.get_cookies()
        far = int(time.time()) + 60 * 60 * 24 * 180  # +180 дней session-cookie
        for c in cookies:
            if not c.get("expiry"):
                c["expiry"] = far
        json.dump(cookies, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        log(f"[cookies] сохранено {len(cookies)} cookie -> {path}")

    def load_cookies(self, path=COOKIES_FILE):
        try:
            cookies = json.load(open(path, encoding="utf-8"))
        except Exception:
            return False
        self.d.get(HOME)
        time.sleep(4)  # дать uc пройти Cloudflare-challenge
        ok = 0
        for c in cookies:
            c.pop("sameSite", None)  # selenium иногда падает на sameSite
            try:
                self.d.add_cookie(c)
                ok += 1
            except Exception:
                continue
        self.d.get(HOME)
        time.sleep(3)
        log(f"[cookies] загружено {ok}/{len(cookies)} cookie из {path}")
        return ok > 0

    def __exit__(self, *exc):
        if self.d is not None:
            try:
                self.d.quit()
            except Exception:
                pass
            self.d = None

    def _card_html(self, url):
        self.d.get(url)
        time.sleep(4)
        return self.d.page_source

    def contacts_unlocked(self, html=None):
        """Открыты ли контакты (не замаскированы, нет плашки и есть реальный телефон)."""
        html = html if html is not None else self._card_html(TEST_CARD)
        masked = PAYWALL in html or _MASK in html
        real = bool(_REAL_TEL.search(html))
        return real and not masked

    def login_and_wait(self, timeout=420, poll=12):
        """Открыть окно для ручного входа; ждать, пока контакты не откроются."""
        self.d.get(HOME)
        log("\n>>> В ОТКРЫВШЕМСЯ ОКНЕ Chrome войди в аккаунт RusProfile (кнопка «Войти»).")
        log(">>> Введи логин/пароль (+ SMS/капчу, если попросят). Окно НЕ закрывай.")
        log(f">>> Жду до {timeout//60} мин, проверяю каждые {poll}с, открылись ли контакты...\n")
        end = time.time() + timeout
        # отдельная вкладка под проверку, чтобы не мешать вкладке логина
        main = self.d.current_window_handle
        self.d.switch_to.new_window("tab")
        probe = self.d.current_window_handle
        try:
            while time.time() < end:
                time.sleep(poll)
                try:
                    self.d.switch_to.window(probe)
                    html = self._card_html(TEST_CARD)
                except Exception:
                    continue
                if self.contacts_unlocked(html):
                    self.save_cookies()  # сохранить сессию -> вход больше не нужен
                    log("[OK] Контакты ОТКРЫТЫ — аккаунт с профессиональным доступом. "
                        "Cookie сохранены: следующие прогоны идут БЕЗ логина.")
                    return "unlocked"
                # залогинен, но контакты всё ещё под маской -> тариф без контактов
                if PAYWALL not in html and _MASK not in html:
                    continue
            # таймаут
            html = self._card_html(TEST_CARD)
            if PAYWALL in html or _MASK in html:
                log("[!] Время вышло: контакты всё ещё замаскированы. Либо вход не завершён, "
                    "либо у аккаунта НЕТ платного «профессионального доступа» (контакты)." )
                return "locked"
            return "unknown"
        finally:
            try:
                self.d.switch_to.window(main)
            except Exception:
                pass

    _JS_CONTACTS = r"""
      var R={phones:[],emails:[],website:'',block:''};
      // сайт: ссылка рядом с меткой «Сайт»
      var lab=[...document.querySelectorAll('*')].find(function(e){return (e.textContent||'').trim()==='Сайт';});
      if(lab){var a=lab.parentElement && lab.parentElement.querySelector('a[href]'); if(a) R.website=a.href;}
      // телефоны/почты из tel:/mailto:
      document.querySelectorAll('a[href^="tel:"]').forEach(function(a){R.phones.push(a.getAttribute('href').replace('tel:',''));});
      document.querySelectorAll('a[href^="mailto:"]').forEach(function(a){R.emails.push(a.getAttribute('href').replace('mailto:',''));});
      // текст блока «Контакты» для добора регэкспом
      var c=[...document.querySelectorAll('*')].find(function(x){return /^Контакты/.test((x.innerText||'').trim()) && x.children.length<14;});
      R.block=c?c.innerText:'';
      return R;
    """

    def contacts_by_url(self, link):
        """Карточка RusProfile (/id/.. или полный URL) -> {phone, emails, website}."""
        url = link if link.startswith("http") else "https://www.rusprofile.ru" + link
        html = self._card_html(url)
        if PAYWALL in html or _MASK in html or not _REAL_TEL.search(html):
            return {}  # контакты закрыты под этим профилем
        R = self.d.execute_script(self._JS_CONTACTS) or {}
        block = R.get("block", "")
        tels = list(R.get("phones") or []) + re.findall(r"\+7[\s\d()\-]{8,16}", block)
        tels = [re.sub(r"\s+", " ", t).strip() for t in tels if re.search(r"\d", t)]
        mails = list(R.get("emails") or []) + _REAL_MAIL.findall(block)
        mails = [m for m in mails if not m.lower().endswith((".png", ".jpg", ".svg"))]
        site = (R.get("website") or "").strip()
        if not site:
            m = re.search(r"Сайт[\s\S]{0,30}?([a-zA-Zа-яё0-9.\-]+\.(?:ru|рф|com|su|org))", block)
            site = ("http://" + m.group(1)) if m else ""
        return {
            "phone": ", ".join(dict.fromkeys(tels))[:90],
            "emails": list(dict.fromkeys(m.lower() for m in mails)),
            "website": site,
        }

    def enrich_leads(self, leads, only_missing=True, log=log, checkpoint=None):
        """Заполнить лидам website/phone/email из карточки RusProfile (по _rusprofile_url).
        Контакты RusProfile бесплатны под твоим аккаунтом и БЕЗ суточного лимита.
        checkpoint — колбэк без аргументов, зовётся каждые 20 карточек: инкрементальное
        сохранение прогресса, чтобы обрыв Chrome не терял уже собранные контакты.
        В ответе locked=True — платная сессия протухла (контакты закрыты, нужен --login)."""
        try:
            from checko_enrich import _best_email
        except Exception:
            _best_email = lambda ems: (ems[0] if ems else None, "общая", False)
        if not self.contacts_unlocked():
            log("[RusProfile] контакты закрыты (сессия истекла?) — нужен повторный --login")
            return {"used": 0, "site": 0, "phone": 0, "email": 0, "locked": True}

        def need(l):
            url = l.get("_rusprofile_url") or (
                l.get("_revenue_source_url") if "/id/" in (l.get("_revenue_source_url") or "") else "")
            if not url:
                return None
            if only_missing and (l.get("website") or l.get("phone") or l.get("email")):
                return None
            return url

        def _ckpt():                         # чекпойнт опционален и НИКОГДА не должен ронять сбор
            if checkpoint:
                try:
                    checkpoint()
                except Exception:
                    pass
        site = phone = mail = used = 0
        todo = [(l, need(l)) for l in leads]
        todo = [(l, u) for l, u in todo if u]
        log(f"[RusProfile] контакты по {len(todo)} карточкам ...")
        for l, url in todo:
            used += 1
            try:
                c = self.contacts_by_url(url)
            except Exception:
                continue
            if not c:
                continue
            if c.get("website") and not l.get("website"):
                l["website"] = c["website"]; site += 1
            if c.get("phone") and not l.get("phone"):
                l["phone"] = c["phone"]; phone += 1
            if c.get("emails") and not l.get("email"):
                best, kind, is_t = _best_email(c["emails"])
                if best:
                    l["email"] = best; l["_email_kind"] = kind
                    l["_email_is_target"] = is_t; l["_email_src"] = "RusProfile"; mail += 1
            if used % 20 == 0:
                log(f"  RusProfile: {used} | сайт {site} тел {phone} email {mail}")
                _ckpt()
        _ckpt()                              # финальный чекпойнт — контакты хвоста
        log(f"[RusProfile] контакты: {used} карточек | сайт {site} | тел {phone} | email {mail}")
        return {"used": used, "site": site, "phone": phone, "email": mail}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--timeout", type=int, default=420)
    a = ap.parse_args()
    with RusProfileAuth(headless=False) as s:
        if a.login:
            s.login_and_wait(timeout=a.timeout)
        elif a.check:
            log("Контакты открыты под профилем: " + ("ДА" if s.contacts_unlocked() else "НЕТ"))
        else:
            log("Укажи --login или --check")


if __name__ == "__main__":
    main()
