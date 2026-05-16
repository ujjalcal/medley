#!/usr/bin/env python3
"""
Build a single mp4 medley from a cut list.

Pairs with medley_studio.html:
  1. Open medley_studio.html in your browser.
  2. Pick a folder, mark in/out points on each video, drag to reorder.
  3. Click "Export medley.json".
  4. Drop the downloaded medley.json next to your videos and run:

        python3 build_medley.py medley.json

medley.json schema
------------------
{
  "folder": "downloads",        # informational; not used for resolution
  "output": "medley.mp4",       # default output file name
  "clips": [
    { "file": "song1.mp4", "start": 35.0,  "end": 90.5  },
    { "file": "song2.mp4", "start": 12.25, "end": 70.0  }
  ]
}

How it works
------------
ffmpeg's `concat` filter is used to trim each input to [start, end] and join
the segments into one mp4 (re-encoded to h264 + aac so codec/parameter
mismatches between source files don't cause issues). This is slower than
stream-copy but produces frame-accurate cuts and a uniform output.

Requirements
------------
- ffmpeg in PATH      (macOS: brew install ffmpeg)
- Python 3.8+
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def fmt_time(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:06.3f}"


def build(
    cutlist_path: Path,
    folder: Path | None,
    output: Path | None,
    crf: int,
    preset: str,
    height: int | None,
    transition_duration: float | None,
    dry_run: bool,
) -> int:
    if not shutil.which("ffmpeg"):
        sys.stderr.write(
            "ERROR: ffmpeg not found in PATH.\n"
            "Install:  macOS  → brew install ffmpeg\n"
            "          Linux  → apt install ffmpeg\n"
        )
        return 2

    if not cutlist_path.is_file():
        sys.stderr.write(f"ERROR: cut list not found: {cutlist_path}\n")
        return 2

    data = json.loads(cutlist_path.read_text(encoding="utf-8"))
    clips = data.get("clips", [])
    if not clips:
        sys.stderr.write("ERROR: cut list has no clips.\n")
        return 2

    # Resolve videos folder. Default: same folder as the JSON file.
    base = folder.resolve() if folder else cutlist_path.parent.resolve()

    # Verify every clip file exists before invoking ffmpeg.
    missing = []
    for c in clips:
        p = base / c["file"]
        if not p.is_file():
            missing.append(p)
    if missing:
        sys.stderr.write(
            "ERROR: these clip files were not found in "
            f"{base}:\n"
            + "\n".join(f"  - {m.name}" for m in missing)
            + "\n"
        )
        return 2

    # Determine output path.
    out_name = data.get("output", "medley.mp4")
    out_path = (output.resolve() if output else (base / out_name)).with_suffix(".mp4")

    # Build the ffmpeg command.
    # We pass each clip as a separate input with -ss/-to for input-side
    # seeking. Then we concat-filter the trimmed inputs in order. To make the
    # concat filter happy across heterogeneous inputs, we normalize each
    # stream with scale + fps + setsar + aformat before concat.
    # Concat filter requires every input to have identical pixel dimensions.
    # If a target height is set, derive a fixed 16:9 width (rounded to even).
    # Otherwise, fall back to a sensible default of 1280x720 to guarantee
    # uniformity across mixed-resolution sources.
    target_h = height or 720
    target_w = (target_h * 16 // 9) // 2 * 2
    target_w_expr = str(target_w)
    target_h_expr = str(target_h)

    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    for c in clips:
        cmd += [
            "-ss", f'{float(c["start"]):.3f}',
            "-to", f'{float(c["end"]):.3f}',
            "-i", str(base / c["file"]),
        ]

    # Build the filter graph.
    n = len(clips)
    parts = []
    durs = [float(c["end"]) - float(c["start"]) for c in clips]

    # Resolve transition duration. CLI flag wins if set; else pull from JSON;
    # else 0 (hard cuts). Cap at the shortest clip minus a small safety margin
    # so xfade never tries to overlap more than a clip's worth.
    xfade = transition_duration
    if xfade is None:
        xfade = float(data.get("transition_duration", 0) or 0)
    if xfade > 0 and n > 1:
        max_safe = max(0.0, min(durs) - 0.1)
        if xfade > max_safe:
            print(f"NOTE: shortening crossfade from {xfade:.2f}s to {max_safe:.2f}s "
                  f"(shortest clip is {min(durs):.2f}s).")
            xfade = max_safe

    # Trim/normalize each input — same shape regardless of transition mode.
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={target_w_expr}:{target_h_expr}:force_original_aspect_ratio=decrease,"
            f"pad={target_w_expr}:{target_h_expr}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=30,format=yuv420p[v{i}]"
        )
        parts.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
        )

    if xfade > 0 and n > 1:
        # Chain xfade + acrossfade across consecutive clips. Each xfade
        # consumes the trailing `xfade` seconds of the running stream and
        # the leading `xfade` seconds of the next clip and replaces them
        # with a smooth dissolve, so the running cumulative duration
        # advances by (clip_dur - xfade) per step.
        cum = durs[0]
        cur_v, cur_a = "v0", "a0"
        for i in range(1, n):
            offset = max(0.0, cum - xfade)
            new_v = f"xv{i}"
            new_a = f"xa{i}"
            parts.append(
                f"[{cur_v}][v{i}]xfade=transition=fade:duration={xfade:.3f}:"
                f"offset={offset:.3f}[{new_v}]"
            )
            parts.append(f"[{cur_a}][a{i}]acrossfade=d={xfade:.3f}[{new_a}]")
            cum = cum + durs[i] - xfade
            cur_v, cur_a = new_v, new_a
        parts.append(f"[{cur_v}]format=yuv420p[outv]")
        parts.append(f"[{cur_a}]anull[outa]")
    else:
        # Hard cuts: classic concat demuxer-via-filter approach.
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]")

    filter_complex = ";".join(parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    total = sum(float(c["end"]) - float(c["start"]) for c in clips)
    print(f"Output:    {out_path}")
    print(f"Clips:     {n}")
    print(f"Duration:  {fmt_time(total)}")
    print(f"Settings:  h264 crf={crf} preset={preset} "
          f"target {target_w}x{target_h}, aac 192k stereo 44.1kHz")
    if xfade > 0 and n > 1:
        print(f"Crossfade: {xfade:.2f}s between clips (output ≈ {fmt_time(sum(durs) - xfade * (n - 1))})")
    print("-" * 60)

    if dry_run:
        print("Dry run — not executing. Command:")
        print(" ".join(repr(x) if " " in x else x for x in cmd))
        return 0

    try:
        proc = subprocess.run(cmd)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130
    if proc.returncode != 0:
        sys.stderr.write(f"\nffmpeg exited with code {proc.returncode}\n")
        return proc.returncode

    if out_path.is_file():
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print("-" * 60)
        print(f"Done. Wrote {out_path.name} ({size_mb:.1f} MB)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Render a video medley from a medley_studio cut list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("cutlist", help="Path to medley.json")
    p.add_argument(
        "--folder",
        help="Folder containing the source videos (default: the JSON's directory).",
    )
    p.add_argument("-o", "--output", help="Output mp4 path.")
    p.add_argument(
        "--crf",
        type=int, default=20,
        help="x264 CRF — lower = higher quality / bigger file. Default 20.",
    )
    p.add_argument(
        "--preset",
        default="medium",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"],
        help="x264 preset (encoding speed vs. compression). Default 'medium'.",
    )
    p.add_argument(
        "--height",
        type=int, default=720,
        help="Target output height in pixels (16:9 width derived). "
             "Clips are scaled+padded to this size to guarantee uniform "
             "dimensions across the medley. Default 720 (→ 1280x720). "
             "Use 1080 for full HD (→ 1920x1080).",
    )
    p.add_argument(
        "--crossfade",
        type=float, default=None,
        help="Crossfade duration in seconds between adjacent clips. "
             "Overrides transition_duration in the JSON. "
             "0 (default if neither set) = hard cuts.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the ffmpeg command and exit.",
    )
    args = p.parse_args()

    return build(
        cutlist_path=Path(args.cutlist).expanduser(),
        folder=Path(args.folder).expanduser() if args.folder else None,
        output=Path(args.output).expanduser() if args.output else None,
        crf=args.crf,
        preset=args.preset,
        height=args.height,
        transition_duration=args.crossfade,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
