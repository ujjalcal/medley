---
name: youtube-downloader
description: Download YouTube videos or extract their audio as mp3 using a bundled yt-dlp wrapper script. Use this skill whenever the user pastes a YouTube URL (youtube.com/watch, youtu.be/, /shorts/, /playlist) and asks to download, save, grab, rip, fetch, archive, get-offline, or convert to mp3 — even when they don't say the word "skill" or explicitly mention yt-dlp. Also trigger when the user asks for a YouTube downloader, wants to pull audio out of a video, wants to save a playlist locally, or wants to back up a channel they own. Do NOT trigger for non-YouTube video sites (Vimeo, TikTok, etc.) unless the user explicitly asks to handle them with yt-dlp anyway.
---

# YouTube Downloader

This skill downloads YouTube videos (mp4) or extracts audio (mp3) using a small wrapper around `yt-dlp`. The wrapper script is bundled — do not reinvent it. Just invoke it.

## When to use

Trigger as soon as the user shares a YouTube URL with downloading intent. Common phrasings: "download this", "save this video", "get me the mp3", "grab this playlist", "rip the audio", "make this offline". A bare URL with no verb is also a strong signal in a context where downloading was already discussed.

## Legal / ToS note

YouTube's Terms of Service generally prohibit downloading content without permission from the rights holder or YouTube itself. This skill is intended for personal/educational use, content the user owns, or Creative-Commons / explicitly licensed material. If the user's intent looks like large-scale redistribution of copyrighted material, gently flag the concern once — then respect their judgment. Do not refuse routine personal-use downloads.

## How to run it

The bundled script lives at `scripts/youtube_downloader.py` (relative to this SKILL.md). It depends on:

- **yt-dlp** — install with `pip install --upgrade yt-dlp` (or `pip install --break-system-packages --upgrade yt-dlp` in sandboxed environments).
- **ffmpeg** — required for merging best-quality video+audio streams and for mp3 extraction. macOS: `brew install ffmpeg`. Linux: `apt install ffmpeg`. Windows: https://ffmpeg.org/download.html.

If `yt-dlp` is missing, install it first; do not silently fall back to a worse downloader. If `ffmpeg` is missing and the user wants mp3 or merged best quality, tell them and offer to install or to fall back to a single-stream format.

### Invocation patterns

Run the script from a shell. The script accepts URLs as positional args plus a few flags:

```
python scripts/youtube_downloader.py [-o OUT] [-a] [-q HEIGHT] [--playlist] [-f URLS_FILE] [--quiet] URL [URL ...]
```

| Flag | Meaning |
| --- | --- |
| `-o, --output` | Output directory. Default `./downloads`. **Always pass an absolute path inside the user's workspace folder** so the file is visible to them. |
| `-a, --audio` | Audio-only (mp3 at 192 kbps, with thumbnail embedded). |
| `-q, --quality HEIGHT` | Cap video height in pixels (e.g. 720, 1080, 1440, 2160). Omit for best available. |
| `--playlist` | Download the whole playlist when given a playlist URL (default downloads only the single video). |
| `-f, --file PATH` | Read URLs from a text file, one per line (`#` comments allowed). |
| `--quiet` | Suppress yt-dlp progress lines. |

### Common recipes

**Single video, best quality, into the user's workspace:**
```bash
python scripts/youtube_downloader.py -o "$WORKSPACE/downloads" "https://www.youtube.com/watch?v=ID"
```

**Audio-only (mp3):**
```bash
python scripts/youtube_downloader.py -a -o "$WORKSPACE/downloads" "https://youtu.be/ID"
```

**Cap quality at 1080p (smaller file, fewer fragment merges):**
```bash
python scripts/youtube_downloader.py -q 1080 -o "$WORKSPACE/downloads" "URL"
```

**Whole playlist:**
```bash
python scripts/youtube_downloader.py --playlist -o "$WORKSPACE/downloads" "https://www.youtube.com/playlist?list=..."
```

**Batch from a file:**
```bash
python scripts/youtube_downloader.py -f urls.txt -o "$WORKSPACE/downloads"
```

## Behavior expectations

1. **Pick the right output directory.** If the user has a workspace/projects folder available, save into a `downloads/` subdirectory there so the file is reachable from their file manager. Don't dump into a sandbox-only path the user can't see.

2. **Tell the user what you're about to do** for non-trivial requests (e.g. a 4K download, a 200-item playlist) so they can cancel before bandwidth is spent.

3. **Surface failures clearly.** The script keeps going on errors and prints `FAILED: <url>` to stderr — repeat any failures back to the user so they don't think everything succeeded.

4. **Filename hygiene.** The script names files `<title> [<videoid>].<ext>`. Don't try to rename further unless asked; the video ID makes re-downloads idempotent and helps the user re-find a video on YouTube later.

5. **After download, share the file with a direct link.** Give the user the path and (in environments that support it) a clickable link to the merged `.mp4` or `.mp3`. Intermediate fragment files (`.f137.mp4`, `.f140.m4a`) may linger if the script can't delete them — mention they're safe to remove.

## Edge cases

- **Age-gated or private videos** require cookies. If the user hits an age-gate error, suggest exporting cookies from their browser (`yt-dlp --cookies-from-browser chrome`) and offer to re-run with that flag added.
- **Live streams in progress** — yt-dlp can record but it'll keep going until the stream ends; ask the user if they want a fixed-length capture.
- **Region-locked content** — the error message will mention "not available in your country". A user-supplied proxy (`--proxy`) is the only clean fix; don't auto-add one.
- **Subtitles** — not enabled by default. If the user asks, you can add `--write-subs --sub-langs all` directly to a `yt_dlp` call, or extend the bundled script.

## Why a bundled script and not raw yt-dlp?

The wrapper sets sensible defaults (mp4 merge, mp3 192 kbps, retry counts, thumbnail embedding for audio, robust output template) so the user gets a good result on the first try without you having to remember a five-line yt-dlp incantation. Use the script unless the user explicitly asks for raw yt-dlp control.
