@echo off
rem osk-system CLI launcher. Runs osk.cli with this vault's own Python and the engine on PYTHONPATH.
rem   osk-cli validate | status | search "words" | update --apply | ...
setlocal
set "PYTHONPATH=%~dp0_governance\_engine"
"%~dp0.venv\Scripts\python.exe" -m osk.cli %*
