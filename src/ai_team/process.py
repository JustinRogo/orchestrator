"""Options shared by every external command the coordinator launches."""
from __future__ import annotations

import subprocess
import sys


def console_flags() -> int:
    """Keep child processes hidden when the dashboard was launched without a console.

    Under pythonw (the desktop shortcut) Windows would otherwise open a new console window
    for every git and agent CLI call. Console launches keep the default so CLIs behave as before.
    """
    if sys.platform == "win32" and sys.stdout is None:
        return subprocess.CREATE_NO_WINDOW
    return 0
