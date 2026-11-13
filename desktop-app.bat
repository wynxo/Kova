@echo off
rem Open Kova's desktop client: desktop-app.bat [--browser]
setlocal
cd /d "%~dp0"

if "%~1"=="--browser" goto python
where npm >nul 2>nul
if errorlevel 1 goto python
if not exist desktop\package.json goto python
cd desktop
if exist node_modules\.bin\electron.cmd goto electron
call npm ci
if errorlevel 1 (
    cd ..
    goto python
)
:electron
set ELECTRON_RUN_AS_NODE=
call npm start -- %*
exit /b %errorlevel%
:python
where uv >nul 2>nul
if not errorlevel 1 goto run
if not exist "%USERPROFILE%\.local\bin\uv.exe" goto no_uv
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto run

:no_uv
echo Kova needs uv, and uv is not installed.
echo.
echo Install it with:
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
echo Then run desktop-app.bat again. (install.bat does all of this for you.)
exit /b 1

:run
if "%~1"=="" (
    uv run kova-app
) else (
    uv run kova-app %*
)
endlocal & exit /b %errorlevel%
