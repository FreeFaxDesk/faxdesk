@echo off
rem FaxDesk - Free Fax Desk. Double-click to start. Needs Python 3.9+ (python.org or Microsoft Store).
rem Runs minimized: the small "FaxDesk" window in the taskbar IS the program; close it to stop FaxDesk.
cd /d "%~dp0"
if exist "%~dp0FaxDesk.exe" ( start "" "%~dp0FaxDesk.exe" & exit /b )
where python >nul 2>nul || ( echo Python is not installed. In this window type: python   and press Enter to get it from the Microsoft Store, then run FaxDesk.bat again. & pause & exit /b 1 )
start "FaxDesk" /min python -m faxdesk
