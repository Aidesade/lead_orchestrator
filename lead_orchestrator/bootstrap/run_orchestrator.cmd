@echo off
rem Canonical desktop launcher: current full pipeline, Kimi runtime by default.
call "%~dp0run_kimi_orchestrator.cmd" %*
exit /b %errorlevel%
