@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Не найдено окружение .venv.
    echo Создай его командами:
    echo     py -3 -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -u tools\console.py

echo.
echo Мост остановлен. Код выхода: %ERRORLEVEL%
pause