@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Urban Crisis: tetbiq + AYRICA Xarici Xerite API (real kesinti demosu) ===
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  %PY% -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
echo [1/2] Xarici Xerite API ayrica pencerede: http://127.0.0.1:8001
echo      Bu pencereni BAGLASANIZ - real API kesintisi bas verir, sistem ehtiyat rejime kecir.
start "Xarici Xerite API (bagla = kesinti)" .venv\Scripts\python -m uvicorn external_api.main:app --host 127.0.0.1 --port 8001
timeout /t 3 >nul
echo [2/2] Esas tetbiq: http://localhost:8000
set UCE_EXTERNAL_API_URL=http://127.0.0.1:8001
start "" /b cmd /c "timeout /t 3 >nul && start http://localhost:8000"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof
:error
echo XETA: Python 3.10+ qurasdirildigini yoxlayin: https://www.python.org/downloads/
pause
