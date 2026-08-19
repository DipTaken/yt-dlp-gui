#!/usr/bin/env python3
"""YT-DLP GUI — launcher.

The application lives in the `ytdlp_gui` package; this file only starts it so
that `python gui.py`, `pythonw gui.py` and the PyInstaller build in gui.mk all
keep working unchanged.

Run:  python gui.py
"""
import os
import sys

# Make the vendored yt_dlp (and this package) importable when run from source.
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ytdlp_gui.app import App


def main():
    App().mainloop()


if __name__ == '__main__':
    main()
