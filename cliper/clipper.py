"""Cut, reframe, edit and caption a clip with ffmpeg, streaming just the needed section."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import CAPTIONS_SUPPORTED, FFMPEG, MAX_HEIGHT, YTDLP
from .sources import cookie_args, pick_formats, pick_streams

log = logging.getLogger("cliper.clipper")
OUT_W, OUT_H = 1080, 1920


def _expr_track(track: list[tuple[float, float]]) -> str:
    """Piecewise-linear x(t) for the crop filter from [(t, cx_fraction)] keyframes ('ow' is the crop width)."""
    if not track:
        return "(iw-ow)/2"
    pts = [(t, f"({cx:.4f}*iw-ow/2)") for t, cx in track]
    expr = pts[-1][1]
    for (t0, x0), (t1, x1) in reversed(list(zip(pts, pts[1:]))):
        if t1 <= t0:
            continue
        seg = f"({x0}+({x1}-{x0})*(t-{t0:.2f})/{t1 - t0:.2f})"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    if len(pts) > 1:
        expr = f"if(lt(t,{pts[0][0]:.2f}),{pts[0][1]},{expr})"
    return f"clip({expr},0,iw-ow)"


def layout_filter(plan: dict) -> str:
    """Filter chain that turns the source frame into OUT_WxOUT_H (or landscape for 'original')."""
    kind = plan.get("type", "crop_center")
    cw = "min(iw\\,ih*9/16)"
    if kind == "crop":
        x = _expr_track(plan.get("track", []))
        return f"crop=w={cw}:h=ih:x='{x}':y=0,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"
    if kind == "crop_center":
        return f"crop=w={cw}:h=ih:x=(iw-{cw})/2:y=0,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"
    if kind == "split":  # two 1080x960 panels (9:8), each around a person
        a, b = plan["cx"]
        pw = "min(iw\\,ih*9/8)"
        ph = f"({pw}*8/9)"
        return (f"split[pa][pb];[pa]crop=w={pw}:h={ph}:x='clip({a:.4f}*iw-ow/2,0,iw-ow)':y=(ih-oh)/2,"
                f"scale={OUT_W}:960:flags=lanczos[ta];"
                f"[pb]crop=w={pw}:h={ph}:x='clip({b:.4f}*iw-ow/2,0,iw-ow)':y=(ih-oh)/2,"
                f"scale={OUT_W}:960:flags=lanczos[tb];[ta][tb]vstack,setsar=1")
    if kind == "stack_game":  # facecam on top (1080x720), game below cropped to 4:3 (1080x810) over blur (1080x1200)
        x, y, w, h = plan["cam"]
        return (f"split=3[pf][pg][pb];"
                f"[pf]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},scale={OUT_W}:720:flags=lanczos[tf];"
                f"[pb]scale={OUT_W}:1200:force_original_aspect_ratio=increase,crop={OUT_W}:1200,gblur=sigma=30,eq=brightness=-0.2[bg];"
                f"[pg]crop=w=min(iw\\,ih*4/3):h=ih:x=(iw-ow)/2:y=0,scale={OUT_W}:810:flags=lanczos[tg];"
                f"[bg][tg]overlay=0:(H-h)/2[gb];[tf][gb]vstack,setsar=1")
    if kind == "blur":
        return (f"split[a][b];[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},gblur=sigma=40,eq=brightness=-0.15[bg];"
                f"[b]scale={OUT_W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    return "scale=-2:'min(1080,ih)':flags=lanczos,setsar=1"  # original


def _punch_expr(segments: list[tuple[float, float]], fps: int) -> str:
    """zoompan z(t): alternate 1.0 / 1.08 on phrase boundaries (the classic Shorts punch-in)."""
    terms = [f"between(in/{fps},{a:.2f},{b:.2f})" for a, b in segments]
    return "1+0.06*(" + "+".join(terms) + ")" if terms else "1"


def build_video_filter(plan: dict, style: dict, ass_name: str | None, punch_segments: list[tuple[float, float]],
                       fps: int = 30) -> str:
    speed = float(style.get("speed", 1.0) or 1.0)
    vertical = plan.get("type") != "original"
    parts = []
    if speed != 1.0:
        parts.append(f"setpts=PTS/{speed:.4f}")
    parts.append(f"fps={fps}")
    parts.append(layout_filter(plan))
    if style.get("grade"):
        parts.append("eq=contrast=1.06:saturation=1.18,unsharp=5:5:0.4")
    if style.get("punch") and punch_segments and vertical:
        z = _punch_expr(punch_segments, fps)
        parts.append(f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={OUT_W}x{OUT_H}:fps={fps}")
    if style.get("mirror"):
        parts.append("hflip")
    if ass_name and CAPTIONS_SUPPORTED:
        parts.append(f"ass={ass_name}")
    return ",".join(parts)


def build_audio_filter(style: dict) -> str:
    speed = float(style.get("speed", 1.0) or 1.0)
    pitch = float(style.get("pitch", 1.0) or 1.0)
    parts = ["aresample=48000"]  # sources are 44.1k or 48k; asetrate below assumes a known rate
    if pitch != 1.0:  # re-labelling the rate shifts pitch and tempo; atempo undoes the tempo part
        parts.append(f"asetrate={int(round(48000 * pitch))},aresample=48000,atempo={1 / pitch:.4f}")
    if speed != 1.0:
        parts.append(f"atempo={speed:.4f}")
    parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")  # the Shorts / Reels loudness target
    return ",".join(parts)


def _ffmpeg_inputs(streams: list[tuple[str, str]], start: float, length: float | None = None) -> list[str]:
    args: list[str] = []
    for url, headers in streams:
        if headers:
            args += ["-headers", headers]
        args += ["-ss", f"{start:.3f}"]
        if length is not None:
            # input-side read limit counts from the keyframe before `start`, so over-read a little;
            # the exact cut is the output -t (in output time, after any speed change)
            args += ["-t", f"{length + 4:.3f}"]
        args += ["-i", url]
    return args


def _headers(fmt: dict) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in (fmt.get("http_headers") or {}).items())


def _encode_args(crf: int) -> list[str]:
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart"]


def render_clip(info: dict, start: float, end: float, out: Path, plan: dict, style: dict,
                ass_path: Path | None, punch_segments: list[tuple[float, float]], crf: int = 18) -> None:
    fmts = pick_formats(info, int(style.get("max_height") or MAX_HEIGHT))
    if not fmts:
        raise RuntimeError("No usable media streams found")
    streams = [(f["url"], _headers(f)) for f in fmts]
    fps = 60 if (fmts[0].get("fps") or 30) >= 50 else 30  # keep 60 fps sources smooth
    log.info("source: %sp %s %s fps, audio %s", fmts[0].get("height"), str(fmts[0].get("vcodec", ""))[:4],
             fmts[0].get("fps"), fmts[-1].get("format_id"))
    vf = build_video_filter(plan, style, ass_path.name if ass_path else None, punch_segments, fps=fps)
    af = build_audio_filter(style)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    cmd += _ffmpeg_inputs(streams, start, end - start)
    a_in = "1:a:0" if len(streams) == 2 else "0:a:0"
    speed = float(style.get("speed", 1.0) or 1.0)
    cmd += ["-filter_complex", f"[0:v]{vf}[v];[{a_in}]{af}[a]", "-map", "[v]", "-map", "[a]",
            "-t", f"{(end - start) / speed:.3f}", *_encode_args(crf), str(out.resolve())]
    cwd = ass_path.resolve().parent if ass_path else None
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900, cwd=cwd)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"direct stream failed: {e.stderr.strip()[-400:]}") from e


def render_clip_via_ytdlp(info_json: Path, start: float, end: float, out: Path, plan: dict, style: dict,
                          ass_path: Path | None, punch_segments: list[tuple[float, float]], workdir: Path,
                          crf: int = 18) -> None:
    """Fallback: let yt-dlp download the section (exact cut), then edit locally."""
    raw_base = workdir / f"raw_{int(start)}"
    mh = int(style.get("max_height") or MAX_HEIGHT)
    fmt = f"bv*[height<={mh}][ext=mp4]+ba[ext=m4a]/bv*[height<={mh}]+ba/b[height<={mh}]/b"
    cmd = [*YTDLP, *cookie_args(), "--load-info-json", str(info_json), "--download-sections", f"*{start}-{end}",
           "--force-keyframes-at-cuts", "-f", fmt, "--merge-output-format", "mp4",
           "-o", f"{raw_base}.%(ext)s", "-q", "--no-warnings"]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1800)
    raw = next(iter(workdir.glob(raw_base.name + ".*")), None)
    if raw is None:
        raise RuntimeError("yt-dlp produced no file")
    vf = build_video_filter(plan, style, ass_path.name if ass_path else None, punch_segments)
    af = build_audio_filter(style)
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(raw.resolve()),
        "-filter_complex", f"[0:v]{vf}[v];[0:a:0]{af}[a]", "-map", "[v]", "-map", "[a]",
        *_encode_args(crf), str(out.resolve()),
    ], check=True, capture_output=True, text=True, timeout=900, cwd=ass_path.resolve().parent if ass_path else None)
    raw.unlink(missing_ok=True)


def extract_audio(info: dict, start: float, end: float, out: Path) -> None:
    """Small wav of a window, for Whisper."""
    streams = pick_streams(info, MAX_HEIGHT, audio_only=True)
    if not streams:
        raise RuntimeError("No audio stream")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    cmd += _ffmpeg_inputs(streams, start)
    cmd += ["-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
