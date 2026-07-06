# -*- coding: utf-8 -*-
r"""
Дымовой тест ЛОГИКИ движка deep_research_engine — БЕЗ сети и БЕЗ LLM.
Проверяет ядро: регэксп-экстракт целевых таблиц, детектор пробелов
(completeness_critic), слияние находок и ПЕТЛЮ ЦЕЛЕВОГО ДОБОРА (пробел закрывается
follow-up-находкой). Это LLM-независимая страховка пайплайна.

Запуск:  py test_deep_research.py
"""
import os
import sys

os.environ.setdefault("DR_USE_LLM", "0")  # тест чисто логический — LLM не дёргаем
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import deep_research_engine as E

_fails = []


def check(name, cond, detail=""):
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# Синтетическая «главная» в стиле Tilda «Рязаньавтодор»: 3 филиала — один полный,
# один без телефона, один без директора; + соцсети + контакты.
SYNTH_PAGE_TEXT = """
АО Рязаньавтодор +7 (4912) 55-01-70 info@avtodor-rzn.ru
г. Рязань, Куйбышевское шоссе, дом 35
КОНТАКТЫ Телефон: +7 (4912) 55-01-70 Электронная почта: info@avtodor-rzn.ru
Мы в соцсетях: t.me/autodor_ryazan vk.com/autodor62
ФИЛИАЛЫ
Рязанское ДРСУ Директор: Фадеев Александр Викторович Телефон: (4912) 50-12-07 Диспетчер: (4912) 30-00-02
Пронское ДРСУ Директор: Чубукова Марина Вячеславовна
Скопинское ДРСУ Телефон: (49156) 2-00-46
"""


def test_regex_extract():
    print("\n[1] regex_findings: экстракт филиалов/соцсетей/контактов с одной страницы")
    pages = [{"url": "https://avtodor-rzn.ru/", "markdown": SYNTH_PAGE_TEXT, "source": "site:http"}]
    f = E.regex_findings(pages)
    names = {b["branch"] for b in f["branches"]}
    check("найден полный филиал (Рязанское ДРСУ)",
          any("Рязанское" in n for n in names), f"branches={names}")
    ryaz = next((b for b in f["branches"] if "Рязанское" in b["branch"]), {})
    check("у Рязанского ДРСУ есть директор Фадеев",
          "Фадеев" in ryaz.get("director", ""), ryaz)
    check("у Рязанского ДРСУ есть телефон с источником",
          "50-12-07" in ryaz.get("phone", "") and ryaz.get("source", "").startswith("http"), ryaz)
    socials = {s["kind"] for s in f["contacts"]["social"]}
    check("соцсети извлечены (Telegram + ВКонтакте)",
          "Telegram" in socials and "ВКонтакте" in socials, socials)
    emails = {e["email"] for e in f["contacts"]["emails"]}
    check("извлечён e-mail info@avtodor-rzn.ru", "info@avtodor-rzn.ru" in emails, emails)
    check("у каждой строки филиала есть source URL",
          all(b.get("source", "").startswith("http") for b in f["branches"]))
    return f


def test_completeness_critic(findings):
    print("\n[2] completeness_critic: детектор пробелов целевых таблиц + follow-up")
    # подмешаем неполные строки руководства/отделов — как после первого прохода
    findings["branches"].append({"branch": "Пронское ДРСУ",
                                 "director": "Чубукова Марина Вячеславовна",
                                 "phone": "", "source": "https://avtodor-rzn.ru/"})
    crit = E.completeness_critic(findings, name="АО Рязаньавтодор", inn="6234065445",
                                 domain="https://avtodor-rzn.ru")
    goals = {f["goal"] for f in crit["followups"]}
    check("обнаружен пробел по филиалу без телефона",
          any("branch" == f["goal"] for f in crit["followups"]), crit["counts"])
    check("сгенерирован follow-up на тендерный контакт", "tender" in goals, goals)
    check("сгенерирован follow-up на ИТ/цифровизацию", "it" in goals, goals)
    check("follow-up на руководство (замы/гл.инженер)", "leadership" in goals, goals)
    check("follow-up на экосистему/вертикаль (учредитель/ведомство)", "ecosystem" in goals, goals)
    check("соцсети НЕ помечены пробелом (они есть)",
          crit["counts"]["has_social"] is True, crit["counts"])
    check("счётчики полноты присутствуют",
          "branches_filled" in crit["counts"] and "branches_total" in crit["counts"],
          crit["counts"])
    # находка экосистемы ЗАКРЫВАЕТ пробел (и merge её не теряет)
    f2 = E.merge_findings([findings, {"ecosystem": [
        {"entity": "Минцифры РТ", "relation": "курирующее ведомство", "source": "https://x"}]}])
    crit2 = E.completeness_critic(f2, name="X", inn="1", domain="d")
    check("экосистема из находок закрывает пробел вертикали",
          crit2["counts"]["has_ecosystem"] is True and
          not any(f["goal"] == "ecosystem" for f in crit2["followups"]), crit2["counts"])
    return crit


