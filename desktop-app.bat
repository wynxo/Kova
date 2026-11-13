@echo off
rem Open Kova's desktop client: desktop-app.bat [--browser]
rem kova-app runs the Electron app in desktop\ (installing its dependencies on
rem first use); --browser opens the same chats in your default browser instead.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if not errorlevel 1 goto run
if not exist "%USERPROFILE%\.local\bin\uv.exe" goto no_uv
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

:run
uv run kova-app %*
endlocal & exit /b %errorlevel%

:no_uv
echo Kova needs uv, and uv is not installed.
echo.
echo Install it with:
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
echo Then run desktop-app.bat again. (install.bat does all of this for you.)
exit /b 1
