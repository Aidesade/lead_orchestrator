# -*- coding: utf-8 -*-
"""
Валидация и поиск ИНН/ОГРН по контрольной сумме.

Зачем контрольная сумма: в JSON-карточке 2ГИС и в выдаче полно цифровых строк
(телефоны, id фирм, координаты). Чтобы не принять их за ИНН, любой кандидат
проверяется официальной контрольной суммой ФНС — ложноположительные практически
исключены.

API:
  valid_inn(s)   -> bool     ИНН 10 (ЮЛ) или 12 (ФЛ/ИП) цифр, к.с. сходится
  valid_ogrn(s)  -> bool     ОГРН 13 (ЮЛ) или 15 (ИП) цифр, к.с. сходится
  find_requisites(obj) -> {"inns": [...], "ogrns": [...], "paths": {inn: path}}
                            рекурсивный обход dict/list: ключи *inn*/*ogrn* +
                            скан строковых значений ("ИНН 7701…") с проверкой к.с.
"""
import re

_DIGITS = re.compile(r"\d{10,15}")


def _only_digits(s):
    return re.sub(r"\D", "", str(s or ""))


def valid_inn(s):
    d = _only_digits(s)
    if len(d) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        c = sum(int(d[i]) * w[i] for i in range(9)) % 11 % 10
        return c == int(d[9])
    if len(d) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(int(d[i]) * w1[i] for i in range(10)) % 11 % 10
        c2 = sum(int(d[i]) * w2[i] for i in range(11)) % 11 % 10
        return c1 == int(d[10]) and c2 == int(d[11])
    return False


def valid_ogrn(s):
    d = _only_digits(s)
    if len(d) == 13:
        return int(d[:12]) % 11 % 10 == int(d[12])
    if len(d) == 15:
        return int(d[:14]) % 13 % 10 == int(d[14])
    return False


def find_requisites(obj):
    """Рекурсивно собрать все валидные ИНН/ОГРН из произвольного JSON-объекта.
    Возвращает {"inns": [...], "ogrns": [...], "paths": {value: dotted_path}}."""
    inns, ogrns, paths = [], [], {}

    def add(kind, val, path):
        if kind == "inn" and val not in inns:
            inns.append(val)
            paths[val] = path
        elif kind == "ogrn" and val not in ogrns:
            ogrns.append(val)
            paths[val] = path

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                p = f"{path}.{k}" if path else str(k)
                if isinstance(v, (str, int)):
                    sv = _only_digits(v)
                    if ("inn" in kl) and valid_inn(sv):
                        add("inn", sv, p)
                    elif ("ogrn" in kl) and valid_ogrn(sv):
                        add("ogrn", sv, p)
                    elif isinstance(v, str):
                        # строки вида "ИНН 7701234567 / ОГРН ..."
                        for m in _DIGITS.findall(v):
                            if valid_inn(m):
                                add("inn", m, p)
                            elif valid_ogrn(m):
                                add("ogrn", m, p)
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for m in _DIGITS.findall(node):
                if valid_inn(m):
                    add("inn", m, path)
                elif valid_ogrn(m):
                    add("ogrn", m, path)

    walk(obj, "")
    return {"inns": inns, "ogrns": ogrns, "paths": paths}


if __name__ == "__main__":
    # быстрый самотест контрольных сумм
    assert valid_inn("7707083893")      # Сбербанк (ЮЛ, 10)
    assert valid_inn("500100732259")    # пример ФЛ (12)
    assert not valid_inn("1234567890")
    assert valid_ogrn("1027700132195")  # Сбербанк ОГРН (13)
    assert not valid_ogrn("1027700132196")  # та же, испорчена к.с.
    assert not valid_inn("7707083894")      # Сбербанк ИНН, испорчена к.с.
    print("inn_util self-test OK")
