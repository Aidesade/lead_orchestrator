@echo off
rem Canonical desktop launcher: LLM runtime is Claude Agent SDK (ORQ_KIMI_ONLY=1 -> Kimi K2.7).
call "%~dp0run_kimi_orchestrator.cmd" %*
exit /b %errorlevel%
