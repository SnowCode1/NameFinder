import math
import random

import pytest

from namefinder import ratings


def test_empty_input():
    assert ratings.fit([]) == ({}, {}, {})


def test_consistent_rankings_order_the_names():
    matches = [["a", "b", "c"]] * 5 + [["b", "c"]] * 5
    rating, se, games = ratings.fit(matches)
    assert rating["a"] > rating["b"] > rating["c"]
    assert games == {"a": 5, "b": 10, "c": 10}
    assert all(v > 0 for v in se.values())


def test_ratings_are_centred_on_the_base():
    matches = [["a", "b", "c", "d"], ["b", "a", "d", "c"], ["a", "c", "b", "d"]]
    rating, _, _ = ratings.fit(matches)
    # strengths are normalised to a geometric mean of 1, i.e. an average rating of BASE
    assert sum(rating.values()) / len(rating) == pytest.approx(ratings.BASE)


def test_prior_keeps_unbeaten_names_finite():
    rating, se, _ = ratings.fit([["a", "b"]] * 20)
    assert math.isfinite(rating["a"]) and math.isfinite(rating["b"])
    assert math.isfinite(se["a"])


def test_more_games_shrink_the_standard_error():
    few = ratings.fit([["a", "b", "c"], ["c", "b", "a"]])[1]
    many = ratings.fit([["a", "b", "c"], ["c", "b", "a"]] * 20)[1]
    assert many["b"] < few["b"]


def test_result_does_not_depend_on_match_order():
    rng = random.Random(7)
    names = list("abcdefgh")
    matches = [rng.sample(names, 5) for _ in range(60)]
    first = ratings.fit(matches)[0]
    shuffled = matches[:]
    rng.shuffle(shuffled)
    second = ratings.fit(shuffled)[0]
    for n in names:
        assert first[n] == pytest.approx(second[n], abs=1e-3)


def test_warm_start_converges_to_the_same_fit():
    matches = [["a", "b", "c"], ["b", "a", "c"], ["a", "c", "b"]] * 3
    cold = ratings.fit(matches)[0]
    warm = ratings.fit(matches, init={"a": 1900, "b": 1200, "c": 1500})[0]
    for n in cold:
        assert warm[n] == pytest.approx(cold[n], abs=1e-2)


def test_recovers_true_strength_order():
    """Simulate Plackett-Luce rankings from known strengths; the fit should recover their order."""
    rng = random.Random(1)
    true = {n: s for n, s in zip("abcdef", [8.0, 4.0, 2.0, 1.0, 0.5, 0.25])}

    def draw(group):
        left, out = list(group), []
        while left:
            pick = rng.choices(left, weights=[true[x] for x in left])[0]
            out.append(pick)
            left.remove(pick)
        return out

    matches = [draw(rng.sample(list(true), 4)) for _ in range(400)]
    rating = ratings.fit(matches)[0]
    assert sorted(rating, key=rating.get, reverse=True) == list("abcdef")
