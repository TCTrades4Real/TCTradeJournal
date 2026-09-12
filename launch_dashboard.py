"""
launch_dashboard.py — starts a local HTTP server for dashboard/ (if not already
running) and opens it in a tabless Chrome app window (no tabs, no address bar).

Needed because Chrome/Edge block fetch() of local JSON under file:// (CORS) —
see CLAUDE.md's Dashboard Notes.

Usage:
    python launch_dashboard.py
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD_DIR = os.path.join(_HERE, 'dashboard')
_PORT = 8000
_URL = f'http://localhost:{_PORT}/index.html'
_CHROME_ENV_VARS = ('ProgramFiles', 'ProgramFiles(x86)', 'LocalAppData')


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _find_chrome():
    override = os.environ.get('CHROME_PATH', '').strip()
    if override and os.path.isfile(override):
        return override
    for env_var in _CHROME_ENV_VARS:
        base = os.environ.get(env_var)
        if not base:
            continue
        candidate = os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe')
        if os.path.isfile(candidate):
            return candidate
    return None


def main():
    if not _port_open(_PORT):
        subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(_PORT)],
            cwd=_DASHBOARD_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _port_open(_PORT):
            time.sleep(0.1)

    chrome_path = _find_chrome()
    if chrome_path is None:
        webbrowser.open(_URL)
        return

    subprocess.Popen([chrome_path, f'--app={_URL}'])


if __name__ == '__main__':
    main()
