@echo off
rem ==========================================================================
rem  kimi_orchestrator - full lead-gen chain (PHASE 1 + PHASE 2).
rem    Double-click       -> interactive agent (talk to it in Russian).
rem    With a request arg -> one-shot, e.g.:
rem        run_kimi_orchestrator.cmd "collect 10 mining, dry-run"
rem
rem  Every LLM stage uses the SAME Kimi K2.7 key/model:
rem    NL controller + deep research extract + five research roles +
rem    two DOCX writers + one-pager PDF.
rem
rem  Per company: 2 neutral .docx + one-pager .pdf, all on Kimi -> storage.
rem  Chrome during scraping is HIDDEN (offscreen).
rem  NOTE: keep this file ASCII + CRLF - cmd.exe garbles UTF-8/LF batch files.
rem ==========================================================================
chcp 65001 >nul
set PYTHONUTF8=1

rem Use the real main Python, not the Windows py launcher (it may have no registered runtime).
if not defined ORQ_MAIN_PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "ORQ_MAIN_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined ORQ_MAIN_PY (
    echo [ERROR] Main Python 3.12 was not found. Set ORQ_MAIN_PY to python.exe.
    exit /b 5
)
"%ORQ_MAIN_PY%" -c "import docx, openai; assert docx.__version__ and openai.__version__" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python-docx or openai is missing in %ORQ_MAIN_PY%.
    echo Install project requirements before launch.
    exit /b 6
)

if /I "%~1"=="oil28" goto oil28

rem --- Single Kimi runtime. Key: KIMI_API_KEY, fallback GPLLM_API_KEY ---
if not defined KIMI_API_KEY set "KIMI_API_KEY=%GPLLM_API_KEY%"
if not defined KIMI_BASE_URL set "KIMI_BASE_URL=https://gpllmkeeper.dtc.tatar/v1"
if not defined KIMI_MODEL_NAME set "KIMI_MODEL_NAME=kimi-k2.7-code"
set "DR_LLM_PROVIDER=kimi"
set "ORQ_KIMI_ONLY=1"

if not defined KIMI_API_KEY (
    echo [ERROR] No Kimi key: neither KIMI_API_KEY nor GPLLM_API_KEY is set.
    echo         Set the key and reopen the console.
    exit /b 7
)

title Kimi K2.7 Lead Orchestrator - end to end
"%ORQ_MAIN_PY%" "%~dp0orchestrator_agent.py" %*
echo.
echo --- agent finished, press any key to close ---
pause >nul
exit /b %errorlevel%

:oil28
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_kimi_oil28.ps1"
exit /b %errorlevel%
