"""Estimate how likely a clip is to be flagged by automated copyright matching.

Content ID and its equivalents are proprietary, so this is an *estimate* built from the things
that are known to drive matches:
  1. recognisable commercial music (checked with Shazam - music claims are near-certain)
  2. how similar the clip still is to its source, visually (perceptual hashes, robust to crops and
     mirroring) and audibly (spectrogram correlation, robust to speed/pitch tweaks)
  3. how long a stretch of the source is reused
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import numpy as np

from .config import FFMPEG, MAX_HEIGHT

log = logging.getLogger("cliper.copyright")


# ------------------------------------------------------------------------------ music

def _wav(src: Path | str, out: Path, start: float, length: float, headers: str | None = None, sr: int = 44100) -> bool:
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), str(out)]
    return subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0 and out.exists()


def music_check(path: Path, duration: float, workdir: Path) -> list[dict]:
    """Shazam a few windows of the file; return unique recognised tracks."""
    try:
        from shazamio import Shazam  # noqa: PLC0415
    except ImportError:
        return []
    win = min(10.0, max(duration, 1.0))
    starts = sorted({0.0, max(0.0, duration / 2 - win / 2), max(0.0, duration - win)})
    found: dict[str, dict] = {}

    async def run() -> None:
        sh = Shazam()
        for i, st in enumerate(starts):
            wav = workdir / f"shz_{i}.wav"
            if not _wav(path, wav, st, win):
                continue
            try:
                r = await sh.recognize(str(wav))
            except Exception as e:  # noqa: BLE001
                log.warning("shazam failed: %s", e)
                continue
            finally:
                wav.unlink(missing_ok=True)
            t = r.get("track") or {}
            if t.get("title"):
                key = t.get("key") or t["title"]
                found.setdefault(key, {"title": t.get("title"), "artist": t.get("subtitle"), "at": st,
                                       "url": t.get("url")})

    asyncio.run(run())
    return list(found.values())


# ------------------------------------------------------------------------------ visual

def _frames(src: str, start: float, length: float, fps: float, headers: str | None, w: int = 320, h: int = 180) -> np.ndarray:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src, "-an",
            "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=600).stdout
    n = len(raw) // (w * h)
    return np.frombuffer(raw[: n * w * h], np.uint8).reshape(n, h, w)


def _phash(img: np.ndarray) -> np.ndarray:
    import cv2  # noqa: PLC0415

    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(small)[:8, :8].flatten()
    return d > np.median(d[1:])


def _candidates(frame: np.ndarray) -> list[np.ndarray]:
    """Regions a 9:16 clip could have been cut from: full frame, centre/left/right vertical crops."""
    h, w = frame.shape
    cw = int(h * 9 / 16)
    regs = [frame, frame[:, (w - cw) // 2:(w - cw) // 2 + cw], frame[:, :cw], frame[:, w - cw:],
            frame[:, int(w * 0.2):int(w * 0.2) + cw], frame[:, int(w * 0.5):int(w * 0.5) + cw],
            frame[: int(h * 0.75), (w - cw) // 2:(w - cw) // 2 + cw]]
    return regs + [r[:, ::-1] for r in regs]  # mirrored too


def visual_similarity(clip: Path, src: str, src_start: float, speed: float, headers: str | None, length: float) -> float:
    """Mean over clip seconds of the best pHash match against candidate crops of the aligned source frame."""
    a = _frames(str(clip), 0, length, 1.0, None)
    b = _frames(src, src_start, length * speed, 1.0 / speed, headers)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    sims = []
    for i in range(n):
        ha = _phash(a[i])
        best = max(1.0 - float(np.count_nonzero(ha != _phash(c))) / ha.size for c in _candidates(b[i]))
        sims.append(best)
    return float(np.mean(sims))


# ------------------------------------------------------------------------------ audio

def _logspec(wav: Path, sr: int = 8000, hop: int = 800, bands: int = 32) -> np.ndarray:
    import wave  # noqa: PLC0415

    with wave.open(str(wav)) as f:
        pcm = np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768
    win = hop * 2
    n = max(0, (len(pcm) - win) // hop)
    if n == 0:
        return np.zeros((0, bands), np.float32)
    frames = np.stack([pcm[i * hop:i * hop + win] * np.hanning(win) for i in range(n)])
    mag = np.abs(np.fft.rfft(frames, axis=1))[:, 1:]
    edges = np.logspace(np.log10(1), np.log10(mag.shape[1]), bands + 1).astype(int)
    spec = np.stack([mag[:, edges[k]:max(edges[k] + 1, edges[k + 1])].mean(axis=1) for k in range(bands)], axis=1)
    spec = np.log1p(spec * 100)
    spec -= spec.mean(axis=0, keepdims=True)
    spec /= spec.std(axis=0, keepdims=True) + 1e-6
    return spec


def audio_similarity(clip: Path, src: str, src_start: float, speed: float, headers: str | None, length: float,
                     workdir: Path) -> float:
    """Normalised correlation of log-spectrograms, source time-scaled to the clip, best over ±1.5 s lag."""
    a_wav, b_wav = workdir / "sim_a.wav", workdir / "sim_b.wav"
    if not _wav(clip, a_wav, 0, length, sr=8000):
        return 0.0
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        cmd += ["-headers", headers]
    pad = 1.5
    cmd += ["-ss", f"{max(0.0, src_start - pad * speed):.2f}", "-t", f"{(length + 2 * pad) * speed:.2f}", "-i", src,
            "-vn", "-ac", "1", "-ar", "8000", "-af", f"atempo={min(2.0, max(0.5, speed)):.4f}", str(b_wav)]
    if subprocess.run(cmd, capture_output=True, timeout=300).returncode != 0:
        return 0.0
    A, B = _logspec(a_wav), _logspec(b_wav)
    a_wav.unlink(missing_ok=True)
    b_wav.unlink(missing_ok=True)
    n = len(A)
    if n < 5 or len(B) < n:
        return 0.0
    best = 0.0
    for lag in range(0, len(B) - n + 1):
        seg = B[lag:lag + n]
        r = float((A * seg).sum() / (np.linalg.norm(A) * np.linalg.norm(seg) + 1e-9))
        best = max(best, r)
    return best


# ------------------------------------------------------------------------------ verdict

def assess(clip: Path, workdir: Path, info: dict | None = None, src_start: float | None = None,
           speed: float = 1.0, mirror: bool = False) -> dict:
    from .sources import pick_streams  # noqa: PLC0415

    dur = _duration(clip)
    report: dict = {"duration": round(dur, 1), "music": [], "visual": None, "audio": None, "reasons": [],
                    "license": (info or {}).get("license"), "source_compared": False}

    report["music"] = music_check(clip, dur, workdir)
    if report["music"]:
        names = ", ".join(f"{m['title']} – {m['artist']}" for m in report["music"])
        report["reasons"].append(f"Recognisable music: {names}. Music claims are near-automatic.")

    if info and src_start is not None:
        streams = pick_streams(info, min(MAX_HEIGHT, 720))
        if streams:
            v_url, v_h = streams[0]
            a_url, a_h = streams[-1]
            try:
                report["visual"] = round(visual_similarity(clip, v_url, src_start, speed, v_h, dur), 3)
                report["audio"] = round(audio_similarity(clip, a_url, src_start, speed, a_h, dur, workdir), 3)
                report["source_compared"] = True
            except Exception as e:  # noqa: BLE001
                log.warning("similarity failed: %s", e)

    score = 0.0
    if report["music"]:
        score = max(score, 0.9)
    if report["visual"] is not None:
        v = report["visual"]
        report["reasons"].append(f"Visual match to the source: {v:.0%} "
                                 f"({'strong' if v >= 0.8 else 'moderate' if v >= 0.65 else 'weak'} — hashes are robust to crops and mirroring).")
        score = max(score, min(1.0, (v - 0.5) / 0.4))
    if report["audio"] is not None:
        a = report["audio"]
        report["reasons"].append(f"Audio match to the source: {a:.0%} "
                                 f"({'strong' if a >= 0.6 else 'moderate' if a >= 0.35 else 'weak'} — after speed/pitch changes).")
        score = max(score, min(1.0, a / 0.75))
    if dur >= 10:
        report["reasons"].append(f"{dur:.0f} s of continuous footage — most matching systems need only a few seconds.")
        score = max(score, 0.35 if report["source_compared"] else 0.5)
    lic = (report.get("license") or "")
    if "creative commons" in lic.lower():
        report["reasons"].append("Source is Creative Commons — reuse with attribution is allowed.")
        score *= 0.5
    if not report["source_compared"] and not report["music"]:
        report["reasons"].append("No source given, so only music was checked.")

    report["score"] = round(float(score), 2)
    report["level"] = "high" if score >= 0.7 else "medium" if score >= 0.4 else "low"
    report["note"] = ("Estimate only. Matching systems are proprietary; edits reduce automated matches but a claim "
                      "is always possible on footage you don't own.")
    return report


def _duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    import re  # noqa: PLC0415

    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
