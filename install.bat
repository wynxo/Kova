@echo off
rem Install Kova on Windows: uv, its dependencies, and the kova command on PATH.
rem Safe to run again: it updates what is already there.
setlocal
cd /d "%~dp0"

echo Kova installer
echo.

where uv >nul 2>nul
if not errorlevel 1 goto have_uv
if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    goto have_uv
)

echo uv is not installed. Installing it now from https://astral.sh/uv ...
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv's installer ran, but uv still was not found.
    echo Close this window, open a new one, and run install.bat again.
    exit /b 1
)
echo Installed uv.

:have_uv
echo.
echo Installing Kova's dependencies (uv sync) ...
uv sync
if errorlevel 1 exit /b 1

echo.
echo Installing the kova command ...
uv tool install --editable . --force
if errorlevel 1 exit /b 1

echo.
echo Done. Now run: kova chat
echo First time? Point Kova at a model server: kova model add NAME URL MODEL
endlocal
