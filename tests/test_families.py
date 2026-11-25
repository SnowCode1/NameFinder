import random

from namefinder import config, families


def test_sound_key_merges_spellings_of_one_sound():
    assert families.sound_key("fotogram") == families.sound_key("photogram")
    assert families.sound_distance(families.sound_key("fotogram"), families.sound_key("photogram")) == 0


def test_sound_distance_is_cheap_for_near_sounds():
    near = families.sound_distance(families.sound_key("codox"), families.sound_key("codex"))
    far = families.sound_distance(families.sound_key("lacunome"), families.sound_key("codex"))
    assert near < far


def test_similarity():
    assert families.similarity("abc", "abc") == 1.0
    assert families.similarity("fotogram", "photogram") == 1 - 2 / 9


def test_collisions_catch_brand_sound_alikes():
    hits = families.collisions("instagrame", ["instagram", "kodak"], threshold=0.75)
    assert [h.term for h in hits] == ["instagram"]
    assert families.collisions("lacunome", ["instagram", "kodak"], threshold=0.75) == []


def test_collision_terms_parse_comments_and_separators(monkeypatch, tmp_path):
    (tmp_path / "collision_terms.txt").write_text("# brands\ntineye, kodak  truepic\nab  # too short\nfoo-corp\n", "utf-8")
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    # letters-only terms of 3+ characters; hyphenated or short tokens are ignored
    assert families.load_terms() == ["tineye", "kodak", "truepic"]


def test_bans(monkeypatch):
    monkeypatch.setattr(config, "BANNED_ROOTS", ("truth",))
    monkeypatch.setattr(config, "BANNED_PREFIXES", ("deep",))
    monkeypatch.setattr(config, "BANNED_ENDINGS", ("ify",))
    assert families.banned("untruthome") == "truth"
    assert families.banned("deepgram") == "deep-"
    assert families.banned("lacunify") == "-ify"
    assert families.banned("lacunome") is None


def test_family_keys():
    assert families.head("aetherix") == families.head("etherix")   # ae- and e- are one family
    assert families.tail("lacunome") == "-ome"
    assert families.tail("varigram") == "-gram"
    assert families.tail("stemzzq") == "-zzq"                     # unknown ending: last three letters


def test_saturation_caps_a_crowded_family_but_never_closes_it(monkeypatch):
    names = ["stemix", "stemar", "stemop", "corlex", "vanlex", "mirlex", "pelado", "rumova"]
    sat = families.compute_saturation(names, share=0.25, minimum=2)   # cap = 2 of 8
    crowded = dict(sat.saturated)
    assert crowded["stem-"] == 3            # opening: first four letters
    assert crowded["-lex"] == 3             # a known ending
    assert "pela-" not in crowded
    assert sat.hits("stemova") == ["stem-"]
    monkeypatch.setattr(config, "FAMILY_KEEP", 0.0)
    assert sat.admit("stemova", random.Random(0)) is False
    assert sat.admit("lacunome", random.Random(0)) is True
    monkeypatch.setattr(config, "FAMILY_KEEP", 1.0)
    assert sat.admit("stemova", random.Random(0)) is True   # a crowded family is capped, never closed


def test_free_endings_are_never_saturated():
    names = ["alphome", "betome", "gamome", "deltome"]
    sat = families.compute_saturation(names, share=0.25, minimum=2)
    assert "-ome" in config.FAMILY_FREE_ENDINGS
    assert "-ome" not in dict(sat.saturated)
