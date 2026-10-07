@echo off
rem Builds FaxDesk.exe (one file, no Python needed on the target). Run on a Windows PC with Python 3.11+ from python.org
rem or the Microsoft Store. Once per release. Stops a running FaxDesk first (the old EXE would otherwise be locked).
cd /d "%~dp0"
taskkill /F /IM FaxDesk.exe >nul 2>nul
if exist dist\FaxDesk.exe del /f /q dist\FaxDesk.exe
if exist dist\FaxDesk.exe ( echo dist\FaxDesk.exe is locked - close FaxDesk ^(tray icon ^> Quit, or Task Manager^) and run this again. & pause & exit /b 1 )
python -m pip install --user pyinstaller cryptography==46.0.7 pymupdf==1.28.2 >nul 2>nul || python -m pip install pyinstaller cryptography==46.0.7 pymupdf==1.28.2 >nul 2>nul
python -m PyInstaller --noconfirm --onefile --noconsole --name FaxDesk --icon faxdesk\www\faxdesk.ico --add-data "faxdesk\www;faxdesk\www" run_faxdesk.py
if exist dist\FaxDesk.exe ( echo. & echo BUILT dist\FaxDesk.exe & dir dist\FaxDesk.exe | findstr FaxDesk.exe ) else ( echo. & echo BUILD FAILED - see the lines above )
pause
