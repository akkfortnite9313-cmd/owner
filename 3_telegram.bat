@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Настройка уведомлений в Telegram
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите 1_install.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py telegram
pause
