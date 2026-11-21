@echo off
rem Contributor-friendly wrapper; the PowerShell installer also works without a clone.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\install.ps1" %*
exit /b %errorlevel%
