"""Upload dashboard files to tctrades.com via FTP.

Paths are relative to the repo root. Files under dashboard/ preserve their
subdirectory structure on the server (e.g. dashboard/backtest/foo.json
uploads to {remotePath}/backtest/foo.json).

NOTE: dashboard/watchlist_setups/data.json is intentionally NOT in the
default FILES list. Once watchlists.html is live, callouts are added/edited
straight on tctrades.com via dashboard/api/setups.php, which becomes the
authoritative copy of that file server-side. A routine run of this script
would otherwise overwrite live edits with a stale local copy. Push it
explicitly and deliberately if you ever want to force-seed the server from
your local copy: `python ftp_dashboard.py dashboard/watchlist_setups/data.json`.
"""
import ftplib, json, pathlib, sys

cfg      = json.loads(pathlib.Path(".vscode/sftp.json").read_text())
host     = cfg["host"]
port     = cfg.get("port", 21)
user     = cfg["username"]
password = cfg["password"]
remote   = cfg["remotePath"].rstrip("/")

FILES = [
    "dashboard/account_balance.json",
    "dashboard/index.html",
    "dashboard/month.html",
    "dashboard/day.html",
    "dashboard/candlestick.html",
    "dashboard/monte_carlo.html",
    "dashboard/reports.html",
    "dashboard/trades.html",
    "dashboard/watchlists.html",
    "dashboard/api/setups.php",
    "dashboard/nav.js",
    "dashboard/auth.js",
] + [str(p) for p in sorted(pathlib.Path("dashboard/backtest").glob("*.json"))]

specific = sys.argv[1:]  # optional: pass specific file paths to upload only those
targets  = specific if specific else FILES

def ensure_remote_dir(ftp, remote_dir):
    """Create remote directory tree if it doesn't exist."""
    parts = remote_dir.split("/")
    path  = ""
    for part in parts:
        if not part:
            continue
        path += "/" + part
        try:
            ftp.mkd(path)
        except ftplib.error_perm:
            pass  # already exists

print(f"Connecting to {host}:{port} ...")
with ftplib.FTP() as ftp:
    ftp.connect(host, port)
    ftp.login(user, password)
    for local in targets:
        p = pathlib.Path(local)
        if not p.exists():
            print(f"  SKIP (not found): {local}")
            continue
        # Preserve subdirectory under dashboard/ on the server
        try:
            rel = p.relative_to("dashboard")
            remote_path = f"{remote}/{rel.as_posix()}"
        except ValueError:
            remote_path = f"{remote}/{p.name}"
        remote_dir = remote_path.rsplit("/", 1)[0]
        ensure_remote_dir(ftp, remote_dir)
        with open(p, "rb") as fh:
            ftp.storbinary(f"STOR {remote_path}", fh)
        print(f"  Uploaded: {rel if 'rel' in dir() else p.name}")

print("Done.")
