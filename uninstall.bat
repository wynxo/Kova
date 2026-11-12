@echo off
rem Keep the standalone and installed-command uninstall behavior consistent.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
exit /b %errorlevel%
