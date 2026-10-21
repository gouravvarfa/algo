import pytest


@pytest.fixture(autouse=True)
def _fresh_api_cache():
    """The options API keeps a short-lived read cache; every test starts with an empty one."""
    from app.options import api
    api._CACHE.clear()
    yield
    api._CACHE.clear()
