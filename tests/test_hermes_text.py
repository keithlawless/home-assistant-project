from taco.voice.hermes import asks_question, clean_for_speech, split_sentences


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


def test_asks_question():
    assert asks_question("Which city did you mean?")
    assert asks_question("I couldn't get that forecast. Which state is it in? I'll try again with that.")
    assert not asks_question("Why not? Tomatoes like heat. Water them daily.")
    assert not asks_question("It's 57 degrees.")
    assert not asks_question("")
