@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\ClassBot.lnk'); $s.TargetPath = '%~dp0start.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()"
if errorlevel 1 (
  echo Не получилось добавить в автозагрузку.
) else (
  echo Готово: бот будет запускаться сам при входе в Windows.
  echo Убрать из автозагрузки: Win+R, shell:startup, удалить ClassBot.
)
pause
