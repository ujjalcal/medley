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
            if url.path == "/pitch":
                return self.serve_static("pitch_studio.html", "text/html; charset=utf-8")
            if url.path == "/download":
                return self.serve_static("download_studio.html", "text/html; charset=utf-8")
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
            if url.path == "/api/shift_pitch_test":
                return self.api_shift_pitch_test()
            if url.path == "/api/shift_pitch_save":
                return self.api_shift_pitch_save()
            self.send_error(404)
        except Exception as e:
            sys.stderr.write(f"[medley] error: {e}\n")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- handlers ----

    def serve_static(self, name, ctype):
        """Serve a sibling file as-is (no template injection)."""
        path = SCRIPT_DIR / name
        if not path.is_file():
            return self.send_error(500, f"{name} not found next to medley_server.py")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        if body.get("pitch") is not None:
            cmd += ["--pitch", str(int(body["pitch"]))]

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

    # ---- pitch studio ----

    AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}

    def _stream_body_to(self, path: Path) -> int:
        """Write the request body to `path`. Returns bytes written."""
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return 0
        remaining = n
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        return n - remaining

    def api_shift_pitch_test(self):
        """Upload a video/audio file and render a pitch-shifted preview.

        Headers:
          X-Filename: URL-encoded original filename (e.g. "song.mp4")
          X-Pitch:    integer, -12..+12

        Body: raw bytes of the file.

        Output is written to `downloads/pitch/.preview_<safe_name>` —
        a single preview slot per input filename, overwritten on each call.
        """
        raw_name = self.headers.get("X-Filename") or "input.mp4"
        filename = urllib.parse.unquote(raw_name)
        try:
            pitch = int(self.headers.get("X-Pitch") or "0")
        except ValueError:
            return self.send_json({"error": "X-Pitch must be an integer"}, 400)
        if not -12 <= pitch <= 12:
            return self.send_json({"error": "pitch must be -12..+12"}, 400)

        # Sanitize: keep only the basename so a path can't escape via X-Filename.
        safe_name = Path(filename).name or "input.mp4"
        suffix = Path(safe_name).suffix.lower() or ".mp4"
        stem = Path(safe_name).stem or "input"
        is_audio = suffix in self.AUDIO_EXTS

        staging = ROOT / "downloads" / "pitch"
        staging.mkdir(parents=True, exist_ok=True)
        in_path = staging / f".input_{safe_name}"
        out_path = staging / f".preview_{safe_name}"

        written = self._stream_body_to(in_path)
        if written == 0:
            return self.send_json({"error": "empty body"}, 400)

        if pitch == 0:
            # No shift requested — preview is just a copy.
            import shutil as _sh
            _sh.copyfile(in_path, out_path)
            return self.send_json({
                "ok": True,
                "preview": str(out_path),
                "suggested_name": f"{stem}_pitch+0{suffix}",
                "pitch": pitch,
            })

        ratio = 2 ** (pitch / 12.0)
        af = (f"asetrate=44100*{ratio:.6f},"
              f"aresample=44100,atempo={1 / ratio:.6f}")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
               "-i", str(in_path), "-af", af, "-b:a", "192k"]
        if not is_audio:
            # Video: passthrough the video stream, re-encode audio to AAC.
            cmd += ["-c:v", "copy", "-c:a", "aac",
                    "-movflags", "+faststart"]
        cmd += [str(out_path)]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "render timed out (30 min cap)"}, 504)

        if result.returncode != 0:
            return self.send_json({
                "ok": False,
                "error": "ffmpeg failed",
                "stderr": result.stderr[-2000:],
            }, 500)

        return self.send_json({
            "ok": True,
            "preview": str(out_path),
            "suggested_name": f"{stem}_pitch{pitch:+d}{suffix}",
            "pitch": pitch,
        })

    def api_shift_pitch_save(self):
        """Promote the preview slot to a permanent name.

        Body: JSON { "preview": "<path>", "save_as": "<filename>" }
        Both must resolve inside downloads/pitch/.
        """
        body = self.read_json()
        preview = body.get("preview")
        save_as = body.get("save_as")
        if not preview or not save_as:
            return self.send_json({"error": "missing preview or save_as"}, 400)

        # save_as is a filename only — no path components, no escapes.
        save_name = Path(save_as).name
        if not save_name:
            return self.send_json({"error": "save_as must be a filename"}, 400)

        try:
            pdir = (ROOT / "downloads" / "pitch").resolve()
            src = safe_path(preview)
            dst = safe_path(pdir / save_name)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not src.is_file():
            return self.send_json({"error": f"preview not found: {src.name}"}, 404)
        # Confine both to the pitch staging folder.
        try:
            src.relative_to(pdir)
            dst.relative_to(pdir)
        except ValueError:
            return self.send_json({"error": "paths must stay in downloads/pitch/"}, 400)

        import shutil as _sh
        _sh.copyfile(src, dst)
        return self.send_json({"ok": True, "output": str(dst)})


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
