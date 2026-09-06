@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Python орчин үүсгэж байна...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

python reconcile.py
set RC=%ERRORLEVEL%

set REPORT=reports\mismatch_report.xlsx
if exist "%REPORT%" (
    start "" "%REPORT%"
)

echo.
if %RC%==0 (
    echo Дууслаа: зөрүүгүй.
) else (
    echo Дууслаа: зөрүү олдсон тул тайланг нээлээ.
)
pause
