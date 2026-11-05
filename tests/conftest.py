from pathlib import Path

import pytest

from pwa_overlay.dictionary import Dictionary

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def dictionary() -> Dictionary:
    return Dictionary.from_dir(ROOT / "data" / "mappings")
