@echo off
cd /d "%~dp0"
if not exist "data" mkdir "data"
set "LOG=%~dp0data\update.log"
set "PYEXE=C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYEXE%" set "PYEXE=py"

echo ==================================================
echo   US screener: update and publish
echo ==================================================
echo ==== START ==== >> "%LOG%"

echo [0/3] Seed history from published docs (fresh-clone safety) ...
robocopy "docs\history" "dashboard\history" /E /XC /XN /XO /NJH /NJS /NDL /NFL >nul 2>&1
if exist "..\stock-core\leftside_core" robocopy "..\stock-core\leftside_core" "vendor\leftside_core" /MIR /XD __pycache__ /NJH /NJS /NDL /NFL >nul 2>&1

echo [1/3] Fetch data and score ...
"%PYEXE%" watchdog.py >> "%LOG%" 2>&1

echo [2/3] Copy result to docs ...
copy /Y "dashboard\index.html" "docs\index.html" >nul
copy /Y "dashboard\dashboard_data.js" "docs\dashboard_data.js" >nul
if exist "dashboard\backtest_data.js" copy /Y "dashboard\backtest_data.js" "docs\backtest_data.js" >nul
if exist "dashboard\quality_data.js" copy /Y "dashboard\quality_data.js" "docs\quality_data.js" >nul
if exist "dashboard\sentiment_data.js" copy /Y "dashboard\sentiment_data.js" "docs\sentiment_data.js" >nul
if exist "dashboard\paper_data.js" copy /Y "dashboard\paper_data.js" "docs\paper_data.js" >nul
if exist "dashboard\biweekly_data.js" copy /Y "dashboard\biweekly_data.js" "docs\biweekly_data.js" >nul
if exist "dashboard\watch_data.js" copy /Y "dashboard\watch_data.js" "docs\watch_data.js" >nul
if exist "..\stock-core\dashboard\leftside_shared.js" copy /Y "..\stock-core\dashboard\leftside_shared.js" "dashboard\leftside_shared.js" >nul
if exist "dashboard\leftside_shared.js" copy /Y "dashboard\leftside_shared.js" "docs\leftside_shared.js" >nul
robocopy "dashboard\history" "docs\history" /MIR /NJH /NJS /NDL /NFL >nul 2>&1

echo [3/3] Publish to GitHub Pages ...
git add docs vendor >> "%LOG%" 2>&1
git commit -m "auto update data" >> "%LOG%" 2>&1
git pull --rebase --autostash origin main >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

echo ==== DONE ==== >> "%LOG%"
echo.
echo Done. Wait 1-2 minutes, then open the "US dashboard" desktop shortcut.
echo (log file: data\update.log)
if /I "%~1"=="auto" goto :eof
echo.
pause
