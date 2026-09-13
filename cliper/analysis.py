"""Turn a per-second interest signal into highlights and clip windows."""
from __future__ import annotations

import numpy as np


def smooth(sig: np.ndarray, window: int = 5) -> np.ndarray:
    if window <= 1 or len(sig) < window:
        return sig
    k = np.ones(window, dtype=np.float32) / window
    return np.convolve(sig, k, mode="same")


def downsample(sig: np.ndarray, points: int = 200) -> list[float]:
    """Compact version of the signal for the UI chart."""
    n = len(sig)
    if n <= points:
        return [round(float(v), 4) for v in sig]
    edges = np.linspace(0, n, points + 1).astype(int)
    return [round(float(sig[a:b].max()) if b > a else 0.0, 4) for a, b in zip(edges[:-1], edges[1:])]


def find_peaks(sig: np.ndarray, min_distance: int) -> list[int]:
    """Local maxima, strongest first, at least min_distance seconds apart."""
    n = len(sig)
    if n == 0:
        return []
    order = np.argsort(-sig, kind="stable")
    taken: list[int] = []
    for i in order:
        i = int(i)
        if sig[i] <= 0:
            break
        if all(abs(i - t) >= min_distance for t in taken):
            taken.append(i)
    return taken


def _window_around(sig: np.ndarray, peak: int, min_len: int, max_len: int) -> tuple[int, int]:
    """Grow outward from the peak while the signal stays 'hot', then pad to min_len."""
    n = len(sig)
    thresh = sig[peak] * 0.55
    a = b = peak
    while b - a < max_len:
        left = sig[a - 1] if a > 0 else -1
        right = sig[b + 1] if b + 1 < n else -1
        if left < thresh and right < thresh:
            break
        if left >= right:
            a -= 1
        else:
            b += 1
    while b - a < min_len:
        grew = False
        if a > 0:
            a -= 1
            grew = True
        if b - a < min_len and b < n:
            b += 1
            grew = True
        if not grew:
            break
    # bias slightly earlier so the build-up to the moment is included
    lead = min(a, 2)
    a -= lead
    b = min(n, b - lead) if b - a > max_len else b
    return a, min(b, n)


def _snap(t: float, boundaries: list[float], tolerance: float) -> float:
    if not boundaries:
        return t
    best = min(boundaries, key=lambda x: abs(x - t))
    return best if abs(best - t) <= tolerance else t


