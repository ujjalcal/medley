# Medley Studio

Local-first browser app for stitching clips from videos into a single rendered "medley," with a companion **Pitch Studio** for shifting the pitch of any video or audio file. Two studios share one tiny Python server. Everything runs on `localhost`; nothing leaves the machine.

- **Medley Studio** (`/`) — pick a folder of videos, mark in/out points, drag-reorder the timeline, render with crossfades + EBU R128 loudness normalization + optional global pitch shift.
- **Pitch Studio** (`/pitch`) — drop a single video or audio file, preview a pitch shift, save with a clean name.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| **Python 3.9+** | Server + render scripts | macOS has it at `/usr/bin/python3`. Linux: distro package. Windows: [python.org](https://www.python.org/downloads/) |
| **ffmpeg** | Renders and pitch-shifts (via subprocess) | macOS: `brew install ffmpeg`. Linux: `apt install ffmpeg`. Windows: https://ffmpeg.org/download.html |
| **yt-dlp** | YouTube download backend | `pip install -r requirements.txt` (see Setup) |

Verify ffmpeg is on `PATH` with `ffmpeg -version`.

## Setup

```bash
git clone https://github.com/ujjalcal/medley.git
cd medley

# Install Python deps. On macOS system Python, --user is the safest target.
python3 -m pip install --user -r requirements.txt
```

If pip refuses because the Python is "externally managed" (Homebrew Python on recent macOS), either add `--break-system-packages` or create a venv first (`python3 -m venv .venv && source .venv/bin/activate`).

No build step, no `node_modules` — every page is a self-contained HTML file and the server is Python stdlib only.

## Running

```bash
python3 medley_server.py --open
```

Starts the server at http://localhost:8765/ and opens the editor in your default browser. Ctrl+C stops it. The `Start Medley Studio.command` file is a double-clickable macOS Finder launcher.

The server binds `127.0.0.1` only — never exposed to the network. All file paths accepted via the API are constrained to stay inside the `--root` directory (default: the folder containing `medley_server.py`).

## Workflows

### Build a medley video

1. Open the editor at http://localhost:8765/.
2. **Download from YouTube** — paste one or more URLs, set max height + output folder, click ⬇ Download. Files land in `downloads/<folder>/`.
3. **Pick a folder of videos** (server-side picker or `Upload videos…`).
4. For each clip, scrub to set the **in** and **out** points, then add it to the timeline.
5. Drag clips to reorder.
6. Set **Crossfade** (e.g. `0.6s`) for smooth transitions, and **Pitch** in semitones if you want the whole medley shifted into your singing range.
7. Click **⬇ Build medley video**. ffmpeg renders to `downloads/<folder>/medley.mp4` and the result plays inline.

Loudness is normalized across clips to ~-16 LUFS by default (EBU R128, single-pass `loudnorm`). Pass `--no-normalize` on the CLI to skip it.

### Shift pitch of a single file

1. Sidebar → 🎵 **Pitch** (or http://localhost:8765/pitch).
2. Drop or click to pick a video (mp4/mov/webm/mkv) or audio (mp3/m4a/wav/ogg/flac) file.
3. Set semitones (±12). Click ▶ **Test** — uploads + renders a preview in seconds (video stream is passthrough; only audio is re-encoded).
4. Adjust pitch and re-Test as needed; each Test overwrites the same preview slot.
5. Click 💾 **Save** to commit the current preview to `downloads/pitch/<stem>_pitch+N.<ext>`.

### CLI (no server, direct render)

```bash
# Render a medley from an existing cut-list JSON:
python3 build_medley.py path/to/medley.json --pitch +2 --crossfade 0.6

# Download YouTube videos:
python3 youtube-downloader/scripts/youtube_downloader.py -o downloads/main "URL1" "URL2"
```

Both scripts support `--help`.

## Project layout

```
medley/
├── medley_server.py                       # Local server. Routes /, /pitch, /api/*.
├── medley_studio.html                     # Editor UI for assembling a medley.
├── pitch_studio.html                      # Standalone pitch-shifter UI.
├── build_medley.py                        # ffmpeg-based renderer. Reads medley.json, writes mp4.
├── youtube-downloader/
│   ├── SKILL.md                           # Anthropic skill bundle metadata.
│   └── scripts/
│       └── youtube_downloader.py          # yt-dlp wrapper CLI.
├── downloads/                             # Source clips, rendered output (gitignored).
├── requirements.txt
└── Start Medley Studio.command            # macOS double-click launcher.
```

## Notes for Claude / AI agents continuing development

These are the patterns that hold across this codebase — internalize them before editing.

- **HTML/CSS/JS changes don't require a server restart.** `medley_server.py` reads HTML from disk on every `GET` request. Just refresh the browser.
- **`medley_server.py` changes do require a restart** — its routes and handlers live in the running Python process. `pkill -f medley_server.py && python3 medley_server.py` is the canonical restart.
- **`build_medley.py` and `youtube_downloader.py` are invoked as subprocesses per API call.** Edits to them are picked up automatically on the next request — no restart needed.
- **ffmpeg failures bubble up via API response `stderr`/`stdout` fields**, then surface in the UI as a "Build failed" modal. Read those before guessing.
- **Filter graphs live in `build_medley.py`.** When adding audio effects (loudness, pitch, EQ, etc.), insert them into the per-input chain or as a final post-mix stage; the existing `[mixa] → [outa]` pattern is the place for post-mix effects.
- **Pitch shifting uses `asetrate=44100*R, aresample=44100, atempo=1/R`** — Homebrew ffmpeg doesn't ship with `rubberband`, so this 3-filter chain is the portable approach (good for ±3 semitones, audible artifacts beyond ±6).
- **Loudness uses single-pass EBU R128 (`loudnorm=I=-16:TP=-1.5:LRA=11`)** applied per input before the uniform `aformat` step. Upgrading to two-pass would require a measurement step per clip.
- **Sidebar nav and theme are shared CSS** copy-pasted in both `medley_studio.html` and `pitch_studio.html`. Keep them in sync when editing — there's no template engine.
- **Preferences (sidebar collapse, light/dark theme) live in `localStorage`** and are restored via a tiny inline `<script>` in `<head>` before paint to avoid a flash of the wrong state.
- **Don't commit anything under `downloads/`** — it's gitignored and contains heavy media.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: ffmpeg not found in PATH` | ffmpeg not installed | `brew install ffmpeg` |
| `ERROR: yt-dlp is not installed` | Python package missing | `python3 -m pip install --user -r requirements.txt` |
| YouTube download fails with "format not available" | yt-dlp out of date | `python3 -m pip install --user --upgrade yt-dlp` |
| Port 8765 in use | Existing server | `lsof -i :8765 -t \| xargs kill` then retry |
| Editor shows "Build failed" / generic ffmpeg error | Filter graph or codec issue | Inspect `data.stderr` in the response; usually a missing input or a filter typo |
