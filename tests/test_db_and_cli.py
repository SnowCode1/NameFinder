import pytest

from namefinder import cli, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_ideas_deduplicate_on_normalised_text(conn):
    first = db.add_idea(conn, "Every copy has a parent.", [{"root": "stemma"}], "lineage", "m", "t")
    assert first is not None
    assert db.add_idea(conn, "  every COPY has   a parent. ", [], "origin", "m", "t") is None
    assert [r["text"] for r in db.list_ideas(conn)] == ["Every copy has a parent."]


def test_idea_review_states(conn):
    a = db.add_idea(conn, "idea one", [], "origin", "m", "t")
    b = db.add_idea(conn, "idea two", [], "origin", "m", "t")
    db.set_idea(conn, a, status="approved")
    db.set_idea(conn, b, status="rejected", text="idea two, reworded")
    assert [r["id"] for r in db.list_ideas(conn, "approved")] == [a]
    assert db.list_ideas(conn, "rejected")[0]["text"] == "idea two, reworded"
    assert db.list_ideas(conn, "pending") == []


def test_tournament_matches_round_trip_into_ratings(conn):
    from namefinder import ratings

    for ranking in (["a", "b", "c"], ["a", "c", "b"], ["b", "a", "c"]):
        db.save_tournament_match(conn, "judge|x", 1, ranking, {}, "t")
    rankings = db.tournament_match_rankings(conn, "judge|x")
    assert len(rankings) == 3
    rating = ratings.fit(rankings)[0]
    assert max(rating, key=rating.get) == "a"
    assert db.tournament_match_rankings(conn, "other|y") == []


def test_meta(conn):
    db.set_meta(conn, "k", "v")
    assert db.get_meta(conn, "k") == "v"
    assert db.get_meta(conn, "missing", "d") == "d"


@pytest.mark.parametrize("argv", [
    ["ideas", "generate", "--per-angle", "3", "--angle", "origin"],
    ["generate", "--per-idea", "10", "--no-domains"],
    ["tournament", "--source", "ideas", "--rounds", "2"],
    ["taste", "--eval", "5", "--seed", "1"],
    ["families", "--word", "lacunome"],
    ["top", "--by", "tournament", "-n", "5"],
])
def test_cli_parses_documented_commands(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.cmd == argv[0]


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--help"])
    assert e.value.code == 0
    assert "Recommended workflow" in capsys.readouterr().out
