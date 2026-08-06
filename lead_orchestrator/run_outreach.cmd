@echo off
rem ==========================================================================
rem  outreach pipeline - collect -> find LPR mailbox -> letter from @tatar.ru.
rem    Double-click       -> stage 8 report only (safe, changes nothing).
rem    With args          -> full run, e.g.:
rem        run_outreach.cmd --industries mining --count 10
rem        run_outreach.cmd "D:\leads\leads_mining.json" --send --limit 5
rem
rem  DRAFTS BY DEFAULT. Mail is only really sent with an explicit --send.
rem  Mailbox verification (stage 4) runs on the CIT RT host where PTR and SPF
rem  are configured: start "python email_verify.py --serve 8080" there and set
rem  EMAIL_VERIFIER_URL here.
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

rem Letter stage runs on the Claude Agent SDK in the MAIN environment.
if not defined ORQ_LLM_RUNTIME set "ORQ_LLM_RUNTIME=claude"
if not defined LEAD_SOURCE set "LEAD_SOURCE=rusprofile"
if not defined RUSPROFILE_BROWSER set "RUSPROFILE_BROWSER=playwright"
if not defined RUSPROFILE_COOKIES_FILE set "RUSPROFILE_COOKIES_FILE=%~dp0..\env\rusprofile_cookies.json"
if not defined OUTREACH_FROM set "OUTREACH_FROM=tatar.ru"

rem --- stage 8 only: no preconditions needed, nothing is changed ---
if "%~1"=="" goto report

"%ORQ_MAIN_PY%" -c "import claude_agent_sdk" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] claude-agent-sdk is missing in %ORQ_MAIN_PY%.
    echo         Install project requirements: pip install -r requirements.txt
    exit /b 7
)
"%ORQ_MAIN_PY%" -c "import win32com.client" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pywin32 is missing in %ORQ_MAIN_PY% - Outlook cannot be driven.
    echo         Install project requirements: pip install -r requirements.txt
    exit /b 8
)
if not defined EMAIL_VERIFIER_URL (
    echo [WARN] EMAIL_VERIFIER_URL is not set - mailbox existence cannot be proven.
    echo        On the CIT RT host: python email_verify.py --serve 8080
    echo        Here:               set EMAIL_VERIFIER_URL=http://HOST:8080
    echo        Run anyway with --no-verify-server to send to unverified guesses.
)
if not exist "%RUSPROFILE_COOKIES_FILE%" (
    echo [WARN] No RusProfile cookie file at "%RUSPROFILE_COOKIES_FILE%".
    echo        Fresh collection will fail; a ready leads JSON still works.
    echo        Login once: py rusprofile_session.py --login
)

title Outreach [%ORQ_LLM_RUNTIME%] - drafts unless --send
"%ORQ_MAIN_PY%" "%~dp0outreach.py" %*
goto done

:report
title Outreach - stage 8 report
"%ORQ_MAIN_PY%" "%~dp0outreach.py" --check

:done
echo.
echo --- finished, press any key to close ---
pause >nul
exit /b %errorlevel%
