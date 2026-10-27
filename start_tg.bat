@echo off
title DealHunter TG Scanner
cd /d "%~dp0"
echo Запуск TG-сканера барахолок (каждые 15 минут, скан+отправка на сервер). Ctrl+C — стоп.
:loop
"%~dp0.venv\Scripts\python.exe" tg_scan.py --scan --push
timeout /t 900 /nobreak >nul
goto loop
