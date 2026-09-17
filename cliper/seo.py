"""SEO metadata generator for video clips (Title, Description, Tags, Hashtags)."""
from __future__ import annotations

import re


def _clean_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _slugify_tag(s: str) -> str:
    cleaned = re.sub(r"[^\w\s]", "", s).strip()
    return "".join(part.capitalize() for part in cleaned.split())


def generate_clip_seo(clip: dict, info: dict) -> dict:
    """Generate YouTube/Shorts SEO metadata for a clip."""
    video_title = (info.get("title") or "Viral Video").strip()
    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    hook = _clean_text(clip.get("hook") or "")
    text = _clean_text(clip.get("text") or "")
    rank = clip.get("rank", 1)

    # 1. Optimized Title
    if hook:
        short_hook = hook.strip('".?! ')
        if len(short_hook) > 55:
            short_hook = short_hook[:52] + "..."
        opt_title = f"{short_hook} 😱🔥"
    elif text:
        first_phrase = text.split(".")[0].split("?")[0].strip()
        if len(first_phrase) > 55:
            first_phrase = first_phrase[:52] + "..."
        opt_title = f"{first_phrase} 💥"
    else:
        opt_title = f"Unbelievable Moment #{rank} in {video_title[:45]} 🔥"

    # 2. Recommended Hashtags
    hashtags = ["#Shorts", "#Viral", "#Trending", "#Reels", "#FYP"]
    if uploader:
        tag_uploader = "#" + _slugify_tag(uploader)
        if tag_uploader and len(tag_uploader) > 1 and tag_uploader not in hashtags:
            hashtags.append(tag_uploader)

    title_words = [w.capitalize() for w in re.findall(r"\b[A-Za-z]{4,}\b", video_title)
                   if w.lower() not in ("with", "from", "that", "this", "have", "video", "full", "part")]
    for w in title_words[:2]:
        ht = "#" + w
        if ht not in hashtags and len(hashtags) < 8:
            hashtags.append(ht)

    hashtags_str = " ".join(hashtags)

    # 3. High-Converting Description
    desc_lines = [
        f"🔥 {opt_title}",
        "",
        f"“{text if text else 'Watch this epic moment from ' + video_title}”",
        "",
        f"📍 Original video: {video_title}" + (f" by {uploader}" if uploader else ""),
        "👉 Subscribe for more daily viral clips & highlights!",
        "",
        hashtags_str,
    ]
    description = "\n".join(desc_lines)

    # 4. Optimized Tags (For YouTube SEO)
    raw_tags = info.get("tags") or []
    tags = ["shorts", "youtube shorts", "viral shorts", "trending clips", "reels", "tiktok"]
    if uploader:
        tags.append(uploader.lower())

    clip_words = re.findall(r"\b[A-Za-z]{3,}\b", (hook + " " + text).lower())
    for w in clip_words:
        if w not in tags and len(tags) < 15 and w not in ("you", "the", "and", "that", "this", "what", "with", "from", "have"):
            tags.append(w)

    for t in raw_tags:
        if isinstance(t, str):
            t_clean = t.strip().lower()
            if t_clean and t_clean not in tags and len(tags) < 20:
                tags.append(t_clean)

    return {
        "title": opt_title,
        "description": description,
        "tags": ", ".join(tags),
        "hashtags": hashtags_str,
    }
