@echo off
rem Canonical desktop launcher: the whole LLM path is Kimi K2.7.
call "%~dp0run_kimi_orchestrator.cmd" %*
exit /b %errorlevel%
