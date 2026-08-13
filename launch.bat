@echo off
net session >nul 2>&1
if %errorLevel% == 0 goto :run

echo Requesting administrator privileges...
powershell -Command "Start-Process '%~f0' -ArgumentList '%*' -Verb RunAs -Wait"
exit

:run
echo TEST > C:\test_log.txt
cd /d "%~dp0"
echo [%date% %time%] Starting >> C:\test_log.txt
python -m Documents.OS_proj.OS.master %* >> C:\test_log.txt 2>&1
echo [%date% %time%] Exited with code %errorLevel% >> C:\test_log.txt
echo.
echo Done. Check C:\test_log.txt
pause