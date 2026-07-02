# -*- coding: utf-8 -*-
r"""
MCP-сервер Яндекс Диска — stdio-обёртка над connectors/yadisk_client.py
(вся логика REST там; здесь только регистрация тулз в FastMCP и selftest).

Python-аналог ВСЕХ дисковых тулз yacli (info / list / mkdir / upload /
upload_link / download / publish / unpublish) ПЛЮС то, чего в yacli нет:
удаление, Корзина (list/restore/empty), move/copy и загрузка по внешнему URL.

Токен: env YANDEX_DISK_TOKEN (scope cloud_api:disk.write) — см. yadisk_client.py.

Инструменты (пути на Диске — 'disk:/...' или '/...', в Корзине — 'trash:/...'):
  yadisk_info()                                — квота: всего/занято/свободно/Корзина
  yadisk_list(path, limit, offset)             — содержимое папки или карточка файла
  yadisk_mkdir(path, parents)                  — создать папку (parents=True — с родителями)
  yadisk_upload(source, path, overwrite)       — локальный файл -> Диск (стриминг)
  yadisk_upload_link(source, path, overwrite)  — то же + сразу вернуть публичную ссылку
  yadisk_upload_url(url, path)                 — Диск САМ скачает файл по внешнему URL
  yadisk_download(path, output_path, force)    — файл с Диска -> локальный (папка -> .zip)
  yadisk_publish(path) / yadisk_unpublish(path)— выдать / отозвать публичную ссылку
  yadisk_move(from_path, to_path, overwrite)   — переместить/переименовать
  yadisk_copy(from_path, to_path, overwrite)   — скопировать
  yadisk_delete(path, permanently)             — удалить (по умолчанию — в Корзину)
  yadisk_trash_list(path, limit, offset)       — содержимое Корзины (+origin_path)
  yadisk_restore(path, name, overwrite)        — восстановить из Корзины
  yadisk_empty_trash(path)                     — очистить Корзину (всю или элемент)

Запуск как MCP (stdio):  py connectors\yadisk_mcp.py   (клиентом — по .mcp.json)
Оффлайн-самопроверка логики:  py connectors\yadisk_mcp.py --selftest
"""
import os
import sys

# запускается и как скрипт (py connectors\yadisk_mcp.py), и как модуль пакета
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yadisk_client as yd  # noqa: E402


# --------------------------------- MCP-обёртка --------------------------------
def _build_mcp():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("yadisk")

    @mcp.tool()
    def yadisk_info() -> str:
        """Квота Яндекс Диска: всего/занято/свободно + размер Корзины."""
        return yd._info()

    @mcp.tool()
    def yadisk_list(path: str = "disk:/", limit: int = 200, offset: int = 0) -> str:
        """Показать содержимое папки Яндекс Диска или карточку файла (размер, public_url).
        path: 'disk:/Лиды/...' или '/Лиды/...' (по умолчанию корень)."""
        return yd._list(path, limit, offset)

    @mcp.tool()
    def yadisk_mkdir(path: str, parents: bool = False) -> str:
        """Создать папку на Яндекс Диске. parents=True — создать и недостающих родителей
        (аналог mkdir -p); False — только один уровень (как в yacli)."""
        return yd._mkdir(path, parents)

    @mcp.tool()
    def yadisk_upload(source: str, path: str, overwrite: bool = False) -> str:
        """Загрузить ОДИН локальный файл на Яндекс Диск (стриминг, большие файлы ок).
        source: локальный путь; path: путь НАЗНАЧЕНИЯ на Диске ('disk:/Лиды/x.docx');
        overwrite=False — не перезаписывать существующий."""
        return yd._upload(source, path, overwrite)

    @mcp.tool()
    def yadisk_upload_link(source: str, path: str, overwrite: bool = False) -> str:
        """Загрузить локальный файл на Диск и СРАЗУ опубликовать: вернёт public_url
        (аналог yacli_disk_upload_link)."""
        return yd._upload_link(source, path, overwrite)

    @mcp.tool()
    def yadisk_upload_url(url: str, path: str) -> str:
        """Загрузить файл на Диск ПО ВНЕШНЕМУ URL — сервер Яндекса сам его скачает
        (в yacli такого нет). url: http(s)-ссылка; path: назначение на Диске."""
        return yd._upload_url(url, path)

    @mcp.tool()
    def yadisk_download(path: str, output_path: str, force: bool = False) -> str:
        """Скачать файл с Яндекс Диска в локальный путь (папка приедет .zip-архивом).
        force=False — не перезаписывать существующий локальный файл."""
        return yd._download(path, output_path, force)

    @mcp.tool()
    def yadisk_publish(path: str) -> str:
        """Опубликовать файл/папку на Яндекс Диске и вернуть публичную ссылку (public_url)."""
        return yd._publish(path)

    @mcp.tool()
    def yadisk_unpublish(path: str) -> str:
        """Отозвать публичную ссылку у файла/папки Яндекс Диска."""
        return yd._unpublish(path)

    @mcp.tool()
    def yadisk_move(from_path: str, to_path: str, overwrite: bool = False) -> str:
        """Переместить/переименовать файл или папку на Яндекс Диске
        (большие папки — асинхронно, статус опрашивается автоматически)."""
        return yd._move_copy("move", from_path, to_path, overwrite)

    @mcp.tool()
    def yadisk_copy(from_path: str, to_path: str, overwrite: bool = False) -> str:
        """Скопировать файл или папку на Яндекс Диске (большие — асинхронно)."""
        return yd._move_copy("copy", from_path, to_path, overwrite)

    @mcp.tool()
    def yadisk_delete(path: str, permanently: bool = False) -> str:
        """УДАЛИТЬ папку или файл на Яндекс Диске (корень удалять нельзя).
        permanently=False -> в Корзину (обратимо, ПО УМОЛЧАНИЮ); True -> безвозвратно.
        Большие папки удаляются асинхронно (202) — статус опрашивается автоматически."""
        return yd._delete(path, permanently)

    @mcp.tool()
    def yadisk_trash_list(path: str = "trash:/", limit: int = 200, offset: int = 0) -> str:
        """Показать содержимое Корзины ('trash:/...'); у элементов виден origin_path —
        исходный путь до удаления (пригодится для yadisk_restore)."""
        return yd._trash_list(path, limit, offset)

    @mcp.tool()
    def yadisk_restore(path: str, name: str = "", overwrite: bool = False) -> str:
        """Восстановить элемент из Корзины по его trash:/-пути (см. yadisk_trash_list).
        name — необязательное новое имя при восстановлении."""
        return yd._restore(path, name, overwrite)

    @mcp.tool()
    def yadisk_empty_trash(path: str = "") -> str:
        """Очистить Корзину Яндекс Диска целиком (path пуст) или конкретный элемент
        Корзины (path='trash:/...'). Действие безвозвратно."""
        return yd._empty_trash(path)

    return mcp


