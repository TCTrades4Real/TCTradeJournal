"""Upload dashboard files to tctrades.com via FTP.

Paths are relative to the repo root. Files under dashboard/ preserve their
subdirectory structure on the server.
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
    "dashboard/candlestick-chart.html",
    "dashboard/monte_carlo.html",
    "dashboard/reports.html",
    "dashboard/trades.html",
    "dashboard/nav.js",
    "dashboard/auth.js",
]

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
        # Preserve subdirectory under dashboard/ on the server. Accepts both relative
        # paths (e.g. "dashboard/index.html", from FILES) and absolute paths (e.g.
        # from import_tradezero.py's build_and_write(), which uses OUTPUT_DIR — an
        # absolute path) — relative_to() alone only handles the former.
        parts = p.as_posix().split("/")
        if "dashboard" in parts:
            idx = len(parts) - 1 - parts[::-1].index("dashboard")
            remote_path = f"{remote}/{'/'.join(parts[idx + 1:])}"
        else:
            remote_path = f"{remote}/{p.name}"
        remote_dir = remote_path.rsplit("/", 1)[0]
        ensure_remote_dir(ftp, remote_dir)

        local_size = p.stat().st_size
        # Verify the transfer actually landed — a STOR can report success (226) on a
        # flaky connection without the bytes actually persisting server-side. One retry
        # covers a transient hiccup; a second mismatch is treated as a real failure.
        for attempt in (1, 2):
            with open(p, "rb") as fh:
                ftp.storbinary(f"STOR {remote_path}", fh)
            remote_size = ftp.size(remote_path)
            if remote_size == local_size:
                break
            print(f"  WARNING: size mismatch after upload (local {local_size}, remote {remote_size}) — "
                  f"{'retrying' if attempt == 1 else 'giving up'}: {remote_path}")
        else:
            raise RuntimeError(f"Upload verification failed for {remote_path} after 2 attempts")

        print(f"  Uploaded: {remote_path}")

print("Done.")
