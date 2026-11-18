@echo off
setlocal
for /f "tokens=2 delims=:." %%a in ('chcp') do set "_OLDCP=%%a"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
pushd "%~dp0"
set "PYCMD="
if exist "%~dp0runtime\python.exe" set PYCMD="%~dp0runtime\python.exe"
if not defined PYCMD py -3 -c "import sys" >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys" >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
  echo Python was not found. The "runtime" folder is missing and no Python 3 is installed.
  set "RC=2"
  goto :end
)
rem MEISHI_NOPAUSE is set by tests and unattended runs: skip the review screen.
set "MODE=review"
if defined MEISHI_NOPAUSE set "MODE=run"
%PYCMD% -X utf8 -m app %MODE% %*
set "RC=%ERRORLEVEL%"
:end
popd
if not defined MEISHI_NOPAUSE pause
chcp %_OLDCP% >nul
exit /b %RC%
