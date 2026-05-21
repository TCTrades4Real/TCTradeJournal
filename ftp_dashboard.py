"""Upload dashboard HTML/JS files to tctrades.com via FTP."""
import ftplib, json, pathlib, sys

cfg      = json.loads(pathlib.Path(".vscode/sftp.json").read_text())
host     = cfg["host"]
port     = cfg.get("port", 21)
user     = cfg["username"]
password = cfg["password"]
remote   = cfg["remotePath"].rstrip("/")

FILES = [
    "dashboard/index.html",
    "dashboard/month.html",
    "dashboard/day.html",
    "dashboard/candlestick.html",
    "dashboard/monte_carlo.html",
    "dashboard/reports.html",
    "dashboard/trades.html",
    "dashboard/nav.js",
    "dashboard/auth.js",
]

specific = sys.argv[1:]  # optional: pass specific file paths to upload only those

targets = specific if specific else FILES

print(f"Connecting to {host}:{port} ...")
with ftplib.FTP() as ftp:
    ftp.connect(host, port)
    ftp.login(user, password)
    for local in targets:
        p = pathlib.Path(local)
        if not p.exists():
            print(f"  SKIP (not found): {local}")
            continue
        with open(p, "rb") as fh:
            ftp.storbinary(f"STOR {remote}/{p.name}", fh)
        print(f"  Uploaded: {p.name}")

print("Done.")
