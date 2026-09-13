import numpy as np

from cliper import analysis, clipper, framing, modes


def test_transcript_clips_align_to_phrases():
    sig = np.zeros(300, dtype=np.float32)
    sig[95:110] = 1.0
    words = []
    t = 90.0
    for i in range(40):  # 40 short words, a pause every 5 -> phrases of ~2.5s
        words.append((t, t + 0.4, "what?" if i % 5 == 4 else "word"))
        t += 0.5 if i % 5 != 4 else 1.2
    clips = analysis.pick_clips_transcript(sig, words, count=2, min_len=6, max_len=15)
    assert clips and clips[0]["rank"] == 1
    top = clips[0]
    assert 6 <= top["end"] - top["start"] <= 15.5
    assert top["text"] and top["phrases"]
    assert top["start"] <= 100 <= top["end"]  # covers the peak


def test_modes():
    assert modes.detect_mode({"title": "My Podcast Ep. 12 with X"})[0] == "podcast"
    assert modes.detect_mode({"categories": ["Gaming"], "title": "x"})[0] == "gameplay"
    assert modes.detect_mode({"extractor_key": "TwitchVod", "game": "Just Chatting", "title": "x"})[0] == "irl"
    assert modes.style_defaults("music")["captions"] is False


def test_framing_plans():
    one = [[(0.3, 0.5, 0.2, 0.35)]] * 10
    p = framing.plan("talking", one)
    assert p["type"] == "crop" and abs(p["track"][0][1] - 0.3) < 0.01
    two = [[(0.25, 0.5, 0.2, 0.35), (0.75, 0.5, 0.2, 0.35)]] * 10
    assert framing.plan("podcast", two)["type"] == "split"
    corner = [[(0.9, 0.85, 0.08, 0.14)]] * 10
    assert framing.plan("gameplay", corner)["type"] == "stack_game"
    assert framing.plan("gameplay", [[]] * 10)["type"] == "blur"
    assert framing.plan("talking", [[]] * 10)["type"] == "crop_center"


def test_filter_strings_build():
    plan = {"type": "crop", "track": [(0.0, 0.3), (2.0, 0.6)]}
    style = {"speed": 1.05, "pitch": 1.02, "grade": True, "punch": True, "mirror": True}
    vf = clipper.build_video_filter(plan, style, "1.ass", [(1.0, 2.0)])
    for piece in ("setpts=PTS/1.0500", "fps=30", "crop=w=min(iw\\,ih*9/16)", "zoompan", "hflip", "ass=1.ass"):
        assert piece in vf
    af = clipper.build_audio_filter(style)
    assert af.startswith("aresample=48000,asetrate=48960,") and "atempo=1.0500" in af and "loudnorm" in af
    assert "vstack" in clipper.layout_filter({"type": "split", "cx": [0.2, 0.8]})
    assert "vstack" in clipper.layout_filter({"type": "stack_game", "cam": (0.7, 0.7, 0.3, 0.3)})
