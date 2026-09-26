@echo off
rem Makes FaxDesk start when you log in. No admin rights: it puts a small starter in your Startup folder.
rem uninstall_startup.bat removes it.
cd /d "%~dp0"
set "SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%SU%" mkdir "%SU%"
> "%SU%\FaxDesk.cmd" echo @echo off
>> "%SU%\FaxDesk.cmd" echo cd /d "%~dp0"
if exist "%~dp0FaxDesk.exe" ( >> "%SU%\FaxDesk.cmd" echo start "" "%~dp0FaxDesk.exe" --no-browser ) else ( >> "%SU%\FaxDesk.cmd" echo start "FaxDesk" /min python -m faxdesk --no-browser )
if exist "%SU%\FaxDesk.cmd" ( echo FaxDesk will start at logon. Open http://127.0.0.1:8750 after you log in. ) else ( echo Could not write to the Startup folder. )
pause
