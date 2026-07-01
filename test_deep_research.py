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
    check("соцсети НЕ помечены пробелом (они есть)",
          crit["counts"]["has_social"] is True, crit["counts"])
    check("счётчики полноты присутствуют",
          "branches_filled" in crit["counts"] and "branches_total" in crit["counts"],
          crit["counts"])
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


def main():
    print("=== ДЫМОВОЙ ТЕСТ ЛОГИКИ deep_research_engine (без сети, без LLM) ===")
    f = test_regex_extract()
    test_completeness_critic(f)
    test_refill_closes_gap()
    test_placeholder_detection()
    test_consolidate_renders()
    print("\n" + ("=" * 60))
    if _fails:
        print(f"ПРОВАЛЕНО проверок: {len(_fails)} -> {_fails}")
        sys.exit(1)
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ.")


if __name__ == "__main__":
    main()
