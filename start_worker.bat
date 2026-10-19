@echo off
chcp 65001 >nul
title DealHunter scanner
cd /d "%~dp0"
"C:\Users\PC\AppData\Local\Temp\opencode\dh-venv\Scripts\python.exe" worker.py
echo.
echo Сканер остановлен. Окно можно закрыть.
pause
