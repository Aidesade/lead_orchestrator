@echo off
rem Launcher for the orchestrator SDK agent (orchestrator_agent.py).
rem   Double-click       -> interactive agent (talk to it in natural language).
rem   With a request arg -> one-shot, e.g.:
rem       run_orchestrator.cmd "собери 10 по mining, dry-run"
rem Full chain: collect leads (RusProfile) + per-company dossier/strategy .docx -> Yandex Disk.
rem Chrome during scraping is HIDDEN (offscreen) by default.
chcp 65001 >nul
set PYTHONUTF8=1
title Lead Orchestrator Agent (sbor + research -> Yandex Disk)
py "%~dp0orchestrator_agent.py" %*
echo.
echo --- agent finished, press any key to close ---
pause >nul