def _selftest():
    """Оффлайн: нормализация/гарды/форматирование ядра + сборка FastMCP, без сети/токена."""
    ok = True

    def expect_raise(fn, what):
        nonlocal ok
        try:
            fn()
            ok = False
            print(f"FAIL: {what} не отклонено")
        except RuntimeError:
            print(f"OK: {what} отклонено")

    # гарды корня и схем
    expect_raise(lambda: yd._norm("disk:/"), "операция над корнем disk:/")
    expect_raise(lambda: yd._norm("/"), "операция над корнем /")
    expect_raise(lambda: yd._norm("disk:/x", scheme="trash"), "disk:-путь там, где ждут trash:")
    expect_raise(lambda: yd._norm("trash:/x"), "trash:-путь там, где ждут disk:")
    # нормализация
    assert yd._norm("Лиды/старое") == "disk:/Лиды/старое", yd._norm("Лиды/старое")
    assert yd._norm("/Лиды/x") == "disk:/Лиды/x"
    assert yd._norm("disk:/Лиды/x") == "disk:/Лиды/x"
    assert yd._norm("disk:/", allow_root=True) == "disk:/"
    assert yd._norm("", allow_root=True, scheme="trash") == "trash:/"
    assert yd._norm("x", scheme="trash") == "trash:/x"
    print("OK: нормализация путей disk:/trash:")
    # цепочка родителей для mkdir parents=True
    assert yd._parents_chain("disk:/a/b/c") == ["disk:/a", "disk:/a/b", "disk:/a/b/c"]
    print("OK: _parents_chain")
    # размеры и вспомогательные хелперы (без сети)
    assert yd._fmt_size(0) == "0 Б" and yd._fmt_size(1536) == "1.5 КБ"
    assert yd._fmt_size(3 * 1024 ** 3) == "3.0 ГБ" and yd._fmt_size(None) == "?"
    assert yd._b(True) == "true" and yd._b(False) == "false"
    assert yd._poll_op({}) == "in-progress"
    print("OK: _fmt_size / _b / _poll_op")
    # локальные гарды без сети
    assert yd._upload(r"C:\нет\такого\файла.bin", "disk:/x.bin").startswith("Нет локального файла")
    assert "force=True" in yd._download("disk:/x.bin", os.path.abspath(__file__))
    assert "http(s)-URL" in yd._upload_url("ftp://x/y", "disk:/y")
    print("OK: гарды upload/download/upload_url без сети")
    # обёртки пайплайна: ошибка ядра -> RuntimeError
    expect_raise(lambda: yd.upload_file(r"C:\нет\такого\файла.bin", "disk:/x.bin"),
                 "upload_file несуществующего файла")
    # рендер листинга (без сети)
    fake = {"_embedded": {"total": 2, "offset": 0, "items": [
        {"type": "dir", "name": "Папка", "path": "disk:/Лиды/Папка"},
        {"type": "file", "name": "a.docx", "path": "disk:/Лиды/a.docx",
         "size": 34567, "public_url": "https://yadi.sk/x"}]}}
    out = yd._render_listing("disk:/Лиды", fake)
    assert "2 из 2" in out and "[public]" in out and "33.8 КБ" in out, out
    print("OK: рендер листинга")
    # MCP-обёртка собирается (пакет mcp установлен, все тулзы регистрируются)
    try:
        _build_mcp()
        print("OK: FastMCP собрался (15 инструментов)")
    except Exception as e:
        ok = False
        print(f"FAIL: FastMCP не собрался: {e}")
    print("selftest passed" if ok else "selftest FAILED")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # консоль Windows = cp1251
        except Exception:
            pass
        sys.exit(0 if _selftest() else 1)
    _build_mcp().run()
