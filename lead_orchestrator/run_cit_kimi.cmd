@echo off
rem Dedicated AO CIT RT run: fresh Kimi research -> exactly two DOCX files.
rem Keep this file ASCII + CRLF for cmd.exe compatibility.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"

if not defined ORQ_MAIN_PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "ORQ_MAIN_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined ORQ_MAIN_PY (
    echo [ERROR] Main Python 3.12 was not found. Set ORQ_MAIN_PY to python.exe.
    exit /b 5
)
"%ORQ_MAIN_PY%" -c "import docx; assert docx.__version__" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python-docx is missing in %ORQ_MAIN_PY%.
    exit /b 6
)

if not defined KIMI_API_KEY set "KIMI_API_KEY=%GPLLM_API_KEY%"
if not defined KIMI_BASE_URL set "KIMI_BASE_URL=https://gpllmkeeper.dtc.tatar/v1"
if not defined KIMI_MODEL_NAME set "KIMI_MODEL_NAME=kimi-k2.7-code"

if not defined KIMI_API_KEY (
    echo [ERROR] KIMI_API_KEY and GPLLM_API_KEY are not set.
    echo Run: setx GPLLM_API_KEY "your-key" and reopen Windows session.
    pause
    exit /b 2
)

set "DR_LLM_PROVIDER=kimi"
set "KIMI_WRITER_AGENT=1"
set "ORQ_RESEARCH_TIMEOUT=0"
set "KIMI_WRITER_AGENT_TIMEOUT=0"
set "ORQ_STORE=local"

if /I "%CIT_KIMI_VERIFY%"=="1" (
    if not exist "%~dp0orchestrator.py" exit /b 3
    if not exist "%~dp0cit_lead.json" exit /b 4
    echo VERIFY OK: CIT Kimi launcher is ready.
    exit /b 0
)

del "D:\orq_cache\findings_1655505808__process.md" 2>nul
del "D:\orq_cache\findings_1655505808__roles.md" 2>nul
del "D:\orq_cache\enrichment_1655505808.json" 2>nul

title AO CIT RT - Kimi deep research - 2 DOCX
echo Starting fresh Kimi deep research for AO CIT RT...
echo Output: D:\deliverables\1655505808
echo.

"%ORQ_MAIN_PY%" "%~dp0orchestrator.py" "%~dp0cit_lead.json" --model kimi --no-presentation --redo
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo DONE: two DOCX files are in D:\deliverables\1655505808
    explorer "D:\deliverables\1655505808"
) else (
    echo FAILED: orchestrator exit code %RC%.
)
echo Press any key to close.
pause >nul
exit /b %RC%
