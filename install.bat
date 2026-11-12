@echo off
rem Contributor-friendly wrapper; the PowerShell installer also works without a clone.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %errorlevel%
