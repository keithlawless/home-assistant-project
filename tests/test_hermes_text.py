from taco.voice.hermes import clean_for_speech, split_sentences


def test_split_keeps_remainder():
    done, rest = split_sentences("Basil is great with tomatoes. Also try ore")
    assert done == ["Basil is great with tomatoes."]
    assert rest == "Also try ore"


def test_short_fragments_merge_with_next_sentence():
    done, rest = split_sentences("Yes. It is about 68 degrees today. Win")
    assert done == ["Yes. It is about 68 degrees today."]
    assert rest == "Win"


def test_no_boundary_returns_nothing():
    assert split_sentences("Still going") == ([], "Still going")


def test_clean_markdown():
    text = "**Basil** is [great](https://x.com) with `tomatoes`.\n- Oregano too"
    assert clean_for_speech(text) == "Basil is great with tomatoes. Oregano too"
