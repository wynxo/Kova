@echo off
rem Reinstall Kova from this checkout: reinstall.bat
rem Stops the server, removes the installed kova command, and installs it again
rem from this folder. Your chats, memory and settings (%USERPROFILE%\.kova) are kept.
setlocal
cd /d "%~dp0"

where kova >nul 2>nul
if errorlevel 1 (
    echo - No installed kova command found; skipping the uninstall step.
) else (
    echo - Stopping Kova and removing the installed command ^(your data is kept^) ...
    kova uninstall --keep-data
    if errorlevel 1 (
        echo Could not reinstall Kova: uninstall failed; your data was kept.
        exit /b 1
    )
    rem The uninstall finishes in the background once kova.exe is no longer running.
    timeout /t 5 /nobreak >nul
)

echo - Installing Kova from this folder ...
call "%~dp0install.bat" --dev
if errorlevel 1 (
    echo Could not reinstall Kova: install failed. Run install.bat to see why.
    exit /b 1
)
echo Done. Your chats and memory are still in %USERPROFILE%\.kova.
echo Run kova to chat, kova-app for the desktop app, or kova-web in your browser.
endlocal & exit /b 0
