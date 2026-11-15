@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Urban Crisis ^& Crowd Evacuation Engine ===
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo [1/3] Virtual muhit yaradilir...
  %PY% -m venv .venv || goto :error
)
echo [2/3] Asililiqlar qurasdirilir...
.venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
echo [3/3] Server ise salinir: http://localhost:8000
start "" /b cmd /c "timeout /t 3 >nul && start http://localhost:8000"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof
:error
echo.
echo XETA: Python 3.10+ qurasdirildigini yoxlayin: https://www.python.org/downloads/
pause
