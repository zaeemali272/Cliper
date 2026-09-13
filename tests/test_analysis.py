import numpy as np

from cliper import analysis


def _signal():
    sig = np.zeros(600, dtype=np.float32)
    for centre, height in ((100, 1.0), (300, 0.7), (500, 0.4)):
        sig[centre - 10: centre + 10] = height
    return sig


def test_pick_clips_are_ranked_and_non_overlapping():
    clips = analysis.pick_clips(_signal(), count=3, min_len=15, max_len=60)
    assert [c["rank"] for c in clips] == [1, 2, 3]
    assert clips[0]["start"] <= 100 <= clips[0]["end"]
    for c in clips:
        assert 15 <= c["end"] - c["start"] <= 63
    spans = sorted((c["start"], c["end"]) for c in clips)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_pick_clips_fills_when_few_peaks():
    clips = analysis.pick_clips(_signal(), count=8, min_len=20, max_len=30)
    assert len(clips) == 8


def test_highlights_spaced():
    hs = analysis.highlights(_signal(), 5)
    assert hs[0]["time"] in range(88, 112)
    times = [h["time"] for h in hs]
    assert all(abs(a - b) >= 60 for i, a in enumerate(times) for b in times[i + 1:])


def test_downsample_length():
    assert len(analysis.downsample(np.linspace(0, 1, 5000, dtype=np.float32), 240)) == 240
