import pytest


@pytest.fixture(autouse=True)
def _isolated_expenser_dirs(tmp_path_factory, monkeypatch):
    """Never touch the real ~/.expenser or ~/Expenser from tests."""
    base = tmp_path_factory.mktemp("expenser-home")
    monkeypatch.setenv("EXPENSER_HOME", str(base / "state"))
    monkeypatch.setenv("EXPENSER_OUTPUT_DIR", str(base / "out"))
