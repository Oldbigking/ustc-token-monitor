@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
echo Starting USTC token monitor ... http://127.0.0.1:8790
python monitor.py --serve
pause
