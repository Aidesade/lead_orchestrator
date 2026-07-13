@echo off
rem ==========================================================================
rem  kimi_orchestrator - full lead-gen chain (PHASE 1 + PHASE 2).
rem    Double-click       -> interactive agent (talk to it in Russian).
rem    With a request arg -> one-shot, e.g.:
rem        run_kimi_orchestrator.cmd "collect 10 mining, dry-run"
rem
rem  Difference from run_orchestrator.cmd: this one explicitly sets the Kimi
rem  provider env used by STAGE 3 (one-pager .pdf). Without a key that stage is
rem  silently skipped and the company gets only the two .docx.
rem
rem  Per company: 2 neutral .docx (Claude) + one-pager .pdf (Kimi) -> Yandex Disk.
rem  Chrome during scraping is HIDDEN (offscreen).
rem  NOTE: keep this file ASCII + CRLF - cmd.exe garbles UTF-8/LF batch files.
rem ==========================================================================
chcp 65001 >nul
set PYTHONUTF8=1

rem --- Kimi env (stage 3). Key: KIMI_API_KEY, fallback GPLLM_API_KEY ---
if not defined KIMI_API_KEY set "KIMI_API_KEY=%GPLLM_API_KEY%"
if not defined KIMI_BASE_URL set "KIMI_BASE_URL=https://gpllmkeeper.dtc.tatar/v1"
if not defined KIMI_MODEL_NAME set "KIMI_MODEL_NAME=kimi-k2.7-code"

if not defined KIMI_API_KEY (
    echo [!] No Kimi key: neither KIMI_API_KEY nor GPLLM_API_KEY is set.
    echo     One-pager stage will be SKIPPED; two .docx are still produced.
    echo     Fix: setx GPLLM_API_KEY "sk-..."  then reopen the console.
    echo.
)

title Kimi Orchestrator (collect + research + one-pager -> Yandex Disk)
py "%~dp0orchestrator_agent.py" %*
echo.
echo --- agent finished, press any key to close ---
pause >nul
