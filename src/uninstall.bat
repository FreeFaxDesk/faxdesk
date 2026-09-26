@echo off
rem Removes FaxDesk from this PC: stops it, removes the start-at-logon task, and (if you say Y) deletes the state folder
rem with the saved token and every fax. This folder itself you delete by hand afterwards.
cd /d "%~dp0"
taskkill /F /IM FaxDesk.exe >nul 2>nul
for /f "tokens=2" %%p in ('wmic process where "commandline like '%%faxdesk%%' and (name='python.exe' or name='pythonw.exe')" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /F /PID %%p >nul 2>nul
schtasks /Delete /F /TN "FaxDesk" >nul 2>nul
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FaxDesk.cmd" >nul 2>nul
echo FaxDesk stopped.
set /p wipe=Also delete the saved token and ALL faxes in "%LOCALAPPDATA%\FaxDesk"? (Y/N):
if /i "%wipe%"=="Y" ( rmdir /s /q "%LOCALAPPDATA%\FaxDesk" && echo State folder deleted. ) else ( echo State folder kept: %LOCALAPPDATA%\FaxDesk )
echo Now delete this folder to finish. 
pause
