@echo off
setlocal
for /f "tokens=2 delims=:." %%a in ('chcp') do set "_OLDCP=%%a"
chcp 65001 >nul
pushd "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\setup_runtime.ps1" -Root "%~dp0."
set "RC=%ERRORLEVEL%"
popd
if not defined MEISHI_NOPAUSE pause
chcp %_OLDCP% >nul
exit /b %RC%
