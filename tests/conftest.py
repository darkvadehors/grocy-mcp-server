import pytest

from tests.fake_grocy import FakeGrocy


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake():
    return FakeGrocy()
