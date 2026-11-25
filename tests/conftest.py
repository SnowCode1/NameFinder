"""Run every test against an empty prompt set and a throwaway database.

`namefinder.config` reads the prompt folder at import time, so the environment is set here, before any
test module imports the package. Tests never see a developer's private prompts or their database.
"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="namefinder-tests-"))
(_TMP / "prompts").mkdir()
os.environ["NAMEFINDER_PROMPTS"] = str(_TMP / "prompts")
os.environ["NAMEFINDER_DB"] = str(_TMP / "test.db")

REPO = Path(__file__).resolve().parent.parent