def test_refill_closes_gap():
    print("\n[3] петля добора: follow-up-находка ЗАКРЫВАЕТ пробел (директор+телефон)")
    # старт: филиал без телефона
    base = E.merge_findings([{
        "branches": [{"branch": "Скопинское ДРСУ", "director": "", "phone": "",
                      "source": "https://avtodor-rzn.ru/"}],
        "contacts": {"phones": [], "emails": [], "social": [
            {"kind": "Telegram", "url": "https://t.me/autodor_ryazan", "source": "x"}]},
    }])
    crit0 = E.completeness_critic(base, name="X", inn="1", domain="d")
    gap0 = crit0["counts"]["branches_filled"]
    # follow-up принёс полную строку того же филиала
    refill = {
        "branches": [{"branch": "Скопинское ДРСУ", "director": "Корольков Андрей Петрович",
                      "phone": "(49156) 2-00-46", "source": "https://2gis.ru/..."}],
        "contacts": {"phones": [], "emails": [], "social": []},
    }
    merged = E.merge_findings([base, refill])
    crit1 = E.completeness_critic(merged, name="X", inn="1", domain="d")
    row = merged["branches"][0]
    check("после merge у филиала появился директор", "Корольков" in row.get("director", ""), row)
    check("после merge у филиала появился телефон", "2-00-46" in row.get("phone", ""), row)
    check("дублей филиала не возникло (merge схлопнул по имени)",
          len(merged["branches"]) == 1, merged["branches"])
    check("счётчик заполненных филиалов вырос (пробел закрыт)",
          crit1["counts"]["branches_filled"] > gap0,
          f"{gap0} -> {crit1['counts']['branches_filled']}")


def test_placeholder_detection():
    print("\n[4] детектор «не подтверждено» считается ПУСТЫМ (как у текущего агента)")
    findings = E.merge_findings([{
        "branches": [
            {"branch": "Ряжское ДРСУ", "director": "не подтверждено в открытых источниках",
             "phone": "—", "source": ""},
            {"branch": "Касимовское ДРСУ", "director": "через приёмную", "phone": "",
             "source": ""}],
        "contacts": {"phones": [], "emails": [], "social": []},
    }])
    crit = E.completeness_critic(findings, name="X", inn="1", domain="d")
    check("строки с «не подтверждено»/«через приёмную»/«—» НЕ считаются заполненными",
          crit["counts"]["branches_filled"] == 0, crit["counts"])
    check("на каждую такую строку есть follow-up добора",
          sum(1 for f in crit["followups"] if f["goal"] == "branch") == 2, crit["followups"])


def test_consolidate_renders():
    print("\n[5] consolidate: рендер источникованного документа + JSON-блок")
    f = E.regex_findings([{"url": "https://avtodor-rzn.ru/", "markdown": SYNTH_PAGE_TEXT,
                           "source": "site:http"}])
    crit = E.completeness_critic(f, name="АО Рязаньавтодор", inn="6234065445", domain="https://avtodor-rzn.ru")
    doc = E.consolidate("АО Рязаньавтодор", "6234065445", "https://avtodor-rzn.ru", "",
                        f, crit, ["https://avtodor-rzn.ru/"], [], [], rounds=1)
    check("документ содержит таблицу филиалов", "Филиалы / обособленные" in doc)
    check("документ содержит блок полноты для писателя", "Полнота целевых таблиц" in doc)
    check("документ содержит структурный JSON", "```json" in doc and "\"branches\"" in doc)
    check("документ содержит дисциплину отрицаний", "ДИСЦИПЛИНА" in doc)
    check("документ содержит ФИО директора с источником",
          "Фадеев" in doc and "avtodor-rzn.ru" in doc)
    check("в блоке полноты есть строка про экосистему/вертикаль", "Экосистема/вертикаль" in doc)
    # consolidate принимает и СПИСОК доменов (мульти-домен), и рендерит секцию экосистемы
    f_eco = E.merge_findings([f, {"ecosystem": [
        {"entity": "Минцифры РТ", "relation": "курирующее ведомство",
         "person": "Начвин И.С.", "source": "https://digital.tatarstan.ru"}]}])
    crit_eco = E.completeness_critic(f_eco, name="X", inn="1", domain="d")
    doc2 = E.consolidate("X", "1", ["https://a.ru", "https://b.tatarstan.ru"], "",
                         f_eco, crit_eco, [], [], [], rounds=0)
    check("мульти-домен: оба сайта в шапке документа", "a.ru" in doc2 and "b.tatarstan.ru" in doc2)
    check("секция экосистемы отрендерена с ключевым лицом",
          "Экосистема и вертикаль" in doc2 and "Начвин" in doc2)


def test_domain_validation():
    print("\n[6] _text_belongs: «родовое» название не матчится по одному слову (кейс cbr.ru)")
    name = "АО Центр Информационных Технологий РТ"
    check("страница Центробанка НЕ признаётся сайтом «ЦИТ» (слово «центр» — не доказательство)",
          not E._text_belongs("Центральный банк Российской Федерации. Пресс-центр. "
                              "Информационные сообщения.", name, "1655505808"))
    check("страница с ПОЛНОЙ фразой названия — признаётся",
          E._text_belongs("АО «Центр информационных технологий РТ» — оператор инфраструктуры",
                          name, ""))
    check("ИНН на странице — признаётся без фразы",
          E._text_belongs("Реквизиты: ИНН 1655505808, ОГРН ...", name, "1655505808"))
    check("отличительный токен матчится по границе слова",
          E._text_belongs("О компании Рязаньавтодор: дороги region", "АО Рязаньавтодор", "")
          and not E._text_belongs("газета «Автодорожник»", "АО Рязаньавтодор", ""))


def main():
    print("=== ДЫМОВОЙ ТЕСТ ЛОГИКИ deep_research_engine (без сети, без LLM) ===")
    f = test_regex_extract()
    test_completeness_critic(f)
    test_refill_closes_gap()
    test_placeholder_detection()
    test_consolidate_renders()
    test_domain_validation()
    print("\n" + ("=" * 60))
    if _fails:
        print(f"ПРОВАЛЕНО проверок: {len(_fails)} -> {_fails}")
        sys.exit(1)
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ.")


if __name__ == "__main__":
    main()
