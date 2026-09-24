@echo off
REM The daily host-side recurrence collect, as the Scheduled Task runs it.
REM Registered by scripts\schedule_collect.ps1 -Install.
REM
REM A .cmd wrapper rather than putting python straight in the task action, for
REM one reason: the action is then a FILE, editable and readable in the repo,
REM instead of an argument string living only in the task registry where nobody
REM reviews it and no diff shows it changing.
REM
REM Every run appends its outcome to audit\recurrence-collect.log — including
REM failures. A scheduled task that fails silently is the thing this whole
REM register exists to stop, and "LastTaskResult 0" only says the wrapper exited
REM cleanly, not that the collect found anything.

setlocal
cd /d "%~dp0.."
if not exist "audit" mkdir "audit"
set "LOG=audit\recurrence-collect.log"

for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('s')+'Z'"`) do set "TS=%%t"
if not defined AICP_GH_REPO set "AICP_GH_REPO=jgobuilds/ai-control-plane"
REM Under the Task Scheduler there is no console, so python falls back to the
REM locale codepage and writes cp1252 bytes into a log everything else reads as
REM UTF-8. Hand-running it looked fine; the scheduled run did not.
set "PYTHONIOENCODING=utf-8"

echo. >> "%LOG%"
echo === %TS% collect === >> "%LOG%"
python scripts\recurrence.py collect >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo exit=%RC% >> "%LOG%"

REM Report the collect's own exit code to the Task Scheduler, so a failing
REM collector shows as a failing task and not as a green one that wrote a sad
REM line into a log file nobody opens.
endlocal & exit /b %RC%
