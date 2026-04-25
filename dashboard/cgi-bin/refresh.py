#!/usr/bin/env python3
"""
CGI endpoint: POST /cgi-bin/refresh.py
Runs export.py on the server, returns JSON with stdout/stderr.
Set this file to chmod 755 after uploading to HostGator.
"""
import json, subprocess, sys, os
from pathlib import Path

print("Content-Type: application/json")
print("Access-Control-Allow-Origin: https://tctrades.com")
print("Access-Control-Allow-Methods: POST, OPTIONS")
print()

EXPORT = Path("/home/fttdeqte/app/export.py")

# Scan for all Python 3.x binaries on the server so we can find 3.10+
def find_python():
    search_dirs = [
        "/opt/cpanel/ea-python312/root/usr/bin",
        "/opt/cpanel/ea-python311/root/usr/bin",
        "/opt/cpanel/ea-python310/root/usr/bin",
        "/opt/cpanel/ea-python39/root/usr/bin",
        "/usr/local/bin",
        "/usr/bin",
    ]
    for d in search_dirs:
        for name in ("python3.12", "python3.11", "python3.10"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return sys.executable

PYTHON = find_python()

env = os.environ.copy()
env.setdefault("TRADE_DATA_DIR", "/home/fttdeqte/trade_data")
env.setdefault("OUTPUT_DIR",     "/home/fttdeqte/public_html")

result = subprocess.run(
    [PYTHON, str(EXPORT)],
    capture_output=True,
    text=True,
    cwd=str(EXPORT.parent),
    env=env,
)

print(json.dumps({
    "success": result.returncode == 0,
    "python":  PYTHON,
    "output":  result.stdout,
    "error":   result.stderr,
}))
