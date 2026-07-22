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
if not defined LEAD_SOURCE set "LEAD_SOURCE=rusprofile"
if not defined RUSPROFILE_BROWSER set "RUSPROFILE_BROWSER=playwright"
if not defined RUSPROFILE_COOKIES_FILE set "RUSPROFILE_COOKIES_FILE=%~dp0..\env\rusprofile_cookies.json"
set "DR_LLM_PROVIDER=kimi"
set "ORQ_KIMI_ONLY=1"

"%ORQ_MAIN_PY%" -c "import sys;sys.path.insert(0,r'%~dp0.');from project_env import load_project_env;load_project_env();import kimi_config as k;raise SystemExit(0 if k.api_key() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No KIMI_API_KEY or GPLLM_API_KEY in "%~dp0..\env\.env" or environment.
    echo         Add the key there; the env folder is Git-ignored.
    exit /b 7
)
if /I "%LEAD_SOURCE%"=="ofdata" (
    "%ORQ_MAIN_PY%" -c "import os,sys;sys.path.insert(0,r'%~dp0.');from project_env import load_project_env;load_project_env();raise SystemExit(0 if os.environ.get('OFDATA_API_KEY') else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] No OfData key in "%~dp0..\env\.env".
        echo         Add OFDATA_API_KEY there; the env folder is Git-ignored.
        exit /b 8
    )
)
if /I "%LEAD_SOURCE%"=="rusprofile" (
    if not exist "%RUSPROFILE_COOKIES_FILE%" (
        echo [ERROR] No RusProfile cookie file at "%RUSPROFILE_COOKIES_FILE%".
        echo         Run: py rusprofile_session.py --login
        exit /b 9
    )
    "%ORQ_MAIN_PY%" -c "import playwright.sync_api" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Playwright is missing in %ORQ_MAIN_PY%.
        echo         Install project requirements and Playwright Chromium.
        exit /b 10
    )
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