def pick_clips(
    sig: np.ndarray,
    count: int,
    min_len: int,
    max_len: int,
    sentence_starts: list[float] | None = None,
    sentence_ends: list[float] | None = None,
) -> list[dict]:
    n = len(sig)
    sm = smooth(sig, 5)
    min_len = max(5, min(min_len, n))
    max_len = max(min_len, min(max_len, n))

    candidates = []
    for p in find_peaks(sm, min_distance=max(10, min_len // 2)):
        a, b = _window_around(sm, p, min_len, max_len)
        candidates.append((float(sm[p]), p, a, b))

    chosen: list[dict] = []
    covered = np.zeros(n, dtype=bool)
    for score, p, a, b in candidates:
        if len(chosen) >= count:
            break
        if covered[max(0, a - 5):min(n, b + 5)].mean() > 0.15:  # no overlap, and a small gap between clips
            continue
        covered[a:b] = True
        chosen.append({"start": a, "end": b, "peak": p, "score": score})

    # Not enough distinct peaks: fill from the best uncovered stretches.
    if len(chosen) < count:
        win = min_len
        cs = np.concatenate([[0.0], np.cumsum(sm)])
        means = (cs[win:] - cs[:-win]) / win if n >= win else np.array([])
        for i in np.argsort(-means):
            if len(chosen) >= count:
                break
            a, b = int(i), int(i) + win
            if covered[max(0, a - 5):min(n, b + 5)].any():
                continue
            covered[a:b] = True
            chosen.append({"start": a, "end": b, "peak": a + int(np.argmax(sm[a:b])), "score": float(means[i])})

    # Snap edges to sentence boundaries when captions exist.
    for c in chosen:
        s = _snap(float(c["start"]), sentence_starts or [], 2.5)
        e = _snap(float(c["end"]), sentence_ends or [], 2.5)
        if min_len <= e - s <= max_len + 3:
            c["start"], c["end"] = s, e
        c["start"], c["end"] = round(float(c["start"]), 2), round(float(min(c["end"], n)), 2)
        c["score"] = round(float(c["score"]), 4)

    chosen.sort(key=lambda c: -c["score"])
    for i, c in enumerate(chosen, 1):
        c["rank"] = i
    return chosen


def highlights(sig: np.ndarray, limit: int = 10) -> list[dict]:
    sm = smooth(sig, 5)
    return [{"time": int(p), "score": round(float(sm[p]), 4)} for p in find_peaks(sm, 60)[:limit]]


# ----------------------------------------------------------------------------- transcript-aware

HOOK_WORDS = ("wait", "what", "why", "how", "no way", "bro", "oh my", "look", "listen", "watch", "insane",
              "crazy", "never", "actually", "secret", "stop", "dude", "yo", "nah", "really", "seriously",
              "unbelievable", "holy", "let's go", "lets go", "oh no", "chat", "guys", "everyone", "nobody",
              "worst", "best", "first time", "finally", "literally", "imagine", "trust me", "i swear")

Word = tuple[float, float, str]


def sentences_from_words(words: list[Word], gap: float = 0.5, max_len: float = 7.0) -> list[dict]:
    """Group words into phrases: break on punctuation or pauses; anything still longer than
    max_len is split at its biggest internal gap (auto-captions have no punctuation at all)."""
    out: list[dict] = []
    cur: list[Word] = []

    def emit(chunk: list[Word]) -> None:
        if not chunk:
            return
        if chunk[-1][1] - chunk[0][0] > max_len and len(chunk) > 3:
            gaps = [chunk[k + 1][0] - chunk[k][1] for k in range(len(chunk) - 1)]
            # prefer a real pause; otherwise just cut in the middle
            k = int(np.argmax(gaps)) if max(gaps) > 0.15 else len(chunk) // 2 - 1
            k = min(max(k, 0), len(chunk) - 2)
            emit(chunk[:k + 1])
            emit(chunk[k + 1:])
            return
        out.append({"start": chunk[0][0], "end": chunk[-1][1], "text": " ".join(w[2] for w in chunk),
                    "complete": chunk[-1][2].rstrip()[-1:] in ".?!"})

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = (nxt[0] - w[1]) if nxt else 99
        if w[2].rstrip()[-1:] in ".?!" or pause > gap or nxt is None:
            emit(cur)
            cur = []
    return out


def hook_score(text: str) -> float:
    t = text.lower()
    s = 0.0
    s += 0.5 * sum(1 for w in HOOK_WORDS if w in t)
    s += 0.6 * ("?" in t) + 0.4 * ("!" in t)
    return min(s, 2.0)


def pick_clips_transcript(sig: np.ndarray, words: list[Word], count: int, min_len: int, max_len: int) -> list[dict]:
    """Clips that start and end on phrase boundaries, ranked by interest + hook value. Regions
    without speech still compete via plain signal windows."""
    n = len(sig)
    sm = smooth(sig, 5)
    cs = np.concatenate([[0.0], np.cumsum(sm)])
    sents = [s for s in sentences_from_words(words) if s["end"] <= n]

    def seg_score(a: float, b: float) -> tuple[float, float]:
        ia, ib = int(a), max(int(a) + 1, int(np.ceil(b)))
        ib = min(ib, n)
        mean = float((cs[ib] - cs[ia]) / max(ib - ia, 1))
        peak = float(sm[ia:ib].max()) if ib > ia else 0.0
        return mean, peak

    cands: list[dict] = []
    for i in range(len(sents)):
        for j in range(i, min(i + 12, len(sents))):
            a, b = sents[i]["start"] - 0.25, sents[j]["end"] + 0.35
            dur = b - a
            if dur > max_len:
                break
            if dur < min_len and not (j + 1 == len(sents) or sents[j + 1]["end"] + 0.35 - a > max_len):
                continue  # keep growing unless the next phrase would overflow
            if dur < min_len * 0.6:
                continue
            mean, peak = seg_score(a, b)
            score = 0.55 * mean + 0.45 * peak + 0.06 * hook_score(sents[i]["text"]) \
                + 0.03 * sents[j]["complete"] + 0.02 * min(dur / max_len, 1.0)
            cands.append({"start": max(0.0, a), "end": min(float(n), b), "score": score,
                          "text": " ".join(s["text"] for s in sents[i:j + 1]),
                          "hook": max((hook_score(s["text"]), s["text"]) for s in sents[i:j + 1])[1],
                          "phrases": [(s["start"], s["end"]) for s in sents[i:j + 1]]})

    # speech-less fallbacks (loud game moments, music) - slightly penalised so worded clips win ties
    for c in pick_clips(sig, count * 2, min_len, max_len):
        cands.append({"start": float(c["start"]), "end": float(c["end"]), "score": c["score"] * 0.85,
                      "text": "", "hook": "", "phrases": []})

    cands.sort(key=lambda c: -c["score"])
    chosen: list[dict] = []
    covered = np.zeros(n + 1, dtype=bool)
    for c in cands:
        if len(chosen) >= count:
            break
        a, b = int(c["start"]), int(np.ceil(c["end"]))
        if covered[max(0, a - 4): min(n, b + 4)].any():
            continue
        covered[a:b] = True
        c["peak"] = a + int(np.argmax(sm[a:max(a + 1, b)]))
        chosen.append(c)

    for i, c in enumerate(chosen, 1):
        c["rank"] = i
        c["start"], c["end"], c["score"] = round(c["start"], 2), round(c["end"], 2), round(float(c["score"]), 4)
    return chosen
