@echo off
rem ==========================================================================
rem  lead orchestrator - full lead-gen chain (PHASE 1 + PHASE 2).
rem    Double-click       -> interactive agent (talk to it in Russian).
rem    With a request arg -> one-shot, e.g.:
rem        run_kimi_orchestrator.cmd "collect 10 mining, dry-run"
rem
rem  Default LLM runtime is KIMI end to end. Same pipeline as before:
rem    NL controller + deep research extract + five research roles +
rem    two DOCX writers + one-pager PDF.
rem  Set ORQ_LLM_RUNTIME=claude for the explicit Claude Agent SDK fallback
rem  (Claude Code login / ANTHROPIC_API_KEY).
rem
rem  Per company: 2 neutral .docx + one-pager .pdf -> storage.
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

rem --- Runtime selection: Kimi is the default, Claude is the explicit fallback ---
if not defined ORQ_KIMI_ONLY set "ORQ_KIMI_ONLY=0"
if not defined ORQ_LLM_RUNTIME set "ORQ_LLM_RUNTIME=kimi"
if /I "%ORQ_KIMI_ONLY%"=="1" set "ORQ_LLM_RUNTIME=kimi"

rem Kimi endpoint defaults stay exported: harmless for claude, required for kimi.
if not defined KIMI_API_KEY set "KIMI_API_KEY=%GPLLM_API_KEY%"
if not defined KIMI_BASE_URL set "KIMI_BASE_URL=https://gpllmkeeper.dtc.tatar/v1"
if not defined KIMI_MODEL_NAME set "KIMI_MODEL_NAME=kimi-k2.7-code"
if not defined LEAD_SOURCE set "LEAD_SOURCE=rusprofile"
if not defined RUSPROFILE_BROWSER set "RUSPROFILE_BROWSER=playwright"
if not defined RUSPROFILE_COOKIES_FILE set "RUSPROFILE_COOKIES_FILE=%~dp0..\..\env\rusprofile_cookies.json"
if /I "%ORQ_LLM_RUNTIME%"=="kimi" (
    set "DR_LLM_PROVIDER=kimi"
) else (
    if not defined DR_LLM_PROVIDER set "DR_LLM_PROVIDER=claude"
)

if /I not "%ORQ_LLM_RUNTIME%"=="kimi" goto runtime_ready
"%ORQ_MAIN_PY%" -c "import sys;sys.path.insert(0,r'%~dp0..\app');from project_env import load_project_env;load_project_env();import kimi_config as k;raise SystemExit(0 if k.api_key() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No KIMI_API_KEY or GPLLM_API_KEY in "%~dp0..\..\env\.env" or environment.
    echo         Add the key there; the env folder is Git-ignored.
    exit /b 7
)
:runtime_ready
if /I not "%ORQ_LLM_RUNTIME%"=="claude" goto claude_ready
"%ORQ_MAIN_PY%" -c "import claude_agent_sdk" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] claude-agent-sdk is missing in %ORQ_MAIN_PY%.
    echo         Install project requirements: pip install -r requirements.txt
    exit /b 7
)
:claude_ready
if /I "%LEAD_SOURCE%"=="ofdata" (
    "%ORQ_MAIN_PY%" -c "import os,sys;sys.path.insert(0,r'%~dp0..\app');from project_env import load_project_env;load_project_env();raise SystemExit(0 if os.environ.get('OFDATA_API_KEY') else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] No OfData key in "%~dp0..\..\env\.env".
        echo         Add OFDATA_API_KEY there; the env folder is Git-ignored.
        exit /b 8
    )
)
if /I "%LEAD_SOURCE%"=="rusprofile" (
    if not exist "%RUSPROFILE_COOKIES_FILE%" (
        echo [ERROR] No RusProfile cookie file at "%RUSPROFILE_COOKIES_FILE%".
        echo         Run from app: py rusprofile_session.py --login
        exit /b 9
    )
    "%ORQ_MAIN_PY%" -c "import playwright.sync_api" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Playwright is missing in %ORQ_MAIN_PY%.
        echo         Install project requirements and Playwright Chromium.
        exit /b 10
    )
)

title Lead Orchestrator [%ORQ_LLM_RUNTIME%] - end to end
"%ORQ_MAIN_PY%" "%~dp0..\app\orchestrator_agent.py" %*
echo.
echo --- agent finished, press any key to close ---
pause >nul
exit /b %errorlevel%

:oil28
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_kimi_oil28.ps1"
exit /b %errorlevel%
