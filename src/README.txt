Free Fax Desk (FaxDesk) - fax from your PC with a Phone.com fax line.

Start:      Windows: double-click FaxDesk.bat     Mac: double-click FaxDesk.command
Then:       your browser opens http://127.0.0.1:8750 - go to Settings, paste your Phone.com token, account id and extension,
            add your people's names, Save, Test connection.
Start at logon (Windows): install_startup.bat (uninstall_startup.bat removes it).
Where things live: Windows %LOCALAPPDATA%\FaxDesk   Mac ~/Library/Application Support/FaxDesk
Uninstall: delete this folder (and the state folder above if you want the faxes gone too).
Needs Python 3.9 or newer (python.org; on Windows tick "Add to PATH").
Free for one PC. The "whole office" switch in Settings (other desks on your network) is the paid plan.
MIT licence. Provided as is, without warranty of any kind.
Uninstall: uninstall.bat (stops it, removes the logon task, optionally wipes %LOCALAPPDATA%\FaxDesk), then delete the folder.

1.1.0 - Delivery watch: after your fax service accepts a fax, FaxDesk checks for 90 minutes whether it was delivered. Not delivered -> the row turns failed with the reason in plain words and Resend; delivered -> the row says so. Also: two sends of the same PDF in the same second no longer share a name.
1.0.1 - Done button replaces the Status dropdown (one click; +why afterwards for the reason; Junk offers to block the sender). New icon and tray icon. Coffee link in Settings.
