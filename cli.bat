@echo off
rem Run Kova straight from this checkout, no install step: cli.bat [command...]
rem With no command it starts chat. Everything else is passed to kova.
setlocal
cd /d "%~dp0"

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
echo Then run cli.bat again. (install.bat does all of this for you.)
exit /b 1

:run
if "%~1"=="" (
    uv run kova
) else (
    uv run kova %*
)
endlocal & exit /b %errorlevel%
