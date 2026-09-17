"""Fetch video metadata and build an "interest" signal (0..1 per second) for a video.

Signal sources, in order of preference:
  1. YouTube "Most replayed" heatmap (comes back for free from yt-dlp)
  2. Chat replay density (Twitch VODs, YouTube stream VODs) via chat-downloader
  3. Audio energy (RMS loudness per second) as a last resort
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .config import BROWSER, COOKIES, DATA_DIR, FFMPEG, MAX_HEIGHT, YTDLP

log = logging.getLogger("cliper.sources")

Progress = Callable[[str, float], None]

_YT = re.compile(r"(?:^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$")
_TW = re.compile(r"(?:^|\.)twitch\.tv$")


def detect_platform(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    if _YT.search(host):
        return "youtube"
    if _TW.search(host):
        return "twitch"
    raise ValueError("Only YouTube and Twitch URLs are supported")


def video_key(url: str) -> str | None:
    """The video id inside a YouTube / Twitch URL, so the same video is recognised whatever the URL form."""
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url)
    host = (u.hostname or "").lower()
    if "youtu.be" in host:
        return u.path.strip("/").split("/")[0] or None
    if "youtube" in host:
        if u.path.startswith("/watch"):
            return (parse_qs(u.query).get("v") or [None])[0]
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            return parts[1]
    if "twitch" in host:
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "videos":
            return parts[1]
    return None


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    return url


# --------------------------------------------------------------------------- metadata

_BLOCKED = ("sign in to confirm", "not a bot", "http error 403", "login required", "private video")


def cookie_args(force_browser: bool = False) -> list[str]:
    """yt-dlp cookie flags: an explicit cookies.txt always; the browser only when asked for."""
    cookie_file = os.environ.get("CLIPER_COOKIES")
    if not cookie_file and (DATA_DIR / "cookies.txt").exists():
        cookie_file = str(DATA_DIR / "cookies.txt")
    if cookie_file:
        return ["--cookies", cookie_file]
    if force_browser and BROWSER:
        return ["--cookies-from-browser", BROWSER]
    return []


def run_ytdlp(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run yt-dlp with smart fallbacks for cookies, browser cookies, and player client overrides."""
    c_args = cookie_args()
    cmd = [*YTDLP, *c_args, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err_lower = (r.stderr or "").lower()
        is_blocked = any(k in err_lower for k in _BLOCKED)

        # 1. If explicit cookies failed/expired, retry without cookies
        if c_args and ("cookies are no longer valid" in err_lower or "reloaded" in err_lower or is_blocked):
            log.info("Explicit cookies failed/expired; retrying yt-dlp without cookies")
            r = subprocess.run([*YTDLP, *args], capture_output=True, text=True, timeout=timeout)

        # 2. If blocked and local browser detected (Firefox/Chrome/etc.), try browser cookies
        if r.returncode != 0 and BROWSER and is_blocked:
            log.info("YouTube refused request; retrying with local %s browser cookies", BROWSER)
            r = subprocess.run([*YTDLP, *cookie_args(True), *args], capture_output=True, text=True, timeout=timeout)

        # 3. Try ios,web player client fallback (most reliable for datacenter IPs)
        if r.returncode != 0 and is_blocked:
            log.info("Retrying yt-dlp with ios,web player client fallback")
            fallback_cmd = [*YTDLP, "--extractor-args", "youtube:player_client=ios,web", *c_args, *args]
            r = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=timeout)

        # 4. Try visionos,tv player client fallback
        if r.returncode != 0:
            log.info("Retrying yt-dlp with visionos,tv player client fallback")
            fallback_cmd = [*YTDLP, "--extractor-args", "youtube:player_client=visionos,tv,web", *args]
            r = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=timeout)

    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"yt-dlp exited {r.returncode}")
    return r


