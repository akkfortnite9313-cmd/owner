@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Проверка настроек
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите 1_install.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py check
pause
