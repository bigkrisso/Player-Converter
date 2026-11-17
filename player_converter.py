#!/usr/bin/env python3
"""
Player Converter — convert movies into a format the external media player
can play, and copy them onto a removable drive.

Player profile (worked out by testing on the actual player):
  * Video : H.264, 8-bit, level 4.1 or lower, up to 1920x1080, up to ~30 fps
  * Audio : AAC only (stereo or 5.1). AC3 / E-AC3 (Dolby) are NOT supported.
  * Container : MKV, MP4 and AVI all work.

Anything that doesn't match is fixed with the lightest change possible:
  * H.264 that is only tagged above level 4.1 -> relabelled (no re-encode)
  * Dolby / DTS / other audio               -> converted to AAC
  * HEVC, 10-bit, MPEG-4 Part 2, etc.        -> re-encoded to H.264

Usage:
  player_converter.py                 start the GUI
  player_converter.py --cli DEST FILE...  convert from the command line (no GUI)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Player profile
# --------------------------------------------------------------------------
MAX_W, MAX_H = 1920, 1088          # largest frame the player accepts as-is
TARGET_MAX_H = 1080                # height we scale down to when re-encoding
MAX_LEVEL = 41                     # H.264 level 4.1
MAX_FPS = 30.5
OK_H264_PROFILES = {"Baseline", "Constrained Baseline", "Main", "High"}
OK_PIX_FMTS = {"yuv420p", "yuvj420p"}
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1
DRIVE_RESERVE = 64 * 1024 ** 2     # always leave this much free on the drive
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg",
              ".ts", ".m2ts", ".mts", ".webm", ".flv", ".vob", ".divx", ".ogv", ".3gp"}
PASSTHROUGH_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}   # containers the player handles

# (label, H.264 video bitrate in kbit/s at 1080p — scaled down for smaller frames)
QUALITIES = [("Smaller files (2.5 Mb/s)", 2500),
             ("Balanced (4 Mb/s)", 4000),
             ("Higher quality (6 Mb/s)", 6000)]
DEFAULT_QUALITY = 1
RESOLUTIONS = [("Original (up to 1080p)", 1080), ("720p", 720), ("480p (SD)", 480)]

ENCODERS = {  # key: (label, ffmpeg encoder name)
    "x264":  ("CPU – libx264 (best quality per GB)", "libx264"),
    "nvenc": ("NVIDIA GPU – NVENC (fast)", "h264_nvenc"),
    "qsv":   ("Intel Quick Sync (fast)", "h264_qsv"),
    "vaapi": ("VA-API GPU (fast)", "h264_vaapi"),
}
VAAPI_DEVICE = "/dev/dri/renderD128"

CONFIG_PATH = os.path.join(os.path.expanduser("~/.config"), "player-converter.json")
TEMP_DIR = os.path.join(os.path.expanduser("~/.cache"), "player-converter")

CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2",
               "vc1": "VC-1", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "wmv3": "WMV",
               "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3", "dts": "DTS", "truehd": "TrueHD",
               "mp3": "MP3", "mp2": "MP2", "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis",
               "pcm_s16le": "PCM", "pcm_s24le": "PCM", "wmav2": "WMA"}
SUB_COPY = {"subrip", "ass", "ssa", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "webvtt"}
SUB_TO_SRT = {"mov_text", "text"}


def has_filter(name):
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                             text=True, timeout=20).stdout
        return re.search(rf"\s{re.escape(name)}\s", out) is not None
    except Exception:
        return False


HAS_ZSCALE = has_filter("zscale")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def human_time(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_rate(s):
    try:
        num, den = str(s).split("/")
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def stream_bitrate(s):
    v = s.get("bit_rate")
    if v and str(v).isdigit():
        return int(v)
    for k, v in (s.get("tags") or {}).items():
        if k.upper().startswith("BPS") and str(v).isdigit():
            return int(v)
    return 0


def channel_name(ch):
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch")


def fat_safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.rstrip(" .") or "movie"


def fs_type(path):
    """Filesystem type of the mount containing path (from /proc/mounts)."""
    best, best_type = "", ""
    try:
        path = os.path.realpath(path)
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, best_type = mnt, parts[2]
    except OSError:
        pass
    return best_type


def is_fat(path):
    return fs_type(path) in ("vfat", "msdos", "fat")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Probing and planning
# --------------------------------------------------------------------------
@dataclass
class MediaInfo:
    path: str
    size: int
    duration: float
    container: str            # 'matroska', 'mp4', 'avi', or ffprobe's format name
    v_index: int
    v_codec: str
    v_profile: str
    v_level: int
    v_pix: str
    width: int
    height: int
    fps: float
    fps_str: str
    v_bitrate: int
    hdr: bool
    bit_depth: int
    interlaced: bool
    audio: list               # dicts: index, codec, channels, bitrate, lang
    subs: list                # dicts: index, codec, lang
    has_attachments: bool
    ref_frames: int = 0       # real H.264 max_num_ref_frames / DPB size (from the SPS)


def h264_ref_frames(path, v_index):
    """Read the real reference-frame count from the H.264 SPS (ffprobe's 'refs' is wrong)."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", path,
                            "-map", f"0:{v_index}", "-c", "copy", "-frames:v", "1",
                            "-bsf:v", "trace_headers", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        vals = []
        for line in r.stderr.splitlines():
            if ("max_num_ref_frames" in line or "max_dec_frame_buffering" in line) and "=" in line:
                try:
                    vals.append(int(line.rsplit("=", 1)[1]))
                except ValueError:
                    pass
        return max(vals) if vals else 0
    except Exception:
        return 0


def max_ref_frames_ok(w, h):
    """Reference frames the player copes with: the strict H.264 level 4.1 DPB limit
    (32768 macroblocks) — e.g. 4 at 1080p. Files with 5 at 1920x1040 and 16 at 1920x802
    were confirmed NOT to play."""
    mbs = max(1, ((w + 15) // 16) * ((h + 15) // 16))
    return max(1, min(16, 32768 // mbs))


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "ffprobe could not read the file")
    d = json.loads(r.stdout or "{}")
    fmt = d.get("format", {})
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    if v is None:
        raise RuntimeError("No video stream found")

    size = int(fmt.get("size") or os.path.getsize(path))
    duration = float(fmt.get("duration") or v.get("duration") or 0)
    fname = fmt.get("format_name", "")
    container = ("matroska" if "matroska" in fname else
                 "mp4" if "mp4" in fname or "mov" in fname else
                 "avi" if fname == "avi" else fname)

    audio = []
    for s in streams:
        if s.get("codec_type") == "audio":
            audio.append({"index": s["index"], "codec": s.get("codec_name", "?"),
                          "profile": s.get("profile", "") or "",
                          "channels": int(s.get("channels") or 2),
                          "bitrate": stream_bitrate(s),
                          "lang": (s.get("tags") or {}).get("language", "")})
    subs = [{"index": s["index"], "codec": s.get("codec_name", ""),
             "lang": (s.get("tags") or {}).get("language", "")}
            for s in streams if s.get("codec_type") == "subtitle"]
    has_att = any(s.get("codec_type") == "attachment" for s in streams)

    pix = v.get("pix_fmt", "")
    depth = 10 if ("10" in pix) else 12 if ("12" in pix) else 8
    try:
        depth = int(v.get("bits_per_raw_sample") or depth)
    except ValueError:
        pass
    fps_str = v.get("avg_frame_rate") or "0/0"
    fps = parse_rate(fps_str)
    if not fps:
        fps_str = v.get("r_frame_rate") or "0/0"
        fps = parse_rate(fps_str)

    v_br = stream_bitrate(v)
    if duration > 0:
        # Stream bitrate tags (MKV "BPS") can be stale leftovers from an earlier
        # conversion, so only trust them if they agree with the actual file size.
        total = int(size * 8 / duration)
        a_known = sum(a["bitrate"] or a["channels"] * 96000 for a in audio)
        derived = max(total - a_known, 0)
        if not v_br or v_br > total or v_br < derived * 0.8:
            v_br = derived

    refs = h264_ref_frames(path, v["index"]) if v.get("codec_name") == "h264" else 0

    return MediaInfo(
        ref_frames=refs,
        path=path, size=size, duration=duration, container=container,
        v_index=v["index"], v_codec=v.get("codec_name", "?"),
        v_profile=v.get("profile", "") or "", v_level=int(v.get("level") or 0),
        v_pix=pix, width=int(v.get("width") or 0), height=int(v.get("height") or 0),
        fps=fps, fps_str=fps_str, v_bitrate=v_br,
        hdr=v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        bit_depth=depth,
        interlaced=v.get("field_order") in ("tt", "bb", "tb", "bt"),
        audio=audio, subs=subs, has_attachments=has_att)


@dataclass
class Plan:
    video: str                # 'copy' | 'relabel' | 'encode'
    target_bps: int
    out_w: int
    out_h: int
    fps_filter: str
    audio: list               # dicts: index, action('copy'|'aac'), channels, bitrate
    subs: list                # dicts: index, action('copy'|'srt')
    passthrough: bool         # file can be copied exactly as it is
    out_name: str
    est_bytes: int
    summary: str
    reasons: list = field(default_factory=list)


def fit_dims(w, h, max_h=TARGET_MAX_H):
    max_w = MAX_W * max_h // TARGET_MAX_H          # 1920 for 1080p, 1280 for 720p, 853 for 480p
    s = min(1.0, max_w / w if w else 1, max_h / h if h else 1)
    return max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2)


def make_plan(info, quality_kbps, no_grow=True, max_h=TARGET_MAX_H, shrink_all=False):
    reasons = []
    # ---- video
    h264_ok = (info.v_codec == "h264" and info.v_pix in OK_PIX_FMTS
               and info.v_profile in OK_H264_PROFILES
               and info.width <= MAX_W and info.height <= MAX_H
               and (info.fps <= MAX_FPS or info.fps == 0))
    too_many_refs = info.ref_frames > max_ref_frames_ok(info.width, info.height)
    if h264_ok and too_many_refs:
        h264_ok = False
        reasons.append(f"uses {info.ref_frames} reference frames — more than the player's "
                       f"decoder can hold (max {max_ref_frames_ok(info.width, info.height)})")
    if h264_ok and (info.v_level <= MAX_LEVEL):
        video = "copy"
    elif h264_ok and info.v_bitrate < 40_000_000:
        video = "relabel"
        reasons.append(f"H.264 level {info.v_level / 10:.1f} is above the player's 4.1 limit")
    else:
        video = "encode"
        if info.v_codec != "h264":
            reasons.append(f"{CODEC_NAMES.get(info.v_codec, info.v_codec)} video isn't supported")
        if info.bit_depth > 8:
            reasons.append(f"{info.bit_depth}-bit video isn't supported")
        if info.width > MAX_W or info.height > MAX_H:
            reasons.append("resolution above 1080p")
        if info.fps > MAX_FPS:
            reasons.append(f"{info.fps:.0f} fps is above 30")

    # Optionally shrink files that would otherwise play as-is, to save space
    if (shrink_all and video != "encode"
            and fit_dims(info.width, info.height, max_h) != (info.width, info.height)):
        video = "encode"
        reasons.append(f"shrinking to {max_h}p to save space")

    out_w, out_h, fps_filter, target = info.width, info.height, "", 0
    if video == "encode":
        out_w, out_h = fit_dims(info.width, info.height, max_h)
        if info.fps > MAX_FPS and "/" in info.fps_str:
            num, den = info.fps_str.split("/")
            fps_filter = f"fps={num}/{int(den) * 2}"
        ratio = max((out_w * out_h) / (1920 * 1080), 0.25)
        target = int(quality_kbps * 1000 * ratio)
        if info.v_bitrate:  # don't inflate low-bitrate sources
            target = min(target, max(int(info.v_bitrate * 1.6), 1_000_000))

    # ---- audio
    audio = []
    for a in info.audio:
        # Only plain AAC-LC is proven to play; HE-AAC (AAC+) is not supported.
        is_lc = a["codec"] == "aac" and a.get("profile", "LC") in ("LC", "")
        if is_lc and a["channels"] <= 6:
            audio.append({**a, "action": "copy"})
        else:
            ch = min(a["channels"], 6)
            br = {1: 128000, 2: 192000}.get(ch, 384000)
            if no_grow and a["bitrate"]:
                # Don't use more bits than the source track, but keep a sensible floor
                floor = {1: 96000, 2: 128000}.get(ch, 256000)
                br = max(floor, min(br, a["bitrate"] // 8000 * 8000))
            audio.append({**a, "action": "aac", "channels": ch, "bitrate": br, "src_channels": a["channels"]})
            if a["codec"] == "aac" and not is_lc:
                reasons.append(f"{a['profile'] or 'this AAC'} audio isn't supported (only AAC-LC)")
            elif a["codec"] == "aac":
                reasons.append(f"{channel_name(a['channels'])} audio will be downmixed to 5.1")
            else:
                reasons.append(f"{CODEC_NAMES.get(a['codec'], a['codec'])} audio isn't supported")

    # ---- subtitles (kept; MKV can hold all of these)
    subs = []
    for s in info.subs:
        if s["codec"] in SUB_COPY:
            subs.append({**s, "action": "copy"})
        elif s["codec"] in SUB_TO_SRT:
            subs.append({**s, "action": "srt"})

    # ---- keep re-encoded files no bigger than the original
    capped = False
    if no_grow and video == "encode" and info.duration > 0:
        abytes = sum((a["bitrate"] or a["channels"] * 96000) * info.duration / 8 for a in audio)
        budget = info.size * 0.97 - abytes - 150_000 * len(subs) - 1_000_000
        max_v = int(budget * 8 / info.duration / 1.006)
        if max_v < target:
            target = max(max_v, 600_000)
            capped = True

    ext = os.path.splitext(info.path)[1].lower()
    passthrough = (video == "copy" and all(a["action"] == "copy" for a in audio)
                   and ext in PASSTHROUGH_EXTS)
    base = os.path.basename(info.path)
    out_name = fat_safe_name(base if passthrough else os.path.splitext(base)[0] + ".mkv")

    # ---- size estimate
    if passthrough:
        est = info.size
    else:
        vbytes = (target if video == "encode" else info.v_bitrate) * info.duration / 8
        abytes = sum((a["bitrate"] or a["channels"] * 96000) * info.duration / 8 for a in audio)
        est = int((vbytes + abytes + 150_000 * len(subs)) * 1.006 + 1_000_000)

    # ---- one-line summary
    vname = CODEC_NAMES.get(info.v_codec, info.v_codec.upper())
    vdesc = f"{vname}{' ' + str(info.bit_depth) + '-bit' if info.bit_depth > 8 else ''}"
    if video == "copy":
        vpart = f"{vdesc} ✓"
    elif video == "relabel":
        vpart = f"{vdesc} level {info.v_level / 10:.1f} → 4.1 (no re-encode)"
    else:
        extra = []
        if (out_w, out_h) != (info.width, info.height):
            extra.append(f"{out_w}×{out_h}")
        if info.hdr:
            extra.append("HDR→SDR")
        if fps_filter:
            extra.append("30 fps")
        if capped:
            extra.append("capped at original size")
        vpart = f"{vdesc} → re-encode H.264 {target / 1e6:.1f} Mb/s" + (f" ({', '.join(extra)})" if extra else "")
    aparts = []
    for a in audio:
        cname = a["profile"] if a["codec"] == "aac" and a.get("profile") not in ("LC", "", None) else CODEC_NAMES.get(a["codec"], a["codec"])
        src = f"{cname} {channel_name(a.get('src_channels', a['channels']))}"
        aparts.append(f"{src} ✓" if a["action"] == "copy" else f"{src} → AAC")
    apart = ", ".join(aparts) if aparts else "no audio"
    if len(aparts) > 2:
        apart = f"{len(aparts)} audio tracks" + (" → AAC" if any(a["action"] == "aac" for a in audio) else " ✓")
    action = "copy as-is" if passthrough else "convert"
    summary = (f"{human_time(info.duration)} · {vpart} · {apart} · "
               f"{action} ≈ {human_size(est)}")
    return Plan(video, target, out_w, out_h, fps_filter, audio, subs, passthrough,
                out_name, est, summary, reasons)


# --------------------------------------------------------------------------
# ffmpeg command building and the conversion engine
# --------------------------------------------------------------------------
def video_encode_args(encoder, info, plan):
    vf = []
    if info.interlaced:
        vf.append("yadif")
    if info.hdr and HAS_ZSCALE:
        vf.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                  "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    if plan.fps_filter:
        vf.append(plan.fps_filter)
    if (plan.out_w, plan.out_h) != (info.width, info.height):
        vf.append(f"scale={plan.out_w}:{plan.out_h}:flags=lanczos")
    t = plan.target_bps
    rate = ["-b:v", str(t), "-maxrate", str(int(t * 1.5)), "-bufsize", str(int(t * 3))]
    pre = []
    if encoder == "nvenc":
        vf.append("format=yuv420p")
        c = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
             "-profile:v", "high", "-level:v", "4.1"]
    elif encoder == "qsv":
        vf.append("format=nv12")
        c = ["-c:v", "h264_qsv", "-preset", "medium", "-profile:v", "high", "-level:v", "41"]
    elif encoder == "vaapi":
        pre = ["-vaapi_device", VAAPI_DEVICE]
        vf.append("format=nv12,hwupload")
        c = ["-c:v", "h264_vaapi", "-profile:v", "high", "-level:v", "4.1"]
    else:
        vf.append("format=yuv420p")
        c = ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-level:v", "4.1"]
    if info.hdr:
        c += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    return pre, vf, c + rate


def build_ffmpeg_cmd(info, plan, encoder, out_path):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1"]
    pre, vf, vargs = video_encode_args(encoder, info, plan) if plan.video == "encode" else ([], [], [])
    if info.container == "avi":
        cmd += ["-fflags", "+genpts"]
    cmd += pre + ["-i", info.path, "-map", f"0:{info.v_index}"]
    for a in plan.audio:
        cmd += ["-map", f"0:{a['index']}"]
    for s in plan.subs:
        cmd += ["-map", f"0:{s['index']}"]
    if info.has_attachments:
        cmd += ["-map", "0:t?"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    # Drop the source's MKV statistics tags — they describe the old streams, not the new ones.
    for tag in ("BPS", "NUMBER_OF_BYTES", "NUMBER_OF_FRAMES", "DURATION", "_STATISTICS_TAGS",
                "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC"):
        for suffix in ("", "-eng"):
            cmd += ["-metadata:s", f"{tag}{suffix}="]
    if plan.video == "relabel":
        cmd += ["-bsf:v", f"h264_metadata=level={MAX_LEVEL / 10:.1f}"]
    elif plan.video == "encode":
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += vargs
    for i, a in enumerate(plan.audio):
        if a["action"] == "aac":
            cmd += [f"-c:a:{i}", "aac", f"-b:a:{i}", str(a["bitrate"]), f"-ac:a:{i}", str(a["channels"])]
    for j, s in enumerate(plan.subs):
        if s["action"] == "srt":
            cmd += [f"-c:s:{j}", "srt"]
    cmd += ["-max_muxing_queue_size", "4096", out_path]
    return cmd


def detect_encoders():
    """Return the encoder keys that actually work on this machine."""
    found = ["x264"]
    fake = MediaInfo(path="", size=0, duration=1, container="matroska", v_index=0,
                     v_codec="hevc", v_profile="", v_level=0, v_pix="yuv420p",
                     width=640, height=360, fps=24, fps_str="24/1", v_bitrate=0,
                     hdr=False, bit_depth=8, interlaced=False, audio=[], subs=[],
                     has_attachments=False)
    plan = Plan("encode", 1_000_000, 640, 360, "", [], [], False, "", 0, "")
    for key in ("nvenc", "qsv", "vaapi"):
        pre, vf, vargs = video_encode_args(key, fake, plan)
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + pre +
               ["-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=1", "-vf", ",".join(vf)] +
               vargs + ["-frames:v", "12", "-f", "null", "-"])
        try:
            if subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0:
                found.append(key)
        except Exception:
            pass
    return found


class Cancelled(Exception):
    pass


class Engine:
    """Converts one file at a time. on_progress(stage, fraction, detail_text)."""

    def __init__(self, encoder="x264", temp_dir=TEMP_DIR):
        self.encoder = encoder
        self.temp_dir = temp_dir
        self.cancel = threading.Event()
        self.proc = None

    def stop(self):
        self.cancel.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _run_ffmpeg(self, cmd, duration, on_progress):
        with tempfile.TemporaryFile() as errf:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                         text=True, bufsize=1)
            t, speed, last = 0.0, 0.0, 0.0
            for line in self.proc.stdout:
                k, _, v = line.strip().partition("=")
                if k in ("out_time_us", "out_time_ms"):
                    try:
                        t = int(v) / 1e6
                    except ValueError:
                        pass
                elif k == "speed":
                    try:
                        speed = float(v.rstrip("x"))
                    except ValueError:
                        pass
                elif k == "progress":
                    now = time.monotonic()
                    if now - last > 0.4 or v == "end":
                        last = now
                        frac = min(t / duration, 1.0) if duration else 0.0
                        eta = (duration - t) / speed if speed > 0 else 0
                        detail = f"{speed:.1f}× speed" + (f" · {human_time(eta)} left" if eta else "")
                        on_progress("encode", frac, detail)
            rc = self.proc.wait()
            self.proc = None
            if self.cancel.is_set():
                raise Cancelled()
            if rc != 0:
                errf.seek(0)
                lines = errf.read().decode(errors="replace").strip().splitlines()
                raise RuntimeError("ffmpeg: " + (lines[-1] if lines else f"exit code {rc}"))

    def _copy(self, src, dst, on_progress):
        part = dst + ".part"
        total = os.path.getsize(src)
        done, start, last = 0, time.monotonic(), 0.0
        try:
            with open(src, "rb") as fi, open(part, "wb") as fo:
                while True:
                    if self.cancel.is_set():
                        raise Cancelled()
                    chunk = fi.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    fo.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.4 or done == total:
                        last = now
                        rate = done / max(now - start, 0.001)
                        eta = (total - done) / rate if rate > 0 else 0
                        on_progress("copy", done / total if total else 1.0,
                                    f"{rate / 1e6:.0f} MB/s · {human_time(eta)} left")
                fo.flush()
                on_progress("copy", 1.0, "finishing write to drive…")
                os.fsync(fo.fileno())
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(part, dst)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise

    def convert(self, info, plan, dest_dir, on_progress):
        os.makedirs(dest_dir, exist_ok=True)
        final = os.path.join(dest_dir, plan.out_name)
        fat = is_fat(dest_dir)
        free = shutil.disk_usage(dest_dir).free - DRIVE_RESERVE
        if plan.est_bytes > free:
            raise RuntimeError(f"Not enough space on drive (needs ≈{human_size(plan.est_bytes)}, "
                               f"{human_size(max(free, 0))} free)")
        if plan.passthrough:
            self._copy(info.path, final, on_progress)
            return final

        os.makedirs(self.temp_dir, exist_ok=True)
        tfree = shutil.disk_usage(self.temp_dir).free
        if plan.est_bytes * 1.2 > tfree:
            raise RuntimeError(f"Not enough space in temp folder {self.temp_dir}")
        tmp = os.path.join(self.temp_dir, f"{uuid.uuid4().hex}.mkv")
        try:
            on_progress("encode", 0.0, "starting…")
            self._run_ffmpeg(build_ffmpeg_cmd(info, plan, self.encoder, tmp),
                             info.duration, on_progress)
            out_dur = probe(tmp).duration
            if info.duration and abs(out_dur - info.duration) > max(3.0, info.duration * 0.005):
                raise RuntimeError(f"Converted file is incomplete ({human_time(out_dur)} of "
                                   f"{human_time(info.duration)})")
            actual = os.path.getsize(tmp)
            if fat and actual > FAT32_MAX_FILE:
                raise RuntimeError(f"Converted file is {human_size(actual)} — too big for a FAT32 "
                                   "drive (4 GB limit). Try a smaller quality setting.")
            free = shutil.disk_usage(dest_dir).free - DRIVE_RESERVE
            if actual > free:
                raise RuntimeError(f"Converted file ({human_size(actual)}) no longer fits on the drive")
            self._copy(tmp, final, on_progress)
            return final
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Command-line mode (also handy for testing)
# --------------------------------------------------------------------------
def cli_main(args):
    if len(args) < 2:
        print("usage: player_converter.py --cli DEST_DIR FILE... [--encoder x264|nvenc|qsv|vaapi] "
              "[--quality 0|1|2] [--allow-bigger] [--max-height 1080|720|480] [--shrink-all]")
        return 2
    encoder, quality, max_h = "x264", DEFAULT_QUALITY, TARGET_MAX_H
    no_grow = "--allow-bigger" not in args
    shrink_all = "--shrink-all" in args
    args = [a for a in args if a not in ("--allow-bigger", "--shrink-all")]
    if "--max-height" in args:
        i = args.index("--max-height"); max_h = int(args[i + 1]); del args[i:i + 2]
    if "--encoder" in args:
        i = args.index("--encoder"); encoder = args[i + 1]; del args[i:i + 2]
    if "--quality" in args:
        i = args.index("--quality"); quality = int(args[i + 1]); del args[i:i + 2]
    dest, files = args[0], args[1:]
    eng = Engine(encoder)
    rc = 0
    for f in files:
        try:
            info = probe(f)
            plan = make_plan(info, QUALITIES[quality][1], no_grow, max_h, shrink_all)
            print(f"\n{os.path.basename(f)}\n  {plan.summary}")
            for r in plan.reasons:
                print(f"  - {r}")

            def prog(stage, frac, detail):
                print(f"\r  {stage:6s} {frac * 100:5.1f}%  {detail:40s}", end="", flush=True)
            out = eng.convert(info, plan, dest, prog)
            print(f"\n  -> {out} ({human_size(os.path.getsize(out))})")
        except Exception as e:
            print(f"\n  FAILED: {e}")
            rc = 1
    return rc


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cli":
    sys.exit(cli_main(sys.argv[2:]))


# --------------------------------------------------------------------------
# GUI (GTK 4 + libadwaita)
# --------------------------------------------------------------------------
import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "com.example.PlayerConverter"


class FileItem:
    def __init__(self, path):
        self.path = path
        self.info = None
        self.plan = None
        self.error = None
        self.fits = False
        self.state = "probing"   # probing | ready | queued | working | done | failed | skipped
        self.row = Adw.ActionRow(title=os.path.basename(path), use_markup=False)
        self.row.set_title_lines(1)
        self.row.set_subtitle_lines(2)
        self.row.set_tooltip_text(path)
        self.row.set_subtitle("Analysing…")
        self.check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
        self.row.add_prefix(self.check)
        self.row.set_activatable_widget(self.check)
        self.status = Gtk.Label(valign=Gtk.Align.CENTER, xalign=1)
        self.status.add_css_class("caption-heading")
        self.row.add_suffix(self.status)
        self.remove_btn = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Remove from list")
        self.remove_btn.add_css_class("flat")
        self.row.add_suffix(self.remove_btn)

    def set_status(self, text, style=None):
        self.status.set_text(text)
        for c in ("success", "warning", "error", "dim-label", "accent"):
            self.status.remove_css_class(c)
        if style:
            self.status.add_css_class(style)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Player Converter")
        self.set_default_size(960, 860)
        self.cfg = load_config()
        self.items = []
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.engine = None
        self.running = False
        self.dest_choices = []        # list of (label, root_path)
        self.custom_dest = self.cfg.get("custom_dest")
        self._rebuilding = False
        self.encoder_keys = ["x264"]

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                       margin_top=18, margin_bottom=18, margin_start=16, margin_end=16)
        clamp = Adw.Clamp(maximum_size=980, child=page)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        view.set_content(scroller)

        # ---- Destination
        dest_group = Adw.PreferencesGroup(
            title="Destination drive",
            description="The removable drive the media player will read from.")
        self.dest_row = Adw.ComboRow(title="Drive")
        self.dest_row.connect("notify::selected", lambda *a: self.on_dest_changed())
        choose_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Choose a different folder or drive…")
        choose_btn.add_css_class("flat")
        choose_btn.connect("clicked", self.on_choose_dest)
        self.dest_row.add_suffix(choose_btn)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Refresh drives and free space")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", lambda *_: self.refresh_drives())
        self.dest_row.add_suffix(refresh_btn)
        dest_group.add(self.dest_row)

        self.subdir_row = Adw.EntryRow(title="Folder on drive (blank = top level)")
        self.subdir_row.set_text(self.cfg.get("subdir", "Movies"))
        self.subdir_row.connect("changed", lambda *_: self.recompute_fit())
        dest_group.add(self.subdir_row)

        self.space_row = Adw.ActionRow(title="Space", use_markup=False)
        space_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            valign=Gtk.Align.CENTER, width_request=260)
        self.space_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self.space_bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_LOW)
        self.space_bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH)
        self.space_bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_FULL)
        space_box.append(self.space_bar)
        self.space_row.add_suffix(space_box)
        dest_group.add(self.space_row)
        page.append(dest_group)

        # ---- Settings
        set_group = Adw.PreferencesGroup(title="Conversion settings")
        self.quality_row = Adw.ComboRow(
            title="Quality for re-encoded video",
            subtitle="Only used for files that must be re-encoded (e.g. HEVC / 10-bit)",
            model=Gtk.StringList.new([q[0] for q in QUALITIES]))
        self.quality_row.set_selected(min(int(self.cfg.get("quality", DEFAULT_QUALITY)), len(QUALITIES) - 1))
        self.quality_row.connect("notify::selected", lambda *_: self.on_quality_changed())
        set_group.add(self.quality_row)
        self.nogrow_row = Adw.SwitchRow(
            title="Never make files bigger than the original",
            subtitle="Lowers the bitrate of re-encoded video (and converted audio) when needed. "
                     "H.264 needs more data than HEVC for the same quality, so this can soften the picture slightly.")
        self.nogrow_row.set_active(bool(self.cfg.get("no_grow", True)))
        self.nogrow_row.connect("notify::active", lambda *_: self.on_quality_changed())
        set_group.add(self.nogrow_row)
        self.res_row = Adw.ComboRow(
            title="Maximum resolution",
            subtitle="Lower resolution fits more movies; 720p usually looks better than "
                     "1080p squeezed to a very low bitrate",
            model=Gtk.StringList.new([r[0] for r in RESOLUTIONS]))
        self.res_row.set_selected(min(int(self.cfg.get("resolution", 0)), len(RESOLUTIONS) - 1))
        self.res_row.connect("notify::selected", lambda *_: self.on_quality_changed())
        set_group.add(self.res_row)
        self.shrink_row = Adw.SwitchRow(
            title="Also shrink movies that already play",
            subtitle="Re-encode files above the chosen resolution even if the player could play "
                     "them as-is (slower, but frees space)")
        self.shrink_row.set_active(bool(self.cfg.get("shrink_all", False)))
        self.shrink_row.connect("notify::active", lambda *_: self.on_quality_changed())
        set_group.add(self.shrink_row)
        self._sync_shrink_sensitivity()
        self.encoder_row = Adw.ComboRow(title="Video encoder",
                                        subtitle="Checking for hardware encoders…",
                                        model=Gtk.StringList.new([ENCODERS["x264"][0]]))
        set_group.add(self.encoder_row)
        page.append(set_group)

        # ---- Files
        files_group = Adw.PreferencesGroup(
            title="Movies",
            description="Add files from a local drive or a network share (in the file "
                        "picker use “Other Locations” to connect to a server), or drag "
                        "files and folders here. Files are copied in list order.")
        btns = Gtk.Box(spacing=6)
        b = Gtk.Button(label="Add Files…"); b.connect("clicked", self.on_add_files); btns.append(b)
        b = Gtk.Button(label="Add Folder…"); b.connect("clicked", self.on_add_folder); btns.append(b)
        self.clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear list")
        self.clear_btn.connect("clicked", self.on_clear)
        btns.append(self.clear_btn)
        self.add_buttons = btns
        files_group.set_header_suffix(btns)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        ph = Gtk.Label(label="No movies added yet", margin_top=24, margin_bottom=24)
        ph.add_css_class("dim-label")
        self.listbox.set_placeholder(ph)
        files_group.add(self.listbox)
        page.append(files_group)
        self.settings_widgets = [dest_group, set_group, btns]

        # ---- Bottom progress / action bar
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=10, margin_bottom=12, margin_start=16, margin_end=16)
        self.summary_label = Gtk.Label(xalign=0, wrap=True)
        self.summary_label.add_css_class("heading")
        bottom.append(self.summary_label)
        self.file_label = Gtk.Label(xalign=0, ellipsize=3)
        self.file_label.add_css_class("dim-label")
        bottom.append(self.file_label)
        self.file_bar = Gtk.ProgressBar(show_text=True)
        bottom.append(self.file_bar)
        self.total_bar = Gtk.ProgressBar(show_text=True)
        bottom.append(self.total_bar)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.eject_btn = Gtk.Button(label="Eject Drive")
        self.eject_btn.connect("clicked", self.on_eject)
        actions.append(self.eject_btn)
        self.cancel_btn = Gtk.Button(label="Cancel", sensitive=False)
        self.cancel_btn.add_css_class("destructive-action")
        self.cancel_btn.connect("clicked", self.on_cancel)
        actions.append(self.cancel_btn)
        self.start_btn = Gtk.Button(label="Convert && Copy")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", self.on_start)
        actions.append(self.start_btn)
        bottom.append(actions)
        self.progress_widgets = [self.file_label, self.file_bar, self.total_bar]
        for w in self.progress_widgets:
            w.set_visible(False)
        view.add_bottom_bar(bottom)

        # ---- Drag and drop
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)

        # ---- Drive monitoring
        self.vm = Gio.VolumeMonitor.get()
        self.vm.connect("mount-added", lambda *_: self.refresh_drives())
        self.vm.connect("mount-removed", lambda *_: self.refresh_drives())
        self.refresh_drives()
        GLib.timeout_add_seconds(10, self._periodic_space)

        self.connect("close-request", self.on_close_request)
        threading.Thread(target=self._detect_encoders, daemon=True).start()
        self.recompute_fit()

    # ------------------------------------------------------------ encoders
    def _detect_encoders(self):
        keys = detect_encoders()
        GLib.idle_add(self._set_encoders, keys)

    def _set_encoders(self, keys):
        self.encoder_keys = keys
        self.encoder_row.set_model(Gtk.StringList.new([ENCODERS[k][0] for k in keys]))
        saved = self.cfg.get("encoder", "x264")
        self.encoder_row.set_selected(keys.index(saved) if saved in keys else 0)
        hw = [ENCODERS[k][0].split(" (")[0] for k in keys if k != "x264"]
        self.encoder_row.set_subtitle(
            ("Hardware encoding available: " + ", ".join(hw)) if hw
            else "No hardware encoder found — using the CPU")

    def current_encoder(self):
        i = self.encoder_row.get_selected()
        return self.encoder_keys[i] if 0 <= i < len(self.encoder_keys) else "x264"

    def current_max_h(self):
        return RESOLUTIONS[max(0, self.res_row.get_selected())][1]

    def _sync_shrink_sensitivity(self):
        self.shrink_row.set_sensitive(self.current_max_h() < TARGET_MAX_H)

    def current_quality(self):
        return QUALITIES[max(0, self.quality_row.get_selected())][1]

    # ------------------------------------------------------------ destination
    def refresh_drives(self):
        prev = self.dest_root()
        choices = []
        for m in self.vm.get_mounts():
            root = m.get_root()
            p = root.get_path() if root else None
            if not p or not os.path.isdir(p):
                continue
            if m.can_eject() or p.startswith(("/run/media/", "/media/")):
                choices.append((m.get_name(), p))
        if self.custom_dest and os.path.isdir(self.custom_dest) and \
                not any(p == self.custom_dest for _, p in choices):
            choices.append((f"Folder: {os.path.basename(self.custom_dest) or self.custom_dest}",
                            self.custom_dest))
        self._rebuilding = True
        self.dest_choices = choices
        labels = [f"{n}  ({p})" for n, p in choices] or ["No removable drive found — plug one in"]
        self.dest_row.set_model(Gtk.StringList.new(labels))
        want = prev or self.cfg.get("dest")
        idx = next((i for i, (_, p) in enumerate(choices) if p == want), 0)
        self.dest_row.set_selected(idx)
        self._rebuilding = False
        self.on_dest_changed()

    def dest_root(self):
        i = self.dest_row.get_selected() if hasattr(self, "dest_row") else -1
        return self.dest_choices[i][1] if 0 <= i < len(self.dest_choices) else None

    def dest_dir(self):
        root = self.dest_root()
        if not root:
            return None
        sub = self.subdir_row.get_text().strip().strip("/")
        return os.path.join(root, sub) if sub else root

    def on_dest_changed(self):
        if self._rebuilding:
            return
        self.recompute_fit()

    def on_choose_dest(self, *_):
        dlg = Gtk.FileDialog(title="Choose destination drive or folder", modal=True)

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            p = f.get_path()
            if not p:
                self.toast("That location isn't available as a local folder")
                return
            self.custom_dest = p
            self.cfg["custom_dest"] = p
            self.cfg["dest"] = p
            self.subdir_row.set_text("")
            self.refresh_drives()
            idx = next((i for i, (_, q) in enumerate(self.dest_choices) if q == p), 0)
            self.dest_row.set_selected(idx)
        dlg.select_folder(self, None, done)

    def _periodic_space(self):
        if not self.running:
            self.recompute_fit()
        return True

    # ------------------------------------------------------------ adding files
    def video_filter_store(self):
        vf = Gtk.FileFilter()
        vf.set_name("Video files")
        for e in sorted(VIDEO_EXTS):
            vf.add_suffix(e.lstrip("."))
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(vf)
        store.append(allf)
        return store, vf

    def on_add_files(self, *_):
        dlg = Gtk.FileDialog(title="Add movies", modal=True)
        store, vf = self.video_filter_store()
        dlg.set_filters(store)
        dlg.set_default_filter(vf)
        last = self.cfg.get("last_source")
        if last and os.path.isdir(last):
            dlg.set_initial_folder(Gio.File.new_for_path(last))

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.add_gfiles([files.get_item(i) for i in range(files.get_n_items())])
        dlg.open_multiple(self, None, done)

    def on_add_folder(self, *_):
        dlg = Gtk.FileDialog(title="Add all movies in a folder", modal=True)
        last = self.cfg.get("last_source")
        if last and os.path.isdir(last):
            dlg.set_initial_folder(Gio.File.new_for_path(last))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.add_gfiles([f])
        dlg.select_folder(self, None, done)

    def on_drop(self, _target, value, _x, _y):
        if self.running:
            return False
        self.add_gfiles(list(value.get_files()))
        return True

    def add_gfiles(self, gfiles):
        paths, bad = [], 0
        for g in gfiles:
            p = g.get_path()
            if p:
                paths.append(p)
            else:
                bad += 1
        if bad:
            self.toast(f"{bad} item(s) aren't reachable as local paths — mount the share first")
        if paths:
            self.cfg["last_source"] = os.path.dirname(paths[0]) if os.path.isfile(paths[0]) else paths[0]
            threading.Thread(target=self._expand_paths, args=(paths,), daemon=True).start()

    def _expand_paths(self, paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for dirpath, dirnames, names in os.walk(p):
                    dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                    for n in sorted(names):
                        if os.path.splitext(n)[1].lower() in VIDEO_EXTS and not n.startswith("."):
                            files.append(os.path.join(dirpath, n))
            elif os.path.isfile(p):
                files.append(p)
        GLib.idle_add(self._add_paths, files)

    def _add_paths(self, files):
        existing = {i.path for i in self.items}
        added = 0
        for p in files:
            if p in existing:
                continue
            existing.add(p)
            item = FileItem(p)
            item.check.connect("toggled", lambda *_: self.recompute_fit())
            item.remove_btn.connect("clicked", lambda _b, it=item: self.remove_item(it))
            item.set_status("Analysing…", "dim-label")
            self.items.append(item)
            self.listbox.append(item.row)
            self.pool.submit(self._probe_item, item)
            added += 1
        if not added and files:
            self.toast("Those files are already in the list")
        elif not files:
            self.toast("No video files found there")
        self.recompute_fit()

    def _probe_item(self, item):
        try:
            info = probe(item.path)
            GLib.idle_add(self._probe_done, item, info, None)
        except Exception as e:
            GLib.idle_add(self._probe_done, item, None, str(e))

    def _probe_done(self, item, info, err):
        if item not in self.items:
            return
        item.info, item.error = info, err
        if err:
            item.state = "failed"
            item.row.set_subtitle(f"Can't read this file: {err}")
            item.check.set_active(False)
        else:
            item.state = "ready"
            self.update_plan(item)
        self.recompute_fit()

    def update_plan(self, item):
        if item.info:
            item.plan = make_plan(item.info, self.current_quality(), self.nogrow_row.get_active(),
                                  self.current_max_h(), self.shrink_row.get_active())
            item.row.set_subtitle(item.plan.summary)
            if item.plan.reasons:
                item.row.set_tooltip_text(item.path + "\n\nNeeds converting because:\n• " +
                                          "\n• ".join(dict.fromkeys(item.plan.reasons)))

    def on_quality_changed(self):
        self._sync_shrink_sensitivity()
        for it in self.items:
            self.update_plan(it)
        self.recompute_fit()

    def remove_item(self, item):
        if self.running:
            return
        self.items.remove(item)
        self.listbox.remove(item.row)
        self.recompute_fit()

    def on_clear(self, *_):
        if self.running:
            return
        for it in self.items:
            self.listbox.remove(it.row)
        self.items.clear()
        self.recompute_fit()

    # ------------------------------------------------------------ fit calculation
    def recompute_fit(self):
        if self.running:
            return
        root, ddir = self.dest_root(), self.dest_dir()
        free, total, fat = None, None, False
        if root and os.path.isdir(root):
            try:
                du = shutil.disk_usage(root)
                free, total = max(du.free - DRIVE_RESERVE, 0), du.total
                fat = is_fat(root)
            except OSError:
                pass
        used = fit_n = sel_n = skip_n = 0
        for it in self.items:
            it.fits = False
            if it.state in ("done",):
                continue
            if it.info is None:
                if it.error:
                    it.set_status("Unreadable", "error")
                continue
            if not it.check.get_active():
                it.set_status("Not selected", "dim-label")
                continue
            sel_n += 1
            if ddir and os.path.exists(os.path.join(ddir, it.plan.out_name)):
                it.set_status("Already on drive", "dim-label")
                skip_n += 1
                continue
            if fat and it.plan.est_bytes > FAT32_MAX_FILE:
                it.set_status("File over 4.29 GB (FAT32 limit)", "error")
                continue
            if free is None:
                it.set_status("Ready", "accent")
                it.fits = True
                fit_n += 1
                continue
            if used + it.plan.est_bytes <= free:
                used += it.plan.est_bytes
                it.fits = True
                fit_n += 1
                it.set_status("Fits ✓", "success")
            else:
                it.set_status("Won't fit", "warning")

        # Space row + summary
        if free is not None:
            fstype = fs_type(root)
            fat_note = " · FAT32: max 4.29 GB per file" if fat else ""
            self.space_row.set_subtitle(
                f"{human_size(free)} free of {human_size(total)} ({fstype or '?'}){fat_note}")
            self.space_row.set_title(f"After copying: {human_size(max(free - used, 0))} free")
            self.space_bar.set_value(min(used / free, 1.0) if free else 0)
        else:
            self.space_row.set_title("Space")
            self.space_row.set_subtitle("No destination selected")
            self.space_bar.set_value(0)

        probing = sum(1 for i in self.items if i.state == "probing")
        if not self.items:
            text = "Add movies to see how many will fit on the drive."
        else:
            text = f"{fit_n} of {sel_n} selected movie{'s' if sel_n != 1 else ''} will fit"
            if free is not None:
                text += f" — {human_size(used)} of {human_size(free)} free space"
            if skip_n:
                text += f" · {skip_n} already on drive"
            if probing:
                text += f" · analysing {probing}…"
        self.summary_label.set_text(text)
        self.start_btn.set_sensitive(fit_n > 0 and ddir is not None and not probing)
        self.eject_btn.set_sensitive(root is not None)

    # ------------------------------------------------------------ running
    def on_start(self, *_):
        jobs = [it for it in self.items if it.fits and it.plan]
        ddir = self.dest_dir()
        if not jobs or not ddir:
            return
        self.cfg.update({"quality": self.quality_row.get_selected(), "no_grow": self.nogrow_row.get_active(),
                         "resolution": self.res_row.get_selected(), "shrink_all": self.shrink_row.get_active(),
                         "encoder": self.current_encoder(),
                         "subdir": self.subdir_row.get_text(),
                         "dest": self.dest_root()})
        save_config(self.cfg)
        self.running = True
        self.engine = Engine(self.current_encoder())
        for w in self.settings_widgets:
            w.set_sensitive(False)
        for it in self.items:
            it.check.set_sensitive(False)
            it.remove_btn.set_sensitive(False)
        for it in jobs:
            it.state = "queued"
            it.set_status("Waiting", "dim-label")
        for w in self.progress_widgets:
            w.set_visible(True)
        self.start_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        self.eject_btn.set_sensitive(False)
        self.file_bar.set_fraction(0)
        self.total_bar.set_fraction(0)
        threading.Thread(target=self._worker, args=(jobs, ddir), daemon=True).start()

    def _worker(self, jobs, ddir):
        n = len(jobs)
        done = failed = 0
        for idx, it in enumerate(jobs):
            if self.engine.cancel.is_set():
                break
            GLib.idle_add(self._job_start, it, idx, n)
            two_stage = not it.plan.passthrough

            def prog(stage, frac, detail, it=it, idx=idx, two_stage=two_stage):
                if two_stage:
                    overall = frac * 0.8 if stage == "encode" else 0.8 + frac * 0.2
                else:
                    overall = frac
                GLib.idle_add(self._job_progress, it, idx, n, stage, frac, overall, detail)
            try:
                self.engine.convert(it.info, it.plan, ddir, prog)
                done += 1
                GLib.idle_add(self._job_end, it, True, None)
            except Cancelled:
                GLib.idle_add(self._job_end, it, False, "Cancelled")
                break
            except Exception as e:
                failed += 1
                GLib.idle_add(self._job_end, it, False, str(e))
        GLib.idle_add(self._all_done, done, failed, n)

    def _job_start(self, it, idx, n):
        it.state = "working"
        it.set_status("Starting…", "accent")
        self.summary_label.set_text(f"Movie {idx + 1} of {n}: {os.path.basename(it.path)}")
        self.total_bar.set_text(f"Overall: {idx} of {n} done")

    def _job_progress(self, it, idx, n, stage, frac, overall, detail):
        verb = "Encoding" if stage == "encode" else "Copying to drive"
        pct = int(frac * 100)
        it.set_status(f"{'Encoding' if stage == 'encode' else 'Copying'} {pct}%", "accent")
        self.file_label.set_text(f"{verb} — {detail}")
        self.file_bar.set_fraction(frac)
        self.file_bar.set_text(f"{verb} {pct}%")
        tot = (idx + overall) / n
        self.total_bar.set_fraction(tot)
        self.total_bar.set_text(f"Overall {int(tot * 100)}% — {idx} of {n} done")

    def _job_end(self, it, ok, err):
        if ok:
            it.state = "done"
            it.set_status("Done ✓", "success")
            it.check.set_active(False)
        else:
            it.state = "failed" if err != "Cancelled" else "ready"
            it.set_status("Cancelled" if err == "Cancelled" else "Failed", "error")
            if err and err != "Cancelled":
                it.row.set_subtitle(f"Failed: {err}")
                it.row.set_tooltip_text(f"{it.path}\n\n{err}")

    def _all_done(self, done, failed, n):
        cancelled = self.engine.cancel.is_set()
        self.running = False
        self.engine = None
        for w in self.settings_widgets:
            w.set_sensitive(True)
        for it in self.items:
            it.check.set_sensitive(True)
            it.remove_btn.set_sensitive(True)
            if it.state == "failed" and it.info:
                it.state = "ready"   # allow retry
        self.cancel_btn.set_sensitive(False)
        self.file_bar.set_fraction(1 if not cancelled else self.file_bar.get_fraction())
        self.file_label.set_text("")
        msg = f"Finished: {done} of {n} copied" + (f", {failed} failed" if failed else "")
        if cancelled:
            msg = f"Cancelled — {done} of {n} copied"
        self.total_bar.set_fraction(done / n if n else 1)
        self.total_bar.set_text(msg)
        self.recompute_fit()
        self.summary_label.set_text(msg + ". " + self.summary_label.get_text())
        self.toast(msg)
        if not cancelled and done:
            app = self.get_application()
            note = Gio.Notification.new("Player Converter")
            note.set_body(msg)
            app.send_notification("finished", note)

    def on_cancel(self, *_):
        if self.engine:
            self.cancel_btn.set_sensitive(False)
            self.file_label.set_text("Cancelling…")
            self.engine.stop()

    def on_close_request(self, *_):
        if not self.running:
            save_config({**self.cfg, "quality": self.quality_row.get_selected(), "no_grow": self.nogrow_row.get_active(),
                         "resolution": self.res_row.get_selected(), "shrink_all": self.shrink_row.get_active(),
                         "encoder": self.current_encoder(),
                         "subdir": self.subdir_row.get_text(),
                         "dest": self.dest_root() or self.cfg.get("dest")})
            return False
        dlg = Adw.AlertDialog(heading="Stop converting?",
                              body="A conversion is in progress. The movie being "
                                   "converted won't be copied.")
        dlg.add_response("keep", "Keep Going")
        dlg.add_response("quit", "Stop and Quit")
        dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "quit":
                if self.engine:
                    self.engine.stop()
                self.running = False
                self.get_application().quit()
        dlg.connect("response", resp)
        dlg.present(self)
        return True

    # ------------------------------------------------------------ eject
    def on_eject(self, *_):
        root = self.dest_root()
        if not root:
            return
        try:
            mount = Gio.File.new_for_path(root).find_enclosing_mount(None)
        except GLib.Error as e:
            self.toast(f"Can't eject: {e.message}")
            return
        op = Gtk.MountOperation.new(self)

        def done(m, res):
            try:
                if m.can_eject():
                    m.eject_with_operation_finish(res)
                else:
                    m.unmount_with_operation_finish(res)
                self.toast("Drive ejected — safe to remove")
            except GLib.Error as e:
                self.toast(f"Couldn't eject: {e.message}")
        if mount.can_eject():
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)
        else:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS | Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self)
        self.win.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        if not self.win.running:
            self.win.add_gfiles(list(files))


def cleanup_temp():
    try:
        for n in os.listdir(TEMP_DIR):
            if n.endswith(".mkv"):
                os.remove(os.path.join(TEMP_DIR, n))
    except OSError:
        pass


if __name__ == "__main__":
    cleanup_temp()
    sys.exit(App().run(sys.argv))
