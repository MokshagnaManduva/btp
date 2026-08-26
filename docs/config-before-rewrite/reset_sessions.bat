@echo off
REM ---------------------------------------------------------------------------
REM  Make every scenario selectable again in the FPSci menu.
REM
REM  FPSci hides a scenario once you finish it. userstatus.sessions.csv is an
REM  append-only log of "userID,sessionID" for every completed session
REM  (UserStatus.cpp:51-73); it is read at startup and GuiElements.cpp:667-684
REM  drops everything in it from the session drop-down. Emptying the file makes
REM  all scenarios available again, for every user.
REM
REM  FPSci reads this list ONLY at startup, so clearing it while the game is open
REM  changes nothing until you restart FPSci. Easier: use play.bat, which clears
REM  the log and launches the game in one step.
REM
REM  Nothing else lives in this file. Your results .db files are untouched, and
REM  their Sessions table still records everything you played.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "userstatus.sessions.csv" (
    echo No completed-session log found -- every scenario is already selectable.
    exit /b 0
)

copy /y "userstatus.sessions.csv" "userstatus.sessions.csv.bak" >nul 2>&1
type nul > "userstatus.sessions.csv" 2>nul

REM Verify by size rather than errorlevel: a failed redirect prints its own
REM message but does not reliably set errorlevel, so a locked file would
REM otherwise be reported as a success.
for %%A in ("userstatus.sessions.csv") do set "SZ=%%~zA"
if not "%SZ%"=="0" (
    echo FAILED to clear the log -- FPSci is running and locks this file.
    echo Close FPSci and retry, or just use play.bat which does this for you.
    exit /b 1
)

echo Cleared the completed-session log -- every scenario is selectable again.
echo The previous list was backed up to userstatus.sessions.csv.bak
endlocal
