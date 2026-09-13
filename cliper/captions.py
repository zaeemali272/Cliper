"""Word-level captions: from YouTube's free auto-subs (json3) or an optional local Whisper model.
Rendered as chunked, centered ASS subtitles in the usual Shorts/Reels style."""
from __future__ import annotations

import json
from pathlib import Path

Word = tuple[float, float, str]  # start, end, text


def words_from_json3(path: Path) -> list[Word]:
    data = json.loads(path.read_text(encoding="utf-8"))
    words: list[Word] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        t0 = ev["tStartMs"] / 1000.0
        dur = (ev.get("dDurationMs") or 0) / 1000.0
        raw = [(t0 + (s.get("tOffsetMs") or 0) / 1000.0, (s.get("utf8") or "")) for s in segs]
        # drop sound tags like [Music] / [Applause]
        raw = [(t, w.strip().replace(">>", "").strip()) for t, w in raw]
        raw = [(t, w) for t, w in raw if w and not w.startswith("[")]
        for i, (t, w) in enumerate(raw):
            end = raw[i + 1][0] if i + 1 < len(raw) else t0 + dur
            # auto-subs run each word until the next one starts, hiding pauses; a spoken word is
            # rarely longer than ~0.7 s, so cap it and let the silence show as a gap
            end = min(end, t + 0.7)
            words.append((t, max(end, t + 0.05), w))
    # auto-subs may repeat words across overlapping events; keep monotonic
    out: list[Word] = []
    for w in sorted(words):
        if out and w[0] < out[-1][0]:
            continue
        out.append(w)
    return out


def sentence_boundaries(words: list[Word], gap: float = 0.7) -> tuple[list[float], list[float]]:
    """Guess phrase starts/ends from pauses between words."""
    starts, ends = [], []
    for i, (s, e, _) in enumerate(words):
        if i == 0 or s - words[i - 1][1] > gap:
            starts.append(s)
        if i == len(words) - 1 or words[i + 1][0] - e > gap:
            ends.append(e)
    return starts, ends


def transcribe_with_whisper(media: Path, model_size: str = "base") -> list[Word]:
    """Optional: `uv sync --extra whisper`. Runs fully locally on CPU."""
    from faster_whisper import WhisperModel  # type: ignore

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(media), word_timestamps=True, vad_filter=True)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            txt = w.word.strip()
            if txt:
                words.append((float(w.start), float(w.end), txt))
    return words


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


ACCENT = "&H006D4DFF&"      # #ff4d6d in ASS BGR
HIGHLIGHT = "&H0000D8FF&"   # #ffd800


def build_ass(words: list[Word], clip_start: float, clip_end: float, vertical: bool,
              speed: float = 1.0, title: str | None = None, progress: bool = True,
              max_words: int = 3, pause: float = 0.8, captions: bool = True) -> str:
    """Captions (3 words at a time, current word highlighted), an optional hook title card for
    the first seconds, and a thin progress bar. All times are in output time (source / speed)."""
    w, h = (1080, 1920) if vertical else (1920, 1080)
    size = 78 if vertical else 56
    margin_v = 560 if vertical else 90
    length = (clip_end - clip_start) / speed
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,5,2,2,60,60,{margin_v},1
Style: Title,Arial,{int(size * 0.85)},&H0000D8FF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,80,80,{300 if vertical else 60},1
Style: Bar,Arial,20,{ACCENT},{ACCENT},{ACCENT},&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    T = lambda t: _ass_time((t - clip_start) / speed)  # noqa: E731

    if captions:
        inside = [(s, e, t) for s, e, t in words if e > clip_start and s < clip_end]
        chunks: list[list[Word]] = []
        cur: list[Word] = []
        for wd in inside:
            if cur and (len(cur) >= max_words or wd[0] - cur[-1][1] > pause or cur[-1][2].rstrip()[-1:] in ".?!"):
                chunks.append(cur)
                cur = []
            cur.append(wd)
        if cur:
            chunks.append(cur)
        for ci, chunk in enumerate(chunks):
            chunk_end = chunk[-1][1]
            if ci + 1 < len(chunks) and chunks[ci + 1][0][0] - chunk_end <= 2.0:
                chunk_end = chunks[ci + 1][0][0]  # hold until the next chunk: no flicker
            chunk_end = min(chunk_end, clip_end)
            for k, (s, e, t) in enumerate(chunk):
                end = chunk[k + 1][0] if k + 1 < len(chunk) else chunk_end
                if end <= max(s, clip_start):
                    continue
                parts = []
                for m, (_, _, txt) in enumerate(chunk):
                    txt = _clean(txt).upper()
                    parts.append(f"{{\\c{HIGHLIGHT}}}{txt}{{\\c&H00FFFFFF&}}" if m == k else txt)
                lines.append(f"Dialogue: 1,{T(max(s, clip_start))},{T(end)},Cap,,0,0,0,,{' '.join(parts)}")

    if title:
        t_end = clip_start + min(2.8 * speed, clip_end - clip_start)
        lines.append(f"Dialogue: 2,{T(clip_start)},{T(t_end)},Title,,0,0,0,,{{\\fad(120,200)}}{short_title(title)}")

    if progress:
        step = 0.1
        y = h - 12
        n = int(length / step) + 1
        for i in range(n):
            t0, t1 = i * step, min((i + 1) * step, length)
            if t1 <= t0:
                break
            bw = int(w * (t1 / length))
            draw = f"{{\\an7\\pos(0,{y})\\p1\\bord0\\shad0\\c{ACCENT}}}m 0 0 l {bw} 0 l {bw} 12 l 0 12{{\\p0}}"
            lines.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Bar,,0,0,0,,{draw}")

    return header + "\n".join(lines) + "\n"


def short_title(t: str, max_words: int = 6) -> str:
    """A hook is a few words, not a paragraph: cut at the first sentence end, then at max_words."""
    t = _clean(t)
    for ch in ".?!":
        if ch in t[3:]:
            t = t[: t.index(ch, 3) + 1]
            break
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]) + "…"
    return t.upper()


def _clean(t: str) -> str:
    return t.replace("{", "").replace("}", "").replace("\\", "").strip()