def fetch_info(url: str, job_dir: Path, want_subs: bool) -> dict:
    """Run yt-dlp once: write info.json (reused later for stream URLs) and, for YouTube,
    the auto-generated English subtitles in json3 (word-level timestamps)."""
    base = ["--no-playlist", "--skip-download", "--no-warnings", "-q", "-o", str(job_dir / "info")]
    run_ytdlp(base + ["--write-info-json", url])
    info_path = job_dir / "info.info.json"
    with info_path.open() as f:
        info = json.load(f)
    if want_subs:
        # only the original / plain English track; "en.*" would pull dozens of translations
        try:
            run_ytdlp(base + ["--write-subs", "--write-auto-subs", "--sub-langs", "en-orig,en",
                              "--sub-format", "json3", url])
        except RuntimeError as e:
            log.warning("subtitles unavailable: %s", e)
    return info


def find_subtitle_file(job_dir: Path) -> Path | None:
    for name in ("info.en-orig.json3", "info.en.json3"):
        if (job_dir / name).exists():
            return job_dir / name
    return next(iter(sorted(job_dir.glob("info.*.json3"))), None)


# --------------------------------------------------------------------------- signals

def _to_per_second(buckets: list[tuple[float, float, float]], duration: int) -> np.ndarray:
    """Turn [(start, end, value)] buckets into a per-second array."""
    out = np.zeros(duration, dtype=np.float32)
    for s, e, v in buckets:
        a, b = int(max(0, math.floor(s))), int(min(duration, math.ceil(e)))
        if b > a:
            out[a:b] = np.maximum(out[a:b], v)
    return out


def _normalize(x: np.ndarray) -> np.ndarray:
    x = np.nan_to_num(x.astype(np.float32))
    lo, hi = float(x.min()), float(x.max())
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


def heatmap_from_info(info: dict) -> np.ndarray | None:
    hm = info.get("heatmap")
    duration = int(info.get("duration") or 0)
    if not hm or not duration:
        return None
    buckets = [(h["start_time"], h["end_time"], float(h.get("value") or 0)) for h in hm]
    return _normalize(_to_per_second(buckets, duration))


