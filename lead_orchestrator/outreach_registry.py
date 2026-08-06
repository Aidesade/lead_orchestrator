# -*- coding: utf-8 -*-
r"""Реестр отработанных компаний — стадия 1 и стадия 8 outreach-пайплайна.

Зачем отдельный файл, а не «посмотреть, что лежит на диске»: до появления рассылки
«сделанность» компании определялась только размерами деливераблов, и этого хватало.
Теперь нужен факт, которого на диске нет вообще — **писали мы этой компании или нет**.
Повторное письмо тому же ЛПР через неделю — не «лишний прогон», а испорченный контакт,
поэтому факт отправки хранится явно и переживает перезапуск.

Ключ — ИНН (не название: названия дублируются, у «Татнефть» их три вида написания).
Компания считается ОТРАБОТАННОЙ, если выполнено любое из двух:
  * по ней есть готовые деливераблы (бэкфилл из ``<data>/deliverables/<ИНН>/``);
  * ей уже отправлено письмо (стадия ``sent``).

Запись атомарна (tmp + ``os.replace``) — прогон могут прервать Ctrl+C посреди рассылки,
и половина JSON-файла означала бы потерю всего списка отправленных.

CLI:
  py outreach_registry.py --backfill      # разово втянуть уже сделанные компании
  py outreach_registry.py --list          # что в реестре
  py outreach_registry.py --check         # какая стадия по какой компании пропущена
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERSION = 1

# Порядок стадий, которые пайплайн проходит ПО КАЖДОЙ компании. Стадии 1/4/5 (реестр,
# верификатор, почтовый ящик) сюда не входят: они общие для прогона и проверяются
# прекондишенами до первой компании.
STAGES = (
    ("parsed", "карточка RusProfile разобрана"),
    ("onepager", "one-pager подобран"),
    ("email", "адрес ЛПР найден и проверен"),
    ("letter", "текст письма готов"),
    ("sent", "письмо отправлено"),
)
STAGE_IDS = tuple(sid for sid, _ in STAGES)
STAGE_TITLE = dict(STAGES)

OK, SKIP, FAIL = "ok", "skip", "fail"

# .docx меньше этого размера — заготовка, а не документ. Порог тот же, что у
# orchestrator._local_state: рассинхрон порогов означал бы, что реестр и резюм
# оркестратора расходятся во мнении, сделана компания или нет.
REAL_DOCX_MIN = 5000


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _work_base(subdir):
    """ORQ_DATA_ROOT/<subdir> -> D:\\<subdir> -> %TEMP%\\<subdir>.

    Намеренная копия orchestrator._work_base: тянуть сюда весь orchestrator ради
    четырёх строк нельзя (он поднимает asyncio-пайплайн на импорте). Так же
    поступает web/api/config.py."""
    data_root = (os.environ.get("ORQ_DATA_ROOT") or "").strip()
    if data_root:
        return os.path.join(data_root, subdir)
    root = "D:\\" if os.path.isdir("D:\\") else tempfile.gettempdir()
    return os.path.join(root, subdir)


def registry_path():
    """Путь к файлу реестра; ORQ_OUTREACH_REGISTRY перекрывает."""
    explicit = (os.environ.get("ORQ_OUTREACH_REGISTRY") or "").strip()
    return explicit or os.path.join(_work_base("orq_outreach"), "registry.json")


def deliverables_root():
    """Корень локального хранилища деливераблов — тот же, что читает оркестратор и веб."""
    explicit = (os.environ.get("ORQ_DELIVERABLES_DIR") or "").strip()
    return explicit or _work_base("deliverables")


def _inn_of(lead):
    return str((lead or {}).get("_inn") or (lead or {}).get("inn") or "").strip()


class Registry:
    """Реестр в памяти + атомарное сохранение."""

    def __init__(self, path=None):
        self.path = path or registry_path()
        self.data = self._load()

    # ------------------------------------------------------------ хранение ----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        except Exception as exc:                   # noqa: BLE001 — битый файл не должен ронять прогон
            print(f"[реестр] файл повреждён ({str(exc)[:60]}) — начинаем с пустого, "
                  f"старый не перезаписываем до первой успешной записи")
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("version", VERSION)
        companies = raw.get("companies")
        raw["companies"] = companies if isinstance(companies, dict) else {}
        return raw

    def save(self):
        """Атомарно: сначала во временный файл рядом, потом os.replace."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # -------------------------------------------------------------- записи ----
    @property
    def companies(self):
        return self.data["companies"]

    def get(self, inn):
        return self.companies.get(str(inn).strip())

    def upsert(self, lead):
        """Завести/обновить запись по лиду. Возвращает саму запись."""
        inn = _inn_of(lead)
        if not inn:
            raise ValueError("у лида нет ИНН — в реестр по названию не пишем")
        rec = self.companies.setdefault(inn, {"inn": inn, "stages": {}})
        rec["name"] = lead.get("name") or rec.get("name") or ""
        rec["industry"] = lead.get("_industry") or rec.get("industry") or ""
        for key, field in (("site", "website"), ("phone", "phone")):
            if lead.get(field):
                rec[key] = lead[field]
        for key in ("_phones", "_emails", "_founders"):
            if lead.get(key):
                rec[key.lstrip("_")] = lead[key]
        if lead.get("contact_person"):
            rec["ceo"] = {"fio": lead["contact_person"], "post": lead.get("_ceo_post") or ""}
        rec.setdefault("stages", {})
        return rec

    def mark(self, inn, stage, status=OK, note=""):
        """Отметить стадию. ``skip`` и ``fail`` обязаны нести причину — иначе стадия 8
        покажет «пропущено» без объяснения, а именно за объяснением её и зовут."""
        if stage not in STAGE_IDS:
            raise ValueError(f"неизвестная стадия: {stage}")
        if status in (SKIP, FAIL) and not note:
            raise ValueError(f"стадия {stage}: для статуса {status} нужна причина")
        rec = self.companies.setdefault(str(inn).strip(), {"inn": str(inn).strip(), "stages": {}})
        rec.setdefault("stages", {})[stage] = {"status": status, "at": _now(), "note": note}
        return rec

    def mark_sent(self, inn, to, subject, draft=False, note=""):
        """Факт отправки — отдельным полем, а не только стадией: это единственное
        необратимое действие пайплайна, и его лог не должен зависеть от формата стадий."""
        rec = self.mark(inn, "sent", OK if not draft else SKIP,
                        note=note or ("черновик, не отправлено" if draft else ""))
        rec["sent"] = {"at": _now(), "to": to, "subject": subject, "draft": bool(draft)}
        return rec

    # ------------------------------------------------------------- вопросы ----
    def stage_status(self, inn, stage):
        rec = self.get(inn) or {}
        return ((rec.get("stages") or {}).get(stage) or {}).get("status")

    def was_sent(self, inn):
        """Письмо реально ушло (черновик не считается)."""
        rec = self.get(inn) or {}
        sent = rec.get("sent") or {}
        return bool(sent) and not sent.get("draft")

    def is_worked(self, inn):
        """Отработана = есть деливераблы ИЛИ письмо уже отправлено."""
        rec = self.get(inn) or {}
        return bool(rec.get("deliverables")) or self.was_sent(inn)

    def filter_new(self, leads):
        """Отсеять уже отработанные. Лиды без ИНН пропускаем вперёд: без ИНН мы всё
        равно не сможем ни записать их, ни надёжно опознать повтор."""
        fresh = []
        for lead in leads or []:
            inn = _inn_of(lead)
            if inn and self.is_worked(inn):
                continue
            fresh.append(lead)
        return fresh

    # -------------------------------------------------------------- бэкфилл ---
    def backfill_deliverables(self, root=None, log=print):
        """Разово втянуть компании, по которым уже сделаны материалы.

        Смотрим ровно то же, что резюм оркестратора: два .docx больше REAL_DOCX_MIN
        в папке <deliverables>/<ИНН>/. Имена файлов не проверяем — они зависят от
        названия компании, а оно в папке не хранится."""
        root = root or deliverables_root()
        if not os.path.isdir(root):
            log(f"[реестр] папки деливераблов нет: {root} — бэкфилл пропущен")
            return 0
        added = 0
        for entry in sorted(os.listdir(root)):
            inn = entry.strip()
            # папки вида "1655505808_before_free_agent_20260720" — ручные бэкапы, не компании
            if not inn.isdigit():
                continue
            comp = os.path.join(root, entry)
            if not os.path.isdir(comp):
                continue
            docx = []
            for fname in os.listdir(comp):
                if not fname.lower().endswith(".docx"):
                    continue
                try:
                    if os.path.getsize(os.path.join(comp, fname)) > REAL_DOCX_MIN:
                        docx.append(fname)
                except OSError:
                    continue
            if len(docx) < 2:
                continue
            rec = self.companies.setdefault(inn, {"inn": inn, "stages": {}})
            if not rec.get("deliverables"):
                added += 1
            rec["deliverables"] = True
            rec.setdefault("name", "")
            rec.setdefault("stages", {})
        log(f"[реестр] бэкфилл деливераблов: +{added} компаний (всего в реестре {len(self.companies)})")
        return added

    # -------------------------------------------------------------- стадия 8 --
    def audit(self):
        """По каждой компании: докуда дошли и КАКИЕ стадии пропущены — с причинами.

        Пропущенных стадий может быть несколько: пропуск one-pager, например, прогон
        не останавливает — письмо уходит без вложения. Поэтому возвращается весь
        список, а не первая проблема: иначе отчёт врал бы, что на one-pager всё встало.

        Возвращает список словарей — печать отдельно, чтобы результат можно было
        отдать и в тест, и в лог, и когда-нибудь в веб."""
        rows = []
        for inn, rec in sorted(self.companies.items()):
            stages = rec.get("stages") or {}
            done, missing = [], []
            for sid in STAGE_IDS:
                st = (stages.get(sid) or {}).get("status")
                if st == OK:
                    done.append(sid)
                    continue
                note = (stages.get(sid) or {}).get("note") or ""
                missing.append({
                    "stage": sid,
                    "reason": f"{st}: {note or 'без причины'}" if st else "не выполнялась",
                })
            if not missing:
                state = "готово"
            elif not stages:
                # компания попала в реестр бэкфиллом: материалы по ней есть, но
                # рассылку по ней просто не запускали — это не «застряла»
                state = "ранее" if rec.get("deliverables") else "не начата"
            else:
                state = "частично"
            rows.append({
                "inn": inn,
                "name": rec.get("name") or "",
                "state": state,
                "last_ok": done[-1] if done else None,
                "missing": missing,
                "worked": self.is_worked(inn),
                "sent": self.was_sent(inn),
            })
        return rows


