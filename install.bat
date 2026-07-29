@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Creating the widget virtual environment...
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :error
)
echo Installing pinned dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Done. Launch start_widget.vbs to run the widget.
pause
exit /b 0

:error
echo.
echo Installation failed. Review the messages above and try again.
pause
exit /b 1
