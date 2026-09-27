# Free Fax Desk (FaxDesk)

Faxing. Magical - Delightful - Easy A small free program that puts your **Phone.com fax line** on your PC.
Drop a PDF, it goes out with a cover sheet. Incoming faxes land on the page, get assigned to a person and marked handled.
Everything stays on your computer. No cloud account, no telemetry.

**Website and download:** https://freefaxdesk.com

- Free for one PC. The "whole office" plan (every desk on your network) is a licence key - see the site.
- Windows: download `FaxDesk.exe` from Releases. Mac/Linux: `python3 -m faxdesk` from `src/` (Python 3.9+).
- Only network call: `api.phone.com`. Your token is sealed to your Windows account (DPAPI).

## Run from source
    cd src
    python -m faxdesk            # opens http://127.0.0.1:8750
    python -m faxdesk --selftest # fake Phone.com on loopback, no network

## Build the Windows EXE
    src\build_exe.bat            # PyInstaller one-file; output dist\FaxDesk.exe

## Licence
MIT. Provided as is, without warranty of any kind. Not affiliated with Phone.com.