def format_audit(rows):
    """Таблица стадии 8 текстом. Компании, по которым рассылку не запускали,
    сворачиваются в одну строку — иначе двести бэкфилл-записей забьют вывод."""
    if not rows:
        return "[стадия 8] реестр пуст — по компаниям ещё ничего не делали"
    lines = [f"[стадия 8] компаний в реестре: {len(rows)}"]
    counts = {"готово": 0, "частично": 0, "ранее": 0, "не начата": 0}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
        if row["state"] == "готово":
            lines.append(f"  ✓ {row['inn']} {row['name'][:40]}: все стадии пройдены")
        elif row["state"] == "частично":
            last = STAGE_TITLE.get(row["last_ok"], "") if row["last_ok"] else "ничего"
            lines.append(f"  • {row['inn']} {row['name'][:40]}\n      дошли до: {last}")
            for item in row["missing"]:
                lines.append(f"      пропущено: {STAGE_TITLE.get(item['stage'], item['stage'])}"
                             f" — {item['reason']}")
    if counts.get("ранее"):
        lines.append(f"  … {counts['ранее']} компаний с готовыми материалами — "
                     f"рассылка по ним не запускалась")
    if counts.get("не начата"):
        lines.append(f"  … {counts['не начата']} компаний заведены, но не обрабатывались")
    lines.append(f"[стадия 8] пройдено полностью: {counts['готово']}, "
                 f"с пропусками: {counts['частично']}, из {len(rows)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="реестр отработанных компаний outreach-пайплайна")
    ap.add_argument("--backfill", action="store_true", help="втянуть компании с готовыми деливераблами")
    ap.add_argument("--list", action="store_true", help="показать реестр")
    ap.add_argument("--check", action="store_true", help="стадия 8: какая стадия по какой компании пропущена")
    ap.add_argument("--path", default=None, help="путь к файлу реестра")
    a = ap.parse_args()

    reg = Registry(a.path)
    print(f"[реестр] {reg.path}")
    if a.backfill:
        reg.backfill_deliverables()
        reg.save()
    if a.list:
        for inn, rec in sorted(reg.companies.items()):
            flags = []
            if rec.get("deliverables"):
                flags.append("деливераблы")
            if reg.was_sent(inn):
                flags.append(f"письмо {rec['sent']['at']}")
            print(f"  {inn} {(rec.get('name') or '')[:45]:<45} {', '.join(flags) or '—'}")
    if a.check or not (a.backfill or a.list):
        print(format_audit(reg.audit()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
