@echo off
chcp 65001 >nul
title DealHunter - Gifts scan (Tonnel)
:loop
cd /d "%~dp0"
"C:\Users\PC\AppData\Local\Temp\opencode\dh-venv\Scripts\python.exe" gifts_scan.py
echo.
echo === Пауза 15 минут до следующего скана подарков... ===
timeout /t 900 /nobreak >nul
goto loop
