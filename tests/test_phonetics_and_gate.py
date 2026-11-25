import pytest

from namefinder import coin, phonetics


@pytest.mark.parametrize("word, run", [("traceln", "ln"), ("sourcr", "rcr"), ("tsktsk", "tsktsk")])
def test_unreadable_endings(word, run):
    assert phonetics.bad_ending(word) == run


@pytest.mark.parametrize("word", ["prism", "lands", "world", "lacunome", "codex", "match"])
def test_legal_endings(word):
    assert phonetics.bad_ending(word) is None


def test_syllables():
    assert phonetics.syllabify("lacunome") == ["la", "cu", "nome"]
    assert len(phonetics.syllabify("prism")) == 1


def test_unreadable_names_score_lower():
    assert phonetics.analyze("traceln").score < phonetics.analyze("lacunome").score


def test_phonetic_gate_bounds():
    assert phonetics.phonetic_gate(1.0) == 1.0
    assert phonetics.phonetic_gate(0.0, weight=0.3) == pytest.approx(0.7)
    assert phonetics.phonetic_gate(5.0) == 1.0


@pytest.mark.parametrize("word", ["lacunome", "varigram", "progeneme"])
def test_gate_accepts_term_like_coinages(word):
    assert coin.acceptable(word)


@pytest.mark.parametrize("word, why", [
    ("table", "an ordinary dictionary word"),
    ("xkqzrt", "unpronounceable"),
    ("fuckome", "a crude fragment"),
    ("abc", "too short"),
])
def test_gate_rejects(word, why):
    assert not coin.acceptable(word), why


def test_one_edit_neighbours():
    n = coin.one_edit_neighbours("cat")
    assert {"at", "cats", "bat", "cot"} <= n
