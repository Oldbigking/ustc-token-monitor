@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
echo Starting USTC token monitor ... http://127.0.0.1:8790
echo (Ctrl+C to stop; other port: start.bat --port 9000)
python monitor.py --serve %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo [x] start failed, exit code %RC%
  echo     2 = missing API key / port already in use; 1 = poll failed ^(invalid key or platform unreachable^)
  pause
)
exit /b %RC%
