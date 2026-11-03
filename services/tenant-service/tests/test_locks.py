"""The locks on Postgres, as two replicas would hold them (CI runs these against a Postgres service).

    TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres pytest tests/test_locks.py
"""

import os
import threading
import time

import pytest
from sqlalchemy import create_engine

from tenant_service.history import Locks

PG = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG, reason="TEST_POSTGRES_URL not set")


@pytest.fixture
def replicas():
    """Two services, each with its own engine and process locks, sharing one database."""
    engines = [create_engine(PG), create_engine(PG)]
    yield [Locks(e) for e in engines]
    for e in engines:
        e.dispose()


def test_a_sync_is_skipped_while_another_replica_runs_one(replicas):
    a, b = replicas
    with a.try_hold("sync") as got_a:
        assert got_a is True
        with b.try_hold("sync") as got_b:
            assert got_b is False
    with b.try_hold("sync") as got_b:                       # released
        assert got_b is True


def test_an_admin_change_waits_for_the_other_replicas(replicas):
    a, b = replicas
    order = []

    def other():
        with b.hold("admins"):
            order.append("b")

    with a.hold("admins"):
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.5)
        order.append("a")
    t.join(5)
    assert order == ["a", "b"]


def test_locks_with_different_names_dont_block(replicas):
    a, b = replicas
    with a.hold("admins"):
        with b.try_hold("sync") as got:
            assert got is True
