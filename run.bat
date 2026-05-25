@echo off
cd /d "%~dp0"

:: Install Flask if not present
python -c "import flask" 2>nul || (
    echo Installing Flask...
    pip install flask
)

echo.
echo  Sky Action starting at http://127.0.0.1:5000
echo  Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:5000
python app.py
pause
