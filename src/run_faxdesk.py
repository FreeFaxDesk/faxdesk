"""Entry point for the frozen EXE (pyinstaller) and for `python run_faxdesk.py`."""
import sys
from faxdesk.__main__ import main
if __name__ == "__main__":
    sys.exit(main())
