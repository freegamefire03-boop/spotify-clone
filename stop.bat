@echo off
setlocal
cd /d "%~dp0"

set "PIDFILE=%~dp0spotify-clone.pid"

if exist "%PIDFILE%" (
  for /f "usebackq delims=" %%P in ("%PIDFILE%") do (
    echo stopping %%P
    taskkill /PID %%P /T /F >nul 2>&1
  )
  del /q "%PIDFILE%" >nul 2>&1
) else (
  echo no pid file - nothing to stop
)

endlocal
