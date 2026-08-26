@echo off
REM ---------------------------------------------------------------------------
REM  Launch FPSci, then parse your final run after you quit.
REM
REM  OPTIONAL. Double-clicking FirstPersonScience.exe works fine and all four
REM  scenarios will be there. The only thing you lose is the last parse:
REM
REM    exe directly -> your FINAL run of a sitting never reaches the CSVs,
REM                    because the session-start hook only parses the PREVIOUS
REM                    run and nothing starts after your last one. Double-click
REM                    parse_results.bat when you finish and you are level.
REM    play.bat     -> does that last parse for you, automatically.
REM
REM  The disappearing-scenario problem this script was originally written for is
REM  already fixed for good, two ways over: sessions never complete (so
REM  markSessComplete never runs and nothing is appended to
REM  userstatus.sessions.csv), and that file is now empty and read-only anyway.
REM  The clear below is just belt and braces.
REM
REM  TRADE-OFF: this window must stay open while you play. Closing it kills the
REM  running parse -- that is what stranded a lock file once already.
REM
REM  Your data is not affected: results .db files are untouched and their Sessions
REM  table still records every run you played.
REM ---------------------------------------------------------------------------
REM Delayed expansion is required: %SZ% inside the parenthesised block below
REM would be expanded when the block is parsed, i.e. before the for loop sets it.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "FirstPersonScience.exe" (
    echo Cannot find FirstPersonScience.exe next to this script.
    pause
    exit /b 1
)

REM Empty the completed-session log, then mark it READ-ONLY. Empty means nothing
REM is hidden at startup; read-only means FPSci cannot append to it either, so no
REM scenario can ever be removed again no matter how the game is launched. FPSci
REM only logs a warning when it cannot open this file for appending -- harmless.
if exist "userstatus.sessions.csv" (
    attrib -R "userstatus.sessions.csv" >nul 2>&1
    copy /y "userstatus.sessions.csv" "userstatus.sessions.csv.bak" >nul 2>&1
    type nul > "userstatus.sessions.csv" 2>nul
    for %%A in ("userstatus.sessions.csv") do set "SZ=%%~zA"
    if not "!SZ!"=="0" (
        echo.
        echo WARNING: could not clear the completed-session log.
        echo A copy of FPSci is already running and has the file locked.
        echo Close every copy of FPSci, then run this again.
        echo.
        pause
    ) else (
        attrib +R "userstatus.sessions.csv" >nul 2>&1
    )
)

echo.
echo  Starting FPSci with all scenarios available.
echo  Leave this window open -- it parses your final run after you quit.
echo.

REM Run the game in the FOREGROUND so this script blocks until FPSci exits, then
REM parse once more. That last parse is the whole point of waiting:
REM
REM   - Sessions never complete (sessionFeedbackDuration is a day), so
REM     endLogging() at Session.cpp:720 never runs. The results database is only
REM     flushed and closed when the next session replaces the Session object
REM     (FPSciApp.cpp:640) or when the app exits cleanly.
REM   - The warm-up answer is queued the moment you pick it
REM     (Session.cpp:836 -> logger->addQuestion), but it is not durable on disk
REM     until one of those two flushes happens.
REM
REM So after your LAST run of a sitting, nothing else starts and nothing parses.
REM Waiting for the process to exit and parsing here captures that final run
REM together with its warm-up condition. Quit from the in-game menu so the
REM logger destructor flushes -- killing the process can still lose the tail.
"FirstPersonScience.exe"

echo.
echo  FPSci closed. Parsing your final run...

REM parse_results.bat skips itself while another parse holds analysis\parse.lock.
REM A session-start parse can still be running if you quit soon after starting a
REM scenario, so wait for it (up to a minute) instead of silently being skipped.
set /a WAITS=0
:waitlock
if not exist "analysis\parse.lock" goto lockfree
if !WAITS! GEQ 12 goto lockfree
timeout /t 5 /nobreak >nul 2>&1
set /a WAITS+=1
goto waitlock
:lockfree

REM Explicit ".\" -- a bare name is not found when the environment sets
REM NoDefaultCurrentDirectoryInExePath, which drops the CWD from the search path.
call ".\parse_results.bat"
echo.
echo  Done. CSVs are in analysis\ -- see analysis\parse_log.txt for details.
timeout /t 10 /nobreak >nul 2>&1
endlocal
