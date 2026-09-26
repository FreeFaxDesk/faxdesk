#!/bin/bash
# FaxDesk - Free Fax Desk (Mac). Double-click to start (first time: right-click > Open).
cd "$(dirname "$0")"
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is needed (python.org)."; read -n 1; exit 1; }
python3 -m faxdesk
