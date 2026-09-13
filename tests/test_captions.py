import re
import json

from cliper import captions


def test_json3_words_and_ass(tmp_path):
    j = {"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 800}]},
        {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 6000, "dDurationMs": 1500, "segs": [{"utf8": "again"}]},
    ]}
    p = tmp_path / "s.json3"
    p.write_text(json.dumps(j))
    words = captions.words_from_json3(p)
    assert [w[2] for w in words] == ["hello", "world", "again"]
    assert words[1][0] == 1.8

    starts, ends = captions.sentence_boundaries(words)
    assert starts == [1.0, 6.0]

    ass = captions.build_ass(words, clip_start=0.5, clip_end=8, vertical=True, title="hello", progress=False)
    plain = re.sub(r"\{[^}]*\}", "", ass)
    assert "PlayResX: 1080" in ass
    assert "HELLO WORLD" in plain and "AGAIN" in plain
    assert "Title,,0,0,0,,HELLO" in plain
    assert ass.count(",Cap,") == 3  # one event per highlighted word
    assert "Bar" not in ass.split("[Events]")[1]

    withbar = captions.build_ass(words, clip_start=0.5, clip_end=8, vertical=True, progress=True, speed=1.5)
    assert withbar.count(",Bar,") == 50  # 7.5s / 1.5 = 5s at 0.1s steps
