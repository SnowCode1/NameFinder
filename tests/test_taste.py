from namefinder import taste

TASTE = """\
# comment line
lineadex     A   # an index of lines of descent
lacunagram   B   # true story [held out]
varigram     C
hiatome      D   # surgical
pending      ?   # not rated yet
"""


def _file(tmp_path):
    p = tmp_path / "taste.txt"
    p.write_text(TASTE, encoding="utf-8")
    return p


def test_load_reads_tiers_and_reasons(tmp_path):
    labels, reasons = taste.load(_file(tmp_path))
    assert labels == {"lineadex": 4, "lacunagram": 3, "varigram": 2, "hiatome": 1}
    assert reasons["hiatome"] == "surgical"
    assert "varigram" not in reasons


def test_missing_file_is_empty(tmp_path):
    assert taste.load(tmp_path / "nope.txt") == ({}, {})


def test_held_out_names(tmp_path):
    assert taste.held_out(_file(tmp_path)) == {"lacunagram"}


def test_examples_never_include_held_out_names(tmp_path):
    for _ in range(20):
        best, worst = taste.examples(_file(tmp_path))
        assert "lacunagram" not in best
        assert best == ["lineadex"]
        assert worst == ["hiatome"]


def test_agreement_perfect_reversed_and_ties():
    labels = {"a": 4, "b": 3, "c": 2, "d": 1}
    same = {"a": 4.0, "b": 3.0, "c": 2.0, "d": 1.0}
    assert taste.agreement(labels, same)[:3] == (1.0, 6, 4)
    reversed_ = {k: -v for k, v in same.items()}
    assert taste.agreement(labels, reversed_)[0] == 0.0
    flat = {k: 0.0 for k in same}
    assert taste.agreement(labels, flat)[0] == 0.5


def test_agreement_skips_same_tier_and_unscored_names():
    labels = {"a": 4, "b": 4, "c": 1, "x": 2}
    share, pairs, names, _ = taste.agreement(labels, {"a": 1.0, "b": 2.0, "c": 0.0})
    assert (share, pairs, names) == (1.0, 2, 3)


def test_worst_misses_come_first():
    labels = {"a": 4, "b": 3, "d": 1}
    scores = {"a": 0.0, "b": 0.5, "d": 1.0}       # every pair is wrong
    misses = taste.agreement(labels, scores)[3]
    assert misses[0] == ("a", "d")               # the A name losing to the D name is the biggest gap
    assert set(misses) == {("a", "d"), ("a", "b"), ("b", "d")}
