from cliper import seo


def test_generate_clip_seo():
    clip = {
        "rank": 1,
        "start": 10.0,
        "end": 25.0,
        "hook": "Wait what happened right there?",
        "text": "Wait what happened right there? That was insane, bro!",
    }
    info = {
        "title": "Unbelievable Gaming Challenge #45",
        "uploader": "Gamer Pro",
        "tags": ["gaming", "challenge", "funny"],
    }
    res = seo.generate_clip_seo(clip, info)
    assert "title" in res
    assert "description" in res
    assert "tags" in res
    assert "hashtags" in res
    assert "Wait what happened right there" in res["title"]
    assert "#Shorts" in res["hashtags"]
    assert "#GamerPro" in res["hashtags"]
    assert "gamer pro" in res["tags"]
