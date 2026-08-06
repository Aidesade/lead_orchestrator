@echo off
rem ==========================================================================
rem  outreach pipeline - collect -> find LPR mailbox -> letter from @tatar.ru.
rem    Double-click       -> interactive menu (report / drafts / real send).
rem    With args          -> one-shot, e.g.:
rem        run_outreach.cmd --industries mining --count 10
rem        run_outreach.cmd "D:\leads\leads_mining.json" --send --limit 5
rem
rem  DRAFTS BY DEFAULT. Real sending needs an explicit --send, and the menu
rem  asks for a typed confirmation before it.
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

title Outreach - collect, find LPR mailbox, letter from @tatar.ru

rem --- explicit arguments win over the menu ---
if not "%~1"=="" goto direct

:menu
echo.
echo ==========================================================
echo   Outreach pipeline
echo ==========================================================
echo   [1] Report only - who was contacted, where runs stopped
echo       (changes nothing, no network, no mail)
echo   [2] Collect companies from RusProfile -^> DRAFT letters
echo   [3] Use a ready leads JSON            -^> DRAFT letters
echo   [4] SEND letters for real             (asks to type SEND)
echo   [5] Preconditions check: Outlook mailbox and verifier
echo   [0] Exit
echo.
set "pick="
set /p "pick=Choice [1]: "
if not defined pick set "pick=1"
if "%pick%"=="1" goto report
if "%pick%"=="2" goto collect
if "%pick%"=="3" goto fromjson
if "%pick%"=="4" goto sendmode
if "%pick%"=="5" goto precheck_only
if "%pick%"=="0" exit /b 0
echo Unknown choice "%pick%".
goto menu

:report
"%ORQ_MAIN_PY%" "%~dp0outreach.py" --check
goto done

:precheck_only
call :warnings
"%ORQ_MAIN_PY%" "%~dp0outlook_send.py" --check
goto done

:collect
call :requirements
if errorlevel 1 goto done
call :warnings
echo.
echo Industry keys: mining, energy, construction, processing, manufacturing,
echo transport, agriculture, ict, opk, water, trade ... (21 in total)
echo Full list: py -c "import source_rusprofile as R; print(*sorted(R.INDUSTRY))"
set "inds="
set /p "inds=Industries (comma separated) [mining]: "
if not defined inds set "inds=mining"
set "howmany="
set /p "howmany=How many companies in total [10]: "
if not defined howmany set "howmany=10"
echo.
echo Running: --industries %inds% --count %howmany%  (letters go to Drafts)
"%ORQ_MAIN_PY%" "%~dp0outreach.py" --industries %inds% --count %howmany%
goto done

:fromjson
call :requirements
if errorlevel 1 goto done
call :warnings
set "leadsfile="
set /p "leadsfile=Path to leads JSON: "
if not defined leadsfile (
    echo No path given.
    goto done
)
"%ORQ_MAIN_PY%" "%~dp0outreach.py" %leadsfile%
goto done

:sendmode
call :requirements
if errorlevel 1 goto done
call :warnings
echo.
echo   *** REAL SENDING ***
echo   Letters cannot be recalled. Review the drafts in Outlook first.
echo   Sender resolved from OUTREACH_FROM="%OUTREACH_FROM%":
"%ORQ_MAIN_PY%" "%~dp0outlook_send.py" --check
if errorlevel 1 (
    echo   Sender mailbox is not available - nothing to send from.
    goto done
)
echo.
set "leadsfile="
set /p "leadsfile=Path to leads JSON (empty = collect from RusProfile): "
set "howmany="
set /p "howmany=Limit - how many letters at most [1]: "
if not defined howmany set "howmany=1"
set "sure="
set /p "sure=Type SEND in capitals to confirm: "
if not "%sure%"=="SEND" (
    echo Cancelled - nothing was sent.
    goto done
)
if defined leadsfile (
    "%ORQ_MAIN_PY%" "%~dp0outreach.py" %leadsfile% --send --limit %howmany%
) else (
    "%ORQ_MAIN_PY%" "%~dp0outreach.py" --industries mining --count %howmany% --send --limit %howmany%
)
goto done

:direct
call :requirements
if errorlevel 1 goto done
call :warnings
"%ORQ_MAIN_PY%" "%~dp0outreach.py" %*
goto done

rem ---------------------------------------------------------------- helpers --
:requirements
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
exit /b 0

:warnings
if not defined EMAIL_VERIFIER_URL (
    echo [WARN] EMAIL_VERIFIER_URL is not set - mailbox existence cannot be proven.
    echo        On the CIT RT host: python email_verify.py --serve 8080
    echo        Here:               set EMAIL_VERIFIER_URL=http://HOST:8080
    echo        Or pass --no-verify-server to accept unverified guesses.
)
if not exist "%RUSPROFILE_COOKIES_FILE%" (
    echo [WARN] No RusProfile cookie file at "%RUSPROFILE_COOKIES_FILE%".
    echo        Fresh collection will fail; a ready leads JSON still works.
    echo        Login once: py rusprofile_session.py --login
)
exit /b 0

:done
echo.
echo --- finished, press any key to close ---
pause >nul
exit /b %errorlevel%
