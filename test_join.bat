@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Тест захода на встречу
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите 1_install.bat
  pause
  exit /b 1
)
set /p "URL=Вставьте ссылку на Google Meet и нажмите Enter: "
".venv\Scripts\python.exe" main.py join "%URL%" --minutes 3
pause
