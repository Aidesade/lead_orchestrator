# -*- coding: utf-8 -*-
r"""
Python-клиент Яндекс Диска на официальном REST API — ЯДРО коннектора, без MCP.
Без сторонних зависимостей (urllib из stdlib). Поверх него:
  * пайплайн (disk_organize.py) — mkdir/upload ПО УМОЛЧАНИЮ через этот клиент
    (yacli остаётся фолбэком, см. там же);
  * MCP-сервер connectors/yadisk_mcp.py — интерактивные тулзы yadisk_*.

Токен: переменная окружения YANDEX_DISK_TOKEN — OAuth-токен Яндекс Диска со scope
cloud_api:disk.write (+ .read). Получить: https://oauth.yandex.ru (создать
приложение с правами «Яндекс.Диск REST API: запись/чтение») либо polygon
https://yandex.ru/dev/disk/poligon/. НЕ храните токен в файлах репозитория —
только в env (например: setx YANDEX_DISK_TOKEN "...").

Функции _info/_list/_mkdir/_upload/... возвращают ЧЕЛОВЕКОЧИТАЕМУЮ строку
(«✓ ...» при успехе, текст ошибки при неудаче) — формат MCP-тулз.
Для пайплайна есть исключение-ориентированные обёртки ensure_dir()/upload_file()
и проверка available().
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk"

# Поля листинга — чтобы ответ был компактным (API по умолчанию отдаёт огромный JSON)
_LS = ("type,name,path,size,modified,public_url,origin_path,"
       "_embedded.total,_embedded.limit,_embedded.offset,"
       "_embedded.items.name,_embedded.items.type,_embedded.items.path,"
       "_embedded.items.size,_embedded.items.public_url,_embedded.items.origin_path")


def available():
    """Есть ли OAuth-токен (можно ли вообще пользоваться этим коннектором)."""
    return bool((os.environ.get("YANDEX_DISK_TOKEN") or "").strip())


def _token():
    t = (os.environ.get("YANDEX_DISK_TOKEN") or "").strip()
    if not t:
        raise RuntimeError(
            "Не задан YANDEX_DISK_TOKEN (OAuth Яндекс Диска, scope cloud_api:disk.write). "
            "Получить: https://oauth.yandex.ru или https://yandex.ru/dev/disk/poligon/ , "
            'затем: setx YANDEX_DISK_TOKEN "<токен>" и перезапустить клиент.')
    return t


def _q(s):
    return urllib.parse.quote(s or "", safe="")


def _b(flag):
    """Python bool -> строка 'true'/'false' для query-параметров API."""
    return "true" if flag else "false"


def _norm(path, allow_root=False, scheme="disk"):
    """Нормализовать путь к 'disk:/...' либо 'trash:/...'.

    scheme — схема по умолчанию, когда путь дан без префикса ('/Лиды' и т.п.).
    Явный префикс уважается, но disk:-путь там, где ждут Корзину (и наоборот), — ошибка.
    allow_root=False запрещает операцию над корнем (гард от удаления/публикации всего Диска).
    """
    p = (path or "").strip()
    if not p and allow_root:
        p = scheme + ":/"
    if not p:
        raise RuntimeError("Пустой путь.")
    got = scheme
    if p.startswith("trash:"):
        got, p = "trash", p[6:]
    elif p.startswith("disk:"):
        got, p = "disk", p[5:]
    if got != scheme:
        raise RuntimeError(f"Ожидался путь схемы {scheme}:/..., получен {got}:/... — проверьте аргумент.")
    core = "/" + p.lstrip("/")
    if core == "/" and not allow_root:
        raise RuntimeError("Отказ: операция над КОРНЕМ запрещена — укажите конкретную папку/файл.")
    return f"{scheme}:{core}"


def _fmt_size(n):
    """1234567 -> '1.2 МБ' (человекочитаемый размер)."""
    try:
        n = float(int(n))
    except (TypeError, ValueError):
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024.0


def _parents_chain(p):
    """'disk:/a/b/c' -> ['disk:/a', 'disk:/a/b', 'disk:/a/b/c'] (для mkdir parents=True)."""
    scheme, core = p.split(":", 1)
    parts = [s for s in core.split("/") if s]
    return [f"{scheme}:/" + "/".join(parts[:i + 1]) for i in range(len(parts))]


def _req(method, url, data=None, headers=None, timeout=60, auth=True, _retry=True):
    """HTTP-запрос -> (status, json|текст). Сетевые ошибки -> (0, {'error': ...})."""
    r = urllib.request.Request(url, data=data, method=method)
    if auth:
        r.add_header("Authorization", f"OAuth {_token()}")
    r.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
            parsed = json.loads(body) if body[:1] in ("{", "[") else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return e.code, raw
    except urllib.error.URLError as e:
        # Windows нередко сбрасывает ПЕРВОЕ соединение после простоя (WinError 10054) —
        # GET без тела безопасно (идемпотентно) повторить один раз.
        if _retry and method == "GET" and data is None:
            time.sleep(2)
            return _req(method, url, headers=headers, timeout=timeout, auth=auth, _retry=False)
        return 0, {"error": f"network: {e}"}


def _req423(method, url, tries=3, **kw):
    """То же, но с ретраем на 423 Locked (десктоп-синк Я.Диска лочит файлы)."""
    st, data = 0, {}
    for i in range(tries):
        st, data = _req(method, url, **kw)
        if st != 423:
            return st, data
        time.sleep(2 * (i + 1))
    return st, data


def _poll(href, tries=60):
    """Опрос статуса асинхронной операции (202: большая папка, копия, загрузка по URL)."""
    for _ in range(tries):
        st, data = _req("GET", href)
        status = (data or {}).get("status")
        if status in ("success", "failed"):
            return status
        time.sleep(1)
    return "in-progress"


def _poll_op(data):
    """Опросить асинхронную операцию по href из 202-ответа (нет href -> 'in-progress')."""
    href = (data or {}).get("href")
    return _poll(href) if href else "in-progress"


def _fail(st, data, p=""):
    """Единый текст типовых ошибок API."""
    if st == 404:
        return f"Не найдено: {p or data}"
    if st in (401, 403):
        return f"Ошибка авторизации {st}: проверьте YANDEX_DISK_TOKEN (scope cloud_api:disk.write). {data}"
    if st == 423:
        return (f"423 Locked: {p} залочен десктоп-синком Я.Диска. "
                "Приостановите синхронизацию на ПК и повторите (см. CLAUDE.md).")
    if st == 507:
        return "507: на Диске закончилось место (см. yadisk_info)."
    return f"Ошибка {st}: {data}"


# --------------------------- логика операций (тестируема без MCP) -------------
def _info():
    st, d = _req("GET", API + "/")
    if st != 200:
        return _fail(st, d)
    total, used = d.get("total_space", 0), d.get("used_space", 0)
    lines = [
        "Яндекс Диск:",
        f"  всего:    {_fmt_size(total)}",
        f"  занято:   {_fmt_size(used)}",
        f"  свободно: {_fmt_size(total - used)}",
        f"  Корзина:  {_fmt_size(d.get('trash_size', 0))}",
    ]
    if d.get("max_file_size"):
        lines.append(f"  макс. файл: {_fmt_size(d['max_file_size'])}")
    return "\n".join(lines)


def _fmt_item(it):
    typ = (it.get("type") or "?")[:4]
    size = f"  {_fmt_size(it['size'])}" if it.get("size") is not None and it.get("type") == "file" else ""
    pub = "  [public]" if it.get("public_url") else ""
    orig = f"  <- {it['origin_path']}" if it.get("origin_path") else ""   # у элементов Корзины
    return f"  [{typ:4}] {it.get('name')}{size}{pub}   {it.get('path')}{orig}"


def _list_url(base, p, limit, offset):
    return f"{base}?path={_q(p)}&limit={int(limit)}&offset={int(offset)}&fields={_LS}"


def _render_listing(p, data):
    if data.get("type") == "file":                    # карточка одиночного файла
        out = [f"{data.get('path')} — файл {data.get('name')}, {_fmt_size(data.get('size'))}"
               f", изменён {data.get('modified', '?')}"]
        if data.get("public_url"):
            out.append(f"  public_url: {data['public_url']}")
        return "\n".join(out)
    emb = data.get("_embedded") or {}
    items, total = emb.get("items") or [], emb.get("total", 0)
    if not items:
        return f"{p}: пусто"
    out = [f"{p} — {len(items)} из {total} (offset={emb.get('offset', 0)}):"]
    out += [_fmt_item(it) for it in items]
    return "\n".join(out)


def _listing(base, scheme, path, limit, offset):
    """Общая логика list / trash_list (различаются схемой и базовым URL)."""
    p = _norm(path, allow_root=True, scheme=scheme)
    st, data = _req("GET", _list_url(base, p, limit, offset))
    if st != 200:
        return _fail(st, data, p)
    return _render_listing(p, data)


def _list(path="disk:/", limit=200, offset=0):
    return _listing(f"{API}/resources", "disk", path, limit, offset)


def _trash_list(path="trash:/", limit=200, offset=0):
    return _listing(f"{API}/trash/resources", "trash", path, limit, offset)


def _exists_dir(p):
    """Существует ли на Диске папка p (для фолбэка mkdir при 423)."""
    st, d = _req("GET", f"{API}/resources?path={_q(p)}&fields=type")
    return st == 200 and (d or {}).get("type") == "dir"


def _mkdir(path, parents=False):
    p = _norm(path)
    chain = _parents_chain(p) if parents else [p]
    made, existed = [], []
    for step in chain:
        st, data = _req423("PUT", f"{API}/resources?path={_q(step)}")
        err = (data or {}).get("error") if isinstance(data, dict) else ""
        if st == 201:
            made.append(step)
        elif st == 409 and err == "DiskPathPointsToExistentDirectoryError":
            existed.append(step)
        elif st == 409 and err == "DiskPathDoesntExistsError":
            return f"Нет родительской папки для {step} — вызовите с parents=True."
        elif st == 423 and _exists_dir(step):
            # папка залочена десктоп-синком Я.Диска, но УЖЕ существует — для mkdir это успех
            existed.append(step)
        else:
            return _fail(st, data, step)
    if made:
        return f"✓ Создано: {', '.join(made)}"
    return f"Уже существует: {existed[-1]}"


def _upload(source, path, overwrite=False):
    src = os.path.abspath(os.path.expanduser(source))
    if not os.path.isfile(src):
        return f"Нет локального файла: {src}"
    size = os.path.getsize(src)
    p = _norm(path)
    st, data = _req423("GET", f"{API}/resources/upload?path={_q(p)}&overwrite={_b(overwrite)}")
    if st == 409:
        return f"На Диске уже есть {p} — повторите с overwrite=True."
    if st != 200 or not (data or {}).get("href"):
        return _fail(st, data, p)
    # PUT содержимого стримингом (Content-Length задаём сами — иначе urllib уйдёт в chunked)
    with open(src, "rb") as f:
        st2, d2 = _req("PUT", data["href"], data=f, auth=False, timeout=1800,
                       headers={"Content-Length": str(size),
                                "Content-Type": "application/octet-stream"})
    if st2 in (201, 202):
        return f"✓ Загружено: {src} -> {p} ({_fmt_size(size)})"
    return _fail(st2, d2, p)


def _publish(path):
    p = _norm(path)
    st, data = _req("PUT", f"{API}/resources/publish?path={_q(p)}")
    if st != 200:
        return _fail(st, data, p)
    st2, d2 = _req("GET", f"{API}/resources?path={_q(p)}&fields=public_url,public_key")
    url = (d2 or {}).get("public_url") if st2 == 200 else None
    return f"✓ Опубликовано: {p}\n  {url or '(public_url не вернулся — проверьте yadisk_list)'}"


def _unpublish(path):
    p = _norm(path)
    st, data = _req("PUT", f"{API}/resources/unpublish?path={_q(p)}")
    if st != 200:
        return _fail(st, data, p)
    return f"✓ Публичная ссылка отозвана: {p}"


def _upload_link(source, path, overwrite=False):
    res = _upload(source, path, overwrite)
    if not res.startswith("✓"):
        return res
    return res + "\n" + _publish(path)


def _upload_url(url, path):
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return f"Нужен внешний http(s)-URL, получено: {url!r}"
    p = _norm(path)
    st, data = _req("POST", f"{API}/resources/upload?url={_q(u)}&path={_q(p)}")
    if st != 202 or not (data or {}).get("href"):
        return _fail(st, data, p)
    res = _poll(data["href"], tries=120)              # сервер сам качает — может быть долго
    return (f"✓ Загрузка по URL на {p}: {res}" if res == "success"
            else f"Загрузка по URL на {p}: {res} (проверьте yadisk_list)")


def _download(path, output_path, force=False):
    p = _norm(path)
    dst = os.path.abspath(os.path.expanduser(output_path))
    if os.path.exists(dst) and not force:
        return f"Локальный файл уже есть: {dst} — повторите с force=True."
    st, data = _req("GET", f"{API}/resources/download?path={_q(p)}")
    if st != 200 or not (data or {}).get("href"):
        return _fail(st, data, p)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        # href — подписанная временная ссылка, OAuth-заголовок не нужен
        with urllib.request.urlopen(urllib.request.Request(data["href"]), timeout=1800) as r, \
                open(dst, "wb") as w:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                w.write(chunk)
    except Exception as e:
        try:
            os.remove(dst)                            # не оставлять битый недокачанный файл
        except OSError:
            pass
        return f"Ошибка скачивания {p}: {e}"
    return f"✓ Скачано: {p} -> {dst} ({_fmt_size(os.path.getsize(dst))})"


def _move_copy(op, from_path, to_path, overwrite=False):
    src, dst = _norm(from_path), _norm(to_path)
    st, data = _req423("POST", f"{API}/resources/{op}?from={_q(src)}&path={_q(dst)}"
                               f"&overwrite={_b(overwrite)}")
    verb = "Перемещено" if op == "move" else "Скопировано"
    if st == 201:
        return f"✓ {verb}: {src} -> {dst}"
    if st == 202:
        return f"✓ {verb} (асинхронно): {src} -> {dst} — операция {_poll_op(data)}"
    if st == 409:
        return f"Целевой путь занят или нет родительской папки: {dst}. {data}"
    return _fail(st, data, src)


def _delete(path, permanently=False):
    p = _norm(path)
    st, data = _req423("DELETE", f"{API}/resources?path={_q(p)}&permanently={_b(permanently)}")
    where = "БЕЗВОЗВРАТНО" if permanently else "в Корзину"
    if st == 204:
        return f"✓ Удалено ({where}): {p}"
    if st == 202:
        return f"✓ Удаление большой папки {p} ({where}): операция {_poll_op(data)}"
    return _fail(st, data, p)


def _restore(path, name="", overwrite=False):
    p = _norm(path, scheme="trash")
    url = f"{API}/trash/resources/restore?path={_q(p)}&overwrite={_b(overwrite)}"
    nm = (name or "").strip()
    if nm:
        url += f"&name={_q(nm)}"
    st, data = _req("PUT", url)
    if st == 201:
        return f"✓ Восстановлено из Корзины: {p}" + (f" (как {nm})" if nm else "")
    if st == 202:
        return f"✓ Восстановление {p}: операция {_poll_op(data)}"
    if st == 409:
        return f"Целевой путь уже существует — повторите с overwrite=True или задайте name. {data}"
    return _fail(st, data, p)


def _empty_trash(path=""):
    p = (path or "").strip()
    url = f"{API}/trash/resources"
    if p:
        url += f"?path={_q(_norm(p, scheme='trash'))}"
    st, data = _req("DELETE", url)
    if st == 204:
        return f"✓ Удалено из Корзины: {p}" if p else "✓ Корзина очищена"
    if st == 202:
        return f"✓ Очистка Корзины: операция {_poll_op(data)}"
    return _fail(st, data, p)


# --------------------- обёртки для пайплайна (исключения) ---------------------
def ensure_dir(path, parents=False):
    """Идемпотентно создать папку; «уже существует» — успех, ошибка -> RuntimeError."""
    res = _mkdir(path, parents)
    if res.startswith("✓") or res.startswith("Уже существует"):
        return res
    raise RuntimeError(res)


def upload_file(source, path, overwrite=True):
    """Загрузить файл; ошибка (включая 409 без overwrite) -> RuntimeError."""
    res = _upload(source, path, overwrite)
    if not res.startswith("✓"):
        raise RuntimeError(res)
    return res
