#!/usr/bin/env python3
"""
Medley Studio — local server.

Serves the editor HTML and exposes a small JSON API so the editor can
download YouTube videos, render the medley, and play back the rendered file
without round-tripping through the terminal.

Usage
-----
    python3 medley_server.py
    # then visit http://localhost:8765/

It expects medley_studio.html and build_medley.py to live next to it, plus
the bundled YouTube downloader at youtube-downloader/scripts/youtube_downloader.py
(they ship as a set). All file paths the API accepts are constrained to be
inside --root (default: this script's folder).

Endpoints
---------
GET  /                       Serves medley_studio.html with a small inline
                             script setting window.MEDLEY_SERVER = true so
                             the editor knows it can use the API.
GET  /api/health             {"ok": true} — used by the editor to confirm
                             server-mode at startup.
GET  /api/files?folder=PATH  Lists video files in PATH.
GET  /file?path=PATH         Streams a video file (with Range support, so
                             the <video> tag can seek properly).
POST /api/download           Body: {urls, audio, quality, output}.
                             Runs youtube_downloader.py and returns when
                             complete. Long-running but bounded to 10 min.
POST /api/build              Body: {cutlist, folder, output}.
                             Runs build_medley.py via a temp JSON. Bounded
                             to 30 min.

Security
--------
Listens on 127.0.0.1 only. Every path the API accepts is resolved and
rejected if it escapes --root, so a stray request can't read or write
arbitrary files on the machine.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
ROOT: Path = SCRIPT_DIR  # set in main()

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}


def safe_path(rel_or_abs: str | os.PathLike) -> Path:
    """Resolve a user-supplied path and ensure it stays inside ROOT.

    Path traversal (../../etc/passwd) and absolute paths outside ROOT both
    get rejected. We resolve symlinks too so a symlink inside ROOT can't
    point us out of it.
    """
    p = Path(rel_or_abs).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    try:
        p.relative_to(ROOT)
    except ValueError:
        raise ValueError(f"path escapes root: {p}")
    return p


class Handler(http.server.BaseHTTPRequestHandler):

    # Quieter, prefixed logging.
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[medley] {self.address_string()} {fmt % args}\n")

    # ---- helpers ----

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Allow file:// origin to probe for server presence and call APIs.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routing ----

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/":
                return self.serve_html()
            if url.path == "/api/health":
                return self.send_json({"ok": True, "root": str(ROOT)})
            if url.path == "/api/files":
                return self.api_files(url)
            if url.path == "/file":
                return self.api_file(url)
            self.send_error(404)
        except Exception as e:
            sys.stderr.write(f"[medley] error: {e}\n")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/download":
                return self.api_download()
            if url.path == "/api/build":
                return self.api_build()
            self.send_error(404)
        except Exception as e:
            sys.stderr.write(f"[medley] error: {e}\n")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- handlers ----

    def serve_html(self):
        path = SCRIPT_DIR / "medley_studio.html"
        if not path.is_file():
            return self.send_error(500, "medley_studio.html not found next to medley_server.py")
        html = path.read_text(encoding="utf-8")
        # Tell the page it's running in server mode and where the root is.
        injection = (
            "<script>"
            "window.MEDLEY_SERVER = true;"
            f"window.MEDLEY_ROOT = {json.dumps(str(ROOT))};"
            "</script>\n</head>"
        )
        html = html.replace("</head>", injection, 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_files(self, url):
        params = urllib.parse.parse_qs(url.query)
        folder = params.get("folder", [str(ROOT)])[0]
        try:
            p = safe_path(folder)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not p.is_dir():
            return self.send_json({"error": f"not a directory: {p}"}, 400)
        files = sorted(
            (f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS),
            key=lambda x: x.name.lower(),
        )
        return self.send_json({
            "folder": str(p),
            "files": [
                {"name": f.name, "size": f.stat().st_size, "path": str(f)}
                for f in files
            ],
        })

    def api_file(self, url):
        params = urllib.parse.parse_qs(url.query)
        if "path" not in params:
            return self.send_error(400, "missing ?path=")
        try:
            p = safe_path(params["path"][0])
        except ValueError as e:
            return self.send_error(403, str(e))
        if not p.is_file():
            return self.send_error(404, f"not found: {p.name}")

        size = p.stat().st_size
        rng = self.headers.get("Range")
        ctype = "video/mp4"
        if p.suffix.lower() == ".webm":
            ctype = "video/webm"
        elif p.suffix.lower() == ".mkv":
            ctype = "video/x-matroska"

        if rng and (m := re.match(r"bytes=(\d+)-(\d*)", rng)):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", ctype)
            self.end_headers()
            with open(p, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'inline; filename="{p.name}"')
            self.end_headers()
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def api_download(self):
        body = self.read_json()
        urls = [u.strip() for u in (body.get("urls") or []) if u and u.strip()]
        if not urls:
            return self.send_json({"error": "no urls"}, 400)
        try:
            out = safe_path(body.get("output") or "downloads/main")
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        out.mkdir(parents=True, exist_ok=True)

        cmd = [sys.executable,
               str(SCRIPT_DIR / "youtube-downloader" / "scripts" / "youtube_downloader.py"),
               "-o", str(out), "--quiet"]
        if body.get("audio"):
            cmd.append("-a")
        q = body.get("quality")
        if q:
            cmd += ["-q", str(int(q))]
        cmd += urls

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "download timed out (10 min cap)"}, 504)

        return self.send_json({
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "output": str(out),
        })

    def api_build(self):
        body = self.read_json()
        cutlist = body.get("cutlist") or {}
        if "clips" not in cutlist or not cutlist["clips"]:
            return self.send_json({"error": "cutlist missing 'clips'"}, 400)
        try:
            folder = safe_path(body.get("folder") or "downloads/main")
            output = safe_path(body.get("output") or (folder / "medley.mp4"))
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not folder.is_dir():
            return self.send_json({"error": f"not a directory: {folder}"}, 400)
        output.parent.mkdir(parents=True, exist_ok=True)

        # build_medley.py reads a JSON path; stage one in the output folder.
        json_path = output.parent / ".medley_cutlist.json"
        json_path.write_text(json.dumps(cutlist), encoding="utf-8")
        cmd = [sys.executable, str(SCRIPT_DIR / "build_medley.py"),
               str(json_path),
               "--folder", str(folder),
               "--output", str(output),
               "--preset", "veryfast"]
        if body.get("crf") is not None:
            cmd += ["--crf", str(int(body["crf"]))]
        if body.get("height") is not None:
            cmd += ["--height", str(int(body["height"]))]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        finally:
            try:
                json_path.unlink()
            except FileNotFoundError:
                pass

        return self.send_json({
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
            "output": str(output),
        })


def find_free_port(default: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main() -> int:
    global ROOT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--root", default=str(SCRIPT_DIR),
        help="Folder containing medley_studio.html plus your videos. "
             "All file API calls are constrained to this directory. "
             "Default: where this script lives.",
    )
    p.add_argument("--port", type=int, default=8765, help="Port (default 8765).")
    p.add_argument("--open", action="store_true", help="Open the editor in your browser on start.")
    args = p.parse_args()

    ROOT = Path(args.root).expanduser().resolve()
    if not ROOT.is_dir():
        sys.stderr.write(f"--root not a directory: {ROOT}\n")
        return 2

    port = find_free_port(args.port)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/"
    print(f"Medley Studio")
    print(f"  root: {ROOT}")
    print(f"  url:  {url}")
    print("Press Ctrl+C to stop.")

    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
