# -*- coding: utf-8 -*-
r"""
Патчи для .venv_kimi — ЕДИНЫЙ источник для Docker и локальной установки.

Раньше патчей было два и жили они врозь: №1 — только в README (прозой), №2 — только инлайном
в Dockerfile. Итог: установка строго по README давала нерабочее окружение, а Docker молча
собирал ОБРАТНОЕ — образ, в котором стадия падала на первом же вызове модели (патч №2 написан
под kimi-cli 1.4x, а резолвер ставил в образ 1.12.x). Теперь источник один, и он проверяет,
что делает.

Запускать ИНТЕРПРЕТАТОРОМ ЦЕЛЕВОГО VENV (пакеты ищутся импортом, а не по путям):
    .venv_kimi\Scripts\python.exe patches\apply_patches.py            # применить
    .venv_kimi\Scripts\python.exe patches\apply_patches.py --check    # только проверить (CI/Docker)

Идемпотентен: повторный прогон ничего не портит и выходит с кодом 0.
⚠️ Любая переустановка пакетов СТИРАЕТ патчи — прогонять снова.

Патч №1 — статический: баг kimi-cli под Python 3.12, ломает сам import.
Патч №2 — АДАПТИВНЫЙ: приводит вызов внутри SDK к сигнатуре ФАКТИЧЕСКИ установленного kimi-cli.
Имя параметра берётся интроспекцией KimiCLI.create, а не угадывается. При закреплённой паре
(SDK 0.0.5 + kimi-cli 1.12.0) правка не требуется — и скрипт честно скажет, что не требуется,
вместо того чтобы ломать рабочий вызов.
"""
import argparse
import importlib
import inspect
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Пара из requirements.txt. Разошлось — предупредим (но патч №2 всё равно подстроится сам).
EXPECTED = {"kimi-cli": "1.12.0", "kimi-agent-sdk": "0.0.5"}


def _pkg_file(module, *relpath):
    mod = importlib.import_module(module)
    path = pathlib.Path(mod.__file__).resolve().parent.joinpath(*relpath)
    if not path.is_file():
        raise SystemExit(f"[FAIL] не найден файл пакета: {path}\n"
                         f"       Структура {module} изменилась — патчи надо пересмотреть.")
    return path


# --------------------------------------------------------------------------------------
# Патч №1: kimi-cli под Python 3.12 — падает сам импорт.
# SlashCommand объявлен как @dataclass(frozen=True, slots=True) И PEP 695-generic
# (class SlashCommand[F]), но инстанцируется параметризованно: SlashCommand[F](...). typing
# пытается записать __orig_class__ на frozen+slots-инстанс -> TypeError: super(type, obj).
# В аннотациях (dict[str, SlashCommand[F]], SlashCommand[F] | None) за [F] нет скобки, поэтому
# замена по 'SlashCommand[F](' задевает ровно инстанцирование и не трогает типы.
# --------------------------------------------------------------------------------------
P1_OLD, P1_NEW = "SlashCommand[F](", "SlashCommand("


def patch_slashcmd(check=False):
    path = _pkg_file("kimi_cli", "utils", "slashcmd.py")
    text = path.read_text(encoding="utf-8")
    if P1_OLD not in text:
        print("[ok]   патч 1 (slashcmd, Python 3.12): уже применён")
        return False
    if check:
        print("[MISS] патч 1 (slashcmd, Python 3.12): НЕ применён — import kimi_agent_sdk упадёт")
        return True
    path.write_text(text.replace(P1_OLD, P1_NEW), encoding="utf-8")
    print(f"[fix]  патч 1 (slashcmd, Python 3.12): применён -> {path}")
    return True


# --------------------------------------------------------------------------------------
# Патч №2: согласовать вызов KimiCLI.create() внутри SDK с сигнатурой установленного kimi-cli.
#   kimi-cli 1.12.x  -> create(..., skills_dir=KaosPath|None)     <- SDK 0.0.5 пишет так ИЗ КОРОБКИ
#   kimi-cli >=1.4x  -> create(..., skills_dirs=list|None)        <- нужна правка
# Имя параметра определяем интроспекцией, а не по номеру версии: так скрипт не соврёт ни на
# одной будущей версии и не «починит» то, что не сломано.
# --------------------------------------------------------------------------------------
CALL_SINGULAR = "skills_dir=skills_dir"
CALL_PLURAL = "skills_dirs=[skills_dir] if skills_dir else None"


