@echo off
rem Remove the kova command on Windows and, if you say so, your Kova data.
rem Your data is kept unless you confirm deleting it (or pass --purge).
setlocal
cd /d "%~dp0"

if "%KOVA_HOME%"=="" (
    set "KOVA_DIR=%USERPROFILE%\.kova"
) else (
    set "KOVA_DIR=%KOVA_HOME%"
)

set "PURGE=0"
set "KEEP=0"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--purge" set "PURGE=1"
if /i "%~1"=="--keep-data" set "KEEP=1"
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo Unknown option: %~1
exit /b 2

:usage
echo Usage: uninstall.bat [--keep-data ^| --purge]
echo.
echo   --keep-data   stop the server and remove the kova command, keep %KOVA_DIR%
echo   --purge       also delete %KOVA_DIR% without asking
exit /b 0

:parsed
echo Stopping the Kova server (if running) ...
where kova >nul 2>nul
if not errorlevel 1 kova stop >nul 2>nul

echo Removing the kova command ...
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found; nothing to remove there.
) else (
    uv tool uninstall kova >nul 2>nul
    echo The kova command is gone.
)

echo.
if not exist "%KOVA_DIR%" (
    echo Done. Kova is removed.
    exit /b 0
)

if "%PURGE%"=="1" goto delete_data
if "%KEEP%"=="1" goto keep_data

set "answer="
set /p "answer=Delete your Kova data in %KOVA_DIR% (chats, memory, model settings, token)? [y/N] "
if /i "%answer%"=="y" goto delete_data
if /i "%answer%"=="yes" goto delete_data
goto keep_data

:delete_data
rmdir /s /q "%KOVA_DIR%"
echo Deleted %KOVA_DIR%.
goto done

:keep_data
echo Keeping your data in %KOVA_DIR%.

:done
echo.
echo Done. Kova is removed. It still runs from this checkout with cli.bat.
endlocal
