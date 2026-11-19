import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_json():
    return lambda name: json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests away from the real cache, state and decisions files."""
    from lgtv_mcp.feedwall import store, wall

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(store, "DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(wall, "DECISIONS_FILE", tmp_path / "decisions.json")
    return tmp_path
