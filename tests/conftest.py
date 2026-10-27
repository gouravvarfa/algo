import pytest


@pytest.fixture(autouse=True)
def _fresh_api_cache():
    """The options API keeps a short-lived read cache; every test starts with an empty one."""
    from app.options import api
    api._CACHE.clear()
    yield
    api._CACHE.clear()


@pytest.fixture(autouse=True)
def _dryrun_and_shadow_files_stay_in_the_test_folder(tmp_path, monkeypatch):
    """The dry run and the shadow account write under data/paper_trading; a test that drives the engine must never add rows to a developer's real files."""
    from app.paper_trading import dryrun, shadow
    monkeypatch.setattr(dryrun, "DRYRUN_PATH", tmp_path / "dryrun_orders.jsonl")
    monkeypatch.setattr(shadow, "STATE_PATH", tmp_path / "shadow_state.json")
    monkeypatch.setattr(shadow, "LOG_PATH", tmp_path / "shadow_log.jsonl")
    yield


@pytest.fixture(autouse=True)
def _no_platform_users_on_this_machine(tmp_path, monkeypatch):
    """Whether the app asks for a login depends on whether data/platform.db has users. A developer who has created one must not see every
    page test turn into a redirect, so every test starts with its own empty platform database (tests/security brings its own as well)."""
    from app.db import platform_db
    from app.security import mode
    monkeypatch.setattr(platform_db, "DB", tmp_path / "no-platform.db")
    mode.reset()
    yield
    mode.reset()

