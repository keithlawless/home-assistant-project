from taco.voice.stt import MAX_VOCABULARY, build_vocabulary

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
