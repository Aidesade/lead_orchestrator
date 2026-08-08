@echo off
rem ==========================================================================
rem  SSH tunnel to the mailbox verifier VM (stage 4 of the outreach pipeline).
rem    Double-click  -> keeps the tunnel up until the window is closed.
rem    With args     -> run_verify_tunnel.cmd verifier@203.0.113.10
rem
rem  The verifier listens on 127.0.0.1:8080 on the VM and is NOT published to
rem  the internet: its HTTP API has no auth, so an open port would mean other
rem  people running SMTP probes on our reputation. This tunnel is the way in.
rem  Then set EMAIL_VERIFIER_URL=http://127.0.0.1:8080 in env\.env.
rem  Setup guide: DEPLOY_VERIFIER.md in the repository root.
rem  NOTE: keep this file ASCII + CRLF - cmd.exe garbles UTF-8/LF batch files.
rem ==========================================================================
setlocal

set "TARGET=%~1"
if not defined TARGET set "TARGET=%VERIFY_SSH%"
if not defined TARGET (
    echo [ERROR] Verifier host is unknown.
    echo         Pass it as an argument:  run_verify_tunnel.cmd verifier@1.2.3.4
    echo         or set VERIFY_SSH=verifier@1.2.3.4 in env\.env
    exit /b 5
)

if not defined VERIFY_LOCAL_PORT set "VERIFY_LOCAL_PORT=8080"
if not defined VERIFY_REMOTE_PORT set "VERIFY_REMOTE_PORT=8080"

where ssh >nul 2>&1
if errorlevel 1 (
    echo [ERROR] ssh.exe not found. Enable "OpenSSH Client" in Windows optional features.
    exit /b 5
)

echo Tunnel: 127.0.0.1:%VERIFY_LOCAL_PORT% -^> %TARGET% : %VERIFY_REMOTE_PORT%
echo Use EMAIL_VERIFIER_URL=http://127.0.0.1:%VERIFY_LOCAL_PORT%
echo Ctrl+C stops the tunnel.
echo.

rem ExitOnForwardFailure: fail loudly if the local port is already taken,
rem otherwise ssh would sit there silently and the pipeline would talk to
rem whatever else listens on 8080.
:loop
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L %VERIFY_LOCAL_PORT%:127.0.0.1:%VERIFY_REMOTE_PORT% %TARGET%
echo [warn] tunnel dropped (exit %errorlevel%) - reconnecting in 10 s
timeout /t 10 /nobreak >nul
goto loop
