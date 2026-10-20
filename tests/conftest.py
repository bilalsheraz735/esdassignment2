"""Test setup: temp database, temp log file, near-instant kitchen. Must run before app imports."""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="esd-tests-")
os.environ["ORDERS_DB"] = os.path.join(_tmp, "orders.db")
os.environ["LOG_FILE"] = os.path.join(_tmp, "app-logs.json")
os.environ["COOK_TIME_MIN"] = "0.05"
os.environ["COOK_TIME_MAX"] = "0.1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import order_core  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    order_core.reset_db()
    import app
    app.RATE_LIMITER.reset()
    yield


@pytest.fixture
def http():
    import app
    return app.app.test_client()
