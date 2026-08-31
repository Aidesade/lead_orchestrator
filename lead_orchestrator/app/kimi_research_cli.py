# -*- coding: utf-8 -*-
"""Read-only веб-инструменты для Kimi-субагентов.

Скрипт запускается отдельным процессом ОСНОВНОГО venv из изолированного Kimi-venv.
Так Kimi получает поиск/чтение/краул поверх deep_research_engine, но несовместимые
kimi-agent-sdk и claude-agent-sdk никогда не импортируются одним интерпретатором.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import deep_research_engine as DRE


PAGE_CHARS = 12000
CRAWL_PAGE_CHARS = 5000


def _search_text(rows: list[dict]) -> str:
    if not rows:
        return "Поиск не вернул результатов. Попробуй другой запрос."
    out = []
    for i, row in enumerate(rows, 1):
        out.append(
            f"{i}. {row.get('title') or '(без заголовка)'}\n"
            f"URL: {row.get('url') or ''}\n"
            f"Сниппет: {row.get('snippet') or ''}"
        )
    return "\n\n".join(out)


def _page_text(page: dict | None, url: str) -> str:
    if not page:
        return f"Страница не прочитана: {url}"
    text = (page.get("markdown") or "")[:PAGE_CHARS]
    return f"URL: {page.get('url') or url}\nИсточник: {page.get('source') or '?'}\n\n{text}"


def _crawl_text(pages: list[dict], domain: str) -> str:
    if not pages:
        return f"{domain}: обход не собрал ни одной страницы."
    out = [f"Обход {domain}: {len(pages)} страниц."]
    for page in pages:
        out.append(
            f"\n--- {page.get('url') or ''}\n"
            f"{(page.get('markdown') or '')[:CRAWL_PAGE_CHARS]}"
        )
    return "\n".join(out)


async def _run(args) -> str:
    if args.command == "search":
        return _search_text(await DRE.web_search(args.query, args.limit))
    if args.command == "fetch":
        DRE._validate_public_http_url(args.url)
        return _page_text(await DRE.fetch_page(args.url), args.url)
    if args.command == "crawl":
        target = args.domain if "://" in args.domain else "https://" + args.domain
        DRE._validate_public_http_url(target)
        # Модель управляет доменом в yolo-режиме, поэтому browser crawl здесь запрещён:
        # JS/subresources/redirects браузера нельзя надёжно пропустить через stdlib SSRF guard.
        # Основной pipeline может пользоваться Crawl4AI отдельно; Kimi tools всегда HTTP-only.
        os.environ["DR_USE_CRAWL4AI"] = "0"
        try:
            keywords = json.loads(args.keywords)
        except json.JSONDecodeError as exc:
            raise ValueError(f"keywords должен быть JSON-массивом: {exc}") from exc
        if not isinstance(keywords, list):
            raise ValueError("keywords должен быть JSON-массивом")
        pages = await DRE.SiteCrawler(
            max_pages=max(1, min(args.max_pages, 15)),
            keywords=[str(x) for x in keywords if str(x).strip()],
        ).crawl_sections(target)
        return _crawl_text(pages, target)
    raise ValueError(f"неизвестная команда: {args.command}")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Read-only ресёрч-инструменты Kimi-субагентов")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=6)

    p = sub.add_parser("fetch")
    p.add_argument("--url", required=True)

    p = sub.add_parser("crawl")
    p.add_argument("--domain", required=True)
    p.add_argument("--keywords", default="[]")
    p.add_argument("--max-pages", type=int, default=8)
    return ap


def _selftest() -> None:
    search = _search_text([{"title": "T", "url": "https://example.test", "snippet": "S"}])
    assert "https://example.test" in search and "Сниппет: S" in search
    page = _page_text({"url": "https://example.test/a", "markdown": "body", "source": "http"}, "")
    assert "body" in page and "Источник: http" in page
    crawl = _crawl_text([{"url": "https://example.test", "markdown": "home"}], "example.test")
    assert "1 страниц" in crawl and "home" in crawl
    assert DRE._validate_public_http_url("https://8.8.8.8", resolve=False)
    for blocked in ("file:///etc/passwd", "http://127.0.0.1", "http://169.254.169.254",
                    "http://user:pass@example.com"):
        try:
            DRE._validate_public_http_url(blocked, resolve=False)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {blocked}")
    print("selftest passed")


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    args = _parser().parse_args(argv)
    try:
        print(asyncio.run(_run(args)))
        return 0
    except Exception as exc:  # noqa: BLE001 — ошибка должна доехать до Kimi как результат тула
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
