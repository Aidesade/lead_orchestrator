# -*- coding: utf-8 -*-
r"""
HTML → PDF для третьего деливерабла (редакционный one-pager).

Модель по ONEPAGER_SYSTEM выдаёт HTML в каркасе `x-dc` (со `<script src="./support.js">`,
`<x-dc>`, `<helmet>`). Для автономного рендера этот каркас не годится: support.js рядом
нет, а серый фон-обёртка страницы не нужен — нам нужен САМ лист (800px, белый). Поэтому:

  1) из ответа модели вынимаем самодостаточный лист-div (`width:800px`),
  2) встраиваем фото спикера как data-URI (модель не знает пути к файлу — ставит токен),
  3) оборачиваем в минимальный HTML с теми же Google Fonts и рендерим Playwright'ом в PDF.

Playwright + chromium уже стоят в проекте (краул движка) — новых зависимостей нет.
Эта часть НЕ зависит от Kimi/Claude SDK и полностью тестируется офлайн:
    py html_to_pdf.py <input.html> <output.pdf> [--photo path.png]
"""
import asyncio
import base64
import mimetypes
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onepager_system import SPEAKER_PHOTO_TOKEN  # noqa: E402

# Тот же набор шрифтов, что в промпте (Source Serif 4 + Archivo).
_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700'
    '&family=Archivo:wght@400;500;600;700;800&display=swap" rel="stylesheet">'
)


def _strip_fence(raw):
    """Снять markdown-ограждение ```html … ``` вокруг ответа модели, если оно есть."""
    m = re.search(r"```(?:html)?\s*(.+?)```", raw, re.S | re.I)
    return (m.group(1) if m else raw).strip()


def _extract_sheet(html):
    """Вынуть самодостаточный лист — <div …width:800px…> с балансировкой вложенных div.
    None, если маркер листа не найден (тогда используем фолбэк)."""
    idx = html.find("width:800px")
    if idx == -1:
        return None
    start = html.rfind("<div", 0, idx)
    if start == -1:
        return None
    depth = 0
    for m in re.finditer(r"<(/?)div\b", html[start:], re.I):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            end = html.find(">", start + m.end())
            return html[start:end + 1] if end != -1 else None
    return None


def _embed_photo(html, photo_path):
    """Заменить токен фото спикера на data-URI реального файла. Нет файла -> оставляем
    как есть (в PDF будет пустой <img> — не критично, лист не ломается)."""
    if SPEAKER_PHOTO_TOKEN not in html:
        return html
    if not (photo_path and os.path.exists(photo_path)):
        return html
    data = base64.b64encode(open(photo_path, "rb").read()).decode("ascii")
    mime = mimetypes.guess_type(photo_path)[0] or "image/png"
    return html.replace(SPEAKER_PHOTO_TOKEN, f"data:{mime};base64,{data}")


def prepare_html(raw, photo_path=None):
    """Ответ модели -> готовый к рендеру автономный HTML (лист на белом фоне)."""
    body = _strip_fence(raw)
    sheet = _extract_sheet(body)
    if sheet is None:                         # фолбэк: весь ответ, только убрать support.js
        sheet = re.sub(r'<script[^>]*support\.js[^>]*>\s*</script>', "", body, flags=re.I)
    sheet = _embed_photo(sheet, photo_path)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        + _FONTS
        + '<style>*{box-sizing:border-box;margin:0;padding:0}html,body{background:#fff}</style>'
        + "</head><body>" + sheet + "</body></html>"
    )


# Запас к высоте страницы. Высота листа дробная (напр. 1953.98 px), а размер страницы
# задаётся целым; при конвертации px->дюймы Chromium теряет доли и контент вылезает на
# микроскопическую величину — неразрывный блок футера целиком уезжает на 2-ю страницу.
# Пара пикселей запаса убирает лишнюю страницу и в PDF не видна.
_PAGE_SLACK_PX = 2


async def render_pdf(html, out_path):
    """Автономный HTML -> PDF (лист 800px, высота по факту контента, РОВНО одна страница).
    print_background — обязателен: без него белый фон и линейки в PDF не отрисуются."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            try:
                await page.evaluate("document.fonts.ready")
            except Exception:
                pass
            # Картинки (фото спикера — data-URI) должны быть декодированы ДО замера высоты.
            try:
                await page.evaluate(
                    "() => Promise.all(Array.from(document.images)"
                    ".map(i => i.complete ? null : i.decode().catch(() => null)))"
                )
            except Exception:
                pass
            await page.wait_for_timeout(300)   # дать шрифтам применить метрики до замера высоты
            h = await page.evaluate("() => Math.ceil(document.body.scrollHeight)")
            await page.pdf(
                path=out_path, width="800px", height=f"{max(1, h) + _PAGE_SLACK_PX}px",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
        finally:
            await browser.close()


async def onepager_html_to_pdf(raw, out_path, photo_path=None):
    """Полная стадия рендера: ответ модели -> PDF. Для встраивания в async-оркестратор."""
    await render_pdf(prepare_html(raw, photo_path), out_path)


def _main(argv):
    if len(argv) < 2:
        print("использование: py html_to_pdf.py <input.html> <output.pdf> [--photo path.png]")
        return 2
    inp, out = argv[0], argv[1]
    photo = None
    if "--photo" in argv:
        photo = argv[argv.index("--photo") + 1]
    raw = open(inp, encoding="utf-8").read()
    asyncio.run(onepager_html_to_pdf(raw, out, photo))
    size = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"PDF: {out} ({size} байт)")
    return 0 if size > 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
