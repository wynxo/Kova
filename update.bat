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

echo Done. Run cli.bat to chat.
endlocal & exit /b 0

:no_uv
echo Could not update Kova: uv is not installed. Run install.bat first.
exit /b 1
