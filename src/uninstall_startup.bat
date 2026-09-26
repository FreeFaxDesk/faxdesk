@echo off
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FaxDesk.cmd" >nul 2>nul && echo Removed. || echo Nothing to remove.
schtasks /Delete /F /TN "FaxDesk" >nul 2>nul
pause
