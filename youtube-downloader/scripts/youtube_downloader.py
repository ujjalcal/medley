#!/usr/bin/env python3
"""
YouTube Downloader
==================

A small CLI wrapper around yt-dlp for downloading YouTube videos or audio.

LEGAL / TOS NOTE
----------------
YouTube's Terms of Service generally prohibit downloading content unless a
download button or link is explicitly provided by YouTube, or you have
permission from the rights holder. This tool is provided for personal and
educational use; you are responsible for complying with applicable laws and
the terms of any service you use it with.

REQUIREMENTS
------------
1. Python 3.8+
2. yt-dlp:        pip install --upgrade yt-dlp
3. ffmpeg:        Required for merging best video+audio streams and for
                  audio-only (mp3) extraction.
                  - macOS:   brew install ffmpeg
                  - Linux:   apt install ffmpeg  (or your package manager)
                  - Windows: https://ffmpeg.org/download.html

USAGE
-----
    # Download a single video at best available quality (mp4 preferred):
    python youtube_downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"

    # Audio-only as mp3:
    python youtube_downloader.py -a "https://www.youtube.com/watch?v=VIDEO_ID"

    # Cap quality at 1080p and pick a custom output folder:
    python youtube_downloader.py -q 1080 -o ~/Downloads/yt "URL1" "URL2"

    # Download a whole playlist:
    python youtube_downloader.py --playlist "https://www.youtube.com/playlist?list=..."

    # Read a list of URLs from a file (one per line):
    python youtube_downloader.py -f urls.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.stderr.write(
        "ERROR: yt-dlp is not installed.\n"
        "Install it with:  pip install --upgrade yt-dlp\n"
    )
    sys.exit(1)


def build_options(
    output_dir: Path,
    audio_only: bool,
    max_height: int | None,
    playlist: bool,
    quiet: bool,
) -> dict:
    """Translate CLI flags into a yt-dlp options dict."""
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / "%(title)s [%(id)s].%(ext)s")

    opts: dict = {
        "outtmpl": outtmpl,
        "noplaylist": not playlist,
        "ignoreerrors": True,         # keep going if one item fails
        "restrictfilenames": False,
        "nooverwrites": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "quiet": quiet,
        "no_warnings": quiet,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [],
    }

    if audio_only:
        # Best audio, transcoded to mp3 via ffmpeg.
        opts["format"] = "bestaudio/best"
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })
    else:
        # Prefer H.264 (avc1) + AAC for maximum compatibility — these play
        # everywhere (Safari, QuickTime, every browser, every editor). YouTube
        # also serves AV1 and VP9 streams which are higher-quality at the same
        # bitrate but Safari and many editors won't decode AV1/VP9-in-mp4
        # reliably. Each fallback widens the codec net only after the more
        # compatible option is unavailable.
        h = f"[height<={max_height}]" if max_height else ""
        opts["format"] = (
            # 1st choice: H.264 video + AAC audio (universally playable).
            f"bestvideo{h}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            # 2nd: any H.264 video + best audio.
            f"bestvideo{h}[vcodec^=avc1]+bestaudio/"
            # 3rd: any mp4 video stream + m4a audio.
            f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]/"
            # 4th: anything goes — accept VP9/AV1 if that's all that's offered.
            f"bestvideo{h}+bestaudio/"
            # 5th: pre-merged single file (older/short videos).
            f"best{h}"
        )
        opts["merge_output_format"] = "mp4"

    # Embed thumbnail and metadata when possible — nice for offline libraries.
    opts["postprocessors"].append({"key": "FFmpegMetadata"})
    opts["writethumbnail"] = audio_only
    if audio_only:
        opts["postprocessors"].append({
            "key": "EmbedThumbnail",
            "already_have_thumbnail": False,
        })

    return opts


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.urls or [])
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            sys.stderr.write(f"ERROR: URL file not found: {path}\n")
            sys.exit(2)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if not urls:
        sys.stderr.write("ERROR: No URLs provided. See --help.\n")
        sys.exit(2)
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download YouTube videos or audio via yt-dlp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more YouTube URLs (videos, shorts, or playlists).",
    )
    parser.add_argument(
        "-o", "--output",
        default="./downloads",
        help="Output directory (default: ./downloads).",
    )
    parser.add_argument(
        "-a", "--audio",
        action="store_true",
        help="Audio only — extract as mp3 (requires ffmpeg).",
    )
    parser.add_argument(
        "-q", "--quality",
        type=int,
        default=None,
        metavar="HEIGHT",
        help="Cap video height in pixels (e.g. 720, 1080, 1440, 2160).",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Download full playlist when a playlist URL is given "
             "(default: only the single video).",
    )
    parser.add_argument(
        "-f", "--file",
        help="Path to a text file containing URLs (one per line, "
             "lines starting with # are ignored).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress yt-dlp progress output.",
    )

    args = parser.parse_args()

    urls = collect_urls(args)
    output_dir = Path(args.output).expanduser().resolve()

    opts = build_options(
        output_dir=output_dir,
        audio_only=args.audio,
        max_height=args.quality,
        playlist=args.playlist,
        quiet=args.quiet,
    )

    print(f"Saving to: {output_dir}")
    print(f"Mode:      {'audio (mp3)' if args.audio else 'video (mp4)'}")
    if not args.audio and args.quality:
        print(f"Max height: {args.quality}p")
    print(f"URLs:      {len(urls)}")
    print("-" * 60)

    failures = 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        for url in urls:
            try:
                ydl.download([url])
            except yt_dlp.utils.DownloadError as exc:
                failures += 1
                sys.stderr.write(f"FAILED: {url}\n  {exc}\n")
            except KeyboardInterrupt:
                sys.stderr.write("\nInterrupted by user.\n")
                return 130

    print("-" * 60)
    print(f"Done. {len(urls) - failures}/{len(urls)} succeeded.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
