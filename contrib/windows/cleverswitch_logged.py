"""Runs cleverswitch with output to ~/cleverswitch.log (pythonw-safe, rotates at 5 MB)."""

import os
import sys

log_path = os.path.join(os.path.expanduser("~"), "cleverswitch.log")
try:
    if os.path.getsize(log_path) > 5_000_000:
        os.replace(log_path, log_path + ".1")
except OSError:
    pass
try:
    sys.stdout = sys.stderr = open(log_path, "a", buffering=1, encoding="utf-8")
except OSError:
    pass  # run without redirection rather than crash-loop the scheduled task

from cleverswitch.cli.cli_module import main

sys.exit(main())
