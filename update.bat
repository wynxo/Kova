@echo off
rem Update Kova from this checkout: update.bat
rem Pulls the latest code (fast-forward only), refreshes the dependencies, and
rem updates the installed kova command too if you have one.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if not errorlevel 1 goto have_uv
if not exist "%USERPROFILE%\.local\bin\uv.exe" goto no_uv
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

:have_uv
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo Could not update Kova: this is not a git checkout. Run: kova update
    exit /b 1
)
set "DIRTY="
for /f "delims=" %%i in ('git status --porcelain --untracked-files=no') do set "DIRTY=1"
if defined DIRTY (
    echo Could not update Kova: you have local changes that would block the pull. Commit or stash them, then run update.bat again.
    exit /b 1
)

echo - Pulling the latest Kova ...
git pull --ff-only >nul 2>nul
if errorlevel 1 (
    echo Could not update Kova: git pull failed ^(no upstream, offline, or your branch has diverged^). Try: git pull --ff-only
    exit /b 1
)
echo + Pulled the latest code.

echo - Refreshing dependencies ...
uv sync >nul 2>nul
if errorlevel 1 (
    echo Could not update Kova: uv sync failed. Run "uv sync" to see why.
    exit /b 1
)
echo + Dependencies are current.

rem If Kova is also installed as a uv tool, update that copy too.
where kova >nul 2>nul
if not errorlevel 1 (
    echo - Updating the installed kova command ...
    kova update
    if errorlevel 1 (
        echo Could not update Kova: the installed kova command could not be updated. Try: kova update
        exit /b 1
    )
)

rem Keep the desktop app files current, so kova-app runs this checkout's desktop\.
set "KOVA_DATA=%USERPROFILE%\.kova"
if defined KOVA_HOME set "KOVA_DATA=%KOVA_HOME%"
if exist "%~dp0desktop\package.json" (
    echo - Refreshing the desktop app files ...
    robocopy "%~dp0desktop" "%KOVA_DATA%\desktop" /E /XD node_modules dist /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
    if errorlevel 8 (
        echo Could not update Kova: copying the desktop app files failed.
        exit /b 1
    )
    echo + Desktop app files: %KOVA_DATA%\desktop
)

echo Done. Run cli.bat to chat, kova-app for the desktop app, or kova-web in your browser.
endlocal & exit /b 0

:no_uv
echo Could not update Kova: uv is not installed. Run install.bat first.
exit /b 1
