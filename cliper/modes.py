"""Guess what kind of video this is so framing and editing defaults fit it."""
from __future__ import annotations

import re

MODES = ("talking", "podcast", "gameplay", "irl", "music")

_GAME_WORDS = ("gameplay", "playthrough", "walkthrough", "let's play", "lets play", "speedrun", "ranked",
               "minecraft", "fortnite", "valorant", "gta", "warzone", "apex", "league of legends", "roblox",
               "elden ring", "cs2", "counter-strike", "overwatch", "rocket league", "call of duty", "fifa",
               "ea fc", "nba 2k", "among us", "pubg", "rust", "tarkov", "dota", "hearthstone", "pokemon")
_PODCAST_WORDS = ("podcast", "interview", "episode", "ep.", "ep ", "#ep", "conversation", "talks with",
                  "sits down", "full episode", "the show")
_IRL_WORDS = ("irl", "vlog", "in real life", "street", "travel", "walking", "day in", "trip", "meet",
              "just chatting", "live stream", "livestream", "stream")

_MODE_DEFAULTS = {
    #           punch  progress title grade speed pitch captions
    "talking":  dict(punch=True,  progress=True, title=False, grade=True, speed=1.05, pitch=1.02, captions=True),
    "podcast":  dict(punch=True,  progress=True, title=False, grade=True, speed=1.05, pitch=1.02, captions=True),
    "irl":      dict(punch=True,  progress=True, title=False, grade=True, speed=1.05, pitch=1.02, captions=True),
    "gameplay": dict(punch=False, progress=True, title=False, grade=True, speed=1.0,  pitch=1.0,  captions=True),
    "music":    dict(punch=False, progress=True, title=False, grade=True, speed=1.0, pitch=1.0,  captions=False),
}


_NOT_GAMES = ("just chatting", "irl", "special events", "talk shows & podcasts", "music", "art", "asmr",
              "pools, hot tubs, and beaches", "sports", "travel & outdoors", "food & drink", "science & technology")


def _chapter_mode(title: str) -> str:
    t = title.lower()
    if t in ("talk shows & podcasts",):
        return "podcast"
    if t in ("music",):
        return "music"
    if t in _NOT_GAMES:
        return "irl"
    return "gameplay"


def mode_at(info: dict, t: float, default: str) -> str:
    """Twitch VODs list the category per stream segment as chapters; use the one at time t."""
    if not info.get("extractor_key", "").lower().startswith("twitch"):
        return default
    for ch in info.get("chapters") or []:
        if ch.get("start_time", 0) <= t < ch.get("end_time", 0):
            return _chapter_mode(ch.get("title") or "")
    return default


def detect_mode(info: dict) -> tuple[str, str]:
    """Return (mode, reason) from yt-dlp metadata only; framing later refines with what it sees."""
    chapters = info.get("chapters") or []
    if info.get("extractor_key", "").lower().startswith("twitch") and chapters:
        by_mode: dict[str, float] = {}
        for ch in chapters:
            by_mode[_chapter_mode(ch.get("title") or "")] = by_mode.get(_chapter_mode(ch.get("title") or ""), 0) \
                + (ch.get("end_time", 0) - ch.get("start_time", 0))
        top = max(by_mode, key=by_mode.get)  # type: ignore[arg-type]
        return top, "Twitch categories: " + ", ".join(dict.fromkeys(ch.get("title") or "" for ch in chapters))
    cats = [c.lower() for c in (info.get("categories") or [])]
    text = " ".join([info.get("title") or "", " ".join(info.get("tags") or []),
                     (info.get("description") or "")[:600], info.get("game") or ""]).lower()
    twitch = info.get("extractor_key", "").lower().startswith("twitch")
    game = (info.get("game") or "").lower()

    if any(w in text for w in _PODCAST_WORDS) and not any(w in text for w in _GAME_WORDS):
        return "podcast", "title/tags mention podcast or interview"
    if twitch and game and game not in ("just chatting", "irl", "special events", "talk shows & podcasts"):
        return "gameplay", f"Twitch category: {info.get('game')}"
    if "gaming" in cats or any(w in text for w in _GAME_WORDS):
        return "gameplay", "gaming category / game names in metadata"
    if "music" in cats:
        return "music", "music category"
    if twitch or info.get("was_live") or any(re.search(rf"\b{re.escape(w)}\b", text) for w in _IRL_WORDS):
        return "irl", "live stream / IRL keywords"
    return "talking", "default"


def style_defaults(mode: str) -> dict:
    return dict(_MODE_DEFAULTS.get(mode, _MODE_DEFAULTS["talking"]))
