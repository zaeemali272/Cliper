"""Chat-activity signal for stream VODs (Twitch, and YouTube streams that have no "most replayed"
graph). Uses the same public endpoints the web players use - no auth, no API keys.

Reading every message of a long stream takes many minutes, so we *sample* the timeline instead:
ask for the page of chat at offset T and measure how many seconds those messages span. That is
messages/second at T directly, and hundreds of samples run in parallel in well under a minute.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import numpy as np
import requests

log = logging.getLogger("cliper.chat")
Progress = Callable[[str, float], None]
Sampler = Callable[[int], list[float]]  # offset seconds -> message timestamps (seconds)


def _rate_signal(sampler: Sampler, duration: int, progress: Progress, label: str,
                 max_samples: int = 720, workers: int = 8) -> np.ndarray | None:
    step = max(15, int(np.ceil(duration / max_samples)))
    offsets = list(range(0, duration, step))
    rates = np.full(len(offsets), np.nan, dtype=np.float32)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_safe, sampler, off): i for i, off in enumerate(offsets)}
        for f in as_completed(futs):
            i, ts = futs[f], f.result()
            ts = [t for t in ts if t >= offsets[i] - 1]  # drop pinned/engagement items stamped at 0
            if len(ts) >= 2:
                rates[i] = (len(ts) - 1) / max(max(ts) - min(ts), 1.0)
            elif ts:
                rates[i] = 0.2
            done += 1
            if done % 20 == 0:
                progress(f"{label} ({done}/{len(offsets)} samples)", done / len(offsets))
    good = ~np.isnan(rates)
    log.info("%s: %d/%d samples ok, step %ds", label, int(good.sum()), len(offsets), step)
    if good.mean() < 0.5:
        return None
    idx = np.arange(len(rates))
    rates = np.interp(idx, idx[good], rates[good])
    per_sec = np.repeat(np.log1p(rates), step)[:duration]  # log: hype spikes don't erase the rest
    if len(per_sec) < duration:
        per_sec = np.pad(per_sec, (0, duration - len(per_sec)), mode="edge")
    lo, hi = float(per_sec.min()), float(per_sec.max())
    return (per_sec - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros(duration, dtype=np.float32)


def _safe(sampler: Sampler, off: int) -> list[float]:
    for attempt in range(4):
        try:
            return sampler(off)
        except Exception as e:  # noqa: BLE001
            log.debug("sample %s failed (%s): %s", off, attempt, e)
            time.sleep(0.5 * (attempt + 1))
    return []


# ------------------------------------------------------------------------------------ Twitch

_TW_GQL = "https://gql.twitch.tv/gql"
_TW_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's own web client id
_TW_QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def twitch_signal(video_id: str, duration: int, progress: Progress) -> np.ndarray | None:
    video_id = str(video_id).lstrip("v")
    session = requests.Session()

    def sampler(offset: int) -> list[float]:
        body = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": video_id, "contentOffsetSeconds": offset},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _TW_QUERY_HASH}},
        }]
        r = session.post(_TW_GQL, json=body, headers={"Client-ID": _TW_CLIENT_ID}, timeout=20)
        video = (r.json()[0].get("data") or {}).get("video") or {}
        edges = ((video.get("comments") or {}).get("edges")) or []
        return [float(e["node"]["contentOffsetSeconds"]) for e in edges]

    return _rate_signal(sampler, duration, progress, "Sampling Twitch chat")


# ------------------------------------------------------------------------------------ YouTube

def youtube_signal(video_id: str, duration: int, progress: Progress, cookies: str | None = None) -> np.ndarray | None:
    """Live-chat replay for a YouTube stream VOD. yt-dlp does the fragile part (watch-page parsing);
    we then hit the same innertube endpoint the player uses, seeking with playerOffsetMs."""
    import yt_dlp  # noqa: PLC0415
    from yt_dlp.extractor.youtube import YoutubeIE  # noqa: PLC0415
    from yt_dlp.utils import traverse_obj  # noqa: PLC0415

    from .config import BROWSER  # noqa: PLC0415

    opts = {"quiet": True, "no_warnings": True}
    if cookies:
        opts["cookiefile"] = cookies
    elif BROWSER:
        opts["cookiesfrombrowser"] = (BROWSER,)
    ie = YoutubeIE(yt_dlp.YoutubeDL(opts))
    page = ie._download_webpage(f"https://www.youtube.com/watch?v={video_id}", video_id)
    ytcfg = ie.extract_ytcfg(video_id, page) or {}
    data = ie.extract_yt_initial_data(video_id, page)
    cont = traverse_obj(data, ("contents", "twoColumnWatchNextResults", "conversationBar",
                               "liveChatRenderer", "continuations", 0, "reloadContinuationData", "continuation"))
    ctx, key = ytcfg.get("INNERTUBE_CONTEXT"), ytcfg.get("INNERTUBE_API_KEY")
    if not (cont and ctx and key):
        log.info("no live chat replay available for %s", video_id)
        return None
    client = ctx.get("client", {})
    headers = {"x-youtube-client-name": str(client.get("clientName", "1")),
               "x-youtube-client-version": str(client.get("clientVersion", "")),
               "origin": "https://www.youtube.com"}
    url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat_replay?key={key}&prettyPrint=false"
    session = requests.Session()

    def sampler(offset: int) -> list[float]:
        body = {"context": ctx, "continuation": cont, "currentPlayerState": {"playerOffsetMs": str(offset * 1000)}}
        r = session.post(url, json=body, headers=headers, timeout=20).json()
        acts = traverse_obj(r, ("continuationContents", "liveChatContinuation", "actions")) or []
        return [int(a["replayChatItemAction"]["videoOffsetTimeMsec"]) / 1000
                for a in acts if "replayChatItemAction" in a]

    return _rate_signal(sampler, duration, progress, "Sampling YouTube chat")
