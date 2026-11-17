# Player Converter

Convert movies into a format your external media player can play, and copy them onto a
removable drive — in one go.

Player Converter is a small GTK 4 / libadwaita desktop app (with a command-line mode) built
on `ffmpeg`. Drop movies in, pick the USB drive, and it works out for each file the
**lightest change** that makes it playable, shows you how many will fit, then converts and
copies them.

## Why

Many TVs, car head units and cheap media players only decode a narrow slice of video
formats. This one was profiled by testing on the actual player:

| | Supported | Not supported |
|---|---|---|
| **Video** | H.264, 8-bit, level ≤ 4.1, up to 1920×1080, up to ~30 fps | HEVC/H.265, 10-bit, AV1, VP9, MPEG-4 Part 2, level > 4.1, > 30 fps, > 1080p |
| **Audio** | AAC-LC, stereo or 5.1 | AC3 / E-AC3 (Dolby), DTS, TrueHD, HE-AAC, FLAC, Opus, … |
| **Container** | MKV, MP4, AVI | — |

## What it does to each file

| Situation | Action | Re-encode? |
|---|---|---|
| Already plays | Copied to the drive byte-for-byte | No |
| H.264 that is only *tagged* above level 4.1 | Level flag rewritten to 4.1 | No |
| Dolby / DTS / other audio | Audio converted to AAC (5.1 kept, 7.1 downmixed to 5.1) | Audio only |
| HEVC, 10-bit, too many reference frames, > 1080p, > 30 fps, … | Video re-encoded to H.264 High@4.1 | Yes |

While converting it also:

- keeps **all audio tracks, subtitles, chapters, metadata and attachments** (fonts) — output is MKV;
- **deinterlaces** interlaced sources and **tone-maps HDR to SDR** (when ffmpeg has `zscale`);
- halves frame rates above 30 fps (e.g. 60 → 30);
- by default **never makes a file bigger than the original** (bitrate is capped to fit);
- checks the converted file is complete before copying it;
- writes to a `.part` file and only renames it when fully synced, so a pulled drive never
  leaves a half-written movie behind.

## Features

- **Fit preview** – for every movie: what will change, why, and the estimated size; the
  space bar shows how many selected movies fit on the drive (keeping 64 MB free).
- **FAT32 aware** – flags files over the 4 GB per-file limit and cleans up file names.
- **Skips duplicates** – movies already on the drive are marked and skipped.
- **Hardware encoding** – detects and offers NVIDIA NVENC, Intel Quick Sync and VA-API if
  they actually work on your machine; otherwise uses libx264 (best quality per GB).
- **Settings** – quality (2.5 / 4 / 6 Mb/s at 1080p), maximum resolution
  (1080p / 720p / 480p), "never bigger than original", and "also shrink movies that
  already play" to squeeze more onto the drive.
- **Drag and drop** files or whole folders, or open videos with it from your file manager.
- **Eject Drive** button and a desktop notification when the batch finishes.
- Remembers your settings, drive and folder between runs.

## Requirements

- Linux
- Python 3.10+
- `ffmpeg` and `ffprobe`
- For the GUI: GTK 4, libadwaita ≥ 1.5 and PyGObject

On **Ubuntu / Debian** (Ubuntu 24.04 or newer):

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg
```

On **Fedora**:

```sh
sudo dnf install python3-gobject gtk4 libadwaita ffmpeg   # ffmpeg from RPM Fusion
```

On **Arch**:

```sh
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg
```

## Installation

### Option 1 — Debian package (Ubuntu / Debian)

```sh
make deb
sudo apt install ./dist/player-converter_1.0.0_all.deb
```

`apt` pulls in all the dependencies above. Remove it with `sudo apt remove player-converter`.

### Option 2 — Install for your user only (any distro, no root)

```sh
make install          # installs into ~/.local
make uninstall        # removes it again
```

This adds the `player-converter` command, an app-menu entry and an icon. For a
system-wide install use `sudo make install PREFIX=/usr/local`.

### Option 3 — pipx / pip

```sh
pipx install --system-site-packages .
```

`--system-site-packages` lets it use your distro's PyGObject, which is not listed as a pip
dependency on purpose (building it with pip needs a compiler and development headers).
This installs the command only — use Option 2 if you also want the app-menu entry.

### Option 4 — Just run it

It's a single file:

```sh
python3 player_converter.py
```

## Usage

### GUI

1. Plug in the USB drive and start **Player Converter** from the app menu
   (or run `player-converter`, optionally followed by movie files).
2. Choose the drive (or any folder) and, optionally, a sub-folder on it.
3. Add movies — **Add Files…**, **Add Folder…**, or drag and drop.
4. Check the plan and size estimate for each movie; untick anything you don't want.
5. Click **Convert & Copy**. When it finishes, click **Eject Drive**.

### Command line

The CLI doesn't need GTK, so it also works over SSH or on a headless machine:

```sh
player-converter --cli DEST_DIR FILE... [options]
```

| Option | Meaning |
|---|---|
| `--encoder x264\|nvenc\|qsv\|vaapi` | Video encoder (default `x264`) |
| `--quality 0\|1\|2` | 2.5 / 4 / 6 Mb/s at 1080p (default `1`) |
| `--max-height 1080\|720\|480` | Maximum output height (default `1080`) |
| `--shrink-all` | Also re-encode playable movies above `--max-height` |
| `--allow-bigger` | Don't cap output size at the original's |

Example:

```sh
player-converter --cli /media/$USER/USB ~/Videos/*.mkv --max-height 720
```

```
Some Movie.mkv
  1:52:10 · HEVC 10-bit → re-encode H.264 2.4 Mb/s (1280×720) · E-AC3 5.1 → AAC · convert ≈ 2.4 GB
  - HEVC video isn't supported
  - 10-bit video isn't supported
  - E-AC3 audio isn't supported
  -> /media/me/USB/Some Movie.mkv (2.3 GB)
```

## Files

| Path | Purpose |
|---|---|
| `~/.config/player-converter.json` | Saved settings |
| `~/.cache/player-converter/` | Temporary files during conversion (cleaned up automatically) |

Converted movies are built in the temp folder first, so you need free space there roughly
equal to the largest converted movie.

## Adapting it to a different player

The player's limits are constants at the top of [`player_converter.py`](player_converter.py)
(`MAX_W`, `MAX_H`, `MAX_LEVEL`, `MAX_FPS`, `OK_H264_PROFILES`, `OK_PIX_FMTS`,
`PASSTHROUGH_EXTS`) together with the audio rules in `make_plan()`. Change them to match
what your device accepts.

## Development

```sh
make check    # syntax check + validate the .desktop and AppStream files
make deb      # build the .deb into dist/
make clean
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
