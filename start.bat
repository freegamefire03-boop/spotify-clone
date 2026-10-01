@echo off
setlocal
cd /d "%~dp0"

set "PIDFILE=%~dp0spotify-clone.pid"
set "ERRLOG=%~dp0startup-error.log"

rem ---------------------------------------------------------------- stop old
if exist "%PIDFILE%" (
  for /f "usebackq delims=" %%P in ("%PIDFILE%") do (
    echo stopping previous instance %%P
    taskkill /PID %%P /T /F >nul 2>&1
  )
  rem wait for it to be really gone, otherwise WebView2 profile stays locked
  for /l %%I in (1,1,20) do (
    set "ALIVE="
    for /f "usebackq delims=" %%P in ("%PIDFILE%") do (
      tasklist /FI "PID eq %%P" 2>nul | findstr /I "%%P" >nul && set "ALIVE=1"
    )
    if not defined ALIVE goto :launched
    powershell -NoProfile -Command "Start-Sleep -Milliseconds 250"
  )
)
:launched
if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1
if exist "%ERRLOG%" del /q "%ERRLOG%" >nul 2>&1

rem ------------------------------------------------- find pythonw.exe (no console)
set "PYW="
for /f "usebackq delims=" %%I in (`python -c "import sys,os;p=sys.executable;w=os.path.join(os.path.dirname(p),'pythonw.exe');print(w if os.path.exists(w) else '')"`) do set "PYW=%%I"
if not defined PYW (
  echo ERROR: pythonw.exe not found next to python - cannot launch without a console window
  exit /b 1
)

rem ------------------------------------------------------------- launch the GUI
rem NOTE: no -WindowStyle Hidden anywhere: SW_HIDE makes the pywebview window
rem itself invisible, so the app runs with nothing on screen.
start "" "%PYW%" "%~dp0main.py"

for /l %%I in (1,1,40) do (
  if exist "%PIDFILE%" goto :ready
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 250"
)
echo FAILED: app did not start
if exist "%ERRLOG%" (
  echo --- startup-error.log ---
  type "%ERRLOG%"
) else (
  echo no startup-error.log was written
)
exit /b 1

:ready
echo window launching ^(pid in spotify-clone.pid^) - it should appear now
endlocal