# -*- coding: utf-8 -*-
"""Утилиты запуска браузера для uc-скрейперов.

Зачем: undetected_chromedriver иногда подтягивает ChromeDriver новее
установленного Chrome («This version of ChromeDriver only supports Chrome
version N. Current browser version is M»). Лечится передачей в uc.Chrome
параметра version_main = мажорная версия РЕАЛЬНО установленного Chrome —
тогда uc берёт совпадающий драйвер. Авто-определение переживает будущие
обновления Chrome (в отличие от хардкода версии).
"""
import glob
import os
import re


def chrome_major():
    """Мажорная версия установленного Chrome (Windows) или None, если не нашли."""
    # 1) реестр BLBeacon — самый надёжный источник
    try:
        import winreg
        for hive, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Google\Chrome\BLBeacon"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    v, _ = winreg.QueryValueEx(k, "version")
                    return int(str(v).split(".")[0])
            except OSError:
                continue
    except Exception:
        pass
    # 2) запасной путь — версионные подпапки .../Chrome/Application/<версия>
    majors = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if not root:
            continue
        base = os.path.join(root, r"Google\Chrome\Application")
        for d in glob.glob(os.path.join(base, "*")):
            m = re.match(r"(\d+)\.\d", os.path.basename(d))
            if m and os.path.isdir(d):
                majors.append(int(m.group(1)))
    return max(majors) if majors else None
