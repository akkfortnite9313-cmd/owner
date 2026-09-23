@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка ClassBot из исходников ===
echo (Проще скачать готовый ClassBot.exe - см. README.md)
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Не найден Python. Установите его с https://www.python.org/downloads/
  echo При установке отметьте галочку "Add python.exe to PATH", потом запустите этот файл снова.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Создаю окружение...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)
echo Устанавливаю зависимости...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Готово. Запускайте ClassBot.bat
pause
exit /b 0

:fail
echo Что-то пошло не так, смотрите сообщения выше.
pause
exit /b 1
