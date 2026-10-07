Free Fax Desk (FaxDesk) - fax from your PC with a Phone.com fax line.

Start:      Windows: double-click FaxDesk.bat     Mac: double-click FaxDesk.command
Then:       your browser opens http://127.0.0.1:8750 - go to Settings, paste your Phone.com token, account id and extension,
            add your people's names, Save, Test connection.
Start at logon (Windows): install_startup.bat (uninstall_startup.bat removes it).
Where things live: Windows %LOCALAPPDATA%\FaxDesk   Mac ~/Library/Application Support/FaxDesk
Uninstall: delete this folder (and the state folder above if you want the faxes gone too).
Needs Python 3.9 or newer (python.org; on Windows tick "Add to PATH"). Road (phones) needs: python -m pip install cryptography; Search needs: python -m pip install pymupdf + Tesseract OCR
            (the EXE already has it; without it the fax desk works and Settings > Road says what to install).
Free for one PC. The "whole office" switch in Settings (other desks on your network) is the paid plan.
MIT licence. Provided as is, without warranty of any kind.
Uninstall: uninstall.bat (stops it, removes the logon task, optionally wipes %LOCALAPPDATA%\FaxDesk), then delete the folder.

1.3.0 - Search the words inside your faxes. Install Tesseract OCR once (Settings > Search says how); FaxDesk reads each
        incoming fax on this PC and the search box on Incoming finds a name, claim or auth number - and opens the page it is on.
1.2.0 - Road: your office fax desk on your phones. Settings > Road > Turn on, Invite a phone, Allow it. Faxes go to a phone with one
        click (To phone); the phone photographs pages or picks a PDF and they land in Incoming, assigned to the person named.
        Everything is sealed on the device to the other side's key (P-256 + AES-GCM); the relay at api.freefaxdesk.com holds
        only sealed envelopes for 30 days and never a key. Phone page: https://freefaxdesk.com/road.html (add to Home Screen).
1.1.1 - A resent fax is watched for delivery afresh (the first resend kept the old verdict).
1.1.0 - Delivery watch: after your fax service accepts a fax, FaxDesk checks for 90 minutes whether it was delivered. Not delivered -> the row turns failed with the reason in plain words and Resend; delivered -> the row says so. Also: two sends of the same PDF in the same second no longer share a name.
1.0.1 - Done button replaces the Status dropdown (one click; +why afterwards for the reason; Junk offers to block the sender). New icon and tray icon. Coffee link in Settings.
