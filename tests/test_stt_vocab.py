from taco.voice.stt import MAX_VOCABULARY, build_vocabulary, echoes_prompt

HOME = {"name": "Northborough, Massachusetts", "lat": 42.3, "lon": -71.6}


def test_home_town_comes_first():
    assert build_vocabulary(HOME, ["Shrewsbury"]) == ["Northborough", "Shrewsbury"]


def test_no_location():
    assert build_vocabulary(None, ["Shrewsbury"]) == ["Shrewsbury"]
    assert build_vocabulary(None, []) == []


def test_duplicates_and_blanks_dropped():
    assert build_vocabulary(HOME, ["northborough", " ", "Westborough "]) == ["Northborough", "Westborough"]


def test_capped():
    assert len(build_vocabulary(HOME, [f"Town{i}" for i in range(100)])) == MAX_VOCABULARY


def test_prompt_echo_dropped():
    vocab = ["Springfield", "Riverton"]
    assert echoes_prompt("Springfield", vocab)        # a stuck mic, 2026-11-21
    assert echoes_prompt("Springfield.", vocab)
    assert echoes_prompt("Hey Taco.", vocab)
    assert echoes_prompt("Hey Taco, Springfield, Riverton.", vocab)


def test_real_requests_kept():
    vocab = ["Springfield"]
    assert not echoes_prompt("What's the weather in Springfield?", vocab)
    assert not echoes_prompt("Turn off the bedroom.", vocab)
    assert not echoes_prompt("", vocab)