def heatmap_from_audio(info: dict, job_dir: Path, progress: Progress) -> np.ndarray | None:
    """RMS loudness per second. yt-dlp fetches the smallest audio track (chunked, so YouTube does
    not throttle it), then ffmpeg decodes the local file in seconds."""
    duration = int(info.get("duration") or 0)
    if not duration:
        return None
    audio = _download_audio(job_dir / "info.info.json", job_dir, progress)
    if audio is None:
        return None
    progress("Measuring loudness", 0.9)
    sr = 8000
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(audio),
                           "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                          capture_output=True, timeout=1800)
    audio.unlink(missing_ok=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    secs = len(pcm) // sr
    if secs < 5:
        log.warning("audio decode failed: %s", proc.stderr[-300:])
        return None
    rms = np.sqrt((pcm[: secs * sr].reshape(secs, sr) ** 2).mean(axis=1))
    out = np.zeros(duration, dtype=np.float32)
    n = min(secs, duration)
    out[:n] = rms[:n]
    return _normalize(out)


def _download_audio(info_json: Path, job_dir: Path, progress: Progress) -> Path | None:
    base = job_dir / "audio"
    cmd = [*YTDLP, *cookie_args(), "--load-info-json", str(info_json), "-f", "wa/ba/w", "--no-warnings",
           "--newline", "--progress", "-o", f"{base}.%(ext)s"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        m = re.search(r"\[download\]\s+([\d.]+)%(?:.*?\bat\s+(\S+))?", line)
        if m:
            pct = float(m.group(1))
            speed = f" @ {m.group(2)}" if m.group(2) else ""
            progress(f"Downloading audio ({pct:.0f}%{speed})", 0.85 * pct / 100)
    proc.wait()
    if proc.returncode != 0:
        log.warning("audio download failed (exit %s)", proc.returncode)
        return None
    return next(iter(job_dir.glob("audio.*")), None)


MAX_AUDIO_ANALYSIS_SECONDS = 3 * 3600


def _discount_edges(sig: np.ndarray, head: int = 45, tail: int = 15) -> np.ndarray:
    """Every replay graph spikes at 0:00 (that's where people start) and chat spikes at the end
    (goodbyes); neither is a highlight. Ramp those edges down."""
    n = len(sig)
    out = sig.astype(np.float32).copy()
    h = min(head, n // 4)
    if h > 0:
        out[:h] *= np.linspace(0.05, 1.0, h, dtype=np.float32)
    t = min(tail, n // 4)
    if t > 0:
        out[n - t:] *= np.linspace(1.0, 0.2, t, dtype=np.float32)
    return _normalize(out)


def build_signal(url: str, info: dict, job_dir: Path, progress: Progress) -> tuple[np.ndarray, str]:
    """Return (per-second signal, source name)."""
    from . import chat_signal  # noqa: PLC0415

    duration = int(info.get("duration") or 0)
    if not duration:
        raise ValueError("Could not determine video duration (is this a live stream?)")

    sig = heatmap_from_info(info)
    if sig is not None and sig.max() > 0:
        return _discount_edges(sig), "youtube_heatmap"

    is_twitch = info.get("extractor_key", "").lower().startswith("twitch")
    if is_twitch or info.get("was_live"):
        progress("Sampling chat activity", 0.0)
        if is_twitch:
            sig = chat_signal.twitch_signal(str(info.get("id")), duration, progress)
        else:
            sig = chat_signal.youtube_signal(str(info.get("id")), duration, progress, COOKIES)
        if sig is not None:
            return _discount_edges(sig), "chat_density"
        progress("No chat replay found - falling back to audio", 0.0)

    if duration > MAX_AUDIO_ANALYSIS_SECONDS:
        raise ValueError("This video has no replay heatmap or chat replay, and it is too long "
                         "for audio analysis (limit 3 hours)")
    sig = heatmap_from_audio(info, job_dir, progress)
    if sig is not None:
        return sig, "audio_energy"
    raise ValueError("Could not build an interest signal for this video")


# --------------------------------------------------------------------------- streams

def _headers_str(fmt: dict) -> str:
    h = fmt.get("http_headers") or {}
    return "".join(f"{k}: {v}\r\n" for k, v in h.items())


def pick_formats(info: dict, max_height: int, audio_only: bool = False) -> list[dict]:
    """Choose formats for ffmpeg: one combined stream or [video, audio].
    Highest resolution up to max_height, SDR over HDR (HDR needs tone-mapping to look right in an
    SDR short), h264 > vp9 > av1 at equal size (decode speed), best audio bitrate."""
    fmts = [f for f in info.get("formats", []) if f.get("url") and not str(f.get("format_id", "")).startswith("sb")
            and "drc" not in str(f.get("format_id", ""))]  # skip YouTube's dynamic-range-compressed audio variants

    def has_v(f):
        return f.get("vcodec") not in (None, "none")

    def has_a(f):
        return f.get("acodec") not in (None, "none")

    audio = [f for f in fmts if has_a(f) and not has_v(f)]
    audio.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0, f.get("ext") == "m4a"))
    if audio_only:
        if audio:
            return [audio[0]]  # smallest: plenty for loudness / speech
        combined = [f for f in fmts if has_a(f) and has_v(f)]
        combined.sort(key=lambda f: (f.get("height") or 0))
        return [combined[0]] if combined else []

    def vkey(f):
        vc = str(f.get("vcodec", ""))
        codec_pref = 2 if vc.startswith("avc1") else 1 if vc.startswith("vp") else 0
        sdr = (f.get("dynamic_range") or "SDR").upper() == "SDR"
        return (sdr, f.get("height") or 0, codec_pref, f.get("fps") or 0, f.get("tbr") or 0)

    video_only = [f for f in fmts if has_v(f) and not has_a(f) and (f.get("height") or 0) <= max_height]
    if video_only and audio:
        return [max(video_only, key=vkey), audio[-1]]
    combined = [f for f in fmts if has_v(f) and has_a(f) and (f.get("height") or 0) <= max_height]
    if not combined:
        combined = [f for f in fmts if has_v(f) and has_a(f)]
    return [max(combined, key=vkey)] if combined else []


def pick_streams(info: dict, max_height: int, audio_only: bool = False) -> list[tuple[str, str]]:
    return [(f["url"], _headers_str(f)) for f in pick_formats(info, max_height, audio_only)]
