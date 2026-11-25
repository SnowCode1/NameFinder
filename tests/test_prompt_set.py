"""The prompt set: file loading, fallbacks, and the shipped worked example."""

import importlib
import os
import subprocess
import sys

from conftest import REPO
from namefinder import config, ideas


def test_load_prompt_falls_back_when_missing_or_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    assert config.load_prompt("product.md", "built-in") == "built-in"
    (tmp_path / "product.md").write_text("   \n", encoding="utf-8")
    assert config.load_prompt("product.md", "built-in") == "built-in"
    (tmp_path / "product.md").write_text("  My brief.  \n", encoding="utf-8")
    assert config.load_prompt("product.md", "built-in") == "My brief."


def test_load_list_strips_comments_and_blanks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    (tmp_path / "x.txt").write_text("# header\nalpha\n\n beta  # note\n#gamma\n", encoding="utf-8")
    assert config.load_list("x.txt") == ("alpha", "beta")
    assert config.load_list("missing.txt", ("d",)) == ("d",)


def test_bans_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    (tmp_path / "bans.txt").write_text("root Proof\nprefix deep-\nending -ify\nnonsense line\nroot\n", "utf-8")
    assert config._load_bans() == {"root": ("proof",), "prefix": ("deep",), "ending": ("ify",)}


def test_angles_file_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    assert ideas._load_angles() == ideas.DEFAULT_ANGLES
    (tmp_path / "angles.txt").write_text("origin | 1.2 | where it began\nbad line\nx | heavy | not a weight\n", "utf-8")
    assert ideas._load_angles() == (("origin", "where it began", 1.2),)


def test_built_in_prompts_are_product_neutral():
    """With an empty prompt set every prompt carries the placeholder brief, not a hard-coded product."""
    from namefinder import evolve, ideanames, score, tournament

    assert "describe yours in prompts/product.md" in config.PRODUCT_BLURB
    for text in (ideas.ideas_prompt(), ideanames.names_prompt(), evolve.evolve_prompt(),
                 tournament.judge_prompt(), score.BRAND_SYSTEM_PROMPT, score.ROOT_SYSTEM_PROMPT):
        assert config.PRODUCT_BLURB in text
        assert "{product}" not in text


def test_worked_example_loads():
    """Import config in a fresh interpreter pointed at examples/image-provenance."""
    code = ("from namefinder import config, ideas, families; "
            "print(len(ideas.ANGLES), len(config.BANNED_ROOTS), len(config.EMBED_CATEGORY_ANCHORS), "
            "'tineye' in families.load_terms(), config.PRODUCT_BLURB.startswith('An image provenance tool'))")
    env = dict(os.environ, NAMEFINDER_PROMPTS=str(REPO / "examples" / "image-provenance"))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["9", "6", "7", "True", "True"]


def test_example_taste_file_is_valid():
    from namefinder import taste

    labels, _ = taste.load(REPO / "examples" / "image-provenance" / "taste.txt")
    assert len(labels) >= 30
    assert set(labels.values()) == {1, 2, 3, 4}
    assert taste.held_out(REPO / "examples" / "image-provenance" / "taste.txt")


def test_every_module_imports():
    import pkgutil

    import namefinder

    for m in pkgutil.iter_modules(namefinder.__path__):
        importlib.import_module(f"namefinder.{m.name}")