def _expected_kwarg():
    from kimi_cli.app import KimiCLI
    params = inspect.signature(KimiCLI.create).parameters
    if "skills_dirs" in params:
        return "skills_dirs"
    if "skills_dir" in params:
        return "skills_dir"
    raise SystemExit("[FAIL] у KimiCLI.create нет ни skills_dir, ни skills_dirs — "
                     "API kimi-cli изменился, патч надо переписать.")


def patch_session(check=False):
    path = _pkg_file("kimi_agent_sdk", "_session.py")
    text = path.read_text(encoding="utf-8")
    want = _expected_kwarg()

    if want == "skills_dir":
        # kimi-cli ждёт единственное число — SDK 0.0.5 так и написан. Правка НЕ нужна.
        if CALL_PLURAL in text:
            # Ровно этим и был сломан Docker-образ: патч под 1.4x, накатанный на 1.12.x.
            if check:
                print("[MISS] патч 2 (skills_dir): вызов переписан под skills_dirs, а kimi-cli "
                      "ждёт skills_dir -> TypeError при первом же prompt()")
                return True
            path.write_text(text.replace(CALL_PLURAL, CALL_SINGULAR), encoding="utf-8")
            print(f"[fix]  патч 2 (skills_dir): ОТКАЧЕН — kimi-cli ждёт skills_dir -> {path}")
            return True
        print("[ok]   патч 2 (skills_dir): не требуется — kimi-cli ждёт skills_dir, SDK так и зовёт")
        return False

    # kimi-cli >= 1.4x: нужен список skills_dirs.
    if CALL_PLURAL in text:
        print("[ok]   патч 2 (skills_dirs): уже применён")
        return False
    if CALL_SINGULAR not in text:
        raise SystemExit(f"[FAIL] патч 2: в {path} нет ни {CALL_SINGULAR!r}, ни следа правки. "
                         f"SDK изменился — патч надо пересмотреть.")
    if check:
        print("[MISS] патч 2 (skills_dirs): НЕ применён — kimi-cli ждёт skills_dirs -> TypeError")
        return True
    path.write_text(text.replace(CALL_SINGULAR, CALL_PLURAL), encoding="utf-8")
    print(f"[fix]  патч 2 (skills_dirs): применён -> {path}")
    return True


def _version_drift():
    from importlib.metadata import PackageNotFoundError, version
    out = []
    for dist, want in EXPECTED.items():
        try:
            got = version(dist)
        except PackageNotFoundError:
            raise SystemExit(f"[FAIL] пакет {dist} не установлен. Сначала: "
                             f"pip install -r requirements.txt")
        if got != want:
            out.append(f"{dist}: установлен {got}, в requirements.txt закреплён {want}")
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="Патчи .venv_kimi (Docker + локальная установка)")
    ap.add_argument("--check", action="store_true",
                    help="только проверить; код 1, если окружение не согласовано")
    a = ap.parse_args(argv)

    print(f"venv: {sys.executable}")
    for w in _version_drift():
        print(f"[warn] {w}")

    # ВАЖЕН ПОРЯДОК: патч 2 требует импорта kimi_cli.app, а без патча 1 импорт падает на 3.12.
    needs = patch_slashcmd(check=a.check)
    if a.check and needs:
        # Проверить патч 2 интроспекцией не выйдет — импорт всё равно упадёт.
        print("\nОкружение не согласовано (запусти без --check).")
        return 1
    needs |= patch_session(check=a.check)

    if a.check:
        if needs:
            print("\nОкружение не согласовано — стадия one-pager упадёт (запусти без --check).")
            return 1
        print("\nОкружение согласовано: патчи на месте, вызов SDK совпадает с сигнатурой kimi-cli.")
        return 0

    # Патчи бессмысленны, если после них модуль всё равно не импортится.
    try:
        from kimi_agent_sdk import prompt  # noqa: F401
    except Exception as e:
        raise SystemExit(f"[FAIL] после патчей `from kimi_agent_sdk import prompt` всё ещё "
                         f"падает: {type(e).__name__}: {e}")
    print(f"\n[ok] import kimi_agent_sdk.prompt работает"
          + ("" if needs else " (изменений не потребовалось)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
